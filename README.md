# MANUBIS Bot — Customer Product Core

An independent project directory for the MANUBIS multi-panel bot platform. The pre-existing mother/factory bot is separately named **Anubis Bot Creatore** and is outside this project’s live operational scope; do not rename, restart or deploy it from this workspace. The existing reference checkout and `/root/fork/main.zip` are not modified by this project.

## Architecture

- `PanelAdapter` is the provider-neutral contract used by application services.
- `PanelConnection` contains tenant and connection metadata plus a **secret reference**, never a raw credential.
- `PanelCapabilities` explicitly describes what a provider can do.
- `AdapterRegistry` resolves adapters by provider kind.
- Independent adapters exist for PasarGuard, Rebecca and Sanaei/3x-ui. They have local contract/unit tests; exact deployed versions and live panel permissions are not yet staging-accepted.

## Security and operational rules

- Resolve credentials from a secret store via `secret_resolver`; do not place raw tokens/passwords in logs, DB rows, exceptions, or connection objects.
- Use TLS verification by default. Do not disable it to make a failed connection pass.
- Enforce tenant ownership and authorization in the application service before calling an adapter.
- Treat delete, suspend, renewal, and quota changes as audited operations. Add idempotency keys at the service/worker boundary before exposing them to users.
- Capabilities are not proof of compatibility with every deployed version. Run health/version discovery and adapter contract tests against a disposable test panel before production use.

## Initial status

PasarGuard methods are wired against the SDK call patterns found in the existing reference project. This is a first implementation scaffold, not a claim of live-panel compatibility. Exact status semantics, reseller-management methods, group selection, and usage response fields must be contract-tested against the target PasarGuard version. No panel credentials or production endpoints are included.

## Development

```bash
python -m pip install -e '.[test]'
pytest
```


## Product names and separation

- Customer-facing product brand: **MANUBIS**; product descriptor: **MANUBIS Bot**.
- Mother bot / factory name: **Anubis Bot Creatore**.
- The current Python package and environment-variable prefixes retain their historical internal identifiers to avoid a risky broad refactor; user-facing branding is being migrated independently.

## Mother bot and customer bot are separate products

- Mother-bot Telegram UI: `src/manubisguard/telegram/control_bot.py`. It is only for creating/listing/managing isolated customer bot instances. It must not contain panel connections, reseller plans, customer sales, or customer payments.
- Customer-facing bot UI: `src/manubisguard/telegram/customer_bot.py`. Its menu owns the product areas: shop, customer services, panel connections, resellers, wallet/payments, trust channel, support and product settings.
- Native Telegram button styles are sent through the Bot API `style` field: `primary`, `success`, and `danger`. These are Telegram-defined colors, not arbitrary CSS colors.
- Static responsive mother-bot Mini App: `webapp/index.html`. It contains the bot-factory workflow only; it no longer exposes panel/reseller management as mother-bot pages.
- The control-bot factory lifecycle is still a preview: persistent instance registry, automated customer-token provisioning and isolated start/stop/recovery management are incomplete. Both bot entrypoints now fail closed unless `MANUBISGUARD_TEST_MODE=1` is explicitly set; the local test runners set this flag deliberately. Customer panel adapters and wallet/manual-receipt flows exist in local code, but are not live-accepted; no automatic gateway/webhook payment integration is enabled.
- The existing `@ManubisVPNBot` staging token belongs to the mother bot. Do not reuse it for the customer bot. The customer test runner requires a separate `.env.customer.test` and token.


### Preview boundary

A static factory Mini App preview and its deployment-specific configuration remain in the local workspace, but are intentionally outside this backend base-commit allowlist until the factory/customer boundary is split. The preview is not an authenticated web application. No deployment or live-service change is performed by the local test workflow.

## Editable bot content and branding

Both Telegram admin interfaces now expose **مدیریت متن‌ها و لوگو**. An administrator can edit the bot's display title and welcome copy, upload the in-chat logo by sending a photo, change that bot's menu labels, and override the copy shown on each supported page. Send `/clear` to restore a field's default or `/cancel` to leave an edit unchanged. The editor is scoped: the mother bot exposes factory fields, while each customer bot exposes its own shop/service/panel/reseller/wallet/support fields.

- Content overrides persist in SQLite. Configure `ANUBIS_CONTENT_DB` to a writable durable path.
- Use `ANUBIS_MOTHER_CONTENT_NAMESPACE=mother` for the factory and a unique `ANUBIS_CUSTOMER_CONTENT_NAMESPACE=customer:<tenant-id>` per customer bot when multiple namespaces share the same DB.
- The mother editor's **Mini App logo URL** accepts an HTTPS image URL. The mother content store publishes the public title, logo URL, welcome copy, and whitelisted Mini App copy fields to `ANUBIS_BRAND_JSON`; the static Mini App loads these from `/brand.json` and applies them to its editable text slots.
- The in-chat Telegram logo is a Telegram `file_id`; it is sent on `/start`. Changing the actual Telegram profile avatar or username still requires BotFather.
- This content editor does not yet turn the static Mini App into a full authenticated web-admin, and it does not provision customer bots or enable live payments/panel mutations.


## Customer bot: reseller plans and metered configurations

`src/manubisguard/customer_store.py` now persists customer identities, product catalog, panel connections, orders, service state, wallet ledger, payment intents, support tickets, reseller accounts, encrypted native-reseller credentials, billing cursors, debt and a transactional trust-notification outbox. The ledger uses integer currency units, `BEGIN IMMEDIATE`, idempotency keys and non-negative-balance protection.

### Current implementation

- **Customer panel adapters:** PasarGuard, Rebecca and Sanaei/3x-ui. Adapter requests are HTTPS-only with TLS verification enabled. `/panel-check PANEL_ID` now reports operation-by-operation local adapter/SDK coverage separately from unknown panel permissions; `manage_resellers` and `reset_usage` are explicitly unsupported in Rebecca/Sanaei adapters. A successful health check is read-only and does not certify mutations. The API contracts are covered by mocked tests; no live provider panel has been accepted in production.
- **Customer products:** `fixed`, `unlimited`, `hourly`, and `usage`, with tenant-scoped categories and category-filtered shop navigation. Fixed/unlimited products charge once and apply duration/traffic limits; hourly/usage products are billed by the background worker. Usage counter resets are handled without charging the same traffic twice.
- **Resellers:** PasarGuard creates a native admin using a configured role ID. Rebecca has a separate native-admin provider gated by the live OpenAPI schema (create/list/update/disable/enable/delete), explicit numeric service scope, reseller role defaults and limits. Unsupported contracts, missing scope or insufficient API-key permissions fail closed before wallet reservation. Child-config purchases for native Rebecca representatives are also blocked until a scoped per-admin API key is available, preventing owner-key scope bypass. Sanaei/3x-ui remains customer-config-only and is rejected before wallet debit.
- **Encrypted reseller credentials:** Native PasarGuard and Rebecca reseller passwords are encrypted at rest using `ANUBIS_CREDENTIAL_ENCRYPTION_KEY`. The key must be generated and stored outside source control and backed up separately. A missing/invalid key blocks native reseller purchase before wallet reservation.
- **Wallet and support:** `/deposit AMOUNT_MINOR [CURRENCY]` creates a pending payment intent and support ticket; `/ticket-reply TICKET_ID | text` adds a receipt reference. An admin must independently verify payment and then run `/wallet-verify PAYMENT_ID | AMOUNT_MINOR | CURRENCY | PROVIDER_TRANSACTION_REFERENCE`. Settlement is idempotent and checks the exact requested amount/currency. Automatic payment gateway checkout and signed webhooks are not connected yet. `/wallet-credit USER_ID | AMOUNT_MINOR | CURRENCY | RECEIPT_REFERENCE` remains available for audited manual adjustments.
- **Billing:** `scripts/run_billing_once.py` runs one customer/reseller billing pass. `deploy/systemd/anubis-billing.service` and `.timer` are templates only and have not been installed/activated. Hourly/usage billing will not run continuously until the timer is installed in the isolated customer-bot staging environment.
- **SQLite backup/restore helper:** `scripts/customer_db_maintenance.py` supports `backup`, `verify`, and restore-to-new-path with integrity/FK checks, destination no-overwrite, and mode `0600`. Usage and key-recovery limits are documented in `docs/CUSTOMER_DB_BACKUP_RESTORE.md`; this is not a live DB backup or cutover.
- **Database strategy:** see `docs/DATABASE_STRATEGY.md`. Current SQLite backend uses WAL on the same host and `synchronous=FULL`; PostgreSQL is the target production backend after repository abstraction, parity tests, migration rehearsal and restore acceptance. PostgreSQL support is not implemented yet.

**Customer Mini App (separate from the factory preview):** `customer_webapp/` plus `src/manubisguard/web/customer_app.py` implements Telegram initData HMAC verification, HTTPS-origin checks, tenant/user-scoped shop/wallet/services/order/wallet-transaction history, wallet-backed purchase, and a dedicated Persian `پشتیبانی` tab with idempotent ticket submission/history. Support tickets use a durable notification outbox and configurable numeric Telegram chat/topic routes. Only `support_ticket` is wired to the new route registry; purchase/payment/log/backup event routing and the admin backup scheduler remain incomplete. See `docs/CUSTOMER_MINIAPP.md` and `docs/ADMIN_NOTIFICATIONS_AND_BACKUPS.md`; live HTTPS deployment and Telegram launch remain unaccepted.
- **Trust channel:** wallet-credit and completed order/reseller events are written to the privacy-filtered outbox in the same SQLite transaction as the business state change. The billing timer also drains the outbox with retries so a process crash after commit does not permanently drop the notification.
- **Manual-product fulfillment notifications:** migration v5 creates a separate tenant/order-scoped customer-delivery outbox. Staff fulfillment and notification enqueue commit atomically; retries use exponential backoff and recover stale claims after restart. Eight failed attempts dead-letter the notification; admins can requeue with `/manual-notification-retry ORDER_ID`. Delivery is at-least-once because Telegram send does not expose an idempotency key; order debit/fulfillment itself is not repeated.
- **Support ticket outbox and notification routes:** migration v6 creates a durable ticket-notification outbox; migration v7 adds per-tenant event-to-chat/topic destinations. `/notify-route list`, `/notify-route add EVENT_KEY | CHAT_ID | TOPIC_ID`, and `/notify-route remove EVENT_KEY | CHAT_ID | TOPIC_ID` manage routes; only support-ticket delivery is connected so far. Admins can requeue dead ticket notices with `/ticket-notification-retry TICKET_ID`.
- **Backup policy configuration:** migration v8 stores per-tenant schedule/timezone, local/Telegram/both destination, chat/topic, local directory and retention policy. `/backup-policy show|schedule|destination|retention` is admin-only. This is configuration only; the automatic scheduler and Telegram document upload are not implemented and cannot be enabled.
- **Factory registry and token storage (local core):** `factory_store.py` persists customer bot metadata, owner-scoped listing, allowlisted lifecycle transitions and append-only audit events. `factory_secrets.py` stores tokens encrypted at rest using `ANUBIS_CREDENTIAL_ENCRYPTION_KEY`, with create/read/rotate/revoke operations; `factory_service.py` compensates for failed registration. This is a local encrypted SQLite adapter, not a networked Vault service; it has no per-instance OS authorization, is not connected to Telegram UI, and does not start/stop services. No live token flow is enabled.

### Useful commands

- Customers: `/start`, `/shop`, `/wallet`, `/deposit 100000 IRR`, `/myservices`, `/myresellers`, `/support`, `/mytickets`, `/ticket TICKET_ID`, `/ticket-reply TICKET_ID | message`, `/resume-service SERVICE_ID`, `/reseller-resume ACCOUNT_ID`.
- Admin panel management: open `/admin → مدیریت پنل‌ها` for a paginated list, per-panel details, a read-only connection check, and confirmation-based enable/disable of local sales. The `افزودن پنل جدید` flow now guides the admin through provider/auth mode, internal ID, label, HTTPS URL, environment-secret reference, optional Sanaei subscription origin/path, and local sales state. It never asks for the actual credential in chat and does not call or mutate the provider during registration. State changes require an authorized admin, bind to the state shown on the confirmation page, and are recorded in an append-only audit table. Command fallbacks remain: `/panel add ...` (re-run with the same ID to edit), `/panel-enable PANEL_ID on|off`, `/panels`, `/panel-check PANEL_ID`, `/panel-roles PANEL_ID` (PasarGuard only).
- Product catalog: `/category add ID | title | optional description`, `/categories`, `/category enable ID on|off`; `/product add ID | title | mode | price_minor | unit_price_minor | currency | duration_hours | traffic_GiB | panel_id | group/service/inbound_ids | optional_category_id` (the 10-field form remains supported), `/product bind ID | panel_id | IDs`, `/product enable ID on|off`, `/products`. Every published product must be bound to an enabled panel and have provider-specific IDs; the six-field legacy form auto-binds only when exactly one enabled panel has valid defaults.
- Manual products: `/manual-product add ID | title | price_minor | currency | instant/inventory/manual | delivery_text | description | category_id? | purchase_cap? | sort_order? | terms_text? | image_url?`, `/manual-product edit` uses the same optional fields; terms are shown at checkout and snapshotted on the order. Optional image URLs must use HTTPS; obvious local/private host forms are rejected, but DNS resolution is not checked. Use `-` in the final field to clear an existing image. Product cards use Telegram photos with a text fallback if image delivery fails. `/manual-key add PRODUCT_ID | KEY`, `/manual-orders [pending_delivery|fulfilled|all]`, `/manual-fulfill ORDER_ID | delivery_text`, `/manual-notification-retry ORDER_ID` (for dead-lettered customer delivery notifications).
- Reseller plans: `/reseller-plan add ...` (12-field basic or 21-field extended form), `/reseller-plans`.
- Support and payments: `/tickets`, `/reply TICKET_ID | message`, `/ticket-notification-retry TICKET_ID`, `/notify-route list`, `/notify-route add support_ticket | CHAT_ID | TOPIC_ID`, `/notify-route remove support_ticket | CHAT_ID | TOPIC_ID`, `/wallet-verify ...`, `/wallet-credit ...`.
- Backup policy: `/backup-policy show`, `/backup-policy schedule | daily | Europe/Berlin | 03:00 | 1440`, `/backup-policy destination | local | 0 | 0 | /var/lib/anubis-bot/backups`, `/backup-policy retention | 14 | 30`. Configuration is persisted per tenant; automatic execution is not implemented and `enable` is refused.

Provider limitations, wallet/debt behavior, and operational boundaries are summarized in the **Current implementation** section above. The separate provider-matrix and metered-billing guides are not included in this source checkout.

## Verification status (2026-10-27)

- Latest full isolated run after the guided panel-registration wizard, 15-minute form expiry, and duplicate-ID protection: **471 tests passed (`OK`) in 64.004 seconds**. Five new wizard journey tests cover validation/secret references, Sanaei metadata, cancellation, expiry, and safe rejection of an existing panel ID.
- Static checks passed: `python3 -m compileall -q -f src tests scripts`, `bash -n scripts/*.sh`, `node --check customer_webapp/app.js`, and `git diff --check`.
- The isolated runner creates temporary HOME/TMP/cache directories, clears inherited environment variables, blocks IPv4/IPv6 network connections, and enforces a 20-second per-test timeout by default.
- The customer-bot admin panel menu has a paginated panel list, per-panel details, a read-only connection-check action, and confirmation-based enable/disable of local sales. Callback data uses short hashed panel references below Telegram's 64-byte limit; stale or ambiguous references fail closed. Migration v21 adds an append-only audit trail with actor, previous/new state, source, and idempotency key; stale confirmations are rejected.
- Test evidence is local unit/mock only. It does not prove live Telegram delivery, live provider CRUD/usage, automatic gateway/webhook processing, or production database behavior.
- `deploy/systemd/anubis-billing.service` and `.timer` are templates only. They have not been installed or activated; their paths need review before any future staging use.

**Operational boundary:** work was performed in the isolated checkout `/root/bot/workspace/AnubisBot`. No production panel, N2, live bot process, production database, or deployment was changed. No process was restarted, and no live provider or payment gateway acceptance is claimed. See `docs/PROGRESS.md` for the current checkpoint and next steps.
