"""Rebecca REST adapter based on the provider's documented API contract.

The API key is resolved from a secret reference and sent as an Authorization Bearer token.
Provider errors and response bodies are intentionally not copied into logs/exceptions.
"""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import quote, urljoin, urlsplit

from ..contracts import (PanelCapabilities, PanelConnection, PanelError, PanelUser, UserCreate,
                         UserUpdate, local_capability_report)


class RebeccaAdapter:
    kind = "rebecca"

    def __init__(self, secret_resolver: Callable[[str], str] | None = None,
                 client_factory: Callable[..., Any] | None = None):
        self._secret_resolver = secret_resolver or (lambda ref: "")
        self._client_factory = client_factory

    def _validate(self, connection: PanelConnection) -> str:
        parts = urlsplit(connection.base_url)
        if not connection.enabled:
            raise PanelError("Rebecca connection is disabled")
        if not connection.verify_tls or parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
            raise PanelError("Rebecca requires an HTTPS URL with TLS verification and no embedded credentials")
        if connection.auth_mode != "api_key":
            raise PanelError("Rebecca requires auth_mode=api_key")
        secret = self._secret_resolver(connection.secret_ref)
        if not secret:
            raise PanelError("Rebecca API key is not configured")
        return secret

    async def _request(self, connection: PanelConnection, method: str, path: str,
                       *, payload: dict[str, Any] | None = None) -> Any:
        secret = self._validate(connection)
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
                response = await client.request(method, path,
                    headers={"Accept": "application/json", "Authorization": f"Bearer {secret}"},
                    json=payload)
            if response.status_code < 200 or response.status_code >= 300:
                raise PanelError(f"Rebecca returned HTTP {response.status_code}")
            if not response.content:
                return {}
            try:
                result = response.json()
            except ValueError:
                raise PanelError("Rebecca returned an invalid JSON response") from None
            if isinstance(result, dict) and result.get("detail") and response.status_code >= 400:
                raise PanelError("Rebecca rejected the operation")
            return result
        except PanelError:
            raise
        except Exception as exc:
            raise PanelError(f"Rebecca transport failed ({type(exc).__name__})") from None

    @staticmethod
    def _map(obj: Any, base_url: str) -> PanelUser:
        if not isinstance(obj, dict):
            raise PanelError("Rebecca returned an unexpected user object")
        username = str(obj.get("username") or obj.get("name") or "")
        if not username:
            raise PanelError("Rebecca response omitted user identity")
        exp = obj.get("expire") or obj.get("expire_at")
        if isinstance(exp, (int, float)):
            exp = datetime.fromtimestamp(exp, timezone.utc) if exp > 0 else None
        elif isinstance(exp, str):
            try: exp = datetime.fromisoformat(exp.replace("Z", "+00:00"))
            except ValueError: exp = None
        sub = obj.get("subscription_url") or obj.get("subscription_link")
        if not sub and isinstance(obj.get("links"), list):
            sub = next((item for item in obj["links"] if isinstance(item, str) and item.strip()), None)
        if not sub and isinstance(obj.get("subscription_urls"), dict):
            urls = obj["subscription_urls"]
            sub = urls.get("username-key") or urls.get("username") or urls.get("base")
        return PanelUser(provider_user_id=username, username=username,
            status=str(obj.get("status") or "unknown"),
            used_bytes=int(obj.get("used_traffic") or obj.get("used_bytes") or 0),
            data_limit_bytes=int(obj.get("data_limit") or obj.get("data_limit_bytes") or 0),
            expire_at=exp if isinstance(exp, datetime) else None,
            subscription_url=urljoin(base_url.rstrip("/") + "/", str(sub)) if sub else None,
            raw_status=str(obj.get("status") or "unknown"), metadata={"service_id": obj.get("service_id")})

    async def capability_report(self, connection: PanelConnection) -> dict[str, Any]:
        """Report local method coverage separately from unknown panel permissions."""
        validation_error = None
        try:
            self._validate(connection)
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
        return PanelCapabilities(create_user=True, delete_user=True, update_user=True,
            read_usage=True, read_status=True, suspend_user=True, activate_user=True,
            subscription_link=True, list_users=True, manage_resellers=False, health_check=True)

    async def health_check(self, connection: PanelConnection) -> dict[str, Any]:
        try:
            result = await self._request(connection, "GET", "/api/system")
            return {"ok": isinstance(result, dict), "panel": self.kind, "connection_id": connection.id,
                    "checked_at": datetime.now(timezone.utc).isoformat()}
        except Exception as exc:
            return {"ok": False, "panel": self.kind, "connection_id": connection.id,
                    "error_type": type(exc).__name__}

    async def get_user(self, connection: PanelConnection, username: str) -> PanelUser:
        result = await self._request(connection, "GET", f"/api/user/{quote(username, safe='')}")
        if isinstance(result, dict) and isinstance(result.get("data"), dict): result = result["data"]
        return self._map(result, connection.base_url)

    async def create_user(self, connection: PanelConnection, user: UserCreate) -> PanelUser:
        service_id = user.extra.get("service_id") or connection.metadata.get("service_id")
        if not isinstance(service_id, (str, int)) or not str(service_id).strip():
            raise PanelError("Rebecca user creation requires a configured service_id")
        payload: dict[str, Any] = {"username": user.username, "data_limit": user.data_limit_bytes or 0,
            "service_id": service_id, "note": user.note or "", "ip_limit": int(user.extra.get("ip_limit", 0) or 0)}
        if user.expire_at:
            payload["expire"] = int(user.expire_at.timestamp())
        elif user.extra.get("on_hold_expire_duration_seconds"):
            payload["status"] = "on_hold"
            payload["on_hold_expire_duration"] = int(user.extra["on_hold_expire_duration_seconds"])
        result = await self._request(connection, "POST", "/api/v2/users", payload=payload)
        if isinstance(result, dict) and isinstance(result.get("data"), dict): result = result["data"]
        return self._map(result, connection.base_url)

    async def update_user(self, connection: PanelConnection, provider_user_id: str, update: UserUpdate) -> PanelUser:
        payload: dict[str, Any] = {}
        if update.data_limit_bytes is not None: payload["data_limit"] = update.data_limit_bytes
        if update.expire_at is not None: payload["expire"] = int(update.expire_at.timestamp())
        if update.note is not None: payload["note"] = update.note
        if update.status is not None:
            if update.status not in {"active", "disabled", "on_hold", "limited", "expired"}:
                raise PanelError("Unsupported normalized status for Rebecca")
            payload["status"] = update.status
        result = await self._request(connection, "PUT", f"/api/v2/users/{quote(provider_user_id, safe='')}", payload=payload)
        if isinstance(result, dict) and isinstance(result.get("data"), dict): result = result["data"]
        return self._map(result, connection.base_url)

    async def delete_user(self, connection: PanelConnection, provider_user_id: str) -> None:
        await self._request(connection, "DELETE", f"/api/user/{quote(provider_user_id, safe='')}")

    async def list_users(self, connection: PanelConnection, *, limit: int = 200, offset: int = 0) -> list[PanelUser]:
        limit = max(1, min(int(limit), 200))
        offset = max(0, int(offset))
        result = await self._request(connection, "GET", f"/api/users?limit={limit}&offset={offset}")
        if isinstance(result, dict) and isinstance(result.get("data"), dict): result = result["data"]
        users = result.get("users", []) if isinstance(result, dict) else []
        if not isinstance(users, list): raise PanelError("Rebecca user list response was invalid")
        return [self._map(user, connection.base_url) for user in users if isinstance(user, dict)]

    async def get_usage(self, connection: PanelConnection, username: str) -> dict[str, Any]:
        result = await self._request(connection, "GET", f"/api/user/{quote(username, safe='')}/usage")
        return result if isinstance(result, dict) else {"data": result}

    async def get_subscription_url(self, connection: PanelConnection, username: str) -> str:
        user = await self.get_user(connection, username)
        if not user.subscription_url:
            raise PanelError("Rebecca response did not include a subscription URL")
        return user.subscription_url
