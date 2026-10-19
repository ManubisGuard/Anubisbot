import os
import unittest
from unittest.mock import AsyncMock, patch

from manubisguard.telegram.control_bot import BotConfig, ControlBot, TelegramAPIError


class BotConfigTests(unittest.TestCase):
    def test_requires_token_and_admin(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "MANUBISGUARD_BOT_TOKEN"):
                BotConfig.from_env()

    def test_parses_admin_allowlist(self):
        with patch.dict(os.environ, {"MANUBISGUARD_BOT_TOKEN": "dummy-test-token", "MANUBISGUARD_ADMIN_IDS": "6883349141,12345"}, clear=True):
            cfg = BotConfig.from_env()
        self.assertEqual(cfg.admin_ids, frozenset({6883349141, 12345}))
        self.assertNotIn("dummy-test-token", repr(cfg.admin_ids))

    def test_rejects_non_numeric_admin(self):
        with patch.dict(os.environ, {"MANUBISGUARD_BOT_TOKEN": "dummy-test-token", "MANUBISGUARD_ADMIN_IDS": "not-an-id"}, clear=True):
            with self.assertRaisesRegex(ValueError, "numeric IDs"):
                BotConfig.from_env()


class ControlBotAuthorizationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config = BotConfig(token="dummy-test-token", admin_ids=frozenset({6883349141}))
        self.bot = ControlBot(self.config)
        self.bot.api.send_text = AsyncMock()
        self.bot.api.answer_callback = AsyncMock()
        self.bot.api.get_me = AsyncMock(return_value={"id": 555, "username": "test_bot"})

    async def test_authorized_admin_private_start(self):
        await self.bot._handle_message({
            "from": {"id": 6883349141},
            "chat": {"id": 6883349141, "type": "private"},
            "text": "/start",
        })
        self.bot.api.send_text.assert_awaited_once()
        self.assertIn("ANUBIS BOT CREATORE", self.bot.api.send_text.await_args.args[1])
        keyboard = self.bot.api.send_text.await_args.args[2]
        self.assertIn("inline_keyboard", keyboard)
        create_button = keyboard["inline_keyboard"][0][0]
        self.assertEqual(create_button["text"], "＋ ساخت ربات جدید")
        self.assertEqual(create_button["callback_data"], "bot:create")
        self.assertEqual(create_button["style"], "success")
        self.assertNotIn("panels", {button.get("callback_data") for row in keyboard["inline_keyboard"] for button in row})
        self.assertNotIn("resellers", {button.get("callback_data") for row in keyboard["inline_keyboard"] for button in row})
        bot_list_button = keyboard["inline_keyboard"][1][0]
        self.assertEqual(bot_list_button["style"], "primary")
        web_app_button = keyboard["inline_keyboard"][1][1]
        self.assertEqual(web_app_button["text"], "🖥 پنل گرافیکی")
        self.assertEqual(web_app_button["web_app"]["url"], "https://Bott.qoqnusradio.top/")
        self.assertEqual(web_app_button["style"], "primary")

    async def test_create_bot_callback_explains_separate_customer_instance(self):
        await self.bot._handle_callback({
            "id": "callback-1",
            "from": {"id": 6883349141},
            "message": {"chat": {"id": 6883349141, "type": "private"}},
            "data": "bot:create",
        })
        self.bot.api.answer_callback.assert_awaited_once_with("callback-1")
        self.bot.api.send_text.assert_awaited_once()
        message = self.bot.api.send_text.await_args.args[1]
        self.assertIn("@BotFather", message)
        self.assertIn("توکن واقعی نفرست", message)
        self.assertIn("پورت", message)

    async def test_unknown_user_is_silent(self):
        await self.bot._handle_message({
            "from": {"id": 123},
            "chat": {"id": 123, "type": "private"},
            "text": "/start",
        })
        self.bot.api.send_text.assert_not_awaited()

    async def test_group_chat_is_not_authorized(self):
        await self.bot._handle_message({
            "from": {"id": 6883349141},
            "chat": {"id": -100123, "type": "supergroup"},
            "text": "/start",
        })
        self.bot.api.send_text.assert_not_awaited()

    async def test_start_refuses_existing_webhook_without_removing_it(self):
        self.bot.api.call = AsyncMock(return_value={"url": "https://example.invalid/webhook"})
        with self.assertRaisesRegex(SystemExit, "Webhook is configured"):
            await self.bot.run()
        self.bot.api.call.assert_awaited_once_with("getWebhookInfo")
        self.bot.api.get_me.assert_not_awaited()

    async def test_health_does_not_expose_error(self):
        self.bot.api.get_me.side_effect = TelegramAPIError("redacted")
        await self.bot._handle_message({
            "from": {"id": 6883349141},
            "chat": {"id": 6883349141, "type": "private"},
            "text": "/health",
        })
        reply = self.bot.api.send_text.await_args.args[1]
        self.assertNotIn("redacted", reply)
        self.assertIn("ناموفق", reply)


if __name__ == "__main__":
    unittest.main()
