import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from manubisguard.telegram.control_bot import BotConfig, ControlBot
from manubisguard.telegram.customer_bot import CustomerBot, CustomerBotConfig


class BotContentFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "content.sqlite3")
        self.env = patch.dict("os.environ", {
            "ANUBIS_CONTENT_DB": self.db,
            "ANUBIS_CONTENT_NAMESPACE": "mother:test",
        })
        self.env.start()
        self.mother = ControlBot(BotConfig(token="dummy", admin_ids=frozenset({123})))
        self.mother.api.send_text = AsyncMock()
        self.mother.api.answer_callback = AsyncMock()
        self.mother.api.call = AsyncMock(return_value={})
        self.mother.content.namespace = "mother:test"
        self.customer = CustomerBot(CustomerBotConfig(token="dummy", admin_ids=frozenset({123})))
        self.customer.api.send_text = AsyncMock()
        self.customer.api.answer_callback = AsyncMock()
        self.customer.api.call = AsyncMock(return_value={})
        self.customer.content.namespace = "customer:test"

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    async def _edit_title(self, bot, callback_prefix, title):
        await bot._handle_callback({
            "id": "cb-edit",
            "from": {"id": 123},
            "message": {"chat": {"id": 123, "type": "private"}},
            "data": "content:edit:title",
        })
        await bot._handle_message({
            "from": {"id": 123},
            "chat": {"id": 123, "type": "private"},
            "text": title,
        })
        self.assertEqual(bot.content.get("title"), title)
        self.assertFalse(bot._pending_content)

    async def test_mother_admin_can_edit_title(self):
        await self._edit_title(self.mother, "page", "Factory title")

    async def test_customer_admin_can_edit_title(self):
        await self._edit_title(self.customer, "customer", "Customer title")

    async def test_customer_page_copy_uses_admin_override(self):
        self.customer.content.set("page_panels", "Custom panel page copy")
        await self.customer._handle_callback({
            "id": "cb-page",
            "from": {"id": 123},
            "message": {"chat": {"id": 123, "type": "private"}},
            "data": "customer:panels",
        })
        self.assertEqual(self.customer.api.send_text.await_args.args[1], "Custom panel page copy")

    async def test_customer_logo_photo_is_saved_and_used_on_start(self):
        await self.customer._handle_callback({
            "id": "cb-logo",
            "from": {"id": 123},
            "message": {"chat": {"id": 123, "type": "private"}},
            "data": "content:edit:logo_file_id",
        })
        await self.customer._handle_message({
            "from": {"id": 123},
            "chat": {"id": 123, "type": "private"},
            "photo": [{"file_id": "logo-small"}, {"file_id": "logo-large"}],
        })
        self.assertEqual(self.customer.content.get("logo_file_id"), "logo-large")
        await self.customer._handle_message({
            "from": {"id": 123},
            "chat": {"id": 123, "type": "private"},
            "text": "/start",
        })
        photo_call = next(call for call in self.customer.api.call.await_args_list
                          if call.args[0] == "sendPhoto")
        payload = photo_call.args[1]
        self.assertEqual(payload["chat_id"], 123)
        self.assertEqual(payload["photo"], "logo-large")
        self.assertIn("caption", payload)
        self.assertIn("reply_markup", payload)
        self.assertIn("keyboard", payload["reply_markup"])
        self.assertTrue(payload["protect_content"])


if __name__ == "__main__":
    unittest.main()
