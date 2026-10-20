import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from manubisguard.customer_store import CustomerDataStore
from manubisguard.panels.contracts import PanelCapabilities, PanelUser, UserUpdate
from manubisguard.panels.registry import AdapterRegistry
from manubisguard.sales.service import CustomerServiceChangeService, PurchaseNeedsReconciliation


class MutableAdapter:
    kind = "pasarguard"

    def __init__(self, fail_update=False, fail_after_update=False):
        self.user = PanelUser(provider_user_id="provider-1", username="alice-service",
            status="active", data_limit_bytes=1024**3,
            expire_at=datetime(2030, 1, 1, tzinfo=timezone.utc))
        self.fail_update = fail_update
        self.fail_after_update = fail_after_update
        self.update_calls = 0

    async def capabilities(self, connection):
        return PanelCapabilities(update_user=True)

    async def get_user(self, connection, username):
        if username != self.user.username:
            raise LookupError("missing user")
        return self.user

    async def update_user(self, connection, provider_user_id, update: UserUpdate):
        self.update_calls += 1
        if self.fail_update:
            raise TimeoutError("uncertain provider result")
        if provider_user_id != self.user.provider_user_id:
            raise LookupError("wrong provider ID")
        self.user = replace(self.user,
            expire_at=update.expire_at if update.expire_at is not None else self.user.expire_at,
            data_limit_bytes=update.data_limit_bytes if update.data_limit_bytes is not None else self.user.data_limit_bytes,
            status=update.status if update.status is not None else self.user.status)
        if self.fail_after_update:
            raise TimeoutError("provider applied update but response was lost")
        return self.user

    async def health_check(self, connection):
        return {"ok": True}

    async def create_user(self, connection, user):
        raise NotImplementedError

    async def delete_user(self, connection, provider_user_id):
        raise NotImplementedError


class CustomerServiceChangeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = CustomerDataStore(str(Path(self.tmp.name) / "db.sqlite3"), tenant_id="t1")
        self.store.ensure_customer(123)
        self.store.register_panel_connection(panel_id="p1", label="Test", provider="pasarguard",
            base_url="https://panel.example", auth_mode="api_key", secret_ref="env:PANEL_KEY", enabled=True)
        self.store.put_product(product_id="basic", title="Basic", price_minor=700, currency="IRR",
            panel_id="p1", traffic_bytes=1024**3, duration_hours=24, group_ids=["1"])
        self.store.post_wallet_entry(telegram_id=123, idempotency_key="deposit", amount_minor=5000,
            entry_type="deposit")
        self.store.create_metered_service(service_id="svc-1", telegram_id=123,
            product={"product_id":"basic","panel_id":"p1","pricing_mode":"fixed","currency":"IRR",
                "unit_price_minor":0,"duration_hours":24,"traffic_bytes":1024**3,
                "expires_at":"2030-01-01T00:00:00+00:00"},
            provider_user_id="provider-1", username="alice-service",
            subscription_url=None, now=1000)
        self.adapter = MutableAdapter()
        self.registry = AdapterRegistry()
        self.registry.register(self.adapter)
        self.service = CustomerServiceChangeService(self.store, secret_resolver=lambda _: "test-secret",
            registry=self.registry)

    def tearDown(self):
        self.tmp.cleanup()

    async def test_read_only_preview_computes_before_after_without_reserving_wallet(self):
        balance_before = self.store.wallet_balance(123)
        preview = await self.service.preview(telegram_id=123, service_id="svc-1",
            change_type="renewal", now=1000)
        self.assertEqual(preview["before_expires_at"], "2030-01-01T00:00:00+00:00")
        self.assertEqual(preview["target_expires_at"], "2030-01-02T00:00:00+00:00")
        self.assertEqual(preview["amount_minor"], 700)
        self.assertEqual(self.store.wallet_balance(123), balance_before)
        self.assertEqual(self.store.list_service_change_operations(status="all"), [])

    async def test_renewal_extends_existing_expiry_and_charges_once(self):
        result = await self.service.renew(telegram_id=123, service_id="svc-1",
            idempotency_key="renew-once", now=1000)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.adapter.user.expire_at, datetime(2030, 1, 2, tzinfo=timezone.utc))
        self.assertEqual(self.store.get_customer_service("svc-1", 123)["expires_at"],
            "2030-01-02T00:00:00+00:00")
        self.assertEqual(self.store.wallet_balance(123), 4300)
        replay = await self.service.renew(telegram_id=123, service_id="svc-1",
            idempotency_key="renew-once", now=1001)
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(self.adapter.update_calls, 1)
        self.assertEqual(self.store.wallet_balance(123), 4300)

    async def test_traffic_topup_increases_absolute_limit_and_charges_once(self):
        result = await self.service.top_up_traffic(telegram_id=123, service_id="svc-1",
            idempotency_key="topup-once", now=1000)
        self.assertEqual(result["traffic_limit_bytes"], 2 * 1024**3)
        self.assertEqual(self.adapter.user.data_limit_bytes, 2 * 1024**3)
        self.assertEqual(self.store.get_customer_service("svc-1", 123)["traffic_limit_bytes"], 2 * 1024**3)
        self.assertEqual(self.store.wallet_balance(123), 4300)

    async def test_ambiguous_provider_update_is_not_refunded_or_retried(self):
        self.adapter.fail_update = True
        with self.assertRaises(PurchaseNeedsReconciliation):
            await self.service.top_up_traffic(telegram_id=123, service_id="svc-1",
                idempotency_key="topup-uncertain", now=1000)
        balance = self.store.wallet_balance(123)
        with self.assertRaises(PurchaseNeedsReconciliation):
            await self.service.top_up_traffic(telegram_id=123, service_id="svc-1",
                idempotency_key="topup-uncertain", now=1001)
        self.assertEqual(self.adapter.update_calls, 1)
        self.assertEqual(self.store.wallet_balance(123), balance)
        operations = self.store.list_service_change_operations(status="reconcile_required")
        self.assertEqual(len(operations), 1)
        self.assertEqual(operations[0]["before_traffic_limit_bytes"], 1024**3)
        self.assertEqual(operations[0]["target_traffic_limit_bytes"], 2 * 1024**3)
        comparison = await self.service.inspect_reconciliation(operation_id=operations[0]["operation_id"])
        self.assertEqual(comparison["comparison"], "before_present")
        self.assertEqual(comparison["observed_value"], 1024**3)

    async def test_read_only_reconciliation_detects_target_applied_before_timeout(self):
        self.adapter.fail_after_update = True
        with self.assertRaises(PurchaseNeedsReconciliation):
            await self.service.top_up_traffic(telegram_id=123, service_id="svc-1",
                idempotency_key="topup-applied-timeout", now=1000)
        operations = self.store.list_service_change_operations(status="reconcile_required")
        self.assertEqual(len(operations), 1)
        comparison = await self.service.inspect_reconciliation(operation_id=operations[0]["operation_id"])
        self.assertEqual(comparison["comparison"], "target_present")
        self.assertEqual(comparison["observed_value"], 2 * 1024**3)
        self.assertEqual(self.store.wallet_balance(123), 4300)
        self.assertEqual(self.adapter.update_calls, 1)


    async def test_read_only_reconciliation_supports_rebecca_after_ambiguous_update(self):
        self.store.register_panel_connection(panel_id="p1", label="Rebecca", provider="rebecca",
            base_url="https://panel.example", auth_mode="api_key", secret_ref="env:PANEL_KEY", enabled=True)
        self.adapter = MutableAdapter(fail_after_update=True)
        self.adapter.kind = "rebecca"
        self.registry = AdapterRegistry()
        self.registry.register(self.adapter)
        self.service = CustomerServiceChangeService(self.store, secret_resolver=lambda _: "test-secret",
            registry=self.registry)
        with self.assertRaises(PurchaseNeedsReconciliation):
            await self.service.top_up_traffic(telegram_id=123, service_id="svc-1",
                idempotency_key="rebecca-ambiguous-topup", now=1000)
        operation = self.store.list_service_change_operations(status="reconcile_required")[0]
        comparison = await self.service.inspect_reconciliation(operation_id=operation["operation_id"])
        self.assertEqual(comparison["comparison"], "target_present")
        self.assertTrue(comparison["identity_verified"])
        self.assertEqual(comparison["observed_value"], 2 * 1024**3)
        self.assertEqual(self.adapter.update_calls, 1)
        self.assertEqual(self.store.wallet_balance(123), 4300)

    async def test_read_only_reconciliation_supports_sanaei_after_ambiguous_update(self):
        self.store.register_panel_connection(panel_id="p1", label="Sanaei", provider="sanaei",
            base_url="https://panel.example", auth_mode="access_token", secret_ref="env:PANEL_KEY", enabled=True)
        self.adapter = MutableAdapter(fail_after_update=True)
        self.adapter.kind = "sanaei"
        self.registry = AdapterRegistry()
        self.registry.register(self.adapter)
        self.service = CustomerServiceChangeService(self.store, secret_resolver=lambda _: "test-secret",
            registry=self.registry)
        with self.assertRaises(PurchaseNeedsReconciliation):
            await self.service.top_up_traffic(telegram_id=123, service_id="svc-1",
                idempotency_key="sanaei-ambiguous-topup", now=1000)
        operation = self.store.list_service_change_operations(status="reconcile_required")[0]
        comparison = await self.service.inspect_reconciliation(operation_id=operation["operation_id"])
        self.assertEqual(comparison["comparison"], "target_present")
        self.assertTrue(comparison["identity_verified"])
        self.assertEqual(comparison["observed_value"], 2 * 1024**3)
        self.assertEqual(self.adapter.update_calls, 1)
        self.assertEqual(self.store.wallet_balance(123), 4300)

    async def test_reconciliation_refuses_username_mismatch_even_when_provider_id_matches(self):
        self.adapter.fail_update = True
        with self.assertRaises(PurchaseNeedsReconciliation):
            await self.service.top_up_traffic(telegram_id=123, service_id="svc-1",
                idempotency_key="topup-username-mismatch", now=1000)
        operation = self.store.list_service_change_operations(status="reconcile_required")[0]
        async def mismatched_user(connection, username):
            return replace(self.adapter.user, username="different-username")
        self.adapter.get_user = mismatched_user
        comparison = await self.service.inspect_reconciliation(operation_id=operation["operation_id"])
        self.assertEqual(comparison["comparison"], "identity_mismatch")
        self.assertIsNone(comparison["observed_value"])

    async def test_reconciliation_refuses_provider_identity_mismatch(self):
        self.adapter.fail_update = True
        with self.assertRaises(PurchaseNeedsReconciliation):
            await self.service.top_up_traffic(telegram_id=123, service_id="svc-1",
                idempotency_key="topup-identity-mismatch", now=1000)
        operation = self.store.list_service_change_operations(status="reconcile_required")[0]
        self.adapter.user = replace(self.adapter.user, provider_user_id="different-provider-id")
        comparison = await self.service.inspect_reconciliation(operation_id=operation["operation_id"])
        self.assertEqual(comparison["comparison"], "identity_mismatch")
        self.assertIsNone(comparison["observed_value"])

    async def test_reconciliation_read_failure_is_reported_without_mutation(self):
        self.adapter.fail_update = True
        with self.assertRaises(PurchaseNeedsReconciliation):
            await self.service.top_up_traffic(telegram_id=123, service_id="svc-1",
                idempotency_key="topup-read-failure", now=1000)
        operation = self.store.list_service_change_operations(status="reconcile_required")[0]
        original_get_user = self.adapter.get_user

        async def failing_get_user(connection, username):
            raise TimeoutError("sensitive detail must not be returned")

        self.adapter.get_user = failing_get_user
        comparison = await self.service.inspect_reconciliation(operation_id=operation["operation_id"])
        self.assertEqual(comparison["comparison"], "read_failed")
        self.assertEqual(comparison["reason_code"], "TimeoutError")
        self.assertIsNone(comparison["observed_value"])
        self.assertNotIn("sensitive detail", str(comparison))
        self.adapter.get_user = original_get_user

    async def test_finalize_requires_fresh_target_and_never_replays_provider_mutation(self):
        self.adapter.fail_after_update = True
        with self.assertRaises(PurchaseNeedsReconciliation):
            await self.service.top_up_traffic(telegram_id=123, service_id="svc-1",
                idempotency_key="finalize-applied-timeout", now=1000)
        operation = self.store.list_service_change_operations(status="reconcile_required")[0]
        before = await self.service.inspect_reconciliation(operation_id=operation["operation_id"])
        self.assertEqual(before["comparison"], "target_present")
        result = await self.service.finalize_target_present(operation_id=operation["operation_id"],
            actor_telegram_id=9001, reason="Confirmed target on fresh panel read")
        self.assertFalse(result["idempotent_replay"])
        self.assertEqual(self.store.get_service_change_operation(operation["operation_id"])["status"], "completed")
        self.assertEqual(self.store.wallet_balance(123), 4300)
        self.assertEqual(self.adapter.update_calls, 1)
        replay = await self.service.finalize_target_present(operation_id=operation["operation_id"],
            actor_telegram_id=9001, reason="Confirmed target on fresh panel read")
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(self.adapter.update_calls, 1)
        self.assertEqual(len(self.store.list_service_change_events(operation["operation_id"])), 1)

    async def test_finalize_keeps_reservation_on_before_present_or_identity_mismatch(self):
        self.adapter.fail_update = True
        with self.assertRaises(PurchaseNeedsReconciliation):
            await self.service.top_up_traffic(telegram_id=123, service_id="svc-1",
                idempotency_key="finalize-before-timeout", now=1000)
        operation = self.store.list_service_change_operations(status="reconcile_required")[0]
        balance = self.store.wallet_balance(123)
        with self.assertRaisesRegex(Exception, "before_present"):
            await self.service.finalize_target_present(operation_id=operation["operation_id"],
                actor_telegram_id=9001, reason="Attempt finalize before")
        self.assertEqual(self.store.get_service_change_operation(operation["operation_id"])["status"], "reconcile_required:timeouterror")
        self.assertEqual(self.store.wallet_balance(123), balance)
        self.adapter.user = replace(self.adapter.user, provider_user_id="other-id", data_limit_bytes=2 * 1024**3)
        with self.assertRaisesRegex(Exception, "identity_mismatch"):
            await self.service.finalize_target_present(operation_id=operation["operation_id"],
                actor_telegram_id=9001, reason="Identity mismatch")
        self.assertEqual(self.store.wallet_balance(123), balance)
        self.assertEqual(self.store.list_service_change_events(operation["operation_id"]), [])

    async def test_reconciliation_rejects_completed_operation(self):
        result = await self.service.top_up_traffic(telegram_id=123, service_id="svc-1",
            idempotency_key="topup-completed-no-reconcile", now=1000)
        self.assertEqual(result["status"], "completed")
        with self.assertRaisesRegex(Exception, "Only pending or reconciliation-required"):
            await self.service.inspect_reconciliation(operation_id=result["operation_id"])


if __name__ == "__main__":
    unittest.main()
