"""PasarGuard adapter, isolated behind the provider-neutral contract.

Secrets are resolved by an injected resolver so raw credentials never belong in
PanelConnection or logs. SDK import is lazy to keep contract tests independent.
"""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import urljoin

from ..contracts import PanelCapabilities, PanelConnection, PanelError, PanelUser, PanelUserNotFound, UserCreate, UserUpdate

SecretResolver = Callable[[str], str]


class PasarGuardAdapter:
    kind = "pasarguard"

    def __init__(self, secret_resolver: SecretResolver | None = None, sdk_factory: Callable[..., Any] | None = None):
        self._secret_resolver = secret_resolver or self._missing_secret
        self._sdk_factory = sdk_factory

    @staticmethod
    def _missing_secret(ref: str) -> str:
        raise PanelError(f"No secret resolver configured for secret reference: {ref}")

    def _client(self, connection: PanelConnection):
        if not connection.enabled:
            raise PanelError("Panel connection is disabled")
        if not connection.verify_tls:
            raise PanelError("TLS certificate verification must remain enabled")
        if not connection.base_url.lower().startswith("https://"):
            raise PanelError("PasarGuard panel connections require an HTTPS base URL")
        if connection.auth_mode not in {"api_key", "access_token", "username_password"}:
            raise PanelError(f"Unsupported PasarGuard auth mode: {connection.auth_mode}")
        username = None
        if connection.auth_mode == "username_password":
            username = connection.metadata.get("username")
            if not isinstance(username, str) or not username.strip():
                raise PanelError("username_password auth requires metadata.username")
            username = username.strip()
        # Reject incomplete/unsupported auth configuration before touching a secret
        # resolver. Secret stores may be remote or audited and should not be consulted
        # for a connection that cannot be used even if credentials were available.
        secret = self._secret_resolver(connection.secret_ref)
        if not secret:
            raise PanelError("Panel credential resolved to an empty value")
        factory = self._sdk_factory
        if factory is None:
            try:
                from pasarguard import PasarguardAPI
            except ImportError as exc:
                raise PanelError("PasarGuard SDK is not installed; install project dependencies") from None
            factory = PasarguardAPI
        base_url = connection.base_url.rstrip("/")
        # The SDK defaults TLS verification to False. Explicitly pass the validated
        # connection policy; merely requiring an https URL is not sufficient.
        client_options = {
            "base_url": base_url,
            "verify": connection.verify_tls,
            "timeout": connection.timeout_seconds,
        }
        if connection.auth_mode == "api_key":
            return factory(**client_options, api_key=secret), secret
        if connection.auth_mode == "access_token":
            return factory(**client_options, token=secret), secret
        if connection.auth_mode == "username_password":
            return factory(**client_options), (username, secret)
        # The supported auth modes were validated before secret resolution above.
        raise PanelError("PasarGuard auth mode validation failed")

    async def _authed_client(self, connection: PanelConnection):
        client, credential = self._client(connection)
        if connection.auth_mode != "username_password":
            return client, credential
        username, password = credential
        try:
            token = await client.get_token(username=username, password=password)
            factory = self._sdk_factory
            if factory is None:
                from pasarguard import PasarguardAPI
                factory = PasarguardAPI
            return factory(
                base_url=connection.base_url.rstrip("/"),
                verify=connection.verify_tls,
                timeout=connection.timeout_seconds,
                token=token.access_token,
            ), token.access_token
        except Exception as exc:
            raise PanelError(f"PasarGuard authentication failed ({type(exc).__name__})") from None

    async def capability_report(self, connection: PanelConnection) -> dict[str, Any]:
        """Report SDK surface evidence without confusing it with live panel permission.

        A method/model found in the installed SDK only proves local client support. It
        does not prove the configured panel version or credential can perform the action.
        """
        report: dict[str, Any] = {
            "provider": self.kind,
            "evidence_level": "local_sdk_introspection_only",
            "live_panel_verified": False,
            "operations": {},
        }
        try:
            client, _ = self._client(connection)
            try:
                from pasarguard import (
                    AdminCreate, AdminModify, RoleLimits,
                    UserCreate as SDKUserCreate, UserModify as SDKUserModify,
                )
            except ImportError:
                SDKUserCreate = SDKUserModify = AdminCreate = AdminModify = RoleLimits = None
            checks = {
                "create_user": (any(hasattr(client, name) for name in ("create_user", "add_user")) and SDKUserCreate is not None),
                "read_user": hasattr(client, "get_user_by_username"),
                "update_user": (hasattr(client, "modify_user_by_id") and SDKUserModify is not None),
                "delete_user": hasattr(client, "remove_user_by_id"),
                # UserResponse exposes the cumulative used_traffic counter; the
                # /usage endpoint is a time series and is not a substitute for lifetime usage.
                "read_usage": hasattr(client, "get_user_by_id"),
                "read_groups": hasattr(client, "get_groups_simple"),
                "list_users": hasattr(client, "get_users"),
                "read_roles": hasattr(client, "get_roles_simple"),
                # Match the actual SDK method names used by PasarGuardResellerProvider.
                # Include every required model, or reseller support could be advertised
                # even though the provider cannot construct its payload.
                "manage_resellers": (
                    all(hasattr(client, name) for name in (
                        "create_admin", "get_admins", "modify_admin_by_id", "remove_admin_by_id",
                        "get_roles_simple",
                    ))
                    and AdminCreate is not None and AdminModify is not None and RoleLimits is not None
                ),
            }
            report["operations"] = {
                name: {"status": "sdk_surface_present" if supported else "sdk_surface_missing",
                       "panel_permission": "unknown", "live_test": "not_tested"}
                for name, supported in checks.items()
            }
        except Exception as exc:
            # Keep diagnostics safe: never expose exception text, URL query, or credentials.
            report["error_type"] = type(exc).__name__
            report["operations"] = {
                name: {"status": "unknown", "panel_permission": "unknown", "live_test": "not_tested"}
                for name in ("create_user", "read_user", "update_user", "delete_user", "read_usage",
                             "read_groups", "list_users", "read_roles", "manage_resellers")
            }
        return report

    async def capabilities(self, connection: PanelConnection) -> PanelCapabilities:
        """Conservative booleans derived from the installed SDK's exposed surface.

        These flags are not live permission checks; health_check remains read-only and
        staging acceptance is required before enabling purchases or reseller sales.
        """
        report = await self.capability_report(connection)
        operations = report.get("operations", {})
        supported = lambda name: operations.get(name, {}).get("status") == "sdk_surface_present"
        return PanelCapabilities(
            create_user=supported("create_user"),
            delete_user=supported("delete_user"),
            update_user=supported("update_user"),
            read_usage=supported("read_usage"),
            read_status=supported("read_user"),
            suspend_user=supported("update_user"),
            activate_user=supported("update_user"),
            subscription_link=supported("read_user"),
            list_users=supported("list_users"),
            manage_resellers=supported("manage_resellers"),
            health_check=supported("read_groups"),
            sync=False,
        )

    async def health_check(self, connection: PanelConnection) -> dict[str, Any]:
        try:
            client, token = await self._authed_client(connection)
            # Groups are a low-impact authenticated endpoint and confirm API access.
            try:
                await client.get_groups_simple(all=True, token=token)
            except TypeError:
                await client.get_groups_simple(all=True)
            return {"ok": True, "panel": self.kind, "connection_id": connection.id, "checked_at": datetime.now(timezone.utc).isoformat()}
        except Exception as exc:
            return {"ok": False, "panel": self.kind, "connection_id": connection.id, "error_type": type(exc).__name__}

    @staticmethod
    def _value(obj: Any, *names: str, default: Any = None) -> Any:
        for name in names:
            if isinstance(obj, dict) and name in obj:
                return obj[name]
            if hasattr(obj, name):
                return getattr(obj, name)
        return default

    @classmethod
    def _map_user(cls, obj: Any, base_url: str) -> PanelUser:
        username = str(cls._value(obj, "username", default=""))
        if not username.strip():
            raise PanelError("PasarGuard response omitted user identity")
        uid = cls._value(obj, "id", "user_id", default=username)
        expire = cls._value(obj, "expire", "expire_at")
        if isinstance(expire, (int, float)):
            try:
                expire = datetime.fromtimestamp(expire, tz=timezone.utc) if expire > 0 else None
            except (OverflowError, OSError, ValueError):
                raise PanelError("PasarGuard response contained an invalid expiry") from None
        elif isinstance(expire, str):
            try:
                expire = datetime.fromisoformat(expire.replace("Z", "+00:00"))
            except ValueError:
                raise PanelError("PasarGuard response contained an invalid expiry") from None
        sub = cls._value(obj, "subscription_url", "subscription_link")
        if sub:
            sub = urljoin(base_url.rstrip("/") + "/", str(sub))
        used = cls._value(obj, "used_traffic", "used_bytes", "used_traffic_bytes")
        limit = cls._value(obj, "data_limit", "data_limit_bytes")
        raw_status = cls._value(obj, "status", default=cls._value(obj, "enable", default="unknown"))
        status = cls._normalize_status(raw_status)
        if isinstance(expire, datetime) and expire.tzinfo is None:
            expire = expire.replace(tzinfo=timezone.utc)
        raw_groups = cls._value(obj, "group_ids", "groupIds", default=None)
        group_ids = None
        if isinstance(raw_groups, (list, tuple, set)):
            normalized_groups = []
            for item in raw_groups:
                candidate = cls._value(item, "id", default=item)
                try:
                    normalized_groups.append(int(candidate))
                except (TypeError, ValueError, OverflowError):
                    normalized_groups = []
                    break
            if normalized_groups or not raw_groups:
                group_ids = sorted(set(normalized_groups))
        metadata = {"group_ids": group_ids} if group_ids is not None else {}
        return PanelUser(provider_user_id=str(uid), username=username,
            status=status,
            used_bytes=cls._validated_counter(used, "usage counter"),
            data_limit_bytes=cls._validated_counter(limit, "data limit"),
            expire_at=expire if isinstance(expire, datetime) else None,
            subscription_url=str(sub) if sub else None,
            raw_status=str(raw_status), metadata=metadata)

    @staticmethod
    def _validated_counter(value: Any, label: str) -> int | None:
        if value is None:
            return None
        # bool subclasses int in Python, but true/false are never valid byte counts.
        if isinstance(value, bool):
            raise PanelError(f"PasarGuard response contained an invalid {label}") from None
        try:
            number = int(value)
        except (TypeError, ValueError, OverflowError):
            raise PanelError(f"PasarGuard response contained an invalid {label}") from None
        if number < 0:
            raise PanelError(f"PasarGuard response contained a negative {label}") from None
        return number

    @staticmethod
    def _normalize_status(value: Any) -> str:
        """Normalize common PasarGuard status representations without guessing unknowns."""
        if isinstance(value, bool):
            return "active" if value else "disabled"
        normalized = str(value).strip().lower()
        if normalized in {"true", "1", "enabled", "active", "on"}:
            return "active"
        if normalized in {"false", "0", "disabled", "disable", "inactive", "off"}:
            return "disabled"
        if normalized in {"limited", "expired"}:
            return normalized
        return "unknown" if not normalized else normalized

    async def list_roles(self, connection: PanelConnection) -> list[dict[str, Any]]:
        client, token = await self._authed_client(connection)
        try:
            response = await client.get_roles_simple(token=token)
            if isinstance(response, list):
                roles = response
            else:
                roles = getattr(response, "roles", None) or getattr(response, "items", None) or []
            return [{"id": int(role.get("id") if isinstance(role, dict) else role.id),
                     "name": str(role.get("name") if isinstance(role, dict) else role.name),
                     "is_owner": bool((role.get("is_owner", False) if isinstance(role, dict) else getattr(role, "is_owner", False)))}
                    for role in roles]
        except Exception as exc:
            raise PanelError(f"PasarGuard role lookup failed ({type(exc).__name__})") from None

    async def get_user(self, connection: PanelConnection, username: str) -> PanelUser:
        client, token = await self._authed_client(connection)
        try:
            result = await client.get_user_by_username(username=username, token=token)
            return self._map_user(result, connection.base_url)
        except PanelError:
            raise
        except Exception as exc:
            status_code = getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)
            error_name = type(exc).__name__.replace("_", "").lower()
            if status_code == 404 or "notfound" in error_name or "userdoesnotexist" in error_name:
                raise PanelUserNotFound("PasarGuard user was not found") from None
            raise PanelError(f"PasarGuard get_user failed ({type(exc).__name__})") from None

    async def create_user(self, connection: PanelConnection, user: UserCreate) -> PanelUser:
        client, token = await self._authed_client(connection)
        try:
            from pasarguard import UserCreate as SDKUserCreate
            payload = SDKUserCreate(username=user.username, group_ids=list(user.group_ids),
                data_limit=user.data_limit_bytes, expire=int(user.expire_at.timestamp()) if user.expire_at else None,
                note=user.note)
            create_method = getattr(client, "create_user", None) or getattr(client, "add_user", None)
            if create_method is None:
                raise PanelError("Installed PasarGuard SDK has no supported user creation method")
            result = await create_method(user=payload, token=token)
            return self._map_user(result, connection.base_url)
        except PanelError:
            raise
        except Exception as exc:
            raise PanelError(f"PasarGuard create_user failed ({type(exc).__name__})") from None

    async def update_user(self, connection: PanelConnection, provider_user_id: str, update: UserUpdate) -> PanelUser:
        client, token = await self._authed_client(connection)
        try:
            from pasarguard import UserModify as SDKUserModify
            values: dict[str, Any] = {}
            if update.data_limit_bytes is not None: values["data_limit"] = update.data_limit_bytes
            if update.expire_at is not None: values["expire"] = int(update.expire_at.timestamp())
            if update.note is not None: values["note"] = update.note
            if update.status is not None:
                # PasarGuard versions differ in status representation; only map explicit values.
                if update.status not in {"active", "disabled", "limited", "expired", "on_hold"}:
                    raise PanelError(f"Unsupported normalized status: {update.status}")
                values["status"] = update.status
            payload = SDKUserModify(**values)
            result = await client.modify_user_by_id(user_id=provider_user_id, user=payload, token=token)
            return self._map_user(result, connection.base_url)
        except PanelError:
            raise
        except Exception as exc:
            raise PanelError(f"PasarGuard update_user failed ({type(exc).__name__})") from None

    async def delete_user(self, connection: PanelConnection, provider_user_id: str) -> None:
        client, token = await self._authed_client(connection)
        try:
            await client.remove_user_by_id(user_id=provider_user_id, token=token)
        except Exception as exc:
            raise PanelError(f"PasarGuard delete_user failed ({type(exc).__name__})") from None

    async def list_users(self, connection: PanelConnection, *, limit: int = 100, offset: int = 0) -> list[PanelUser]:
        """List a bounded page of users; callers control pagination explicitly."""
        client, token = await self._authed_client(connection)
        try:
            page_size = max(1, min(int(limit), 200))
            page_offset = max(0, int(offset))
            result = await client.get_users(token=token, offset=page_offset, limit=page_size)
            # An absent envelope is not evidence that the panel has zero users. Treat
            # malformed successful responses as errors so callers cannot reconcile a
            # partial/empty snapshot as authoritative.
            users = self._value(result, "users", default=None)
            if not isinstance(users, (list, tuple)):
                raise PanelError("PasarGuard user list response was invalid")
            return [self._map_user(user, connection.base_url) for user in users]
        except PanelError:
            raise
        except Exception as exc:
            raise PanelError(f"PasarGuard list_users failed ({type(exc).__name__})") from None

    @classmethod
    def _map_usage(cls, result: Any) -> dict[str, Any]:
        """Normalize documented PasarGuard usage counters to the byte-based adapter contract.

        Keep provider fields for diagnostics/forward compatibility, but never treat a missing
        counter as zero: usage billing must fail closed rather than undercharge silently.
        """
        if hasattr(result, "model_dump"):
            raw = result.model_dump()
        elif isinstance(result, dict):
            raw = dict(result)
        elif hasattr(result, "__dict__"):
            raw = dict(vars(result))
        else:
            raise PanelError("PasarGuard usage response was not an object")

        def find_counter(source: dict[str, Any], names: tuple[str, ...], label: str) -> int | None:
            for name in names:
                value = source.get(name)
                if value is None:
                    continue
                # bool subclasses int, and coercing True to 1 can silently corrupt billing.
                if isinstance(value, bool):
                    raise PanelError(f"PasarGuard usage response contained an invalid {label}") from None
                try:
                    number = int(value)
                except (TypeError, ValueError, OverflowError):
                    raise PanelError(f"PasarGuard usage response contained an invalid {label}") from None
                if number < 0:
                    raise PanelError(f"PasarGuard usage response contained a negative {label}") from None
                return number
            return None

        nested = next((raw.get(key) for key in ("usage", "data", "result")
                       if isinstance(raw.get(key), dict)), None)
        sources = [raw] + ([nested] if isinstance(nested, dict) else [])
        used = next((value for source in sources if (value := find_counter(
            source, ("used_bytes", "used_traffic", "used_traffic_bytes", "total_usage"), "usage counter")) is not None), None)
        if used is None:
            raise PanelError("PasarGuard usage response omitted a recognized byte counter")
        limit = next((value for source in sources if (value := find_counter(
            source, ("data_limit_bytes", "data_limit", "total_bytes"), "data limit")) is not None), None)
        normalized = dict(raw)
        normalized["used_bytes"] = used
        if limit is not None:
            normalized["data_limit_bytes"] = limit
        return normalized

    async def get_usage(self, connection: PanelConnection, provider_user_id: str) -> dict[str, Any]:
        client, token = await self._authed_client(connection)
        try:
            # Read the user's current cumulative counter. The SDK /usage endpoint returns
            # per-period time-series rows, which must not be mistaken for cumulative usage.
            try:
                user_id = int(str(provider_user_id).strip())
            except (TypeError, ValueError, OverflowError):
                raise PanelError("Invalid PasarGuard provider user ID") from None
            if user_id <= 0:
                raise PanelError("Invalid PasarGuard provider user ID")
            get_user_by_id = getattr(client, "get_user_by_id", None)
            if get_user_by_id is None:
                raise PanelError("PasarGuard SDK lacks cumulative user-usage lookup")
            result = await get_user_by_id(user_id=user_id, token=token)
            return self._map_usage(result)
        except PanelError:
            raise
        except Exception as exc:
            raise PanelError(f"PasarGuard get_usage failed ({type(exc).__name__})") from None

    async def get_subscription_url(self, connection: PanelConnection, username: str) -> str:
        user = await self.get_user(connection, username)
        if not user.subscription_url:
            raise PanelError("PasarGuard response did not include a subscription URL")
        return user.subscription_url
