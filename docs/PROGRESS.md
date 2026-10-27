# MANUBIS progress checkpoint

Updated: 2026-10-27

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

## Current limits / do not overclaim

- The wizard state is in memory and is lost if the bot process restarts; it does not store partially completed forms. Registration does not prove credentials, API scopes, live connectivity, or compatibility with the installed provider version.
- A read-only health check does not prove create/update/delete permissions or compatibility with the installed panel version.
- PasarGuard, Rebecca and Sanaei acceptance is still E2 local mock evidence. Live mutation tests require a disposable authorized staging panel and must never be performed against production users.
- Online payment gateways and signed gateway webhooks remain out of scope. Manual card-to-card receipt review remains the current payment path.
- Billing systemd files are templates only; do not install or activate timers as part of this work.

## Next steps in order

1. Build guided product creation/edit screens with an explicit fulfillment choice (manual, inventory, file, panel). Preserve quote revalidation, order snapshots, inventory atomicity, existing command fallbacks, and the separation between manual-product and panel-backed purchase flows.
2. Improve admin order/payment review navigation without weakening receipt verification, immutable wallet ledger, idempotency or reconciliation protections.
3. Review PasarGuard operation contracts one by one and add focused mock tests for missing/invalid config, permissions unknown, and ambiguous provider mutation outcomes.
4. Live staging acceptance remains blocked until a disposable panel and explicit authorization are provided.
5. After each independent implementation step: focused tests, full `./scripts/run_isolated_tests.sh`, static checks, reviewed-path-only staging, commit/push to `feature/discount-codes`. Never use `git add -A`; never restart services or deploy without explicit authorization.
