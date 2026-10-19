"""Provider-side reseller administration with explicit capability checks.

Rebecca native admin support is version/capability-gated against its published OpenAPI contract;
Sanaei/3x-ui remains customer-config-only. Provider errors never expose response bodies/secrets.
"""
from __future__ import annotations
import secrets
import string
from urllib.parse import quote, urlencode
from decimal import Decimal
from typing import Any, Callable
from types import SimpleNamespace

from manubisguard.customer_store import CustomerDataStore
from manubisguard.panels.contracts import PanelConnection, PanelError
from manubisguard.panels.adapters.pasarguard import PasarGuardAdapter
from .models import ResellerPlan
from .service import ProviderReseller


class PasarGuardResellerProvider:
    requires_role = True
    requires_persistent_credentials = True

    def __init__(self, store: CustomerDataStore, *, secret_resolver: Callable[[str], str],
                 sdk_factory: Callable[..., Any] | None = None):
        self.store = store
        self._adapter = PasarGuardAdapter(secret_resolver=secret_resolver, sdk_factory=sdk_factory)

    async def _verified_role_id(self, connection: PanelConnection, value: Any) -> int:
        try:
            role_id = int(value)
        except (TypeError, ValueError, OverflowError):
            raise PanelError("A valid PasarGuard provider role ID is required") from None
        if role_id <= 0:
            raise PanelError("A valid PasarGuard provider role ID is required")
        roles = await self._adapter.list_roles(connection)
        matches = [role for role in roles if role.get("id") == role_id]
        if not matches:
            raise PanelError("Configured PasarGuard role ID was not found on this panel")
        if bool(matches[0].get("is_owner")):
            raise PanelError("PasarGuard owner roles cannot be assigned to reseller plans")
        return role_id

    async def validate_plan(self, *, panel_id: str, username: str, plan: ResellerPlan) -> None:
        connection = self._connection(panel_id)
        if not connection.enabled:
            raise PanelError("PasarGuard connection is disabled")
        role_id = await self._verified_role_id(connection, plan.provider_role_id)
        try:
            from pasarguard import AdminCreate, RoleLimits
        except ImportError:
            raise PanelError("PasarGuard SDK is required for native reseller provisioning") from None
        overrides = dict(plan.permission_overrides)
        if plan.max_users > 0: overrides.setdefault("max_users", int(plan.max_users))
        payload: dict[str, Any] = {"username": username, "password": self._new_password(username),
                                   "role_id": role_id, "note": "anubis-plan-validation"}
        if plan.data_limit_bytes > 0: payload["data_limit"] = int(plan.data_limit_bytes)
        if overrides: payload["permission_overrides"] = RoleLimits(**overrides)
        # Validate SDK/Pydantic model construction before reserving any wallet funds.
        AdminCreate(**payload)

    def _connection(self, panel_id: str) -> PanelConnection:
        row = self.store.get_panel_connection(panel_id)
        if not row:
            raise PanelError("Reseller panel connection not found")
        if row["provider"] != "pasarguard":
            raise PanelError("Native reseller admins are currently supported only on PasarGuard")
        return PanelConnection(id=row["panel_id"], tenant_id=self.store.tenant_id, name=row["label"],
            kind=row["provider"], base_url=row["base_url"], auth_mode=row["auth_mode"],
            secret_ref=row["secret_ref"], enabled=bool(row["enabled"]), verify_tls=True,
            metadata=row.get("metadata") or {})

    async def username_exists(self, *, panel_id: str, username: str) -> bool:
        connection = self._connection(panel_id)
        client, token = await self._adapter._authed_client(connection)
        try:
            result = await client.get_admins(token=token, usernames=[username], limit=10)
            admins = result.get("admins") if isinstance(result, dict) else getattr(result, "admins", None)
            if not isinstance(admins, (list, tuple)):
                raise PanelError("PasarGuard admin lookup returned an incompatible response")
            for admin in admins:
                value = admin.get("username", "") if isinstance(admin, dict) else getattr(admin, "username", "")
                if str(value).casefold() == username.casefold():
                    return True
            return False
        except PanelError:
            raise
        except Exception as exc:
            raise PanelError(f"PasarGuard admin lookup failed ({type(exc).__name__})") from None

    @staticmethod
    def _new_password(username: str, length: int = 20) -> str:
        alphabet = string.ascii_letters + string.digits + "!@#$%^&*()-_=+"
        for _ in range(20):
            value = "".join(secrets.choice(alphabet) for _ in range(length))
            if any(c.isupper() for c in value) and any(c.islower() for c in value) and any(c.isdigit() for c in value) and any(c in "!@#$%^&*()-_=+" for c in value) and username.casefold() not in value.casefold():
                return value
        raise PanelError("Could not generate a provider-compliant one-time password")

    async def create_reseller(self, *, panel_id: str, username: str, plan: ResellerPlan,
                              owner_id: str, volume_gb: Decimal | None) -> ProviderReseller:
        connection = self._connection(panel_id)
        if not connection.enabled:
            raise PanelError("PasarGuard connection is disabled")
        role_id = await self._verified_role_id(connection, plan.provider_role_id)
        client, token = await self._adapter._authed_client(connection)
        try:
            from pasarguard import AdminCreate, RoleLimits
        except ImportError as exc:
            raise PanelError("PasarGuard SDK is required for native reseller provisioning") from exc
        password = self._new_password(username)
        overrides = dict(plan.permission_overrides)
        if plan.max_users > 0:
            overrides.setdefault("max_users", int(plan.max_users))
        payload: dict[str, Any] = {
            "username": username,
            "password": password,
            "role_id": role_id,
            "note": str(owner_id),
        }
        if plan.data_limit_bytes > 0:
            payload["data_limit"] = int(plan.data_limit_bytes)
        if overrides:
            payload["permission_overrides"] = RoleLimits(**overrides)
        try:
            admin = await client.create_admin(admin=AdminCreate(**payload), token=token)
        except Exception as exc:
            # Do not return or log credentials; the caller marks an ambiguous mutation for reconciliation.
            raise PanelError(f"PasarGuard reseller creation failed ({type(exc).__name__})") from None
        admin_id = getattr(admin, "id", None)
        returned_username = str(getattr(admin, "username", username) or username)
        if admin_id is None:
            raise PanelError("PasarGuard create-admin response omitted the admin ID; reconcile before retry")
        return ProviderReseller(str(admin_id), returned_username, password)

    @staticmethod
    def _admin_id(value: str) -> int:
        try:
            admin_id = int(str(value).strip())
        except (TypeError, ValueError, OverflowError):
            raise PanelError("Invalid PasarGuard provider admin ID") from None
        if admin_id <= 0:
            raise PanelError("Invalid PasarGuard provider admin ID")
        return admin_id

    async def delete_reseller(self, *, panel_id: str, provider_account_id: str) -> None:
        admin_id = self._admin_id(provider_account_id)
        connection = self._connection(panel_id)
        client, token = await self._adapter._authed_client(connection)
        try:
            await client.remove_admin_by_id(admin_id=admin_id, token=token)
        except Exception as exc:
            raise PanelError(f"PasarGuard reseller cleanup failed ({type(exc).__name__})") from None

    async def get_reseller(self, *, panel_id: str, provider_account_id: str) -> Any:
        admin_id = self._admin_id(provider_account_id)
        connection = self._connection(panel_id)
        client, token = await self._adapter._authed_client(connection)
        try:
            result = await client.get_admins(token=token, ids=[admin_id], limit=1)
            admins = getattr(result, "admins", None)
            if admins is None and isinstance(result, dict):
                admins = result.get("admins")
            if not isinstance(admins, (list, tuple)):
                raise PanelError("PasarGuard admin lookup returned an incompatible response")
            for admin in admins:
                found_id = admin.get("id", "") if isinstance(admin, dict) else getattr(admin, "id", "")
                if str(found_id) == str(admin_id):
                    return admin
            return None
        except PanelError:
            raise
        except Exception as exc:
            raise PanelError(f"PasarGuard admin lookup failed ({type(exc).__name__})") from None

    async def set_reseller_status(self, *, panel_id: str, provider_account_id: str, enabled: bool) -> None:
        admin_id = self._admin_id(provider_account_id)
        connection = self._connection(panel_id)
        client, token = await self._adapter._authed_client(connection)
        try:
            from pasarguard import AdminModify
            await client.modify_admin_by_id(admin_id=admin_id,
                admin=AdminModify(status="active" if enabled else "disabled"), token=token)
        except Exception as exc:
            raise PanelError(f"PasarGuard reseller status update failed ({type(exc).__name__})") from None


class RebeccaNativeResellerProvider:
    """Rebecca native admin lifecycle, gated by the panel's live OpenAPI schema.

    Rebecca uses /api/admin and /api/admins routes (the OpenAPI paths omit the /api prefix).
    The configured key must belong to an owner with admin-management permissions. A failed
    permission check fails closed; this provider never falls back to a bot-managed account.
    """
    requires_role = False
    requires_persistent_credentials = True

    def __init__(self, store: CustomerDataStore, *, secret_resolver: Callable[[str], str],
                 client_factory: Callable[..., Any] | None = None):
        self.store = store
        self._secret_resolver = secret_resolver
        self._client_factory = client_factory

    def _connection(self, panel_id: str) -> PanelConnection:
        row = self.store.get_panel_connection(panel_id)
        if not row:
            raise PanelError("Rebecca connection not found")
        if row["provider"] != "rebecca":
            raise PanelError("Rebecca native reseller provider cannot manage this panel type")
        if not row["enabled"]:
            raise PanelError("Rebecca connection is disabled")
        if row["auth_mode"] != "api_key":
            raise PanelError("Rebecca native admin API requires an API key")
        from manubisguard.panels.contracts import PanelConnection
        return PanelConnection(id=str(row["panel_id"]), tenant_id=self.store.tenant_id,
            name=str(row["label"]), kind="rebecca", base_url=str(row["base_url"]),
            auth_mode=str(row["auth_mode"]), secret_ref=str(row["secret_ref"]), enabled=True,
            verify_tls=True, metadata=row.get("metadata") or {})

    async def _request(self, connection: PanelConnection, method: str, path: str,
                       payload: dict[str, Any] | None = None) -> Any:
        from urllib.parse import urlsplit
        parts = urlsplit(connection.base_url)
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
            raise PanelError("Rebecca requires HTTPS with verified TLS and no embedded credentials")
        secret = self._secret_resolver(connection.secret_ref)
        if not secret:
            raise PanelError("Rebecca API key is not configured")
        try:
            factory = self._client_factory
            if factory is None:
                import httpx
                factory = httpx.AsyncClient
            async with factory(base_url=connection.base_url.rstrip("/"), timeout=connection.timeout_seconds,
                               verify=True, follow_redirects=False) as client:
                response = await client.request(method, path,
                    headers={"Accept":"application/json", "Authorization":f"Bearer {secret}"}, json=payload)
            if response.status_code < 200 or response.status_code >= 300:
                if response.status_code == 403:
                    raise PanelError("Rebecca API key lacks permission for native admin management")
                if response.status_code == 404:
                    raise PanelError("Rebecca native admin API endpoint is unavailable on this panel version")
                if response.status_code == 409:
                    raise PanelError("Rebecca rejected a duplicate or conflicting admin operation")
                raise PanelError(f"Rebecca admin API returned HTTP {response.status_code}")
            if not response.content:
                return {}
            try:
                result = response.json()
            except ValueError:
                raise PanelError("Rebecca admin API returned invalid JSON") from None
            if isinstance(result, dict) and isinstance(result.get("data"), dict):
                result = result["data"]
            return result
        except PanelError:
            raise
        except Exception as exc:
            raise PanelError(f"Rebecca admin API transport failed ({type(exc).__name__})") from None

    async def _assert_contract(self, connection: PanelConnection) -> None:
        schema = await self._request(connection, "GET", "/openapi.json")
        paths = schema.get("paths", {}) if isinstance(schema, dict) else {}
        required = {("/admin", "post"), ("/admins", "get"),
                    ("/admin/{username}/disable", "post"), ("/admin/{username}/enable", "post"),
                    ("/admin/{username}", "delete"), ("/admin/{username}", "put")}
        missing = sorted(f"{method.upper()} {path}" for path, method in required
                         if not isinstance(paths.get(path), dict) or method not in paths[path])
        if missing:
            raise PanelError("Rebecca native reseller lifecycle is unsupported by this API contract")
        version = (schema.get("info") or {}).get("version") if isinstance(schema, dict) else None
        if not version:
            raise PanelError("Rebecca OpenAPI schema omitted its version; native admin API is unverified")

    @staticmethod
    def _admin_rows(result: Any) -> list[dict[str, Any]]:
        if isinstance(result, list):
            rows = result
        elif isinstance(result, dict):
            rows = result.get("admins")
        else:
            rows = None
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise PanelError("Rebecca admin list response is incompatible; refusing to continue")
        return rows

    async def _validated_services(self, connection: PanelConnection, plan: ResellerPlan) -> list[int]:
        services = self._service_ids(connection, plan)
        if not services:
            raise PanelError("Rebecca native reseller requires at least one explicitly scoped service ID")
        if plan.permission_overrides:
            raise PanelError("Rebecca role permissions use Rebecca's native role defaults; unsupported provider permission overrides must be removed")
        service_result = await self._request(connection, "GET", "/api/v2/services")
        service_rows = service_result.get("services", service_result.get("items", [])) if isinstance(service_result, dict) else service_result
        if not isinstance(service_rows, list) or any(not isinstance(row, dict) for row in service_rows):
            raise PanelError("Rebecca service list response is incompatible; no wallet debit was made")
        available: set[int] = set()
        for row in service_rows:
            try:
                service_id = int(row.get("id", row.get("service_id")))
            except (TypeError, ValueError, OverflowError):
                continue
            if service_id > 0:
                available.add(service_id)
        if any(service_id not in available for service_id in services):
            raise PanelError("Rebecca reseller scope references a service ID not present on this panel")
        if plan.max_users < 0 or plan.data_limit_bytes < 0:
            raise PanelError("Rebecca reseller limits must be non-negative")
        return services

    async def validate_plan(self, *, panel_id: str, username: str, plan: ResellerPlan) -> None:
        connection = self._connection(panel_id)
        await self._assert_contract(connection)
        # Listing admins is a read-only authorization preflight; a later mutation may still be denied.
        result = await self._request(connection, "GET", "/api/admins?" + urlencode({"username": username, "limit": 1}))
        rows = self._admin_rows(result)
        if any(str(row.get("username", "")).casefold() == username.casefold() for row in rows):
            raise PanelError("Rebecca admin username already exists")
        await self._validated_services(connection, plan)

    @staticmethod
    def _service_ids(connection: PanelConnection, plan: ResellerPlan) -> list[int]:
        raw = list(plan.allowed_group_ids)
        if not raw:
            metadata = connection.metadata
            configured = metadata.get("service_ids")
            if isinstance(configured, list): raw = configured
            elif metadata.get("service_id") is not None: raw = [metadata["service_id"]]
        ids: list[int] = []
        for item in raw:
            try: value = int(item)
            except (TypeError, ValueError): raise PanelError("Rebecca service scope must use numeric service IDs") from None
            if value <= 0: raise PanelError("Rebecca service scope must use positive service IDs")
            if value not in ids: ids.append(value)
        return ids

    async def username_exists(self, *, panel_id: str, username: str) -> bool:
        connection = self._connection(panel_id)
        result = await self._request(connection, "GET", "/api/admins?" + urlencode({"username": username, "limit": 10}))
        rows = self._admin_rows(result)
        return any(str(row.get("username", "")).casefold() == username.casefold() for row in rows)

    @staticmethod
    def _password(username: str, length: int = 24) -> str:
        alphabet = string.ascii_letters + string.digits + "!@#$%^&*()-_=+"
        for _ in range(30):
            value = "".join(secrets.choice(alphabet) for _ in range(length))
            if (any(c.isupper() for c in value) and any(c.islower() for c in value)
                    and any(c.isdigit() for c in value) and any(c in "!@#$%^&*()-_=+" for c in value)
                    and username.casefold() not in value.casefold()): return value
        raise PanelError("Could not generate a provider-compliant one-time password")

    async def create_reseller(self, *, panel_id: str, username: str, plan: ResellerPlan,
                              owner_id: str, volume_gb: Decimal | None) -> ProviderReseller:
        connection = self._connection(panel_id)
        # Direct calls must enforce the same duplicate, service-scope and privilege preflight
        # as the purchase workflow; callers cannot bypass it by invoking create_reseller alone.
        await self.validate_plan(panel_id=panel_id, username=username, plan=plan)
        services = self._service_ids(connection, plan)
        password = self._password(username)
        payload: dict[str, Any] = {"username": username, "password": password, "role": "reseller",
            "services": services, "users_limit": int(plan.max_users or 0),
            "data_limit": int(plan.data_limit_bytes or 0)}
        # Do not grant admin-management, system, node, integration, or settings permissions.
        try:
            created = await self._request(connection, "POST", "/api/admin", payload)
        except Exception:
            # The caller's saga keeps funds reserved when provider creation may have committed.
            raise
        if not isinstance(created, dict) or not created.get("username"):
            raise PanelError("Rebecca create-admin response omitted identity; reconcile before retry")
        created_username = str(created["username"])
        return ProviderReseller(created_username, created_username, password)

    async def get_reseller(self, *, panel_id: str, provider_account_id: str) -> Any:
        connection = self._connection(panel_id)
        result = await self._request(connection, "GET", "/api/admins?" + urlencode({"username": provider_account_id, "limit": 10}))
        rows = self._admin_rows(result)
        admin = next((row for row in rows if str(row.get("username", "")).casefold() == provider_account_id.casefold()), None)
        return SimpleNamespace(**admin) if admin else None

    async def update_reseller_permissions(self, *, panel_id: str, provider_account_id: str,
                                          plan: ResellerPlan) -> None:
        """Update native role/scope/limits without granting elevated role permissions."""
        connection = self._connection(panel_id)
        await self._assert_contract(connection)
        services = await self._validated_services(connection, plan)
        payload = {"role": "reseller", "services": services,
                   "users_limit": int(plan.max_users or 0), "data_limit": int(plan.data_limit_bytes or 0)}
        await self._request(connection, "PUT", f"/api/admin/{quote(provider_account_id, safe='')}", payload)

    async def set_reseller_status(self, *, panel_id: str, provider_account_id: str, enabled: bool) -> None:
        connection = self._connection(panel_id)
        await self._assert_contract(connection)
        username = quote(provider_account_id, safe="")
        path = f"/api/admin/{username}/enable" if enabled else f"/api/admin/{username}/disable"
        await self._request(connection, "POST", path, None if enabled else {"reason": "wallet_or_plan_suspension"})

    async def delete_reseller(self, *, panel_id: str, provider_account_id: str) -> None:
        connection = self._connection(panel_id)
        await self._assert_contract(connection)
        await self._request(connection, "DELETE", f"/api/admin/{quote(provider_account_id, safe='')}")


class MultiPanelResellerProvider:
    """Routes native reseller operations by configured provider; unsupported panels fail closed."""
    def __init__(self, store: CustomerDataStore, *, secret_resolver: Callable[[str], str],
                 rebecca_client_factory: Callable[..., Any] | None = None,
                 pasarguard_sdk_factory: Callable[..., Any] | None = None):
        self.store = store
        self.pg = PasarGuardResellerProvider(store, secret_resolver=secret_resolver, sdk_factory=pasarguard_sdk_factory)
        self.rebecca = RebeccaNativeResellerProvider(store, secret_resolver=secret_resolver, client_factory=rebecca_client_factory)
        self.requires_role = False
        self.requires_persistent_credentials = True

    def _provider(self, panel_id: str) -> Any:
        panel = self.store.get_panel_connection(panel_id)
        if not panel: raise PanelError("Reseller panel connection not found")
        if panel["provider"] == "pasarguard": return self.pg
        if panel["provider"] == "rebecca": return self.rebecca
        raise PanelError("Sanaei does not support reseller accounts; no wallet debit was made")

    async def validate_plan(self, *, panel_id: str, username: str, plan: ResellerPlan) -> None:
        await self._provider(panel_id).validate_plan(panel_id=panel_id, username=username, plan=plan)

    async def username_exists(self, *, panel_id: str, username: str) -> bool:
        return await self._provider(panel_id).username_exists(panel_id=panel_id, username=username)

    async def create_reseller(self, *, panel_id: str, username: str, plan: ResellerPlan, owner_id: str, volume_gb: Decimal | None) -> ProviderReseller:
        return await self._provider(panel_id).create_reseller(panel_id=panel_id, username=username, plan=plan, owner_id=owner_id, volume_gb=volume_gb)

    async def delete_reseller(self, *, panel_id: str, provider_account_id: str) -> None:
        await self._provider(panel_id).delete_reseller(panel_id=panel_id, provider_account_id=provider_account_id)

    async def get_reseller(self, *, panel_id: str, provider_account_id: str) -> Any:
        return await self._provider(panel_id).get_reseller(panel_id=panel_id, provider_account_id=provider_account_id)

    async def set_reseller_status(self, *, panel_id: str, provider_account_id: str, enabled: bool) -> None:
        await self._provider(panel_id).set_reseller_status(panel_id=panel_id, provider_account_id=provider_account_id, enabled=enabled)

    async def update_reseller_permissions(self, *, panel_id: str, provider_account_id: str, plan: ResellerPlan) -> None:
        provider = self._provider(panel_id)
        method = getattr(provider, "update_reseller_permissions", None)
        if method is None:
            raise PanelError("This provider does not expose native reseller permission updates")
        await method(panel_id=panel_id, provider_account_id=provider_account_id, plan=plan)


class PlatformResellerProvider:
    """Bot-managed reseller account for panels without a verified native sub-admin API.

    The reseller operates through this bot and receives no panel-admin password. Child user
    creation is scoped and billed locally; the configured panel adapter still provisions users.
    """
    requires_role = False
    requires_persistent_credentials = False

    def __init__(self, store: CustomerDataStore): self.store = store

    async def validate_plan(self, *, panel_id: str, username: str, plan: ResellerPlan) -> None:
        panel = self.store.get_panel_connection(panel_id)
        if not panel or panel["provider"] != "rebecca" or not panel["enabled"]:
            raise PanelError("Sanaei does not support reseller accounts; platform-managed reseller mode is available only for Rebecca")

    async def username_exists(self, *, panel_id: str, username: str) -> bool:
        # Username uniqueness is enforced transactionally by reseller_accounts before this call.
        return False

    async def create_reseller(self, *, panel_id: str, username: str, plan: ResellerPlan,
                              owner_id: str, volume_gb: Decimal | None) -> ProviderReseller:
        panel = self.store.get_panel_connection(panel_id)
        if not panel or panel["provider"] != "rebecca":
            raise PanelError("Sanaei does not support reseller accounts; platform-managed reseller mode is available only for Rebecca")
        # Account is created in the local transactional repository; no unsupported panel admin API is called.
        return ProviderReseller("platform-managed", username, "")

    async def delete_reseller(self, *, panel_id: str, provider_account_id: str) -> None:
        if provider_account_id != "platform-managed":
            raise PanelError("Unexpected provider account identifier")
