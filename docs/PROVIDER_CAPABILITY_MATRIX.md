# MANUBIS Bot 2 — Provider Capability Matrix

Updated: 2026-10-28. Scope: source-level audit of `src/manubisguard/panels/adapters/{pasarguard,rebecca,sanaei}.py`, the provider-neutral contract, reseller provider paths, and isolated tests. This document deliberately distinguishes local code/mocks from deployed-panel acceptance.

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
| Read user/status | Implemented and mapping tested | Implemented and mapping exercised | Implemented and mapping tested | Implemented; same identity across multiple inbounds is aggregated for read, conflicting duplicate email fails closed |
| List users | Implemented, bounded to 200; tested | Implemented, bounded to 200; tested | Implemented, bounded to 200; direct list test covers mapping, auth header, offset/limit and malformed non-list fail-closed response | Implemented; consistent same-client identity is deduplicated across inbounds and all inbound IDs are retained; conflicting identity fails closed; multi-inbound behavior mock-tested |
| Update user | Implemented through SDK `modify_user_by_id`; status/limit regression tested | Implemented through `PUT /api/v2/users/{id}`; status/limit regression tested | Implemented through `POST /panel/api/clients/update/{id}`; identity/status/limit tested; HTTP 503 failure path asserts `PanelError` and no success mapping | Uses protocol-specific clientId (VLESS/VMess `id`, Trojan `password`, Shadowsocks `email`); rejects multi-inbound users before mutation; one-inbound behavior tested for all four protocol labels |
| Delete user | Implemented through SDK `remove_user_by_id`; regression tested | Implemented through `DELETE /api/user/{id}`; regression tested | Implemented through `/panel/api/clients/del/{id}`; endpoint tested | Uses protocol-specific clientId (VLESS/VMess `id`, Trojan `password`, Shadowsocks `email`); rejects multi-inbound users before mutation; one-inbound behavior tested for all four protocol labels |
| Read usage | Cumulative counter via SDK user-by-ID response, normalized to bytes; missing counter fails closed; tested | Raw usage response returned; billing consumer recognizes documented/local `used_traffic` and nested counters; endpoint regression tested | Traffic endpoint normalized to byte counters; tested | Sums `up+down` across matching `clientStats`, uses maximum reported `total`, and fails closed on missing/malformed/negative counters; local aggregation tests added; multi-inbound semantics still need staging confirmation |
| Subscription link | Derived from user response; relative URL normalization tested | Derived from user response; create/list fixtures cover links, dedicated failure test gap | Explicit valid HTTPS URL accepted; otherwise builds `https://{subscription_base_url}/{subscription_path}/{subId}` only when valid metadata is configured and API returns a `subId`. Direct protocol links are ignored; capability stays false when configuration is absent/invalid. Mock-tested. | Unsupported by explicit fail-closed error |
| Suspend / activate | Implemented via `update_user(status=disabled|active)`; update path tested | Implemented via `update_user(status=disabled|active)`; update path tested | Implemented via `update_user(enable=false|true)`; token path tested | Same normalized statuses in code, but direct update test gap |
| Reset usage counter | Unsupported; no adapter contract/method | Unsupported; no adapter contract/method | No dedicated reset operation; create payload's `reset` field is not a reset API | No dedicated reset operation |
| Native reseller/admin management | Separate `PasarGuardResellerProvider`; role, owner-role and selected lifecycle behavior tested. Capability report checks SDK methods **and** required SDK models. Live permissions unknown. | Not supported by the normal user adapter. Separate native Rebecca provider is contract/scope gated and fails closed when requirements are absent; local mock tests only. | Unsupported for reseller plans; platform-managed child users only | Unsupported for reseller plans; platform-managed child users only |
| Provider sync/bulk reconciliation | `sync=False`; no generic sync operation | `sync=False`; no generic sync operation | `sync=False`; no generic sync operation | `sync=False`; no generic sync operation |

## Cross-cutting findings

1. `PanelCapabilities` currently models create/delete/update, usage/status, suspend/activate, subscription link, list users, reseller management and health check. It does not define a `reset_usage` capability, although reset is a distinct provider operation in some panels. Do not advertise/reset usage until a provider-specific contract and safe tests exist.
2. `capability_report()` is local SDK/method introspection. It cannot establish API version compatibility, credential scope, provider-side permission, endpoint availability, or successful mutation. A successful health GET is not proof that create/update/delete works.
3. PasarGuard `get_usage()` intentionally reads the cumulative user counter through `get_user_by_id`; the SDK time-series `/usage` endpoint must not be treated as a cumulative billing counter. It rejects missing counters rather than silently billing zero.
4. Sanaei traffic limits are whole GiB only. Token API and legacy inbounds API have different operation paths. The legacy update/delete identifier is protocol-dependent; tests cover VLESS, VMess, Trojan and Shadowsocks identifier selection. Legacy create is restricted to VLESS/VMess because the current `UserCreate` contract does not provide Trojan/Shadowsocks protocol credentials; those creates fail closed before mutation. Legacy usage sums counters across matching `clientStats`, uses the maximum reported total quota, and fails closed when `up`, `down`, or `total` is absent, malformed, boolean, or negative.
5. The official token `/panel/api/clients/links/{email}` endpoint returns direct protocol URLs for a client, potentially several across attached inbounds. These are not equivalent to MANUBIS's HTTPS `subscription_url` field (also consumed by QR generation). The adapter ignores `links` arrays for subscription URLs. For generated URLs, admins must explicitly configure `subscription_base_url` as the separate HTTPS subscription-server origin (including its actual port) and `subscription_path` as the panel's configured single path segment; the client response must include a valid `subId`. The admin registration path validates these fields and capability remains false when absent/invalid. No default port or equivalence with panel API `base_url` is assumed.
6. Rebecca and Sanaei user-response mappers reject Python booleans in expiry/usage/quota numeric fields. A boolean is an int subclass in Python and must not be accepted as a byte count or GiB quota. Sanaei mapping also raises a sanitized PanelError for malformed usage values instead of silently normalizing them to zero. Regression tests cover these input shapes; this remains local mock evidence, not provider-version acceptance.
6. Rebecca user creation requires a configured service ID. Its normal adapter's reseller capability is unsupported; native reseller handling is separate and gated by OpenAPI/scope/permission contracts.
7. Provider mutation timeout/ambiguous-result recovery must remain operation-specific. No adapter mock proves whether a remote write happened when a real connection times out.
8. PasarGuard user/usage mapping rejects boolean counters rather than coercing Python True to 1; present-but-invalid or negative usage counters fail closed. Unsupported auth modes and username/password configuration without a non-empty username are rejected before the secret resolver is called. These checks validate local inputs only and do not establish live permissions or provider-version compatibility.

## Remaining acceptance gates

- **E2 coverage gaps to close in source/tests:** additional legacy suspend/activate edge coverage and deeper multi-inbound usage/read edge cases. Token-mode update HTTP 503 is now covered as a fail-closed path. URL generation has local tests for valid/invalid origin/path metadata, missing `subId`, explicit HTTPS URL, and direct-link rejection.
- **E4 live acceptance still required:** confirm the configured origin/port/path matches the deployed Subscription Server and that the returned `subId` actually resolves. No live request has been made.
- **E3/public-contract work done for this slice:** official MHSanaei/3x-ui docs distinguish the client `/links/{email}` response (protocol URLs) from the subscription server endpoints and describe `/get/:email`. See https://github.com/MHSanaei/3x-ui/blob/main/docs/content/docs/en/reference/api/clients.mdx and https://github.com/MHSanaei/3x-ui/blob/main/docs/content/docs/en/reference/api/subscription-server.mdx. These sources ground endpoint semantics only; they do not establish the user's deployed version or permissions.
- **E4/E5/live work blocked:** per-operation staging acceptance on disposable panels, with explicit authorization and credentials supplied through the approved environment. Test create/read/update/status/usage/subscription/delete and timeout-after-write reconciliation separately. No production mutation or live panel calls are authorized by this audit.

## Local evidence from this audit

- Sanaei adapter suite: **22 tests passed in 0.083s**; includes token-mode update HTTP 503 failure handling. The targeted test checks that the provider error becomes `PanelError` and no successful mapped user is returned.
- Full isolated suite: **383 tests passed in 79.851s (OK, exit 0)**. Python compileall, Bash syntax, Mini App JavaScript syntax, and `git diff --check` all passed.
- Evidence level remains E2 (isolated local mock tests); no live panel/provider acceptance is claimed.


## Session 129 addendum — user lookup and reconciliation error classification (2026-10-21)

- Added direct mock tests for all three adapters: definitive 404 during user lookup is represented as `PanelUserNotFound`; 401/403/500 and transport errors remain generic `PanelError`; successful but malformed responses without user identity fail closed. Error-body/transport details are not exposed.
- Found and fixed a real PasarGuard defect: the not-found type was used but not imported. `_map_user()` now rejects blank/missing usernames, and `get_user()` preserves `PanelError` from validation rather than wrapping it into an opaque generic failure.
- Focused adapter suite: **51 tests passed in 0.177s**. Full isolated suite: **432 tests passed in 103.173s (OK, exit 0)**. Compileall, Bash syntax, `node --check customer_webapp/app.js`, and `git diff --check` passed.
- E2 local mock evidence only. No live panel API, installed/deployed-version acceptance, credential permission verification, Telegram, payment gateway, production DB, staging mutation, deployment, or GitHub write. Current branch remains `feature/discount-codes`, HEAD `dc29a86`, divergence `0 0`.
- Remaining: verify update/delete failure semantics and capability-report invalid-configuration cases; test provider timeout-after-write only against an explicitly authorized disposable staging panel. Do not treat mock 404 semantics as proof of each deployed panel's behavior.


## Session 130 addendum — sanitized PasarGuard update/delete failures (2026-10-21)

- PasarGuard `update_user` and `delete_user` now suppress the original SDK exception chain when converting provider failures to safe `PanelError` messages. A direct fake-client regression test asserts secret-bearing provider text is not retained in the public exception cause/context.
- Focused three-adapter suite: **52 passed in 0.197s**. Latest full isolated suite: **433 passed in 100.058s (OK, exit 0)**. Static checks passed: compileall, Bash syntax, `node --check customer_webapp/app.js`, and `git diff --check`.
- E2 local mocks only; no live panel acceptance. Next: capability-report invalid/missing-connection tests and Sanaei legacy update/delete error matrix. Keep staging/live checks blocked absent explicit authorization.


## Session 131 addendum — invalid connection capability reports (2026-10-21)

- Added adapter tests for invalid/insecure connection settings. PasarGuard, Rebecca and Sanaei reports must fail validation without resolving secrets or constructing clients; they never imply live permission, and operation status remains unknown.
- Focused adapter suite: 55 passed in 0.183s. Full isolated suite: 436 passed in 102.146s (OK). Static checks passed including `node --check customer_webapp/app.js`.
- E2 only; no live acceptance. Next focus: Sanaei legacy-mode update/delete error semantics and ambiguous mutation outcomes.


## Session 132 addendum — Sanaei legacy mutation error handling (2026-10-21)

- Added tests for legacy-mode update/delete HTTP 503 and transport errors after the mutation endpoint is reached, plus preflight not-found behavior. Provider response/transport text remains redacted; ambiguous outcomes are generic errors and missing users are detected before mutation.
- Focused Sanaei suite: 29 passed in 0.131s. Full isolated suite: 439 passed in 103.797s (OK). Static checks passed.
- E2 only. Next: PasarGuard/Rebecca mutation response validation and ambiguous post-write error tests; no live acceptance claimed.


## Session 133 addendum — Rebecca response validation (2026-10-21)

- Rebecca user mapping now rejects malformed/unrepresentable expiry values, invalid or negative usage counters, and invalid or negative data limits with sanitized `PanelError` messages and suppressed exception chains.
- Focused Rebecca suite: 11 passed in 0.037s. Full isolated suite: 440 passed in 108.875s (OK). Static checks passed.
- E2 mock evidence only. Next: mutation response validation for PasarGuard/Rebecca; no live acceptance claimed.


## Session 134 addendum — mutation response validation (2026-10-21)

- PasarGuard: preserved safe mapping errors in create path; malformed expiry/counters and negative counters now fail closed. Added malformed create/update response tests.
- Rebecca: explicit JSON failure indicators on HTTP 2xx now produce a redacted `PanelError`; malformed user-list entries fail the entire response rather than being silently dropped.
- Focused adapter tests: 34 passed in 0.124s. Full suite: 444 passed in 104.128s (OK); static checks passed.
- E2 mock evidence only; live provider behavior still unverified. Next: confirm Rebecca schema and test ambiguous post-write timeout behavior for PasarGuard mutations.


## Session 135 addendum — ambiguous PasarGuard mutations (2026-10-21)

- Added timeout-after-mutation tests for PasarGuard create/update/delete. All are sanitized; each endpoint is invoked once with no automatic retry.
- Focused adapters: 35 passed in 0.135s. Full isolated suite: 445 passed in 98.043s (OK). Static checks passed after final change.
- E2 only; live provider semantics remain unverified.


## Session 136 — Provider contract inspection and reconciliation regression run (2026-10-21)
- Reconnected to the confirmed device label `8017638839-640919` (current device ID `ad943f1e-f97b-4540-8a59-68d2fe046556`) and verified repo `/root/fork/AnubisBot`, branch `feature/discount-codes`, HEAD `dc29a86`; existing dirty/untracked work remains intact.
- Reviewed `CustomerPurchaseService` provisioning flow: it persists provisioning intent before the provider create, avoids provider retries on idempotent replay, flags ambiguous post-mutation outcomes for reconciliation, and leaves funds reserved until operator reconciliation. Existing service-change tests cover timeout-before/after-update, read-only comparison, identity mismatch, explicit finalization and replay without a second provider mutation.
- Reviewed PasarGuard adapter deletion contract: adapter calls `remove_user_by_id(user_id=..., token=...)`, ignores the SDK return payload and reports a sanitized `PanelError` on exceptions. However, the `pasarguard` package is not installed in the device's default `/usr/bin/python3` interpreter; no project virtualenv or `uv` executable was found in the repository inspection. Therefore SDK method signature/return semantics cannot be independently verified from installed SDK source in this environment. Do not claim SDK-contract acceptance; repeat using the exact pinned/installed dependency environment when available.
- Reviewed Rebecca `_request`: HTTPS/TLS verification, no redirects, HTTP status checks, JSON parsing, explicit top-level `success:false` / `ok:false` / truthy `error` rejection, sanitized transport errors, and malformed list-item rejection are present. The authoritative provider response schema was not available locally, so undocumented response variants remain unverified.
- Started a fresh `./scripts/run_isolated_tests.sh` run on the connected device after inspection. Its final result must be recorded only after process completion; do not infer PASS from partial output.
- No provider API/network call, staging, production DB, Telegram, payment gateway, commit, push, deployment, reset, or cleanup was performed. All existing modified/untracked files preserved.
- Next: capture the full isolated test result and rerun static checks; obtain the correct project dependency environment or authoritative local SDK source and Rebecca API schema before making stronger contract claims. Keep all live acceptance disabled until explicitly authorized.

- Completion result for the isolated suite: **445 tests passed in 115.900s; exit code 0** (`./scripts/run_isolated_tests.sh`). Static checks also passed: `python3 -m compileall -q -f src tests scripts`, `bash -n scripts/*.sh`, `node --check customer_webapp/app.js`, and `git diff --check` (`STATIC_CHECKS_OK`).
- Final source check: branch `feature/discount-codes`, HEAD `dc29a86`, ahead/behind `0/0` versus `origin/feature/discount-codes`. No commit or push. Worktree remains dirty with the pre-existing modified/untracked source files and updated handoff docs; nothing was reset or cleaned.


## Session 137 addendum — Sanaei legacy multi-inbound identity safety (2026-10-21)

- Fixed a real legacy-mode representation hazard: `list_users` and `_find_client` previously retained only the first inbound when one email appeared in multiple inbounds. They now preserve all inbound IDs for a consistent identity, accept positive numeric-string inbound IDs, and fail closed if the same email maps to conflicting protocol/client identifiers.
- Update/delete refuse multi-inbound identities before mutation because atomic multi-inbound writes are not implemented; this avoids partially changing or deleting only one of several live copies.
- Added three regression tests. Test-first run failed before the fix; focused Sanaei suite then passed **32 tests**, and the full isolated suite passed **448 tests in 103.962s (OK, exit 0)**. Compileall, shell syntax, Mini App JS syntax and `git diff --check` passed.
- Evidence E2 isolated mocks only; no live provider acceptance. Remaining: additional legacy suspend/activate and multi-inbound usage/read edge coverage; live subscription-server and panel-version acceptance remain separate gates.
