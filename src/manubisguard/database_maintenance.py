#!/usr/bin/env python3
"""Non-destructive SQLite backup/verification/restore for MANUBIS Bot 2.

Restore always writes to a NEW destination. It never replaces the configured/live DB.
External vault/encryption keys are deliberately not copied by this utility.
"""
from __future__ import annotations

import argparse
import json
from contextlib import closing
import os
import sqlite3
import stat
import tempfile
from pathlib import Path
from urllib.parse import quote


DEFAULT_DB = os.environ.get("ANUBIS_CUSTOMER_DB", "/var/lib/anubis-bot/customer.sqlite3")


class DatabaseMaintenanceError(RuntimeError):
    pass


def _source_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if path.is_symlink() or not path.is_file():
        raise DatabaseMaintenanceError("Source database must be an existing regular, non-symlink file")
    return path.resolve(strict=True)


def _destination_path(value: str | Path, source: Path) -> Path:
    path = Path(value).expanduser()
    if not path.parent.is_dir():
        raise DatabaseMaintenanceError("Destination parent directory must already exist")
    resolved_parent = path.parent.resolve(strict=True)
    target = resolved_parent / path.name
    if target == source:
        raise DatabaseMaintenanceError("Source and destination must be different paths")
    if path.exists() or path.is_symlink():
        raise DatabaseMaintenanceError("Destination already exists; refusing to overwrite")
    return target


def _connect_readonly(path: Path) -> sqlite3.Connection:
    uri = "file:" + quote(str(path), safe="/") + "?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=15)


def verify_database(path: str | Path) -> dict[str, object]:
    source = _source_path(path)
    try:
        with closing(_connect_readonly(source)) as db:
            integrity = db.execute("PRAGMA integrity_check").fetchone()
            if not integrity or integrity[0] != "ok":
                raise DatabaseMaintenanceError("SQLite integrity_check failed")
            violations = db.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise DatabaseMaintenanceError(f"SQLite foreign_key_check found {len(violations)} violation(s)")
            version_table = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
            ).fetchone()
            migration_count = 0
            if version_table:
                migration_count = int(db.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0])
    except sqlite3.DatabaseError as exc:
        raise DatabaseMaintenanceError(f"SQLite verification failed ({type(exc).__name__})") from None
    return {"ok": True, "integrity": "ok", "foreign_key_violations": 0,
            "migration_records": migration_count, "size_bytes": source.stat().st_size}


def _snapshot(source: Path, destination: Path) -> dict[str, object]:
    destination = _destination_path(destination, source)
    temp_path: Path | None = None
    try:
        fd, name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp",
                                    dir=str(destination.parent))
        temp_path = Path(name)
        os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
        os.close(fd)
        with closing(_connect_readonly(source)) as src, closing(sqlite3.connect(str(temp_path), timeout=15)) as dst:
            src.backup(dst)
            dst.commit()
            dst.execute("PRAGMA foreign_keys=ON")
            integrity = dst.execute("PRAGMA integrity_check").fetchone()
            violations = dst.execute("PRAGMA foreign_key_check").fetchall()
            if not integrity or integrity[0] != "ok":
                raise DatabaseMaintenanceError("Snapshot failed SQLite integrity_check")
            if violations:
                raise DatabaseMaintenanceError(f"Snapshot has {len(violations)} foreign-key violation(s)")
        os.chmod(temp_path, stat.S_IRUSR | stat.S_IWUSR)
        with temp_path.open("rb") as handle:
            os.fsync(handle.fileno())
        # Hard-link is atomic and fails if another process created destination meanwhile.
        os.link(temp_path, destination)
        temp_path.unlink()
        temp_path = None
        dir_fd = os.open(destination.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
        result = verify_database(destination)
        result.update({"created": True, "mode": oct(stat.S_IMODE(destination.stat().st_mode))})
        return result
    except sqlite3.DatabaseError as exc:
        raise DatabaseMaintenanceError(f"SQLite snapshot failed ({type(exc).__name__})") from None
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass


def backup_database(source: str | Path, destination: str | Path) -> dict[str, object]:
    src = _source_path(source)
    verify_database(src)
    return _snapshot(src, Path(destination).expanduser())


def restore_database(backup: str | Path, destination: str | Path) -> dict[str, object]:
    src = _source_path(backup)
    verify_database(src)
    result = _snapshot(src, Path(destination).expanduser())
    result["restored_to_new_path"] = True
    result["live_database_replaced"] = False
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    backup = sub.add_parser("backup", help="create a consistent SQLite snapshot without overwriting")
    backup.add_argument("--source", default=DEFAULT_DB)
    backup.add_argument("--destination", required=True)
    verify = sub.add_parser("verify", help="check SQLite integrity and foreign keys")
    verify.add_argument("--path", required=True)
    restore = sub.add_parser("restore", help="restore a verified snapshot to a new path")
    restore.add_argument("--backup", required=True)
    restore.add_argument("--destination", required=True)
    args = parser.parse_args()
    try:
        if args.action == "backup":
            result = backup_database(args.source, args.destination)
        elif args.action == "verify":
            result = verify_database(args.path)
        else:
            result = restore_database(args.backup, args.destination)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (DatabaseMaintenanceError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
