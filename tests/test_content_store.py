import sqlite3
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

    def test_migration_adopts_legacy_content_and_is_idempotent(self):
        with sqlite3.connect(self.db) as connection:
            connection.execute("""CREATE TABLE bot_content (
                namespace TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(namespace, key))""")
            connection.execute(
                "INSERT INTO bot_content(namespace,key,value) VALUES(?,?,?)",
                ("customer:alpha", "title", "Existing title"),
            )
        store = BotContentStore("customer:alpha", self.defaults, self.db)
        self.assertEqual(store.get("title"), "Existing title")
        BotContentStore("customer:alpha", self.defaults, self.db)
        with sqlite3.connect(self.db) as connection:
            versions = connection.execute(
                "SELECT version,name FROM content_schema_migrations ORDER BY version"
            ).fetchall()
            self.assertEqual(versions, [(1, "bot_content_namespace_overrides")])
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_migration_failure_rolls_back_new_content_schema(self):
        with sqlite3.connect(self.db) as connection:
            connection.execute("""CREATE TABLE content_schema_migrations (
                version INTEGER PRIMARY KEY CHECK(version > 0), name TEXT NOT NULL,
                applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
            connection.execute("""CREATE TRIGGER fail_content_migration
                BEFORE INSERT ON content_schema_migrations WHEN NEW.version=1
                BEGIN SELECT RAISE(ABORT, 'injected migration failure'); END""")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "injected migration failure"):
            BotContentStore("customer:alpha", self.defaults, self.db)
        with sqlite3.connect(self.db) as connection:
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            self.assertNotIn("bot_content", tables)
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM content_schema_migrations"
            ).fetchone()[0], 0)

    def test_unknown_future_content_schema_fails_closed(self):
        with sqlite3.connect(self.db) as connection:
            connection.execute("""CREATE TABLE content_schema_migrations (
                version INTEGER PRIMARY KEY CHECK(version > 0), name TEXT NOT NULL,
                applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
            connection.execute(
                "INSERT INTO content_schema_migrations(version,name) VALUES(2,'future')"
            )
        with self.assertRaisesRegex(ContentStoreError, "newer than this application supports"):
            BotContentStore("customer:alpha", self.defaults, self.db)

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
