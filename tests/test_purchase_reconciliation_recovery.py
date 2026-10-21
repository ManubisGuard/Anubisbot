"""Recovery when provider creation succeeds but local order finalization fails."""

import tempfile
import unittest
from pathlib import Path

from manubisguard.customer_store import CustomerDataStore
from manubisguard.panels.contracts import PanelCapabilities, PanelUser, PanelUserNotFound, UserCreate, UserUpdate
from manubisguard.panels.registry import AdapterRegistry
from manubisguard.sales.service import CustomerPurchaseService, PurchaseNeedsReconciliation


class RecoveryAdapter:
    kind = "pasarguard"

    def __init__(self):
        self.create_calls = 0
        self.created = None

    async def capabilities(self, connection):
        return PanelCapabilities(create_user=True, read_status=True)

    async def health_check(self, connection):
        return {"ok": True}

    async def create_user(self, connection, user: UserCreate):
        self.create_calls += 1
        self.created = PanelUser(
            provider_user_id="provider-recovered",
            username=user.username,
            status="active",
            data_limit_bytes=user.data_limit_bytes,
            expire_at=user.expire_at,
            subscription_url="https://sub.example/recovered",
            metadata={"group_ids": list(user.group_ids)},
        )
        return self.created

    async def get_user(self, connection, username):
        if self.created is None:
            raise PanelUserNotFound("Provider user was not found")
        return self.created

    async def update_user(self, connection, provider_user_id, update: UserUpdate):
        raise NotImplementedError

    async def delete_user(self, connection, provider_user_id):
        raise NotImplementedError


class PurchaseReconciliationRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = CustomerDataStore(str(Path(self.tmp.name) / "db.sqlite3"), tenant_id="recovery-tenant")
        self.store.ensure_customer(123)
        self.store.register_panel_connection(
            panel_id="recovery-panel",
            label="Recovery panel",
            provider="pasarguard",
            base_url="https://panel.example",
            auth_mode="api_key",
            secret_ref="env:RECOVERY_PANEL_KEY",
            enabled=True,
        )
        self.store.put_product(
            product_id="recovery-product",
            title="Recovery product",
            price_minor=2500,
            panel_id="recovery-panel",
            traffic_bytes=1024**3,
            duration_hours=24,
            group_ids=["1"],
        )
        self.store.post_wallet_entry(
            telegram_id=123,
            idempotency_key="recovery-funding",
            amount_minor=5000,
            entry_type="deposit",
        )
        self.adapter = RecoveryAdapter()
        self.registry = AdapterRegistry()
        self.registry.register(self.adapter)
        self.service = CustomerPurchaseService(
            self.store,
            secret_resolver=lambda _: "test-only-secret",
            registry=self.registry,
        )

    def tearDown(self):
        self.tmp.cleanup()

    async def test_local_finalize_failure_after_provider_create_recovers_without_second_create(self):
        original_finalize = self.store.finalize_product_order
        finalize_calls = 0

        def fail_once(*, order_id, service_id, provider_reference):
            nonlocal finalize_calls
            finalize_calls += 1
            if finalize_calls == 1:
                raise RuntimeError("injected local finalize failure")
            return original_finalize(
                order_id=order_id,
                service_id=service_id,
                provider_reference=provider_reference,
            )

        self.store.finalize_product_order = fail_once
        try:
            with self.assertRaises(PurchaseNeedsReconciliation):
                await self.service.buy_from_wallet(
                    telegram_id=123,
                    product_id="recovery-product",
                    idempotency_key="recovery-purchase",
                    now=1000,
                )
        finally:
            self.store.finalize_product_order = original_finalize

        order = self.store.get_order_by_idempotency("recovery-purchase", 123)
        self.assertEqual(order["status"], "provisioning")
        self.assertTrue(order["provider_reference"].startswith("reconcile_required:"))
        self.assertEqual(self.adapter.create_calls, 1)
        self.assertEqual(self.store.wallet_balance(123), 2500)

        local_service = self.store.get_customer_service(order["order_id"], 123)
        self.assertEqual(local_service["provider_user_id"], "provider-recovered")
        inspected = await self.service.inspect_product_order_reconciliation(order_id=order["order_id"])
        self.assertEqual(inspected["comparison"], "target_present")
        self.assertTrue(inspected["identity_verified"])

        result = await self.service.finalize_product_order_target_present(
            order_id=order["order_id"],
            actor_telegram_id=9001,
            reason="Provider create succeeded; local finalization recovered after read-only verification",
        )
        self.assertFalse(result["idempotent_replay"])
        self.assertEqual(self.store.get_order_by_idempotency("recovery-purchase", 123)["status"], "active")
        self.assertEqual(self.store.get_customer_service(order["order_id"], 123)["provider_user_id"], "provider-recovered")
        self.assertEqual(self.store.wallet_balance(123), 2500)
        self.assertEqual(self.adapter.create_calls, 1)
        self.assertEqual(len(self.store.list_product_order_reconciliation_events(order["order_id"])), 1)


if __name__ == "__main__":
    unittest.main()
