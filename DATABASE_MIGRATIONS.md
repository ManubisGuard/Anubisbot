# SQLite schema migration status

## Implemented in the current worktree

- `CustomerDataStore._initialize()` creates the database-wide `schema_migrations` table.
- Migration version 1 consolidates the prior additive column/index changes, including a previously duplicated `reseller_accounts` dictionary key that caused one set of legacy columns to be omitted from that mapping.
- Migration DDL/DML and the version record execute in one `BEGIN IMMEDIATE` transaction; failure rolls the migration transaction back.
- Reopening an already migrated database does not reapply version 1.
- Startup fails closed if a recorded schema version exceeds the version supported by this code.
- A unit test verifies version recording, repeat initialization, and `PRAGMA foreign_key_check`.

## Limits and follow-up

Migration v2 (`service_change_operations_and_audit`) creates or upgrades `customer_service_operations`, creates `customer_service_operation_events`, and installs append-only update/delete triggers for the event log. It preserves the existing additive columns for databases that already have the operations table. The service-operation methods still retain defensive `CREATE TABLE IF NOT EXISTS` and additive-column checks for compatibility; those lazy paths are not yet removed. Manual shop, content and notification outbox setup remain outside the versioned registry, so this is not complete schema migration coverage.

The migration was validated only on isolated test databases. No production or staging database was migrated, and no production backup or restore was run. Before deployment, test upgrades from representative prior schema snapshots and verify an independent backup/restore procedure.

## Isolated migration acceptance tests

The isolated test suite now includes a representative unversioned legacy snapshot built in a temporary SQLite file. It seeds and verifies customer-wallet ledger, order, payment-attempt and service-operation records while upgrading missing core/service-operation columns, the migration index, audit table and append-only triggers. It checks migration version rows, idempotent reopen, `PRAGMA foreign_key_check`, the audit table's composite tenant/operation foreign key, and rejection of a cross-tenant audit event.

A deterministic SQLite trigger injects a failure when migration v1 records its version, after migration DDL/DML has begun. The regression test verifies the transaction rolls back added columns, the idempotency index and the version row while retaining seeded wallet data. A separate test asserts an unsupported future schema version fails closed.

These are disposable local SQLite/unit tests only. The legacy fixture is synthesized from the current schema by removing migration-owned additions; it is not a byte-for-byte historical production backup. No staging or production database was accessed, and this does not validate application backup/restore or deployment rollback.


## Independent audit addendum — 2026-10-20 (Session 78)

### Current source verification
- The current `CustomerDataStore._initialize()` registers v1 `core_additive_legacy_migrations` and v2 `service_change_operations_and_audit`.
- v1 is applied in a `BEGIN IMMEDIATE` transaction with its migration row; it adds core legacy columns, repairs the duplicate `reseller_accounts` migration map and creates the reseller idempotency index.
- v2 creates or additively upgrades `customer_service_operations`, creates `customer_service_operation_events`, uses tenant-scoped `(tenant_id, operation_id)` foreign-key/unique constraints, and installs triggers that reject UPDATE/DELETE of audit events.
- `tests/test_customer_store.py::test_core_schema_migration_is_versioned_and_reopening_is_idempotent` currently asserts versions 1 and 2 and checks a reopened DB; `test_service_operation_read_paths_migrate_legacy_table_additively` covers a legacy operation table. Full isolated suite now passed **227 tests in 59.683 seconds, exit 0**. `compileall` and `git diff --check` both exited 0.
- This remains E2 local isolated evidence only.

### Confirmed gaps
- Feature-specific DDL remains outside the versioned registry: manual shop tables, manual product-order event table, and notification outbox setup. Service-operation lazy schema creation/column checks remain as compatibility fallbacks.
- Current tests do not yet prove migration rollback under deterministic injected failure or upgrade from a representative complete pre-versioned database containing customer, wallet, order, payment and service-operation data.
- Unsupported future versions are rejected by code, but migration-gap/corrupt-history behavior is not comprehensively covered.
- The SQLite `Connection.backup()` test is a temporary-database round trip, not an application backup command, production-shaped restore, off-host backup, filesystem consistency check or encryption-key recovery.
- No staging/production DB was migrated. Do not run migration against an operational database as part of this task.

### P0 acceptance task: AUD-DB-01A
Build isolated legacy snapshots with representative seeded rows; assert data preservation, version records, columns/indexes/triggers, tenant-scoped FK behavior and `PRAGMA foreign_key_check`; test idempotent reopen and unsupported future version; inject a deterministic failure and prove rollback leaves no partially committed migration. Modify runtime code only if a reproducible defect is found. Do not deploy or touch live databases.


## Session 79 verification addendum — AUD-DB-01A (2026-10-20)

- Added isolated regression coverage for representative unversioned legacy data preservation, deterministic v1 migration failure/rollback, and unsupported future schema versions.
- Final `./scripts/run_isolated_tests.sh`: **230 tests passed in 65.364s (OK)**. `compileall` and `git diff --check` passed.
- The legacy snapshot is synthesized from the current schema by removing migration-owned additions and is not a historical production backup. Evidence remains local E2 only; no staging/production database was accessed.
- Runtime migration code was unchanged. Next: inventory/test the manual-shop lazy-schema family before considering its own small migration increment.


## Session 80 — AUD-DB-01B: manual-shop schema (in progress)

- Added migration v3 `manual_shop_tables` to create `manual_products`, `manual_product_inventory`, `manual_product_orders`, and `manual_product_order_events` within the existing `BEGIN IMMEDIATE` migration transaction. Existing lazy-created tables are preserved by `CREATE TABLE IF NOT EXISTS`.
- Raised the supported schema-version ceiling to 3 and updated version/idempotent-reopen and legacy migration assertions.
- The three targeted tests for versioned migration/reopen, representative legacy migration, and manual-order fulfillment passed (3 tests, exit 0).
- The compatibility lazy setup helper still exists as a defensive fallback; a follow-up should remove its DDL only after migration coverage explicitly proves upgrade from a database where all four manual-shop tables are absent. Full suite and compile/diff checks are still pending.


### Final verification — AUD-DB-01B (2026-10-20)

- Full isolated test discovery: **231 tests passed in 71.021 seconds (OK)**. `python3 -m compileall -q -f src tests scripts` and `git diff --check` both exited 0.
- Added test coverage for migration v3 idempotency, preservation of an existing manual-product row, recreation of all four manual-shop tables when absent, and `PRAGMA foreign_key_check`.
- Migration v3 is transactionally versioned, but the old lazy `CREATE TABLE IF NOT EXISTS` fallback remains in the manual-shop helper and fulfillment method as compatibility defense. The next small cleanup can remove these duplicate DDL paths after reviewing legacy database expectations; no production/staging DB, deployment, restart, stage, commit, or push was performed.


### AUD-DB-01B — final debug pass (2026-10-20)

- Removed the manual-shop lazy DDL from `_ensure_manual_shop_tables`; it is now a compatibility shim only. Removed the fulfillment-path DDL for `manual_product_order_events`, so that table is owned by migration v3.
- Added SQLite `BEFORE UPDATE` and `BEFORE DELETE` triggers for `manual_product_order_events`; trigger creation runs on every initialization so previously migrated databases also receive the audit hardening.
- Regression coverage now proves manual-order audit events reject UPDATE/DELETE. Targeted tests: 3/3 passed. Full isolated suite: **231 tests passed in 72.027 seconds**. `python3 -m compileall -q -f src tests scripts` and `git diff --check` passed.
- Branch/HEAD unchanged (`feature/discount-codes`, `e7528db16aa5de69adf53855b99090de8ba60f49`). Existing unrelated/untracked work was preserved. No staging, commit, push, production/staging DB access, deployment, or service restart.

### AUD-DB-01C — editable bot content schema (2026-10-20)

- Added a dedicated `content_schema_migrations` registry for the separate content database. Version 1 (`bot_content_namespace_overrides`) adopts the existing `bot_content` table using `CREATE TABLE IF NOT EXISTS`, preserving namespace/key/value overrides already stored by older versions.
- Migration DDL and its version row run inside one `BEGIN IMMEDIATE` transaction. Unsupported future content schema versions fail closed.
- Added isolated tests for legacy-row preservation and idempotent reopen, deterministic failure/rollback, and future-version rejection. Targeted `test_content_store.py`: 8 tests passed.
- This migration covers only `BotContentStore`'s database; notification outbox setup remains a separate scope. No production/staging database, deployment, restart, stage, commit, or push was performed.

### AUD-DB-01D — notification outbox schema (2026-10-20)

- Added a separate `notification_schema_migrations` registry for the outbox table in the customer database. Version 1 (`notification_outbox_and_due_index`) creates or adopts the existing outbox, adds `claimed_at` to legacy tables when absent, and creates the due-work index inside one `BEGIN IMMEDIATE` transaction.
- Existing outbox rows are preserved. Reopening is idempotent, injected migration failure rolls back table/index/version changes, and unknown future versions fail closed.
- Targeted outbox tests: 14 passed. This is isolated SQLite test evidence only; no production/staging DB was accessed, and no deploy/restart/stage/commit/push was performed.

### AUD-DB-01C/01D — combined debug verification (2026-10-20)

- Final isolated suite after both content and outbox migrations: **237 tests passed in 76.925 seconds (OK, exit 0)**.
- `python3 -m compileall -q -f src tests scripts` and `git diff --check` both exited 0.
- Current branch/HEAD remain `feature/discount-codes` / `e7528db16aa5de69adf53855b99090de8ba60f49`. All work remains unstaged and uncommitted. No production/staging DB access, deployment, or service restart occurred.


### AUD-DB-01E — remove remaining service-operation lazy DDL (2026-10-20)

- Removed duplicate `customer_service_operations` creation/column-upgrade logic from the reservation path and removed lazy `customer_service_operation_events` creation from reconciliation finalization. Read/list paths no longer perform schema `ALTER TABLE` or silently mask missing tables; `CustomerDataStore._initialize()` owns those schemas.
- Updated the legacy-upgrade regression test to reopen the store and exercise migrations rather than relying on read-time DDL. Added a regression test proving append-only audit triggers are reinstalled when an existing v2 database is opened.
- Targeted customer-store suite passed 48 tests before the new trigger-reinstallation test; full isolated suite passed 237 tests in 68.008s before that final test addition. A final full-suite run is required to validate the new regression test.
- Updated `docs/SQLITE_SCHEMA_MIGRATIONS.md` and README verification notes. No stage/commit/push/deploy/restart or operational database access.

### AUD-DB-01E — final full verification (2026-10-20)

- After adding the trigger-reinstallation regression test, `./scripts/run_isolated_tests.sh` passed **238 tests in 72.689 seconds (OK, exit 0)**.
- `python3 -m compileall -q -f src tests scripts`, `git diff --check`, shell syntax checks (`bash -n scripts/*.sh`), and the inline Mini App JavaScript syntax check (`node --check`) passed.
- Current branch/HEAD remain `feature/discount-codes` / `e7528db16aa5de69adf53855b99090de8ba60f49`. Changes remain unstaged and uncommitted. No production/staging DB access, deployment, or service restart occurred.


## Session 88 — manual-order replay snapshot migration (2026-10-20)

- Added customer-store schema migration v4, `manual_order_purchase_snapshot`, inside the existing `BEGIN IMMEDIATE` transaction. It adds immutable `product_title_snapshot` and `delivery_mode_snapshot` fields to manual orders and backfills existing rows from the matching product; if the product no longer exists, it falls back to the product ID and safe manual-delivery mode.
- New manual orders persist the title and delivery mode at purchase time. Idempotent replay now returns the original title/mode/payload/status rather than querying the possibly edited product or returning a row missing fields the Telegram handler expects.
- Duplicate customer callback UX now explicitly identifies a replay; fulfilled manual orders can return their committed delivery details. No second debit, inventory consumption or order is created on replay.
- Added regression tests for instant-product replay after product edits, inventory replay/no second debit, already-fulfilled manual order replay, legacy v4 backfill, and duplicate Telegram callback behavior.
- A regression test first failed before the fix with `KeyError: 'title'` on the duplicate order replay. The focused post-fix group passed 6 tests in 2.862 seconds.
- Full isolated suite and static checks are pending in this session. Evidence is E2 local isolated tests only. No live Telegram/panel/payment APIs, production/staging DB, service restart, deployment, GitHub write, stage or commit occurred.


### Session 88 final verification — 2026-10-20

- `./scripts/run_isolated_tests.sh`: **273 tests passed in 79.615 seconds (OK, exit 0)**.
- `python3 -m compileall -q -f src tests scripts`: passed.
- `bash -n scripts/*.sh`: passed.
- `git diff --check`: passed.
- Focused post-fix group: **6 tests passed in 2.862 seconds**. Before the fix, the new regression test failed with `KeyError: 'title'`, confirming the defect.
- Schema migration v4 is covered by fresh initialization/reopen, legacy snapshot backfill and existing migration regression tests. Evidence remains E2 isolated local SQLite/unit/mock tests only; no production-shaped DB, live API or staging acceptance is implied.
- No staging/production database, live Telegram/panel/payment API, deployment, service restart, stage, commit, push or GitHub write was performed.


### v4 legacy-data limitation

For orders created before v4, the original title/delivery mode was not persisted, so the migration can only backfill from the matching product's current row. If that product was edited before migration, the historical title/mode cannot be reconstructed with certainty; if the product is absent, the migration uses the product ID and the conservative manual-delivery mode. Orders created after v4 store the purchase-time snapshot exactly. This is a local schema migration, not proof of historical production-data recovery.


## Session 89 — manual fulfillment notification recovery + inventory delivery copy (2026-10-20)

- Re-read the required handoff/matrix/master prompt; re-verified host `206.1.97.165`, repo `/root/fork/AnubisBot`, branch `feature/discount-codes`, HEAD `e7528db16aa5de69adf53855b99090de8ba60f49`. Preserved existing dirty/untracked work.
- Test-first: inventory purchase confirmation test failed because `delivery_mode=inventory` was shown as `دستی توسط پشتیبانی`. Fixed copy so `instant` and `inventory` are automatic, while `manual` remains support-delivered.
- Injected simulated Telegram send failure after manual fulfillment commits. Verified order remains fulfilled, wallet stays debited once, and replaying the original purchase callback retrieves the original delivery without another debit. Improved admin failure notice with order ID and recovery guidance; log records order ID and exception class only.
- Focused tests: **4 passed**. Full isolated suite: **275 tests passed in 78.055s (OK)**. Compileall, `bash -n scripts/*.sh`, and `git diff --check` passed.
- Evidence E2 only. No live APIs, operational DB, restart/deploy, GitHub write, stage, commit or push.
- Remaining: recovery depends on the original callback remaining available; there is no dedicated durable notification outbox/retry path for manual fulfillment. Next task: implement and test that outbox/retry safely, including restart/stale-claim recovery and no duplicate order/debit.


## Session 90 — durable manual-delivery notification outbox (2026-10-20)

- Revalidated the existing Bot 2 workspace and preserved all dirty/untracked work. No stage/commit/push, live API, operational DB, deployment or service restart.
- Added schema migration v5 `manual_delivery_notification_outbox` and `manual_delivery_notifications`, with tenant/order uniqueness, due index, processing lease, attempts, sent/dead states and non-sensitive error code. Queue references the order and does not duplicate delivery secrets into a second payload column.
- `fulfill_manual_order` now writes fulfilled status, append-only staff event and outbox row in one transaction. If enqueue fails, the transaction rolls back and the order remains pending; the wallet debit already committed at purchase remains exactly once.
- Customer bot drains pending notifications after polling batches and immediately after fulfillment. Retries use exponential backoff, stale processing claims are recovered after 300 seconds, and 8 failures move an item to `dead`. Admin can requeue a dead item with `/manual-notification-retry ORDER_ID`.
- Local tests cover atomic enqueue, rollback on enqueue failure, send failure/retry, backoff/dead-letter, stale-claim recovery after reopen, and no second wallet debit. Focused group: 8 tests passed.
- Delivery guarantee is at-least-once: a crash after Telegram accepts the message but before the `sent` write can cause a duplicate notification; Telegram send has no idempotency key. Order fulfillment and wallet debit are not repeated.
- Evidence remains E2 local tests; the final full-suite and static-check results are recorded in the verification addendum below. No live Telegram acceptance is claimed.
- The inventory last-item concurrency and tenant-scoped notification queue tests are now covered by the final verification below. Next single task: reconcile the remaining acceptance matrix and choose the next concrete Bot 2 gap; automatic gateway/webhook, provider staging and authenticated Mini App work remain separate slices. Bot 1 stays out of scope.


### Session 90 final verification — 2026-10-20

- `./scripts/run_isolated_tests.sh`: **282 tests passed in 80.511 seconds (OK, exit 0)**. This includes v5 migration/reopen and legacy-schema coverage, atomic outbox enqueue and rollback, backoff/dead-letter/admin requeue, stale-claim recovery after reopen, tenant isolation, and concurrent last-inventory-item purchase.
- Focused group for outbox/fulfillment: **8 tests passed**; separate migration/concurrency focused group: **3 tests passed**.
- `python3 -m compileall -q -f src tests scripts`, `bash -n scripts/*.sh`, and `git diff --check`: passed.
- Evidence remains E2 local isolated SQLite/unit/mock only. No live Telegram/panel/payment API, operational database, deployment, service restart, stage, commit, push or GitHub write.
- Next single task: inspect remaining Bot 2 acceptance gaps and prioritize the next concrete slice. Automatic payment gateway/webhook remains missing; provider mutation staging acceptance, authenticated Mini App/backend, broader customer journey parity and production backup/restore remain unaccepted. Do not claim full Bot 2 production acceptance.


## Session 91 — cross-provider reconciliation and SQLite maintenance (2026-10-20)

- Confirmed target host/repo and current dirty worktree; preserved all existing tracked/untracked work. No stage/commit/push, live API, operational DB, deployment or service restart.
- `src/manubisguard/sales/service.py`: read-only `inspect_reconciliation()` now supports exactly PasarGuard, Rebecca and Sanaei using the normalized `get_user()` contract; provider identity and target/before value must still match. Unknown provider kinds fail closed. No provider mutation is retried.
- Test-first regression tests for Rebecca and Sanaei each failed before the fix with `provider_not_enabled`; after the fix both detect `target_present` after an injected post-mutation timeout. `PYTHONPATH=src:tests python3 -m unittest test_customer_service_change_service -v`: 12 tests passed.
- Added `scripts/customer_db_maintenance.py` with `backup`, `verify`, and `restore` to a new path only. Uses SQLite online backup, checks `integrity_check` and `foreign_key_check`, rejects symlink sources and overwrite/same-path attempts, creates final files with mode 0600, and does not perform live DB cutover.
- Added `tests/test_customer_db_maintenance.py` (5 tests) for backup/restore round-trip, data preservation, permissions, overwrite/symlink refusal, FK violations and invalid DB input; all 5 focused tests passed. Added `docs/CUSTOMER_DB_BACKUP_RESTORE.md` with copy/pasteable commands and key-recovery limits.
- `README.md` and `CUSTOMER_PRODUCT_ACCEPTANCE_MATRIX.md` updated: MAN-02 stale defect corrected, REC-01/SVC-02 reflects cross-provider local recovery, OPS-01 reflects new helper and remaining operational gaps. The current `webapp/` is confirmed to be the factory/Bot 1 preview and was intentionally not modified.
- Evidence remains E2 local unit/mock and temporary SQLite only. Provider API versions/permissions, staging mutation tests, authenticated customer Mini App/API, off-host backup, production-shaped restore and external encryption-key recovery remain unaccepted. Automatic gateway checkout/signed webhook stays last per user's explicit request.
- Full isolated suite and static checks are running; append exact results after completion. Next task: operation-by-operation provider contract audit and hardening using mocks, without live endpoints; record per-operation support and any remaining unsafe semantics.


## Session 92 — operation capability reports and reconciliation identity hardening (2026-10-20)

- Added shared `local_capability_report()` contract. Rebecca and Sanaei now expose per-operation status (`unknown` vs `unsupported`), whether a local adapter method exists, `panel_permission=unknown`, `live_test=not_tested`, and a generic configuration-validation result. The report does not call the provider network and never claims live permission/version acceptance.
- Customer admin `/panel-check PANEL_ID` now displays operation-by-operation results and explicitly says a successful read-only health check does not prove mutating permissions or installed-version compatibility. Updated the obsolete reconciliation UI label from `provider_not_enabled` to `provider_unsupported`.
- Test-first security regression: fresh provider response with the expected provider ID but a different username was incorrectly accepted as `before_present`. `inspect_reconciliation()` now requires both provider ID and username to match the persisted operation before comparing values. Mismatch returns `identity_mismatch` with no observed value; no mutation/retry/refund occurs.
- Focused tests: **44 passed** covering service-change reconciliation, all three adapter suites, SQLite maintenance, and the `/panel-check` UI report.
- Evidence E2 only; actual provider versions, endpoint semantics, credentials and staging permissions remain unknown. The existing factory `webapp/` is Bot 1 and was not modified. Online payment gateway/webhook remains explicitly last.
- Full isolated suite is running; record the final count and static checks in the verification addendum. Keep the worktree uncommitted/un-staged and do not touch operational services.


## Session 93 — authenticated customer Mini App and wallet purchase (2026-10-20)

- Added separate customer Mini App assets under `customer_webapp/`; root `webapp/` (factory/Bot 1 preview) remains untouched.
- Added `src/manubisguard/web/customer_app.py`: Telegram initData HMAC-SHA-256 verification, duplicate-field rejection, 5-minute auth freshness/future-skew checks, HTTPS Origin allowlist, `Cache-Control: no-store`, user-scoped `/api/v1/me`, `/shop`, `/wallet`, `/services`, and wallet-backed `POST /api/v1/purchase`. The API has no gateway/webhook/admin route and returns only generic error codes rather than DB/provider exception text.
- Purchase POST requires exact allowlisted Origin, JSON, body <=4 KiB, product ID validation and client idempotency key. The server namespaces the key with the signed Telegram ID and delegates to the existing `CustomerPurchaseService`; ambiguous provider outcomes return 202 and do not trigger a blind retry.
- Added a responsive Persian RTL frontend with wallet/catalog/service cards and confirmation before wallet purchase. A per-product idempotency key is held in `sessionStorage` only; the Telegram initData is never persisted. The bot emits Telegram `web_app` button only for a valid HTTPS `MANUBIS_CUSTOMER_WEBAPP_URL`, otherwise the button shows a configuration notice.
- Found and fixed an additional purchase idempotency bug: reusing an idempotency key for a different product/reseller/discount previously replayed the prior active order. The service now rejects any request-dimension mismatch; regression test proves no second provider call or wallet debit.
- Added `docs/CUSTOMER_MINIAPP.md`, optional `web` extra for Gunicorn, and acceptance matrix row WEB-01. Focused tests passed (30 tests across Mini App, purchase service and menu). Full isolated suite is running; record exact count after completion.
- Evidence remains local E2 only. No URL/domain configured, no live Telegram launch, panel API, payment gateway, service restart or deployment performed. Gateway checkout/webhook remains the final project stage.


### Session 93 final verification addendum — 2026-10-20

- `./scripts/run_isolated_tests.sh`: **311 tests passed in 93.059 seconds (OK, exit 0)**.
- Focused Mini App + purchase service + customer menu tests: **30 passed**; provider adapter/service-change capability and identity suite had **69 passed** in 18.959 seconds earlier in this session.
- Static checks: `python3 -m compileall -q -f src tests scripts`, `bash -n scripts/*.sh`, `node --check customer_webapp/app.js`, and `git diff --check` all passed.
- Branch remains `feature/discount-codes`; HEAD remains `e7528db16aa5de69adf53855b99090de8ba60f49`; no staged files. Existing dirty/untracked work was preserved. No commit/push, live provider/API/payment call, operational DB, deployment or service restart occurred.
- Non-payment local stages progressed: cross-provider read-only reconciliation, identity mismatch fail-closed, operation capability reports, SQLite backup/verify/restore-to-new-path, authenticated customer Mini App with wallet purchase and idempotency, and HTTPS-only bot Web App button gating.
- Remaining non-payment acceptance blockers: actual installed panel versions and staging CRUD/usage/timeout tests; configure HTTPS Mini App URL and reverse-proxy/TLS/CSP/rate limits then launch in Telegram; production-shaped restore drill, off-host retention and separately protected encryption-key recovery; broader admin/reseller web portal and finance reporting scope. These require staging configuration/credentials or explicit scope/operational authorization. Online payment gateway checkout/signed webhook remains the final project stage by user instruction.


## Session 96 — support ticket outbox and event routes (2026-10-20)

- Migration v6 (`customer_ticket_notification_outbox`) adds a tenant/ticket-unique durable queue with due index, attempts, processing lease, sent/dead state and non-sensitive error code. Ticket row, first customer message and notification row are committed atomically.
- Migration v7 (`per_event_telegram_notification_routes`) adds tenant-scoped numeric Telegram chat routes and optional forum topic IDs for the registered event keys. It allows multiple destinations per event and is configured through admin-only `/notify-route` commands.
- Supported schema version ceiling is now 8. Migration v8 adds tenant-scoped backup policy settings, disabled by default. Reopen and synthetic unversioned-legacy tests cover migration rows, foreign-key checks and idempotency.
- Only support-ticket notification delivery is connected to the route registry today; the other event keys are reserved for future wiring. Backup schedule/configuration and Telegram backup upload are not implemented. Existing backup/verify/restore helper remains a manual local tool.
- Focused tests cover ticket submission/idempotency, outbox delivery, group/topic routing and route tenant isolation. Full isolated suite/static checks pending final verification. E2 only; no operational DB migration, Telegram destination test or timer activation.


### Session 96 final verification correction

After adding an admin regression test for dead-lettered support-ticket notification requeue, the final `./scripts/run_isolated_tests.sh` run passed **322 tests in 94.982 seconds (OK, exit 0)**. The earlier Session 96 count of 321 tests is superseded. Python compileall, shell syntax, Mini App JavaScript syntax and `git diff --check` are to be recorded after the final static pass. Evidence remains isolated local E2 only.

Final static pass after Session 96 source/tests passed: Python compileall, `bash -n scripts/*.sh`, `node --check customer_webapp/app.js`, and `git diff --check`. Final isolated suite is 322/322 in 94.982 seconds.


## Session 97 — backup policy settings and SQLite durability (2026-10-20)

- Migration v8 (`tenant_backup_policy_disabled_by_default`) adds a per-tenant backup policy: frequency, IANA timezone, local wall-clock time, interval minutes, local/Telegram/both destination, numeric Telegram chat/topic, local directory, retention count/age, last-run status fields and updater audit ID. New and updated policies remain `enabled=0`; `/backup-policy enable` is deliberately rejected because the execution worker/scheduler is not yet implemented.
- Admin-only `/backup-policy show`, `/backup-policy schedule | hourly/daily/interval | IANA_TIMEZONE | HH:MM | INTERVAL_MINUTES`, `/backup-policy destination | local/telegram/both | CHAT_ID-or-0 | TOPIC_ID | /absolute/path`, and `/backup-policy retention | MAX_BACKUPS | MAX_AGE_DAYS` persist settings. `show` displays a computed next-occurrence preview, not a real scheduled run.
- `CustomerDataStore` requests SQLite WAL mode on initialization, `synchronous=FULL`, foreign keys and a 15-second busy timeout. WAL is for same-host access only; it must not be placed on a network filesystem. The backup helper continues to use SQLite online backup API.
- `src/manubisguard/backup_policy.py` validates IANA zones, wall-clock syntax, interval/retention bounds, destination/chat requirements and normalized absolute path; it calculates next UTC occurrence and handles nonexistent DST wall times. No timer, actual backup, Telegram upload, cleanup or restore was run.
- Focused test group: 15 passed, including policy validation, DST schedule, migration/reopen, tenant isolation, WAL/full-sync pragmas and admin/non-admin command behavior. Full isolated suite: **335 tests passed in 62.911 seconds (OK)**. Python compileall, Bash syntax, Mini App JS syntax and `git diff --check` passed. Evidence E2 only.


## Session 98 — shared admin notification outbox (2026-10-20)

- Kept the existing SQLite backend. Migration v9 (`durable_per_destination_admin_notification_outbox`) adds tenant-scoped parent events and per-destination deliveries with unique event idempotency, unique chat/topic delivery, retry state, claim lease, due indexes and dead-letter status. Unknown future migration versions now fail closed above v9.
- Added allowlisted payload serialization so arbitrary fields such as passwords, initData, provider secrets and subscription URLs are not persisted or sent. Events snapshot current routes at enqueue time; when no route exists, the worker uses the configured admin allowlist, and marks an event dead with a non-sensitive `no_destination` code if no destination exists.
- Added per-destination claim/send/retry/mark-sent and admin dead-letter requeue, with exponential backoff, stale-processing lease recovery, bounded batches and admin-only `/admin-notifications [all|pending|dead|sent]` / `/admin-notification-retry ID` commands. A successful destination is not resent when another destination fails.
- Transactionally wired event enqueue into wallet-ledger writes, verified wallet deposit/payment, customer purchases, manual product purchases, refunds, and service-change reserve/complete/pre-provider-refund. This keeps event creation atomic with the corresponding SQLite business state.
- Tests added for payload redaction, idempotent enqueue, tenant isolation, multiple destinations including Telegram forum topics, fallback routes, no-destination dead-letter/requeue, per-destination retry and mock worker delivery. Focused group: 4 tests passed; customer store/bot/purchase service group: 146 tests passed in 44.468 seconds.
- Evidence remains E2 isolated local SQLite/unit/mock tests only. No live Telegram send, staging/production DB, deployment, service restart, stage, commit or push. Remaining event producers for service-test, bot-log, backup and security-alert still need explicit wiring; backup scheduler/execution is still not implemented. Delivery is at-least-once if Telegram accepts a send but the process crashes before recording success.


### Session 98 — final verification (2026-10-20)

- After the admin queue/requeue command was adjusted to enqueue only (not bypass the worker), the complete isolated suite `./scripts/run_isolated_tests.sh` passed **340 tests in 67.311 seconds (OK, exit 0)**.
- The new command regression verifies admin-only queue inspection/requeue without directly invoking the sender; targeted notification tests also passed. Final Python compileall, Bash syntax, Mini App JavaScript syntax and `git diff --check` are run separately after the last edit.
- Evidence remains E2 local SQLite/unit/mock only. No live Telegram call, staging/production database, deployment, service restart, stage, commit or push. PostgreSQL remains unimplemented and current Bot 2 remains SQLite.


## Session 99 — redacted security-alert producer (2026-10-20)

- The customer bot blocks non-admin attempts to invoke known admin-only commands and the mutating `/deposit-info set` form before business handlers run. Forged `admin:` and `content:` callbacks from non-admin users are also denied.
- These paths queue tenant-scoped `security_alert` records containing only a blocked status, a fixed reason code, and the numeric customer ID. Raw commands, callback data, arguments, receipt references and card-like values are never persisted. Repeated command attempts and repeated callback attempts are independently coalesced per user per 60-second window to reduce alert flooding.
- The admin route help now accurately distinguishes connected event producers from remaining unconnected `service_test`, `bot_log` and `backup` producers. Backup policy remains configuration-only; no scheduler is enabled.
- Focused tests: 3 passed. Complete isolated suite: **341 tests passed in 62.813 seconds (OK)**. Python compileall, Bash syntax, Mini App JavaScript syntax and `git diff --check` passed.
- Evidence remains E2 local SQLite/unit/mock only. No live Telegram delivery, panel/payment API, operational DB, timer activation, deployment or service restart was performed.


## Session 101 — forged admin callback alerts and final verification (2026-10-20)

- Non-admin `admin:` and `content:` callback attempts are denied and queue a privacy-safe `security_alert`; callback data itself is not stored. Attempts are coalesced by user per 60-second window.
- Complete isolated test suite: **343 tests passed in 66.829 seconds (OK, exit 0)**. Final static checks passed: Python `compileall`, Bash syntax, Mini App JavaScript syntax, and `git diff --check`.
- Source commit `9e1012b` (`feat: alert on unauthorized admin callbacks`) is pushed to `origin/feature/discount-codes`; local and remote branch heads match. No staged files remain. Separate pre-existing README/control-bot/Factory/deploy/script/root-webapp changes remain uncommitted and untouched.
- Evidence is E2 local SQLite/unit/mock only. No live Telegram, payment/provider API, production database, deployment, service restart, or timer activation was performed.


## Session 103 — panel-check notification and Mini App preview (2026-10-20)

- The existing admin `/panel-check PANEL_ID` read-only capability/health workflow now queues a `service_test` notification with a random per-check idempotency key and only a status, fixed reason code and numeric actor ID. No panel URL, secret or raw exception is included. The route help now accurately lists `service_test` as connected; `bot_log` and `backup` remain unwired.
- The customer Mini App now exposes authenticated `POST /api/v1/service-change-preview` for `renewal`/`traffic_topup` only. It invokes the read-only service-change domain preview with signed Telegram user ID, exact allowlisted HTTPS Origin and a response field allowlist; no wallet reservation/provider mutation occurs. Actual Mini App confirmation/execution is not implemented.
- Full isolated suite after these changes: **345 tests passed in 69.450 seconds (OK)** before the final help-text-only correction; focused regression group after the correction: **4 passed**. A final full suite and static pass are being rerun before commit.
- Evidence E2 only; no live provider API, wallet mutation, production DB, deployment/restart or gateway work.
