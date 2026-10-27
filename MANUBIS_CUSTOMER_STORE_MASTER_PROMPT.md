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
- After each independent step: focused tests, full `./scripts/run_isolated_tests.sh`, static checks, inspect diff and status.
- Stage only explicitly reviewed paths. Never use `git add -A`.
- If local commit is blocked by Git identity or excludes, do not invent an author or force unsafe local changes. Verify file blob hashes against local `git hash-object`, then use the repository's supported commit workflow and update the branch non-forcefully against the expected parent. Verify remote HEAD and synchronize the workspace after publication.
- Report actual commit/hash/test output. Distinguish `unit/mock`, `public-source`, and `live` evidence. Never describe mocked evidence as live acceptance.

## Current product roadmap

- Completed locally/published: panel management/read-only checks; guided panel registration; category management; guided product creation for manual/inventory/file/panel fulfillment.
- Current next step: improve the admin navigation and guarded review journey for card-to-card wallet receipts, preserving independent verification, immutable wallet ledger, idempotency and audit history.
- Then: continue focused PasarGuard adapter contract verification and missing/unknown permission/ambiguous mutation recovery tests.
- Live staging acceptance is blocked until a disposable panel and explicit authorization exist.
- Online gateways remain deferred. Mini App expansion is later roadmap work.

## Handoff rule

A future AI must first check this file and `docs/PROGRESS.md`, then verify current repository state and continue from the latest completed checkpoint, not restart the roadmap or repeat completed test-only work. Append a dated entry to `docs/PROGRESS.md` after each meaningful step with: scope, files, behavioral changes, exact test counts/commands, static checks, commit/branch/HEAD, evidence level, known limits, and what remains. Keep this file as the single main prompt and `docs/PROGRESS.md` as the single chronological progress log; do not scatter competing handoff prompts across the repository.
