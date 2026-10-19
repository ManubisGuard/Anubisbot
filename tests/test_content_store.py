import tempfile
import unittest
from pathlib import Path

from manubisguard.telegram.content_store import BotContentStore, ContentStoreError


class BotContentStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "content.sqlite3")
        self.defaults = {"title": "Anubis Bot", "welcome_text": "Welcome", "logo_file_id": ""}

    def tearDown(self):
        self.tmp.cleanup()

    def test_defaults_and_persisted_overrides(self):
        store = BotContentStore("customer:alpha", self.defaults, self.db)
        self.assertEqual(store.get("title"), "Anubis Bot")
        store.set("title", "My VPN Bot")
        store.set("welcome_text", "سلام")
        reopened = BotContentStore("customer:alpha", self.defaults, self.db)
        self.assertEqual(reopened.get("title"), "My VPN Bot")
        self.assertEqual(reopened.get("welcome_text"), "سلام")

    def test_namespaces_are_isolated(self):
        first = BotContentStore("customer:alpha", self.defaults, self.db)
        second = BotContentStore("mother", {"title": "Anubis Bot Creatore"}, self.db)
        first.set("title", "Customer A")
        self.assertEqual(second.get("title"), "Anubis Bot Creatore")
        self.assertEqual(first.get("title"), "Customer A")

    def test_reset_restores_default(self):
        store = BotContentStore("customer:alpha", self.defaults, self.db)
        store.set("title", "Temporary")
        store.reset("title")
        self.assertEqual(store.get("title"), "Anubis Bot")

    def test_rejects_unknown_keys_and_oversized_text(self):
        store = BotContentStore("customer:alpha", self.defaults, self.db)
        with self.assertRaises(ContentStoreError):
            store.set("arbitrary_sql", "no")
        with self.assertRaises(ContentStoreError):
            store.set("welcome_text", "x" * 4001)

    def test_logo_url_requires_http_scheme(self):
        store = BotContentStore("customer:alpha", self.defaults, self.db)
        with self.assertRaises(ContentStoreError):
            store.set("logo_url", "javascript:alert(1)")
        store.set("logo_url", "https://example.com/logo.png")
        self.assertEqual(store.get("logo_url"), "https://example.com/logo.png")


if __name__ == "__main__":
    unittest.main()
