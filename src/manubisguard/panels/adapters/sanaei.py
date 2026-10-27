"""Sanaei/3x-ui adapter with explicit legacy-cookie and token API modes.

The legacy API is the familiar 3x-ui inbound API. The token API follows the optional
client endpoints used by the audited Faoxima integration. Capabilities are conservative:
we never advertise operations that this adapter cannot verify for the selected mode.
"""
from __future__ import annotations
from datetime import datetime, timezone
import json
import re
from typing import Any, Callable
from urllib.parse import quote, urlsplit
from uuid import UUID, uuid4

from ..contracts import (PanelCapabilities, PanelConnection, PanelError, PanelUser, PanelUserNotFound, UserCreate,
                         UserUpdate, local_capability_report)


class SanaeiAdapter:
    kind = "sanaei"

    def __init__(self, secret_resolver: Callable[[str], str] | None = None,
                 client_factory: Callable[..., Any] | None = None):
        self._secret_resolver = secret_resolver or (lambda ref: "")
        self._client_factory = client_factory

    def _credentials(self, connection: PanelConnection) -> tuple[str, str | None]:
        parts = urlsplit(connection.base_url)
        if not connection.enabled:
            raise PanelError("Sanaei connection is disabled")
        if not connection.verify_tls or parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
            raise PanelError("Sanaei requires HTTPS, verified TLS and a URL without embedded credentials")
        secret = self._secret_resolver(connection.secret_ref)
        if not secret:
            raise PanelError("Sanaei credential is not configured")
        if connection.auth_mode == "access_token":
            return "token", secret
        if connection.auth_mode == "username_password":
            if not str(connection.metadata.get("username", "")).strip():
                raise PanelError("Legacy Sanaei auth requires metadata.username")
            return "legacy", secret
        raise PanelError("Sanaei supports access_token or username_password auth mode")

    async def _request(self, connection: PanelConnection, method: str, path: str,
                       *, payload: dict[str, Any] | None = None,
                       user_lookup: bool = False) -> Any:
        mode, secret = self._credentials(connection)
        headers = {"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"}
        try:
            factory = self._client_factory
            if factory is None:
                try:
                    import httpx
                except ImportError as exc:
                    raise PanelError("httpx is required for panel API requests") from exc
                factory = httpx.AsyncClient
            async with factory(base_url=connection.base_url.rstrip("/"),
                                            timeout=connection.timeout_seconds, verify=True,
                                            follow_redirects=False) as client:
                if mode == "token":
                    headers["Authorization"] = f"Bearer {secret}"
                else:
                    login = await client.post("/login", data={
                        "username": str(connection.metadata["username"]), "password": secret,
                    }, headers=headers)
                    if login.status_code < 200 or login.status_code >= 300:
                        raise PanelError(f"Sanaei login returned HTTP {login.status_code}")
                    try: login_data = login.json()
                    except ValueError: login_data = {}
                    if isinstance(login_data, dict) and login_data.get("success") is False:
                        raise PanelError("Sanaei login was rejected")
                response = await client.request(method, path, json=payload, headers=headers)
            if response.status_code == 404 and user_lookup:
                raise PanelUserNotFound("Sanaei user was not found")
            if response.status_code < 200 or response.status_code >= 300:
                raise PanelError(f"Sanaei returned HTTP {response.status_code}")
            try: result = response.json() if response.content else {}
            except ValueError: raise PanelError("Sanaei returned invalid JSON") from None
            if isinstance(result, dict) and result.get("success") is False:
                raise PanelError("Sanaei rejected the operation")
            return result
        except PanelError:
            raise
        except Exception as exc:
            raise PanelError(f"Sanaei transport failed ({type(exc).__name__})") from None

    @staticmethod
    def _obj(result: Any) -> dict[str, Any]:
        if not isinstance(result, dict):
            raise PanelError("Sanaei returned an unexpected response")
        obj = result.get("obj", result)
        if isinstance(obj, dict): return obj
        if obj is None and result.get("success") is True: return {}
        raise PanelError("Sanaei response omitted object data")

    @staticmethod
    def _safe_subscription_url(value: Any) -> str | None:
        if not isinstance(value, str) or len(value) > 4096 or any(ord(ch) < 32 for ch in value):
            return None
        candidate = value.strip()
        try:
            parsed = urlsplit(candidate)
            if (parsed.scheme.lower() != "https" or not parsed.hostname
                    or parsed.username or parsed.password):
                return None
            _ = parsed.port
        except ValueError:
            return None
        return candidate

    @staticmethod
    def _subscription_server_config(connection: PanelConnection) -> tuple[str, str] | None:
        metadata = connection.metadata if isinstance(connection.metadata, dict) else {}
        base = metadata.get("subscription_base_url")
        path = metadata.get("subscription_path")
        if not isinstance(base, str) or not isinstance(path, str):
            return None
        if not base or len(base) > 2048 or any(ord(ch) < 32 for ch in base):
            return None
        try:
            parsed = urlsplit(base.strip())
            if (parsed.scheme.lower() != "https" or not parsed.hostname
                    or parsed.username or parsed.password or parsed.path not in ("", "/")
                    or parsed.query or parsed.fragment):
                return None
            _ = parsed.port
        except ValueError:
            return None
        path = path.strip().strip("/")
        if (not re.fullmatch(r"[A-Za-z0-9._~-]{1,128}", path)
                or path in {".", ".."}):
            return None
        return f"https://{parsed.netloc}", path

    @classmethod
    def _map(cls, obj: dict[str, Any], base_url: str) -> PanelUser:
        client = obj.get("client") if isinstance(obj.get("client"), dict) else obj
        username = str(client.get("email") or client.get("username") or "")
        if not username:
            raise PanelError("Sanaei response omitted client email")
        expire_ms = client.get("expiryTime")
        expire = None
        if isinstance(expire_ms, bool):
            raise PanelError("Sanaei returned an invalid expiry")
        if isinstance(expire_ms, (int, float)) and expire_ms > 0:
            # 3x-ui stores client expiry as milliseconds since Unix epoch.
            try:
                expire = datetime.fromtimestamp(float(expire_ms) / 1000, timezone.utc)
            except (OverflowError, OSError, ValueError):
                raise PanelError("Sanaei returned an invalid expiry") from None
        total_gb = client.get("totalGB")
        total_bytes = None
        if total_gb is not None:
            try:
                if isinstance(total_gb, bool):
                    raise ValueError
                total_gb_value = int(total_gb)
                if total_gb_value < 0:
                    raise ValueError
                total_bytes = total_gb_value * (1024 ** 3)
            except (TypeError, ValueError, OverflowError):
                raise PanelError("Sanaei returned an invalid totalGB limit") from None
        used_value = obj.get("used_traffic", 0)
        if isinstance(used_value, bool):
            raise PanelError("Sanaei returned an invalid usage counter")
        try:
            used = int(used_value or 0)
        except (TypeError, ValueError, OverflowError):
            raise PanelError("Sanaei returned an invalid usage counter") from None
        if not used:
            up_value, down_value = client.get("up", 0), client.get("down", 0)
            if isinstance(up_value, bool) or isinstance(down_value, bool):
                raise PanelError("Sanaei returned an invalid usage counter")
            try:
                used = int(up_value or 0) + int(down_value or 0)
            except (TypeError, ValueError, OverflowError):
                raise PanelError("Sanaei returned an invalid usage counter") from None
        if used < 0:
            raise PanelError("Sanaei returned a negative usage counter")
        # The clients/links endpoint returns protocol share URIs, not the HTTPS
        # subscription URL consumed by MANUBIS's service/QR contract.
        sub_candidate = (obj.get("subscription_url") or obj.get("subscription_link")
                         or client.get("subscription_url") or client.get("subscription_link"))
        sub = cls._safe_subscription_url(sub_candidate)
        return PanelUser(provider_user_id=username, username=username,
            status="active" if client.get("enable", True) else "disabled",
            used_bytes=int(used) if used is not None else None,
            data_limit_bytes=total_bytes,
            expire_at=expire, subscription_url=sub,
            raw_status=str(client.get("enable", "unknown")),
            metadata={"inbound_ids": sorted(int(x) for x in obj.get("inboundIds", []) if str(x).isdigit()),
                      "uuid": client.get("id"), "sub_id": client.get("subId") or obj.get("subId"),
                      "ip_limit": client.get("limitIp"), "flow": client.get("flow"),
                      "protocol": obj.get("protocol")})

    async def capability_report(self, connection: PanelConnection) -> dict[str, Any]:
        """Report adapter coverage without making a panel request or claiming permission."""
        validation_error = None
        try:
            self._credentials(connection)
        except Exception as exc:
            validation_error = type(exc).__name__
        methods = {
            "health_check": "health_check", "read_user": "get_user", "list_users": "list_users",
            "create_user": "create_user", "update_user": "update_user", "delete_user": "delete_user",
            "read_usage": "get_usage", "subscription_link": "get_subscription_url",
            "suspend_user": "update_user", "activate_user": "update_user",
            "manage_resellers": None, "reset_usage": None,
        }
        return local_capability_report(self.kind,
            {name: bool(method and callable(getattr(self, method, None))) for name, method in methods.items()},
            validation_error_type=validation_error)

    async def capabilities(self, connection: PanelConnection) -> PanelCapabilities:
        mode, _ = self._credentials(connection)
        # The clients/links endpoint returns direct protocol URIs. A real HTTPS
        # subscription URL can only be built when its separate server origin and
        # configured subPath are explicitly supplied in connection metadata.
        subscription_link = mode == "token" and self._subscription_server_config(connection) is not None
        return PanelCapabilities(create_user=True, delete_user=True, update_user=True,
            read_usage=True, read_status=True, suspend_user=True, activate_user=True,
            subscription_link=subscription_link, list_users=True, manage_resellers=False,
            health_check=True, sync=False)

    async def health_check(self, connection: PanelConnection) -> dict[str, Any]:
        try:
            await self._request(connection, "GET", "/panel/api/inbounds/list")
            return {"ok": True, "panel": self.kind, "connection_id": connection.id,
                    "checked_at": datetime.now(timezone.utc).isoformat()}
        except Exception as exc:
            return {"ok": False, "panel": self.kind, "connection_id": connection.id,
                    "error_type": type(exc).__name__}

    async def _find_client(self, connection: PanelConnection, username: str) -> tuple[dict[str, Any], dict[str, Any]]:
        mode, _ = self._credentials(connection)
        if mode == "token":
            result = await self._request(connection, "GET", f"/panel/api/clients/get/{quote(username, safe='')}", user_lookup=True)
            obj = self._obj(result)
            return obj, obj.get("client", obj)
        result = await self._request(connection, "GET", "/panel/api/inbounds/list")
        raw_obj = result.get("obj", result) if isinstance(result, dict) else None
        inbounds = raw_obj if isinstance(raw_obj, list) else (raw_obj.get("inbounds", raw_obj.get("items", [])) if isinstance(raw_obj, dict) else [])
        if not isinstance(inbounds, list):
            raise PanelError("Sanaei inbound response did not contain a list")
        matches: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for inbound in inbounds:
            if not isinstance(inbound, dict): continue
            settings = inbound.get("settings", {})
            if isinstance(settings, str):
                try: settings = json.loads(settings)
                except ValueError: settings = {}
            clients = settings.get("clients", []) if isinstance(settings, dict) else []
            if not isinstance(clients, list): continue
            for client in clients:
                if isinstance(client, dict) and str(client.get("email") or "") == username:
                    matches.append((inbound, client))
        if not matches:
            raise PanelUserNotFound("Sanaei client was not found")
        identities = {self._legacy_identity_key(client, inbound.get("protocol")) for inbound, client in matches}
        if len(identities) != 1:
            raise PanelError("Legacy Sanaei client identity is ambiguous across inbounds")
        inbound_ids = []
        for inbound, _client in matches:
            inbound_id = self._legacy_inbound_id(inbound.get("id"))
            if inbound_id not in inbound_ids:
                inbound_ids.append(inbound_id)
        first_inbound, first_client = matches[0]
        return {"inboundIds": sorted(inbound_ids), "protocol": str(first_inbound.get("protocol") or "").lower(),
                "client": first_client}, first_client

    @staticmethod
    def _legacy_inbound_id(value: Any) -> int:
        if isinstance(value, bool):
            raise PanelError("Legacy Sanaei client has an invalid inbound ID")
        if isinstance(value, int) and value > 0:
            return value
        if isinstance(value, str) and re.fullmatch(r"[1-9][0-9]*", value.strip()):
            return int(value.strip())
        raise PanelError("Legacy Sanaei client has an invalid inbound ID")

    @staticmethod
    def _legacy_identity_key(client: dict[str, Any], protocol: Any) -> tuple[str, str, str, str, str]:
        """Identify a legacy client without exposing credential values in errors."""
        return (str(protocol or "").lower(), str(client.get("email") or ""),
                str(client.get("id") or ""), str(client.get("password") or ""),
                str(client.get("subId") or ""))

    @staticmethod
    def _legacy_client_identifier(inbound: dict[str, Any], client: dict[str, Any]) -> str:
        """Use the clientId documented by 3x-ui for the inbound's protocol."""
        protocol = str(inbound.get("protocol") or "").lower()
        field = {"vless": "id", "vmess": "id", "trojan": "password", "shadowsocks": "email"}.get(protocol)
        if field is None:
            raise PanelError("Legacy Sanaei client mutation is unsupported for this inbound protocol")
        identifier = client.get(field)
        if not isinstance(identifier, str) or not identifier.strip():
            raise PanelError("Legacy Sanaei client identifier is missing")
        return identifier.strip()

    async def _legacy_create_protocol(self, connection: PanelConnection, inbound_id: int) -> None:
        result = await self._request(connection, "GET", f"/panel/api/inbounds/get/{inbound_id}")
        inbound = self._obj(result)
        protocol = str(inbound.get("protocol") or "").lower()
        # The current create payload can safely provision VLESS/VMess IDs. Trojan
        # and Shadowsocks require protocol-specific secrets that this contract does
        # not yet accept, so fail closed rather than create malformed clients.
        if protocol not in {"vless", "vmess"}:
            raise PanelError("Legacy Sanaei create currently supports VLESS/VMess inbounds only")

    async def get_user(self, connection: PanelConnection, username: str) -> PanelUser:
        obj, _ = await self._find_client(connection, username)
        return self._map(obj, connection.base_url)

    @staticmethod
    def _limit_gb(data_limit_bytes: int | None) -> int:
        if data_limit_bytes is None or data_limit_bytes == 0:
            return 0
        try:
            value = int(data_limit_bytes)
        except (TypeError, ValueError, OverflowError):
            raise PanelError("Sanaei traffic limit must be a whole number of GiB") from None
        if value < 0 or value % (1024 ** 3) != 0:
            raise PanelError("Sanaei supports traffic limits in whole GiB only")
        return value // (1024 ** 3)

    async def create_user(self, connection: PanelConnection, user: UserCreate) -> PanelUser:
        mode, _ = self._credentials(connection)
        inbound_ids = user.extra.get("inbound_ids") or connection.metadata.get("inbound_ids")
        if not isinstance(inbound_ids, list) or not inbound_ids or any(not isinstance(i, int) or i <= 0 for i in inbound_ids):
            raise PanelError("Sanaei creation requires metadata.inbound_ids or UserCreate.extra.inbound_ids")
        expire_ms = int(user.expire_at.timestamp() * 1000) if user.expire_at else 0
        client: dict[str, Any] = {"id": str(user.extra.get("uuid") or user.username), "email": user.username,
            "totalGB": self._limit_gb(user.data_limit_bytes), "expiryTime": expire_ms,
            "enable": True, "tgId": 0, "subId": str(user.extra.get("sub_id") or ""),
            "limitIp": int(user.extra.get("ip_limit", 0) or 0), "reset": 0, "comment": user.note or ""}
        if user.extra.get("flow"): client["flow"] = user.extra["flow"]
        if mode == "token":
            result = await self._request(connection, "POST", "/panel/api/clients/add",
                                         payload={"client": client, "inboundIds": inbound_ids})
        else:
            if len(inbound_ids) != 1:
                raise PanelError("Legacy Sanaei API supports one inbound per create operation in this adapter")
            await self._legacy_create_protocol(connection, inbound_ids[0])
            provided_uuid = user.extra.get("uuid")
            if provided_uuid:
                try:
                    client["id"] = str(UUID(str(provided_uuid)))
                except (ValueError, TypeError, AttributeError):
                    raise PanelError("Legacy Sanaei VLESS/VMess client ID must be a valid UUID") from None
            else:
                client["id"] = str(uuid4())
            settings = {"clients": [client], "decryption": "none", "fallbacks": []}
            result = await self._request(connection, "POST", "/panel/api/inbounds/addClient",
                                         payload={"id": inbound_ids[0], "settings": json.dumps(settings, separators=(",", ":"))})
        obj = self._obj(result)
        if "client" not in obj: obj = {"client": client, "inboundIds": inbound_ids, **obj}
        return self._map(obj, connection.base_url)

    async def update_user(self, connection: PanelConnection, provider_user_id: str, update: UserUpdate) -> PanelUser:
        mode, _ = self._credentials(connection)
        obj, client = await self._find_client(connection, provider_user_id)
        merged = dict(client)
        if update.data_limit_bytes is not None: merged["totalGB"] = self._limit_gb(update.data_limit_bytes)
        if update.expire_at is not None: merged["expiryTime"] = int(update.expire_at.timestamp() * 1000)
        if update.note is not None: merged["comment"] = update.note
        if update.status is not None:
            if update.status not in {"active", "disabled"}: raise PanelError("Sanaei supports active or disabled status only")
            merged["enable"] = update.status == "active"
        if mode == "token":
            result = await self._request(connection, "POST", f"/panel/api/clients/update/{quote(provider_user_id, safe='')}", payload=merged)
            mapped = self._obj(result)
            return self._map(mapped if "client" in mapped else {"client": merged, "inboundIds": obj.get("inboundIds", [])}, connection.base_url)
        inbound_ids = obj.get("inboundIds") or connection.metadata.get("inbound_ids") or []
        client_id = self._legacy_client_identifier(obj, client)
        if len(inbound_ids) != 1:
            raise PanelError("Legacy Sanaei update requires exactly one inbound ID")
        settings = {"clients": [merged], "decryption": "none", "fallbacks": []}
        result = await self._request(connection, "POST", f"/panel/api/inbounds/updateClient/{quote(client_id, safe='')}",
                                     payload={"id": inbound_ids[0], "settings": json.dumps(settings, separators=(",", ":"))})
        return self._map({"client": merged, "inboundIds": inbound_ids}, connection.base_url)

    async def delete_user(self, connection: PanelConnection, provider_user_id: str) -> None:
        mode, _ = self._credentials(connection)
        if mode == "token":
            await self._request(connection, "POST", f"/panel/api/clients/del/{quote(provider_user_id, safe='')}")
            return
        obj, client = await self._find_client(connection, provider_user_id)
        inbound_ids = obj.get("inboundIds") or connection.metadata.get("inbound_ids") or []
        client_id = self._legacy_client_identifier(obj, client)
        if len(inbound_ids) != 1:
            raise PanelError("Legacy Sanaei delete requires exactly one inbound ID")
        await self._request(connection, "POST", f"/panel/api/inbounds/{int(inbound_ids[0])}/delClient/{quote(client_id, safe='')}")

    async def list_users(self, connection: PanelConnection, *, limit: int = 200, offset: int = 0) -> list[PanelUser]:
        mode, _ = self._credentials(connection)
        limit = max(1, min(int(limit), 200)); offset = max(0, int(offset))
        if mode == "token":
            result = await self._request(connection, "GET", "/panel/api/clients/list")
            raw = result.get("obj", []) if isinstance(result, dict) else []
            if not isinstance(raw, list): raise PanelError("Sanaei clients list did not contain a list")
            return [self._map({"client": client}, connection.base_url) for client in raw[offset:offset+limit] if isinstance(client, dict)]
        result = await self._request(connection, "GET", "/panel/api/inbounds/list")
        raw = result.get("obj", []) if isinstance(result, dict) else []
        if not isinstance(raw, list): raise PanelError("Sanaei inbound response did not contain a list")
        grouped: dict[str, dict[str, Any]] = {}
        for inbound in raw:
            if not isinstance(inbound, dict): continue
            settings = inbound.get("settings", {})
            if isinstance(settings, str):
                try: settings = json.loads(settings)
                except ValueError: continue
            clients = settings.get("clients", []) if isinstance(settings, dict) else []
            if not isinstance(clients, list): continue
            protocol = str(inbound.get("protocol") or "").lower()
            for client in clients:
                if not isinstance(client, dict) or not client.get("email"):
                    continue
                username = str(client["email"])
                inbound_id = self._legacy_inbound_id(inbound.get("id"))
                identity = self._legacy_identity_key(client, protocol)
                current = grouped.get(username)
                if current is None:
                    grouped[username] = {"client": client, "protocol": protocol,
                                         "identity": identity, "inbound_ids": [inbound_id]}
                    continue
                if current["identity"] != identity:
                    raise PanelError("Legacy Sanaei client identity is ambiguous across inbounds")
                if inbound_id not in current["inbound_ids"]:
                    current["inbound_ids"].append(inbound_id)
        users = [self._map({"client": item["client"], "protocol": item["protocol"],
                            "inboundIds": sorted(item["inbound_ids"])}, connection.base_url)
                 for item in grouped.values()]
        return users[offset:offset+limit]

    async def get_usage(self, connection: PanelConnection, username: str) -> dict[str, Any]:
        mode, _ = self._credentials(connection)
        if mode == "token":
            result = await self._request(connection, "GET", f"/panel/api/clients/traffic/{quote(username, safe='')}")
            raw = result.get("obj", result) if isinstance(result, dict) else {}
            if isinstance(raw, dict):
                try:
                    if "used_bytes" in raw: used = int(raw["used_bytes"])
                    elif "traffic" in raw: used = int(raw["traffic"])
                    elif "up" in raw or "down" in raw: used = int(raw.get("up", 0) or 0) + int(raw.get("down", 0) or 0)
                    elif "total" in raw: used = int(raw["total"])
                    else: used = -1
                    total = int(raw["total_bytes"]) if "total_bytes" in raw else int(raw.get("totalGB", 0) or 0) * (1024 ** 3)
                    if used >= 0 and total >= 0: return {"username": username, "used_bytes": used, "total_bytes": total}
                except (TypeError, ValueError):
                    pass
            result = await self._request(connection, "GET", "/panel/api/clients/list")
            clients = result.get("obj", []) if isinstance(result, dict) else []
            if isinstance(clients, list):
                for client in clients:
                    if isinstance(client, dict) and str(client.get("email", "")) == username:
                        try: return {"username": username, "used_bytes": int(client.get("traffic", 0) or 0), "total_bytes": int(client.get("totalGB", 0) or 0) * (1024 ** 3)}
                        except (TypeError, ValueError): break
            raise PanelError("Sanaei traffic endpoint returned no recognized counters")
        result = await self._request(connection, "GET", "/panel/api/inbounds/list")
        raw = result.get("obj", []) if isinstance(result, dict) else []
        if not isinstance(raw, list): raise PanelError("Sanaei inbound response did not contain a list")
        matches = []
        total_limit = 0
        for inbound in raw:
            if not isinstance(inbound, dict): continue
            stats = inbound.get("clientStats", [])
            if not isinstance(stats, list): continue
            for stat in stats:
                if isinstance(stat, dict) and str(stat.get("email", "")) == username:
                    if any(key not in stat or stat[key] is None or isinstance(stat[key], bool)
                           for key in ("up", "down", "total")):
                        raise PanelError("Sanaei returned invalid traffic counters")
                    try:
                        up = int(stat["up"]); down = int(stat["down"]); total = int(stat["total"])
                    except (TypeError, ValueError, OverflowError):
                        raise PanelError("Sanaei returned invalid traffic counters") from None
                    if min(up, down, total) < 0: raise PanelError("Sanaei returned negative traffic counters")
                    matches.append(up + down)
                    total_limit = max(total_limit, total)
        if not matches: raise PanelError("Sanaei usage stats were not found for this client")
        return {"username": username, "used_bytes": sum(matches), "total_bytes": total_limit}

    async def get_subscription_url(self, connection: PanelConnection, username: str) -> str:
        mode, _ = self._credentials(connection)
        if mode != "token":
            raise PanelError("Subscription-link endpoint is not verified for legacy Sanaei mode")
        user = await self.get_user(connection, username)
        if user.subscription_url:
            return user.subscription_url
        config = self._subscription_server_config(connection)
        if config is None:
            raise PanelError("Sanaei subscription server origin/path is not configured")
        sub_id = user.metadata.get("sub_id")
        if not isinstance(sub_id, str) or not sub_id.strip() or len(sub_id) > 256 or any(ord(ch) < 32 for ch in sub_id):
            raise PanelError("Sanaei client response omitted a valid subscription ID")
        base_url, path = config
        return f"{base_url}/{path}/{quote(sub_id.strip(), safe='')}"
