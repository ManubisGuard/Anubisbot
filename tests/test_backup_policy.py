import unittest
from datetime import datetime, timezone

from manubisguard.backup_policy import BackupPolicyError, next_scheduled_run, validate_backup_policy


class BackupPolicyTests(unittest.TestCase):
    def valid(self, **overrides):
        values = {
            "frequency": "daily", "timezone_name": "Europe/Berlin", "local_time": "03:00",
            "interval_minutes": 1440, "destination": "local",
            "local_directory": "/var/lib/anubis-bot/backups",
            "telegram_chat_id": None, "telegram_topic_id": 0,
            "retention_count": 14, "retention_days": 30,
        }
        values.update(overrides)
        return validate_backup_policy(**values)

    def test_valid_local_policy_normalizes_and_preserves_options(self):
        policy = self.valid()
        self.assertEqual(policy["timezone_name"], "Europe/Berlin")
        self.assertEqual(policy["destination"], "local")

    def test_telegram_destination_requires_numeric_chat_id(self):
        with self.assertRaisesRegex(BackupPolicyError, "requires a non-zero numeric chat ID"):
            self.valid(destination="telegram", telegram_chat_id=None)
        self.assertEqual(self.valid(destination="both", telegram_chat_id=-1001234567890,
                                    telegram_topic_id=77)["telegram_topic_id"], 77)

    def test_rejects_invalid_timezone_schedule_path_and_retention(self):
        for change, expected in [
            ({"timezone_name": "Not/AZone"}, "IANA timezone"),
            ({"local_time": "25:99"}, "HH:MM"),
            ({"local_directory": "relative/backups"}, "absolute path"),
            ({"local_directory": "/tmp/../etc"}, "without '..'"),
            ({"retention_count": 0}, "retention_count"),
            ({"interval_minutes": 2}, "interval_minutes"),
        ]:
            with self.subTest(change=change), self.assertRaisesRegex(BackupPolicyError, expected):
                self.valid(**change)

    def test_interval_schedule_is_utc_and_minimum_interval_is_enforced(self):
        now = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
        result = next_scheduled_run(now=now, frequency="interval", timezone_name="UTC",
                                    local_time="03:00", interval_minutes=60)
        self.assertEqual(result, datetime(2026, 10, 20, 13, 0, tzinfo=timezone.utc))
        with self.assertRaisesRegex(BackupPolicyError, "between 15 and 10080"):
            next_scheduled_run(now=now, frequency="interval", timezone_name="UTC",
                               local_time="03:00", interval_minutes=1)

    def test_daily_schedule_uses_local_timezone_and_moves_to_next_day(self):
        now = datetime(2026, 10, 20, 1, 0, tzinfo=timezone.utc)  # 03:00 in Berlin
        result = next_scheduled_run(now=now, frequency="daily", timezone_name="Europe/Berlin",
                                    local_time="04:30", interval_minutes=1440)
        self.assertEqual(result, datetime(2026, 10, 20, 2, 30, tzinfo=timezone.utc))

    def test_hourly_schedule_returns_next_hour_boundary(self):
        now = datetime(2026, 10, 20, 12, 42, tzinfo=timezone.utc)
        result = next_scheduled_run(now=now, frequency="hourly", timezone_name="UTC",
                                    local_time="03:00", interval_minutes=60)
        self.assertEqual(result, datetime(2026, 10, 20, 13, 0, tzinfo=timezone.utc))

    def test_daily_schedule_handles_nonexistent_dst_wall_time(self):
        now = datetime(2026, 3, 29, 0, 0, tzinfo=timezone.utc)
        result = next_scheduled_run(now=now, frequency="daily", timezone_name="Europe/Berlin",
                                    local_time="02:30", interval_minutes=1440)
        # 02:30 does not exist on the spring DST transition; next valid local time is 03:00.
        self.assertEqual(result, datetime(2026, 3, 29, 1, 0, tzinfo=timezone.utc))


if __name__ == "__main__":
    unittest.main()
