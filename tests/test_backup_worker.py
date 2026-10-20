import stat
import tempfile
import unittest
from pathlib import Path

from manubisguard.backup_worker import BackupExecutionError, run_backup_once
from manubisguard.customer_store import CustomerDataStore
from manubisguard.database_maintenance import verify_database


class BackupWorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / "customer.sqlite3"
        self.store = CustomerDataStore(str(self.source), tenant_id="tenant-backup")
        self.backup_root = self.root / "approved-backups"
        self.backup_root.mkdir(mode=0o700)
        self.backup_root.chmod(0o700)
        self.configure_policy()

    def tearDown(self):
        self.tmp.cleanup()

    def configure_policy(self, *, destination="local", local_directory=None):
        self.store.update_backup_policy(frequency="daily", timezone_name="UTC", local_time="03:00",
            interval_minutes=1440, destination=destination,
            local_directory=str(local_directory or (self.backup_root / "daily")),
            telegram_chat_id=(-1001234567890 if destination in {"telegram", "both"} else None),
            telegram_topic_id=0, retention_count=14, retention_days=30, updated_by=123)

    def test_manual_one_shot_creates_verified_private_backup_and_queues_redacted_event(self):
        self.store.add_notification_route(event_key="backup", chat_id=-1001234567890, topic_id=0, created_by=123)
        result = run_backup_once(self.store, self.source, approved_root=self.backup_root,
            now=1_800_000_000, run_id="backup-run-0001")
        target = Path(result["path"])
        self.assertEqual(result["status"], "success")
        self.assertTrue(result["ok"])
        self.assertEqual(result["integrity"], "ok")
        self.assertEqual(result["foreign_key_violations"], 0)
        self.assertEqual(result["mode"], "0o600")
        self.assertGreater(result["size_bytes"], 0)
        created = list((self.backup_root / "daily").glob("customer-backup-*.sqlite3"))
        self.assertEqual(len(created), 1)
        self.assertEqual(stat.S_IMODE(created[0].stat().st_mode), 0o600)
        self.assertTrue(verify_database(created[0])["ok"])
        policy = self.store.get_backup_policy()
        self.assertEqual(policy["last_status"], "success")
        self.assertEqual(policy["last_run_at"], 1_800_000_000)
        claimed = self.store.claim_admin_notification_deliveries(now=1_800_000_000)
        backup_events = [item for item in claimed if item["event_key"] == "backup"]
        self.assertEqual(len(backup_events), 1)
        self.assertEqual(backup_events[0]["payload"]["destination"], "local")
        self.assertEqual(backup_events[0]["payload"]["size_bytes"], result["size_bytes"])
        self.assertNotIn(str(self.root), str(backup_events[0]["payload"]))

    def test_telegram_destination_fails_closed_without_creating_local_backup(self):
        self.configure_policy(destination="both")
        self.store.add_notification_route(event_key="backup", chat_id=-1001234567890, topic_id=0, created_by=123)
        with self.assertRaises(BackupExecutionError) as raised:
            run_backup_once(self.store, self.source, approved_root=self.backup_root,
                now=1_800_000_100, run_id="backup-run-0002")
        self.assertEqual(raised.exception.reason_code, "TelegramUploadNotImplemented")
        self.assertEqual(list(self.backup_root.iterdir()), [])
        policy = self.store.get_backup_policy()
        self.assertEqual(policy["last_status"], "failed")
        self.assertEqual(policy["last_error_code"], "TelegramUploadNotImplemented")
        event = self.store.list_admin_notifications(status="all")[0]
        self.assertEqual(event["event_key"], "backup")
        claimed = self.store.claim_admin_notification_deliveries(now=1_800_000_100)
        self.assertEqual(claimed[0]["payload"]["status"], "failed")
        self.assertEqual(claimed[0]["payload"]["reason_code"], "TelegramUploadNotImplemented")
        self.assertNotIn("size_bytes", claimed[0]["payload"])

    def test_backup_path_outside_approved_root_is_rejected_and_recorded(self):
        outside = self.root / "outside"
        outside.mkdir(mode=0o700)
        self.configure_policy(local_directory=str(outside))
        with self.assertRaises(BackupExecutionError) as raised:
            run_backup_once(self.store, self.source, approved_root=self.backup_root,
                now=1_800_000_200, run_id="backup-run-0003")
        self.assertEqual(raised.exception.reason_code, "UnsafeBackupPath")
        self.assertEqual(self.store.get_backup_policy()["last_status"], "failed")
        self.assertEqual(list(outside.iterdir()), [])

    def test_symlink_backup_directory_is_rejected(self):
        outside = self.root / "outside"
        outside.mkdir(mode=0o700)
        link = self.backup_root / "linked"
        link.symlink_to(outside, target_is_directory=True)
        self.configure_policy(local_directory=str(link))
        with self.assertRaises(BackupExecutionError) as raised:
            run_backup_once(self.store, self.source, approved_root=self.backup_root,
                now=1_800_000_300, run_id="backup-run-0004")
        self.assertEqual(raised.exception.reason_code, "SymlinkBackupPath")
        self.assertEqual(list(outside.iterdir()), [])

    def test_duplicate_run_id_does_not_overwrite_existing_verified_backup(self):
        result = run_backup_once(self.store, self.source, approved_root=self.backup_root,
            now=1_800_000_500, run_id="backup-run-0006")
        target = Path(result["path"])
        before = target.read_bytes()
        with self.assertRaises(BackupExecutionError) as raised:
            run_backup_once(self.store, self.source, approved_root=self.backup_root,
                now=1_800_000_500, run_id="backup-run-0006")
        self.assertEqual(raised.exception.reason_code, "BackupAlreadyExists")
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(self.store.get_backup_policy()["last_status"], "success")

    def test_unconfigured_policy_is_rejected_before_any_backup_write(self):
        unconfigured_db = self.root / "unconfigured.sqlite3"
        unconfigured = CustomerDataStore(str(unconfigured_db), tenant_id="tenant-unconfigured")
        with self.assertRaises(BackupExecutionError) as raised:
            run_backup_once(unconfigured, unconfigured_db, approved_root=self.backup_root,
                now=1_800_000_600, run_id="backup-run-0007")
        self.assertEqual(raised.exception.reason_code, "BackupPolicyNotConfigured")
        self.assertEqual(list((self.backup_root / "daily").glob("*")), [])

    def test_unverified_backup_result_is_not_recorded_as_success(self):
        def bad_backup(source, destination):
            Path(destination).write_bytes(b"not a verified database")
            return {"ok": True, "integrity": "failed", "foreign_key_violations": 0, "mode": "0o600", "size_bytes": 28}
        with self.assertRaises(BackupExecutionError) as raised:
            run_backup_once(self.store, self.source, approved_root=self.backup_root,
                now=1_800_000_400, run_id="backup-run-0005", backup_fn=bad_backup)
        self.assertEqual(raised.exception.reason_code, "BackupVerificationFailed")
        self.assertEqual(self.store.get_backup_policy()["last_status"], "failed")
        self.assertEqual(self.store.get_backup_policy()["last_error_code"], "BackupVerificationFailed")
        self.assertEqual(list((self.backup_root / "daily").glob("*")), [])


if __name__ == "__main__":
    unittest.main()
