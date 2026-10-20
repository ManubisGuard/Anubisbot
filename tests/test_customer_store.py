import sqlite3
import tempfile
import time
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

    def test_notification_routes_support_group_topics_and_tenant_isolation(self):
        route = self.store.add_notification_route(event_key="support_ticket", chat_id=-1001234567890,
            topic_id=77, created_by=1001)
        self.assertEqual(route["topic_id"], 77)
        self.assertEqual(self.store.list_notification_routes("support_ticket"), [route])
        other = CustomerDataStore(self.path, tenant_id="bot-beta")
        self.assertEqual(other.list_notification_routes(), [])
        self.assertFalse(other.remove_notification_route(event_key="support_ticket", chat_id=-1001234567890, topic_id=77))
        self.assertTrue(self.store.remove_notification_route(event_key="support_ticket", chat_id=-1001234567890, topic_id=77))
        self.assertEqual(self.store.list_notification_routes(), [])
        with self.assertRaisesRegex(CustomerStoreError, "Unsupported notification event"):
            self.store.add_notification_route(event_key="unknown_event", chat_id=123, created_by=1001)
        with self.assertRaisesRegex(CustomerStoreError, "Topic ID"):
            self.store.add_notification_route(event_key="backup", chat_id=-1001234567890, topic_id=-1, created_by=1001)

    def test_admin_notification_outbox_is_redacted_idempotent_and_tracks_each_destination(self):
        self.store.add_notification_route(event_key="purchase", chat_id=1001, created_by=1001)
        self.store.add_notification_route(event_key="purchase", chat_id=-1001234567890,
                                         topic_id=77, created_by=1001)
        payload = {"public_order_id": "order-public-1", "amount_minor": 2500, "currency": "irr",
                   "product_title": "AI Plan", "status": "paid", "customer_id": 1001,
                   "password": "DO-NOT-STORE", "subscription_url": "https://secret.invalid/x",
                   "initData": "private"}
        notification_id = self.store.enqueue_admin_notification(
            event_key="purchase", idempotency_key="order-public-1", payload=payload, now=1000)
        self.assertEqual(self.store.enqueue_admin_notification(
            event_key="purchase", idempotency_key="order-public-1",
            payload=payload, now=1001), notification_id)
        with self.store._connect() as db:
            raw = db.execute("SELECT payload_json FROM customer_admin_notifications WHERE notification_id=?",
                             (notification_id,)).fetchone()[0]
            self.assertNotIn("DO-NOT-STORE", raw)
            self.assertNotIn("subscription_url", raw)
            self.assertNotIn("initData", raw)
        deliveries = self.store.claim_admin_notification_deliveries(now=1002)
        self.assertEqual(len(deliveries), 2)
        self.assertTrue(all(item["notification_id"] == notification_id for item in deliveries))
        self.assertEqual({(item["chat_id"], item["topic_id"]) for item in deliveries},
                         {(1001, 0), (-1001234567890, 77)})
        self.store.mark_admin_notification_delivery_sent(deliveries[0]["delivery_id"], now=1003)
        self.assertEqual(self.store.retry_admin_notification_delivery(
            deliveries[1]["delivery_id"], attempt=1, error_code="sendfailed",
            now=1004, max_attempts=1), "dead")
        row = self.store.list_admin_notifications(status="dead")[0]
        self.assertEqual(row["sent_destinations"], 1)
        self.assertEqual(row["dead_destinations"], 1)
        self.assertTrue(self.store.retry_admin_notification_now(notification_id, now=1005))
        retried = self.store.claim_admin_notification_deliveries(now=1005)
        self.assertEqual(len(retried), 1)
        self.assertEqual(retried[0]["chat_id"], deliveries[1]["chat_id"])
        self.assertEqual(self.store.mark_admin_notification_delivery_sent(
            retried[0]["delivery_id"], now=1006), "sent")
        self.assertEqual(self.store.list_admin_notifications(status="sent")[0]["status"], "sent")

    def test_admin_notification_outbox_uses_fallback_only_when_no_route_and_tenant_scoped(self):
        notification_id = self.store.enqueue_admin_notification(
            event_key="security_alert", idempotency_key="alert-1",
            payload={"status": "blocked", "reason_code": "auth_mismatch"}, now=2000)
        claimed = self.store.claim_admin_notification_deliveries(
            now=2001, fallback_admin_ids=(1001, 1002))
        self.assertEqual(len(claimed), 2)
        self.assertEqual({row["chat_id"] for row in claimed}, {1001, 1002})
        other = CustomerDataStore(self.path, tenant_id="bot-beta")
        self.assertEqual(other.list_admin_notifications(), [])
        self.assertTrue(all(row["notification_id"] == notification_id for row in claimed))

    def test_admin_notification_without_any_destination_dead_letters_and_can_requeue_after_route_added(self):
        notification_id = self.store.enqueue_admin_notification(
            event_key="backup", idempotency_key="backup-1",
            payload={"status": "failed", "reason_code": "backup_failed"}, now=3000)
        self.assertEqual(self.store.claim_admin_notification_deliveries(now=3001), [])
        self.assertEqual(self.store.list_admin_notifications(status="dead")[0]["last_error_code"], "no_destination")
        self.store.add_notification_route(event_key="backup", chat_id=1001, created_by=1001)
        self.assertTrue(self.store.retry_admin_notification_now(notification_id, now=3002))
        claimed = self.store.claim_admin_notification_deliveries(now=3002)
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0]["chat_id"], 1001)

    def test_database_uses_wal_full_sync_and_foreign_keys(self):
        with self.store._connect() as db:
            self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
            self.assertEqual(db.execute("PRAGMA synchronous").fetchone()[0], 2)
            self.assertEqual(db.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(db.execute("PRAGMA busy_timeout").fetchone()[0], 15000)

    def test_backup_policy_defaults_disabled_and_is_tenant_scoped(self):
        policy = self.store.get_backup_policy()
        self.assertEqual(policy["enabled"], 0)
        self.assertEqual(policy["last_status"], "never")
        self.assertEqual(policy["destination"], "local")
        other = CustomerDataStore(self.path, tenant_id="bot-beta")
        other_policy = other.get_backup_policy()
        self.assertEqual(other_policy["enabled"], 0)
        self.assertNotEqual(other_policy["tenant_id"], policy["tenant_id"])

    def test_backup_policy_persists_and_remains_disabled_after_reopen(self):
        policy = self.store.update_backup_policy(frequency="daily", timezone_name="Europe/Berlin",
            local_time="02:45", interval_minutes=1440, destination="both",
            local_directory="/var/lib/anubis-bot/backups", telegram_chat_id=-1001234567890,
            telegram_topic_id=77, retention_count=20, retention_days=60, updated_by=1001)
        self.assertEqual(policy["frequency"], "daily")
        self.assertEqual(policy["timezone_name"], "Europe/Berlin")
        self.assertEqual(policy["telegram_topic_id"], 77)
        self.assertEqual(policy["retention_count"], 20)
        self.assertEqual(policy["enabled"], 0)
        reopened = CustomerDataStore(self.path, tenant_id="bot-alpha")
        self.assertEqual(reopened.get_backup_policy()["destination"], "both")
        self.assertEqual(reopened.get_backup_policy()["enabled"], 0)
        with reopened._connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM schema_migrations WHERE version=8").fetchone()[0], 1)
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_backup_policy_rejects_invalid_destination_and_does_not_overwrite_previous(self):
        original = self.store.get_backup_policy()
        with self.assertRaisesRegex(CustomerStoreError, "requires a non-zero numeric chat ID"):
            self.store.update_backup_policy(frequency="daily", timezone_name="UTC", local_time="03:00",
                interval_minutes=1440, destination="telegram", local_directory="/var/lib/anubis-bot/backups",
                telegram_chat_id=None, telegram_topic_id=0, retention_count=14, retention_days=30, updated_by=1001)
        self.assertEqual(self.store.get_backup_policy()["frequency"], original["frequency"])
        with self.assertRaisesRegex(CustomerStoreError, "absolute path"):
            self.store.update_backup_policy(frequency="daily", timezone_name="UTC", local_time="03:00",
                interval_minutes=1440, destination="local", local_directory="relative/backups",
                telegram_chat_id=None, telegram_topic_id=0, retention_count=14, retention_days=30, updated_by=1001)

    def test_service_operation_audit_triggers_are_reinstalled_on_existing_v2_database(self):
        with self.store._connect() as db:
            db.execute("DROP TRIGGER customer_service_operation_events_no_update")
            db.execute("DROP TRIGGER customer_service_operation_events_no_delete")
        reopened = CustomerDataStore(self.path, tenant_id="bot-alpha")
        with reopened._connect() as db:
            triggers = {row["name"] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            )}
            self.assertIn("customer_service_operation_events_no_update", triggers)
            self.assertIn("customer_service_operation_events_no_delete", triggers)

    def test_service_operation_read_paths_migrate_legacy_table_additively(self):
        with sqlite3.connect(self.path) as db:
            db.execute("DROP TABLE customer_service_operation_events")
            db.execute("DROP TABLE customer_service_operations")
            db.execute("DELETE FROM schema_migrations WHERE version=2")
            db.execute("""CREATE TABLE customer_service_operations (
                tenant_id TEXT NOT NULL, operation_id TEXT NOT NULL, telegram_id INTEGER NOT NULL,
                service_id TEXT NOT NULL, change_type TEXT NOT NULL, idempotency_key TEXT NOT NULL,
                amount_minor INTEGER NOT NULL, currency TEXT NOT NULL, quantity INTEGER NOT NULL,
                status TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(tenant_id,operation_id), UNIQUE(tenant_id,idempotency_key)
            )""")
        # Reopen so versioned migrations, not read paths, repair legacy schema.
        self.store = CustomerDataStore(self.path, tenant_id="bot-alpha")
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
        with self.assertRaisesRegex(CustomerStoreError, "already has an unresolved change"):
            self.store.reserve_service_change(operation_id="op-change-duplicate", telegram_id=1001,
                service_id="change-service", change_type="traffic_topup",
                idempotency_key="change-key-new", now=1002)
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
        balance_before_blocked_retry = self.store.wallet_balance(1001)
        with self.assertRaisesRegex(CustomerStoreError, "already has an unresolved change"):
            self.store.reserve_service_change(operation_id="op-reconcile-duplicate", telegram_id=1001,
                service_id="reconcile-service", change_type="traffic_topup",
                idempotency_key="reconcile-new-key", now=1001)
        self.assertEqual(self.store.wallet_balance(1001), balance_before_blocked_retry)
        listed = self.store.list_service_change_operations(status="reconcile_required")
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["operation_id"], "op-reconcile")
        self.assertEqual(listed[0]["status"], "reconcile_required:timeouterror")
        self.assertEqual(self.store.list_service_change_operations(status="all", telegram_id=1001)[0]["operation_id"], "op-reconcile")
        self.assertEqual(self.store.list_service_change_operations(status="all", telegram_id=2002), [])
        self.assertEqual(self.store.list_service_change_operations(status="pending"), [])
        self.assertEqual(self.store.get_service_change_operation("op-reconcile")["username"], "reconcile-user")
        self.assertIsNone(CustomerDataStore(self.path, tenant_id="bot-beta").get_service_change_operation("op-reconcile"))

    def _make_reconciliation_operation(self, operation_id="op-finalize", tenant_id="bot-alpha"):
        self.store.register_panel_connection(panel_id="panel-finalize", label="Panel", provider="pasarguard",
            base_url="https://panel.example", auth_mode="api_key", secret_ref="env:PANEL_FINALIZE", enabled=True)
        self.store.put_product(product_id="finalize-product", title="Finalize product", price_minor=500,
            currency="IRR", duration_hours=12, traffic_bytes=1024, panel_id="panel-finalize", group_ids=["1"])
        self.store.post_wallet_entry(telegram_id=1001, idempotency_key="finalize-funds", amount_minor=2000,
            entry_type="deposit")
        self.store.create_metered_service(service_id="finalize-service", telegram_id=1001,
            product={"product_id":"finalize-product","panel_id":"panel-finalize","pricing_mode":"fixed",
                     "currency":"IRR","unit_price_minor":0,"duration_hours":12,"traffic_bytes":1024,
                     "expires_at":"2030-01-01T00:00:00+00:00"},
            provider_user_id="provider-finalize", username="finalize-user", subscription_url=None, now=1000)
        op = self.store.reserve_service_change(operation_id=operation_id, telegram_id=1001,
            service_id="finalize-service", change_type="renewal", idempotency_key=f"{operation_id}-key", now=1000)
        self.store.persist_service_change_target(operation_id=operation_id,
            before_expires_at="2030-01-01T00:00:00+00:00", before_traffic_limit_bytes=1024,
            target_expires_at="2030-01-01T12:00:00+00:00", target_traffic_limit_bytes=None)
        self.store.flag_service_change_reconciliation(operation_id=operation_id, reason_code="timeout")
        return op

    def test_service_change_finalize_requires_verified_target_and_is_append_only_idempotent(self):
        self._make_reconciliation_operation()
        with self.assertRaisesRegex(CustomerStoreError, "target_present"):
            self.store.resolve_service_change_target_present(operation_id="op-finalize",
                actor_telegram_id=9001, comparison="before_present",
                observed_value="2030-01-01T00:00:00+00:00", identity_verified=True, reason="checked")
        self.assertTrue(self.store.get_service_change_operation("op-finalize")["status"].startswith("reconcile_required:"))
        event = self.store.resolve_service_change_target_present(operation_id="op-finalize",
            actor_telegram_id=9001, comparison="target_present",
            observed_value="2030-01-01T12:00:00+00:00", identity_verified=True, reason="fresh target read")
        self.assertFalse(event["idempotent_replay"])
        self.assertEqual(self.store.get_service_change_operation("op-finalize")["status"], "completed")
        self.assertEqual(self.store.get_customer_service("finalize-service")["expires_at"], "2030-01-01T12:00:00+00:00")
        self.assertEqual(self.store.wallet_balance(1001), 1500)
        replay = self.store.resolve_service_change_target_present(operation_id="op-finalize",
            actor_telegram_id=9001, comparison="target_present",
            observed_value="2030-01-01T12:00:00+00:00", identity_verified=True, reason="fresh target read")
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(len(self.store.list_service_change_events("op-finalize")), 1)
        # The audit record must survive reopening the store, as after a process restart.
        reopened = CustomerDataStore(self.path, tenant_id="bot-alpha")
        self.assertEqual(reopened.list_service_change_events("op-finalize")[0]["reason"], "fresh target read")
        with self.store._connect() as db:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                db.execute("UPDATE customer_service_operation_events SET reason='tampered' WHERE operation_id='op-finalize'")
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                db.execute("DELETE FROM customer_service_operation_events WHERE operation_id='op-finalize'")
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
        # Explicitly enable SQLite FK enforcement in this independent connection and ensure
        # the operation row cannot be removed while its audit evidence references it.
        with sqlite3.connect(self.path) as db:
            db.execute("PRAGMA foreign_keys=ON")
            with self.assertRaisesRegex(sqlite3.IntegrityError, "FOREIGN KEY constraint failed"):
                db.execute("DELETE FROM customer_service_operations WHERE tenant_id='bot-alpha' AND operation_id='op-finalize'")
        self.assertEqual(CustomerDataStore(self.path, tenant_id="bot-beta").list_service_change_events("op-finalize"), [])

    def test_service_operation_audit_survives_sqlite_backup_restore_round_trip(self):
        self._make_reconciliation_operation()
        self.store.resolve_service_change_target_present(operation_id="op-finalize", actor_telegram_id=9001,
            comparison="target_present", observed_value="2030-01-01T12:00:00+00:00",
            identity_verified=True, reason="verified after provider timeout")
        backup_path = str(Path(self.tmp.name) / "customer-restored.sqlite3")
        # Use SQLite's online backup API, not a raw file copy, so the test also exercises
        # a consistent snapshot mechanism while the source database is open.
        with sqlite3.connect(self.path) as source, sqlite3.connect(backup_path) as destination:
            source.backup(destination)
        restored = CustomerDataStore(backup_path, tenant_id="bot-alpha")
        events = restored.list_service_change_events("op-finalize")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["reason"], "verified after provider timeout")
        self.assertEqual(restored.get_service_change_operation("op-finalize")["status"], "completed")
        with restored._connect() as db:
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                db.execute("UPDATE customer_service_operation_events SET reason='tampered' WHERE operation_id='op-finalize'")
        self.assertEqual(restored.wallet_balance(1001), 1500)

    def test_service_change_finalize_rejects_wrong_tenant_missing_identity_and_conflicting_replay(self):
        self._make_reconciliation_operation()
        other = CustomerDataStore(self.path, tenant_id="bot-beta")
        with self.assertRaisesRegex(CustomerStoreError, "not found"):
            other.resolve_service_change_target_present(operation_id="op-finalize", actor_telegram_id=9001,
                comparison="target_present", observed_value="2030-01-01T12:00:00+00:00",
                identity_verified=True, reason="fresh target read")
        with self.assertRaisesRegex(CustomerStoreError, "identity-verified"):
            self.store.resolve_service_change_target_present(operation_id="op-finalize", actor_telegram_id=9001,
                comparison="target_present", observed_value="2030-01-01T12:00:00+00:00",
                identity_verified=False, reason="fresh target read")
        self.store.resolve_service_change_target_present(operation_id="op-finalize", actor_telegram_id=9001,
            comparison="target_present", observed_value="2030-01-01T12:00:00+00:00",
            identity_verified=True, reason="fresh target read")
        with self.assertRaisesRegex(CustomerStoreError, "already recorded"):
            self.store.resolve_service_change_target_present(operation_id="op-finalize", actor_telegram_id=9002,
                comparison="target_present", observed_value="2030-01-01T12:00:00+00:00",
                identity_verified=True, reason="different operator decision")

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
        self.store.transition_order(order_id="order-2", new_status="active", provider_reference="provider-svc")
        with self.assertRaisesRegex(CustomerStoreError, "conflicts with provider reference"):
            self.store.transition_order(order_id="order-2", new_status="active",
                                        provider_reference="different-provider-svc")

    def test_finalize_product_order_replay_rejects_mismatched_provider_identity(self):
        self.store.put_product(product_id="finalize-basic", title="Finalize Basic", price_minor=1000,
                               currency="IRR", duration_hours=24, traffic_bytes=1024)
        order = self.store.create_order(order_id="finalize-order", telegram_id=1001,
                                        product_id="finalize-basic", idempotency_key="finalize-buy")
        self.store.transition_order(order_id=order["order_id"], new_status="paid")
        self.store.transition_order(order_id=order["order_id"], new_status="provisioning")
        self.store.create_metered_service(service_id=order["order_id"], telegram_id=1001,
            product={"product_id":"finalize-basic", "panel_id":"panel-finalize", "pricing_mode":"fixed",
                     "currency":"IRR", "unit_price_minor":0, "duration_hours":24, "traffic_bytes":1024},
            provider_user_id="provider-user-1", username="alice-finalize", subscription_url=None, now=1000)
        self.store.finalize_product_order(order_id=order["order_id"], service_id=order["order_id"],
                                          provider_reference="provider-user-1")
        # Exact replay is harmless, but a changed provider identity or service ID must fail closed.
        self.store.finalize_product_order(order_id=order["order_id"], service_id=order["order_id"],
                                          provider_reference="provider-user-1")
        with self.assertRaisesRegex(CustomerStoreError, "conflicts with service or provider reference"):
            self.store.finalize_product_order(order_id=order["order_id"], service_id=order["order_id"],
                                              provider_reference="provider-user-2")
        with self.assertRaisesRegex(CustomerStoreError, "conflicts with service or provider reference"):
            self.store.finalize_product_order(order_id=order["order_id"], service_id="other-service",
                                              provider_reference="provider-user-1")
        current = self.store.get_order_by_idempotency("finalize-buy", 1001)
        self.assertEqual(current["status"], "active")
        self.assertEqual(current["provider_reference"], "provider-user-1")

    def test_metered_service_replay_rejects_changed_provider_identity(self):
        product = {"product_id":"identity-basic", "panel_id":"identity-panel", "pricing_mode":"fixed",
                   "currency":"IRR", "unit_price_minor":0, "duration_hours":24, "traffic_bytes":1024}
        self.store.create_metered_service(service_id="identity-service", telegram_id=1001, product=product,
            provider_user_id="provider-user-original", username="alice-identity",
            subscription_url="https://sub.example/original", now=1000)
        # A byte-for-byte identity replay is accepted.
        self.store.create_metered_service(service_id="identity-service", telegram_id=1001, product=product,
            provider_user_id="provider-user-original", username="alice-identity",
            subscription_url="https://sub.example/original", now=1001)
        with self.assertRaisesRegex(CustomerStoreError, "different customer, provider identity"):
            self.store.create_metered_service(service_id="identity-service", telegram_id=1001, product=product,
                provider_user_id="provider-user-swapped", username="alice-identity",
                subscription_url="https://sub.example/original", now=1002)
        stored = self.store.get_customer_service("identity-service", 1001)
        self.assertEqual(stored["provider_user_id"], "provider-user-original")
        self.assertEqual(stored["subscription_url"], "https://sub.example/original")

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

    def test_concurrent_verified_payment_replays_credit_wallet_once(self):
        self.store.create_payment_attempt(
            payment_id="pay-concurrent-settle", telegram_id=1001, provider="mock",
            amount_minor=3200, currency="IRR", idempotency_key="intent-concurrent-settle",
        )
        def settle(_):
            return self.store.settle_verified_wallet_deposit(
                payment_id="pay-concurrent-settle", verified_amount_minor=3200,
                verified_currency="IRR", external_reference="provider-concurrent-ref",
            )
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(settle, (1, 2)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(self.store.wallet_balance(1001), 3200)
        self.assertEqual([e["event_type"] for e in self.store.list_payment_attempt_events("pay-concurrent-settle")],
                         ["created", "verified"])
        with self.store._connect() as db:
            rows = db.execute("SELECT COUNT(*) FROM wallet_ledger WHERE tenant_id=? AND idempotency_key=?",
                              (self.store.tenant_id, "payment:pay-concurrent-settle")).fetchone()[0]
        self.assertEqual(rows, 1)

    def test_manual_deposit_receipt_is_tenant_scoped_and_unique(self):
        self.store.create_payment_attempt(payment_id="manual-receipt-1", telegram_id=1001,
            provider="manual", amount_minor=4000, currency="IRR", idempotency_key="manual-receipt-intent-1")
        self.store.submit_manual_deposit_receipt(payment_id="manual-receipt-1", telegram_id=1001,
            transfer_reference="tracking-0001", payer_name="Customer One")
        self.store.submit_manual_deposit_receipt(payment_id="manual-receipt-1", telegram_id=1001,
            transfer_reference="tracking-0001", payer_name="Customer One")
        receipt = self.store.get_manual_deposit_receipt("manual-receipt-1")
        self.assertEqual(receipt["transfer_reference"], "tracking-0001")
        self.assertEqual(len(self.store.list_manual_deposit_receipts()), 1)
        other = CustomerDataStore(str(Path(self.tmp.name) / "other-tenant.sqlite3"), tenant_id="other")
        self.assertIsNone(other.get_manual_deposit_receipt("manual-receipt-1"))
        with self.assertRaises(CustomerStoreError):
            self.store.submit_manual_deposit_receipt(payment_id="manual-receipt-1", telegram_id=1001,
                transfer_reference="tracking-0002", payer_name="Customer One")

    def test_manual_deposit_rejection_is_atomic_and_does_not_credit(self):
        self.store.create_payment_attempt(payment_id="manual-reject-1", telegram_id=1001,
            provider="manual", amount_minor=4000, currency="IRR", idempotency_key="manual-reject-intent-1")
        self.store.submit_manual_deposit_receipt(payment_id="manual-reject-1", telegram_id=1001,
            transfer_reference="tracking-reject-1")
        self.store.reject_manual_deposit_receipt(payment_id="manual-reject-1", admin_id=9001, reason="receipt_invalid")
        self.assertEqual(self.store.get_payment_attempt("manual-reject-1")["status"], "failed")
        self.assertEqual(self.store.wallet_balance(1001), 0)
        self.assertEqual(self.store.get_manual_deposit_receipt("manual-reject-1")["status"], "rejected")
        self.assertEqual([e["event_type"] for e in self.store.list_payment_attempt_events("manual-reject-1")],
                         ["created", "failed"])

    def test_manual_deposit_card_details_validate_and_persist(self):
        self.store.set_manual_deposit_details(card_number="1234-5678-1234-5678",
            account_holder="Test Holder", bank_name="Test Bank")
        self.assertEqual(self.store.get_manual_deposit_details()["card_number"], "1234567812345678")
        with self.assertRaises(CustomerStoreError):
            self.store.set_manual_deposit_details(card_number="123", account_holder="Test Holder")

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
        events = self.store.list_payment_attempt_events("pay-failed-once")
        self.assertEqual([e["event_type"] for e in events], ["created", "failed"])
        self.assertEqual(events[-1]["failure_code"], "receipt_not_confirmed")
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

    def test_payment_attempt_audit_is_atomic_immutable_and_tenant_scoped(self):
        self.store.create_payment_attempt(
            payment_id="pay-audit", telegram_id=1001, provider="manual",
            amount_minor=1200, currency="IRR", idempotency_key="intent-audit",
        )
        self.assertEqual([e["event_type"] for e in self.store.list_payment_attempt_events("pay-audit")], ["created"])
        self.store.settle_verified_wallet_deposit(
            payment_id="pay-audit", verified_amount_minor=1200, verified_currency="IRR",
            external_reference="bank-reference-audit", verified_by=9001,
        )
        # Exact replay returns the same settlement without adding a second audit event or credit.
        self.store.settle_verified_wallet_deposit(
            payment_id="pay-audit", verified_amount_minor=1200, verified_currency="IRR",
            external_reference="bank-reference-audit", verified_by=9001,
        )
        events = self.store.list_payment_attempt_events("pay-audit")
        self.assertEqual([e["event_type"] for e in events], ["created", "verified"])
        self.assertEqual(events[-1]["actor_telegram_id"], 9001)
        self.assertEqual(events[-1]["external_reference"], "bank-reference-audit")
        self.assertEqual(self.store.wallet_balance(1001), 1200)
        self.assertEqual(CustomerDataStore(self.path, tenant_id="bot-beta").list_payment_attempt_events("pay-audit"), [])
        with self.store._connect() as db:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                db.execute("UPDATE payment_attempt_events SET actor_telegram_id=1 WHERE tenant_id=? AND payment_id=?", (self.store.tenant_id, "pay-audit"))
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                db.execute("DELETE FROM payment_attempt_events WHERE tenant_id=? AND payment_id=?", (self.store.tenant_id, "pay-audit"))

    def test_product_supports_metered_modes_and_rejects_missing_rate(self):
        self.store.put_product(product_id="hourly", title="Hourly", price_minor=0,
                               pricing_mode="hourly", unit_price_minor=600)
        self.assertEqual(self.store.list_products()[0]["pricing_mode"], "hourly")
        with self.assertRaisesRegex(CustomerStoreError, "positive unit price"):
            self.store.put_product(product_id="bad", title="Bad", price_minor=0,
                                   pricing_mode="usage", unit_price_minor=0)

    def test_sanaei_subscription_server_metadata_is_validated_and_persisted(self):
        valid_metadata={"subscription_base_url":"https://sub.example:2096","subscription_path":"sub"}
        self.store.register_panel_connection(panel_id="sanaei-good",label="Sanaei",provider="sanaei",
            base_url="https://panel.example",auth_mode="access_token",secret_ref="env:SANAEI_TOKEN",
            metadata=valid_metadata,enabled=True)
        self.assertEqual(self.store.get_panel_connection("sanaei-good")["metadata"],valid_metadata)
        invalid_metadata=[
            {"subscription_base_url":"https://sub.example:2096"},
            {"subscription_path":"sub"},
            {"subscription_base_url":"http://sub.example:2096","subscription_path":"sub"},
            {"subscription_base_url":"https://user:pass@sub.example:2096","subscription_path":"sub"},
            {"subscription_base_url":"https://sub.example:2096/path","subscription_path":"sub"},
            {"subscription_base_url":"https://sub.example:2096","subscription_path":"../bad"},
        ]
        for index,metadata in enumerate(invalid_metadata):
            with self.subTest(metadata=metadata):
                with self.assertRaisesRegex(CustomerStoreError,"Sanaei subscription"):
                    self.store.register_panel_connection(panel_id=f"sanaei-bad-{index}",label="Sanaei",provider="sanaei",
                        base_url="https://panel.example",auth_mode="access_token",secret_ref="env:SANAEI_TOKEN",
                        metadata=metadata,enabled=True)
                self.assertIsNone(self.store.get_panel_connection(f"sanaei-bad-{index}"))

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

    def test_manual_products_share_category_filtering_and_hide_disabled_categories(self):
        self.store.put_category(category_id="ai", title="AI services")
        self.store.put_manual_product(product_id="ai-manual", title="AI gift", price_minor=500,
            delivery_mode="instant", delivery_payload="PRIVATE-DELIVERY", category_id="ai")
        self.store.put_manual_product(product_id="uncategorized", title="General item", price_minor=300,
            delivery_mode="instant", delivery_payload="GENERAL-DELIVERY")
        self.assertEqual([item["product_id"] for item in self.store.list_manual_products(category_id="ai")], ["ai-manual"])
        self.assertEqual({item["product_id"] for item in self.store.list_manual_products()}, {"ai-manual", "uncategorized"})
        self.store.set_category_enabled("ai", False)
        self.assertEqual([item["product_id"] for item in self.store.list_manual_products()], ["uncategorized"])
        self.assertIsNone(self.store.get_manual_product("ai-manual"))
        with self.assertRaisesRegex(CustomerStoreError, "unavailable"):
            self.store.buy_manual_product(telegram_id=1001, product_id="ai-manual", idempotency_key="disabled-category-purchase")
        self.assertEqual([item["product_id"] for item in self.store.list_manual_products(enabled_only=False, category_id="ai")], ["ai-manual"])

    def test_manual_product_category_must_exist_in_same_tenant(self):
        other = CustomerDataStore(self.path, tenant_id="bot-beta")
        self.store.put_category(category_id="ai", title="AI services")
        with self.assertRaisesRegex(CustomerStoreError, "category does not exist"):
            other.put_manual_product(product_id="cross-tenant", title="Cross tenant", price_minor=100,
                delivery_mode="manual", category_id="ai")
        self.assertEqual(other.list_manual_products(category_id="ai"), [])
        with self.assertRaisesRegex(CustomerStoreError, "category does not exist"):
            self.store.put_manual_product(product_id="orphan-manual", title="Orphan", price_minor=100,
                delivery_mode="manual", category_id="missing")

    def test_core_schema_migration_is_versioned_and_reopening_is_idempotent(self):
        with self.store._connect() as db:
            versions = db.execute(
                "SELECT version, name FROM schema_migrations ORDER BY version"
            ).fetchall()
            self.assertEqual([(row["version"], row["name"]) for row in versions],
                             [(1, "core_additive_legacy_migrations"), (2, "service_change_operations_and_audit"), (3, "manual_shop_tables"), (4, "manual_order_purchase_snapshot"), (5, "manual_delivery_notification_outbox"), (6, "customer_ticket_notification_outbox"), (7, "per_event_telegram_notification_routes"), (8, "tenant_backup_policy_disabled_by_default"), (9, "durable_per_destination_admin_notification_outbox"), (10, "manual_product_category_binding")])

        reopened = CustomerDataStore(self.path, tenant_id="bot-alpha")
        with reopened._connect() as db:
            versions = db.execute(
                "SELECT version, name FROM schema_migrations ORDER BY version"
            ).fetchall()
            self.assertEqual([(row["version"], row["name"]) for row in versions],
                             [(1, "core_additive_legacy_migrations"), (2, "service_change_operations_and_audit"), (3, "manual_shop_tables"), (4, "manual_order_purchase_snapshot"), (5, "manual_delivery_notification_outbox"), (6, "customer_ticket_notification_outbox"), (7, "per_event_telegram_notification_routes"), (8, "tenant_backup_policy_disabled_by_default"), (9, "durable_per_destination_admin_notification_outbox"), (10, "manual_product_category_binding")])
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_representative_unversioned_legacy_database_preserves_business_rows(self):
        # Start from the current schema, seed durable business records, then remove
        # migration-owned additions to model a pre-registry SQLite database.
        self.store.post_wallet_entry(telegram_id=1001, idempotency_key="legacy-ledger",
                                     amount_minor=2500, entry_type="deposit")
        with self.store._connect() as db:
            db.execute(
                """INSERT INTO customer_orders
                   (tenant_id,order_id,telegram_id,product_id,amount_minor,currency,status,idempotency_key)
                   VALUES('bot-alpha','legacy-order',1001,'legacy-product',1200,'IRR','pending','legacy-order-key')"""
            )
            db.execute(
                """INSERT INTO payment_attempts
                   (tenant_id,payment_id,telegram_id,provider,amount_minor,currency,status,idempotency_key,external_reference)
                   VALUES('bot-alpha','legacy-payment',1001,'manual',1300,'IRR','pending','legacy-payment-key','legacy-ref')"""
            )
            db.execute(
                """INSERT INTO customer_service_operations
                   (tenant_id,operation_id,telegram_id,service_id,change_type,idempotency_key,
                    amount_minor,currency,quantity,status)
                   VALUES('bot-alpha','legacy-operation',1001,'legacy-service','renewal',
                          'legacy-operation-key',400,'IRR',24,'reconcile_required')"""
            )
            db.execute("DROP INDEX IF EXISTS uq_reseller_idempotency")
            for table, column in (
                ("panel_connections", "metadata_json"),
                ("reseller_accounts", "idempotency_key"),
                ("customer_products", "category_id"),
                ("customer_orders", "product_snapshot_json"),
                ("customer_orders", "discount_code"),
                ("customer_orders", "discount_amount_minor"),
                ("manual_deposit_receipts", "receipt_file_id"),
                ("customer_service_operations", "before_expires_at"),
                ("customer_service_operations", "before_traffic_limit_bytes"),
                ("customer_service_operations", "target_expires_at"),
                ("customer_service_operations", "target_traffic_limit_bytes"),
            ):
                db.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
            db.execute("DROP TABLE customer_service_operation_events")
            db.execute("DROP TABLE schema_migrations")

        migrated = CustomerDataStore(self.path, tenant_id="bot-alpha")
        with migrated._connect() as db:
            versions = db.execute(
                "SELECT version, name FROM schema_migrations ORDER BY version"
            ).fetchall()
            self.assertEqual(
                [(row["version"], row["name"]) for row in versions],
                [(1, "core_additive_legacy_migrations"),
                 (2, "service_change_operations_and_audit"),
                 (3, "manual_shop_tables"),
                 (4, "manual_order_purchase_snapshot"),
                 (5, "manual_delivery_notification_outbox"),
                 (6, "customer_ticket_notification_outbox"),
                 (7, "per_event_telegram_notification_routes"),
                 (8, "tenant_backup_policy_disabled_by_default"), (9, "durable_per_destination_admin_notification_outbox"),
                 (10, "manual_product_category_binding")],
            )
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertIn("category_id", {
                row["name"] for row in db.execute("PRAGMA table_info(customer_products)")
            })
            self.assertIn("category_id", {
                row["name"] for row in db.execute("PRAGMA table_info(manual_products)")
            })
            self.assertIn("product_snapshot_json", {
                row["name"] for row in db.execute("PRAGMA table_info(customer_orders)")
            })
            self.assertIn("receipt_file_id", {
                row["name"] for row in db.execute("PRAGMA table_info(manual_deposit_receipts)")
            })
            self.assertIn("target_expires_at", {
                row["name"] for row in db.execute("PRAGMA table_info(customer_service_operations)")
            })
            self.assertIsNotNone(db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='index' AND name='uq_reseller_idempotency'"
            ).fetchone())
            self.assertIsNotNone(db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='trigger' "
                "AND name='customer_service_operation_events_no_update'"
            ).fetchone())
            audit_fk = db.execute(
                "PRAGMA foreign_key_list(customer_service_operation_events)"
            ).fetchall()
            self.assertEqual(
                {(row["from"], row["to"]) for row in audit_fk},
                {("tenant_id", "tenant_id"), ("operation_id", "operation_id")},
            )
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute(
                    """INSERT INTO customer_service_operation_events
                       (tenant_id,operation_id,event_type,actor_telegram_id,comparison,observed_value,reason)
                       VALUES('another-tenant','legacy-operation','operator_finalized_target_present',
                              1,'target_present','value','tenant FK regression')"""
                )
            self.assertEqual(
                tuple(db.execute(
                    "SELECT amount_minor, entry_type FROM wallet_ledger WHERE idempotency_key='legacy-ledger'"
                ).fetchone()),
                (2500, "deposit"),
            )
            self.assertEqual(
                tuple(db.execute(
                    "SELECT amount_minor, status FROM customer_orders WHERE order_id='legacy-order'"
                ).fetchone()),
                (1200, "pending"),
            )
            self.assertEqual(
                tuple(db.execute(
                    "SELECT amount_minor, status FROM payment_attempts WHERE payment_id='legacy-payment'"
                ).fetchone()),
                (1300, "pending"),
            )
            self.assertEqual(
                tuple(db.execute(
                    "SELECT amount_minor, status FROM customer_service_operations "
                    "WHERE operation_id='legacy-operation'"
                ).fetchone()),
                (400, "reconcile_required"),
            )
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM customer_service_operation_events"
            ).fetchone()[0], 0)

        # A second open must not reapply or duplicate either migration.
        reopened = CustomerDataStore(self.path, tenant_id="bot-alpha")
        with reopened._connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0], 10)
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_migration_failure_rolls_back_schema_data_and_version_row(self):
        self.store.post_wallet_entry(telegram_id=1001, idempotency_key="rollback-ledger",
                                     amount_minor=900, entry_type="deposit")
        with self.store._connect() as db:
            db.execute("DROP INDEX IF EXISTS uq_reseller_idempotency")
            for table, column in (
                ("panel_connections", "metadata_json"),
                ("reseller_accounts", "idempotency_key"),
                ("customer_products", "category_id"),
                ("customer_orders", "product_snapshot_json"),
            ):
                db.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
            db.execute("DELETE FROM schema_migrations")
            db.execute(
                """CREATE TRIGGER injected_migration_failure
                   BEFORE INSERT ON schema_migrations
                   WHEN NEW.version = 1
                   BEGIN SELECT RAISE(ABORT, 'injected migration failure'); END"""
            )

        with self.assertRaisesRegex(sqlite3.IntegrityError, "injected migration failure"):
            CustomerDataStore(self.path, tenant_id="bot-alpha")

        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            self.assertNotIn("metadata_json", {
                row["name"] for row in db.execute("PRAGMA table_info(panel_connections)")
            })
            self.assertNotIn("idempotency_key", {
                row["name"] for row in db.execute("PRAGMA table_info(reseller_accounts)")
            })
            self.assertNotIn("category_id", {
                row["name"] for row in db.execute("PRAGMA table_info(customer_products)")
            })
            self.assertNotIn("product_snapshot_json", {
                row["name"] for row in db.execute("PRAGMA table_info(customer_orders)")
            })
            self.assertIsNone(db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='index' AND name='uq_reseller_idempotency'"
            ).fetchone())
            self.assertEqual(db.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0], 0)
            self.assertEqual(tuple(db.execute(
                "SELECT amount_minor, entry_type FROM wallet_ledger WHERE idempotency_key='rollback-ledger'"
            ).fetchone()), (900, "deposit"))

    def test_unknown_future_schema_version_fails_closed(self):
        with self.store._connect() as db:
            db.execute(
                "INSERT INTO schema_migrations(version,name) VALUES(99,'future_schema')"
            )
        with self.assertRaisesRegex(CustomerStoreError, "newer than this application supports"):
            CustomerDataStore(self.path, tenant_id="bot-alpha")

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

    def test_concurrent_inventory_purchases_cannot_sell_the_last_key_twice(self):
        self.store.ensure_customer(1002, "bob", "Bob")
        self.store.post_wallet_entry(telegram_id=1001, idempotency_key="last-key-fund-a",
                                     amount_minor=3000, entry_type="deposit")
        self.store.post_wallet_entry(telegram_id=1002, idempotency_key="last-key-fund-b",
                                     amount_minor=3000, entry_type="deposit")
        self.store.put_manual_product(product_id="last-key", title="Last key", price_minor=700,
                                      delivery_mode="inventory")
        self.store.add_manual_inventory_key(product_id="last-key", secret_payload="ONLY-ONE-KEY")
        def buy(uid: int):
            try:
                return self.store.buy_manual_product(telegram_id=uid, product_id="last-key",
                    idempotency_key=f"last-key-order-{uid}")
            except CustomerStoreError as exc:
                return str(exc)
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(buy, (1001, 1002)))
        successes = [item for item in outcomes if isinstance(item, dict)]
        failures = [item for item in outcomes if isinstance(item, str)]
        self.assertEqual(len(successes), 1)
        self.assertEqual(successes[0]["delivery_payload"], "ONLY-ONE-KEY")
        self.assertEqual(failures, ["Manual product is out of stock; no wallet debit was made"])
        self.assertEqual(sorted((self.store.wallet_balance(1001), self.store.wallet_balance(1002))), [2300, 3000])
        self.assertEqual(self.store.manual_inventory_count("last-key"), 0)

    def test_manual_order_idempotent_replay_returns_original_purchase_snapshot(self):
        self.store.post_wallet_entry(telegram_id=1001, idempotency_key="manual-replay-fund",
                                     amount_minor=3000, entry_type="deposit")
        self.store.put_manual_product(product_id="manual-replay", title="Original title", price_minor=1200,
                                      currency="IRR", delivery_mode="instant",
                                      delivery_payload="original-delivery")
        first = self.store.buy_manual_product(telegram_id=1001, product_id="manual-replay",
                                              idempotency_key="manual-replay-key")
        # Product edits after purchase must not rewrite the order's original fulfillment contract.
        self.store.put_manual_product(product_id="manual-replay", title="Edited title", price_minor=2500,
                                      currency="IRR", delivery_mode="manual", delivery_payload="new-delivery")
        replay = self.store.buy_manual_product(telegram_id=1001, product_id="manual-replay",
                                               idempotency_key="manual-replay-key")
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(replay["order_id"], first["order_id"])
        self.assertEqual(replay["title"], "Original title")
        self.assertEqual(replay["delivery_mode"], "instant")
        self.assertEqual(replay["delivery_payload"], "original-delivery")
        self.assertEqual(self.store.wallet_balance(1001), 1800)

    def test_inventory_order_replay_returns_same_key_without_second_debit(self):
        self.store.post_wallet_entry(telegram_id=1001, idempotency_key="inventory-replay-fund",
                                     amount_minor=2500, entry_type="deposit")
        self.store.put_manual_product(product_id="inventory-replay", title="License", price_minor=700,
                                      currency="IRR", delivery_mode="inventory")
        self.store.add_manual_inventory_key(product_id="inventory-replay", secret_payload="LICENSE-ONE")
        first = self.store.buy_manual_product(telegram_id=1001, product_id="inventory-replay",
                                              idempotency_key="inventory-replay-key")
        replay = self.store.buy_manual_product(telegram_id=1001, product_id="inventory-replay",
                                               idempotency_key="inventory-replay-key")
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(replay["delivery_payload"], "LICENSE-ONE")
        self.assertEqual(replay["order_id"], first["order_id"])
        self.assertEqual(self.store.manual_inventory_count("inventory-replay"), 0)
        self.assertEqual(self.store.wallet_balance(1001), 1800)

    def test_fulfilled_manual_order_replay_returns_original_delivery(self):
        self.store.post_wallet_entry(telegram_id=1001, idempotency_key="manual-delivery-replay-fund",
                                     amount_minor=2500, entry_type="deposit")
        self.store.put_manual_product(product_id="manual-deliv-replay", title="Manual original", price_minor=700,
                                      currency="IRR", delivery_mode="manual")
        first = self.store.buy_manual_product(telegram_id=1001, product_id="manual-deliv-replay",
                                              idempotency_key="manual-delivery-replay-key")
        self.store.fulfill_manual_order(order_id=first["order_id"], delivery_payload="ACTIVATION-ORIGINAL",
                                        fulfilled_by=6883349141)
        self.store.put_manual_product(product_id="manual-deliv-replay", title="Renamed product", price_minor=800,
                                      currency="IRR", delivery_mode="instant", delivery_payload="DIFFERENT")
        replay = self.store.buy_manual_product(telegram_id=1001, product_id="manual-deliv-replay",
                                               idempotency_key="manual-delivery-replay-key")
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(replay["title"], "Manual original")
        self.assertEqual(replay["delivery_mode"], "manual")
        self.assertEqual(replay["status"], "fulfilled")
        self.assertEqual(replay["delivery_payload"], "ACTIVATION-ORIGINAL")
        self.assertEqual(self.store.wallet_balance(1001), 1800)

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
        with self.store._connect() as db:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                db.execute("UPDATE manual_product_order_events SET event_type='edited' WHERE tenant_id=? AND order_id=?",
                           (self.store.tenant_id, order["order_id"]))
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                db.execute("DELETE FROM manual_product_order_events WHERE tenant_id=? AND order_id=?",
                           (self.store.tenant_id, order["order_id"]))
        self.assertEqual(self.store.wallet_balance(1001), 1800)

    def test_manual_fulfillment_enqueues_durable_notification_atomically(self):
        self.store.post_wallet_entry(telegram_id=1001, idempotency_key="notify-outbox-fund",
                                     amount_minor=3000, entry_type="deposit")
        self.store.put_manual_product(product_id="outbox-plan", title="Outbox plan", price_minor=700,
                                     delivery_mode="manual")
        order = self.store.buy_manual_product(telegram_id=1001, product_id="outbox-plan",
                                              idempotency_key="outbox-order")
        fulfilled = self.store.fulfill_manual_order(order_id=order["order_id"],
            delivery_payload="OUTBOX-SECRET-DELIVERY", fulfilled_by=6883349141)
        self.assertEqual(fulfilled["status"], "fulfilled")
        claimed = self.store.claim_manual_delivery_notifications(now=time.time() + 10)
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0]["order_id"], order["order_id"])
        self.assertEqual(claimed[0]["telegram_id"], 1001)
        self.assertEqual(claimed[0]["delivery_payload"], "OUTBOX-SECRET-DELIVERY")
        self.store.mark_manual_delivery_notification_sent(claimed[0]["notification_id"], now=time.time() + 11)
        self.assertEqual(self.store.claim_manual_delivery_notifications(now=time.time() + 20), [])
        self.assertEqual(self.store.wallet_balance(1001), 2300)

    def test_manual_delivery_notifications_are_tenant_scoped(self):
        self.store.post_wallet_entry(telegram_id=1001, idempotency_key="notify-tenant-fund",
                                     amount_minor=3000, entry_type="deposit")
        self.store.put_manual_product(product_id="tenant-plan", title="Tenant plan", price_minor=700,
                                      delivery_mode="manual")
        order = self.store.buy_manual_product(telegram_id=1001, product_id="tenant-plan",
                                              idempotency_key="tenant-order")
        self.store.fulfill_manual_order(order_id=order["order_id"], delivery_payload="TENANT-PRIVATE-DELIVERY",
                                        fulfilled_by=6883349141)
        other_tenant = CustomerDataStore(self.path, tenant_id="bot-beta")
        other_tenant.ensure_customer(2002, "bob", "Bob")
        self.assertEqual(other_tenant.claim_manual_delivery_notifications(now=time.time() + 10), [])
        claimed = self.store.claim_manual_delivery_notifications(now=time.time() + 10)
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0]["delivery_payload"], "TENANT-PRIVATE-DELIVERY")

    def test_manual_notification_enqueue_failure_rolls_back_fulfillment(self):
        self.store.post_wallet_entry(telegram_id=1001, idempotency_key="notify-rollback-fund",
                                     amount_minor=3000, entry_type="deposit")
        self.store.put_manual_product(product_id="rollback-plan", title="Rollback plan", price_minor=700,
                                      delivery_mode="manual")
        order = self.store.buy_manual_product(telegram_id=1001, product_id="rollback-plan",
                                              idempotency_key="rollback-order")
        with self.store._connect() as db:
            db.execute("""CREATE TRIGGER fail_manual_delivery_enqueue
                BEFORE INSERT ON manual_delivery_notifications
                BEGIN SELECT RAISE(ABORT, 'injected outbox failure'); END""")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "injected outbox failure"):
            self.store.fulfill_manual_order(order_id=order["order_id"], delivery_payload="ROLLBACK-DELIVERY",
                                            fulfilled_by=6883349141)
        with self.store._connect() as db:
            row = db.execute("SELECT status,delivery_payload FROM manual_product_orders WHERE order_id=?",
                             (order["order_id"],)).fetchone()
            self.assertEqual(db.execute("SELECT COUNT(*) FROM manual_delivery_notifications").fetchone()[0], 0)
        self.assertEqual(tuple(row), ("pending_delivery", ""))
        self.assertEqual(self.store.wallet_balance(1001), 2300)

    def test_manual_delivery_notification_stale_claim_is_recovered_after_restart(self):
        self.store.post_wallet_entry(telegram_id=1001, idempotency_key="notify-stale-fund",
                                     amount_minor=3000, entry_type="deposit")
        self.store.put_manual_product(product_id="stale-plan", title="Stale plan", price_minor=700,
                                      delivery_mode="manual")
        order = self.store.buy_manual_product(telegram_id=1001, product_id="stale-plan",
                                              idempotency_key="stale-order")
        self.store.fulfill_manual_order(order_id=order["order_id"], delivery_payload="STALE-DELIVERY",
                                        fulfilled_by=6883349141)
        now = time.time() + 10
        first = self.store.claim_manual_delivery_notifications(now=now, lease_seconds=300)
        self.assertEqual(first[0]["attempts"], 1)
        reopened = CustomerDataStore(self.path, tenant_id="bot-alpha")
        recovered = reopened.claim_manual_delivery_notifications(now=now + 301, lease_seconds=300)
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0]["notification_id"], first[0]["notification_id"])
        self.assertEqual(recovered[0]["attempts"], 2)
        self.assertEqual(reopened.wallet_balance(1001), 2300)

    def test_manual_delivery_notification_retry_backoff_and_dead_letter(self):
        self.store.post_wallet_entry(telegram_id=1001, idempotency_key="notify-retry-fund",
                                     amount_minor=3000, entry_type="deposit")
        self.store.put_manual_product(product_id="retry-plan", title="Retry plan", price_minor=700,
                                      delivery_mode="manual")
        order = self.store.buy_manual_product(telegram_id=1001, product_id="retry-plan",
                                              idempotency_key="retry-order")
        self.store.fulfill_manual_order(order_id=order["order_id"], delivery_payload="RETRY-DELIVERY",
                                        fulfilled_by=6883349141)
        now = time.time() + 10
        item = self.store.claim_manual_delivery_notifications(now=now)[0]
        self.store.retry_manual_delivery_notification(item["notification_id"], attempt=item["attempts"],
                                                     error_code="telegram_error", now=now, max_attempts=2)
        self.assertEqual(self.store.claim_manual_delivery_notifications(now=now + 1), [])
        second = self.store.claim_manual_delivery_notifications(now=now + 2)[0]
        self.store.retry_manual_delivery_notification(second["notification_id"], attempt=second["attempts"],
                                                     error_code="telegram_error", now=now + 2, max_attempts=2)
        self.assertEqual(self.store.claim_manual_delivery_notifications(now=now + 9999), [])
        with self.store._connect() as db:
            row = db.execute("SELECT status,last_error_code FROM manual_delivery_notifications WHERE order_id=?",
                              (order["order_id"],)).fetchone()
        self.assertEqual(tuple(row), ("dead", "telegram_error"))
        self.assertEqual(self.store.wallet_balance(1001), 2300)

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


    def test_manual_order_snapshot_migration_backfills_legacy_order(self):
        self.store.put_manual_product(product_id="legacy-order", title="Legacy product title", price_minor=500,
                                      currency="IRR", delivery_mode="inventory")
        with self.store._connect() as db:
            db.execute("ALTER TABLE manual_product_orders DROP COLUMN product_title_snapshot")
            db.execute("ALTER TABLE manual_product_orders DROP COLUMN delivery_mode_snapshot")
            db.execute("DELETE FROM schema_migrations WHERE version=4")
            db.execute("""INSERT INTO manual_product_orders
                (tenant_id,order_id,telegram_id,product_id,amount_minor,currency,status,idempotency_key,delivery_payload)
                VALUES('bot-alpha','legacy-order-1',1001,'legacy-order',500,'IRR','fulfilled',
                       'legacy-order-key','LEGACY-DELIVERY')""")

        migrated = CustomerDataStore(self.path, tenant_id="bot-alpha")
        replay = migrated.buy_manual_product(telegram_id=1001, product_id="legacy-order",
                                             idempotency_key="legacy-order-key")
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(replay["title"], "Legacy product title")
        self.assertEqual(replay["delivery_mode"], "inventory")
        self.assertEqual(replay["delivery_payload"], "LEGACY-DELIVERY")
        with migrated._connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM schema_migrations WHERE version=4").fetchone()[0], 1)

    def test_v10_migration_adds_manual_category_column_without_losing_rows(self):
        self.store.put_manual_product(product_id="pre-v10", title="Pre-v10 item", price_minor=500,
            delivery_mode="manual")
        with self.store._connect() as db:
            db.execute("DROP INDEX IF EXISTS ix_manual_products_tenant_category_enabled")
            db.execute("ALTER TABLE manual_products DROP COLUMN category_id")
            db.execute("DELETE FROM schema_migrations WHERE version=10")
        migrated = CustomerDataStore(self.path, tenant_id="bot-alpha")
        product = migrated.get_manual_product("pre-v10")
        self.assertEqual(product["title"], "Pre-v10 item")
        self.assertEqual(product["category_id"], "")
        with migrated._connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM schema_migrations WHERE version=10").fetchone()[0], 1)
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_manual_shop_migration_preserves_existing_rows_and_creates_missing_tables(self):
        with self.store._connect() as db:
            db.execute(
                "INSERT INTO manual_products(tenant_id,product_id,title,price_minor,currency,delivery_mode) "
                "VALUES('bot-alpha','legacy-shop','Legacy shop item',500,'IRR','manual')"
            )
            db.execute("DELETE FROM schema_migrations WHERE version=3")

        reopened = CustomerDataStore(self.path, tenant_id="bot-alpha")
        with reopened._connect() as db:
            self.assertEqual(
                tuple(db.execute("SELECT product_id,title,price_minor FROM manual_products").fetchone()),
                ("legacy-shop", "Legacy shop item", 500),
            )
            self.assertEqual(db.execute("SELECT COUNT(*) FROM schema_migrations WHERE version=3").fetchone()[0], 1)
            db.execute("DROP TABLE manual_delivery_notifications")
            db.execute("DROP TABLE manual_product_order_events")
            db.execute("DROP TABLE manual_product_orders")
            db.execute("DROP TABLE manual_product_inventory")
            db.execute("DROP TABLE manual_products")
            db.execute("DELETE FROM schema_migrations WHERE version IN (3,4,5,10)")

        recreated = CustomerDataStore(self.path, tenant_id="bot-alpha")
        with recreated._connect() as db:
            tables = {row["name"] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            self.assertTrue({
                "manual_products", "manual_product_inventory",
                "manual_product_orders", "manual_product_order_events", "manual_delivery_notifications",
            }.issubset(tables))
            self.assertIn("category_id", {row["name"] for row in db.execute("PRAGMA table_info(manual_products)")})
            self.assertEqual(db.execute("SELECT COUNT(*) FROM schema_migrations WHERE version=3").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM schema_migrations WHERE version=5").fetchone()[0], 1)
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])


if __name__ == "__main__":
    unittest.main()
