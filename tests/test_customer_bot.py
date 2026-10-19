import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from manubisguard.telegram.customer_bot import (
    CUSTOMER_MENU,
    CustomerBot,
    CustomerBotConfig,
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
        self.assertIn("از منوی زیر استفاده کن", self.bot.api.send_text.await_args.args[1])
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
