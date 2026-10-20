# MANUBIS Bot 2 — Customer Mini App

This is a separate customer-facing Mini App under `customer_webapp/`. Do not confuse it with the root `webapp/` directory, which is the factory/Bot 1 preview and remains out of scope for Bot 2.

## Implemented API

- `GET /healthz`: public liveness only.
- `GET /api/v1/me`, `/shop`, `/wallet`, `/services`, `/orders`, `/transactions`: require valid Telegram Web App `initData` on every request. Identity and all account reads are scoped to the signed Telegram user and the configured customer tenant. Order and wallet history are capped at 50 and omit internal idempotency keys, provider references, payment notes and private references.
- `GET /api/v1/tickets`: returns only the signed user's ticket list (maximum 50); other users' tickets are never visible.
- `POST /api/v1/tickets`: accepts a bounded subject/message and idempotency key, persists the ticket and an admin-notification outbox row in one transaction. Duplicate submits with the same key return the same ticket. The Telegram bot worker retries notification delivery with backoff and dead-letter state.
- `POST /api/v1/purchase`: wallet-backed purchase only. Requires valid `initData`, an exact allowlisted HTTPS `Origin`, `application/json`, a bounded request body, a validated product ID, and a client idempotency key. The server namespaces that key with the signed Telegram user ID and delegates to `CustomerPurchaseService`; provider mutations remain subject to its preflight and reconciliation protections.
- `POST /api/v1/service-change-preview`: authenticated, origin-protected, read-only live preview for a customer-owned service renewal or traffic top-up. The service-change domain checks ownership, enabled original product, panel capability and provider identity; the endpoint returns an explicit field allowlist and declares `mutates_provider=false` and `reserves_wallet=false`. It does not perform a renewal/top-up or debit the wallet.
- No gateway checkout, webhook, admin API, or panel management route is present. Actual Mini App service-change confirmation/execution is not implemented; online gateway work remains deferred until last.

## Environment

Configure these in the customer web API process and the bot process as applicable:

- `MANUBISGUARD_CUSTOMER_BOT_TOKEN`: the token for the customer bot; used only server-side to verify Telegram's HMAC.
- `ANUBIS_CUSTOMER_DB`: the customer bot's SQLite DB path.
- `ANUBIS_CUSTOMER_TENANT`: the matching tenant ID; must match the bot process.
- `MANUBIS_CUSTOMER_WEBAPP_ORIGINS`: comma-separated exact HTTPS origins, e.g. `https://mini.example` (no path).
- `MANUBIS_CUSTOMER_WEBAPP_URL`: exact HTTPS URL opened by Telegram, e.g. `https://mini.example/`; set in the customer bot process only after static files and API routing are configured.
- `ANUBIS_CREDENTIAL_ENCRYPTION_KEY`: required if configured panel secrets use the encrypted credential vault. Back it up separately from SQLite.

## Same-origin deployment model

Serve `customer_webapp/` as the static site at the configured HTTPS origin and proxy `/api/` to a WSGI process bound only to loopback. The reverse proxy must preserve the `/api/v1/...` path. Example WSGI command after installing the optional web dependency:

```bash
pip install '.[web]'
gunicorn --workers 2 --bind 127.0.0.1:8090 'manubisguard.web.customer_app:create_customer_miniapp_app_from_env()'
```

Do not expose Gunicorn directly to the Internet. Configure TLS, request/body limits, access logs that exclude Authorization and request bodies, and same-origin static/API routing at the reverse proxy. The bot's `web_app` button is emitted only for a valid HTTPS `MANUBIS_CUSTOMER_WEBAPP_URL`; otherwise the bot shows a setup notice instead of a broken link.

## Safety notes

- `initData` is never written to local storage or application logs. It is verified with HMAC-SHA-256, duplicate fields are rejected, and `auth_date` must be recent (default 5 minutes; future skew limited to 30 seconds).
- API responses set `Cache-Control: no-store`; no permissive CORS headers are emitted. POST requests require an allowlisted Origin.
- The browser retains only a per-product idempotency key in `sessionStorage`, not the Telegram auth string. A failed/ambiguous purchase reuses that key; a reconciliation-required response disables another attempt and instructs the user to follow up rather than blindly retrying.
- The UI uses `textContent` for server data and opens subscription URLs only when the API confirms they are HTTPS URLs without userinfo.
- WSGI is synchronous; purchase calls run the existing async service in a fresh event loop. Run behind a production WSGI server with bounded workers and a reverse proxy.

## Validation and limits

The Mini App includes a dedicated Persian `پشتیبانی` tab with subject/message form, ticket history/status and submission feedback. New tickets are written to the customer ticket store and durable admin-notification outbox. The bot's admin command `/notify-route` can configure multiple chat IDs and optional forum topic IDs for the `support_ticket` event; until configured, ticket notices fall back to `MANUBISGUARD_CUSTOMER_ADMIN_IDS`.

The services tab also offers `پیش‌نمایش تمدید` and `پیش‌نمایش افزایش حجم`. These buttons call the read-only preview endpoint and render its allowlisted result with `textContent`; they do not reserve funds or modify a panel. A separate, server-revalidated confirmation/execution flow is still required before these actions can be sold through the Mini App.

Tests validate Telegram signatures, stale/future timestamps, duplicate query fields, origin restrictions, tenant/user-scoped reads, order history, wallet transaction history, support ticket idempotency, notification outbox delivery, group/topic routing, hidden panel metadata, wallet purchase idempotency, safe ambiguous outcomes, and non-leaking errors. These are local tests only. No Mini App URL was configured, no live Telegram launch was performed, no provider panel was contacted, and no service/deployment was changed.