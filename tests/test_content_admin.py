import tempfile
import unittest
from pathlib import Path

from manubisguard.telegram.content_admin import (
    begin_content_edit,
    content_admin_keyboard,
    save_pending_content,
)
from manubisguard.telegram.content_store import BotContentStore


class ContentAdminTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = BotContentStore(
            "customer:test",
            {"title": "ANUBIS BOT", "welcome_text": "Default", "logo_file_id": ""},
            str(Path(self.tmp.name) / "content.sqlite3"),
        )
        self.pending = {}

    def tearDown(self):
        self.tmp.cleanup()

    def test_editor_lists_title_text_and_image_fields(self):
        keyboard = content_admin_keyboard()
        buttons = [button for row in keyboard["inline_keyboard"] for button in row]
        callbacks = {button["callback_data"] for button in buttons}
        self.assertIn("content:edit:title", callbacks)
        self.assertIn("content:edit:welcome_text", callbacks)
        self.assertIn("content:edit:logo_file_id", callbacks)
        self.assertIn("content:edit:logo_url", callbacks)

    def test_mother_and_customer_editors_are_scoped(self):
        mother = content_admin_keyboard("mother")
        customer = content_admin_keyboard("customer")
        mother_keys = {button["callback_data"] for row in mother["inline_keyboard"] for button in row}
        customer_keys = {button["callback_data"] for row in customer["inline_keyboard"] for button in row}
        self.assertIn("content:edit:menu_create", mother_keys)
        self.assertNotIn("content:edit:menu_shop", mother_keys)
        self.assertIn("content:edit:menu_shop", customer_keys)
        self.assertNotIn("content:edit:menu_create", customer_keys)

    def test_text_field_is_saved_and_persists(self):
        prompt = begin_content_edit("content:edit:title", self.pending, 123)
        self.assertIn("عنوان", prompt)
        reply = save_pending_content(self.store, self.pending, 123, {"text": "My VPN"})
        self.assertIn("ذخیره شد", reply)
        self.assertEqual(self.store.get("title"), "My VPN")
        self.assertNotIn(123, self.pending)

    def test_logo_accepts_photo_and_stores_file_reference(self):
        begin_content_edit("content:edit:logo_file_id", self.pending, 123)
        reply = save_pending_content(
            self.store,
            self.pending,
            123,
            {"photo": [{"file_id": "small-file"}, {"file_id": "largest-file"}]},
        )
        self.assertIn("ذخیره شد", reply)
        self.assertEqual(self.store.get("logo_file_id"), "largest-file")

    def test_logo_rejects_text_and_keeps_pending_state(self):
        begin_content_edit("content:edit:logo_file_id", self.pending, 123)
        reply = save_pending_content(self.store, self.pending, 123, {"text": "not an image"})
        self.assertIn("خود تصویر", reply)
        self.assertEqual(self.pending[123], "logo_file_id")

    def test_cancel_and_reset(self):
        self.store.set("title", "Custom")
        begin_content_edit("content:edit:title", self.pending, 123)
        reply = save_pending_content(self.store, self.pending, 123, {"text": "/clear"})
        self.assertIn("پیش‌فرض", reply)
        self.assertEqual(self.store.get("title"), "ANUBIS BOT")
        begin_content_edit("content:edit:title", self.pending, 123)
        reply = save_pending_content(self.store, self.pending, 123, {"text": "/cancel"})
        self.assertIn("لغو", reply)
        self.assertNotIn(123, self.pending)


if __name__ == "__main__":
    unittest.main()
