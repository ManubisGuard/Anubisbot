"""Explicit one-shot backup execution; never starts a scheduler or performs retention cleanup."""
from __future__ import annotations

import os
import re
import stat
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from manubisguard.database_maintenance import DatabaseMaintenanceError, backup_database, verify_database
from manubisguard.customer_store import CustomerDataStore, CustomerStoreError


class BackupExecutionError(RuntimeError):
    """A backup could not be safely completed; exposes only a fixed reason code."""

    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        super().__init__(reason_code)


def _secure_backup_directory(policy_directory: str, approved_root: str | Path) -> Path:
    root_input = Path(approved_root)
    if not root_input.is_absolute() or root_input.is_symlink() or not root_input.is_dir():
        raise BackupExecutionError("UnsafeBackupRoot")
    current_root = Path(root_input.anchor)
    for component in root_input.parts[1:]:
        current_root = current_root / component
        if current_root.is_symlink():
            raise BackupExecutionError("SymlinkBackupRoot")
    root = root_input.resolve(strict=True)
    root_stat = root.stat()
    if root_stat.st_uid != os.geteuid() or stat.S_IMODE(root_stat.st_mode) & 0o077:
        raise BackupExecutionError("UnsafeBackupPermissions")
    requested = Path(policy_directory)
    if (not requested.is_absolute() or ".." in requested.parts or str(requested) != policy_directory):
        raise BackupExecutionError("UnsafeBackupPath")
    try:
        relative = requested.relative_to(root)
    except ValueError:
        raise BackupExecutionError("UnsafeBackupPath") from None
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise BackupExecutionError("SymlinkBackupPath")
        if current.exists():
            if not current.is_dir():
                raise BackupExecutionError("UnsafeBackupPath")
        else:
            current.mkdir(mode=0o700)
        info = current.stat()
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise BackupExecutionError("UnsafeBackupPermissions")
    return current


def run_backup_once(store: CustomerDataStore, source_db: str | Path, *,
                    approved_root: str | Path, now: float | None = None,
                    run_id: str | None = None,
                    backup_fn: Callable[[str | Path, str | Path], dict[str, Any]] = backup_database) -> dict[str, Any]:
    """Create one verified local snapshot and atomically record a redacted backup event.

    This is intentionally manual-call-only. It does not consult or enable a timer,
    upload to Telegram, or delete old backups. Telegram/both destinations fail closed
    until a separately tested uploader exists.
    """
    policy = store.get_backup_policy()
    if not policy.get("updated_at"):
        raise BackupExecutionError("BackupPolicyNotConfigured")
    timestamp = time.time() if now is None else float(now)
    actual_run_id = run_id or uuid.uuid4().hex
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", actual_run_id):
        raise BackupExecutionError("InvalidRunId")
    destination = str(policy.get("destination") or "")
    target: Path | None = None
    backup_attempted = False
    try:
        if destination != "local":
            raise BackupExecutionError("TelegramUploadNotImplemented")
        directory = _secure_backup_directory(str(policy.get("local_directory") or ""), approved_root)
        source = Path(source_db)
        if source.is_symlink() or not source.is_file():
            raise BackupExecutionError("SourceUnavailable")
        source = source.resolve(strict=True)
        stamp = datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        target = directory / f"customer-backup-{stamp}-{actual_run_id[:12]}.sqlite3"
        if target.exists() or target.is_symlink():
            raise BackupExecutionError("BackupAlreadyExists")
        backup_attempted = True
        backup_result = backup_fn(source, target)
        if not isinstance(backup_result, dict) or backup_result.get("ok") is not True:
            raise BackupExecutionError("BackupVerificationFailed")
        try:
            verified = verify_database(target)
        except Exception:
            raise BackupExecutionError("BackupVerificationFailed") from None
        actual_mode = oct(stat.S_IMODE(target.stat().st_mode))
        if (verified.get("ok") is not True or verified.get("integrity") != "ok"
                or verified.get("foreign_key_violations") != 0 or actual_mode != "0o600"):
            raise BackupExecutionError("BackupVerificationFailed")
        result = {**verified, "created": True, "mode": actual_mode}
        size_bytes = int(verified["size_bytes"])
    except Exception as exc:
        if backup_attempted and target is not None and (target.exists() or target.is_symlink()):
            try:
                target.unlink()
            except OSError:
                pass
        if isinstance(exc, BackupExecutionError):
            reason_code = exc.reason_code
        elif isinstance(exc, DatabaseMaintenanceError):
            reason_code = "DatabaseMaintenanceError"
        elif isinstance(exc, CustomerStoreError):
            reason_code = "StoreStateError"
        elif isinstance(exc, OSError):
            reason_code = "FilesystemError"
        else:
            reason_code = "BackupError"
        try:
            store.record_backup_run(run_id=actual_run_id, status="failed", destination=destination,
                                    size_bytes=None, reason_code=reason_code, now=timestamp)
        except Exception:
            # Never echo paths, exception text, credentials or database contents.
            pass
        raise BackupExecutionError(reason_code) from None
    try:
        store.record_backup_run(run_id=actual_run_id, status="success", destination=destination,
                                size_bytes=size_bytes, reason_code=None, now=timestamp)
    except Exception:
        # Keep a verified backup if only status/outbox persistence failed.
        raise BackupExecutionError("StatusPersistenceFailed") from None
    return {**result, "run_id": actual_run_id, "destination": destination,
            "path": str(target), "size_bytes": size_bytes, "status": "success"}
