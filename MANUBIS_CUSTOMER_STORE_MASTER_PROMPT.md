# MANUBIS Customer Store — Master Prompt for Continuing Work

Updated: 2026-10-28
Repository: https://github.com/ManubisGuard/Anubisbot
Branch: `feature/discount-codes`
Baseline before the 2026-10-28 receipt-review stage: `351e92bde6bca0b59cf2f049982b194c8cf0afb1`. For the latest published HEAD, always verify Git directly and consult `docs/PROGRESS.md`.
Workspace: `/root/bot/workspace/AnubisBot`

## Start here every time

Before changing code, inspect the real environment and read:
1. This file: `MANUBIS_CUSTOMER_STORE_MASTER_PROMPT.md`
2. `docs/PROGRESS.md` (single chronological project/resume log; append evidence after every meaningful step)
3. `README.md`
4. Current source/tests for the exact feature being changed.

Then verify device identity, repo path, branch, HEAD, remote and `git status`. Device IDs can change; do not assume a remembered device is the same machine. Do not infer project state from an old chat or stale handoff.

## Product scope and architecture

- Public brand: **MANUBIS**. This is Bot 2 / customer store. The separate mother/factory bot is **Anubis Bot Creatore** and is out of scope unless the user explicitly changes priority.
- One unified catalog/orders/payments/wallet/discounts/notifications/support/audit system. Products choose fulfillment: manual, code/account inventory, file, or provider panel. Do not create a separate VPN-only sales system.
- Current backend remains Python under `src/manubisguard` with SQLite. PostgreSQL is a later installer choice, not a reason to rewrite the current store.
- Provider adapters: PasarGuard, Rebecca and Sanaei/3x-ui. Sanaei does not support reseller creation; Rebecca and PasarGuard may, subject to verified capabilities and permissions.
- Capability states must distinguish `verified/allowed/denied/unknown/unsupported/failed/not_tested`. Keep integration mode separate from authentication mode.
- Manual card-to-card receipt review is in scope. Online gateways and automated payment webhooks are deferred.
- Preserve existing order snapshots, quote revalidation, immutable wallet ledger, integer money, idempotency, inventory atomicity, audit trails and recovery of paid-but-undelivered orders.

## Mandatory Telegram API / UI engineering standard

**For every Telegram-related implementation, check the latest official Bot API documentation before coding.** Do not assume an API version or feature is current from memory. Record the checked API version/date and the relevant official documentation link in the progress log for meaningful Telegram UI/API changes.

Official source: https://core.telegram.org/bots/api

At the time of this checkpoint, the official documentation shows **Bot API 10.3 (2026-08-24)**. It documents native button `style` values `primary` (blue), `success` (green) and `danger` (red) for supported keyboard buttons. Use native styles semantically: primary for main navigation/forward actions, success for confirmed/positive actions, danger for destructive/reject/cancel actions. Do not invent arbitrary native Telegram button colors. Verify button-type/client compatibility and keep safe fallback behavior when needed. Keep `callback_data` within Telegram's 1–64 byte limit; prefer short, versioned/tokenized identifiers over long raw payloads.

"Glass" / glassmorphism must be described precisely:
- Telegram's normal native inline/reply keyboard buttons do not expose an arbitrary CSS opacity, blur, gradient or glass-transparency control. Do not claim that adding `style` creates a glass effect.
- For a Telegram Mini App/Web App, use accessible CSS glassmorphism (translucent surfaces, backdrop blur with fallback, clear contrast, responsive layout, reduced-motion support) where it improves usability.
- For regular bot chat UI, use Telegram-native button styles, emoji/icons, spacing, clear hierarchy, concise labels and consistent inline navigation. Never promise a visual effect that the Telegram client does not support.

UI quality requirements:
- Persian-first, responsive where applicable, coherent navigation, Back/Home paths, max 6–8 buttons per screen when possible.
- Confirm financial/destructive actions. Re-check admin/tenant/role and expected prior state inside the handler, not only when rendering the button.
- All user-visible strings should use the editable content system where supported. Do not render fake/unsupported controls; hide them or label them as unavailable/coming soon.
- Test callback lengths, stale callbacks, duplicate/replayed callbacks, unauthorized users and Telegram API payload shape.
- Never copy a reference project's source. Learn behavioral patterns and implement clean-room.

## Operational and Git safety rules

- Do not touch the production panel at `/opt/manubisguard-panel`, N2, mother bot, production DB, secrets, live Telegram processes or systemd timers unless the user explicitly authorizes the exact operation.
- Do not deploy, restart, stop or mutate a live service merely to test a feature. Prefer local isolated tests and mocks.
- Never read/print secrets or tokens. No raw provider credentials in DB/logs/exceptions; use secret references.
- After each independent **code** step: focused tests, full `./scripts/run_isolated_tests.sh`, static checks, inspect diff and status. For docs-only changes, verify `git diff --check`, reviewed diff, blob hashes, publication and clean synchronization; do not rerun code tests without a specific reason.
- Stage only explicitly reviewed paths. Never use `git add -A`.
- If local commit is blocked by Git identity or excludes, do not invent an author or force unsafe local changes. Verify file blob hashes against local `git hash-object`, then use the repository's supported commit workflow and update the branch non-forcefully against the expected parent. Verify remote HEAD and synchronize the workspace after publication.
- Report actual commit/hash/test output. Distinguish `unit/mock`, `public-source`, and `live` evidence. Never describe mocked evidence as live acceptance.

## Current product roadmap

- Completed locally/published: panel management/read-only checks; guided panel registration; category management; guided product creation for manual/inventory/file/panel fulfillment; guarded manual receipt review with audit navigation.
- Latest adapter hardening (2026-10-28): PasarGuard rejects boolean/negative/malformed counters and incomplete auth before secret resolution. Rebecca and Sanaei reject boolean expiry/usage/quota fields; Sanaei fails closed on malformed mapped usage instead of silently returning zero. All three user-list APIs now reject a missing list envelope; Sanaei also rejects malformed list entries/settings instead of silently returning a partial snapshot. Mock timeout tests assert no blind retry for create/update/delete.
- Phase 1 pagination public-source research is recorded; exact deployed versions and completeness remain unknown. Do not repeat adapter tests or implement auto-pagination without new version-specific evidence.
- Phase 2 progress: compact catalog navigation, insufficient-wallet recovery, sanitized reconciliation status, manual receipt-status follow-up, owner-scoped redelivery of fulfilled manual/file/inventory payloads, and active-panel order navigation to My Services are implemented. Order history is limited to four orders per page to keep worst-case keyboard size at eight buttons. Latest focused customer-bot suite: 146 passed in 27.120 seconds; latest full isolated suite: 511 passed in 62.408 seconds. Next audit durable notification recovery after panel service creation; never repeat provider creation just because Telegram delivery failed.
- Live staging acceptance is blocked until a disposable panel and explicit authorization exist.
- Online gateways remain deferred. Mini App expansion is later roadmap work.

## Handoff rule

A future AI must first check this file and `docs/PROGRESS.md`, then verify current repository state and continue from the latest completed checkpoint, not restart the roadmap or repeat completed test-only work. Append a dated entry to `docs/PROGRESS.md` after each meaningful step with: scope, files, behavioral changes, exact test counts/commands, static checks, commit/branch/HEAD, evidence level, known limits, and what remains. Keep this file as the single main prompt and `docs/PROGRESS.md` as the single chronological progress log; do not scatter competing handoff prompts across the repository.

## Reference bots — fixed behavioral sources

These are the four user-approved reference bots. Use them to compare product behavior and journeys only; do not copy source code or assume their current features without checking reliable evidence.

1. **Faoxima** — multi-panel/server operations, capability reports, manual/inventory fulfillment, panel backups, trust channel, test account, reservation/automatic activation, payment gateways, dynamic exchange rates, tickets and Mini App.
2. **PasarguardBot** — panel/service integration and the baseline panel-backed service lifecycle: user/service management, activation, sales and renewal behavior.
3. **PGClockBot** — renewal preview, service-mutation locking, live “my services”, paid-but-undelivered recovery, receipt approve/reject/resend, tickets, roles, scheduled jobs and backup/restore.
4. **MirzaBot** — driver/adapter registry, web-admin customization, languages, phone/channel gates, and expiry/payment/notification jobs.

Do not conflate reference bots with provider adapters. The supported provider order is **PasarGuard → Rebecca → Sanaei/3x-ui**. Sanaei does not offer reseller creation; only PasarGuard and Rebecca may expose native reseller management, and only when the exact provider contract, permission and configuration are verified.

## Test ledger and anti-duplication rule

Latest verified checkpoint on 2026-10-28:
- Latest full isolated suite after discount preview clarity: **515 tests passed in 64.588 seconds**, exit 0, command `./scripts/run_isolated_tests.sh`.
- Latest focused customer bot UI suite: **149 tests passed in 28.154 seconds**, exit 0, command `PYTHONPATH=src:scripts/test_support python3 -m unittest discover -s tests -p 'test_customer_bot.py' -v`.
- Latest focused customer store suite: **109 tests passed in 17.965 seconds**, exit 0, command `PYTHONPATH=src:scripts/test_support python3 -m unittest discover -s tests -p 'test_customer_store.py' -v`.
- Focused combined provider adapter suites: **76 tests passed** across PasarGuard, Rebecca and Sanaei.
- Static checks passed: `python3 -m compileall -q -f src tests scripts`; `bash -n scripts/*.sh`; `node --check customer_webapp/app.js`; `git diff --check`.
- Published baseline before panel quote clarity: `d6429aacb0f9ac612e8288971a4ddfb90587451f` (`Add durable panel-order customer notifications`). Published panel quote clarity commit: `4ce168eba20c009cecfde2c5f99655b91e7c6af1`. The discount preview clarity slice is currently local/unpublished until its reviewed file blobs are published with a non-force update based on the verified remote HEAD.
- Evidence is **E2 local unit/mock**. These results do not prove live provider compatibility or permissions.

Do not rerun the adapter suite or full suite merely to “check again” at the start of the next session. Preserve these as the last-known passing results. Run tests again only when a code change can affect them, a new behavior/contract gap is identified, or a focused regression check is necessary. For docs-only changes, use diff/format/status/hash verification; do not spend a full test run repeating unchanged code. If tests are run for a genuine reason, record exact command, count, duration and exit status.

## Remaining roadmap — execute in this order

**Phase 1 public-source pagination research was performed on 2026-10-28 and recorded in docs/PROVIDER_CAPABILITY_MATRIX.md and docs/PROGRESS.md.** Public docs show offset/limit plus total for current PasarGuard SDK and Rebecca source; current 3x-ui docs distinguish an all-clients endpoint from a newer paged endpoint. However, the project does not pin the PasarGuard SDK and the exact deployed panel versions are unknown, so live/version-specific completeness remains unverified. Do not repeat this source review or run adapter tests without new evidence. Do not implement auto-pagination until the adapter contract preserves enough completeness metadata and deployed-version behavior is established. The next product phase is Phase 2: audit the existing customer purchase journey and identify its real acceptance gaps.

After Phase 1, proceed through these product-first phases. Before each phase, inspect existing code and record what is already complete; do not rebuild existing functionality.

- **Phase 2 — Customer purchase journey:** audit and complete the Persian customer path from catalog/category → product detail/quote → order confirmation → payment choice → order status. Keep snapshots and quote revalidation; every visible button must work or be clearly unavailable.
- **Phase 3 — Payment, wallet and discounts:** complete manual card-to-card receipt submission/review, append-only wallet ledger, duplicate/idempotency protections, discount validation and audit. Online gateways/webhooks remain deferred. Verify paid-but-undelivered states and operator reconciliation; never blindly retry a possibly completed provider mutation or automatically refund an ambiguous result.
- **Phase 4 — Fulfillment by product type:** finish and verify manual fulfillment, code/account inventory, file delivery, and panel-backed fulfillment through one order state machine. Inventory reservation/delivery must be atomic and replay-safe.
- **Phase 5 — PasarGuard fulfillment and recovery:** first provider integration acceptance slice. Verify create/read/update/status/usage/subscription/delete contracts, capabilities and timeout-after-write reconciliation against a disposable staging panel only after explicit authorization. No production mutations.
- **Phase 6 — Support and notifications:** tickets, customer/admin notification outbox, retry/idempotency and trust-channel behavior. Scheduled jobs remain templates until separately authorized; do not install/activate timers.
- **Phase 7 — Rebecca adapter/fulfillment:** implement only gaps found against its evidenced contract, then apply the same isolated staging acceptance gate when authorized.
- **Phase 8 — Sanaei/3x-ui adapter/fulfillment:** same evidence-led process; respect token versus legacy API differences and whole-GiB quota limits. Do not offer reseller creation.
- **Phase 9 — Admin operations:** reports, audit navigation, backup/restore, installation/recovery and configuration UX. Verify backup confidentiality and restore safety before claiming acceptance.
- **Phase 10 — Mini App:** only after the core Telegram store and order lifecycle are coherent. Follow responsive/accessibility/reduced-motion rules; glassmorphism is CSS for the Web App, not a native chat-button capability.
- **Phase 11 — Authorized live acceptance/release:** only with an explicitly approved disposable staging environment and exact authorization. Record each operation and evidence level. Production, N2, mother bot, live Telegram processes, secrets and deployments remain out of scope without separate explicit authorization.

## Next-session handoff in one minute

1. Read this master prompt, then docs/PROGRESS.md, README.md, and docs/PROVIDER_CAPABILITY_MATRIX.md.
2. Verify device label, repository path, branch, local/remote HEAD, clean status and latest GitHub commit. Do not trust old chat state.
3. Durable customer notification recovery after panel service creation is implemented with migration v22. Panel quote and discount preview now use Persian pricing labels, show configured duration/traffic and clearly separate original price, discount amount and final amount. Latest focused customer bot suite: 149 passing; full isolated suite: 515 passing. Next audit delivery/status messaging across manual, file, inventory and panel orders against persisted state and retry behavior. Active panel orders link to My Services; owner-scoped delivery redelivery, catalog navigation, insufficient-wallet recovery, sanitized reconciliation and manual receipt-status follow-up are already implemented. Do not rebuild them. Phase 1 version-specific/live pagination completeness remains unresolved. Do not begin by rerunning adapter tests.
4. Keep the four reference bots and provider order above; preserve product-first milestones and evidence labels.
5. After a meaningful step, update this file only if the governing rules/roadmap changed; append the actual evidence to docs/PROGRESS.md. Keep these two files as the single source of handoff truth.
