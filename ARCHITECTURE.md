# Multi-panel architecture decisions

## Boundaries

1. Platform domain owns tenants, bot instances, subscriptions, billing, users, reseller permissions, audit events, and background-job state.
2. Panel service authorizes a tenant against a `PanelConnection`, resolves its adapter, checks the required capability, and translates domain commands into adapter calls.
3. Adapters own provider-specific authentication, SDK models, status/usage translation, pagination, retry classification, and API-version quirks.
4. Bot and Mini App clients call the platform API only; they never connect directly to panel APIs or receive panel credentials.

## Shared adapter operations

- health check and version/capability discovery
- create, read, update, suspend/activate, delete user
- read usage/status and subscription URL
- list/synchronize users with a cursor where supported
- reseller/admin operations as a separately capability-gated extension

## Important design constraints

- Every connection belongs to exactly one tenant. A panel ID supplied by a client is never authorization by itself.
- Keep internal user IDs separate from provider user IDs. Persist a mapping keyed by tenant, connection, and provider user ID.
- Use normalized states but preserve provider raw state for diagnostics. Never infer `active` solely from a truthy field.
- Use idempotency keys for create/renew/delete commands and an outbox/queue for durable retries.
- Mutations require audit events; credentials are encrypted at rest and redacted from all logs.
- API and source-based integration are two possible transport strategies, not a reason to mix provider logic into core. Source integration must be an explicitly versioned plugin and must not patch a live panel installation.
- A provider capability is enabled only after verifying the installed panel version and a contract test. Unsupported features return a typed unsupported-capability error.

## Rollout

1. Unit/contract tests using fake SDK clients.
2. Test against a disposable PasarGuard instance with TLS verification enabled.
3. Verify create/read/update/suspend/activate/delete, subscription URLs, usage units, auth expiry, and group/protocol behavior.
4. Add Sanaei and Rebeka adapters only after confirming their exact project/version and supported API/source integration.
5. Production rollout only after explicit approval and with backup/rollback plan.
