import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from manubisguard.customer_store import CustomerStoreError
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
            ("menu_shop_enabled", "customer:shop_page:1"),
            ("menu_shop_enabled", "customer:category_page:category-1:1"),
            ("menu_shop_enabled", "customer:category_menu_page:all:1"),
            ("menu_shop_enabled", "customer:manualbuy_variants:product-1:1"),
            ("menu_shop_enabled", "customer:mp:product-1:quote"),
            ("menu_shop_enabled", "customer:file_retry:order-1"),
            ("menu_wallet_enabled", "customer:wallet:payments"),
            ("menu_shop_enabled", "customer:buy_confirm:product-1"),
            ("menu_shop_enabled", "customer:manualbuy:product-1~variant-1"),
            ("menu_shop_enabled", "customer:manualbuy_confirm:product-1"),
            ("menu_shop_enabled", "customer:mv:product-1~variant-1:quote"),
            ("menu_shop_enabled", "customer:buy_discount_confirm:product-1"),
            ("menu_shop_enabled", "customer:buy_discount_confirm:product-1:quote"),
            ("menu_shop_enabled", "customer:bd:product-1:quote"),
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
        self.assertNotIn("AI Gift", root_body)
        self.assertNotIn("AI Plan", root_body)
        keyboard=self.bot.api.send_text.await_args.args[2]
        callbacks={b.get("callback_data") for row in keyboard["inline_keyboard"] for b in row}
        self.assertIn("customer:category:ai",callbacks)
        self.assertIn("customer:category:all",callbacks)
        self.bot.api.send_text.reset_mock()
        await self.bot._handle_callback({"id":"all-products","from":{"id":999},
            "message":{"chat":{"id":999,"type":"private"}},"data":"customer:category:all"})
        all_body=self.bot.api.send_text.await_args.args[1]
        self.assertIn("AI Gift", all_body)
        self.assertIn("AI Plan", all_body)
        self.assertIn("Basic Plan", all_body)
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

[executed on device: 8017638839-32c511 (fc69b50d-fe64-47f9-ab6a-705be5de41dc)]