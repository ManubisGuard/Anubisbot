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


## Latest checkpoint — Rebecca/Sanaei numeric response normalization (2026-10-28)

- Verified device 8017638839-32c511, workspace /root/bot/workspace/AnubisBot, branch feature/discount-codes, starting HEAD d67ccf7b54975d9df1348e002af812bc8b481410, and clean/synchronized Git status before editing.
- Test-first inspection found seven failures: Rebecca accepted boolean expiry, usage and data-limit values; Sanaei accepted boolean totalGB, expiry, used_traffic, and up counters because Python bool subclasses int.
- Rebecca now explicitly rejects booleans in expiry and normalized usage/quota fields with sanitized PanelError messages.
- Sanaei now rejects boolean/invalid expiry, rejects boolean totalGB, and fails closed on malformed or boolean usage values instead of silently converting them to zero. Existing negative-counter checks remain in place.
- Added two regression test methods with seven invalid payload subcases across tests/test_rebecca_adapter.py and tests/test_sanaei_adapter.py.
- Focused Rebecca + Sanaei suites: 47 tests passed in 0.183 seconds.
- Full isolated suite: 499 tests passed in 65.304 seconds (OK, exit 0) via ./scripts/run_isolated_tests.sh.
- Static checks passed: python3 -m compileall -q -f src tests scripts, bash -n scripts/*.sh, node --check customer_webapp/app.js, and git diff --check (STATIC_CHECKS_OK).
- Updated docs/PROVIDER_CAPABILITY_MATRIX.md and MANUBIS_CUSTOMER_STORE_MASTER_PROMPT.md with the new normalization contracts.
- Evidence level: E2 local unit/mock only. No provider network request, live panel, production DB, N2, running service, timer, deployment, restart, or secret retrieval was performed.
- Published in commit 0a5605c324c35f84472c1f92e51edcdcba9375fe (Reject boolean provider usage and quota fields); verified local and remote HEAD matched after fetch/reset.
- Next: inspect response envelope and pagination behavior across provider adapters for malformed-success responses and partial-list ambiguity; preserve no-blind-retry mutation policy and do not claim live acceptance.


## Publication verification — Rebecca/Sanaei numeric normalization (2026-10-28)

- Published code, tests, and docs in commit 0a5605c324c35f84472c1f92e51edcdcba9375fe: https://github.com/ManubisGuard/Anubisbot/commit/0a5605c324c35f84472c1f92e51edcdcba9375fe
- GitHub compare verified this was exactly one commit ahead of d67ccf7b54975d9df1348e002af812bc8b481410, with only seven reviewed paths changed.
- Updated feature/discount-codes with force=false and expected prior SHA d67ccf7b54975d9df1348e002af812bc8b481410. Fetched the branch; local HEAD and origin/feature/discount-codes both matched 0a5605c324c35f84472c1f92e51edcdcba9375fe. All seven local blob hashes matched their remote tree entries, and git status was clean.
- Final post-publication static checks passed: compileall, Bash syntax, JavaScript syntax, and git diff --check. Full 499-test isolated suite passed before publication.

## Latest checkpoint — provider list-envelope and partial-snapshot hardening (2026-10-28)

- Verified device label 8017638839-32c511, workspace /root/bot/workspace/AnubisBot, branch feature/discount-codes, starting HEAD 0c539c68007193030e448cebbd0e58c074f2c234, and a clean/synchronized worktree before editing.
- Test-first review found successful but malformed list responses were being interpreted as empty results: PasarGuard defaulted a missing `users` field to an empty list; Rebecca defaulted absent/non-object `users` envelopes to empty; Sanaei defaulted missing `obj` to empty and skipped malformed token clients and legacy inbounds/settings.
- PasarGuard now requires `users` to be a list/tuple. Rebecca requires the `users` list after optional `data` unwrapping. Sanaei requires an explicit `obj` list in token and legacy modes, rejects malformed token client items, and fails closed for malformed legacy inbound items, settings JSON, settings objects, client lists and client records rather than returning a partial snapshot.
- Added five regression test methods across tests/test_pasarguard_adapter.py, tests/test_rebecca_adapter.py and tests/test_sanaei_adapter.py (including subcases for malformed success envelopes and legacy partial-list ambiguity). The focused three-adapter suite passed: **76 tests**.
- Full isolated suite: **504 tests passed in 64.246 seconds (OK, exit 0)** via `./scripts/run_isolated_tests.sh`.
- Static checks passed after source/test changes: `python3 -m compileall -q -f src tests scripts`, `bash -n scripts/*.sh`, `node --check customer_webapp/app.js`, and `git diff --check` (`STATIC_CHECKS_OK`).
- Updated `docs/PROVIDER_CAPABILITY_MATRIX.md` and this master prompt to record the explicit response-envelope contracts and the remaining pagination-completeness uncertainty.
- Evidence level: E2 local unit/mock only. No provider network requests, live panel, production DB, N2, live Telegram process, timer, deployment, restart or secret resolution beyond fake test resolvers was performed.
- Source/tests/docs were published in commit `eff83db34d9a0116ca507da6158f8b84faab07cd` (`Reject malformed provider user-list snapshots`). The exact changed-path and hash verification is recorded in the publication-verification section below.
- Next: verify provider-version-specific pagination/continuation metadata and whether a well-formed page can still be silently truncated. Keep `list_users` caller-controlled and bounded until supported API contracts prove safe auto-pagination. Continue to avoid blind retries for provider mutations and do not claim live acceptance.

## Publication verification — provider list-envelope hardening (2026-10-28)

- Published source/tests/docs in commit `eff83db34d9a0116ca507da6158f8b84faab07cd` (Reject malformed provider user-list snapshots): https://github.com/ManubisGuard/Anubisbot/commit/eff83db34d9a0116ca507da6158f8b84faab07cd
- GitHub compare confirmed the commit is exactly one commit ahead of `0c539c68007193030e448cebbd0e58c074f2c234` and changes only the nine reviewed paths listed in the checkpoint above.
- Updated `feature/discount-codes` with `force=false` and expected prior SHA `0c539c68007193030e448cebbd0e58c074f2c234`. The GitHub ref was read back as `eff83db34d9a0116ca507da6158f8b84faab07cd`.
- Fetched the published branch and synchronized the workspace. Local HEAD and `origin/feature/discount-codes` matched `eff83db34d9a0116ca507da6158f8b84faab07cd`; all nine local file blob hashes matched the published GitHub content SHAs; `git status --short --branch` was clean.
- After synchronization, compileall, Bash syntax, JavaScript syntax and `git diff --check` passed again (`STATIC_CHECKS_OK`). The full 504-test isolated suite passed before publication.
- Evidence remains E2 local unit/mock only. No live provider, staging, production, Telegram or deployment operation was performed.

## Handoff and phased roadmap clarified — 2026-10-28

### Fixed behavioral reference bots

The user-confirmed behavioral reference set is:
1. **Faoxima** — multi-panel/server, capability reports, manual/inventory fulfillment, panel backup, trust channel, test account, reservation/auto activation, gateways, dynamic exchange rate, tickets, Mini App.
2. **PasarguardBot** — panel/service integration, service lifecycle, activation/sales/renewal behavior.
3. **PGClockBot** — renewal preview, mutation lock, “my services”, paid-but-undelivered recovery, receipt review, tickets, roles, jobs, backup/restore.
4. **MirzaBot** — driver registry, web-admin customization, languages, phone/channel gates, expiry/payment/notification jobs.

These are behavior references only; no source-code copying. Provider adapters are separate and must be handled in order PasarGuard → Rebecca → Sanaei/3x-ui. Sanaei does not support reseller creation; do not advertise it.

### Last-known passing tests — do not repeat without a reason

- Most recent full isolated suite: **504 passed in 64.246 seconds**, exit 0, `./scripts/run_isolated_tests.sh`.
- Most recent focused combined adapter suites: **76 passed** (PasarGuard, Rebecca, Sanaei).
- Static checks passed: `python3 -m compileall -q -f src tests scripts`; `bash -n scripts/*.sh`; `node --check customer_webapp/app.js`; `git diff --check`.
- Latest source/test commit: `eff83db34d9a0116ca507da6158f8b84faab07cd`. The subsequent docs-only publication-verification commit is `f74be144d15ad87b4ed1f55c8d5c2cc0f0e5f80b`. Always verify live Git state before continuing.
- Evidence level: E2 local unit/mock only. No live provider acceptance is established.

**Anti-duplication rule:** do not rerun the 76 adapter tests or 504-test full suite merely as a startup ritual. These results remain the baseline until code changes, a concrete new defect/contract gap, or a necessary regression check gives a reason to run relevant tests. A docs-only change does not justify rerunning all tests. Record exact commands/counts/results whenever a justified run occurs.

### Immediate next step — Phase 1: pagination contract research (not tests)

Inspect existing list-users call paths and pinned SDK/API contracts. Review provider-version-specific official docs or source for total/offset/limit/cursor/next-page metadata and determine whether well-formed pages can be silently truncated. Do not implement automatic pagination or add adapter tests until evidence proves the contract and safe continuation behavior. Record URLs, version caveats, findings and uncertainty in docs/PROVIDER_CAPABILITY_MATRIX.md. If the contract cannot be established, mark completeness unknown and preserve caller-controlled pagination capped at 200. This phase is source/code research only; do not repeat completed adapter tests.

### Remaining phases — do not skip or restart completed work

- **Phase 2 — Customer purchase journey:** audit and complete catalog/category → detail/quote → confirmation → payment choice → order status. Reuse existing code and quote/snapshot guards.
- **Phase 3 — Payments, wallet, discounts and recovery:** manual receipt submission/review, immutable wallet ledger, idempotency, discount validation, audit, and paid-but-undelivered reconciliation. Online gateways remain deferred; no blind retry/refund on ambiguous provider outcomes.
- **Phase 4 — Fulfillment state machine:** manual, code/account inventory, file and panel fulfillment under one order lifecycle; atomic inventory reservation and replay-safe delivery.
- **Phase 5 — PasarGuard first-provider acceptance:** complete contract/capability/reconciliation gaps and, only after explicit authorization, test on a disposable staging panel.
- **Phase 6 — Support and notifications:** tickets, durable outbox, retry/idempotency and trust-channel behavior. Do not activate timers without authorization.
- **Phase 7 — Rebecca:** fill only evidenced gaps, then staging acceptance under the same gate.
- **Phase 8 — Sanaei/3x-ui:** fill evidenced gaps; honor token/legacy differences and whole-GiB quota; no reseller feature.
- **Phase 9 — Admin operations:** reports, audit, backup/restore, installer and recovery UX.
- **Phase 10 — Mini App:** after core purchase and fulfillment journeys; accessible responsive UI, reduced motion and CSS glass treatment only in the Web App.
- **Phase 11 — Live acceptance/release:** authorized disposable staging only. No production panel, N2, mother bot, live process, secret, deployment or restart without separate explicit authorization.

At the start of each phase, first map what already exists and the acceptance gap. Prefer a user-visible product milestone over an isolated test-only loop. Preserve the Telegram API rule in the master prompt: check latest official Bot API docs before meaningful UI/API changes, use only supported button styles and keep callback data within 1–64 bytes.

### Safety and evidence

No live panel/provider calls, production database, N2, active Telegram process, timers, deployments, restarts or secrets were touched in this documentation update. This entry is a handoff/roadmap clarification, not a claim of new product acceptance.

## Phase 1 checkpoint — provider pagination public-source research (2026-10-28)

**Purpose:** examine provider list contracts without rerunning adapter tests or touching live panels.

### Findings

- **PasarGuard:** local adapter calls SDK `get_users(token, offset, limit)`, validates the users envelope and returns a list, but drops total metadata. Current public SDK README documents offset/limit pagination and uses `page.total` to stop paging: https://github.com/AmirKenzo/pasarguard_api/blob/main/README.md. The project declares `pasarguard>=2.3.1` without a lockfile; package is not installed in the default Python interpreter on this device. Exact SDK/provider version remains unverified.
- **Rebecca:** local adapter sends GET `/api/users?limit=...&offset=...`, validates users and returns only the mapped list, dropping total metadata. Current upstream source registers GET `/api/users`, parses query offset/limit and returns the list-service response; OpenAPI describes zero-based offset and notes server-side limit caps. The current response model includes total. Sources: https://github.com/rebeccapanel/Rebecca/blob/master/internal/app/api/routes.go ; https://github.com/rebeccapanel/Rebecca/blob/master/internal/app/api/users.go ; https://github.com/rebeccapanel/Rebecca/blob/master/internal/app/api/openapi/openapi.json ; https://pkg.go.dev/github.com/rebeccapanel/rebecca/internal/app/user. This is current upstream evidence only; deployed version/cap remain unknown.
- **Sanaei/3x-ui token mode:** local adapter calls `/panel/api/clients/list` and slices the returned list locally. Current official docs describe that endpoint as listing every client and separately document `/panel/api/clients/list/paged` with page/pageSize and total: https://github.com/MHSanaei/3x-ui/blob/main/docs/content/docs/en/reference/api/clients.mdx. The adapter does not call the newer paged endpoint, and its availability on the deployed panel is unknown.
- **Sanaei legacy mode:** local adapter calls `/panel/api/inbounds/list`, validates all returned inbounds/settings/clients, aggregates the clients, and slices locally. No pagination/continuation contract was found in the reviewed official reference. Exact installed version and any server-side truncation behavior remain unknown.

### Decision and status

- Public-source research is complete for this checkpoint, but **deployed-version completeness is not verified**. A valid-looking response could still be truncated by an unknown deployed version/cap; available sources do not prove or disprove that for the user's panels.
- Do not implement generic auto-pagination yet. Each adapter currently returns a plain list and discards total/has-more metadata; a safe change needs an explicit normalized page contract plus version-specific evidence. Keep the 200-item bound and explicit caller-controlled offsets. Do not infer that a short page proves completeness where the provider contract/version is unknown.
- **No adapter tests were run**, intentionally. No source code was changed. No provider API/live panel, production database, N2, Telegram process, deployment, timer, restart or secret was accessed. Evidence: public-source + local source inspection only; not live acceptance.
- Next project phase: **Phase 2 — audit the existing customer purchase journey** (catalog/category → product detail/quote → confirmation → payment choice → order status). First map existing code and the acceptance gap; do not rebuild existing screens and do not begin with a test-only loop.

## Phase 2 slice 1 — compact customer catalog navigation (2026-10-28)

### Audit finding

The previous customer shop mixed category buttons and purchase buttons in one screen, with up to 20 categories and up to 10 manual plus 10 panel products. This conflicted with the product UX rule to keep keyboards compact (normally no more than 6–8 buttons) and made category selection noisy.

### Implemented

- Root shop and /shop open a dedicated category selector when categories exist; products are not mixed into that selector.
- Category selection page shows four categories at a time, with explicit “all products”, previous/next and main-menu navigation. Category pagination still reaches every category.
- Product listing combines manual and panel products into one catalog sequence and shows four items per page, preventing two separate product lists from doubling the number of purchase buttons.
- Product pages provide category navigation and main-menu navigation; purchase actions use supported primary styling. Existing confirmation/quote checks, wallet logic and fulfillment handlers were left intact.
- Added/updated UI tests for mixed category/catalog navigation, all-products route, later-page reachability and the eight-button maximum.

### Validation

- Focused customer bot UI suite: **141 tests passed in 26.378 seconds**, exit 0. Command: `PYTHONPATH=src:scripts/test_support python3 -m unittest discover -s tests -p 'test_customer_bot.py' -v`.
- Full isolated regression suite: **504 tests passed in 63.972 seconds**, exit 0. Command: `./scripts/run_isolated_tests.sh`. The full suite naturally includes adapter tests; no separate adapter-only suite was run as part of this product UX step.
- Static checks passed: `python3 -m compileall -q -f src tests scripts`; `bash -n scripts/*.sh`; `node --check customer_webapp/app.js`; `git diff --check`.
- No live Telegram bot, provider API, production panel/database, N2, deployment, timer or process was touched. Evidence remains E2 local tests; no live acceptance claim.

### Next Phase 2 task

Continue the customer journey audit from the next actual gap, without redoing the catalog navigation:
1. Inspect panel-product detail/quote presentation and determine whether a dedicated detail step improves clarity without adding friction.
2. Handle insufficient wallet balance with a clear recovery path to wallet/manual deposit, without implying that card-to-card receipt submission instantly credits funds.
3. Review order status and paid-but-undelivered recovery screens for customer-facing clarity and safe retry.
4. Only then change the relevant journey; add focused customer-flow regressions and run the full isolated suite for code changes. Do not run adapter tests as a standalone task unless a provider-related change creates a specific reason.

## Phase 2 slice 2 — insufficient-wallet purchase recovery (2026-10-28)

### Audit finding

Catalog navigation is now compact, but both manual-product and panel-product purchase confirmation paths returned a generic failure/back-menu screen when the wallet was insufficient. This left the user without a direct route to the wallet or manual top-up flow.

### Implemented

- Added one shared customer-facing purchase-error renderer for wallet-balance failures, including metered plans whose setup charge would leave no positive balance.
- On insufficient balance, the bot now explains in Persian that manual card-to-card top-up is not credited until an admin verifies it, and offers direct buttons for manual wallet top-up, wallet balance, and return to shop.
- Reused the helper in manual product purchase and panel product/discounted purchase failure paths. Other errors preserve their existing back-menu behavior.
- Added two focused regression tests covering manual and panel purchase recovery, checking the three navigation callbacks and confirming no wallet debit occurred in the manual case.

### Validation

- Focused customer bot UI suite: **143 tests passed in 27.535 seconds**, exit 0. Command: `PYTHONPATH=src:scripts/test_support python3 -m unittest discover -s tests -p 'test_customer_bot.py' -v`. An initial focused run exposed two fixture issues in the new tests (missing customer registration and an overlong product ID); fixtures were corrected and the full focused suite passed.
- Full isolated suite: **506 tests passed in 66.650 seconds**, exit 0. Command: `./scripts/run_isolated_tests.sh`. This full-suite run naturally includes adapter tests; no adapter-only test cycle was started.
- Static checks passed: `python3 -m compileall -q -f src tests scripts`; `bash -n scripts/*.sh`; `node --check customer_webapp/app.js`; `git diff --check`.
- Evidence level remains E2 local unit/mock only. No live Telegram bot, live provider, production panel/database, N2, timer, deployment, restart, or secret was touched.

### Next Phase 2 task

Continue with **order-status and paid-but-undelivered recovery clarity**. Inspect the existing order history/status labels and recovery/reconciliation callbacks first; preserve idempotency and never blindly retry a provider mutation or automatically refund an ambiguous outcome. Do not revisit catalog navigation or rerun adapter tests separately.


## Phase 2 slice 3 — order status and paid-but-undelivered recovery clarity (2026-10-28)

### Audit finding

The existing order history already combines panel and manual orders, scopes history to the customer, and supports safe file redelivery without charging again. However, real panel-order states provisioning and active were rendered as raw internal labels, and paid/manual orders awaiting delivery had no direct support follow-up from the history screen.

### Implemented

- Added customer-facing Persian labels for actual provisioning and active states, plus clearer paid/pending-delivery and failed/reconciliation wording.
- Added per-order support follow-up buttons for paid, provisioning, pending-delivery, failed, and reconciliation-needed states. The history screen tells customers to include the order ID in their ticket.
- Added explicit guidance not to repeat a purchase while provider outcome is uncertain; existing reconciliation logic remains read-only until an operator finalizes verified evidence. No automatic refund, provider mutation retry, or new recovery mutation was added.
- Preserved existing owner-scoped file redelivery, which only serves a fulfilled file order and does not debit the wallet again.
- Added a focused regression test for provisioning/pending-delivery/active status clarity and support navigation without offering a repeat purchase.

### Validation

- Focused customer bot UI suite: **144 tests passed in 27.748 seconds**, exit 0. Command: `PYTHONPATH=src:scripts/test_support python3 -m unittest discover -s tests -p 'test_customer_bot.py' -v`. The new test first caught a Persian half-space assertion mismatch; assertion was corrected and both the targeted test and full focused suite passed.
- Full isolated suite: **507 tests passed in 65.876 seconds**, exit 0. Command: `./scripts/run_isolated_tests.sh`. Adapter tests ran only as part of this required full-suite regression run; no standalone adapter suite was run.
- Static checks passed: `python3 -m compileall -q -f src tests scripts`; `bash -n scripts/*.sh`; `node --check customer_webapp/app.js`; `git diff --check`.
- Evidence remains E2 local unit/mock only. No live Telegram bot, provider API, production panel/database, N2, timer, deployment, restart, or secret was touched.

### Next Phase 2 task

Audit whether order-history status needs a customer-safe distinction between a panel order that is still provisioning and one flagged for reconciliation. The provider marker is intentionally not exposed in the customer history query today; if adding this distinction, expose only a sanitized boolean/state label and never provider references or provisioning payloads. Preserve no-blind-retry/no-auto-refund rules. Do not rebuild catalog navigation or insufficient-wallet recovery.


## Publication integrity checkpoint — order-status slice (2026-10-28)

- Final published code commit: `9a58812dcfd69d2ebd8882c0046b18b34844544d` on `feature/discount-codes`.
- During post-publication integrity verification, the first publication (`5a7ff441be2b2696969dc3e25152180f4b2db69d`) was found to contain truncated copies of the two large Python files because the file-reading response was paginated. It was immediately superseded by the non-force corrective commit above, which restores both full files from the prior baseline and reapplies the reviewed patch.
- Verified full file lengths after correction: `src/manubisguard/telegram/customer_bot.py` 4,569 lines; `tests/test_customer_bot.py` 3,000 lines. Their published Git blob hashes match the workspace hashes (`ae46a82bbc0842a684d7d60ed3eed5332c5fcd59` and `b09c1e7774a5c75f51c3426eb93df9d6ee737a06`).
- Local HEAD and `origin/feature/discount-codes` both equal `9a58812dcfd69d2ebd8882c0046b18b34844544d`; working tree clean. Post-correction static checks passed: `python3 -m compileall -q -f src tests scripts`; `bash -n scripts/*.sh`; `node --check customer_webapp/app.js`; `git diff --check`.
- The focused 144-test suite and 507-test full isolated suite ran against the same exact source/test blob hashes before publication. Evidence remains E2 local unit/mock; no live systems were touched.


## Phase 2 slice 4 — sanitized reconciliation status in customer order history (2026-10-28)

### Audit finding

The store already persisted an internal `provider_reference` marker beginning `reconcile_required:` when initial provider provisioning became ambiguous. `list_customer_orders` intentionally omitted that internal reference, but it also omitted a safe indicator, so the customer UI could only show a generic “provisioning” state even when operator reconciliation was required.

### Implemented

- Extended the tenant/customer-scoped order-history query with a computed `needs_reconciliation` boolean, derived only for `provisioning` orders whose internal marker is set. The raw provider reference, marker reason, provisioning intent and provider payload remain excluded from the history result.
- Customer order history maps that safe boolean to the existing Persian message “needs support review; do not purchase again”, while ordinary provisioning remains distinct and unchanged.
- Added store regression coverage for true marker detection, omission of provider/internal snapshot fields, and tenant scoping. Extended the customer UI regression to assert that both reconciliation-required and ordinary provisioning states render differently and safely.
- No new retry, refund, provider call, wallet debit or status mutation was introduced.

### Validation

- Customer store suite: **109 tests passed in 17.965 seconds**, exit 0. Command: `PYTHONPATH=src:scripts/test_support python3 -m unittest discover -s tests -p 'test_customer_store.py' -v`.
- Customer bot suite: **144 tests passed in 27.006 seconds**, exit 0. Command: `PYTHONPATH=src:scripts/test_support python3 -m unittest discover -s tests -p 'test_customer_bot.py' -v`.
- Full isolated suite: **508 tests passed in 63.176 seconds**, exit 0. Command: `./scripts/run_isolated_tests.sh`.
- Static checks passed: `python3 -m compileall -q -f src tests scripts`; `bash -n scripts/*.sh`; `node --check customer_webapp/app.js`; `git diff --check`.
- Evidence remains E2 local unit/mock only. No live Telegram bot, provider API, production panel/database, N2, timer, deployment, restart or secret was touched.

### Next Phase 2 task

Audit customer payment/receipt status journeys for clear distinction between receipt submitted, awaiting admin review, approved/credited and rejected; ensure customer-facing messages and history never imply wallet credit before atomic approval, and that support follow-up is available where needed. Keep online gateways deferred and preserve the existing append-only/idempotent wallet settlement model.


## Phase 2 slice 5 — manual deposit receipt-status follow-up (2026-10-28)

### Audit finding

The customer wallet already mapped the core manual payment states correctly, but the recent-payment screen did not explicitly repeat that a submitted receipt is not a credit, and rejected/failed payments had no direct support route from that screen. Store-level tests already covered owner/tenant scoping, status mapping, and omission of private transfer references.

### Implemented

- Added a clear customer-facing reminder when any recent payment is awaiting a receipt or receipt review: request/receipt submission does not credit the wallet; credit happens only after admin verification.
- For rejected/failed payment records, added a short follow-up explanation and a support button while keeping the keyboard compact. No per-payment callback or privileged action was introduced.
- Extended the existing customer payment-status journey regression to cover rejected receipts, the support route, redaction of the transfer reference, and unchanged wallet balance after rejection.
- The verified state continues to say that the wallet was credited; the submitted state continues to say it is awaiting admin review.

### Validation

- Targeted receipt-status test: **1 passed in 0.251 seconds**, exit 0.
- Focused customer bot suite: **144 tests passed in 27.078 seconds**, exit 0. Command: `PYTHONPATH=src:scripts/test_support python3 -m unittest discover -s tests -p 'test_customer_bot.py' -v`.
- Full isolated suite: **508 tests passed in 63.720 seconds**, exit 0. Command: `./scripts/run_isolated_tests.sh`.
- Static checks passed after code changes: `python3 -m compileall -q -f src tests scripts`; `git diff --check`. Final static suite also includes `bash -n scripts/*.sh` and `node --check customer_webapp/app.js`.
- Evidence remains E2 local unit/mock only. No live Telegram bot, provider API, production panel/database, N2, timer, deployment, restart or secret was touched.

### Next Phase 2 task

Review product detail and quote screens for customer clarity, then verify the displayed fulfillment state for manual, file and inventory orders remains aligned with actual order status and notification state. Avoid rebuilding completed catalog navigation and wallet recovery; preserve idempotency and never expose inventory payloads before purchase.


## Phase 2 slice 6 — fulfilled manual-order redelivery recovery (2026-10-28)

### Audit finding

The order history exposed redelivery only for fulfilled file orders. Instant/manual and inventory-backed orders also retain their exact delivery payload in the immutable order snapshot, but a customer who lost the original message had no history action to retrieve that already-purchased payload. Repeating the purchase was not a safe recovery path.

### Implemented

- The minimal order-history result now includes a safe `can_redeliver_delivery` flag for fulfilled manual orders with a non-empty saved payload, plus the non-secret delivery mode. It never returns the payload or Telegram file ID in history.
- The owner-scoped delivery lookup now supports any fulfilled manual-order delivery mode, not just files. It still requires the exact tenant, order ID, owning Telegram user, fulfilled state and non-empty saved payload.
- Order history now offers a generic redelivery action for fulfilled file, instant, inventory and manually fulfilled orders. Existing `file_retry` callbacks remain supported for older file-delivery messages; new history actions use `delivery_retry`.
- File orders still use Telegram `sendDocument`; other fulfillment modes resend the original saved delivery text. This reads the existing order only: it does not run purchase, consume inventory again, mutate status or debit the wallet.
- Added store and UI regression tests for inventory payload redelivery, no secret in order history, owner/tenant boundary, and unchanged wallet balance. Existing file redelivery and failure-retry coverage remains intact.

### Validation

- Customer store suite: **110 tests passed in 17.363 seconds**, exit 0.
- Customer bot suite: **145 tests passed in 27.127 seconds**, exit 0. Command: `PYTHONPATH=src:scripts/test_support python3 -m unittest discover -s tests -p 'test_customer_bot.py' -v`.
- Full isolated suite: **510 tests passed in 66.282 seconds**, exit 0. Command: `./scripts/run_isolated_tests.sh`.
- Static checks passed: `python3 -m compileall -q -f src tests scripts`; `bash -n scripts/*.sh`; `node --check customer_webapp/app.js`; `git diff --check`.
- One first-pass UI assertion exposed a legacy file retry callback expectation; kept that legacy callback supported and reran the affected tests plus the full focused and isolated suites successfully.
- Evidence remains E2 local unit/mock only. No live Telegram bot, provider API, production panel/database, N2, timer, deployment, restart or secret was touched.

### Next Phase 2 task

Continue the customer purchase journey audit for any remaining gaps in product detail/quote clarity and delivery notifications. Confirm all modes (manual, file, inventory, panel) recover from Telegram send failures without repeat charges, while avoiding duplicate payload exposure in public catalog/history views. Do not add online gateways or trigger live panel tests.


## Phase 2 slice 7 — order history navigation and button budget (2026-10-28)

### Audit finding

If the immediate Telegram message for a panel purchase is missed, the created service is still available in the customer's private service list, but order history did not provide a direct route to that list. Order history also displayed five orders per page; when each order had a follow-up action, the keyboard could exceed the project's eight-button UX limit.

### Implemented

- Active panel orders now show a direct `customer:services` action, so customers can recover the created service and open its private details without making a second purchase.
- Order history now displays four orders per page. In the worst case this permits four per-order actions, two pagination buttons, Shop and Home: eight buttons total.
- Kept actions mutually exclusive per order: active panel order -> services; fulfilled manual order -> saved delivery redelivery; pending/uncertain/failed order -> support follow-up.
- No purchase, provider mutation, wallet debit, or refund occurs when using the new services navigation.
- Added regression coverage for active panel service navigation, pagination, and the eight-button ceiling.

### Validation

- Focused customer bot suite: **146 tests passed in 27.120 seconds**, exit 0. Command: `PYTHONPATH=src:scripts/test_support python3 -m unittest discover -s tests -p 'test_customer_bot.py' -v`.
- Full isolated suite: **511 tests passed in 62.408 seconds**, exit 0. Command: `./scripts/run_isolated_tests.sh`.
- Static checks passed: `python3 -m compileall -q -f src tests scripts`; `bash -n scripts/*.sh`; `node --check customer_webapp/app.js`; `git diff --check`.
- Evidence remains E2 local unit/mock only. No live Telegram bot, provider API, production panel/database, N2, timer, deployment, restart, or secret was touched.

### Next Phase 2 task

Audit notification durability for panel purchases after a service is created but the immediate customer Telegram response fails. Prefer a durable, idempotent customer outbox that references the saved service/order and never repeats provider creation. Keep private subscription URLs out of generic logs and trust-channel payloads. Do not add online gateways or run live adapter tests.


## Phase 2 slice 8 — durable customer notice after panel service creation (2026-10-28)

### Audit finding

A panel-backed purchase persisted the service and marked the order active before sending the immediate Telegram confirmation. If Telegram delivery failed after the provider create/finalization succeeded, the customer could recover the service through My Services/order history, but there was no durable customer-notification record to retry the original confirmation. The existing trust-channel outbox intentionally filters subscription URLs and therefore cannot serve as a private customer-delivery queue.

### Implemented

- Added schema migration v22 with a tenant-scoped, unique-per-order `customer_panel_order_notifications` outbox. It references the saved order; it does not duplicate a subscription URL or other delivery payload into the outbox row.
- `finalize_product_order()` now enqueues the customer notice in the same SQLite transaction as the transition to active and trust-channel event. The notice becomes eligible after a 30-second grace window so the normal immediate response can mark it sent first.
- Added atomic claim/lease recovery, retry backoff, sanitized error codes, dead-letter status, tenant scoping and sent-state updates. The worker only resolves delivery details by joining an active order to its matching active, owner-scoped saved service; it never calls a provider or repeats a purchase.
- Added a customer notification worker to the existing polling loop. If immediate delivery succeeds, the handler marks the queued notice sent; if it fails or the process exits after service finalization, the durable worker can retry. The private subscription URL is sent only in the customer's private chat and is not stored in the outbox or written to logs/trust-channel events.
- Recovery messages include order ID plus My Services and order-history navigation. The Telegram keyboard uses existing supported button types/styles; no unsupported native glass effect is claimed.
- Added regression coverage for delayed availability, tenant isolation, private URL exclusion from outbox storage, successful delivery and sent-state persistence.

### Validation

- Customer store suite: **111 tests passed in 18.336 seconds**, exit 0. Command: `PYTHONPATH=src:scripts/test_support python3 -m unittest discover -s tests -p 'test_customer_store.py' -v`.
- Targeted customer notification-worker regression: **1 test passed in 0.195 seconds**, exit 0. Command: `PYTHONPATH=src:scripts/test_support python3 -m unittest tests.test_customer_bot.CustomerBotUITests.test_panel_order_outbox_recovers_customer_message_with_private_service_details -v`.
- Full isolated regression suite: **513 tests passed in 65.413 seconds**, exit 0. Command: `./scripts/run_isolated_tests.sh`.
- Static checks passed: `python3 -m compileall -q -f src tests scripts`; `bash -n scripts/*.sh`; `node --check customer_webapp/app.js`; `git diff --check`.
- Telegram API documentation was checked before this UI-related implementation: official Bot API **10.3**, dated 2026-08-24: https://core.telegram.org/bots/api and https://core.telegram.org/bots/api-changelog. This change uses the existing `sendMessage` flow and standard inline keyboard behavior; no new Bot API method is assumed.
- Evidence level is **E2 local unit/mock** only. No live Telegram API call, live provider API, production panel/database, N2, mother bot, deployment, service restart, timer activation or secret access was performed.
- Publication status: the six-file change is committed locally using the repository's verified existing author identity. A non-force push was attempted and rejected because this workspace has no GitHub HTTPS credentials (`could not read Username`); remote `feature/discount-codes` remains at `11c0073f977381598dbbb2eb58a87e30917a8ee1`. Do not force-push. Publish this local commit non-forcefully after GitHub authentication is restored.

### Known limits and next step

- Telegram delivery is at-least-once, not exactly-once: a network failure after Telegram accepted a message but before the client received confirmation can still cause a duplicate notice. The worker never repeats provider creation or wallet charging.
- There is no operator-facing dead-letter requeue command for this new panel-order queue yet; dead letters remain visible in SQLite for a future admin-operations slice. The normal My Services and order-history paths remain available as customer recovery paths.
- Continue Phase 2 by auditing product detail/quote clarity and verifying fulfillment-state alignment across manual, file, inventory and panel-backed orders. Do not repeat completed navigation/wallet recovery work or run live panel tests.
