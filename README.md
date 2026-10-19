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
- The control-bot factory lifecycle is still a preview: persistent instance registry, automated customer-token provisioning and isolated start/stop/recovery management are incomplete. Customer panel adapters and wallet/manual-receipt flows exist in local code, but are not live-accepted; no automatic gateway/webhook payment integration is enabled.
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

- **Customer panel adapters:** PasarGuard, Rebecca and Sanaei/3x-ui. Adapter requests are HTTPS-only with TLS verification enabled. The API contracts are covered by mocked tests; no live provider panel has been accepted in production.
- **Customer products:** `fixed`, `unlimited`, `hourly`, and `usage`, with tenant-scoped categories and category-filtered shop navigation. Fixed/unlimited products charge once and apply duration/traffic limits; hourly/usage products are billed by the background worker. Usage counter resets are handled without charging the same traffic twice.
- **Resellers:** PasarGuard creates a native admin using a configured role ID. Rebecca has a separate native-admin provider gated by the live OpenAPI schema (create/list/update/disable/enable/delete), explicit numeric service scope, reseller role defaults and limits. Unsupported contracts, missing scope or insufficient API-key permissions fail closed before wallet reservation. Child-config purchases for native Rebecca representatives are also blocked until a scoped per-admin API key is available, preventing owner-key scope bypass. Sanaei/3x-ui remains customer-config-only and is rejected before wallet debit.
- **Encrypted reseller credentials:** Native PasarGuard and Rebecca reseller passwords are encrypted at rest using `ANUBIS_CREDENTIAL_ENCRYPTION_KEY`. The key must be generated and stored outside source control and backed up separately. A missing/invalid key blocks native reseller purchase before wallet reservation.
- **Wallet and support:** `/deposit AMOUNT_MINOR [CURRENCY]` creates a pending payment intent and support ticket; `/ticket-reply TICKET_ID | text` adds a receipt reference. An admin must independently verify payment and then run `/wallet-verify PAYMENT_ID | AMOUNT_MINOR | CURRENCY | PROVIDER_TRANSACTION_REFERENCE`. Settlement is idempotent and checks the exact requested amount/currency. Automatic payment gateway checkout and signed webhooks are not connected yet. `/wallet-credit USER_ID | AMOUNT_MINOR | CURRENCY | RECEIPT_REFERENCE` remains available for audited manual adjustments.
- **Billing:** `scripts/run_billing_once.py` runs one customer/reseller billing pass. `deploy/systemd/anubis-billing.service` and `.timer` are templates only and have not been installed/activated. Hourly/usage billing will not run continuously until the timer is installed in the isolated customer-bot staging environment.
- **Trust channel:** wallet-credit and completed order/reseller events are written to the privacy-filtered outbox in the same SQLite transaction as the business state change. The billing timer also drains the outbox with retries so a process crash after commit does not permanently drop the notification.

### Useful commands

- Customers: `/start`, `/shop`, `/wallet`, `/deposit 100000 IRR`, `/myservices`, `/myresellers`, `/support`, `/mytickets`, `/ticket TICKET_ID`, `/ticket-reply TICKET_ID | message`, `/resume-service SERVICE_ID`, `/reseller-resume ACCOUNT_ID`.
- Admin panel setup: `/panel add ...` (re-run with the same ID to edit), `/panel-enable PANEL_ID on|off`, `/panels`, `/panel-check PANEL_ID`, `/panel-roles PANEL_ID` (PasarGuard only). Disabling a panel hides its products and stops new reseller purchases; existing services are not deleted.
- Product catalog: `/category add ID | title | optional description`, `/categories`, `/category enable ID on|off`; `/product add ID | title | mode | price_minor | unit_price_minor | currency | duration_hours | traffic_GiB | panel_id | group/service/inbound_ids | optional_category_id` (the 10-field form remains supported), `/product bind ID | panel_id | IDs`, `/product enable ID on|off`, `/products`. Every published product must be bound to an enabled panel and have provider-specific IDs; the six-field legacy form auto-binds only when exactly one enabled panel has valid defaults.
- Reseller plans: `/reseller-plan add ...` (12-field basic or 21-field extended form), `/reseller-plans`.
- Support and payments: `/tickets`, `/reply TICKET_ID | message`, `/wallet-verify ...`, `/wallet-credit ...`.

See [`docs/PANEL_ADAPTER_MATRIX.md`](docs/PANEL_ADAPTER_MATRIX.md) and [`docs/RESELLER_AND_METERED_BILLING.md`](docs/RESELLER_AND_METERED_BILLING.md) for provider limits, command examples, wallet/debt behavior and staging acceptance steps.

## Verification status (2026-10-20)

- `python3 -m compileall -q -f src tests scripts`: passed (exit code 0).
- `./scripts/run_isolated_tests.sh`: **190 tests passed (`OK`) in 37.074 seconds** after the isolated runner was added.
- The isolated runner creates temporary HOME/TMP/cache directories, clears inherited environment variables, blocks IPv4/IPv6 network connections, and enforces a 20-second per-test timeout by default.
- Test evidence is local unit/mock only. It does not prove live Telegram delivery, live provider CRUD/usage, automatic gateway/webhook processing, or production database behavior.
- `deploy/systemd/anubis-billing.service` and `.timer` are templates only. They have not been installed or activated; their paths need review before any future staging use.

**Operational boundary:** no production panel, N2, live mother-bot process, or production deployment was changed during this session. No live provider or payment gateway acceptance is claimed.
