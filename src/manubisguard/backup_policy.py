"""Validation and scheduling primitives for tenant-scoped backup policy.

This module does not run backups or start timers. It only validates persisted policy
and computes the next eligible wall-clock occurrence for display/testing.
"""
from __future__ import annotations

from datetime import datetime, time as daytime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import re


class BackupPolicyError(ValueError):
    """A backup policy contains an unsupported or unsafe value."""


FREQUENCIES = frozenset({"hourly", "daily", "interval"})
DESTINATIONS = frozenset({"local", "telegram", "both"})


def validate_backup_policy(*, frequency: str, timezone_name: str, local_time: str,
                           interval_minutes: int, destination: str, local_directory: str,
                           telegram_chat_id: int | None, telegram_topic_id: int,
                           retention_count: int, retention_days: int) -> dict:
    frequency = str(frequency).strip().lower()
    destination = str(destination).strip().lower()
    timezone_name = str(timezone_name).strip()
    local_time = str(local_time).strip()
    local_directory = str(local_directory).strip()

    if frequency not in FREQUENCIES:
        raise BackupPolicyError("frequency must be hourly, daily or interval")
    if destination not in DESTINATIONS:
        raise BackupPolicyError("destination must be local, telegram or both")
    if len(timezone_name) > 100:
        raise BackupPolicyError("timezone name is too long")
    try:
        ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        raise BackupPolicyError("timezone must be a valid IANA timezone") from None
    if not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", local_time):
        raise BackupPolicyError("local_time must use HH:MM in 24-hour format")
    if isinstance(interval_minutes, bool) or not isinstance(interval_minutes, int) or not 15 <= interval_minutes <= 10080:
        raise BackupPolicyError("interval_minutes must be between 15 and 10080")
    if not local_directory or len(local_directory) > 500:
        raise BackupPolicyError("local_directory is required and must be at most 500 characters")
    path = Path(local_directory)
    if not path.is_absolute() or ".." in path.parts or str(path) != local_directory:
        raise BackupPolicyError("local_directory must be a normalized absolute path without '..'")
    if str(path) == "/" or path == Path.home():
        raise BackupPolicyError("local_directory cannot be a filesystem root or home directory")
    if isinstance(telegram_topic_id, bool) or not isinstance(telegram_topic_id, int) or telegram_topic_id < 0:
        raise BackupPolicyError("telegram_topic_id must be a non-negative integer")
    if destination in {"telegram", "both"}:
        if isinstance(telegram_chat_id, bool) or not isinstance(telegram_chat_id, int) or telegram_chat_id == 0:
            raise BackupPolicyError("Telegram destination requires a non-zero numeric chat ID")
    elif telegram_chat_id is not None and (isinstance(telegram_chat_id, bool) or not isinstance(telegram_chat_id, int) or telegram_chat_id == 0):
        raise BackupPolicyError("telegram_chat_id must be a non-zero integer when supplied")
    if isinstance(retention_count, bool) or not isinstance(retention_count, int) or not 1 <= retention_count <= 1000:
        raise BackupPolicyError("retention_count must be between 1 and 1000")
    if isinstance(retention_days, bool) or not isinstance(retention_days, int) or not 1 <= retention_days <= 3650:
        raise BackupPolicyError("retention_days must be between 1 and 3650")
    return {
        "frequency": frequency, "timezone_name": timezone_name, "local_time": local_time,
        "interval_minutes": interval_minutes, "destination": destination,
        "local_directory": local_directory, "telegram_chat_id": telegram_chat_id,
        "telegram_topic_id": telegram_topic_id, "retention_count": retention_count,
        "retention_days": retention_days,
    }


def next_scheduled_run(*, now: datetime, frequency: str, timezone_name: str,
                       local_time: str, interval_minutes: int) -> datetime:
    """Compute the next run in UTC; caller persists it and owns scheduler lifecycle."""
    if now.tzinfo is None:
        raise BackupPolicyError("now must be timezone-aware")
    if frequency not in FREQUENCIES:
        raise BackupPolicyError("unsupported backup frequency")
    try:
        zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        raise BackupPolicyError("invalid timezone") from None
    utc_now = now.astimezone(timezone.utc)
    if frequency == "interval":
        if isinstance(interval_minutes, bool) or not isinstance(interval_minutes, int) or not 15 <= interval_minutes <= 10080:
            raise BackupPolicyError("interval_minutes must be between 15 and 10080")
        return utc_now + timedelta(minutes=interval_minutes)
    local_now = utc_now.astimezone(zone)
    if frequency == "hourly":
        candidate = local_now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    elif frequency == "daily":
        try:
            hour, minute = (int(part) for part in local_time.split(":"))
            target = daytime(hour, minute)
        except (ValueError, AttributeError):
            raise BackupPolicyError("local_time must use HH:MM") from None
        candidate = datetime.combine(local_now.date(), target, tzinfo=zone)
        if candidate <= local_now:
            candidate = datetime.combine(local_now.date() + timedelta(days=1), target, tzinfo=zone)
        # Wall-clock times can be skipped during a DST spring-forward. Advance to
        # the first valid local minute instead of persisting an imaginary time.
        for _ in range(181):
            round_trip = candidate.astimezone(timezone.utc).astimezone(zone)
            if round_trip.replace(tzinfo=None) == candidate.replace(tzinfo=None):
                break
            candidate += timedelta(minutes=1)
        else:
            raise BackupPolicyError("could not resolve local schedule time")
        if candidate <= local_now:
            candidate = datetime.combine(local_now.date() + timedelta(days=1), target, tzinfo=zone)
    else:
        raise BackupPolicyError("unsupported backup frequency")
    return candidate.astimezone(timezone.utc)
