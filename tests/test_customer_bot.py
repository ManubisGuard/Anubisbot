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

    async def test_customer_wallet_exposes_manual_deposit_and_private_transaction_history(self):
        self.bot.data.ensure_customer(999, username="wallet_owner", first_name="Wallet")
        self.bot.data.ensure_customer(1000, username="other_wallet", first_name="Other")
        self.bot.data.set_manual_deposit_details(
            card_number="4111-1111-1111-1111", account_holder="Wallet Test", bank_name="Test Bank")
        self.bot.data.post_wallet_entry(telegram_id=999, idempotency_key="wallet-history-own-1",
            amount_minor=4321, currency="IRR", entry_type="deposit", note="PRIVATE_INTERNAL_NOTE")
        self.bot.data.post_wallet_entry(telegram_id=999, idempotency_key="wallet-history-own-2",
            amount_minor=222, currency="IRR", entry_type="deposit")
        self.bot.data.post_wallet_entry(telegram_id=1000, idempotency_key="wallet-history-other",
            amount_minor=987654, currency="IRR", entry_type="deposit")

        query = {"from": {"id": 999}, "message": {"message_id": 17,
            "chat": {"id": 999, "type": "private"}}}
        await self.bot._handle_callback({**query, "id": "wallet-home", "data": "customer:wallet"})
        wallet_text = self.bot.api.send_text.await_args.args[1]
        wallet_keyboard = self.bot.api.send_text.await_args.args[2]["inline_keyboard"]
        wallet_callbacks = {b.get("callback_data") for row in wallet_keyboard for b in row}
        self.assertIn("customer:wallet:deposit", wallet_callbacks)
        self.assertIn("customer:wallet:history", wallet_callbacks)
        self.assertIn("موجودی کیف پول", wallet_text)

        self.bot.api.send_text.reset_mock()
        await self.bot._handle_callback({**query, "id": "wallet-deposit", "data": "customer:wallet:deposit"})
        deposit_text = self.bot.api.send_text.await_args.args[1]
        self.assertIn("4111111111111111", deposit_text)
        self.assertIn("/deposit 250000 IRR", deposit_text)
        self.assertIn("فقط بعد از بررسی مدیر", deposit_text)

        self.bot.api.send_text.reset_mock()
        await self.bot._handle_callback({**query, "id": "wallet-history", "data": "customer:wallet:history"})
        history_text = self.bot.api.send_text.await_args.args[1]
        self.assertIn("4,321 IRR", history_text)
        self.assertIn("222 IRR", history_text)
        self.assertNotIn("987,654", history_text)
        self.assertNotIn("PRIVATE_INTERNAL_NOTE", history_text)
        history_callbacks = {b.get("callback_data") for row in self.bot.api.send_text.await_args.args[2]["inline_keyboard"] for b in row}
        self.assertIn("customer:wallet:deposit", history_callbacks)
        self.assertIn("customer:home", history_callbacks)

    async def test_customer_wallet_deposit_hides_card_details_when_not_configured(self):
        query = {"id": "wallet-no-card", "from": {"id": 999},
            "message": {"message_id": 18, "chat": {"id": 999, "type": "private"}},
            "data": "customer:wallet:deposit"}
        await self.bot._handle_callback(query)
        body = self.bot.api.send_text.await_args.args[1]
        self.assertIn("اطلاعات کارت‌به‌کارت توسط مدیر تنظیم نشده", body)
        self.assertNotIn("شماره کارت:", body)
        callbacks = {b.get("callback_data") for row in self.bot.api.send_text.await_args.args[2]["inline_keyboard"] for b in row}
        self.assertIn("customer:support", callbacks)
        self.assertIn("customer:wallet", callbacks)

    async def test_customer_menu_exposes_order_history_and_empty_history_has_safe_navigation(self):
        menu = self.bot._customer_menu(customer_id=1001)
        callbacks = [button.get("callback_data") for row in menu["inline_keyboard"] for button in row]
        self.assertIn("customer:orders:0", callbacks)

        await self.bot._handle_callback({
            "id": "orders-empty", "from": {"id": 1001},
            "message": {"message_id": 7, "chat": {"id": 1001, "type": "private"}},
            "data": "customer:orders:0",
        })
        self.bot.api.send_text.assert_awaited_once()
        args = self.bot.api.send_text.await_args
        self.assertEqual(args.args[0], 1001)
        self.assertIn("سفارش‌های من", args.args[1])
        self.assertIn("هنوز سفارشی", args.args[1])
        callbacks = [button.get("callback_data") for row in args.args[2]["inline_keyboard"] for button in row]
        self.assertIn("customer:shop", callbacks)
        self.assertIn("customer:home", callbacks)

    async def test_customer_profile_shows_own_account_summary_and_navigation(self):
        self.bot.data.ensure_customer(999, username="profile_user", first_name="Profile")
        self.bot.data.upsert_service(service_id="profile-service", telegram_id=999, product_id="prod-a",
            panel_id="pg-a", provider_user_id="provider-profile", status="active")
        self.bot.data.post_wallet_entry(telegram_id=999, idempotency_key="profile-wallet", amount_minor=4321,
            currency="IRR", entry_type="deposit")
        menu = self.bot._customer_menu(customer_id=999)
        callbacks = [button.get("callback_data") for row in menu["inline_keyboard"] for button in row]
        self.assertIn("customer:profile", callbacks)

        await self.bot._handle_callback({
            "id": "profile-summary", "from": {"id": 999, "username": "profile_user",
                "first_name": "Profile", "last_name": "Owner"},
            "message": {"message_id": 9, "chat": {"id": 999, "type": "private"}},
            "data": "customer:profile",
        })
        body = self.bot.api.send_text.await_args.args[1]
        self.assertIn("Profile Owner", body)
        self.assertIn("@profile_user", body)
        self.assertIn("شناسه تلگرام: 999", body)
        self.assertIn("تعداد سرویس‌ها: 1", body)
        self.assertIn("تعداد سفارش‌های اخیر: 0", body)
        self.assertIn("IRR: 4,321", body)
        keyboard = self.bot.api.send_text.await_args.args[2]["inline_keyboard"]
        profile_callbacks = {button.get("callback_data") for row in keyboard for button in row}
        self.assertTrue({"customer:services", "customer:orders:0", "customer:wallet",
                         "customer:support", "customer:home"}.issubset(profile_callbacks))

    async def test_customer_profile_rejects_callbacks_from_another_user_or_group(self):
        forged_queries = (
            {"id": "profile-other-user", "from": {"id": 999},
             "message": {"message_id": 10, "chat": {"id": 1000, "type": "private"}},
             "data": "customer:profile"},
            {"id": "profile-group", "from": {"id": 999},
             "message": {"message_id": 11, "chat": {"id": -100999, "type": "supergroup"}},
             "data": "customer:profile"},
        )
        for query in forged_queries:
            with self.subTest(callback_id=query["id"]):
                self.bot.api.send_text.reset_mock()
                self.bot.api.answer_callback.reset_mock()
                await self.bot._handle_callback(query)
                self.bot.api.send_text.assert_not_awaited()
                self.bot.api.answer_callback.assert_awaited_once_with(
                    query["id"], "برای ادامه از گفت‌وگوی خصوصی استفاده کن"
                )

    async def test_customer_support_menu_opens_new_ticket_and_lists_only_own_tickets(self):
        menu = self.bot._customer_menu(customer_id=1001)
        callbacks = [button.get("callback_data") for row in menu["inline_keyboard"] for button in row]
        self.assertIn("customer:support", callbacks)

        await self.bot._handle_callback({
            "id": "support-home", "from": {"id": 1001},
            "message": {"message_id": 8, "chat": {"id": 1001, "type": "private"}},
            "data": "customer:support",
        })
        keyboard = self.bot.api.send_text.await_args.args[2]["inline_keyboard"]
        callbacks = [button.get("callback_data") for row in keyboard for button in row]
        self.assertIn("customer:support:new", callbacks)
        self.assertIn("customer:support:list:0", callbacks)

        await self.bot._handle_callback({
            "id": "support-new", "from": {"id": 1001},
            "message": {"message_id": 8, "chat": {"id": 1001, "type": "private"}},
            "data": "customer:support:new",
        })
        self.assertIn(1001, self.bot._pending_support)

        self.bot.data.ensure_customer(1001, first_name="Customer one")
        self.bot.data.ensure_customer(2002, first_name="Customer two")
        self.bot.data.create_ticket(ticket_id="own-ticket-1", telegram_id=1001,
                                    subject="مشکل اتصال من", first_message="اتصال قطع است")
        self.bot.data.create_ticket(ticket_id="other-ticket-1", telegram_id=2002,
                                    subject="تیکت کاربر دیگر", first_message="خصوصی")
        await self.bot._handle_callback({
            "id": "support-list", "from": {"id": 1001},
            "message": {"message_id": 8, "chat": {"id": 1001, "type": "private"}},
            "data": "customer:support:list:0",
        })
        rendered = self.bot.api.send_text.await_args.args[1]
        self.assertIn("own-ticket-1", rendered)
        self.assertNotIn("other-ticket-1", rendered)
        self.assertIn("مشکل اتصال من", rendered)

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
        self.assertIn("تولیدکننده‌های لاگ ربات و بک‌آپ هنوز متصل نیستند", help_text)
        self.assertIn("بررسی خواندنی پنل", help_text)
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

    async def test_non_admin_admin_callbacks_queue_redacted_security_alert(self):
        self.bot.data.add_notification_route(event_key="security_alert", chat_id=6883349141,
            created_by=6883349141)
        for callback_id, data in (("forged-admin", "admin:finance"), ("forged-content", "content:buttons")):
            await self.bot._handle_callback({"id": callback_id, "from": {"id": 1001},
                "message": {"message_id": 77, "chat": {"id": 1001, "type": "private"}}, "data": data})
        self.assertEqual(self.bot.api.answer_callback.await_count, 2)
        self.assertIn("فقط برای مدیر", self.bot.api.send_text.await_args.args[1])
        with self.bot.data._connect() as db:
            rows = db.execute("SELECT event_key,idempotency_key,payload_json FROM customer_admin_notifications").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["event_key"], "security_alert")
        self.assertIn("unauthorized-admin-callback:1001:", rows[0]["idempotency_key"])
        payload = json.loads(rows[0]["payload_json"])
        self.assertEqual(payload["reason_code"], "unauthorized_admin_callback")
        self.assertEqual(payload["customer_id"], 1001)
        self.assertNotIn("finance", json.dumps(payload))
        self.assertNotIn("buttons", json.dumps(payload))

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
        self.assertNotIn("📱 ورود به مینی‌اپلیکیشن", labels)
        self.assertNotIn("🧪 تست سرویس‌ها", labels)
        self.assertIn("🛒 خرید سرویس", labels)
        self.assertIn("🏪 خرید پنل نمایندگی", labels)
        self.assertNotIn("🗂 پنل‌های من", labels)
        self.assertIn("💳 کیف پول و شارژ", labels)
        self.assertIn("⚙️ سرویس‌های من", labels)
        self.assertIn("🛡 پنل مدیریت ربات", labels)
        self.assertTrue(keyboard["is_persistent"])
        self.assertEqual(CUSTOMER_MENU["inline_keyboard"][0][0]["style"], "primary")
        self.assertEqual(CUSTOMER_MENU["inline_keyboard"][4][0]["style"], "success")

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
        self.bot.data.add_notification_route(event_key="service_test", chat_id=6883349141,
            created_by=6883349141)
        await self.bot._handle_message({"from":{"id":6883349141},
            "chat":{"id":6883349141,"type":"private"},"text":"/panel-check panel-report"})
        rendered = self.bot.api.send_text.await_args.args[1]
        self.assertIn("اتصال خواندنی موفق بود", rendered)
        self.assertIn("create_user: متد محلی موجود؛ مجوز پنل نامشخص", rendered)
        self.assertIn("manage_resellers: پشتیبانی نمی‌شود", rendered)
        self.assertIn("مجوز عملیات تغییردهنده", rendered)
        adapter.health_check.assert_awaited_once()
        adapter.capability_report.assert_awaited_once()
        with self.bot.data._connect() as db:
            notice = db.execute("SELECT event_key,payload_json FROM customer_admin_notifications ORDER BY notification_id DESC LIMIT 1").fetchone()
        self.assertEqual(notice["event_key"], "service_test")
        payload = json.loads(notice["payload_json"])
        self.assertEqual(payload["status"], "healthy_read_only")
        self.assertEqual(payload["reason_code"], "panel_check_succeeded")
        self.assertNotIn("panel-report", notice["payload_json"])
        self.assertNotIn("https://panel.example", notice["payload_json"])

    async def test_hidden_customer_routes_reject_stale_direct_callbacks(self):
        cases = (
            ("menu_profile_enabled", "customer:profile"),
            ("menu_orders_enabled", "customer:orders:0"),
            ("menu_services_enabled", "customer:services:0"),
            ("menu_support_enabled", "customer:support:list:0"),
            ("menu_shop_enabled", "customer:shop"),
            ("menu_shop_enabled", "customer:buy_confirm:product-1"),
            ("menu_shop_enabled", "customer:manualbuy:product-1~variant-1"),
            ("menu_shop_enabled", "customer:manualbuy_confirm:product-1"),
            ("menu_shop_enabled", "customer:mv:product-1~variant-1:quote"),
            ("menu_shop_enabled", "customer:buy_discount_confirm:product-1"),
            ("menu_services_enabled", "customer:service_detail:service-1"),
            ("menu_services_enabled", "customer:renew_confirm:service-1"),
            ("menu_services_enabled", "customer:topup_execute:service-1"),
            ("menu_resellers_enabled", "customer:reseller_buy_confirm:plan-1"),
            ("menu_myresellers_enabled", "customer:rshop:account-1"),
            ("menu_myresellers_enabled", "customer:r:account-1:product-1"),
        )
        for index, (setting, callback_data) in enumerate(cases):
            with self.subTest(setting=setting):
                self.bot.content.set(setting, "off")
                self.bot.api.send_text.reset_mock()
                await self.bot._handle_callback({
                    "id": f"hidden-route-{index}",
                    "from": {"id": 999, "first_name": "Private"},
                    "message": {"chat": {"id": 999, "type": "private"}},
                    "data": callback_data,
                })
                self.assertIn("غیرفعال است", self.bot.api.send_text.await_args.args[1])
                self.assertNotIn("Private", self.bot.api.send_text.await_args.args[1])

    async def test_customer_menu_uses_only_valid_https_webapp_url(self):
        with patch.dict(os.environ, {"MANUBIS_CUSTOMER_WEBAPP_URL":"https://mini.example/customer/"}):
            keyboard = self.bot._customer_menu()
        button = keyboard["inline_keyboard"][0][0]
        self.assertEqual(button["web_app"]["url"], "https://mini.example/customer/")
        self.assertNotIn("callback_data", button)
        with patch.dict(os.environ, {"MANUBIS_CUSTOMER_WEBAPP_URL":"http://mini.example/customer/"}):
            unavailable = self.bot._customer_menu()
        buttons = [button for row in unavailable["inline_keyboard"] for button in row]
        self.assertFalse(any(button.get("callback_data") == "customer:miniapp" for button in buttons))
        self.assertFalse(any("web_app" in button for button in buttons))

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

    async def test_customer_services_are_paginated_with_compact_keyboard(self):
        self.bot.data.ensure_customer(999, first_name="Service owner")
        for index in range(5):
            service_id = f"svc-page-{index}"
            self.bot.data.upsert_service(service_id=service_id, telegram_id=999, product_id="prod-a",
                panel_id="pg-a", provider_user_id=f"provider-{index}", status="active",
                traffic_limit_bytes=10 * 1024**3, traffic_used_bytes=index * 1024**3)
            with self.bot.data._connect() as db:
                db.execute("UPDATE customer_services SET username=?,pricing_mode=? WHERE tenant_id=? AND service_id=?",
                    (f"user-{index}", "fixed", self.bot.data.tenant_id, service_id))

        await self.bot._handle_callback({"id": "services-page-0", "from": {"id": 999},
            "message": {"chat": {"id": 999, "type": "private"}}, "data": "customer:services"})
        body = self.bot.api.send_text.await_args.args[1]
        keyboard = self.bot.api.send_text.await_args.args[2]["inline_keyboard"]
        callbacks = [button.get("callback_data") for row in keyboard for button in row]
        self.assertIn("صفحه 1 از 5", body)
        self.assertIn("customer:services:1", callbacks)
        self.assertLessEqual(sum(len(row) for row in keyboard), 8)
        self.assertEqual(sum(1 for callback in callbacks if callback and callback.startswith("customer:service_detail:")), 1)

        self.bot.api.send_text.reset_mock()
        await self.bot._handle_callback({"id": "services-page-1", "from": {"id": 999},
            "message": {"chat": {"id": 999, "type": "private"}}, "data": "customer:services:1"})
        body = self.bot.api.send_text.await_args.args[1]
        keyboard = self.bot.api.send_text.await_args.args[2]["inline_keyboard"]
        callbacks = [button.get("callback_data") for row in keyboard for button in row]
        self.assertIn("صفحه 2 از 5", body)
        self.assertIn("customer:services:0", callbacks)
        self.assertIn("customer:services:2", callbacks)
        self.assertLessEqual(sum(len(row) for row in keyboard), 8)

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
        await self.bot._handle_message({**admin,"text":"/manual-product add ai-manual | AI Gift | 300 | IRR | instant | PRIVATE-DELIVERY | Manual AI product | ai"})
        self.assertEqual(self.bot.data.get_product("ai-plan")["category_id"], "ai")
        self.assertEqual(self.bot.data.get_manual_product("ai-manual")["category_id"], "ai")
        self.bot.api.send_text.reset_mock()
        await self.bot._handle_callback({"id":"shop","from":{"id":999},
            "message":{"chat":{"id":999,"type":"private"}},"data":"customer:shop"})
        root_body = self.bot.api.send_text.await_args.args[1]
        self.assertIn("AI Gift", root_body)
        self.assertIn("توضیحات: Manual AI product", root_body)
        keyboard=self.bot.api.send_text.await_args.args[2]
        callbacks={b.get("callback_data") for row in keyboard["inline_keyboard"] for b in row}
        self.assertIn("customer:category:ai",callbacks)
        self.bot.api.send_text.reset_mock()
        await self.bot._handle_callback({"id":"cat","from":{"id":999},
            "message":{"chat":{"id":999,"type":"private"}},"data":"customer:category:ai"})
        body=self.bot.api.send_text.await_args.args[1]
        self.assertIn("AI Plan",body)
        self.assertIn("AI Gift",body)
        self.assertNotIn("Basic Plan",body)
        self.assertNotIn("PRIVATE-DELIVERY",body)

    async def test_manual_product_image_is_sent_as_photo_card_and_edit_preserves_it(self):
        admin = {"from":{"id":6883349141},"chat":{"id":6883349141,"type":"private"}}
        await self.bot._handle_message({**admin, "text":
            "/manual-product add image-item | Image Item | 500 | IRR | manual |  | Image description |  | 0 | 0 | Terms | https://cdn.example.com/products/image.png"})
        product = self.bot.data.get_manual_product("image-item")
        self.assertEqual(product["image_url"], "https://cdn.example.com/products/image.png")
        await self.bot._handle_message({**admin, "text":
            "/manual-product edit image-item | Edited Image | 600 | IRR | manual |  | Updated description"})
        self.assertEqual(self.bot.data.get_manual_product("image-item")["image_url"],
                         "https://cdn.example.com/products/image.png")
        self.bot.api.call = AsyncMock()
        self.bot.api.send_text.reset_mock()
        await self.bot._handle_callback({"id":"image-shop", "from":{"id":999},
            "message":{"chat":{"id":999,"type":"private"}}, "data":"customer:shop"})
        self.bot.api.call.assert_awaited()
        method, payload = self.bot.api.call.await_args.args
        self.assertEqual(method, "sendPhoto")
        self.assertEqual(payload["photo"], "https://cdn.example.com/products/image.png")
        self.assertIn("Edited Image", payload["caption"])
        self.assertEqual(payload["reply_markup"]["inline_keyboard"][0][0]["callback_data"],
                         "customer:manualbuy:image-item")
        self.assertNotIn("delivery_payload", payload["caption"])
        self.assertTrue(self.bot.api.send_text.await_args.args[1].startswith("🛍 فروشگاه"))

    async def test_manual_product_scheduled_visibility_can_be_set_and_hidden_from_shop(self):
        admin = {"from":{"id":6883349141},"chat":{"id":6883349141,"type":"private"}}
        await self.bot._handle_message({**admin, "text":
            "/manual-product add future-item | Future Item | 500 | IRR | manual |  | Scheduled description |  | 0 | 0 |  |  | 2099-01-01T00:00:00Z | 2100-01-01T00:00:00Z"})
        product = self.bot.data.get_manual_product("future-item", enabled_only=False)
        self.assertEqual(product["visible_from_utc"], "2099-01-01T00:00:00Z")
        self.assertEqual(product["visible_until_utc"], "2100-01-01T00:00:00Z")
        await self.bot._handle_message({**admin, "text": "/manual-product edit future-item | Future Item | 500 | IRR | manual |  | Updated description"})
        self.assertEqual(self.bot.data.get_manual_product("future-item", enabled_only=False)["visible_from_utc"],
                         "2099-01-01T00:00:00Z")
        await self.bot._handle_callback({"id":"scheduled-shop", "from":{"id":999},
            "message":{"chat":{"id":999,"type":"private"}}, "data":"customer:shop"})
        body = self.bot.api.send_text.await_args.args[1]
        self.assertNotIn("Future Item", body)
        self.assertIsNone(self.bot.data.get_manual_product("future-item"))

    async def test_manual_product_image_send_failure_falls_back_to_text_listing(self):
        self.bot.data.put_manual_product(product_id="image-fallback", title="Fallback image", price_minor=500,
            delivery_mode="manual", description="Fallback description",
            image_url="https://cdn.example.com/products/fallback.png")
        self.bot.api.call = AsyncMock(side_effect=RuntimeError("telegram unavailable"))
        await self.bot._handle_callback({"id":"image-fallback", "from":{"id":999},
            "message":{"chat":{"id":999,"type":"private"}}, "data":"customer:shop"})
        body = self.bot.api.send_text.await_args.args[1]
        self.assertIn("Fallback image", body)
        self.assertIn("Fallback description", body)
        keyboard = self.bot.api.send_text.await_args.args[2]
        callbacks = {button.get("callback_data") for row in keyboard["inline_keyboard"] for button in row}
        self.assertIn("customer:manualbuy:image-fallback", callbacks)

    async def test_manual_product_terms_are_shown_and_callback_binds_accepted_version(self):
        self.bot.data.ensure_customer(999, first_name="Buyer")
        self.bot.data.post_wallet_entry(telegram_id=999, idempotency_key="terms-bot-wallet", amount_minor=1000, entry_type="deposit")
        self.bot.data.put_manual_product(product_id="terms-bot", title="Terms bot item", price_minor=500,
            currency="IRR", delivery_mode="instant", delivery_payload="SAFE-DELIVERY", terms_text="شرایط نسخه یک")
        await self.bot._handle_callback({"id":"terms-show", "from":{"id":999},
            "message":{"message_id":700,"chat":{"id":999,"type":"private"}},
            "data":"customer:manualbuy:terms-bot"})
        body = self.bot.api.send_text.await_args.args[1]
        keyboard = self.bot.api.send_text.await_args.args[2]
        self.assertIn("شرایط خرید", body)
        self.assertIn("شرایط نسخه یک", body)
        confirm = keyboard["inline_keyboard"][0][0]
        self.assertEqual(confirm["text"], "می‌پذیرم و خرید")
        self.assertTrue(confirm["callback_data"].startswith("customer:mp:terms-bot:"))
        self.bot.data.put_manual_product(product_id="terms-bot", title="Terms bot item", price_minor=500,
            currency="IRR", delivery_mode="instant", delivery_payload="SAFE-DELIVERY", terms_text="شرایط نسخه دو")
        self.bot.api.send_text.reset_mock()
        await self.bot._handle_callback({"id":"terms-stale", "from":{"id":999},
            "message":{"message_id":701,"chat":{"id":999,"type":"private"}},
            "data":confirm["callback_data"]})
        body = self.bot.api.send_text.await_args.args[1]
        self.assertIn("تغییر کرده است", body)
        self.assertEqual(self.bot.data.wallet_balance(999, "IRR"), 1000)

    async def test_manual_product_confirmation_shows_bounded_description_not_delivery_payload(self):
        self.bot.data.ensure_customer(999, first_name="Buyer")
        self.bot.data.put_manual_product(product_id="described-item", title="Described item", price_minor=500,
            currency="IRR", delivery_mode="instant", delivery_payload="DO-NOT-LEAK-DELIVERY",
            description="Customer-facing description")
        await self.bot._handle_callback({"id":"desc-confirm","from":{"id":999},
            "message":{"message_id":41,"chat":{"id":999,"type":"private"}},
            "data":"customer:manualbuy:described-item"})
        body=self.bot.api.send_text.await_args.args[1]
        self.assertIn("توضیحات: Customer-facing description", body)
        self.assertNotIn("DO-NOT-LEAK-DELIVERY", body)

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

    async def test_manual_inventory_out_of_stock_is_visible_and_purchase_button_returns_after_restock(self):
        self.bot.data.put_manual_product(product_id="visible-stock", title="Limited license", price_minor=500,
            currency="IRR", delivery_mode="inventory")
        await self.bot._handle_callback({"id":"stock-shop-empty", "from":{"id":999},
            "message":{"chat":{"id":999,"type":"private"}}, "data":"customer:shop"})
        body = self.bot.api.send_text.await_args.args[1]
        keyboard = self.bot.api.send_text.await_args.args[2]
        callbacks = {b.get("callback_data") for row in keyboard["inline_keyboard"] for b in row}
        self.assertIn("Limited license", body)
        self.assertIn("ناموجود", body)
        self.assertNotIn("customer:manualbuy:visible-stock", callbacks)

        self.bot.data.add_manual_inventory_key(product_id="visible-stock", secret_payload="RESTOCKED-KEY")
        self.bot.api.send_text.reset_mock()
        await self.bot._handle_callback({"id":"stock-shop-restocked", "from":{"id":999},
            "message":{"chat":{"id":999,"type":"private"}}, "data":"customer:shop"})
        body = self.bot.api.send_text.await_args.args[1]
        keyboard = self.bot.api.send_text.await_args.args[2]
        callbacks = {b.get("callback_data") for row in keyboard["inline_keyboard"] for b in row}
        self.assertIn("موجود", body)
        self.assertIn("customer:manualbuy:visible-stock", callbacks)

    async def test_manual_inventory_confirmation_refuses_when_stock_is_empty(self):
        self.bot.data.ensure_customer(999, first_name="Buyer")
        self.bot.data.put_manual_product(product_id="empty-confirm", title="Empty item", price_minor=500,
            currency="IRR", delivery_mode="inventory")
        await self.bot._handle_callback({"id":"stock-confirm-empty", "from":{"id":999},
            "message":{"message_id":452,"chat":{"id":999,"type":"private"}},
            "data":"customer:manualbuy:empty-confirm"})
        body = self.bot.api.send_text.await_args.args[1]
        self.assertIn("ناموجود", body)
        self.assertNotIn("تأیید خرید", body)

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
        self.assertIn("content:toggle:menu_profile_enabled",callbacks)
        self.assertIn("content:toggle:menu_orders_enabled",callbacks)
        self.bot.api.send_text.reset_mock()
        await self.bot._handle_callback({"id":"buttons-toggle","from":{"id":6883349141},
            "message":{"chat":{"id":6883349141,"type":"private"}},"data":"content:toggle:menu_shop_enabled"})
        self.assertEqual(self.bot.content.get("menu_shop_enabled"),"off")
        self.bot.api.send_text.reset_mock()
        await self.bot._handle_message({"from":{"id":999},"chat":{"id":999,"type":"private"},"text":"/start"})
        labels={b.get("text") for row in self.bot.api.send_text.await_args.args[2]["keyboard"] for b in row}
        self.assertNotIn("🛒 خرید سرویس",labels)
        for key in ("menu_profile_enabled", "menu_orders_enabled"):
            self.bot.api.send_text.reset_mock()
            await self.bot._handle_callback({"id":f"toggle-{key}","from":{"id":6883349141},
                "message":{"chat":{"id":6883349141,"type":"private"}},
                "data":f"content:toggle:{key}"})
            self.assertEqual(self.bot.content.get(key), "off")
        menu_callbacks = {
            button.get("callback_data")
            for row in self.bot._customer_menu(customer_id=999)["inline_keyboard"]
            for button in row
        }
        self.assertNotIn("customer:profile", menu_callbacks)
        self.assertNotIn("customer:orders:0", menu_callbacks)

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

    async def test_brand_reply_keyboard_preserves_secure_webapp_button(self):
        with patch.dict(os.environ, {"MANUBIS_CUSTOMER_WEBAPP_URL": "https://mini.example/customer/"}):
            keyboard = self.bot._customer_menu(customer_id=6883349141)
        await self.bot._send_brand_message(6883349141, keyboard)
        markup = self.bot.api.send_text.await_args.args[2]
        webapp_buttons = [
            button for row in markup["keyboard"] for button in row if "web_app" in button
        ]
        self.assertEqual(len(webapp_buttons), 1)
        self.assertEqual(webapp_buttons[0]["web_app"]["url"], "https://mini.example/customer/")
        self.assertNotIn("callback_data", webapp_buttons[0])

    async def test_brand_message_inline_mode_uses_supplied_keyboard_without_rebuilding(self):
        self.bot.content.set("home_keyboard_mode", "inline")
        supplied = {"inline_keyboard": [[{"text": "🧪 تست", "callback_data": "customer:shop"}]]}
        await self.bot._send_brand_message(6883349141, supplied)
        self.assertEqual(self.bot.api.send_text.await_args.args[2], supplied)

    async def test_admin_keyboard_mode_toggle_persists_supported_setting(self):
        query = {"id": "keyboard-mode-1", "from": {"id": 6883349141},
                 "message": {"chat": {"id": 6883349141, "type": "private"}},
                 "data": "admin:keyboard-mode"}
        await self.bot._handle_callback(query)
        self.assertEqual(self.bot.content.get("home_keyboard_mode"), "inline")
        self.bot.api.send_text.reset_mock()
        query["id"] = "keyboard-mode-2"
        await self.bot._handle_callback(query)
        self.assertEqual(self.bot.content.get("home_keyboard_mode"), "reply")
        self.assertIn("حالت منوی اصلی ذخیره شد", self.bot.api.send_text.await_args.args[1])

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

    async def test_admin_can_edit_manual_product_without_rewriting_existing_order_snapshot(self):
        self.bot.data.ensure_customer(999, first_name="Buyer")
        self.bot.data.post_wallet_entry(telegram_id=999, idempotency_key="manual-edit-fund",
                                        amount_minor=5000, entry_type="deposit")
        self.bot.data.put_manual_product(product_id="editable-item", title="Original title", price_minor=1000,
            currency="IRR", delivery_mode="instant", delivery_payload="ORIGINAL-DELIVERY", description="old description",
            max_purchases_per_customer=2)
        original = self.bot.data.buy_manual_product(telegram_id=999, product_id="editable-item",
                                                    idempotency_key="manual-edit-order-before")
        await self.bot._handle_message({"from":{"id":6883349141},"chat":{"id":6883349141,"type":"private"},
            "text":"/manual-product edit editable-item | Updated title | 1200 | IRR | instant | UPDATED-DELIVERY | updated description"})
        updated = self.bot.data.get_manual_product("editable-item")
        self.assertEqual(updated["title"], "Updated title")
        self.assertEqual(updated["price_minor"], 1200)
        self.assertEqual(updated["delivery_payload"], "UPDATED-DELIVERY")
        self.assertEqual(updated["max_purchases_per_customer"], 2)
        self.assertEqual(self.bot.data.list_manual_orders(status="all")[0]["title"], "Original title")
        self.assertEqual(self.bot.data.buy_manual_product(telegram_id=999, product_id="editable-item",
            idempotency_key="manual-edit-order-before")["delivery_payload"], original["delivery_payload"])

    async def test_admin_can_set_manual_product_purchase_cap(self):
        await self.bot._handle_message({"from":{"id":6883349141},"chat":{"id":6883349141,"type":"private"},
            "text":"/manual-product add limited-command | Limited command | 1000 | IRR | manual |  | description |  | 2"})
        product = self.bot.data.get_manual_product("limited-command")
        self.assertEqual(product["max_purchases_per_customer"], 2)
        self.bot.api.send_text.reset_mock()
        await self.bot._handle_message({"from":{"id":6883349141},"chat":{"id":6883349141,"type":"private"},
            "text":"/manual-product list"})
        self.assertIn("سقف خرید: 2", self.bot.api.send_text.await_args.args[1])

    async def test_admin_can_set_manual_product_sort_order_and_edit_preserves_it(self):
        await self.bot._handle_message({"from":{"id":6883349141},"chat":{"id":6883349141,"type":"private"},
            "text":"/manual-product add sort-late | Late | 1000 | IRR | manual |  | description |  | 0 | 30"})
        await self.bot._handle_message({"from":{"id":6883349141},"chat":{"id":6883349141,"type":"private"},
            "text":"/manual-product add sort-first | First | 1000 | IRR | manual |  | description |  | 0 | -5"})
        self.assertEqual([p["product_id"] for p in self.bot.data.list_manual_products()],
                         ["sort-first", "sort-late"])
        await self.bot._handle_message({"from":{"id":6883349141},"chat":{"id":6883349141,"type":"private"},
            "text":"/manual-product edit sort-late | Late updated | 1200 | IRR | manual |  | updated description"})
        self.assertEqual(self.bot.data.get_manual_product("sort-late")["sort_order"], 30)
        self.bot.api.send_text.reset_mock()
        await self.bot._handle_message({"from":{"id":6883349141},"chat":{"id":6883349141,"type":"private"},
            "text":"/manual-product list"})
        self.assertIn("ترتیب: 30", self.bot.api.send_text.await_args.args[1])

    async def test_admin_can_archive_manual_product_without_new_purchase_or_wallet_debit(self):
        self.bot.data.ensure_customer(999, first_name="Buyer")
        self.bot.data.post_wallet_entry(telegram_id=999, idempotency_key="manual-archive-fund",
                                        amount_minor=3000, entry_type="deposit")
        self.bot.data.put_manual_product(product_id="archived-item", title="Archive me", price_minor=500,
            currency="IRR", delivery_mode="instant", delivery_payload="ARCHIVE-DELIVERY",
            max_purchases_per_customer=1)
        self.bot.data.buy_manual_product(telegram_id=999, product_id="archived-item",
                                         idempotency_key="manual-archive-order-before")
        balance_after_original_order = self.bot.data.wallet_balance(999)
        await self.bot._handle_message({"from":{"id":6883349141},"chat":{"id":6883349141,"type":"private"},
            "text":"/manual-product archive archived-item"})
        archived = self.bot.data.get_manual_product("archived-item", enabled_only=False)
        self.assertEqual(archived["enabled"], 0)
        self.assertEqual(archived["max_purchases_per_customer"], 1)
        self.assertNotIn("archived-item", {item["product_id"] for item in self.bot.data.list_manual_products()})
        self.bot.api.send_text.reset_mock()
        await self.bot._handle_message({"from":{"id":6883349141},"chat":{"id":6883349141,"type":"private"},
            "text":"/manual-product list"})
        listing = self.bot.api.send_text.await_args.args[1]
        self.assertIn("archived-item", listing)
        self.assertIn("آرشیو", listing)
        self.assertNotIn("ARCHIVE-DELIVERY", listing)
        with self.assertRaisesRegex(ValueError, "unavailable"):
            self.bot.data.buy_manual_product(telegram_id=999, product_id="archived-item",
                                             idempotency_key="manual-archive-order-after")
        self.assertEqual(self.bot.data.wallet_balance(999), balance_after_original_order)
        self.assertEqual(self.bot.data.list_manual_orders(status="all")[0]["title"], "Archive me")

    async def test_manual_product_is_independent_of_panel_and_delivered_after_wallet_purchase(self):
        self.bot.data.ensure_customer(999, first_name="Buyer")
        self.bot.data.post_wallet_entry(telegram_id=999,idempotency_key="manual-fund",amount_minor=5000,entry_type="deposit")
        self.bot.data.put_manual_product(product_id="chatgpt-key",title="ChatGPT Subscription",price_minor=2500,
            currency="IRR",delivery_mode="instant",delivery_payload="TEST-KEY-DO-NOT-USE",description="test item",
            max_purchases_per_customer=1)
        await self.bot._handle_callback({"id":"manual-shop","from":{"id":999},
            "message":{"message_id":10,"chat":{"id":999,"type":"private"}},"data":"customer:shop"})
        text=self.bot.api.send_text.await_args.args[1]
        self.assertIn("ChatGPT Subscription",text)
        self.assertIn("سقف خرید هر مشتری: 1",text)
        self.bot.api.send_text.reset_mock()
        await self.bot._handle_callback({"id":"manual-buy","from":{"id":999},
            "message":{"message_id":11,"chat":{"id":999,"type":"private"}},"data":"customer:manualbuy:chatgpt-key"})
        self.assertIn("تأیید خرید",self.bot.api.send_text.await_args.args[1])
        self.assertIn("سقف خرید هر مشتری: 1",self.bot.api.send_text.await_args.args[1])
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
        self.bot.data.add_manual_inventory_key(product_id="inventory-copy", secret_payload="INVENTORY-COPY-KEY")
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


    async def test_admin_attaches_file_and_customer_receives_replayable_document_without_second_charge(self):
        self.bot.api.call = AsyncMock(return_value={"ok": True})
        admin = {"from":{"id":6883349141},"chat":{"id":6883349141,"type":"private"}}
        self.bot.data.put_manual_product(product_id="file-ui", title="Download pack", price_minor=700,
            currency="IRR", delivery_mode="manual")
        await self.bot._handle_message({**admin, "caption":"/manual-file file-ui",
            "document":{"file_id":"telegram-file-ui-id","file_name":"pack.zip"}})
        self.assertEqual(self.bot.data.get_manual_product("file-ui")["delivery_mode"], "file")
        self.bot.data.put_manual_product(product_id="file-variant-ui", title="Variant download", price_minor=1000,
            currency="IRR", delivery_mode="manual")
        self.bot.data.put_manual_variant(product_id="file-variant-ui", variant_id="plus", title="Plus", price_minor=900)
        await self.bot._handle_message({**admin, "caption":"/manual-file file-variant-ui | plus",
            "document":{"file_id":"telegram-variant-ui-id","file_name":"plus.zip"}})
        self.assertEqual(self.bot.data.manual_variant_delivery("file-variant-ui", "plus"),
            {"delivery_mode":"file", "delivery_payload":"telegram-variant-ui-id"})
        self.bot.data.ensure_customer(999, first_name="Buyer")
        self.bot.data.post_wallet_entry(telegram_id=999, idempotency_key="file-ui-fund", amount_minor=2000, entry_type="deposit")
        await self.bot._handle_callback({"id":"file-ui-select","from":{"id":999},
            "message":{"message_id":720,"chat":{"id":999,"type":"private"}},
            "data":"customer:manualbuy:file-ui"})
        confirm_data = self.bot.api.send_text.await_args.args[2]["inline_keyboard"][0][0]["callback_data"]
        self.bot.api.send_text.reset_mock()
        await self.bot._handle_callback({"id":"file-ui-buy","from":{"id":999},
            "message":{"message_id":721,"chat":{"id":999,"type":"private"}}, "data":confirm_data})
        self.bot.api.call.assert_awaited_once()
        method, payload = self.bot.api.call.await_args.args
        self.assertEqual(method, "sendDocument")
        self.assertEqual(payload["document"], "telegram-file-ui-id")
        self.assertTrue(payload["protect_content"])
        order_id = self.bot.data.list_manual_orders(status="all")[0]["order_id"]
        self.assertEqual(self.bot.data.wallet_balance(999, "IRR"), 1300)
        self.bot.api.call.reset_mock()
        await self.bot._handle_callback({"id":"file-ui-retry","from":{"id":999},
            "message":{"message_id":722,"chat":{"id":999,"type":"private"}},
            "data":f"customer:file_retry:{order_id}"})
        self.bot.api.call.assert_awaited_once()
        self.assertEqual(self.bot.api.call.await_args.args[1]["document"], "telegram-file-ui-id")
        self.assertEqual(self.bot.data.wallet_balance(999, "IRR"), 1300)
        self.bot.api.call.reset_mock()
        self.bot.api.send_text.reset_mock()
        await self.bot._handle_callback({"id":"file-ui-other-owner","from":{"id":1000},
            "message":{"message_id":723,"chat":{"id":1000,"type":"private"}},
            "data":f"customer:file_retry:{order_id}"})
        self.bot.api.call.assert_not_awaited()
        self.assertIn("متعلق به حساب شما نیست", self.bot.api.send_text.await_args.args[1])

    async def test_order_history_offers_file_redelivery_without_exposing_file_id_or_recharging(self):
        self.bot.api.call = AsyncMock(return_value={"ok": True})
        self.bot.data.ensure_customer(999, first_name="Buyer")
        self.bot.data.post_wallet_entry(telegram_id=999, idempotency_key="history-file-fund", amount_minor=1200, entry_type="deposit")
        self.bot.data.put_manual_product(product_id="history-file", title="History download", price_minor=400,
            currency="IRR", delivery_mode="manual")
        self.bot.data.set_manual_product_file(product_id="history-file", file_id="history-telegram-file-id")
        order = self.bot.data.buy_manual_product(telegram_id=999, product_id="history-file",
            idempotency_key="history-file-order")
        await self.bot._handle_callback({"id":"history-file-orders","from":{"id":999},
            "message":{"message_id":750,"chat":{"id":999,"type":"private"}}, "data":"customer:orders:0"})
        body, keyboard = self.bot.api.send_text.await_args.args[1:3]
        self.assertIn("History download", body)
        self.assertNotIn("history-telegram-file-id", body)
        retry_button = next(button for row in keyboard["inline_keyboard"] for button in row
                            if button["callback_data"] == f"customer:file_retry:{order['order_id']}")
        self.assertIn("دریافت فایل", retry_button["text"])
        await self.bot._handle_callback({"id":"history-file-retry","from":{"id":999},
            "message":{"message_id":751,"chat":{"id":999,"type":"private"}},
            "data":retry_button["callback_data"]})
        self.bot.api.call.assert_awaited_once()
        self.assertEqual(self.bot.api.call.await_args.args[1]["document"], "history-telegram-file-id")
        self.assertEqual(self.bot.data.wallet_balance(999, "IRR"), 800)

    async def test_base_file_quote_rejects_changed_price_or_file_before_debit(self):
        self.bot.api.call = AsyncMock(return_value={"ok": True})
        self.bot.data.ensure_customer(999, first_name="Buyer")
        self.bot.data.post_wallet_entry(telegram_id=999, idempotency_key="file-stale-fund", amount_minor=2000, entry_type="deposit")
        self.bot.data.put_manual_product(product_id="file-stale", title="Stale file", price_minor=500,
            currency="IRR", delivery_mode="manual")
        self.bot.data.set_manual_product_file(product_id="file-stale", file_id="telegram-file-old")
        await self.bot._handle_callback({"id":"file-stale-screen","from":{"id":999},
            "message":{"message_id":740,"chat":{"id":999,"type":"private"}},
            "data":"customer:manualbuy:file-stale"})
        confirm_data = self.bot.api.send_text.await_args.args[2]["inline_keyboard"][0][0]["callback_data"]
        self.assertTrue(confirm_data.startswith("customer:mp:file-stale:"))
        self.bot.data.put_manual_product(product_id="file-stale", title="Stale file", price_minor=600,
            currency="IRR", delivery_mode="file", delivery_payload="telegram-file-new")
        self.bot.api.send_text.reset_mock()
        await self.bot._handle_callback({"id":"file-stale-buy","from":{"id":999},
            "message":{"message_id":741,"chat":{"id":999,"type":"private"}}, "data":confirm_data})
        self.assertIn("فایل این محصول تغییر کرده است", self.bot.api.send_text.await_args.args[1])
        self.assertEqual(self.bot.data.wallet_balance(999, "IRR"), 2000)
        self.assertEqual(self.bot.data.list_manual_orders(status="all"), [])
        self.bot.api.call.assert_not_awaited()

    async def test_file_delivery_failure_offers_retry_without_recharging(self):
        self.bot.api.call = AsyncMock(side_effect=[RuntimeError("transport detail must not leak"), {"ok": True}])
        self.bot.data.ensure_customer(999, first_name="Buyer")
        self.bot.data.post_wallet_entry(telegram_id=999, idempotency_key="file-retry-fund", amount_minor=1500, entry_type="deposit")
        self.bot.data.put_manual_product(product_id="file-retry", title="Retry file", price_minor=500,
            currency="IRR", delivery_mode="manual")
        self.bot.data.set_manual_product_file(product_id="file-retry", file_id="telegram-file-retry-id")
        await self.bot._handle_callback({"id":"file-retry-select","from":{"id":999},
            "message":{"message_id":730,"chat":{"id":999,"type":"private"}},
            "data":"customer:manualbuy:file-retry"})
        confirm_data = self.bot.api.send_text.await_args.args[2]["inline_keyboard"][0][0]["callback_data"]
        self.bot.api.send_text.reset_mock()
        await self.bot._handle_callback({"id":"file-retry-buy","from":{"id":999},
            "message":{"message_id":731,"chat":{"id":999,"type":"private"}}, "data":confirm_data})
        self.assertIn("مبلغ دوباره کسر نمی‌شود", self.bot.api.send_text.await_args.args[1])
        self.assertNotIn("transport detail", self.bot.api.send_text.await_args.args[1])
        self.assertNotIn("telegram-file-retry-id", self.bot.api.send_text.await_args.args[1])
        retry_data = self.bot.api.send_text.await_args.args[2]["inline_keyboard"][0][0]["callback_data"]
        self.assertTrue(retry_data.startswith("customer:file_retry:"))
        self.assertEqual(self.bot.data.wallet_balance(999, "IRR"), 1000)
        self.bot.api.send_text.reset_mock()
        await self.bot._handle_callback({"id":"file-retry-again","from":{"id":999},
            "message":{"message_id":732,"chat":{"id":999,"type":"private"}}, "data":retry_data})
        self.assertEqual(self.bot.api.call.await_count, 2)
        self.assertEqual(self.bot.data.wallet_balance(999, "IRR"), 1000)

    async def test_non_admin_cannot_attach_manual_product_file(self):
        self.bot.data.put_manual_product(product_id="file-denied", title="Private file", price_minor=500,
            currency="IRR", delivery_mode="manual")
        await self.bot._handle_message({"from":{"id":999},"chat":{"id":999,"type":"private"},
            "caption":"/manual-file file-denied", "document":{"file_id":"not-authorized"}})
        self.assertEqual(self.bot.data.get_manual_product("file-denied")["delivery_mode"], "manual")
        self.assertNotEqual(self.bot.data.get_manual_product("file-denied")["delivery_payload"], "not-authorized")

    async def test_manual_variant_selection_confirms_and_charges_variant_price(self):
        self.bot.data.ensure_customer(999, first_name="Buyer")
        self.bot.data.post_wallet_entry(telegram_id=999, idempotency_key="variant-bot-fund", amount_minor=3000, entry_type="deposit")
        self.bot.data.put_manual_product(product_id="variant-bot", title="Variant item", price_minor=1200,
            currency="IRR", delivery_mode="instant", delivery_payload="VARIANT-DELIVERY")
        self.bot.data.put_manual_variant(product_id="variant-bot", variant_id="basic", title="Basic", price_minor=500)
        self.bot.data.put_manual_variant(product_id="variant-bot", variant_id="pro", title="Pro", price_minor=900)
        await self.bot._handle_callback({"id":"variant-select","from":{"id":999},
            "message":{"message_id":700,"chat":{"id":999,"type":"private"}},
            "data":"customer:manualbuy:variant-bot"})
        body = self.bot.api.send_text.await_args.args[1]
        self.assertIn("انتخاب", body)
        keyboard = self.bot.api.send_text.await_args.args[2]["inline_keyboard"]
        callbacks = [button["callback_data"] for row in keyboard for button in row]
        self.assertIn("customer:manualbuy:variant-bot~basic", callbacks)
        self.bot.api.send_text.reset_mock()
        await self.bot._handle_callback({"id":"variant-confirm-screen","from":{"id":999},
            "message":{"message_id":701,"chat":{"id":999,"type":"private"}},
            "data":"customer:manualbuy:variant-bot~basic"})
        body = self.bot.api.send_text.await_args.args[1]
        self.assertIn("Variant item — Basic", body)
        self.assertIn("500 IRR", body)
        confirm_data = self.bot.api.send_text.await_args.args[2]["inline_keyboard"][0][0]["callback_data"]
        self.bot.api.send_text.reset_mock()
        await self.bot._handle_callback({"id":"variant-buy","from":{"id":999},
            "message":{"message_id":702,"chat":{"id":999,"type":"private"}}, "data":confirm_data})
        body = self.bot.api.send_text.await_args.args[1]
        self.assertIn("Variant item — Basic", body)
        self.assertIn("500 IRR", body)
        self.assertEqual(self.bot.data.wallet_balance(999, "IRR"), 2500)
        with self.bot.data._connect() as db:
            row = db.execute("SELECT variant_id_snapshot,variant_title_snapshot,amount_minor FROM manual_product_orders WHERE tenant_id=?",
                (self.bot.data.tenant_id,)).fetchone()
        self.assertEqual(tuple(row), ("basic", "Basic", 500))


    async def test_admin_can_manage_manual_product_variants_and_variant_inventory(self):
        admin = {"from":{"id":6883349141},"chat":{"id":6883349141,"type":"private"}}
        await self.bot._handle_message({**admin, "text":"/manual-product add variant-admin | Variant Admin | 1000 | IRR | inventory |  | Admin variant test"})
        await self.bot._handle_message({**admin, "text":"/manual-variant add variant-admin | monthly | Monthly | 1200"})
        variant = self.bot.data.get_manual_variant("variant-admin", "monthly")
        self.assertEqual(variant["title"], "Monthly")
        self.assertEqual(variant["price_minor"], 1200)
        await self.bot._handle_message({**admin, "text":"/manual-key add variant-admin | VARIANT-KEY | monthly"})
        self.assertEqual(self.bot.data.manual_inventory_count("variant-admin", "monthly"), 1)
        self.bot.api.send_text.reset_mock()
        await self.bot._handle_message({**admin, "text":"/manual-variant list variant-admin"})
        self.assertIn("Monthly", self.bot.api.send_text.await_args.args[1])
        await self.bot._handle_message({**admin, "text":"/manual-variant edit variant-admin | monthly | Monthly Plus | 1500"})
        self.assertEqual(self.bot.data.get_manual_variant("variant-admin", "monthly")["price_minor"], 1500)
        await self.bot._handle_message({**admin, "text":"/manual-variant edit variant-admin | monthly | Monthly Plus | 1500 | instant | PRIVATE-VARIANT-PAYLOAD"})
        variant = self.bot.data.get_manual_variant("variant-admin", "monthly")
        self.assertEqual(variant["delivery_mode"], "instant")
        self.assertEqual(self.bot.data.manual_variant_delivery("variant-admin", "monthly")["delivery_payload"], "PRIVATE-VARIANT-PAYLOAD")
        self.bot.api.send_text.reset_mock()
        await self.bot._handle_message({**admin, "text":"/manual-variant list variant-admin"})
        listing = self.bot.api.send_text.await_args.args[1]
        self.assertIn("خودکار", listing)
        self.assertNotIn("PRIVATE-VARIANT-PAYLOAD", listing)
        await self.bot._handle_message({**admin, "text":"/manual-variant archive variant-admin | monthly"})
        archived = self.bot.data.get_manual_variant("variant-admin", "monthly", enabled_only=False)
        self.assertEqual(archived["enabled"], 0)
        self.assertEqual(self.bot.data.manual_inventory_count("variant-admin", "monthly"), 1)
        self.assertEqual(self.bot.data.list_manual_variants("variant-admin"), [])


    async def test_variant_inventory_override_controls_stock_button_and_delivery(self):
        self.bot.data.ensure_customer(999, first_name="Buyer")
        self.bot.data.post_wallet_entry(telegram_id=999, idempotency_key="variant-mode-ui-fund",
                                        amount_minor=1500, entry_type="deposit")
        self.bot.data.put_manual_product(product_id="variant-mode-ui", title="Mode UI", price_minor=1000,
            currency="IRR", delivery_mode="instant", delivery_payload="PARENT-INSTANT")
        self.bot.data.put_manual_variant(product_id="variant-mode-ui", variant_id="stock", title="Stock",
            price_minor=500, delivery_mode="inventory", delivery_payload=None, delivery_config_provided=True)
        self.bot.data.put_manual_variant(product_id="variant-mode-ui", variant_id="manual", title="Manual",
            price_minor=600, delivery_mode="manual", delivery_payload=None, delivery_config_provided=True)
        await self.bot._handle_callback({"id":"variant-mode-empty","from":{"id":999},
            "message":{"message_id":900,"chat":{"id":999,"type":"private"}},
            "data":"customer:manualbuy:variant-mode-ui"})
        keyboard = self.bot.api.send_text.await_args.args[2]["inline_keyboard"]
        callbacks = [button["callback_data"] for row in keyboard for button in row]
        self.assertNotIn("customer:manualbuy:variant-mode-ui~stock", callbacks)
        self.assertIn("customer:manualbuy:variant-mode-ui~manual", callbacks)
        self.bot.data.add_manual_inventory_key(product_id="variant-mode-ui", variant_id="stock",
                                               secret_payload="VARIANT-ONLY-KEY")
        self.bot.api.send_text.reset_mock()
        await self.bot._handle_callback({"id":"variant-mode-restocked","from":{"id":999},
            "message":{"message_id":901,"chat":{"id":999,"type":"private"}},
            "data":"customer:manualbuy:variant-mode-ui"})
        keyboard = self.bot.api.send_text.await_args.args[2]["inline_keyboard"]
        callbacks = [button["callback_data"] for row in keyboard for button in row]
        self.assertIn("customer:manualbuy:variant-mode-ui~stock", callbacks)
        self.bot.api.send_text.reset_mock()
        await self.bot._handle_callback({"id":"variant-mode-confirm-screen","from":{"id":999},
            "message":{"message_id":902,"chat":{"id":999,"type":"private"}},
            "data":"customer:manualbuy:variant-mode-ui~stock"})
        confirmation = self.bot.api.send_text.await_args.args[1]
        self.assertIn("خودکار پس از پرداخت", confirmation)
        confirm_data = self.bot.api.send_text.await_args.args[2]["inline_keyboard"][0][0]["callback_data"]
        self.bot.api.send_text.reset_mock()
        await self.bot._handle_callback({"id":"variant-mode-buy","from":{"id":999},
            "message":{"message_id":903,"chat":{"id":999,"type":"private"}}, "data":confirm_data})
        body = self.bot.api.send_text.await_args.args[1]
        self.assertIn("VARIANT-ONLY-KEY", body)
        self.assertEqual(self.bot.data.wallet_balance(999, "IRR"), 1000)

    async def test_manual_variant_confirmation_rejects_stale_price_without_debit(self):
        self.bot.data.ensure_customer(999, first_name="Buyer")
        self.bot.data.post_wallet_entry(telegram_id=999, idempotency_key="variant-stale-fund", amount_minor=2000, entry_type="deposit")
        self.bot.data.put_manual_product(product_id="variant-stale", title="Stale quote", price_minor=1000,
            currency="IRR", delivery_mode="instant", delivery_payload="SAFE")
        self.bot.data.put_manual_variant(product_id="variant-stale", variant_id="monthly", title="Monthly", price_minor=500)
        await self.bot._handle_callback({"id":"variant-stale-screen","from":{"id":999},
            "message":{"message_id":810,"chat":{"id":999,"type":"private"}},
            "data":"customer:manualbuy:variant-stale~monthly"})
        confirm_data = self.bot.api.send_text.await_args.args[2]["inline_keyboard"][0][0]["callback_data"]
        self.assertLessEqual(len(confirm_data.encode("utf-8")), 64)
        self.assertTrue(confirm_data.startswith("customer:mv:"))
        self.bot.data.put_manual_variant(product_id="variant-stale", variant_id="monthly", title="Monthly", price_minor=700)
        self.bot.api.send_text.reset_mock()
        await self.bot._handle_callback({"id":"variant-stale-buy","from":{"id":999},
            "message":{"message_id":811,"chat":{"id":999,"type":"private"}}, "data":confirm_data})
        self.assertIn("قیمت، نام یا شرایط", self.bot.api.send_text.await_args.args[1])
        self.assertEqual(self.bot.data.wallet_balance(999, "IRR"), 2000)
        with self.bot.data._connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM manual_product_orders WHERE tenant_id=?", (self.bot.data.tenant_id,)).fetchone()[0], 0)



    async def test_admin_product_order_read_only_check_and_finalize_commands_are_audited(self):
        admin = {"from": {"id": 6883349141}, "chat": {"id": 6883349141, "type": "private"}}
        secret_url = "https://sub.example/private-token"
        observed = {
            "order_id": "ambiguous-order", "telegram_id": 123, "status": "provisioning",
            "comparison": "target_present", "identity_verified": True,
            "username": "u123_abcd1234", "provider_user_id": "provider-safe-id",
            "provider_kind": "pasarguard", "panel_id": "panel-a",
            "observed_status": "active", "observed_data_limit_bytes": 1024,
            "observed_expire_at": "2026-10-22T00:00:00+00:00",
            "subscription_url": secret_url, "evidence": {"panel_id": "panel-a"},
        }
        with patch.object(customer_bot_module.CustomerPurchaseService,
                          "inspect_product_order_reconciliation",
                          new_callable=AsyncMock, return_value=observed) as inspect:
            await self.bot._handle_message({**admin, "text": "/product-order-check ambiguous-order"})
            inspect.assert_awaited_once_with(order_id="ambiguous-order")
            body = self.bot.api.send_text.await_args.args[1]
            self.assertIn("تطبیق دارد", body)
            self.assertIn("u123_abcd1234", body)
            self.assertNotIn(secret_url, body)
            self.assertIn("/product-order-finalize", body)

        self.bot.api.send_text.reset_mock()
        with patch.object(customer_bot_module.CustomerPurchaseService,
                          "finalize_product_order_target_present",
                          new_callable=AsyncMock,
                          return_value={"order_id": "ambiguous-order", "idempotent_replay": False}) as finalize:
            await self.bot._handle_message({**admin,
                "text": "/product-order-finalize ambiguous-order | verified target after read-only comparison"})
            finalize.assert_awaited_once_with(order_id="ambiguous-order",
                actor_telegram_id=6883349141, reason="verified target after read-only comparison")
            body = self.bot.api.send_text.await_args.args[1]
            self.assertIn("نهایی شد", body)
            self.assertIn("هیچ ساخت مجدد", body)

    async def test_admin_product_order_list_and_detail_do_not_dump_secrets(self):
        admin = {"from": {"id": 6883349141}, "chat": {"id": 6883349141, "type": "private"}}
        with patch.object(self.bot.data, "list_product_order_reconciliation", return_value=[{
            "order_id": "ambiguous-order", "telegram_id": 123, "product_id": "basic",
            "title": "Basic", "amount_minor": 1500, "currency": "IRR", "status": "provisioning",
            "provider_reference": "reconcile_required:TimeoutError", "has_provisioning_intent": True,
        }]) as listing:
            await self.bot._handle_message({**admin, "text": "/product-orders reconcile_required"})
            listing.assert_called_once_with(status="reconcile_required", limit=50)
            body = self.bot.api.send_text.await_args.args[1]
            self.assertIn("ambiguous-order", body)
            self.assertIn("/product-order-check", body)

        self.bot.api.send_text.reset_mock()
        private_token = "https://sub.example/must-not-render"
        with patch.object(self.bot.data, "get_product_order_reconciliation", return_value={
            "order_id": "ambiguous-order", "telegram_id": 123, "product_id": "basic",
            "amount_minor": 1500, "currency": "IRR", "status": "provisioning",
            "provider_reference": "reconcile_required:TimeoutError",
            "product_snapshot": {"title": "Basic", "panel_id": "panel-a",
                "_provisioning_intent": {"version": 1, "username": "u123_abcd1234",
                    "provider_binding": {"panel_id": "panel-a", "provider_kind": "pasarguard",
                                         "secret_ref": "env:PANEL_KEY"},
                    "private_url": private_token}},
        }) as detail:
            await self.bot._handle_message({**admin, "text": "/product-order ambiguous-order"})
            detail.assert_called_once_with("ambiguous-order")
            body = self.bot.api.send_text.await_args.args[1]
            self.assertIn("u123_abcd1234", body)
            self.assertIn("pasarguard", body)
            self.assertNotIn(private_token, body)
            self.assertNotIn("env:PANEL_KEY", body)

    async def test_non_admin_cannot_read_or_finalize_product_order_reconciliation(self):
        message = {"from": {"id": 999}, "chat": {"id": 999, "type": "private"},
                   "text": "/product-order-finalize private-order | unauthorized"}
        with patch.object(customer_bot_module.CustomerPurchaseService,
                          "finalize_product_order_target_present", new_callable=AsyncMock) as finalize:
            await self.bot._handle_message(message)
            finalize.assert_not_awaited()
        self.assertIn("فقط برای مدیر مجاز", self.bot.api.send_text.await_args.args[1])


if __name__ == "__main__":
    unittest.main()
