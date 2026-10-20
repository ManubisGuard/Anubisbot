import importlib.util
import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "customer_db_maintenance.py"
SPEC = importlib.util.spec_from_file_location("customer_db_maintenance", MODULE_PATH)
maintenance = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(maintenance)


class CustomerDatabaseMaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / "customer.sqlite3"
        with sqlite3.connect(self.source) as db:
            db.executescript("""
                PRAGMA foreign_keys=ON;
                CREATE TABLE customer(id INTEGER PRIMARY KEY, name TEXT NOT NULL);
                CREATE TABLE orders(id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL,
                    FOREIGN KEY(customer_id) REFERENCES customer(id));
                INSERT INTO customer VALUES(1, 'Alice');
                INSERT INTO orders VALUES(10, 1);
            """)

    def test_cli_runs_from_source_checkout_without_pythonpath(self):
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        completed = subprocess.run([sys.executable, str(MODULE_PATH), "--help"],
            cwd=MODULE_PATH.parents[1], env=env, capture_output=True, text=True, timeout=5, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("backup", completed.stdout)
        self.assertIn("restore", completed.stdout)

    def tearDown(self):
        self.tmp.cleanup()

    def test_backup_verify_and_restore_round_trip_preserves_rows(self):
        backup = self.root / "backups" / "customer-backup.sqlite3"
        backup.parent.mkdir()
        result = maintenance.backup_database(self.source, backup)
        self.assertTrue(result["ok"])
        self.assertEqual(result["integrity"], "ok")
        self.assertEqual(result["foreign_key_violations"], 0)
        self.assertEqual(result["mode"], "0o600")
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
        self.assertTrue(maintenance.verify_database(backup)["ok"])
        restored = self.root / "restore" / "customer-restored.sqlite3"
        restored.parent.mkdir()
        restore_result = maintenance.restore_database(backup, restored)
        self.assertTrue(restore_result["restored_to_new_path"])
        self.assertFalse(restore_result["live_database_replaced"])
        with sqlite3.connect(restored) as db:
            self.assertEqual(db.execute("SELECT name FROM customer WHERE id=1").fetchone()[0], "Alice")
            self.assertEqual(db.execute("SELECT customer_id FROM orders WHERE id=10").fetchone()[0], 1)

    def test_backup_refuses_to_overwrite_existing_destination(self):
        destination = self.root / "existing.sqlite3"
        destination.write_bytes(b"do not replace")
        with self.assertRaisesRegex(maintenance.DatabaseMaintenanceError, "already exists"):
            maintenance.backup_database(self.source, destination)
        self.assertEqual(destination.read_bytes(), b"do not replace")

    def test_backup_refuses_same_path_and_symlink_source(self):
        with self.assertRaisesRegex(maintenance.DatabaseMaintenanceError, "different paths"):
            maintenance.backup_database(self.source, self.source)
        link = self.root / "source-link.sqlite3"
        link.symlink_to(self.source)
        with self.assertRaisesRegex(maintenance.DatabaseMaintenanceError, "non-symlink"):
            maintenance.verify_database(link)

    def test_verification_rejects_foreign_key_violations(self):
        bad = self.root / "bad.sqlite3"
        with sqlite3.connect(bad) as db:
            db.executescript("""
                PRAGMA foreign_keys=OFF;
                CREATE TABLE parent(id INTEGER PRIMARY KEY);
                CREATE TABLE child(parent_id INTEGER REFERENCES parent(id));
                INSERT INTO child VALUES(99);
            """)
        with self.assertRaisesRegex(maintenance.DatabaseMaintenanceError, "foreign_key_check"):
            maintenance.verify_database(bad)

    def test_verification_rejects_non_database_file_without_leaking_contents(self):
        bad = self.root / "not-a-db.sqlite3"
        bad.write_text("do-not-print-this-secret", encoding="utf-8")
        with self.assertRaisesRegex(maintenance.DatabaseMaintenanceError, "SQLite verification failed"):
            maintenance.verify_database(bad)


if __name__ == "__main__":
    unittest.main()
