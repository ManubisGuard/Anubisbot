# MANUBIS Bot 2 — Admin Notification Routing and Backup Policy

## Current implementation boundary (2026-10-20)

- SQLite migrations v6/v7 provide durable ticket notifications and tenant-scoped event destinations (numeric Telegram chat IDs plus optional forum topic IDs). Migration v9 adds a shared per-destination admin notification outbox with idempotency, retry/dead-letter states, claim leases and redacted payload allowlists.
- Migration v8 persists a tenant-scoped backup policy (frequency, timezone, destination, local path, Telegram chat/topic, retention). It remains disabled by default; `/backup-policy enable` is rejected because a durable scheduler is not implemented.
- The shared notification outbox is wired locally for wallet-ledger changes, verified wallet deposit/payment, customer purchases, manual product purchases, refunds, service-change reserve/complete/pre-provider-refund, admin panel checks (`service_test`), security alerts, and the one-shot backup worker. `bot_log` remains unwired. These are E2 local/mock results, not live Telegram delivery acceptance.
- Admin commands include `/notify-route list`, `/notify-route add EVENT_KEY | CHAT_ID | TOPIC_ID`, and `/notify-route remove EVENT_KEY | CHAT_ID | TOPIC_ID`; topic ID 0 means a direct chat. The bot must be a member and have send permission. Existing ticket fallback to `MANUBISGUARD_CUSTOMER_ADMIN_IDS` remains documented; verify actual route behavior in tests before changing it.
- The shared notification outbox retries with bounded exponential backoff, recovers stale claims and dead-letters repeated failures. Telegram delivery is at-least-once: a crash after Telegram accepts a send but before local success persistence may duplicate a notification.
- SQLite uses same-host WAL, `synchronous=FULL`, foreign keys and a 15-second busy timeout. WAL must not be used on a network filesystem.

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

## Backup scheduling and destination policy (partially implemented; E2 local only)

Implemented and locally tested:
- Admin-editable persisted schedule/timezone/destination/retention policy with disabled-by-default state and next-run preview.
- Manual CLI backup, verify, and restore-to-new-path using SQLite's online backup API, integrity and foreign-key checks, restrictive file permissions, overwrite/symlink refusal and no live DB cutover.
- Package-level `run_backup_once` creates one verified local snapshot beneath an explicitly approved private root, records a redacted run result and enqueues a backup notification. Failed/unverified snapshots are not recorded as success; a verified snapshot is retained if only status persistence fails.

Still missing / not accepted:
- A durable scheduler with persisted due times, leases, restart recovery, pause/resume and last/next run execution status.
- Telegram document upload; `telegram`/`both` destinations fail closed.
- Retention preview and cleanup worker, off-host backup and encryption-key recovery drill.
- Bot command/UI to trigger the one-shot worker, dry-run/manual execution workflow and production-shaped restore drill.

No timer, cron, production backup, Telegram upload or cleanup job may be enabled until explicitly authorized. `run_backup_once` is a library-level one-shot operation, not a scheduler or live acceptance.

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
- `/backup-policy enable` is deliberately rejected. Settings remain disabled until a scheduler with durable leases/restart recovery, retention preview, tested Telegram upload limits and restore acceptance are complete. No scheduled work is running.

Schedule validation uses IANA timezone data and stores policy per tenant. The one-shot local worker requires the configured path to remain beneath an explicitly supplied approved root, rejects symlink components, and requires current-user ownership plus private directory permissions. No production backup has been run.
