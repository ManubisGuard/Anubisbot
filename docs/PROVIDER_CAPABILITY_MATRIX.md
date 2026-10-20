# MANUBIS Bot 2 — Provider Capability Matrix

Updated: 2026-10-20. Scope: source-level audit of `src/manubisguard/panels/adapters/{pasarguard,rebecca,sanaei}.py`, the provider-neutral contract, reseller provider paths, and isolated tests. This document deliberately distinguishes local code/mocks from deployed-panel acceptance.

## Evidence legend

- **Implemented / E2 partial**: adapter method exists and at least one isolated mock test exercises the behavior; this does not prove the real panel version or credential accepts it.
- **Implemented / E2 gap**: method exists, but a relevant mode/operation lacks a direct regression test.
- **Unsupported**: adapter intentionally exposes no operation or fails closed for that mode.
- **Live acceptance**: none recorded for any provider. `capability_report()` explicitly leaves `live_panel_verified=false`; panel permissions remain unknown.

## Operation matrix

| Operation | PasarGuard | Rebecca | Sanaei token API | Sanaei legacy username/password |
|---|---|---|---|---|
| Authenticated health/read probe | Implemented; low-impact group endpoint tested | Implemented; `/api/system` mock tested | Implemented; inbound-list probe | Implemented through login + inbound-list path; no separate health-mode test |
| Create user | Implemented; SDK method/model compatibility tested | Implemented; requires configured `service_id`, tested | Implemented; inbound IDs required, whole-GiB limit enforced, tested | Implemented for VLESS/VMess with `/get/:id` preflight and generated UUID; Trojan/Shadowsocks fail closed until their credential payload is modeled; mock-tested |
| Read user/status | Implemented and mapping tested | Implemented and mapping exercised | Implemented and legacy mapping tested | Implemented and legacy mapping tested |
| List users | Implemented, bounded to 200; tested | Implemented, bounded to 200; tested | Implemented, bounded to 200; direct list test gap | Implemented, aggregates/deduplicates by email; one-inbound fixture tested; multi-inbound merge semantics still need a dedicated test |
| Update user | Implemented through SDK `modify_user_by_id`; status/limit regression tested | Implemented through `PUT /api/v2/users/{id}`; status/limit regression tested | Implemented through `POST /panel/api/clients/update/{id}`; identity/status/limit regression tested | Uses the protocol-specific clientId (VLESS/VMess `id`, Trojan `password`, Shadowsocks `email`); one-inbound behavior tested for all four protocol labels |
| Delete user | Implemented through SDK `remove_user_by_id`; regression tested | Implemented through `DELETE /api/user/{id}`; regression tested | Implemented through `/panel/api/clients/del/{id}`; endpoint tested | Uses the protocol-specific clientId (VLESS/VMess `id`, Trojan `password`, Shadowsocks `email`); one-inbound behavior tested for all four protocol labels |
| Read usage | Cumulative counter via SDK user-by-ID response, normalized to bytes; missing counter fails closed; tested | Raw usage response returned; billing consumer recognizes documented/local `used_traffic` and nested counters; endpoint regression tested | Traffic endpoint normalized to byte counters; tested | Sums `up+down` across matching `clientStats`, uses maximum reported `total`, and fails closed on missing/malformed/negative counters; local aggregation tests added; multi-inbound semantics still need staging confirmation |
| Subscription link | Derived from user response; relative URL normalization tested | Derived from user response; create/list fixtures cover links, dedicated failure test gap | **Not currently claimed as supported**: official `/panel/api/clients/links/{email}` returns direct protocol share URIs, not the HTTPS subscription URL used by MANUBIS. Only an explicit valid HTTPS `subscription_url` field is accepted; otherwise fallback fails closed. Mock-tested. | Unsupported by explicit fail-closed error |
| Suspend / activate | Implemented via `update_user(status=disabled|active)`; update path tested | Implemented via `update_user(status=disabled|active)`; update path tested | Implemented via `update_user(enable=false|true)`; token path tested | Same normalized statuses in code, but direct update test gap |
| Reset usage counter | Unsupported; no adapter contract/method | Unsupported; no adapter contract/method | No dedicated reset operation; create payload's `reset` field is not a reset API | No dedicated reset operation |
| Native reseller/admin management | Separate `PasarGuardResellerProvider`; role, owner-role and selected lifecycle behavior tested. Capability report checks SDK methods **and** required SDK models. Live permissions unknown. | Not supported by the normal user adapter. Separate native Rebecca provider is contract/scope gated and fails closed when requirements are absent; local mock tests only. | Unsupported for reseller plans; platform-managed child users only | Unsupported for reseller plans; platform-managed child users only |
| Provider sync/bulk reconciliation | `sync=False`; no generic sync operation | `sync=False`; no generic sync operation | `sync=False`; no generic sync operation | `sync=False`; no generic sync operation |

## Cross-cutting findings

1. `PanelCapabilities` currently models create/delete/update, usage/status, suspend/activate, subscription link, list users, reseller management and health check. It does not define a `reset_usage` capability, although reset is a distinct provider operation in some panels. Do not advertise/reset usage until a provider-specific contract and safe tests exist.
2. `capability_report()` is local SDK/method introspection. It cannot establish API version compatibility, credential scope, provider-side permission, endpoint availability, or successful mutation. A successful health GET is not proof that create/update/delete works.
3. PasarGuard `get_usage()` intentionally reads the cumulative user counter through `get_user_by_id`; the SDK time-series `/usage` endpoint must not be treated as a cumulative billing counter. It rejects missing counters rather than silently billing zero.
4. Sanaei traffic limits are whole GiB only. Token API and legacy inbounds API have different operation paths. The legacy update/delete identifier is protocol-dependent; tests cover VLESS, VMess, Trojan and Shadowsocks identifier selection. Legacy create is restricted to VLESS/VMess because the current `UserCreate` contract does not provide Trojan/Shadowsocks protocol credentials; those creates fail closed before mutation. Legacy usage sums counters across matching `clientStats`, uses the maximum reported total quota, and fails closed when `up`, `down`, or `total` is absent, malformed, boolean, or negative.
5. The official token `/panel/api/clients/links/{email}` endpoint returns direct protocol URLs for a client, potentially several across attached inbounds. These are not equivalent to MANUBIS's HTTPS `subscription_url` field (also consumed by QR generation). The adapter therefore ignores `links` arrays for subscription URLs, accepts only an explicit valid HTTPS `subscription_url`/`subscription_link`, and reports `subscription_link=False` until an actual subscription URL source/configuration is available.
6. Rebecca user creation requires a configured service ID. Its normal adapter's reseller capability is unsupported; native reseller handling is separate and gated by OpenAPI/scope/permission contracts.
7. Provider mutation timeout/ambiguous-result recovery must remain operation-specific. No adapter mock proves whether a remote write happened when a real connection times out.

## Remaining acceptance gates

- **E2 coverage gaps to close in source/tests:** configured source for a true HTTPS subscription URL, multi-inbound list semantics, adapter failure cases for update/delete, and capability-report behavior when configuration validation fails. The adapter now tests that direct share links are not mislabeled as subscription URLs and that missing URLs fail closed.
- **E3/public-contract work done for this slice:** official MHSanaei/3x-ui docs distinguish the client `/links/{email}` response (protocol URLs) from the subscription server endpoints and describe `/get/:email`. See https://github.com/MHSanaei/3x-ui/blob/main/docs/content/docs/en/reference/api/clients.mdx and https://github.com/MHSanaei/3x-ui/blob/main/docs/content/docs/en/reference/api/subscription-server.mdx. These sources ground endpoint semantics only; they do not establish the user's deployed version or permissions.
- **E4/E5/live work blocked:** per-operation staging acceptance on disposable panels, with explicit authorization and credentials supplied through the approved environment. Test create/read/update/status/usage/subscription/delete and timeout-after-write reconciliation separately. No production mutation or live panel calls are authorized by this audit.

## Local evidence from this audit

- Adapter + reseller-provider focused suite after subscription URL contract changes: **50 tests passed in 2.825s**. The tests distinguish direct share links from explicit HTTPS subscription URLs, assert token-mode request path, and verify legacy mode fails before network access.
- Full isolated suite after these tests and contract changes: **376 tests passed in 63.145s (OK)**. Python compileall, Bash syntax, Mini App JavaScript syntax and `git diff --check` passed.
- Evidence level remains E2 (isolated local mock tests); no live panel/provider acceptance is claimed.
