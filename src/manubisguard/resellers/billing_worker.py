"""Scheduled native and bot-managed reseller billing, expiry, and suspension worker."""
from __future__ import annotations
from typing import Callable, Any

from manubisguard.customer_store import CustomerDataStore
from manubisguard.panels.contracts import PanelConnection, PanelError, UserUpdate
from manubisguard.panels.registry import AdapterRegistry, default_registry
from manubisguard.sales.billing_worker import CustomerBillingWorker
from .providers import MultiPanelResellerProvider


class ResellerBillingWorker:
    def __init__(self, store: CustomerDataStore, *, secret_resolver: Callable[[str], str],
                 provider: Any | None = None,
                 registry: AdapterRegistry | None = None):
        self.store = store
        self.provider = provider or MultiPanelResellerProvider(store, secret_resolver=secret_resolver)
        self.registry = registry or default_registry(secret_resolver=secret_resolver)

    def _panel_connection(self, panel_id: str) -> PanelConnection:
        row = self.store.get_panel_connection(panel_id)
        if not row: raise PanelError("Panel connection not found")
        return PanelConnection(id=str(row["panel_id"]), tenant_id=self.store.tenant_id,
            name=str(row["label"]), kind=str(row["provider"]), base_url=str(row["base_url"]),
            auth_mode=str(row["auth_mode"]), secret_ref=str(row["secret_ref"]),
            enabled=bool(row["enabled"]), verify_tls=True, metadata=row.get("metadata") or {})

    @staticmethod
    def _used_bytes(admin: Any) -> int:
        for key in ("used_traffic", "used_bytes", "used_traffic_bytes", "users_usage", "created_traffic"):
            value = getattr(admin, key, None)
            if value is not None:
                try:
                    value = int(value)
                    if value >= 0: return value
                except (TypeError, ValueError): pass
        raise PanelError("PasarGuard admin response omitted a recognized traffic counter")

    @staticmethod
    def _service_identifier(connection: PanelConnection, service: dict[str, Any]) -> str:
        return str(service.get("username") or service.get("provider_user_id") or "") if connection.kind in {"rebecca", "sanaei"} else str(service.get("provider_user_id") or "")

    async def _set_account_enabled(self, account: dict[str, Any], enabled: bool) -> None:
        if account["provider_account_id"] != "platform-managed":
            await self.provider.set_reseller_status(panel_id=str(account["panel_id"]),
                provider_account_id=str(account["provider_account_id"]), enabled=enabled)
            return
        # For Rebecca/Sanaei, native sub-admin APIs are not verified; enforce the plan locally
        # by changing all child services through the supported user adapter.
        for service in self.store.list_reseller_child_services(str(account["account_id"])):
            connection = self._panel_connection(str(service["panel_id"]))
            adapter = self.registry.get(connection.kind)
            identifier = self._service_identifier(connection, service)
            await adapter.update_user(connection, identifier, UserUpdate(status="active" if enabled else "disabled"))

    async def _read_account_usage(self, account: dict[str, Any]) -> int:
        if account["provider_account_id"] != "platform-managed":
            admin = await self.provider.get_reseller(panel_id=str(account["panel_id"]),
                provider_account_id=str(account["provider_account_id"]))
            if admin is None: raise PanelError("Reseller admin not found on PasarGuard")
            return self._used_bytes(admin)
        counters: dict[str, int] = {}
        for service in self.store.list_reseller_child_services(str(account["account_id"])):
            connection = self._panel_connection(str(service["panel_id"]))
            adapter = self.registry.get(connection.kind)
            identifier = self._service_identifier(connection, service)
            usage = await adapter.get_usage(connection, identifier)
            counters[str(service["service_id"])] = CustomerBillingWorker._extract_used_bytes(usage)
        return self.store.update_reseller_usage_counter(account_id=str(account["account_id"]), service_counters=counters)

    async def run_once(self, *, now: int, limit: int = 100) -> dict[str, int]:
        result = {"billed": 0, "suspended": 0, "expired": 0, "expiration_failed": 0,
                  "suspension_retry_failed": 0, "metering_failed": 0, "skipped": 0}
        for account in self.store.list_due_reseller_expirations(now=now, limit=limit):
            try:
                await self._set_account_enabled(account, False)
                self.store.mark_reseller_expired(account_id=str(account["account_id"]))
                result["expired"] += 1
            except Exception:
                result["expiration_failed"] += 1
        for account in self.store.list_reseller_suspensions_pending(limit):
            try:
                await self._set_account_enabled(account, False)
                self.store.confirm_reseller_suspended(account_id=str(account["account_id"]))
                result["suspended"] += 1
            except Exception:
                result["suspension_retry_failed"] += 1
        for account in self.store.list_billable_reseller_accounts(limit):
            try:
                used = await self._read_account_usage(account) if account["pricing_mode"] == "usage" else None
                bill = self.store.bill_reseller_account(account_id=str(account["account_id"]),
                    now=now, used_traffic_bytes=used)
                if bill.get("suspend_required"):
                    pending = next((a for a in self.store.list_reseller_suspensions_pending(limit)
                                    if a["account_id"] == account["account_id"]), None)
                    if pending:
                        try:
                            await self._set_account_enabled(pending, False)
                            self.store.confirm_reseller_suspended(account_id=str(account["account_id"]))
                            result["suspended"] += 1
                        except Exception:
                            result["suspension_retry_failed"] += 1
                elif bill.get("charged_minor", 0) > 0:
                    result["billed"] += 1
                else:
                    result["skipped"] += 1
            except Exception:
                result["metering_failed"] += 1
        return result

    async def resume(self, *, account_id: str, now: int, telegram_id: int | None = None) -> None:
        account = self.store.get_reseller_account(account_id, telegram_id)
        if not account: raise ValueError("Reseller account not found or not owned by this customer")
        await self._set_account_enabled(account, True)
        try:
            self.store.resume_reseller_billing(account_id=account_id, now=now)
        except Exception:
            try:
                await self._set_account_enabled(account, False)
            finally:
                raise
