import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from manubisguard.customer_store import CustomerDataStore, CustomerStoreError
from manubisguard.notifications.trust_channel import SQLiteNotificationOutbox


class CustomerDataStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "customer.sqlite3")
        self.store = CustomerDataStore(self.path, tenant_id="bot-alpha")
        self.store.ensure_customer(1001, "alice", "Alice")

    def tearDown(self):
        self.tmp.cleanup()

    def test_service_operation_read_paths_migrate_legacy_table_additively(self):
        with sqlite3.connect(self.path) as db:
            db.execute("""CREATE TABLE customer_service_operations (
                tenant_id TEXT NOT NULL, operation_id TEXT NOT NULL, telegram_id INTEGER NOT NULL,
                service_id TEXT NOT NULL, change_type TEXT NOT NULL, idempotency_key TEXT NOT NULL,
                amount_minor INTEGER NOT NULL, currency TEXT NOT NULL, quantity INTEGER NOT NULL,
                status TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(tenant_id,operation_id), UNIQUE(tenant_id,idempotency_key)
            )""")
        self.assertEqual(self.store.list_service_change_operations(status="all"), [])
        self.assertIsNone(self.store.get_service_change_operation("legacy-operation"))
        with sqlite3.connect(self.path) as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(customer_service_operations)")}
        self.assertTrue({
            "before_expires_at", "before_traffic_limit_bytes",
            "target_expires_at", "target_traffic_limit_bytes",
        }.issubset(columns))

    def test_service_change_reserves_funds_once_and_enforces_owner(self):
        self.store.register_panel_connection(panel_id="panel-change", label="Panel", provider="pasarguard",
            base_url="https://panel.example", auth_mode="api_key", secret_ref="env:PANEL_CHANGE", enabled=True)
        self.store.put_product(product_id="change-product", title="Change product", price_minor=700,
            currency="IRR", duration_hours=24, traffic_bytes=1024**3, panel_id="panel-change", group_ids=["1"])
        self.store.post_wallet_entry(telegram_id=1001, idempotency_key="change-funds", amount_minor=3000,
            entry_type="deposit")
        self.store.create_metered_service(service_id="change-service", telegram_id=1001,
            product={"product_id":"change-product","panel_id":"panel-change","pricing_mode":"fixed",
                     "currency":"IRR","unit_price_minor":0,"duration_hours":24,"traffic_bytes":1024**3,
                     "expires_at":"2030-01-01T00:00:00+00:00"},
            provider_user_id="provider-1", username="alice-service", subscription_url=None, now=1000)
        with self.assertRaisesRegex(CustomerStoreError, "not found for this customer"):
            self.store.reserve_service_change(operation_id="op-owner", telegram_id=2002,
                service_id="change-service", change_type="renewal", idempotency_key="owner-check", now=1000)
        first = self.store.reserve_service_change(operation_id="op-change-1", telegram_id=1001,
            service_id="change-service", change_type="renewal", idempotency_key="change-key-1", now=1000)
        self.assertEqual(first["amount_minor"], 700)
        self.assertEqual(first["quantity"], 24)
        self.assertEqual(self.store.wallet_balance(1001), 2300)
        replay = self.store.reserve_service_change(operation_id="op-change-2", telegram_id=1001,
            service_id="change-service", change_type="renewal", idempotency_key="change-key-1", now=1001)
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(replay["operation_id"], "op-change-1")
        self.assertEqual(self.store.wallet_balance(1001), 2300)
        self.store.fail_service_change_before_provider(operation_id="op-change-1", reason_code="test")
        self.assertEqual(self.store.wallet_balance(1001), 3000)
        self.store.fail_service_change_before_provider(operation_id="op-change-1", reason_code="retry")
        self.assertEqual(self.store.wallet_balance(1001), 3000)

    def test_service_change_reconciliation_is_listed_and_tenant_scoped(self):
        self.store.register_panel_connection(panel_id="panel-reconcile", label="Panel", provider="pasarguard",
            base_url="https://panel.example", auth_mode="api_key", secret_ref="env:PANEL_RECONCILE", enabled=True)
        self.store.put_product(product_id="reconcile-product", title="Reconcile product", price_minor=500,
            currency="IRR", duration_hours=12, traffic_bytes=1024, panel_id="panel-reconcile", group_ids=["1"])
        self.store.post_wallet_entry(telegram_id=1001, idempotency_key="reconcile-funds", amount_minor=1000,
            entry_type="deposit")
        self.store.create_metered_service(service_id="reconcile-service", telegram_id=1001,
            product={"product_id":"reconcile-product","panel_id":"panel-reconcile","pricing_mode":"fixed",
                     "currency":"IRR","unit_price_minor":0,"duration_hours":12,"traffic_bytes":1024,
                     "expires_at":"2030-01-01T00:00:00+00:00"},
            provider_user_id="provider-reconcile", username="reconcile-user", subscription_url=None, now=1000)
        op = self.store.reserve_service_change(operation_id="op-reconcile", telegram_id=1001,
            service_id="reconcile-service", change_type="renewal", idempotency_key="reconcile-key", now=1000)
        self.store.flag_service_change_reconciliation(operation_id=op["operation_id"], reason_code="TimeoutError")
        listed = self.store.list_service_change_operations(status="reconcile_required")
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["operation_id"], "op-reconcile")
        self.assertEqual(listed[0]["status"], "reconcile_required:timeouterror")
        self.assertEqual(self.store.list_service_change_operations(status="pending"), [])
        self.assertEqual(self.store.get_service_change_operation("op-reconcile")["username"], "reconcile-user")
        self.assertIsNone(CustomerDataStore(self.path, tenant_id="bot-beta").get_service_change_operation("op-reconcile"))

    def test_wallet_ledger_is_idempotent(self):
        first = self.store.post_wallet_entry(
            telegram_id=1001, idempotency_key="deposit:abc", amount_minor=5000,
            entry_type="deposit", note="verified test deposit",
        )
        second = self.store.post_wallet_entry(
            telegram_id=1001, idempotency_key="deposit:abc", amount_minor=5000,
            entry_type="deposit", note="retry",
        )
        self.assertEqual(first, second)
        self.assertEqual(self.store.wallet_balance(1001), 5000)

    def test_wallet_ledger_is_append_only_at_database_level(self):
        ledger_id = self.store.post_wallet_entry(
            telegram_id=1001, idempotency_key="append-only-entry",
            amount_minor=1250, entry_type="deposit",
        )
        with sqlite3.connect(self.path) as db:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                db.execute("UPDATE wallet_ledger SET amount_minor=9999 WHERE ledger_id=?", (ledger_id,))
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                db.execute("DELETE FROM wallet_ledger WHERE ledger_id=?", (ledger_id,))
        self.assertEqual(self.store.wallet_balance(1001), 1250)
        # Corrections must be represented by a separate compensating entry.
        self.store.post_wallet_entry(
            telegram_id=1001, idempotency_key="append-only-reversal",
            amount_minor=-250, entry_type="reversal", reference="append-only-entry",
        )
        self.assertEqual(self.store.wallet_balance(1001), 1000)

    def test_conflicting_idempotency_key_is_rejected(self):
        self.store.post_wallet_entry(
            telegram_id=1001, idempotency_key="same-key", amount_minor=5000,
            entry_type="deposit",
        )
        with self.assertRaisesRegex(CustomerStoreError, "different transaction data"):
            self.store.post_wallet_entry(
                telegram_id=1001, idempotency_key="same-key", amount_minor=7000,
                entry_type="deposit",
            )
        self.assertEqual(self.store.wallet_balance(1001), 5000)

    def test_order_snapshots_price_and_is_idempotent(self):
        self.store.put_product(product_id="basic", title="Basic", price_minor=5000, currency="IRR")
        first = self.store.create_order(order_id="order-1", telegram_id=1001,
                                        product_id="basic", idempotency_key="buy-1")
        self.store.put_product(product_id="basic", title="Basic v2", price_minor=9000, currency="IRR")
        retry = self.store.create_order(order_id="order-retry", telegram_id=1001,
                                        product_id="basic", idempotency_key="buy-1")
        self.assertEqual(first["order_id"], retry["order_id"])
        self.assertEqual(retry["amount_minor"], 5000)

    def test_order_state_machine_rejects_skipped_fulfillment(self):
        self.store.put_product(product_id="basic", title="Basic", price_minor=5000)
        self.store.create_order(order_id="order-2", telegram_id=1001,
                                product_id="basic", idempotency_key="buy-2")
        with self.assertRaisesRegex(CustomerStoreError, "Invalid order transition"):
            self.store.transition_order(order_id="order-2", new_status="active")
        self.store.transition_order(order_id="order-2", new_status="paid")
        self.store.transition_order(order_id="order-2", new_status="provisioning")
        self.store.transition_order(order_id="order-2", new_status="active", provider_reference="provider-svc")

    def test_verified_payment_credits_wallet_once(self):
        self.store.create_payment_attempt(
            payment_id="pay-1", telegram_id=1001, provider="mock", amount_minor=2500,
            currency="IRR", idempotency_key="intent-1",
        )
        first = self.store.settle_verified_wallet_deposit(
            payment_id="pay-1", verified_amount_minor=2500,
            verified_currency="IRR", external_reference="provider-tx-1",
        )
        second = self.store.settle_verified_wallet_deposit(
            payment_id="pay-1", verified_amount_minor=2500,
            verified_currency="IRR", external_reference="provider-tx-1",
        )
        self.assertEqual(first, second)
        self.assertEqual(self.store.wallet_balance(1001), 2500)

    def test_payment_amount_mismatch_does_not_credit_wallet(self):
        self.store.create_payment_attempt(
            payment_id="pay-2", telegram_id=1001, provider="mock", amount_minor=2500,
            currency="IRR", idempotency_key="intent-2",
        )
        with self.assertRaisesRegex(CustomerStoreError, "does not match"):
            self.store.settle_verified_wallet_deposit(
                payment_id="pay-2", verified_amount_minor=2501,
                verified_currency="IRR", external_reference="provider-tx-2",
            )
        self.assertEqual(self.store.wallet_balance(1001), 0)

    def test_external_payment_reference_cannot_credit_two_intents(self):
        self.store.create_payment_attempt(
            payment_id="pay-reference-a", telegram_id=1001, provider="manual",
            amount_minor=900, currency="IRR", idempotency_key="intent-reference-a",
        )
        self.store.create_payment_attempt(
            payment_id="pay-reference-b", telegram_id=1001, provider="manual",
            amount_minor=900, currency="IRR", idempotency_key="intent-reference-b",
        )
        self.store.settle_verified_wallet_deposit(
            payment_id="pay-reference-a", verified_amount_minor=900,
            verified_currency="IRR", external_reference="same-external-reference",
        )
        with self.assertRaisesRegex(CustomerStoreError, "already assigned"):
            self.store.settle_verified_wallet_deposit(
                payment_id="pay-reference-b", verified_amount_minor=900,
                verified_currency="IRR", external_reference="same-external-reference",
            )
        self.assertEqual(self.store.get_payment_attempt("pay-reference-a")["status"], "verified")
        self.assertEqual(self.store.get_payment_attempt("pay-reference-b")["status"], "pending")
        self.assertEqual(self.store.wallet_balance(1001), 900)

    def test_verified_payment_cannot_be_overwritten_as_failed(self):
        self.store.create_payment_attempt(
            payment_id="pay-race-verified", telegram_id=1001, provider="manual",
            amount_minor=1800, currency="IRR", idempotency_key="intent-race-verified",
        )
        self.store.settle_verified_wallet_deposit(
            payment_id="pay-race-verified", verified_amount_minor=1800,
            verified_currency="IRR", external_reference="manual-ref-race",
            verified_by=9001,
        )
        with self.assertRaisesRegex(CustomerStoreError, "Only pending payments"):
            self.store.fail_payment_attempt(payment_id="pay-race-verified", failure_code="late_failure")
        attempt = self.store.get_payment_attempt("pay-race-verified")
        self.assertEqual(attempt["status"], "verified")
        self.assertEqual(self.store.wallet_balance(1001), 1800)

    def test_fail_payment_attempt_is_idempotent_for_already_failed_attempt(self):
        self.store.create_payment_attempt(
            payment_id="pay-failed-once", telegram_id=1001, provider="manual",
            amount_minor=700, currency="IRR", idempotency_key="intent-failed-once",
        )
        self.store.fail_payment_attempt(payment_id="pay-failed-once", failure_code="receipt_not_confirmed")
        self.store.fail_payment_attempt(payment_id="pay-failed-once", failure_code="second_call")
        attempt = self.store.get_payment_attempt("pay-failed-once")
        self.assertEqual(attempt["status"], "failed")
        self.assertEqual(attempt["failure_code"], "receipt_not_confirmed")
        self.assertEqual(self.store.wallet_balance(1001), 0)

    def test_payment_idempotency_conflict_and_pending_multiplicity(self):
        self.store.create_payment_attempt(
            payment_id="pay-3", telegram_id=1001, provider="mock", amount_minor=1000,
            currency="IRR", idempotency_key="intent-3",
        )
        self.store.create_payment_attempt(
            payment_id="pay-4", telegram_id=1001, provider="mock", amount_minor=2000,
            currency="IRR", idempotency_key="intent-4",
        )
        with self.assertRaisesRegex(CustomerStoreError, "different data"):
            self.store.create_payment_attempt(
                payment_id="pay-5", telegram_id=1001, provider="mock", amount_minor=3000,
                currency="IRR", idempotency_key="intent-3",
            )

    def test_product_supports_metered_modes_and_rejects_missing_rate(self):
        self.store.put_product(product_id="hourly", title="Hourly", price_minor=0,
                               pricing_mode="hourly", unit_price_minor=600)
        self.assertEqual(self.store.list_products()[0]["pricing_mode"], "hourly")
        with self.assertRaisesRegex(CustomerStoreError, "positive unit price"):
            self.store.put_product(product_id="bad", title="Bad", price_minor=0,
                                   pricing_mode="usage", unit_price_minor=0)

    def test_reseller_plan_creation_and_enabled_filter(self):
        self.store.register_panel_connection(panel_id="pg", label="PG", provider="pasarguard",
                                             base_url="https://panel.example", auth_mode="api_key",
                                             secret_ref="env:PG_KEY", enabled=False,
                                             metadata={"groups": [1]})
        self.store.put_reseller_plan(plan_id="h1", panel_id="pg", title="Hourly reseller",
                                     pricing_mode="hourly", price_minor=0, unit_price_minor=500,
                                     provider_role_id="3")
        self.store.put_reseller_plan(plan_id="f1", panel_id="pg", title="Fixed reseller",
                                     pricing_mode="fixed", price_minor=10000)
        plans = self.store.list_reseller_plans(enabled_only=True)
        self.assertEqual({p["plan_id"] for p in plans}, {"h1", "f1"})
        self.assertEqual(plans[0]["allowed_group_ids"], [])
        self.store.set_reseller_plan_enabled("h1", False)
        self.assertEqual({p["plan_id"] for p in self.store.list_reseller_plans(enabled_only=True)}, {"f1"})
        conn = self.store.get_panel_connection("pg")
        self.assertEqual(conn["metadata"], {"groups": [1]})
        self.assertEqual(conn["secret_ref"], "env:PG_KEY")
        with self.assertRaisesRegex(CustomerStoreError, "environment variable"):
            self.store.register_panel_connection(panel_id="bad", label="Bad", provider="pasarguard",
                base_url="https://panel.example", auth_mode="api_key", secret_ref="raw-secret", enabled=True)
        with self.assertRaisesRegex(CustomerStoreError, "must not contain credentials"):
            self.store.register_panel_connection(panel_id="badmeta", label="Bad", provider="pasarguard",
                base_url="https://panel.example", auth_mode="api_key", secret_ref="env:PG_KEY",
                metadata={"password":"do-not-store"}, enabled=True)

    def test_hourly_customer_billing_debits_exactly_once(self):
        self.store.post_wallet_entry(telegram_id=1001, idempotency_key="fund-hourly",
                                     amount_minor=1000, entry_type="deposit")
        product = {"product_id":"h", "panel_id":"pg", "pricing_mode":"hourly",
                   "unit_price_minor":600, "currency":"IRR", "traffic_bytes":0}
        self.store.create_metered_service(service_id="hour-1", telegram_id=1001, product=product,
                                          provider_user_id="provider-1", username="alice",
                                          subscription_url=None, now=1000)
        first = self.store.bill_customer_service(service_id="hour-1", now=1120)
        second = self.store.bill_customer_service(service_id="hour-1", now=1120)
        self.assertEqual(first["charged_minor"], 20)
        self.assertEqual(second["charged_minor"], 0)
        self.assertEqual(self.store.wallet_balance(1001), 980)

    def test_usage_billing_carries_fractional_charge_and_handles_counter_reset(self):
        self.store.post_wallet_entry(telegram_id=1001, idempotency_key="fund-usage",
                                     amount_minor=100, entry_type="deposit")
        product = {"product_id":"u", "panel_id":"pg", "pricing_mode":"usage",
                   "unit_price_minor":1, "currency":"IRR", "traffic_bytes":0}
        self.store.create_metered_service(service_id="usage-1", telegram_id=1001, product=product,
                                          provider_user_id="provider-2", username="alice",
                                          subscription_url=None, now=1000)
        half = 512 * 1024**2
        first = self.store.bill_customer_service(service_id="usage-1", now=1060, used_traffic_bytes=half)
        self.assertEqual(first["charged_minor"], 0)
        second = self.store.bill_customer_service(service_id="usage-1", now=1120, used_traffic_bytes=1024**3)
        self.assertEqual(second["charged_minor"], 1)
        reset = self.store.bill_customer_service(service_id="usage-1", now=1180, used_traffic_bytes=half)
        self.assertEqual(reset["delta_bytes"], half)
        self.assertEqual(self.store.wallet_balance(1001), 99)

    def test_metered_service_requests_suspension_when_wallet_runs_out(self):
        self.store.post_wallet_entry(telegram_id=1001, idempotency_key="fund-small",
                                     amount_minor=5, entry_type="deposit")
        product = {"product_id":"h", "panel_id":"pg", "pricing_mode":"hourly",
                   "unit_price_minor":600, "currency":"IRR", "traffic_bytes":0}
        self.store.create_metered_service(service_id="hour-low", telegram_id=1001, product=product,
                                          provider_user_id="provider-3", username="alice",
                                          subscription_url=None, now=1000)
        result = self.store.bill_customer_service(service_id="hour-low", now=1600)
        self.assertEqual(result["charged_minor"], 5)
        self.assertTrue(result["suspend_required"])
        self.assertEqual(len(self.store.pending_customer_service_suspensions()), 1)
        self.store.confirm_customer_service_suspended(service_id="hour-low")
        with self.assertRaisesRegex(CustomerStoreError, "at least 96"):
            self.store.resume_customer_service_billing(service_id="hour-low", now=1700)

    def test_trust_channel_configuration_is_tenant_scoped_and_validated(self):
        self.store.set_trust_channel_config(bot_instance_id="customer", chat_id="@manubis_trust",
            enabled=True, enabled_events=["new_subscription", "reseller_created"], privacy_mode=True)
        config = self.store.get_trust_channel_config("bot-alpha", "customer")
        self.assertTrue(config.enabled)
        self.assertEqual(config.chat_id, "@manubis_trust")
        self.assertEqual({event.value for event in config.enabled_events}, {"new_subscription", "reseller_created"})
        self.assertIsNone(self.store.get_trust_channel_config("another-tenant", "customer"))
        with self.assertRaisesRegex(ValueError, "chat_id"):
            self.store.set_trust_channel_config(bot_instance_id="customer", chat_id="bad channel",
                enabled=True, enabled_events=["new_subscription"])

    def test_wallet_credit_and_trust_outbox_are_committed_together(self):
        outbox = SQLiteNotificationOutbox(self.path)
        self.store.set_trust_channel_config(bot_instance_id="customer", chat_id="@trust",
            enabled=True, enabled_events=["wallet_deposit"], privacy_mode=True)
        self.store.post_wallet_entry(telegram_id=1001, idempotency_key="verified-receipt",
            amount_minor=2000, currency="IRR", entry_type="admin_credit", reference="receipt-abc",
            note="admin private audit detail")
        pending = outbox.claim_due(limit=10)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].event_type, "wallet_deposit")
        self.assertIn("receipt-abc", pending[0].payload_json)
        self.assertNotIn("admin private audit detail", pending[0].payload_json)
        self.assertNotIn("password", pending[0].payload_json)

    def test_wallet_idempotency_rejects_different_reference(self):
        first_id = self.store.post_wallet_entry(
            telegram_id=1001, idempotency_key="same-key", amount_minor=1000,
            currency="IRR", entry_type="admin_credit", reference="receipt-A",
        )
        replay_id = self.store.post_wallet_entry(
            telegram_id=1001, idempotency_key="same-key", amount_minor=1000,
            currency="IRR", entry_type="admin_credit", reference="receipt-A",
        )
        self.assertEqual(replay_id, first_id)
        with self.assertRaisesRegex(CustomerStoreError, "different transaction data"):
            self.store.post_wallet_entry(
                telegram_id=1001, idempotency_key="same-key", amount_minor=1000,
                currency="IRR", entry_type="admin_credit", reference="receipt-B",
            )
        self.assertEqual(self.store.wallet_balance(1001), 1000)
        with self.store._connect() as db:
            rows = db.execute(
                "SELECT COUNT(*) FROM wallet_ledger WHERE tenant_id=? AND idempotency_key=?",
                (self.store.tenant_id, "same-key"),
            ).fetchone()[0]
        self.assertEqual(rows, 1)

    def test_wallet_cannot_go_negative(self):
        with self.assertRaisesRegex(CustomerStoreError, "Insufficient"):
            self.store.post_wallet_entry(
                telegram_id=1001, idempotency_key="purchase:1", amount_minor=-1,
                entry_type="purchase",
            )
        self.assertEqual(self.store.wallet_balance(1001), 0)

    def test_concurrent_debits_cannot_overdraw_wallet(self):
        self.store.post_wallet_entry(
            telegram_id=1001, idempotency_key="initial", amount_minor=1000,
            entry_type="deposit",
        )
        def debit(key):
            try:
                self.store.post_wallet_entry(
                    telegram_id=1001, idempotency_key=key, amount_minor=-700,
                    entry_type="purchase",
                )
                return "ok"
            except CustomerStoreError:
                return "rejected"
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(debit, ("purchase-a", "purchase-b")))
        self.assertCountEqual(results, ["ok", "rejected"])
        self.assertEqual(self.store.wallet_balance(1001), 300)

    def test_tenant_and_customer_balances_are_isolated(self):
        other = CustomerDataStore(self.path, tenant_id="bot-beta")
        other.ensure_customer(1001)
        self.store.post_wallet_entry(
            telegram_id=1001, idempotency_key="alpha:deposit", amount_minor=900,
            entry_type="deposit",
        )
        self.assertEqual(other.wallet_balance(1001), 0)
        self.assertEqual(self.store.wallet_balance(1001), 900)

    def test_product_cannot_be_published_without_panel_binding(self):
        self.store.put_product(product_id="draft", title="Draft", price_minor=1000,
            currency="IRR", duration_hours=24, traffic_bytes=0, enabled=False)
        with self.assertRaisesRegex(CustomerStoreError, "enabled panel"):
            self.store.set_product_enabled("draft", True)
        self.store.register_panel_connection(panel_id="pg", label="PG", provider="pasarguard",
            base_url="https://pg.example", auth_mode="api_key", secret_ref="env:PG_KEY", enabled=True)
        self.store.bind_product_panel(product_id="draft", panel_id="pg", group_ids=["1"])
        self.store.set_product_enabled("draft", True)
        self.assertEqual(self.store.list_products()[0]["product_id"], "draft")

    def test_categories_scope_products_and_hide_disabled_categories(self):
        self.store.put_category(category_id="ai", title="AI services", description="AI-ready plans")
        self.store.put_category(category_id="games", title="Gaming")
        self.store.put_product(product_id="ai-plan", title="AI Plan", price_minor=2500, category_id="ai")
        self.store.put_product(product_id="game-plan", title="Game Plan", price_minor=2500, category_id="games")
        self.assertEqual([p["product_id"] for p in self.store.list_products(category_id="ai")], ["ai-plan"])
        self.assertEqual({c["category_id"] for c in self.store.list_categories()}, {"ai", "games"})
        self.store.set_category_enabled("ai", False)
        self.assertEqual([p["product_id"] for p in self.store.list_products()], ["game-plan"])
        self.assertIsNone(self.store.get_product("ai-plan", enabled_only=True))
        self.assertIsNotNone(self.store.get_product("ai-plan", enabled_only=False))

    def test_existing_database_migrates_product_categories_safely(self):
        legacy_path = str(Path(self.tmp.name) / "legacy.sqlite3")
        with sqlite3.connect(legacy_path) as db:
            db.execute("""CREATE TABLE customer_products (
                tenant_id TEXT NOT NULL, product_id TEXT NOT NULL, title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '', price_minor INTEGER NOT NULL,
                currency TEXT NOT NULL, duration_hours INTEGER NOT NULL DEFAULT 0,
                traffic_bytes INTEGER NOT NULL DEFAULT 0, enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(tenant_id,product_id))""")
        migrated = CustomerDataStore(legacy_path, tenant_id="legacy")
        migrated.put_category(category_id="legacy-cat", title="Legacy category")
        migrated.put_product(product_id="legacy-item", title="Legacy item", price_minor=100,
                              category_id="legacy-cat", enabled=True)
        self.assertEqual(migrated.list_products(category_id="legacy-cat")[0]["product_id"], "legacy-item")

    def test_manual_orders_can_be_fulfilled_once_with_staff_audit(self):
        self.store.post_wallet_entry(telegram_id=1001, idempotency_key="manual-fund",
                                     amount_minor=3000, entry_type="deposit")
        self.store.put_manual_product(product_id="manual-ai", title="AI subscription",
                                      price_minor=1200, currency="IRR", delivery_mode="manual")
        order = self.store.buy_manual_product(telegram_id=1001, product_id="manual-ai",
                                              idempotency_key="manual-order-1")
        self.assertEqual(order["status"], "pending_delivery")
        listed = self.store.list_manual_orders()
        self.assertEqual([item["order_id"] for item in listed], [order["order_id"]])
        self.assertNotIn("delivery_payload", listed[0])
        fulfilled = self.store.fulfill_manual_order(order_id=order["order_id"],
                                                    delivery_payload="delivery-reference-123",
                                                    fulfilled_by=6883349141)
        self.assertEqual(fulfilled["status"], "fulfilled")
        self.assertEqual(fulfilled["delivery_payload"], "delivery-reference-123")
        self.assertEqual(self.store.list_manual_orders(), [])
        self.assertEqual(self.store.list_manual_orders(status="fulfilled")[0]["order_id"], order["order_id"])
        with self.assertRaisesRegex(CustomerStoreError, "not pending"):
            self.store.fulfill_manual_order(order_id=order["order_id"],
                                            delivery_payload="duplicate-delivery",
                                            fulfilled_by=6883349141)
        with self.store._connect() as db:
            event = db.execute("SELECT event_type,actor_telegram_id FROM manual_product_order_events WHERE tenant_id=? AND order_id=?",
                               (self.store.tenant_id, order["order_id"])).fetchone()
        self.assertEqual(tuple(event), ("fulfilled", 6883349141))
        self.assertEqual(self.store.wallet_balance(1001), 1800)

    def test_product_rejects_unknown_category(self):
        with self.assertRaisesRegex(CustomerStoreError, "category does not exist"):
            self.store.put_product(product_id="orphan", title="Orphan", price_minor=2500, category_id="missing")

    def test_catalog_filters_disabled_products(self):
        self.store.put_product(
            product_id="basic", title="Basic", price_minor=2500, duration_hours=24,
        )
        self.store.put_product(
            product_id="hidden", title="Hidden", price_minor=0, enabled=False,
        )
        products = self.store.list_products()
        self.assertEqual([item["product_id"] for item in products], ["basic"])
        self.assertEqual(products[0]["duration_hours"], 24)

    def test_services_are_scoped_to_their_owner(self):
        self.store.ensure_customer(1002)
        self.store.upsert_service(
            service_id="svc-1", telegram_id=1001, product_id="basic", panel_id="panel-a",
            status="active", expires_at="2026-11-01T00:00:00Z",
            traffic_limit_bytes=10 * 1024**3, traffic_used_bytes=2 * 1024**3,
        )
        self.assertEqual([x["service_id"] for x in self.store.list_services(1001)], ["svc-1"])
        self.assertEqual(self.store.list_services(1002), [])

    def test_ticket_stores_first_message_and_scopes_by_owner(self):
        self.store.ensure_customer(1002)
        self.store.create_ticket(
            ticket_id="ticket-1", telegram_id=1001, subject="Connection",
            first_message="It does not connect",
        )
        self.assertEqual([t["ticket_id"] for t in self.store.list_tickets(1001)], ["ticket-1"])
        self.assertEqual(self.store.list_tickets(1002), [])
        self.assertEqual(self.store.ticket_owner("ticket-1"), 1001)
        self.store.add_ticket_message(ticket_id="ticket-1", sender_type="staff", body="We are checking")
        messages = self.store.ticket_messages("ticket-1", telegram_id=1001)
        self.assertEqual([m["sender_type"] for m in messages], ["customer", "staff"])
        self.assertEqual(self.store.ticket_messages("ticket-1", telegram_id=1002), [])


if __name__ == "__main__":
    unittest.main()
