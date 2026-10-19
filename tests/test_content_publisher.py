import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from manubisguard.telegram.content_store import BotContentStore


class PublicBrandPublisherTests(unittest.TestCase):
    def test_mother_brand_edits_publish_safe_json_for_miniapp(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = str(root / "content.sqlite3")
            brand = root / "public" / "brand.json"
            store = BotContentStore(
                "mother",
                {"title": "ANUBIS BOT CREATORE", "logo_url": "", "welcome_text": "Factory"},
                db,
            )
            with patch.dict("os.environ", {"ANUBIS_BRAND_JSON": str(brand)}):
                store.set("title", "New Factory Name")
                store.set("logo_url", "https://example.com/logo.png")
                payload = json.loads(brand.read_text(encoding="utf-8"))
            self.assertEqual(payload["title"], "New Factory Name")
            self.assertEqual(payload["logo_url"], "https://example.com/logo.png")
            self.assertEqual(payload["welcome_text"], "Factory")

    def test_customer_namespace_does_not_publish_mother_brand(self):
        with tempfile.TemporaryDirectory() as directory:
            brand = Path(directory) / "brand.json"
            store = BotContentStore(
                "customer:one",
                {"title": "ANUBIS BOT", "logo_url": "", "welcome_text": "Customer"},
                str(Path(directory) / "content.sqlite3"),
            )
            with patch.dict("os.environ", {"ANUBIS_BRAND_JSON": str(brand)}):
                store.set("title", "Custom Customer Bot")
            self.assertFalse(brand.exists())


if __name__ == "__main__":
    unittest.main()
