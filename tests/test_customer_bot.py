import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from manubisguard.telegram import customer_bot as customer_bot_module
from manubisguard.telegram.customer_bot import (
    CUSTOMER_MENU,
    CustomerBot,
    CustomerBotConfig,
    _provider_freshness_text,
)
from manubisguard.sales.service import CustomerServiceChangeService, PurchaseOutcome


class CustomerBotConfigTests(unittest.TestCase):
    def test_requires_separate_customer_token(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "MANUBISGUARD_CUSTOMER_BOT_TOKEN"):
                CustomerBotConfig.from_env()

    def test_parses_customer_admin_allowlist(self):
        with patch.dict(os.environ, {
            "MANUBISGUARD_CUSTOMER_BOT_TOKEN": "customer-test-token",
            "MANUBISGUARD_CUSTOMER_ADMIN_IDS": "6883349141,12345",
        }, clear=True):
            cfg = CustomerBotConfig.from_env()
        self.assertEqual(cfg.admin_ids, frozenset({6883349141, 12345}))
        self.assertNotIn("customer-test-token", repr(cfg))

    def test_entrypoint_requires_explicit_staging_opt_in_before_loading_secrets(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(customer_bot_module.CustomerBotConfig, "from_env") as load_config:
            with self.assertRaisesRegex(SystemExit, "explicitly set MANUBISGUARD_TEST_MODE=1"):
                customer_bot_module.main()
        load_config.assert_not_called()


class ProviderFreshnessTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)

    def test_missing_or_malformed_timestamp_is_not_reported_fresh(self):
        self.assertIn("هنوز از پنل بررسی نشده", _provider_freshness_text(None, now=self.now))
        self.assertIn("قابل تشخیص نیست", _provider_freshness_text("not-a-date", now=self.now))

    def test_recent_timestamp_is_fresh_and_naive_sqlite_timestamp_is_utc(self):
        recent = (self.now - timedelta(minutes=10)).isoformat().replace("+00:00", "Z")
        self.assertIn("🟢", _provider_freshness_text(recent, now=self.now))
        sqlite_timestamp = (self.now - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
        self.assertIn("🟢", _provider_freshness_text(sqlite_timestamp, now=self.now))

    def test_old_timestamp_is_warned_and_future_clock_skew_is_not_fresh(self):
        old = (self.now - timedelta(days=2)).isoformat()
        future = (self.now + timedelta(hours=1)).isoformat()
        self.assertIn("🔴", _provider_freshness_text(old, now=self.now))
        self.assertIn("ساعت ثبت بررسی", _provider_freshness_text(future, now=self.now))


class CustomerBotUITests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._env = patch.dict(os.environ, {"ANUBIS_CUSTOMER_DB": str(Path(self.tmp.name) / "customer.sqlite3"), "ANUBIS_CONTENT_DB": str(Path(self.tmp.name) / "content.sqlite3")})
        self._env.start()
        self.addCleanup(self._env.stop)
        self.addCleanup(self.tmp.cleanup)
        self.config = CustomerBotConfig(token="dummy-customer-token", admin_ids=frozenset({6883349141}))
        self.bot = CustomerBot(self.config)
        self.bot.api.send_text = AsyncMock()
        self.bot.api.answer_callback = AsyncMock()

    async def test_admin_can_configure_backup_policy_but_cannot_enable_unimplemented_scheduler(self):
        admin = {"from":{"id":6883349141},"chat":{"id":6883349141,"type":"private"}}
        await self.bot._handle_message({**admin,"text":"/backup-policy schedule | daily | Europe/Berlin | 02:45 | 1440"})
        policy = self.bot.data.get_backup_policy()
        self.assertEqual(policy["frequency"], "daily")
        self.assertEqual(policy["timezone_name"], "Europe/Berlin")
        self.assertEqual(policy["local_time"], "02:45")
        self.assertEqual(policy["enabled"], 0)
        await self.bot._handle_message({**admin,"text":"/backup-policy destination | both | -1001234567890 | 77 | /var/lib/anubis-bot/backups"})
        policy = self.bot.data.get_backup_policy()
        self.assertEqual(policy["destination"], "both")
        self.assertEqual(policy["telegram_chat_id"], -1001234567890)
        self.assertEqual(policy["telegram_topic_id"], 77)
        await self.bot._handle_message({**admin,"text":"/backup-policy retention | 20 | 60"})
        policy = self.bot.data.get_backup_policy()
        self.assertEqual((policy["retention_count"], policy["retention_days"]), (20, 60))
        await self.bot._handle_message({**admin,"text":"/backup-policy enable"})
        self.assertEqual(self.bot.data.get_backup_policy()["enabled"], 0)
        self.assertIn("فعال‌سازی رد شد", self.bot.api.send_text.await_args.args[1])

    async def test_non_admin_cannot_change_backup_policy(self):
        before = self.bot.data.get_backup_policy()
        await self.bot._handle_message({"from":{"id":999},"chat":{"id":999,"type":"private"},
            "text":"/backup-policy schedule | daily | Europe/Berlin | 02:45 | 1440"})
        self.assertEqual(self.bot.data.get_backup_policy()["frequency"], before["frequency"])
        self.assertEqual(self.bot.data.get_backup_policy()["enabled"], 0)

    async def test_admin_can_configure_and_list_notification_routes(self):
        await self.bot._handle_message({"from":{"id":6883349141},"chat":{"id":6883349141,"type":"private"},
            "text":"/notify-route add support_ticket | -1001234567890 | 77"})
        routes = self.bot.data.list_notification_routes("support_ticket")
        self.assertEqual(len(routes), 1)
        self.assertEqual(routes[0]["chat_id"], -1001234567890)
        self.assertEqual(routes[0]["topic_id"], 77)
        self.bot.api.send_text.reset_mock()
        await self.bot._handle_message({"from":{"id":6883349141},"chat":{"id":6883349141,"type":"private"},
            "text":"/notify-route list"})
        self.assertIn("support_ticket", self.bot.api.send_text.await_args.args[1])
        self.assertIn("-1001234567890", self.bot.api.send_text.await_args.args[1])
        self.bot.api.send_text.reset_mock()
        await self.bot._handle_message({"from":{"id":6883349141},"chat":{"id":6883349141,"type":"private"},
            "text":"/notify-route"})
        help_text = self.bot.api.send_text.await_args.args[1]
        self.assertIn("تغییر سرویس", help_text)
        self.assertIn("تولیدکننده‌های تست سرویس، لاگ ربات و بک‌آپ هنوز متصل نیستند", help_text)
        self.assertIn("هشدار تلاش غیرمجاز برای اجرای دستور مدیریتی", help_text)
        self.assertNotIn("هشدار امنیتی هنوز متصل نیست", help_text)

    async def test_non_admin_sensitive_commands_are_blocked_and_redacted_security_alerts_are_idempotent(self):
        self.bot.data.add_notification_route(event_key="security_alert", chat_id=6883349141,
            created_by=6883349141)
        messages = [
            {"message_id": 501, "from": {"id": 1001}, "chat": {"id": 1001, "type": "private"},
             "text": "/wallet-credit 1001 | 500000 | IRR | private-receipt-reference"},
            {"message_id": 502, "from": {"id": 1001}, "chat": {"id": 1001, "type": "private"},
             "text": "/deposit-info set 4111111111111111 | Test User | Test Bank"},
        ]
        await self.bot._handle_message(messages[0])
        await self.bot._handle_message(messages[0])
        await self.bot._handle_message(messages[1])
        self.assertEqual(self.bot.api.send_text.await_count, 3)
        self.assertIn("فقط برای مدیر", self.bot.api.send_text.await_args_list[0].args[1])
        with self.bot.data._connect() as db:
            rows = db.execute("SELECT event_key,idempotency_key,payload_json FROM customer_admin_notifications ORDER BY notification_id").fetchall()
        self.assertEqual(len(rows), 1, "repeated attempts in one minute should be coalesced")
        for row in rows:
            self.assertEqual(row["event_key"], "security_alert")
            self.assertIn("unauthorized-admin-command:1001:", row["idempotency_key"])
            payload = json.loads(row["payload_json"])
            self.assertEqual(payload["status"], "blocked")
            self.assertEqual(payload["reason_code"], "unauthorized_admin_command")
            self.assertEqual(payload["customer_id"], 1001)
            rendered = json.dumps(payload)
            self.assertNotIn("wallet-credit", rendered)
            self.assertNotIn("private-receipt-reference", rendered)
            self.assertNotIn("4111111111111111", rendered)

    async def test_admin_can_list_and_requeue_dead_admin_notification_without_bypassing_queue(self):
        admin = {"from":{"id":6883349141},"chat":{"id":6883349141,"type":"private"}}
        self.bot.data.add_notification_route(event_key="security_alert", chat_id=1001, created_by=6883349141)
        notification_id = self.bot.data.enqueue_admin_notification(
            event_key="security_alert", idempotency_key="security-command-1",
            payload={"status":"blocked","reason_code":"auth_mismatch"}, now=100)
        delivery = self.bot.data.claim_admin_notification_deliveries(now=101)[0]
        self.bot.data.retry_admin_notification_delivery(delivery["delivery_id"], attempt=1,
            error_code="sendfailed", now=102, max_attempts=1)
        await self.bot._handle_message({**admin,"text":"/admin-notifications dead"})
        self.assertIn(f"#{notification_id}", self.bot.api.send_text.await_args.args[1])
        self.assertIn("security_alert", self.bot.api.send_text.await_args.args[1])
        self.bot.api.send_text.reset_mock()
        await self.bot._handle_message({**admin,"text":f"/admin-notification-retry {notification_id}"})
        self.assertEqual(self.bot.data.list_admin_notifications(status="pending")[0]["notification_id"], notification_id)
        self.bot.api.send_text.assert_awaited_once()
        self.assertIn("کارگر پس‌زمینه", self.bot.api.send_text.await_args.args[1])
        self.assertEqual(self.bot.api.send_text.await_args.args[0], 6883349141)

    async def test_shared_admin_notification_worker_retries_only_failed_destination(self):
        self.bot.data.add_notification_route(event_key="wallet", chat_id=1001, created_by=6883349141)
        self.bot.data.add_notification_route(event_key="wallet", chat_id=-1001234567890,
            topic_id=77, created_by=6883349141)
        notification_id = self.bot.data.enqueue_admin_notification(
            event_key="wallet", idempotency_key="wallet-worker-1",
            payload={"amount_minor": 5000, "currency": "IRR", "entry_type": "deposit_verified",
                     "customer_id": 1001, "status": "posted", "note": "must not be visible",
                     "password": "must-not-be-visible"}, now=1_800_000_000)
        self.bot.api.send_text.side_effect = [RuntimeError("simulated failure"), None]
        first = await self.bot._deliver_admin_notifications(now=1_800_000_000)
        self.assertEqual(first, {"sent": 1, "retry": 1, "dead": 0})
        self.assertEqual(self.bot.api.send_text.await_count, 2)
        first_args = self.bot.api.send_text.await_args_list[0]
        self.assertEqual(first_args.args[0], 1001)
        second_args = self.bot.api.send_text.await_args_list[1]
        self.assertEqual(second_args.args[0], -1001234567890)
        self.assertEqual(second_args.kwargs["message_thread_id"], 77)
        self.assertNotIn("must not be visible", second_args.args[1])
        self.assertNotIn("must-not-be-visible", second_args.args[1])
        self.assertEqual(self.bot.data.list_admin_notifications()[0]["status"], "pending")
        self.bot.api.send_text.reset_mock()
        self.bot.api.send_text.side_effect = None
        second = await self.bot._deliver_admin_notifications(now=1_800_000_003)
        self.assertEqual(second, {"sent": 1, "retry": 0, "dead": 0})
        self.bot.api.send_text.assert_awaited_once()
        self.assertEqual(self.bot.api.send_text.await_args.args[0], 1001)
        self.assertEqual(self.bot.data.list_admin_notifications()[0]["status"], "sent")
        self.assertEqual(self.bot.data.list_admin_notifications()[0]["notification_id"], notification_id)

    async def test_miniapp_support_ticket_outbox_delivers_ticket_to_configured_admin(self):
        self.bot.data.ensure_customer(1001, "alice", "Alice")
        self.bot.data.create_ticket(ticket_id="web-ticket-123", telegram_id=1001,
            subject="مشکل خرید", first_message="سفارش در حساب نمایش داده نمی‌شود.")
        counts = await self.bot._deliver_customer_ticket_notifications(now=1_800_000_000)
        self.assertEqual(counts["sent"], 1)
        self.bot.api.send_text.assert_awaited_once()
        args = self.bot.api.send_text.await_args.args
        self.assertEqual(args[0], 6883349141)
        self.assertIn("web-ticket-123", args[1])
        self.assertIn("مشکل خرید", args[1])
        self.assertIn("سفارش در حساب نمایش داده نمی‌شود", args[1])
        with self.bot.data._connect() as db:
            status = db.execute("SELECT status FROM customer_ticket_notifications WHERE ticket_id='web-ticket-123'").fetchone()[0]
        self.assertEqual(status, "sent")

    async def test_admin_can_requeue_dead_support_ticket_notification(self):
        self.bot.data.ensure_customer(1003, "cara", "Cara")
        self.bot.data.create_ticket(ticket_id="dead-ticket-123", telegram_id=1003,
            subject="اعلان ناموفق", first_message="این اعلان برای تست بازیابی است.")
        item = self.bot.data.claim_customer_ticket_notifications(now=1_800_000_000)[0]
        self.assertEqual(self.bot.data.retry_customer_ticket_notification(item["notification_id"],
            attempt=1, error_code="testfailure", now=1_800_000_000, max_attempts=1), "dead")
        self.bot.api.send_text.reset_mock()
        await self.bot._handle_message({"from":{"id":6883349141},"chat":{"id":6883349141,"type":"private"},
            "text":"/ticket-notification-retry dead-ticket-123"})
        state = self.bot.data.get_customer_ticket_notification("dead-ticket-123")
        self.assertEqual(state["status"], "sent")
        self.assertGreaterEqual(self.bot.api.send_text.await_count, 2)
        self.assertIn("dead-ticket-123", self.bot.api.send_text.await_args_list[-1].args[1])

    async def test_support_ticket_notification_uses_configured_group_topic_route(self):
        self.bot.data.add_notification_route(event_key="support_ticket", chat_id=-1001234567890,
            topic_id=77, created_by=6883349141)
        self.bot.data.ensure_customer(1002, "bob", "Bob")
        self.bot.data.create_ticket(ticket_id="topic-ticket-123", telegram_id=1002,
            subject="تاپیک پشتیبانی", first_message="این پیام باید به تاپیک برسد.")
        counts = await self.bot._deliver_customer_ticket_notifications(now=1_800_000_000)
        self.assertEqual(counts["sent"], 1)
        args = self.bot.api.send_text.await_args
        self.assertEqual(args.args[0], -1001234567890)
        self.assertIn("topic-ticket-123", args.args[1])
        self.assertEqual(args.kwargs["message_thread_id"], 77)

    async def test_my_service_operations_is_scoped_to_requesting_customer(self):
        self.bot.data.list_service_change_operations = Mock(return_value=[{
            "operation_id": "op-own-123", "status": "completed", "change_type": "renewal",
            "amount_minor": 1200, "currency": "IRR", "created_at": "2030-01-01 00:00:00",
        }])
        await self.bot._handle_message({
            "from": {"id": 1001},
            "chat": {"id": 1001, "type": "private"},
            "text": "/my-service-operations",
        })
        self.bot.data.list_service_change_operations.assert_called_once_with(
            status="all", limit=10, telegram_id=1001)
        rendered = self.bot.api.send_text.await_args.args[1]
        self.assertIn("op-own-123", rendered)
        self.assertIn("تکمیل‌شده", rendered)
        self.assertIn("\nمبلغ:", rendered)
        self.assertNotIn("\\\\n", rendered)

    async def test_start_has_customer_product_features_and_colored_buttons(self):
        await self.bot._handle_message({
            "from": {"id": 6883349141},
            "chat": {"id": 6883349141, "type": "private"},
            "text": "/start",
        })
        self.bot.api.send_text.assert_awaited_once()
        text = self.bot.api.send_text.await_args.args[1]
        self.assertIn("ربات فروش و خدمات", text)
        keyboard = self.bot.api.send_text.await_args.args[2]
        buttons = [button for row in keyboard["keyboard"] for button in row]
        labels = {button.get("text") for button in buttons}
        self.assertIn("📱 ورود به مینی‌اپلیکیشن", labels)
        self.assertIn("🧪 تست سرویس‌ها", labels)
        self.assertIn("🏪 خرید پنل نمایندگی", labels)
        self.assertNotIn("🗂 پنل‌های من", labels)
        self.assertIn("💳 کیف پول و شارژ", labels)
        self.assertIn("⚙️ سرویس‌های من", labels)
        self.assertIn("🛡 پنل مدیریت ربات", labels)
        self.assertTrue(keyboard["is_persistent"])
        self.assertEqual(CUSTOMER_MENU["inline_keyboard"][0][0]["style"], "primary")
        self.assertEqual(CUSTOMER_MENU["inline_keyboard"][1][0]["style"], "success")

    async def test_panel_check_surfaces_operation_report_without_claiming_live_permissions(self):
        from manubisguard.panels.contracts import PanelCapabilities
        self.bot.data.register_panel_connection(panel_id="panel-report", label="Report panel",
            provider="rebecca", base_url="https://panel.example", auth_mode="api_key",
            secret_ref="env:TEST_PANEL_SECRET", enabled=True)
        adapter = self.bot.registry.get("rebecca")
        adapter.health_check = AsyncMock(return_value={"ok": True})
        adapter.capabilities = AsyncMock(return_value=PanelCapabilities(create_user=True, health_check=True))
        adapter.capability_report = AsyncMock(return_value={"provider":"rebecca", "live_panel_verified":False,
            "operations":{"create_user":{"status":"unknown","adapter_method_present":True},
                          "manage_resellers":{"status":"unsupported","adapter_method_present":False}}})
        await self.bot._handle_message({"from":{"id":6883349141},
            "chat":{"id":6883349141,"type":"private"},"text":"/panel-check panel-report"})
        rendered = self.bot.api.send_text.await_args.args[1]
        self.assertIn("اتصال خواندنی موفق بود", rendered)
        self.assertIn("create_user: متد محلی موجود؛ مجوز پنل نامشخص", rendered)
        self.assertIn("manage_resellers: پشتیبانی نمی‌شود", rendered)
        self.assertIn("مجوز عملیات تغییردهنده", rendered)
        adapter.health_check.assert_awaited_once()
        adapter.capability_report.assert_awaited_once()

    async def test_customer_menu_uses_only_valid_https_webapp_url(self):
        with patch.dict(os.environ, {"MANUBIS_CUSTOMER_WEBAPP_URL":"https://mini.example/customer/"}):
            keyboard = self.bot._customer_menu()
        button = keyboard["inline_keyboard"][0][0]
        self.assertEqual(button["web_app"]["url"], "https://mini.example/customer/")
        self.assertNotIn("callback_data", button)
        with patch.dict(os.environ, {"MANUBIS_CUSTOMER_WEBAPP_URL":"http://mini.example/customer/"}):
            fallback = self.bot._customer_menu()["inline_keyboard"][0][0]
        self.assertEqual(fallback["callback_data"], "customer:miniapp")
        self.assertNotIn("web_app", fallback)

    async def test_miniapp_callback_explains_missing_secure_url(self):
        with patch.dict(os.environ, {"MANUBIS_CUSTOMER_WEBAPP_URL":""}):
            await self.bot._handle_callback({"id":"miniapp-cb", "from":{"id":6883349141},
                "message":{"chat":{"id":6883349141,"type":"private"}}, "data":"customer:miniapp"})
        rendered = self.bot.api.send_text.await_args.args[1]
        self.assertIn("MANUBIS_CUSTOMER_WEBAPP_URL", rendered)
        self.assertIn("HTTPS", rendered)

    async def test_trials_callback_clearly_reports_unavailable_until_implemented(self):
        await self.bot._handle_callback({"id":"trial-cb","from":{"id":999},
            "message":{"message_id":42,"chat":{"id":999,"type":"private"}},
            "data":"customer:trials"})
        rendered = self.bot.api.send_text.await_args.args[1]
        self.assertIn("هنوز پیاده‌سازی نشده", rendered)
        self.assertIn("فعلاً در دسترس نیست", rendered)
        self.assertNotIn("سرویس آزمایشی فعالی برای این حساب تعریف نشده", rendered)

    async def test_panel_page_explains_customer_boundary_and_no_live_connection(self):
        await self.bot._handle_callback({
            "id": "cb-1",
            "from": {"id": 6883349141},
            "message": {"chat": {"id": 6883349141, "type": "private"}},
            "data": "customer:panels",
        })
        self.bot.api.answer_callback.assert_awaited_once_with("cb-1")
        text = self.bot.api.send_text.await_args.args[1]
        self.assertIn("Rebecca", text)
        self.assertIn("اتصال خواندنی", text)

    async def test_support_message_is_persisted_as_customer_ticket(self):
        await self.bot._handle_message({
            "from": {"id": 999, "first_name": "Test"},
            "chat": {"id": 999, "type": "private"},
            "text": "/support",
        })
        self.assertIn(999, self.bot._pending_support)
        await self.bot._handle_message({
            "from": {"id": 999, "first_name": "Test"},
            "chat": {"id": 999, "type": "private"},
            "text": "اتصال برقرار نمی‌شود",
        })
        self.assertNotIn(999, self.bot._pending_support)
        tickets = self.bot.data.list_tickets(999)
        self.assertEqual(len(tickets), 1)
        self.assertEqual(tickets[0]["subject"], "اتصال برقرار نمی‌شود")

    async def test_customer_service_list_uses_private_detail_screen_for_subscription_link(self):
        self.bot.data.ensure_customer(999, first_name="Service owner")
        self.bot.data.upsert_service(
            service_id="svc-detail-123", telegram_id=999, product_id="prod-a",
            panel_id="pg-a", provider_user_id="provider-123", status="active",
            expires_at="2026-11-01T00:00:00Z", traffic_limit_bytes=10 * 1024**3,
            traffic_used_bytes=2 * 1024**3, provider_checked_at="2026-10-20T10:00:00Z",
        )
        with self.bot.data._connect() as db:
            db.execute(
                "UPDATE customer_services SET username=?,subscription_url=?,pricing_mode=? WHERE tenant_id=? AND service_id=?",
                ("user-a", "https://secret.invalid/sub/abc", "fixed", self.bot.data.tenant_id, "svc-detail-123"),
            )
        await self.bot._send_customer_services(999, 999)
        listing = self.bot.api.send_text.await_args.args[1]
        keyboard = self.bot.api.send_text.await_args.args[2]
        self.assertNotIn("https://secret.invalid/sub/abc", listing)
        self.assertTrue(any(
            button.get("callback_data") == "customer:service_detail:svc-detail-123"
            for row in keyboard["inline_keyboard"] for button in row
        ))

        self.bot.api.send_text.reset_mock()
        await self.bot._handle_callback({
            "id": "detail-cb",
            "from": {"id": 999},
            "message": {"chat": {"id": 999, "type": "private"}},
            "data": "customer:service_detail:svc-detail-123",
        })
        detail = self.bot.api.send_text.await_args.args[1]
        self.assertIn("جزئیات سرویس", detail)
        self.assertIn("user-a", detail)
        self.assertIn("8.00 GiB", detail)
        self.assertIn("https://secret.invalid/sub/abc", detail)
        self.assertIn("ممکن است تا بررسی بعدی پنل", detail)

    async def test_customer_service_refresh_reads_provider_and_updates_snapshot(self):
        from datetime import datetime, timezone
        from types import SimpleNamespace
        from manubisguard.panels.contracts import PanelUser
        self.bot.data.register_panel_connection(panel_id="pg-refresh", label="PG", provider="pasarguard",
            base_url="https://pg.example", auth_mode="api_key", secret_ref="env:PG_KEY", enabled=True)
        self.bot.data.ensure_customer(999, first_name="Service owner")
        self.bot.data.upsert_service(service_id="svc-refresh", telegram_id=999, product_id="prod-a",
            panel_id="pg-refresh", provider_user_id="123", status="active",
            expires_at="2026-11-01T00:00:00Z", traffic_limit_bytes=10 * 1024**3,
            traffic_used_bytes=2 * 1024**3)
        with self.bot.data._connect() as db:
            db.execute("UPDATE customer_services SET username=? WHERE tenant_id=? AND service_id=?",
                ("user-refresh", self.bot.data.tenant_id, "svc-refresh"))
        fake_adapter = SimpleNamespace(
            capabilities=AsyncMock(return_value=SimpleNamespace(read_status=True, read_usage=True)),
            get_user=AsyncMock(return_value=PanelUser(provider_user_id="123", username="user-refresh",
                status="active", used_bytes=4 * 1024**3, data_limit_bytes=12 * 1024**3,
                expire_at=datetime(2026, 12, 1, tzinfo=timezone.utc), subscription_url=None)),
            get_usage=AsyncMock(return_value={"used_bytes": 4 * 1024**3, "data_limit_bytes": 12 * 1024**3}),
        )
        with patch.object(self.bot.registry, "get", return_value=fake_adapter):
            await self.bot._handle_callback({"id": "refresh-cb", "from": {"id": 999},
                "message": {"chat": {"id": 999, "type": "private"}},
                "data": "customer:service_refresh:svc-refresh"})
        service = self.bot.data.get_customer_service("svc-refresh", telegram_id=999)
        self.assertEqual(service["traffic_used_bytes"], 4 * 1024**3)
        self.assertEqual(service["traffic_limit_bytes"], 12 * 1024**3)
        self.assertEqual(service["expires_at"], "2026-12-01T00:00:00Z")
        self.assertTrue(service["provider_checked_at"])
        self.assertIn("اطلاعات از پنل تازه شد", self.bot.api.send_text.await_args.args[1])
        fake_adapter.get_user.assert_awaited_once()
        fake_adapter.get_usage.assert_awaited_once()

    async def test_customer_service_refresh_does_not_call_provider_for_other_owner(self):
        self.bot.data.ensure_customer(998, first_name="Other owner")
        self.bot.data.upsert_service(service_id="svc-private-refresh", telegram_id=998, panel_id="pg-refresh",
            provider_user_id="123", status="active", traffic_used_bytes=123)
        with self.bot.data._connect() as db:
            db.execute("UPDATE customer_services SET username=? WHERE tenant_id=? AND service_id=?",
                ("other-user", self.bot.data.tenant_id, "svc-private-refresh"))
        with patch.object(self.bot.registry, "get") as registry_get:
            await self.bot._handle_callback({"id": "refresh-private", "from": {"id": 999},
                "message": {"chat": {"id": 999, "type": "private"}},
                "data": "customer:service_refresh:svc-private-refresh"})
        registry_get.assert_not_called()
        service = self.bot.data.get_customer_service("svc-private-refresh", telegram_id=998)
        self.assertEqual(service["traffic_used_bytes"], 123)

    async def test_customer_can_request_private_subscription_qr(self):
        self.bot.data.ensure_customer(999, first_name="QR owner")
        self.bot.data.upsert_service(service_id="svc-qr", telegram_id=999, panel_id="pg-qr",
            provider_user_id="qr-user", status="active")
        with self.bot.data._connect() as db:
            db.execute("UPDATE customer_services SET username=?,subscription_url=? WHERE tenant_id=? AND service_id=?",
                ("qr-user", "https://sub.example/private-token", self.bot.data.tenant_id, "svc-qr"))
        self.bot.api.send_photo_bytes = AsyncMock()
        with patch("manubisguard.telegram.customer_bot.make_subscription_qr_png", return_value=b"PNGDATA") as make_qr:
            await self.bot._handle_callback({"id": "qr-cb", "from": {"id": 999},
                "message": {"chat": {"id": 999, "type": "private"}},
                "data": "customer:service_qr:svc-qr"})
        make_qr.assert_called_once_with("https://sub.example/private-token")
        self.bot.api.send_photo_bytes.assert_awaited_once()
        args = self.bot.api.send_photo_bytes.await_args.args
        self.assertEqual(args[0], 999)
        self.assertEqual(args[2], b"PNGDATA")
        self.assertNotIn("private-token", self.bot.api.send_photo_bytes.await_args.kwargs.get("caption", ""))

    async def test_customer_cannot_open_another_users_service_detail(self):
        self.bot.data.ensure_customer(998, first_name="Other owner")
        self.bot.data.upsert_service(
            service_id="svc-other-owner", telegram_id=998, status="active",
            provider_user_id="provider-other",
        )
        await self.bot._handle_callback({
            "id": "detail-cb-other",
            "from": {"id": 999},
            "message": {"chat": {"id": 999, "type": "private"}},
            "data": "customer:service_detail:svc-other-owner",
        })
        self.assertIn("متعلق به حساب شما نیست", self.bot.api.send_text.await_args.args[1])

    async def test_admin_reply_is_saved_and_sent_to_ticket_owner(self):
        self.bot.data.ensure_customer(999, first_name="Customer")
        self.bot.data.create_ticket(ticket_id="ticket-xyz", telegram_id=999,
                                    subject="Help", first_message="Need help")
        await self.bot._handle_message({
            "from": {"id": 6883349141}, "chat": {"id": 6883349141, "type": "private"},
            "text": "/reply ticket-xyz | بررسی می‌کنیم",
        })
        self.assertEqual(len(self.bot.data.ticket_messages("ticket-xyz", telegram_id=999)), 2)
        self.assertEqual(self.bot.api.send_text.await_args_list[0].args[0], 999)
        self.assertIn("بررسی می‌کنیم", self.bot.api.send_text.await_args_list[0].args[1])

    async def test_rebecca_native_reseller_fails_closed_without_api_key_and_never_charges(self):
        self.bot.data.register_panel_connection(panel_id="rebecca-panel",label="Rebecca",provider="rebecca",
            base_url="https://rebecca.example",auth_mode="api_key",secret_ref="env:REBECCA_KEY",enabled=True)
        self.bot.data.ensure_customer(999, first_name="Reseller")
        self.bot.data.post_wallet_entry(telegram_id=999,idempotency_key="reseller-fund",amount_minor=2000,entry_type="deposit")
        self.bot.data.put_reseller_plan(plan_id="rb-plan",panel_id="rebecca-panel",title="Bot reseller",
            pricing_mode="usage",price_minor=0,unit_price_minor=500,currency="IRR",provider_role_id="")
        query={"id":"cb-1","from":{"id":999},"message":{"message_id":10,"chat":{"id":999,"type":"private"}},"data":"customer:reseller_buy:rb-plan"}
        await self.bot._handle_callback(query)
        query={"id":"cb-2","from":{"id":999},"message":{"message_id":11,"chat":{"id":999,"type":"private"}},"data":"customer:reseller_buy_confirm:rb-plan"}
        await self.bot._handle_callback(query)
        accounts=self.bot.data.list_reseller_accounts(999)
        self.assertEqual(len(accounts),0)
        self.assertEqual(self.bot.data.wallet_balance(999),2000)
        self.assertIn("انجام نشد", self.bot.api.send_text.await_args.args[1])

    async def test_admin_can_add_product_and_customer_shop_lists_it(self):
        self.bot.data.register_panel_connection(panel_id="pg-basic",label="PasarGuard",provider="pasarguard",
            base_url="https://pg.example",auth_mode="api_key",secret_ref="env:PG_KEY",enabled=True,
            metadata={"default_group_ids":[1]})
        await self.bot._handle_message({
            "from": {"id": 6883349141}, "chat": {"id": 6883349141, "type": "private"},
            "text": "/product add basic | پلن پایه | 100000 | IRR | 24 | 10",
        })
        self.assertEqual(self.bot.data.list_products()[0]["product_id"], "basic")
        self.assertEqual(self.bot.data.list_products()[0]["panel_id"], "pg-basic")
        self.bot.api.send_text.reset_mock()
        await self.bot._handle_message({
            "from": {"id": 999}, "chat": {"id": 999, "type": "private"}, "text": "/shop",
        })
        self.assertIn("پلن پایه", self.bot.api.send_text.await_args.args[1])
        self.assertIn("مدل ساعتی/مصرفی", self.bot.api.send_text.await_args.args[1])

    async def test_disabling_panel_hides_its_products_from_customer_shop(self):
        self.bot.data.register_panel_connection(panel_id="pg-shop",label="PG",provider="pasarguard",
            base_url="https://pg.example",auth_mode="api_key",secret_ref="env:PG_KEY",enabled=True,
            metadata={"default_group_ids":[1]})
        self.bot.data.put_product(product_id="shop1",title="Shop plan",price_minor=1000,currency="IRR",
            duration_hours=24,traffic_bytes=0,panel_id="pg-shop",group_ids=[1])
        await self.bot._handle_message({"from":{"id":6883349141},"chat":{"id":6883349141,"type":"private"},
            "text":"/panel-enable pg-shop off"})
        self.bot.api.send_text.reset_mock()
        await self.bot._handle_message({"from":{"id":999},"chat":{"id":999,"type":"private"},"text":"/shop"})
        self.assertIn("محصولی ندارد",self.bot.api.send_text.await_args.args[1])
        self.assertNotIn("Shop plan",self.bot.api.send_text.await_args.args[1])

    async def test_admin_can_bind_legacy_product_and_toggle_visibility(self):
        self.bot.data.put_product(product_id="legacy",title="Legacy",price_minor=1000,currency="IRR",duration_hours=24,traffic_bytes=0)
        self.bot.data.register_panel_connection(panel_id="rb",label="Rebecca",provider="rebecca",
            base_url="https://rb.example",auth_mode="api_key",secret_ref="env:RB_KEY",enabled=True,metadata={"service_id":123})
        await self.bot._handle_message({"from":{"id":6883349141},"chat":{"id":6883349141,"type":"private"},
            "text":"/product bind legacy | rb | "})
        bound=self.bot.data.get_product("legacy")
        self.assertEqual(bound["panel_id"],"rb")
        self.assertEqual(bound["group_ids_json"], '["123"]')
        await self.bot._handle_message({"from":{"id":6883349141},"chat":{"id":6883349141,"type":"private"},
            "text":"/product enable legacy off"})
        self.assertEqual(self.bot.data.list_products(),[])
        await self.bot._handle_message({"from":{"id":6883349141},"chat":{"id":6883349141,"type":"private"},
            "text":"/product enable legacy on"})
        self.assertEqual(len(self.bot.data.list_products()),1)

    async def test_admin_can_categorize_products_and_customer_can_filter_shop(self):
        self.bot.data.register_panel_connection(panel_id="pg",label="PasarGuard",provider="pasarguard",
            base_url="https://pg.example",auth_mode="api_key",secret_ref="env:PG_KEY",enabled=True,
            metadata={"default_group_ids":[1]})
        admin={"from":{"id":6883349141},"chat":{"id":6883349141,"type":"private"}}
        await self.bot._handle_message({**admin,"text":"/category add ai | سرویس‌های هوش مصنوعی"})
        await self.bot._handle_message({**admin,"text":"/product add ai-plan | AI Plan | fixed | 100 | 0 | IRR | 24 | 10 | pg | 1 | ai"})
        await self.bot._handle_message({**admin,"text":"/product add base-plan | Basic Plan | fixed | 100 | 0 | IRR | 24 | 10 | pg | 1"})
        self.assertEqual(self.bot.data.get_product("ai-plan")["category_id"], "ai")
        self.bot.api.send_text.reset_mock()
        await self.bot._handle_callback({"id":"shop","from":{"id":999},
            "message":{"chat":{"id":999,"type":"private"}},"data":"customer:shop"})
        keyboard=self.bot.api.send_text.await_args.args[2]
        callbacks={b.get("callback_data") for row in keyboard["inline_keyboard"] for b in row}
        self.assertIn("customer:category:ai",callbacks)
        self.bot.api.send_text.reset_mock()
        await self.bot._handle_callback({"id":"cat","from":{"id":999},
            "message":{"chat":{"id":999,"type":"private"}},"data":"customer:category:ai"})
        body=self.bot.api.send_text.await_args.args[1]
        self.assertIn("AI Plan",body)
        self.assertNotIn("Basic Plan",body)

    async def test_advanced_admin_product_command_saves_hourly_mode(self):
        self.bot.data.register_panel_connection(panel_id="pg",label="PasarGuard",provider="pasarguard",
            base_url="https://pg.example",auth_mode="api_key",secret_ref="env:PG_KEY",enabled=True,
            metadata={"default_group_ids":[1]})
        await self.bot._handle_message({
            "from": {"id": 6883349141}, "chat": {"id": 6883349141, "type": "private"},
            "text": "/product add hourly | ساعتی | hourly | 0 | 600 | IRR | 0 | 0 | pg | 1",
        })
        product = self.bot.data.get_product("hourly")
        self.assertEqual(product["pricing_mode"], "hourly")
        self.assertEqual(product["unit_price_minor"], 600)

    async def test_admin_configures_card_to_card_details_and_customer_sees_them(self):
        await self.bot._handle_message({"from":{"id":6883349141},"chat":{"id":6883349141,"type":"private"},
            "text":"/deposit-info set 1234-5678-1234-5678 | Test Holder | Test Bank"})
        self.assertEqual(self.bot.data.get_manual_deposit_details()["card_number"], "1234567812345678")
        await self.bot._handle_message({"from":{"id":999},"chat":{"id":999,"type":"private"},
            "text":"/deposit 2500 IRR"})
        body = self.bot.api.send_text.await_args.args[1]
        self.assertIn("1234567812345678", body)
        self.assertIn("/deposit-receipt", body)

    async def test_customer_can_attach_receipt_photo_and_admin_can_view_it(self):
        await self.bot._handle_message({"from":{"id":999},"chat":{"id":999,"type":"private"},
            "message_id":151,"text":"/deposit 2500 IRR"})
        payment_id=self.bot.api.send_text.await_args.args[1].split("شناسه پرداخت: ")[1].splitlines()[0]
        await self.bot._handle_message({"from":{"id":999},"chat":{"id":999,"type":"private"},
            "caption":f"/deposit-receipt {payment_id} | bank-track-151",
            "photo":[{"file_id":"telegram-photo-file-id"}]})
        receipt=self.bot.data.get_manual_deposit_receipt(payment_id)
        self.assertEqual(receipt["receipt_file_id"], "telegram-photo-file-id")
        self.bot.api.call = AsyncMock()
        await self.bot._handle_message({"from":{"id":6883349141},"chat":{"id":6883349141,"type":"private"},
            "text":f"/deposit-receipt-view {payment_id}"})
        self.bot.api.call.assert_awaited_with("sendPhoto", {
            "chat_id":6883349141, "photo":"telegram-photo-file-id",
            "caption":f"رسید پرداخت {payment_id} | کاربر 999 | پیگیری bank-track-151"})

    async def test_manual_deposit_requires_customer_submitted_reference_before_admin_credit(self):
        await self.bot._handle_message({"from":{"id":999},"chat":{"id":999,"type":"private"},
            "message_id":141,"text":"/deposit 2500 IRR"})
        payment_id=self.bot.api.send_text.await_args.args[1].split("شناسه پرداخت: ")[1].splitlines()[0]
        await self.bot._handle_message({"from":{"id":6883349141},"chat":{"id":6883349141,"type":"private"},
            "text":f"/wallet-verify {payment_id} | 2500 | IRR | guessed-reference"})
        self.assertEqual(self.bot.data.wallet_balance(999), 0)
        self.assertEqual(self.bot.data.get_payment_attempt(payment_id)["status"], "pending")

    async def test_manual_deposit_request_and_admin_verification_credit_once(self):
        await self.bot._handle_message({
            "from":{"id":999,"first_name":"Customer"},"chat":{"id":999,"type":"private"},
            "message_id":41,"text":"/deposit 2500 IRR",
        })
        request_text=self.bot.api.send_text.await_args.args[1]
        payment_id=request_text.split("شناسه پرداخت: ")[1].splitlines()[0]
        self.assertEqual(self.bot.data.get_payment_attempt(payment_id,999)["status"],"pending")
        self.assertEqual(self.bot.data.wallet_balance(999),0)
        await self.bot._handle_message({"from":{"id":999},"chat":{"id":999,"type":"private"},
            "text":f"/deposit-receipt {payment_id} | bank-ref-12345"})
        command={"from":{"id":6883349141},"chat":{"id":6883349141,"type":"private"},
                 "text":f"/wallet-verify {payment_id} | 2500 | IRR | bank-ref-12345"}
        await self.bot._handle_message(command)
        await self.bot._handle_message(command)
        self.assertEqual(self.bot.data.wallet_balance(999),2500)
        self.assertEqual(self.bot.data.get_payment_attempt(payment_id)["status"],"verified")
        events = self.bot.data.list_payment_attempt_events(payment_id)
        self.assertEqual([event["event_type"] for event in events], ["created", "verified"])
        self.assertEqual(events[-1]["actor_telegram_id"], 6883349141)
        await self.bot._handle_message({"from":{"id":6883349141},"chat":{"id":6883349141,"type":"private"},
            "text":f"/payment-events {payment_id}"})
        history = self.bot.api.send_text.await_args.args[1]
        self.assertIn("سابقه تغییرناپذیر پرداخت", history)
        self.assertIn("bank-ref-12345", history)
        self.assertIn("6883349141", history)

    async def test_manual_deposit_amount_mismatch_never_credits_wallet(self):
        await self.bot._handle_message({
            "from":{"id":999,"first_name":"Customer"},"chat":{"id":999,"type":"private"},
            "message_id":42,"text":"/deposit 2500 IRR",
        })
        payment_id=self.bot.api.send_text.await_args.args[1].split("شناسه پرداخت: ")[1].splitlines()[0]
        await self.bot._handle_message({"from":{"id":999},"chat":{"id":999,"type":"private"},
            "text":f"/deposit-receipt {payment_id} | bank-ref-mismatch"})
        await self.bot._handle_message({
            "from":{"id":6883349141},"chat":{"id":6883349141,"type":"private"},
            "text":f"/wallet-verify {payment_id} | 2501 | IRR | bank-ref-mismatch",
        })
        self.assertEqual(self.bot.data.wallet_balance(999),0)
        self.assertEqual(self.bot.data.get_payment_attempt(payment_id)["status"],"pending")

    async def test_customer_cannot_read_payment_audit_events(self):
        self.bot.data.ensure_customer(999)
        self.bot.data.create_payment_attempt(payment_id="private-audit-payment", telegram_id=999,
            provider="manual", amount_minor=500, currency="IRR", idempotency_key="private-audit-intent")
        self.bot.data.settle_verified_wallet_deposit(payment_id="private-audit-payment",
            verified_amount_minor=500, verified_currency="IRR", external_reference="private-bank-ref",
            verified_by=6883349141)
        await self.bot._handle_message({"from":{"id":999},"chat":{"id":999,"type":"private"},
            "text":"/payment-events private-audit-payment"})
        response = self.bot.api.send_text.await_args.args[1]
        self.assertNotIn("private-bank-ref", response)
        self.assertNotIn("سابقه تغییرناپذیر پرداخت", response)

    async def test_admin_wallet_credit_is_idempotent_by_receipt_reference(self):
        self.bot.data.ensure_customer(999)
        command={"from":{"id":6883349141},"chat":{"id":6883349141,"type":"private"},
                 "text":"/wallet-credit 999 | 2500 | IRR | receipt-001"}
        await self.bot._handle_message(command)
        await self.bot._handle_message(command)
        self.assertEqual(self.bot.data.wallet_balance(999),2500)

    async def test_admin_can_configure_trust_channel_without_exposing_secrets(self):
        await self.bot._handle_message({
            "from": {"id": 6883349141}, "chat": {"id": 6883349141, "type": "private"},
            "text": "/trust-channel set @manubis_trust | on | privacy_on | new_subscription,reseller_created",
        })
        config = self.bot.data.get_trust_channel_config(self.bot.data.tenant_id, self.bot.bot_instance_id)
        self.assertTrue(config.enabled)
        self.assertTrue(config.privacy_mode)
        self.assertEqual(config.chat_id, "@manubis_trust")

    async def test_trust_event_is_outboxed_and_privacy_safe(self):
        self.bot.data.set_trust_channel_config(bot_instance_id=self.bot.bot_instance_id,
            chat_id="@manubis_trust", enabled=True, enabled_events=["new_subscription"], privacy_mode=True)
        await self.bot._emit_trust_event(__import__('manubisguard.notifications.trust_channel', fromlist=['TrustEvent']).TrustEvent.NEW_SUBSCRIPTION,
            {"public_order_id":"ORD_123", "amount_minor":100, "currency":"IRR", "username":"private-user",
             "subscription_url":"https://secret.invalid/token", "password":"secret"}, "purchase:ORD_123")
        self.bot.api.send_text.assert_awaited_once()
        sent = self.bot.api.send_text.await_args.args[1]
        self.assertIn("ORD_123", sent)
        self.assertNotIn("private-user", sent)
        self.assertNotIn("secret.invalid", sent)

    async def test_wallet_displays_all_currencies_without_mislabeling(self):
        self.bot.data.ensure_customer(999)
        self.bot.data.post_wallet_entry(telegram_id=999,idempotency_key="wallet-irr",amount_minor=1200,currency="IRR",entry_type="deposit")
        self.bot.data.post_wallet_entry(telegram_id=999,idempotency_key="wallet-eur",amount_minor=45,currency="EUR",entry_type="deposit")
        await self.bot._handle_message({"from":{"id":999},"chat":{"id":999,"type":"private"},"text":"/wallet"})
        body=self.bot.api.send_text.await_args.args[1]
        self.assertIn("IRR: 1,200",body)
        self.assertIn("EUR: 45",body)

    async def test_non_admin_wallet_reads_persistent_ledger(self):
        self.bot.data.ensure_customer(999)
        self.bot.data.post_wallet_entry(telegram_id=999, idempotency_key="seed-test",
                                        amount_minor=1234, entry_type="deposit")
        await self.bot._handle_message({
            "from": {"id": 999}, "chat": {"id": 999, "type": "private"},
            "text": "/wallet",
        })
        self.assertIn("1,234", self.bot.api.send_text.await_args.args[1])

    async def test_non_admin_customer_can_start_without_admin_controls(self):
        await self.bot._handle_message({
            "from": {"id": 999},
            "chat": {"id": 999, "type": "private"},
            "text": "/start",
        })
        self.bot.api.send_text.assert_awaited_once()
        keyboard = self.bot.api.send_text.await_args.args[2]
        labels = {b.get("text") for row in keyboard["keyboard"] for b in row}
        self.assertIn("🛒 خرید سرویس", labels)
        self.assertNotIn("🛡 پنل مدیریت ربات", labels)


    async def test_duplicate_manual_purchase_callback_returns_existing_delivery_without_second_charge(self):
        self.bot.data.ensure_customer(999, first_name="Buyer")
        self.bot.data.post_wallet_entry(telegram_id=999, idempotency_key="manual-callback-fund",
                                        amount_minor=3000, entry_type="deposit")
        self.bot.data.put_manual_product(product_id="callback-product", title="Callback product", price_minor=900,
            currency="IRR", delivery_mode="instant", delivery_payload="CALLBACK-DELIVERY")
        query = {"id":"manual-buy-callback", "from":{"id":999},
            "message":{"message_id":456,"chat":{"id":999,"type":"private"}},
            "data":"customer:manualbuy_confirm:callback-product"}
        await self.bot._handle_callback(query)
        first_body = self.bot.api.send_text.await_args.args[1]
        self.assertIn("CALLBACK-DELIVERY", first_body)
        self.bot.api.send_text.reset_mock()
        await self.bot._handle_callback(query)
        replay_body = self.bot.api.send_text.await_args.args[1]
        self.assertIn("قبلاً ثبت شده", replay_body)
        self.assertIn("CALLBACK-DELIVERY", replay_body)
        self.assertEqual(self.bot.data.wallet_balance(999), 2100)

    async def test_manual_inventory_delivers_unique_key_and_out_of_stock_does_not_charge(self):
        self.bot.data.ensure_customer(999, first_name="Buyer")
        self.bot.data.post_wallet_entry(telegram_id=999,idempotency_key="inventory-fund",amount_minor=5000,entry_type="deposit")
        self.bot.data.put_manual_product(product_id="license-stock",title="License",price_minor=1000,
            currency="IRR",delivery_mode="inventory",delivery_payload="",description="unique stock")
        self.bot.data.add_manual_inventory_key(product_id="license-stock",secret_payload="UNIQUE-KEY-001")
        first=self.bot.data.buy_manual_product(telegram_id=999,product_id="license-stock",idempotency_key="inventory-order-1")
        self.assertEqual(first["delivery_payload"],"UNIQUE-KEY-001")
        self.assertEqual(self.bot.data.manual_inventory_count("license-stock"),0)
        with self.assertRaisesRegex(ValueError,"out of stock"):
            self.bot.data.buy_manual_product(telegram_id=999,product_id="license-stock",idempotency_key="inventory-order-2")
        self.assertEqual(self.bot.data.wallet_balance(999),4000)

    async def test_admin_can_toggle_customer_home_buttons_and_setting_persists(self):
        await self.bot._handle_callback({"id":"buttons-open","from":{"id":6883349141},
            "message":{"chat":{"id":6883349141,"type":"private"}},"data":"content:buttons"})
        buttons=self.bot.api.send_text.await_args.args[2]["inline_keyboard"]
        callbacks={b.get("callback_data") for row in buttons for b in row}
        self.assertIn("content:toggle:menu_shop_enabled",callbacks)
        self.bot.api.send_text.reset_mock()
        await self.bot._handle_callback({"id":"buttons-toggle","from":{"id":6883349141},
            "message":{"chat":{"id":6883349141,"type":"private"}},"data":"content:toggle:menu_shop_enabled"})
        self.assertEqual(self.bot.content.get("menu_shop_enabled"),"off")
        self.bot.api.send_text.reset_mock()
        await self.bot._handle_message({"from":{"id":999},"chat":{"id":999,"type":"private"},"text":"/start"})
        labels={b.get("text") for row in self.bot.api.send_text.await_args.args[2]["keyboard"] for b in row}
        self.assertNotIn("🛒 خرید سرویس",labels)

    async def test_reply_keyboard_shop_button_routes_to_customer_shop(self):
        await self.bot._handle_message({"from":{"id":999},"chat":{"id":999,"type":"private"},"text":"/start"})
        self.bot.api.send_text.reset_mock()
        await self.bot._handle_message({"from":{"id":999},"chat":{"id":999,"type":"private"},"text":"🛒 خرید سرویس"})
        self.assertTrue(self.bot.api.send_text.await_count >= 1)
        self.assertIn("فروشگاه",self.bot.api.send_text.await_args.args[1])

    async def test_admin_home_button_is_admin_only_and_can_be_hidden(self):
        await self.bot._handle_message({"from":{"id":6883349141},"chat":{"id":6883349141,"type":"private"},"text":"/start"})
        labels={b.get("text") for row in self.bot.api.send_text.await_args.args[2]["keyboard"] for b in row}
        self.assertIn("🛡 پنل مدیریت ربات",labels)
        self.bot.content.set("menu_admin_enabled","off")
        self.bot.api.send_text.reset_mock()
        await self.bot._handle_message({"from":{"id":6883349141},"chat":{"id":6883349141,"type":"private"},"text":"/start"})
        labels={b.get("text") for row in self.bot.api.send_text.await_args.args[2]["keyboard"] for b in row}
        self.assertNotIn("🛡 پنل مدیریت ربات",labels)

    async def test_start_photo_contains_welcome_caption_and_keyboard(self):
        self.bot.content.set("logo_file_id","test-file-id")
        self.bot.api.call=AsyncMock(return_value={})
        await self.bot._send_brand_message(6883349141,self.bot._customer_menu(include_admin=True,customer_id=6883349141))
        self.bot.api.call.assert_awaited_once()
        args=self.bot.api.call.await_args.args
        self.assertEqual(args[0],"sendPhoto")
        self.assertIn("ربات فروش و خدمات",args[1]["caption"])
        self.assertIn("keyboard",args[1]["reply_markup"])

    async def test_admin_menu_is_separate_and_denied_to_customer(self):
        await self.bot._handle_message({"from":{"id":999},"chat":{"id":999,"type":"private"},"text":"/admin"})
        self.assertIn("فقط برای مدیر مجاز است", self.bot.api.send_text.await_args.args[1])
        self.bot.api.send_text.reset_mock()
        await self.bot._handle_message({"from":{"id":6883349141},"chat":{"id":6883349141,"type":"private"},"text":"/admin"})
        self.assertIn("پنل مدیریت", self.bot.api.send_text.await_args.args[1])
        buttons=self.bot.api.send_text.await_args.args[2]["inline_keyboard"]
        callbacks={b.get("callback_data") for row in buttons for b in row}
        self.assertIn("admin:panels", callbacks)
        self.assertIn("admin:products", callbacks)
        self.assertNotIn("admin:panels", {b.get("callback_data") for row in CUSTOMER_MENU["inline_keyboard"] for b in row})

    async def test_manual_product_is_independent_of_panel_and_delivered_after_wallet_purchase(self):
        self.bot.data.ensure_customer(999, first_name="Buyer")
        self.bot.data.post_wallet_entry(telegram_id=999,idempotency_key="manual-fund",amount_minor=5000,entry_type="deposit")
        self.bot.data.put_manual_product(product_id="chatgpt-key",title="ChatGPT Subscription",price_minor=2500,
            currency="IRR",delivery_mode="instant",delivery_payload="TEST-KEY-DO-NOT-USE",description="test item")
        await self.bot._handle_callback({"id":"manual-shop","from":{"id":999},
            "message":{"message_id":10,"chat":{"id":999,"type":"private"}},"data":"customer:shop"})
        text=self.bot.api.send_text.await_args.args[1]
        self.assertIn("ChatGPT Subscription",text)
        self.bot.api.send_text.reset_mock()
        await self.bot._handle_callback({"id":"manual-buy","from":{"id":999},
            "message":{"message_id":11,"chat":{"id":999,"type":"private"}},"data":"customer:manualbuy:chatgpt-key"})
        self.assertIn("تأیید خرید",self.bot.api.send_text.await_args.args[1])
        self.bot.api.send_text.reset_mock()
        await self.bot._handle_callback({"id":"manual-confirm","from":{"id":999},
            "message":{"message_id":12,"chat":{"id":999,"type":"private"}},"data":"customer:manualbuy_confirm:chatgpt-key"})
        self.assertIn("TEST-KEY-DO-NOT-USE",self.bot.api.send_text.await_args.args[1])
        self.assertEqual(self.bot.data.wallet_balance(999),2500)

    async def test_service_change_confirmation_does_not_charge_before_execute_callback(self):
        self.bot.data.ensure_customer(999, first_name="Buyer")
        self.bot.data.register_panel_connection(panel_id="pg-confirm", label="PasarGuard", provider="pasarguard",
            base_url="https://pg.example", auth_mode="api_key", secret_ref="env:PG_CONFIRM", enabled=True)
        self.bot.data.put_product(product_id="confirm-product", title="Confirm plan", price_minor=2500,
            currency="IRR", duration_hours=24, traffic_bytes=2 * 1024**3,
            panel_id="pg-confirm", group_ids=["1"])
        self.bot.data.post_wallet_entry(telegram_id=999, idempotency_key="confirm-funds",
            amount_minor=5000, entry_type="deposit")
        self.bot.data.create_metered_service(service_id="confirm-service", telegram_id=999,
            product={"product_id":"confirm-product","panel_id":"pg-confirm","pricing_mode":"fixed",
                     "currency":"IRR","unit_price_minor":2500,"duration_hours":24,"traffic_bytes":2 * 1024**3,
                     "expires_at":"2030-01-01T00:00:00+00:00"},
            provider_user_id="provider-confirm", username="confirm-user", subscription_url=None, now=1000)
        balance_before = self.bot.data.wallet_balance(999)
        with patch.object(CustomerServiceChangeService, "preview", new=AsyncMock(return_value={
            "quantity": 24, "amount_minor": 2500, "currency": "IRR",
            "before_expires_at": "2030-01-01T00:00:00+00:00",
            "target_expires_at": "2030-01-02T00:00:00+00:00",
            "before_traffic_limit_bytes": 2 * 1024**3, "traffic_used_bytes": 0,
            "provider_status": "active",
        })):
            await self.bot._handle_callback({"id":"renew-preview","from":{"id":999},
                "message":{"message_id":40,"chat":{"id":999,"type":"private"}},
                "data":"customer:renew_confirm:confirm-service"})
        preview = self.bot.api.send_text.await_args.args[1]
        keyboard = self.bot.api.send_text.await_args.args[2]
        self.assertIn("24 ساعت", preview)
        self.assertIn("2,500 IRR", preview)
        self.assertEqual(self.bot.data.wallet_balance(999), balance_before)
        self.assertEqual(self.bot.data.list_service_change_operations(status="all"), [])
        callbacks = [b["callback_data"] for row in keyboard["inline_keyboard"] for b in row]
        self.assertIn("customer:renew_execute:confirm-service", callbacks)

    async def test_service_change_confirmation_rejects_another_users_service(self):
        self.bot.data.ensure_customer(999, first_name="Owner")
        self.bot.data.ensure_customer(1000, first_name="Other")
        self.bot.data.register_panel_connection(panel_id="pg-confirm", label="PasarGuard", provider="pasarguard",
            base_url="https://pg.example", auth_mode="api_key", secret_ref="env:PG_CONFIRM", enabled=True)
        self.bot.data.put_product(product_id="confirm-product", title="Confirm plan", price_minor=2500,
            currency="IRR", duration_hours=24, traffic_bytes=2 * 1024**3,
            panel_id="pg-confirm", group_ids=["1"])
        self.bot.data.create_metered_service(service_id="owned-service", telegram_id=999,
            product={"product_id":"confirm-product","panel_id":"pg-confirm","pricing_mode":"fixed",
                     "currency":"IRR","unit_price_minor":2500,"duration_hours":24,"traffic_bytes":2 * 1024**3,
                     "expires_at":"2030-01-01T00:00:00+00:00"},
            provider_user_id="provider-owned", username="owned-user", subscription_url=None, now=1000)
        await self.bot._handle_callback({"id":"foreign-renew","from":{"id":1000},
            "message":{"message_id":41,"chat":{"id":1000,"type":"private"}},
            "data":"customer:renew_confirm:owned-service"})
        self.assertIn("متعلق به این حساب نیست", self.bot.api.send_text.await_args.args[1])
        self.assertEqual(self.bot.data.list_service_change_operations(status="all"), [])

    async def test_admin_can_list_and_inspect_reconciliation_required_service_changes(self):
        self.bot.data.ensure_customer(6883349141, first_name="Admin")
        self.bot.data.register_panel_connection(panel_id="reconcile-panel", label="Panel", provider="pasarguard",
            base_url="https://panel.example", auth_mode="api_key", secret_ref="env:PANEL_RECONCILE", enabled=True)
        self.bot.data.put_product(product_id="reconcile-product", title="Reconcile product", price_minor=500,
            currency="IRR", duration_hours=12, traffic_bytes=1024, panel_id="reconcile-panel", group_ids=["1"])
        self.bot.data.post_wallet_entry(telegram_id=6883349141, idempotency_key="reconcile-funds", amount_minor=1000,
            entry_type="deposit")
        self.bot.data.create_metered_service(service_id="reconcile-service", telegram_id=6883349141,
            product={"product_id":"reconcile-product","panel_id":"reconcile-panel","pricing_mode":"fixed",
                     "currency":"IRR","unit_price_minor":0,"duration_hours":12,"traffic_bytes":1024,
                     "expires_at":"2030-01-01T00:00:00+00:00"},
            provider_user_id="provider-reconcile", username="reconcile-user", subscription_url=None, now=1000)
        op = self.bot.data.reserve_service_change(operation_id="op-reconcile", telegram_id=6883349141,
            service_id="reconcile-service", change_type="renewal", idempotency_key="reconcile-key", now=1000)
        self.bot.data.flag_service_change_reconciliation(operation_id=op["operation_id"], reason_code="TimeoutError")
        await self.bot._handle_message({"from":{"id":6883349141},"chat":{"id":6883349141,"type":"private"},
            "text":"/service-operations reconcile_required"})
        self.assertIn("op-reconcile", self.bot.api.send_text.await_args.args[1])
        self.assertIn("فقط گزارش می‌دهد", self.bot.api.send_text.await_args.args[1])
        self.bot.api.send_text.reset_mock()
        await self.bot._handle_message({"from":{"id":6883349141},"chat":{"id":6883349141,"type":"private"},
            "text":"/service-operation op-reconcile"})
        detail = self.bot.api.send_text.await_args.args[1]
        self.assertIn("reconcile_required:timeouterror", detail)
        self.assertIn("reconcile-user", detail)
        self.assertIn("وضعیت واقعی پنل جداگانه بررسی شود", detail)

    async def test_admin_service_operation_finalize_command_is_allowlisted_and_audited(self):
        self.bot.data.ensure_customer(6883349141, first_name="Admin")
        self.bot.data.register_panel_connection(panel_id="reconcile-panel", label="Panel", provider="pasarguard",
            base_url="https://panel.example", auth_mode="api_key", secret_ref="env:PANEL_RECONCILE", enabled=True)
        self.bot.data.put_product(product_id="reconcile-product", title="Reconcile product", price_minor=500,
            currency="IRR", duration_hours=12, traffic_bytes=1024, panel_id="reconcile-panel", group_ids=["1"])
        self.bot.data.post_wallet_entry(telegram_id=6883349141, idempotency_key="finalize-funds", amount_minor=1000,
            entry_type="deposit")
        self.bot.data.create_metered_service(service_id="reconcile-service", telegram_id=6883349141,
            product={"product_id":"reconcile-product","panel_id":"reconcile-panel","pricing_mode":"fixed",
                     "currency":"IRR","unit_price_minor":0,"duration_hours":12,"traffic_bytes":1024,
                     "expires_at":"2030-01-01T00:00:00+00:00"},
            provider_user_id="provider-reconcile", username="reconcile-user", subscription_url=None, now=1000)
        op = self.bot.data.reserve_service_change(operation_id="op-finalize-bot", telegram_id=6883349141,
            service_id="reconcile-service", change_type="renewal", idempotency_key="finalize-bot-key", now=1000)
        self.bot.data.persist_service_change_target(operation_id=op["operation_id"],
            before_expires_at="2030-01-01T00:00:00+00:00", before_traffic_limit_bytes=1024,
            target_expires_at="2030-01-01T12:00:00+00:00", target_traffic_limit_bytes=None)
        self.bot.data.flag_service_change_reconciliation(operation_id=op["operation_id"], reason_code="timeout")
        with patch.object(CustomerServiceChangeService, "inspect_reconciliation", new=AsyncMock(return_value={
            **op, "comparison":"target_present", "observed_value":"2030-01-01T12:00:00+00:00",
            "identity_verified":True,
        })):
            await self.bot._handle_message({"from":{"id":6883349141},"chat":{"id":6883349141,"type":"private"},
                "text":"/service-operation-finalize op-finalize-bot | verified target"})
        self.assertEqual(self.bot.data.get_service_change_operation("op-finalize-bot")["status"], "completed")
        events = self.bot.data.list_service_change_events("op-finalize-bot")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["actor_telegram_id"], 6883349141)
        self.assertEqual(events[0]["reason"], "verified target")
        self.assertIn("ثبت شد", self.bot.api.send_text.await_args.args[1])

    async def test_non_admin_cannot_finalize_service_operation(self):
        self.bot.data.ensure_customer(777, first_name="Customer")
        with patch.object(CustomerServiceChangeService, "finalize_target_present", new=AsyncMock()) as finalize:
            await self.bot._handle_message({"from":{"id":777},"chat":{"id":777,"type":"private"},
                "text":"/service-operation-finalize arbitrary-id | try"})
        finalize.assert_not_awaited()
        self.assertEqual(self.bot.data.list_service_change_events("arbitrary-id"), [])

    async def test_inventory_purchase_confirmation_describes_automatic_delivery(self):
        self.bot.data.ensure_customer(999, first_name="Buyer")
        self.bot.data.put_manual_product(product_id="inventory-copy", title="Inventory item", price_minor=500,
            currency="IRR", delivery_mode="inventory")
        await self.bot._handle_callback({"id":"inventory-copy-callback", "from":{"id":999},
            "message":{"message_id":450,"chat":{"id":999,"type":"private"}},
            "data":"customer:manualbuy:inventory-copy"})
        rendered = self.bot.api.send_text.await_args.args[1]
        self.assertIn("خودکار", rendered)
        self.assertNotIn("دستی توسط پشتیبانی", rendered)

    async def test_manual_delivery_notification_failure_is_recoverable_by_purchase_replay(self):
        self.bot.data.ensure_customer(999, first_name="Buyer")
        self.bot.data.post_wallet_entry(telegram_id=999, idempotency_key="manual-notify-fund",
                                        amount_minor=3000, entry_type="deposit")
        self.bot.data.put_manual_product(product_id="notify-recovery", title="Recovery item", price_minor=700,
            currency="IRR", delivery_mode="manual")
        callback = {"id":"notify-recovery-callback", "from":{"id":999},
            "message":{"message_id":451,"chat":{"id":999,"type":"private"}},
            "data":"customer:manualbuy_confirm:notify-recovery"}
        await self.bot._handle_callback(callback)
        order = self.bot.data.list_manual_orders(status="pending_delivery")[0]

        async def fail_customer_delivery(chat_id, body, *args, **kwargs):
            if chat_id == 999 and "RECOVERABLE-DELIVERY" in body:
                raise RuntimeError("simulated Telegram send failure")

        self.bot.api.send_text.side_effect = fail_customer_delivery
        await self.bot._handle_message({"from":{"id":6883349141},
            "chat":{"id":6883349141,"type":"private"},
            "text":f"/manual-fulfill {order['order_id']} | RECOVERABLE-DELIVERY"})
        persisted = self.bot.data.list_manual_orders(status="fulfilled")
        self.assertEqual(len(persisted), 1)
        self.assertEqual(persisted[0]["order_id"], order["order_id"])
        self.assertEqual(self.bot.data.wallet_balance(999), 2300)
        admin_notice = next(call.args[1] for call in self.bot.api.send_text.await_args_list
                            if call.args[0] == 6883349141)
        self.assertIn("صف پایدار", admin_notice)
        self.assertIn("دوباره تلاش", admin_notice)
        self.assertIn(order["order_id"], admin_notice)

        self.bot.api.send_text.side_effect = None
        self.bot.api.send_text.reset_mock()
        await self.bot._handle_callback(callback)
        recovered = self.bot.api.send_text.await_args.args[1]
        self.assertIn("RECOVERABLE-DELIVERY", recovered)
        self.assertIn(order["order_id"], recovered)
        self.assertEqual(self.bot.data.wallet_balance(999), 2300)

    async def test_manual_delivery_outbox_retries_after_send_failure_without_redebit(self):
        self.bot.data.ensure_customer(999, first_name="Buyer")
        self.bot.data.post_wallet_entry(telegram_id=999, idempotency_key="durable-outbox-fund",
                                        amount_minor=3000, entry_type="deposit")
        self.bot.data.put_manual_product(product_id="durable-outbox", title="Durable outbox", price_minor=700,
                                         delivery_mode="manual")
        order = self.bot.data.buy_manual_product(telegram_id=999, product_id="durable-outbox",
                                                 idempotency_key="durable-outbox-order")
        self.bot.data.fulfill_manual_order(order_id=order["order_id"], delivery_payload="RETRY-DELIVERY-ONCE",
                                           fulfilled_by=6883349141)
        async def fail_once(chat_id, body, *args, **kwargs):
            if chat_id == 999 and "RETRY-DELIVERY-ONCE" in body:
                raise RuntimeError("simulated failure")
        self.bot.api.send_text.side_effect = fail_once
        now = time.time() + 10
        await self.bot._deliver_manual_delivery_notifications(now=now)
        self.assertEqual(self.bot.data.wallet_balance(999), 2300)
        self.assertEqual(self.bot.data.claim_manual_delivery_notifications(now=now + 1), [])
        self.bot.api.send_text.side_effect = None
        self.bot.api.send_text.reset_mock()
        await self.bot._deliver_manual_delivery_notifications(now=now + 2)
        self.assertTrue(any(call.args[0] == 999 and "RETRY-DELIVERY-ONCE" in call.args[1]
                            for call in self.bot.api.send_text.await_args_list))
        self.assertEqual(self.bot.data.wallet_balance(999), 2300)
        self.assertEqual(self.bot.data.claim_manual_delivery_notifications(now=now + 5000), [])

    async def test_admin_can_requeue_dead_manual_delivery_notification(self):
        self.bot.data.ensure_customer(999, first_name="Buyer")
        self.bot.data.post_wallet_entry(telegram_id=999, idempotency_key="dead-requeue-fund",
                                        amount_minor=3000, entry_type="deposit")
        self.bot.data.put_manual_product(product_id="dead-requeue", title="Dead requeue", price_minor=700,
                                         delivery_mode="manual")
        order = self.bot.data.buy_manual_product(telegram_id=999, product_id="dead-requeue",
                                                 idempotency_key="dead-requeue-order")
        self.bot.data.fulfill_manual_order(order_id=order["order_id"], delivery_payload="DEAD-REQUEUE-DELIVERY",
                                           fulfilled_by=6883349141)
        item = self.bot.data.claim_manual_delivery_notifications(now=time.time() + 10)[0]
        self.bot.data.retry_manual_delivery_notification(item["notification_id"], attempt=item["attempts"],
            error_code="telegram_error", now=time.time() + 10, max_attempts=1)
        await self.bot._handle_message({"from":{"id":6883349141},"chat":{"id":6883349141,"type":"private"},
            "text":f"/manual-notification-retry {order['order_id']}"})
        state = self.bot.data.get_manual_delivery_notification(order["order_id"])
        self.assertEqual(state["status"], "sent")
        self.assertTrue(any(call.args[0] == 999 and "DEAD-REQUEUE-DELIVERY" in call.args[1]
                            for call in self.bot.api.send_text.await_args_list))
        self.assertEqual(self.bot.data.wallet_balance(999), 2300)

    async def test_admin_manual_order_list_and_fulfillment_notify_customer(self):
        self.bot.data.ensure_customer(999, first_name="Buyer")
        self.bot.data.post_wallet_entry(telegram_id=999, idempotency_key="manual-staff-fund",
                                        amount_minor=4000, entry_type="deposit")
        self.bot.data.put_manual_product(product_id="manual-plan", title="Manual plan", price_minor=1500,
            currency="IRR", delivery_mode="manual", description="fulfilled by staff")
        order = self.bot.data.buy_manual_product(telegram_id=999, product_id="manual-plan",
                                                 idempotency_key="manual-staff-order")
        await self.bot._handle_message({"from":{"id":6883349141},
            "chat":{"id":6883349141,"type":"private"},"text":"/manual-orders"})
        self.assertIn(order["order_id"], self.bot.api.send_text.await_args.args[1])
        self.bot.api.send_text.reset_mock()
        await self.bot._handle_message({"from":{"id":6883349141},
            "chat":{"id":6883349141,"type":"private"},
            "text":f"/manual-fulfill {order['order_id']} | activation-code-123"})
        self.assertEqual(self.bot.data.list_manual_orders(), [])
        self.assertTrue(any(call.args[0] == 999 and "activation-code-123" in call.args[1]
                            for call in self.bot.api.send_text.await_args_list))
        self.assertTrue(any(call.args[0] == 6883349141 and "وضعیت fulfilled" in call.args[1]
                            for call in self.bot.api.send_text.await_args_list))

    async def test_non_admin_cannot_access_manual_order_management(self):
        await self.bot._handle_message({"from":{"id":999},
            "chat":{"id":999,"type":"private"},"text":"/manual-orders"})
        self.assertNotIn("📦 سفارش‌های محصولات دیجیتال", self.bot.api.send_text.await_args.args[1])


    async def test_admin_can_manage_discount_codes_and_customer_cannot(self):
        await self.bot._handle_message({
            "from": {"id": 6883349141},
            "chat": {"id": 6883349141, "type": "private"},
            "text": "/discount add SAVE20 | 20 | 3 | 0 | ",
        })
        code=self.bot.data.get_discount_code("save20")
        self.assertIsNotNone(code)
        self.assertEqual(code["percentage"],20)
        self.assertEqual(code["max_uses"],3)
        self.assertTrue(code["enabled"])
        await self.bot._handle_message({
            "from": {"id": 6883349141},
            "chat": {"id": 6883349141, "type": "private"},
            "text": "/discount enable SAVE20 off",
        })
        self.assertFalse(self.bot.data.get_discount_code("SAVE20")["enabled"])
        await self.bot._handle_message({
            "from": {"id": 999},
            "chat": {"id": 999, "type": "private"},
            "text": "/discount add HACK20 | 20 | 0 | 0 | ",
        })
        self.assertIsNone(self.bot.data.get_discount_code("HACK20"))

    async def test_customer_can_preview_discount_and_confirm_purchase_with_code(self):
        self.bot.data.register_panel_connection(panel_id="p1",label="Fake",provider="pasarguard",
            base_url="https://panel.example",auth_mode="api_key",secret_ref="env:PANEL_KEY",enabled=True)
        self.bot.data.put_product(product_id="basic",title="Basic",price_minor=2500,panel_id="p1",
            traffic_bytes=1024**3,duration_hours=24,group_ids=["1"])
        self.bot.data.put_discount_code(code="SAVE20",percentage=20,max_uses=2,
            allowed_product_ids=["basic"])
        await self.bot._handle_callback({
            "id":"cb-discount-start","from":{"id":999},
            "message":{"chat":{"id":999,"type":"private"},"message_id":10},
            "data":"customer:buy_discount:basic",
        })
        await self.bot._handle_message({
            "from":{"id":999},"chat":{"id":999,"type":"private"},"text":"save20",
        })
        quote=self.bot.api.send_text.await_args.args[1]
        self.assertIn("2,000",quote)
        keyboard=self.bot.api.send_text.await_args.args[2]
        self.assertEqual(keyboard["inline_keyboard"][0][0]["callback_data"],
            "customer:buy_discount_confirm:basic")
        self.bot.purchase_service.buy_from_wallet=AsyncMock(return_value=PurchaseOutcome(
            order_id="order123",service_id="order123",status="active",amount_minor=2000,
            currency="IRR",username="u999_order123"))
        self.bot._emit_trust_event=AsyncMock()
        await self.bot._handle_callback({
            "id":"cb-discount-confirm","from":{"id":999},
            "message":{"chat":{"id":999,"type":"private"},"message_id":22},
            "data":"customer:buy_discount_confirm:basic",
        })
        self.bot.purchase_service.buy_from_wallet.assert_awaited_once()
        self.assertEqual(self.bot.purchase_service.buy_from_wallet.await_args.kwargs["discount_code"],"SAVE20")
        self.assertIn(":discount:SAVE20",self.bot.purchase_service.buy_from_wallet.await_args.kwargs["idempotency_key"])
        self.assertNotIn(999,self.bot._pending_purchase_discount)


if __name__ == "__main__":
    unittest.main()
