# MANUBIS Bot 2 — SQLite backup and restore

The helper `scripts/customer_db_maintenance.py` creates a consistent SQLite snapshot using Python's SQLite online-backup API. It does not copy a live database file byte-for-byte.

## Safety guarantees

- `backup` and `restore` refuse to overwrite an existing destination.
- The destination directory must already exist. The utility does not create or change operational directories.
- Temporary and final database files are owner-readable/writable only (`0600`).
- The output is checked with `PRAGMA integrity_check` and `PRAGMA foreign_key_check`.
- `restore` only writes to a new path. It never replaces `ANUBIS_CUSTOMER_DB` or restarts a service.
- Symlink sources and invalid SQLite files are rejected.
- The utility prints status metadata, not database rows or secrets.

## Commands

Back up the configured customer DB (or pass `--source`) to an existing, access-restricted backup directory:

```bash
ANUBIS_CUSTOMER_DB=/path/to/customer.sqlite3 python3 scripts/customer_db_maintenance.py backup --destination /secure/backups/customer-$(date -u +%Y%m%dT%H%M%SZ).sqlite3
```

Verify an existing snapshot:

```bash
python3 scripts/customer_db_maintenance.py verify --path /secure/backups/customer-backup.sqlite3
```

Restore into a new path for inspection and acceptance; the destination parent must already exist:

```bash
python3 scripts/customer_db_maintenance.py restore --backup /secure/backups/customer-backup.sqlite3 --destination /secure/restore/customer-restored.sqlite3
```

A successful restore reports `restored_to_new_path=true` and `live_database_replaced=false`. Application cutover is deliberately not automated. Verify the restored file, compare migration history and application-specific records, and plan any later cutover as a separately authorized maintenance operation.

## What this does not back up

This utility handles one SQLite database per invocation. The package-level `run_backup_once` worker can be called by a future explicitly authorized manual runner; it verifies that the policy path stays beneath an approved private root, records the outcome and enqueues a redacted `backup` notification. It is not exposed as a bot command or scheduler, supports only the local destination, and never performs retention cleanup. Telegram/both policies fail closed until upload support is separately implemented and tested.

This utility does not back up other SQLite databases, external files/uploads, environment configuration, bot tokens, panel secrets, or encryption keys. Secret-store/vault encryption keys require a separately protected backup and a documented recovery/rotation procedure; a database restore without the correct key may leave encrypted credentials unrecoverable. Never place the key beside a database backup or in the same public/off-host bucket without independent access controls.

## Evidence and operational limits

Tests use disposable SQLite fixtures only. They cover consistent backup, row preservation, restore-to-new-path, mode `0600`, overwrite prevention, symlink rejection, integrity checking and foreign-key validation. This is local E2 evidence, not a production-shaped restore drill. No operational DB was opened, no backup of a live database was made, and no service was restarted or deployed.