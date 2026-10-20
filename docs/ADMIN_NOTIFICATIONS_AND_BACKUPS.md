# MANUBIS Bot 2 — Admin Notification Routing and Backup Policy

## Current implementation boundary (2026-10-20)

- SQLite migration v6 adds a durable support-ticket notification outbox. Mini App ticket creation and notification enqueue happen in the same DB transaction.
- SQLite migration v7 adds tenant-scoped customer_notification_routes, with multiple numeric Telegram chat IDs per event and optional forum topic IDs.
- SQLite migration v8 adds a persisted, tenant-scoped backup policy (frequency, timezone, destination, local path, Telegram chat/topic, retention) with `enabled=0` by default. Admins can configure/view it using `/backup-policy`; `/backup-policy enable` remains deliberately rejected because the scheduler, durable leases/recovery, Telegram upload and retention acceptance are incomplete.
- SQLite is configured for same-host WAL and `synchronous=FULL`, with foreign keys and a 15-second busy timeout. WAL must not be used on a network filesystem.
- Admin commands:
  - /notify-route list
  - /notify-route add support_ticket | CHAT_ID | TOPIC_ID
  - /notify-route remove support_ticket | CHAT_ID | TOPIC_ID
  - Use topic ID 0 for a chat without a forum topic. Group chat IDs are usually negative. The bot must be a member and have permission to send messages.
- Ticket delivery uses configured support_ticket routes; if none exist, it falls back to MANUBISGUARD_CUSTOMER_ADMIN_IDS. The outbox retries with bounded exponential backoff, recovers stale claims and dead-letters after repeated failures.
- Route keys currently registered: support_ticket, purchase, payment, service_test, bot_log, backup, wallet, service_change, security_alert.
- Support-ticket delivery and the package-level one-shot backup worker now enqueue events through the route registry. The backup worker is not wired to an admin command or scheduler; `bot_log` and the other non-ticket event producers remain incomplete. Do not tell admins purchase/payment/log events are already routed.

## Required event routing scope

Every event producer should call one shared, tenant-scoped notification dispatcher. Each event may target one or more destinations, each represented by numeric chat_id and optional positive message_thread_id. Admin commands/UI should support list/add/remove/enable/disable/test-send, validate chat/topic values, preserve a safe fallback and record delivery outcomes.

Event groups to wire:
- support_ticket: new tickets, staff replies/reopen/escalation where configured.
- purchase: completed purchase/order reports, with sensitive data redacted.
- payment: submitted receipts, manual review decisions, verified/failed payment outcomes; never claim a manual receipt is automatically verified.
- wallet: deposits, credits, refunds and balance corrections; never include credentials or full payment secrets.
- service_test: customer-requested service tests/diagnostics and result summaries.
- service_change: renewal, time extension, traffic top-up and reconciliation outcomes.
- bot_log: operational warnings/errors and health summaries; no tokens, passwords, initData, subscription URLs or personal payload dumps.
- security_alert: auth abuse, repeated failures and privileged configuration changes.
- backup: success/failure, size, destination and last/next run metadata; never include the encryption key.

## Backup scheduling and destination policy (required; not yet implemented)

The admin must be able to configure and inspect:
- schedule: disabled by default; hourly/daily/custom interval and local timezone, with visible last-run and next-run times;
- destination: local server directory, Telegram document upload to configured chat/topic, or both;
- retention: local count/age policy and explicit cleanup preview before destructive deletion;
- validation: SQLite online backup, integrity check, foreign-key check, restrictive file permissions, no overwrite of an existing backup path, and restore-to-new-path drills;
- encryption: protect backups at rest where required, store key separately, and document independent key recovery; never send keys with backup files;
- notifications: success/failure summaries through the backup event route and retry/dead-letter state for Telegram delivery;
- controls: dry-run/preview, pause/resume, manual backup, verify backup, and restore drill that cannot overwrite the live database.

No timer, cron, production backup, Telegram upload, or cleanup job may be enabled until explicitly authorized. The existing scripts/customer_db_maintenance.py backup/verify/restore-to-new-path helper is a manual local tool. The package-level run_backup_once service is tested with disposable databases but is not exposed through a bot command or scheduler; it supports only local snapshots beneath an explicitly approved private directory. Telegram/both destinations fail closed, and no retention cleanup is performed.

## Acceptance checklist

- tenant isolation and admin-only mutations for route and schedule settings;
- multiple destinations and forum topic routing validated with mocked Telegram API calls;
- route change survives process/store reopen;
- failed delivery retries with bounded backoff, stale processing claims recover, dead letters are visible and can be requeued;
- no sensitive payload in logs or reports;
- backup schedule is disabled on clean install and after migration unless explicitly configured;
- backup delivery and local retention are independently configurable;
- failed backup never deletes the prior known-good backup;
- restore test writes only to a new path and confirms integrity/FK checks;
- test suite, schema upgrade/reopen, static checks and a documented E2/E3/E4 evidence level for each stage.

Current evidence: local unit/mock tests only (E2). No live Telegram destination was contacted, no timer was enabled, and no operational database was backed up.

## Admin backup-policy commands (configuration only)

- `/backup-policy show` — view the stored per-tenant policy and a next-run preview. The preview is not an active timer.
- `/backup-policy schedule | hourly/daily/interval | Europe/Berlin | 03:00 | 1440` — configure frequency, IANA timezone, daily wall-clock time and interval length.
- `/backup-policy destination | local/telegram/both | CHAT_ID-or-0 | TOPIC_ID | /var/lib/anubis-bot/backups` — configure destination and path. Telegram/both requires a non-zero numeric chat ID; topic 0 means no forum topic.
- `/backup-policy retention | MAX_BACKUPS | MAX_AGE_DAYS` — configure retention bounds.
- `/backup-policy enable` is deliberately rejected. Settings remain disabled until a scheduler with durable leases/restart recovery, retention preview, tested Telegram upload limits and restore acceptance are complete. The one-shot local worker is not a scheduler; no scheduled work is running.

Schedule validation uses IANA timezone data and stores policy per tenant. The one-shot local worker additionally requires the configured path to remain beneath an explicitly supplied approved root, rejects symlink components and requires current-user ownership plus private directory permissions. The scheduler still does not exist, and no production backup has been run.
