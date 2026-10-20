"""Sanaei/3x-ui adapter with explicit legacy-cookie and token API modes.

The legacy API is the familiar 3x-ui inbound API. The token API follows the optional
client endpoints used by the audited Faoxima integration. Capabilities are conservative:
we never advertise operations that this adapter cannot verify for the selected mode.
"""
from __future__ import annotations
from datetime import datetime, timezone
import json
from typing import Any, Callable
from urllib.parse import quote, urlsplit

from ..contracts import (PanelCapabilities, PanelConnection, PanelError, PanelUser, UserCreate,
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
                       *, payload: dict[str, Any] | None = None) -> Any:
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

    @classmethod
    def _map(cls, obj: dict[str, Any], base_url: str) -> PanelUser:
        client = obj.get("client") if isinstance(obj.get("client"), dict) else obj
        username = str(client.get("email") or client.get("username") or "")
        if not username:
            raise PanelError("Sanaei response omitted client email")
        expire_ms = client.get("expiryTime")
        expire = None
        if isinstance(expire_ms, (int, float)) and expire_ms > 0:
            # 3x-ui stores client expiry as milliseconds since Unix epoch.
            expire = datetime.fromtimestamp(float(expire_ms) / 1000, timezone.utc)
        total_gb = client.get("totalGB")
        total_bytes = None
        if total_gb is not None:
            try:
                total_gb_value = int(total_gb)
                if total_gb_value < 0:
                    raise ValueError
                total_bytes = total_gb_value * (1024 ** 3)
            except (TypeError, ValueError, OverflowError):
                raise PanelError("Sanaei returned an invalid totalGB limit") from None
        try:
            used = int(obj.get("used_traffic", 0) or 0)
        except (TypeError, ValueError):
            used = 0
        if not used:
            try: used = int(client.get("up", 0) or 0) + int(client.get("down", 0) or 0)
            except (TypeError, ValueError): used = 0
        if used < 0:
            raise PanelError("Sanaei returned a negative usage counter")
        links = obj.get("links") or obj.get("subscription_url")
        sub = links[0] if isinstance(links, list) and links else links if isinstance(links, str) else None
        return PanelUser(provider_user_id=username, username=username,
            status="active" if client.get("enable", True) else "disabled",
            used_bytes=int(used) if used is not None else None,
            data_limit_bytes=total_bytes,
            expire_at=expire, subscription_url=sub,
            raw_status=str(client.get("enable", "unknown")),
            metadata={"inbound_ids": obj.get("inboundIds", []), "uuid": client.get("id"), "sub_id": client.get("subId")})

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
        return PanelCapabilities(create_user=True, delete_user=True, update_user=True,
            read_usage=True, read_status=True, suspend_user=True, activate_user=True,
            subscription_link=mode == "token", list_users=True, manage_resellers=False,
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
            result = await self._request(connection, "GET", f"/panel/api/clients/get/{quote(username, safe='')}")
            obj = self._obj(result)
            return obj, obj.get("client", obj)
        result = await self._request(connection, "GET", "/panel/api/inbounds/list")
        raw_obj = result.get("obj", result) if isinstance(result, dict) else None
        inbounds = raw_obj if isinstance(raw_obj, list) else (raw_obj.get("inbounds", raw_obj.get("items", [])) if isinstance(raw_obj, dict) else [])
        if not isinstance(inbounds, list):
            raise PanelError("Sanaei inbound response did not contain a list")
        for inbound in inbounds:
            if not isinstance(inbound, dict): continue
            settings = inbound.get("settings", {})
            if isinstance(settings, str):
                try: settings = json.loads(settings)
                except ValueError: settings = {}
            for client in (settings.get("clients", []) if isinstance(settings, dict) else []):
                if isinstance(client, dict) and client.get("email") == username:
                    return {"inboundIds": [inbound.get("id")], "client": client}, client
        raise PanelError("Sanaei client was not found")

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
        if len(inbound_ids) != 1 or not client.get("id"):
            raise PanelError("Legacy Sanaei update requires one inbound ID and a client UUID")
        settings = {"clients": [merged], "decryption": "none", "fallbacks": []}
        result = await self._request(connection, "POST", f"/panel/api/inbounds/updateClient/{quote(str(client['id']), safe='')}",
                                     payload={"id": inbound_ids[0], "settings": json.dumps(settings, separators=(",", ":"))})
        return self._map({"client": merged, "inboundIds": inbound_ids}, connection.base_url)

    async def delete_user(self, connection: PanelConnection, provider_user_id: str) -> None:
        mode, _ = self._credentials(connection)
        if mode == "token":
            await self._request(connection, "POST", f"/panel/api/clients/del/{quote(provider_user_id, safe='')}")
            return
        obj, client = await self._find_client(connection, provider_user_id)
        inbound_ids = obj.get("inboundIds") or connection.metadata.get("inbound_ids") or []
        if len(inbound_ids) != 1 or not client.get("id"):
            raise PanelError("Legacy Sanaei delete requires one inbound ID and a client UUID")
        await self._request(connection, "POST", f"/panel/api/inbounds/{int(inbound_ids[0])}/delClient/{quote(str(client['id']), safe='')}")

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
        users: dict[str, PanelUser] = {}
        for inbound in raw:
            if not isinstance(inbound, dict): continue
            settings = inbound.get("settings", {})
            if isinstance(settings, str):
                try: settings = json.loads(settings)
                except ValueError: continue
            clients = settings.get("clients", []) if isinstance(settings, dict) else []
            for client in clients:
                if isinstance(client, dict) and client.get("email"):
                    users.setdefault(str(client["email"]), self._map({"client": client, "inboundIds": [inbound.get("id")]}, connection.base_url))
        return list(users.values())[offset:offset+limit]

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
                    try:
                        up = int(stat.get("up", 0) or 0); down = int(stat.get("down", 0) or 0)
                        total = int(stat.get("total", 0) or 0)
                    except (TypeError, ValueError): raise PanelError("Sanaei returned invalid traffic counters") from None
                    if min(up, down, total) < 0: raise PanelError("Sanaei returned negative traffic counters")
                    matches.append(up + down)
                    total_limit = max(total_limit, total)
        if not matches: raise PanelError("Sanaei usage stats were not found for this client")
        return {"username": username, "used_bytes": sum(matches), "total_bytes": total_limit}

    async def get_subscription_url(self, connection: PanelConnection, username: str) -> str:
        mode, _ = self._credentials(connection)
        if mode != "token": raise PanelError("Subscription-link endpoint is not verified for legacy Sanaei mode")
        result = await self._request(connection, "GET", f"/panel/api/clients/links/{quote(username, safe='')}")
        obj = result.get("obj", result) if isinstance(result, dict) else None
        if isinstance(obj, str): candidates = obj.splitlines()
        elif isinstance(obj, list): candidates = [v if isinstance(v, str) else v.get("uri") or v.get("link") or v.get("url", "") for v in obj]
        else: candidates = []
        for candidate in candidates:
            if isinstance(candidate, str) and candidate.strip().startswith(("vless://", "vmess://", "trojan://", "ss://")):
                return candidate.strip()
        raise PanelError("Sanaei response did not contain a supported connection link")
