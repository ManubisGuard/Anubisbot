# MANUBIS progress checkpoint

Updated: 2026-10-28

## Workspace and safety boundary

- Repository: https://github.com/ManubisGuard/Anubisbot
- Branch: feature/discount-codes
- Workspace used for this session: /root/bot/workspace/AnubisBot, an isolated clone created from GitHub.
- Starting commit: 79a36bab6e1faef224c2d36fe4ca8c09b80d3afe (Harden panel quote confirmation callbacks).
- The first completed UI milestone was published as 199ed02830c790ec924d3ff028ac07b46689b57f (Add panel management list and read-only check UI).
- /root/bot originally contained project reports and transfer archives, not a Git checkout. /root/fork/AnubisBot was absent on the connected device.
- No secrets were read or printed. No production DB, active provider panel, N2, live Telegram process, systemd timer, deployment, or service restart was touched.
- Live provider acceptance remains unverified. Automated evidence below is local unit/mock evidence only.

## Completed in this session

1. Re-identified the connected device and verified repository, branch, remote, starting HEAD and clean worktree.
2. Ran the isolated baseline suite: 461 tests passed in 61.143 seconds.
3. Added a customer-bot admin panel journey:
   - /admin → panel management → paginated panel list (five entries per page).
   - Each panel has a detail screen with local status and last recorded read-only check.
   - A panel can be selected for an explicitly requested read-only connection check from buttons.
   - Callback data carries a 12-character SHA-256-derived token instead of the panel ID, stays below Telegram's 64-byte limit, and fails closed for stale, malformed or ambiguous references.
4. Added confirmation-based enable/disable for local panel sales:
   - The confirmation is bound to the state shown when it was rendered. If another admin changes state before confirmation, the stale action is rejected.
   - Only the bot's local sales-enabled flag changes. This does not call or mutate the provider panel and does not delete or modify existing services.
   - Existing /panel-enable commands remain available and now record actor/source for actual state changes.
5. Added schema migration v21 with an append-only panel_connection_events audit table, actor ID, previous/new state, source, and idempotency key. Database triggers reject update/delete of audit rows; replayed actions do not create duplicate events.
6. Added regression tests for pagination, callback length, long IDs, read-only check dispatch, confirmation, audit immutability, idempotent replay and stale-confirmation rejection.
7. Focused customer-store suite: 105 tests passed in 17.566 seconds.
8. Focused customer-bot suite: 116 tests passed in 24.081 seconds.
9. Full isolated suite: 466 tests passed in 61.863 seconds.
10. Static checks passed: Python compileall, Bash syntax, Mini App JavaScript syntax, and git diff --check.

## Latest checkpoint — guided panel registration (2026-10-27)

- Implemented the multi-step Telegram admin wizard at `/admin → مدیریت پنل‌ها → افزودن پنل جدید`.
- Provider-specific authentication options now match the local contract: PasarGuard supports API key or username/password; Rebecca offers API key only; Sanaei offers access token or username/password. Unsupported combinations fail closed.
- The wizard gathers internal panel ID, display label, HTTPS URL, and an environment-variable secret reference. It rejects HTTP, embedded credentials, query/fragment URLs, invalid IDs, and raw credential text that does not use the exact `env:VARIABLE_NAME` reference format.
- Sanaei can optionally collect a separate HTTPS subscription-server origin and one safe path segment. These values are validated by the UI and again by the data-store contract. Skipping the origin leaves subscription-link capability unconfigured.
- The wizard asks whether local sales should be enabled, supports `/cancel`, expires after 15 minutes, and only saves after the final selection. It clearly states that registration does not verify live connectivity or mutate the provider.
- The wizard refuses to overwrite an existing panel ID, avoiding accidental changes to a registered connection; intentional edits remain available through the existing explicit command path.
- Added five end-to-end unit/mock journey tests: validation and secret-reference-only storage, Sanaei subscription metadata, cancellation without saving, expiry without saving, and duplicate-ID overwrite protection.
- Focused customer-bot suite before the duplicate-ID guard: **120 tests passed in 24.082 seconds**; all five wizard-specific tests passed together in 0.839 seconds.
- Full isolated suite after all wizard changes: **471 tests passed in 64.004 seconds (OK, exit 0)**.
- Static checks passed: Python compileall, Bash syntax, Mini App JavaScript syntax, and `git diff --check`.
- Research sources reviewed for behavior only: [PasarguardBot](https://github.com/AmirKenzo/PasarguardBot), [Faoxima](https://github.com/Mmd-Amir/Faoxima), and the [official Telegram Bot API](https://core.telegram.org/bots/api). No source code was copied. Telegram callback data remains bounded to 64 bytes.
- No secrets were read or printed. No production DB, provider panel, N2, live Telegram process, systemd timer, deployment, or service restart was touched. Evidence remains E2 local unit/mock only.

## Latest checkpoint — guided category management (2026-10-28)

- Added an admin category screen reachable from `/admin → مدیریت فروشگاه و محصولات → مدیریت دسته‌بندی‌ها`, with category rows, edit buttons, status controls, add action, and back navigation. The existing `/category` and `/categories` commands remain available.
- Added a private multi-step add/edit wizard for ID, title, and description. It validates the ID and field bounds, requires a final review/confirm action, supports `/cancel`, and expires after 15 minutes without partial writes.
- Adds use atomic `create_category` insert-only storage, so duplicate IDs cannot overwrite another admin's category during a race. Edits use `update_category` and cannot recreate a missing row; the current enabled flag is preserved.
- Enabling/disabling a category requires a separate confirmation, includes the expected prior state, and rejects stale callbacks. The store update itself includes that expected state in its SQL predicate. Disabling a category hides its products from new shop purchases but does not delete products or prior orders.
- Added nine new unit/mock tests covering the guided journey, cancellation/expiry, authorization, stale-state guard, and no-overwrite/update-only store behavior.
- Focused category suite: **9 tests passed in 1.629 seconds**. Full isolated suite after final code changes: **480 tests passed in 65.900 seconds (OK, exit 0)**.
- This is local unit/mock evidence only. No production store, active Telegram process, provider panel, N2, systemd timer, or deployment was changed.

## Latest checkpoint — guided product creation (2026-10-28)

- Connected the admin shop menu to real product-type choices: manual fulfillment, code/account inventory, Telegram file, and a panel-backed product.
- The common wizard validates a unique 1..20 character ASCII ID, title, positive store-product price, three-letter currency, optional existing category and description. It requires a final confirmation, supports `/cancel`, expires after 15 minutes, and does not persist a partial draft.
- File delivery requires a Telegram Document and captures its `file_id` before the final confirmation. Inventory products can be created with zero stock and show the exact command to add inventory. Manual fulfillment points the admin to the existing order/fulfillment workflow.
- Panel-backed product creation collects pricing mode, setup/unit prices, duration, GiB limit, panel ID, group/service/inbound IDs, category, and description. It uses the existing read-only local panel/metadata validator before writing; it does not test credentials against the live panel or create any provider-side service.
- Added insert-only creation behavior for both manual products and panel-backed products. The existing upsert behavior used by legacy edit commands is unchanged. Duplicate IDs are caught at the database layer, not just by the UI's preflight check.
- Focused product wizard/store suite: **9 tests passed in 1.613 seconds**. Final full isolated suite after these code changes: **489 tests passed in 64.807 seconds (OK, exit 0)**. Static checks passed: Python compileall, Bash syntax, Mini App JavaScript syntax, and `git diff --check`.
- No production store, panel, N2, active bot process, timer, or deployment was touched.

## Latest checkpoint — Telegram API standard and guided receipt review (2026-10-28)

### Mandatory UI/API rule captured for future contributors

- Added root-level `MANUBIS_CUSTOMER_STORE_MASTER_PROMPT.md` as the single master continuation prompt. `docs/PROGRESS.md` remains the single chronological evidence log; README links to both. Future AI must read the master prompt + progress log and verify real Git state before editing.
- Checked the official Telegram Bot API documentation on 2026-10-28: it currently shows **Bot API 10.3**, dated 2026-08-24: https://core.telegram.org/bots/api
- The official docs describe native button `style` values `primary` (blue), `success` (green), and `danger` (red) for supported button types. Use them semantically, verify client/button compatibility, and keep callback data within 1–64 bytes.
- Native Telegram chat keyboards do not offer arbitrary CSS glass/blur/transparency. Glassmorphism is a Mini App/Web App CSS treatment only; it must include contrast and fallback behavior. Do not claim native chat buttons are glass. Check official docs again before each meaningful Telegram API/UI implementation and record the current version/date.
- This checkpoint is documented from public official source evidence. It does not claim live Telegram client compatibility has been tested.

### Guided manual receipt review UI

- The admin finance menu now has a native-styled **review pending receipts** button. `/pending-deposits` opens the same paginated queue (eight receipts per page) rather than a text-only dump.
- Receipt detail view shows the payment ID, customer, requested amount/currency, submitted transfer reference, payer label and creation time. It explicitly warns that receipt submission/reference matching is not proof of payment.
- Detail buttons: view the Telegram receipt image (when present), initiate explicit independent-verification confirmation, reject with a required reason, or open payment audit events. Uses Bot API native `primary`, `success`, and `danger` styles and short SHA-256-derived callback tokens.
- Confirmation re-checks that the receipt is still submitted and the manual payment is still pending before forwarding to the existing wallet verification flow. Wallet settlement remains guarded by the existing transactional/idempotent store contract. Replayed/stale callbacks fail closed.
- Reject action asks for a 1–128 character reason, supports `/cancel`, expires after 15 minutes, and uses the existing atomic rejection path + durable customer notification outbox. Rejecting never credits the wallet.
- Callback-token mapping is intentionally in memory. After a bot process restart, old receipt buttons fail closed; the admin can reopen/refresh the queue to mint current references. No partial receipt-rejection state is persisted.
- Added six journey tests: finance-menu entry, queue/detail/token/style and image display, explicit confirmation and duplicate replay, rejection with reason, stale token/non-admin denial, and cancellation without state mutation.
- Focused suite: **6 tests passed in 1.090 seconds**. Full isolated suite after all source/test changes: **495 tests passed in 66.465 seconds (OK, exit 0)**.
- Static checks passed: `python3 -m compileall -q -f src tests scripts`, `bash -n scripts/*.sh`, `node --check customer_webapp/app.js`, and `git diff --check`.
- Evidence level: `unit/mock + public-source`. No live Telegram message was sent as a test, and no production DB, live bot, panel, N2, timer or deployment was touched.
- Published code/tests/rule/docs in commit [`f8626b4`](https://github.com/ManubisGuard/Anubisbot/commit/f8626b499345714ab8564a7a6a846db121c5f9b3), message `Add Telegram UI standards and receipt review flow`. The isolated workspace was fetched/reset to that exact remote commit and verified clean before this log-only follow-up.

## Current limits / do not overclaim

- The wizard state is in memory and is lost if the bot process restarts; it does not store partially completed forms. Registration does not prove credentials, API scopes, live connectivity, or compatibility with the installed provider version.
- A read-only health check does not prove create/update/delete permissions or compatibility with the installed panel version.
- PasarGuard, Rebecca and Sanaei acceptance is still E2 local mock evidence. Live mutation tests require a disposable authorized staging panel and must never be performed against production users.
- Online payment gateways and signed gateway webhooks remain out of scope. Manual card-to-card receipt review remains the current payment path.
- Billing systemd files are templates only; do not install or activate timers as part of this work.

## Next steps in order

1. Continue reviewing provider operation contracts and normalized response schemas across PasarGuard, Rebecca and Sanaei. Check the latest progress entry for already-covered invalid configuration, boolean/negative counters, unknown permission reporting, and no-blind-retry mutation behavior; add only new regression cases.
2. Continue payment review polish only if a concrete gap is found: receipt image absence, old callback references, stale confirmation, rejection reason validation and audit event navigation are covered in this checkpoint.
3. Live staging acceptance remains blocked until a disposable panel and explicit authorization are provided.
4. After each independent implementation step: focused tests, full `./scripts/run_isolated_tests.sh`, static checks, reviewed-path-only staging, publish to `feature/discount-codes`, synchronize and verify clean status. Never use `git add -A`; never restart services or deploy without explicit authorization.


## Latest checkpoint — PasarGuard input and configuration fail-closed hardening (2026-10-28)

- Verified current connected device label 8017638839-32c511, workspace /root/bot/workspace/AnubisBot, remote https://github.com/ManubisGuard/Anubisbot.git, branch feature/discount-codes, starting HEAD 8642cf4bf836b6f505b0552e90ff521c160ad8de, and clean/synchronized status before editing.
- Found a real billing-data edge case: Python booleans are integer subclasses, so True previously normalized to a byte counter of 1. PasarGuard _validated_counter now rejects booleans. _map_usage now rejects explicit boolean, unparseable, or negative counters instead of silently coercing/ignoring those values; absent recognized usage still fails closed.
- _client validates supported auth modes before resolving the configured secret. username_password mode also requires a non-empty string metadata.username before any secret resolution or SDK client construction. Capability reports retain live_panel_verified=false and leave panel permissions unknown on invalid configuration.
- Added regression tests for boolean user-usage and data-limit values, boolean/negative usage response counters, unsupported auth mode, and missing username. Test-first run correctly failed on the boolean counter and both invalid-configuration resolver calls before the implementation fix.
- Focused PasarGuard adapter suite: 24 tests passed in 0.087 seconds. Combined provider contract suites (PasarGuard adapter/reseller, Rebecca adapter/native reseller, Sanaei adapter): 82 tests passed in 2.040 seconds.
- Full isolated suite: 497 tests passed in 65.253 seconds (OK, exit 0) via ./scripts/run_isolated_tests.sh.
- Static checks passed: python3 -m compileall -q -f src tests scripts, bash -n scripts/*.sh, node --check customer_webapp/app.js, and git diff --check (STATIC_CHECKS_OK).
- Updated docs/PROVIDER_CAPABILITY_MATRIX.md, MANUBIS_CUSTOMER_STORE_MASTER_PROMPT.md, and this progress log to reflect the new contracts and remaining scope.
- Evidence: E2 local unit/mock only. No provider network request, staging panel, live Telegram client, production DB, production panel, N2, running service, timer, deployment, restart, or secret retrieval was performed. At the time this entry was drafted, publication was pending; append the actual commit SHA and verified remote/workspace HEAD after publication.
- Next: continue the source/test review for any remaining adapter input/schema gaps and state-reconciliation edge cases. Keep provider mutation mocks no-retry. Live acceptance still requires a disposable authorized staging panel and explicit authorization.

## Publication verification — PasarGuard hardening (2026-10-28)

- Published source/tests and the related documentation in commit 034332555c0c5f53f12c98d3bafb831f296e9734 (Harden PasarGuard counters and auth configuration): https://github.com/ManubisGuard/Anubisbot/commit/034332555c0c5f53f12c98d3bafb831f296e9734
- GitHub compare confirmed the change is exactly one commit ahead of 8642cf4bf836b6f505b0552e90ff521c160ad8de and changes only these five reviewed paths: MANUBIS_CUSTOMER_STORE_MASTER_PROMPT.md, docs/PROGRESS.md, docs/PROVIDER_CAPABILITY_MATRIX.md, src/manubisguard/panels/adapters/pasarguard.py, tests/test_pasarguard_adapter.py.
- Updated the feature/discount-codes ref with force=false and expected parent 8642cf4bf836b6f505b0552e90ff521c160ad8de. The resulting ref was verified as 034332555c0c5f53f12c98d3bafb831f296e9734.
- Fetched the published branch and synchronized the isolated workspace. Local HEAD and origin/feature/discount-codes both matched 034332555c0c5f53f12c98d3bafb831f296e9734; the five local file blob hashes matched their remote tree entries; git status was clean.
- Automated evidence remains E2 unit/mock only; no live provider or production-side operation was performed. The 497-test isolated suite and static checks passed before publication.
