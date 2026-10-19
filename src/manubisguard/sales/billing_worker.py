"""Customer hourly/usage billing worker. Caller schedules this periodically (recommended 60s)."""
from __future__ import annotations
from typing import Callable, Any

from manubisguard.customer_store import CustomerDataStore
from manubisguard.panels.contracts import PanelConnection, PanelError, UserUpdate
from manubisguard.panels.registry import AdapterRegistry, default_registry


class CustomerBillingWorker:
    def __init__(self, store: CustomerDataStore, *, secret_resolver: Callable[[str], str],
                 registry: AdapterRegistry | None = None):
        self.store = store
        self.registry = registry or default_registry(secret_resolver=secret_resolver)

    def _connection(self, panel_id: str, reseller_account_id: str = "") -> PanelConnection:
        row = self.store.get_panel_connection(panel_id)
        if not row:
            raise PanelError("Panel connection not found")
        metadata = dict(row.get("metadata") or {})
        auth_mode = str(row["auth_mode"]); secret_ref = str(row["secret_ref"])
        if reseller_account_id and row["provider"] == "pasarguard":
            account = self.store.get_reseller_account(reseller_account_id)
            if account and account["provider_account_id"] != "platform-managed":
                metadata["username"] = str(account["username"])
                auth_mode = "username_password"; secret_ref = f"vault:{reseller_account_id}"
        return PanelConnection(id=str(row["panel_id"]), tenant_id=self.store.tenant_id,
            name=str(row["label"]), kind=str(row["provider"]), base_url=str(row["base_url"]),
            auth_mode=auth_mode, secret_ref=secret_ref,
            enabled=bool(row["enabled"]), verify_tls=True, metadata=metadata)

    @staticmethod
    def _provider_identifier(connection: PanelConnection, service: dict[str, Any]) -> str:
        return str(service.get("username") or service.get("provider_user_id") or "") if connection.kind in {"rebecca", "sanaei"} else str(service.get("provider_user_id") or "")

    @staticmethod
    def _extract_used_bytes(data: Any) -> int:
        if not isinstance(data, dict): raise PanelError("Usage response is not an object")
        for key in ("used_bytes", "used_traffic", "usedTraffic", "total_usage", "totalUsage"):
            if key in data and data[key] is not None:
                try: value = int(data[key])
                except (TypeError, ValueError): continue
                if value >= 0: return value
        for parent in ("data", "usage", "obj", "result"):
            if isinstance(data.get(parent), dict):
                try: return CustomerBillingWorker._extract_used_bytes(data[parent])
                except PanelError: pass
        for up_key, down_key in (("up", "down"), ("upload", "download"), ("uplink", "downlink")):
            if up_key in data and down_key in data:
                try: value = int(data[up_key]) + int(data[down_key])
                except (TypeError, ValueError): continue
                if value >= 0: return value
        raise PanelError("Usage response omitted a recognized byte counter")

    async def _suspend_pending(self, service: dict[str, Any]) -> bool:
        connection = self._connection(str(service["panel_id"]), str(service.get("reseller_account_id") or ""))
        adapter = self.registry.get(connection.kind)
        identifier = self._provider_identifier(connection, service)
        await adapter.update_user(connection, identifier, UserUpdate(status="disabled"))
        self.store.confirm_customer_service_suspended(service_id=str(service["service_id"]))
        return True

    async def resume(self, *, service_id: str, telegram_id: int, now: int) -> None:
        service = self.store.get_customer_service(service_id, telegram_id)
        if not service:
            raise ValueError("Service not found or not owned by this customer")
        if service["billing_status"] != "suspended_balance":
            raise ValueError("Service is not suspended for wallet balance")
        connection = self._connection(str(service["panel_id"]), str(service.get("reseller_account_id") or ""))
        adapter = self.registry.get(connection.kind)
        identifier = self._provider_identifier(connection, service)
        await adapter.update_user(connection, identifier, UserUpdate(status="active"))
        try:
            self.store.resume_customer_service_billing(service_id=service_id, now=now)
        except Exception:
            try:
                await adapter.update_user(connection, identifier, UserUpdate(status="disabled"))
            finally:
                raise

    async def run_once(self, *, now: int, limit: int = 100) -> dict[str, int]:
        result = {"billed": 0, "suspended": 0, "suspension_retry_failed": 0, "metering_failed": 0, "skipped": 0}
        for service in self.store.pending_customer_service_suspensions(limit):
            try:
                await self._suspend_pending(service)
                result["suspended"] += 1
            except Exception:
                result["suspension_retry_failed"] += 1
        for service in self.store.list_billable_customer_services(limit):
            try:
                connection = self._connection(str(service["panel_id"]))
                adapter = self.registry.get(connection.kind)
                identifier = self._provider_identifier(connection, service)
                used = None
                if service["pricing_mode"] == "usage":
                    usage = await adapter.get_usage(connection, identifier)
                    used = self._extract_used_bytes(usage)
                accrued = self.store.bill_customer_service(service_id=str(service["service_id"]), now=now,
                                                           used_traffic_bytes=used)
                if accrued.get("suspend_required"):
                    pending = next((s for s in self.store.pending_customer_service_suspensions(limit)
                                    if s["service_id"] == service["service_id"]), None)
                    if pending:
                        try:
                            await self._suspend_pending(pending)
                            result["suspended"] += 1
                        except Exception:
                            result["suspension_retry_failed"] += 1
                elif accrued.get("charged_minor", 0) > 0:
                    result["billed"] += 1
                else:
                    result["skipped"] += 1
            except Exception:
                result["metering_failed"] += 1
        return result
