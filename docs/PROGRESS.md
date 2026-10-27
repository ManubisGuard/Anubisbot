# MANUBIS progress checkpoint

Updated: 2026-10-27

## Workspace and safety boundary

- Repository: https://github.com/ManubisGuard/Anubisbot
- Branch: feature/discount-codes
- Workspace used for this session: /root/bot/workspace/AnubisBot, an isolated clone created from GitHub.
- Starting commit: 79a36bab6e1faef224c2d36fe4ca8c09b80d3afe (Harden panel quote confirmation callbacks).
- /root/bot originally contained project reports and transfer archives, not a Git checkout. /root/fork/AnubisBot was absent on the connected device.
- No secrets were read or printed. No production DB, active panel, N2, live Telegram process, systemd timer, deployment, or service restart was touched.
- Live provider acceptance remains unverified. Automated evidence below is local unit/mock evidence only.

## Completed in this session

1. Re-identified the connected device and verified repository, branch, remote, starting HEAD and clean worktree.
2. Ran the isolated baseline suite: 461 tests passed in 61.143 seconds.
3. Improved the customer-bot admin panel journey:
   - /admin → panel management → paginated panel list (five entries per page).
   - Each panel has a detail screen with current local status and last recorded read-only check.
   - A panel can be selected for a read-only connection check from buttons rather than requiring the admin to type its ID.
   - Callback data carries a 12-character SHA-256-derived token instead of the panel ID, keeping callbacks under Telegram's 64-byte limit and avoiding raw IDs in callback payloads.
   - Stale, malformed or ambiguous tokens fail closed. Admin callback authorization remains enforced by the existing dispatcher.
   - The existing /panel-check PANEL_ID command remains available as a fallback.
4. Added two UI regression tests for pagination, long IDs, callback length, detail navigation, check dispatch and stale-button handling.
5. Focused customer-bot suite: 114 tests passed in 24.224 seconds.
6. Full isolated suite after the code change: 463 tests passed in 63.244 seconds.
7. Static checks passed: Python compileall, Bash syntax, Mini App JavaScript syntax, and git diff --check.

## Current limits / do not overclaim

- The new panel UI lists and inspects panels and can route to an explicitly requested read-only health/capability check. It does not yet provide an audited enable/disable confirmation flow or a multi-step panel-registration wizard.
- A read-only health check does not prove create/update/delete permissions or compatibility with the installed panel version.
- PasarGuard, Rebecca and Sanaei acceptance is still E2 local mock evidence. Live mutation tests require a disposable authorized staging panel and must never be performed against production users.
- Online payment gateways and signed gateway webhooks remain out of scope for this milestone. Manual card-to-card receipt review remains the current payment path.
- Billing systemd files are templates only; do not install or activate timers as part of this work.

## Next steps in order

1. Continue the visible admin UX milestone: add an audited, confirmation-based local panel enable/disable screen; keep it separate from provider mutations and preserve tenant authorization.
2. Add a multi-step panel-registration flow that collects non-secret metadata only. Credentials must remain environment secret references; never request raw keys/passwords in Telegram.
3. Move product/category management from command-only help text toward guided screens while preserving quote revalidation, order snapshots, inventory atomicity and existing command fallbacks.
4. Improve admin order/payment review navigation without weakening receipt verification, immutable wallet ledger, idempotency or reconciliation protections.
5. Continue PasarGuard operation-by-operation contract review. Live staging acceptance remains blocked until a disposable panel and explicit authorization are provided.
6. After each independent implementation step: focused tests, full ./scripts/run_isolated_tests.sh, static checks, reviewed-path-only staging, commit/push to feature/discount-codes. Never use git add -A; never restart services or deploy without explicit authorization.
