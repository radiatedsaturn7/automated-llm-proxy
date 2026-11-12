# Scope, enterprise requirements and roadmap

This is a working foundation under active development, **not** a fully enterprise-ready product. Source availability does not eliminate model-provider, database or hosting charges. “Included” describes implementation scope, not independent assurance, an SLA or a performance benchmark. Run the repository tests and review the API before adoption.

| Capability | Implemented scope | Important boundary / next work |
|---|---|---|
| Stateless replicas | External PostgreSQL-backed identity, policy, sessions and accounting | Local SQLite is development-only; external state is essential |
| Users and teams | Primary and explicit secondary memberships; scoped team administrators; shared user and selected-team budgets | Not arbitrary RBAC; SCIM/OIDC maintain primary membership only; delegated API keys cannot exceed caller limits |
| Virtual credentials | Explicit inference/admin/account/provisioning/team_admin permissions, expiry, revocation, rotation, model scopes and rate allowances | Permissions cannot elevate user role; rotation preserves authorization and expiry |
| Installation encryption | Bounded maintenance-only dry run and atomic provider/MFA reencryption with database generation fencing and explicit reverse procedure | Drain traffic and remove older binaries/direct writers; backups and replica restarts required; no automatic signing-key migration or zero-downtime claim |
| Provider abstraction | OpenAI-compatible, Azure OpenAI v1, Anthropic and native Gemini text adapters | Stateless non-streaming text Responses compatibility; not full vendor API parity; validate supported request types |
| Spending control | Shared reservations and UTC monthly accounting | Prices operator-maintained; crash reconciliation required; not invoice reconciliation |
| SSO | OIDC with explicit group mapping and browser sessions | SCIM Users/Groups provisioning subset and explicit offboarding; default-off local passwords and signed back-channel logout; local TOTP option; no SAML or IdP certification |
| Admin UI and API | Create resources, change budgets/enablement, inspect usage/audit, rotate keys | Member portal manages own existing credentials; delegated team UI supports scoped membership and inference-key administration, not arbitrary custom roles |
| Local relay | Authenticated loopback forwarding and short-lived access-token renewal; opt-in durable agent-secret rollover | Optional operator-bounded SSO enrollment uses a protected client journal; original expiry remains; no provider-key rotation or claim of RFC 8628 protocol compatibility |
| Evaluation | Immutable plaintext prompt registry, saved deterministic reports, paired regression gates and opt-in gateway model evaluation CLI | Explicitly saved templates are stored; runner execution can incur provider charges; no LLM judges or quality/safety certification |
| Deployment | Container and deployment examples for simple and replicated setups | Infrastructure must be supplied/validated; examples are not certification |
| Audit | Metadata records, actor-bound signed export cursors, bounded browser downloads and resumable metadata-only operator collection; optional authenticated offline snapshot seals | No immutable storage or automatic SIEM delivery; exports are fixed windows, not database snapshots |
| Monitoring | Health/readiness and metrics integration in application/deployment | Correlation IDs, bounded route metrics, structured metadata logs, example dashboards/alerts; SLO ownership remains operator work |
| Advanced reliability | DB-shared per-model circuits, next-request routing, leased half-open probes and stale-completion fencing | No same-request replay, automatic model equivalence, exactly-once inference or failover SLA |
| Extensibility | Explicit source-level provider adapters | No untrusted plugin loader or isolation sandbox |
| Governance | Model scopes, privileged provider configuration and opt-in inbound/output block/redact heuristics | Fixed reviewed detectors; output policy rejects streaming and unsupported content; no complete DLP coverage, moderation suite, compliance attestation or data residency enforcement |

## Verification status

The fifth increment merged as `3d9a14a`. CI runs `43064080373` and `43064057313` on its head `9f3bdc4` passed Python/lint/dependency audit, PostgreSQL tests, deployment validation and the container browser suite. Browser logs confirm the real admin/prompt flow with a TLS mock provider and two mocked-session portal security cases. No paid provider inference was needed for those tests. They are correctness checks, not vendor interoperability certification or production capacity benchmarks.

The sixth increment merged as `53cd23f`. CI run `43065418836` passed all four jobs at head `fd8c44f`: real PostgreSQL tests and eleven Chromium tests covering the admin/provider/prompt flow, portal security and bounded export interruption paths. The shared circuit breaker, journaled agent-secret rollover and offline regression gates are implemented. Tests use simulated providers, not paid external inference. These results do not establish a failover SLA or production throughput.

Later database/operations and evaluation-runner increments add composite query indexes, bounded pools/deadlines, optional cleanup scheduling, read-only preflight and explicit operator-run model comparisons. Consult each commit's CI checks for its acceptance result; the runner's tests never call paid services.

The ninth increment merged as `0ed13cb`. CI run `43069786191` at head `2f7b8fa` passed all four jobs, including real PostgreSQL, fifteen Chromium cases and twelve Helm tests. This adds explicit multi-team delegation, native Gemini text operations, and opt-in reviewed inbound DLP heuristics. It does not establish full provider API parity or comprehensive sensitive-data detection.

## Latest exact-head acceptance (PR #16)

[CI run 43125212538](https://github.com/radiatedsaturn7/automated-llm-proxy/actions/runs/43125212538)
passed all five jobs at exact head
`7acaa9848d9c9873678bd0a31750a5a05bb11589`:

- Python: **613 passed, 8 skipped**, with dependency audit reporting no known vulnerabilities.
- PostgreSQL: **621 passed**, plus successful disposable logical backup/restore,
  authorization, credential and accounting acceptance.
- Compose browser: **23 passed**, plus successful two-process shared-budget/rate-limit,
  SIGKILL uncertainty and survivor/restart acceptance.
- Actual disposable Keycloak: **2 passed**, using verified TLS and real PKCE,
  group mapping, CSRF enforcement, local browser logout and signed back-channel logout.
- Deployment/chart, Terraform, image and monitoring checks passed.

The [official SDK matrix](sdk-compatibility.md) records OpenAI Python 3.49.0
contracts through the real ASGI gateway with simulated upstream responses.
The Keycloak checks use an actual IdP rather than mocked authentication state.
No paid provider calls were used. This does not certify other IdPs, provider
services, all SDK features, production capacity or deployment security.

**Release status: PR #16 merged as
`7af1ab65c351cf658ff24226a4339dc4087b512c`.** PR #15 merged as
`e3b514392c2a798229af2c02889f89a71ad485ee`. Green checks do not imply release
approval, and later round-17 changes are not covered by this run.

## Research informing priorities

Reviewed official sources on 2026-11-11. These are capability references, not claims of feature parity, endorsements, copied implementations or promises that every competitor feature is free.

- [LiteLLM virtual keys](https://docs.litellm.ai/docs/proxy/virtual_keys): credential-based model access and spend controls. This motivates scoped keys backed by shared persistence rather than per-replica counters.
- [LiteLLM enterprise capabilities](https://docs.litellm.ai/docs/enterprise) and [SSO permissions](https://docs.litellm.ai/docs/proxy/access_management/sso_permissions): centralized identity, administrative boundaries and group mappings are separate from merely supporting bearer tokens.
- [Portkey's gateway repository](https://github.com/Portkey-AI/gateway): provider abstraction, routing, guardrails and reliability are distinct concerns. Reliable failover must account for duplicate billing and partial streams, not simply retry every failure.
- [Envoy AI Gateway observability documentation](https://aigateway.envoyproxy.io/docs/0.4/capabilities/observability/) (now redirects to Agent Router): token/latency metrics, tracing and metadata logging inform operational visibility requirements. This project does not claim equivalent distributed tracing.
- [Envoy Gateway threat model](https://gateway.envoyproxy.io/docs/tasks/security/threat-model/): security also depends on surrounding deployment and infrastructure, not just API authentication.

## Recommended next milestones

1. **Assurance before breadth:** PostgreSQL concurrency/load tests, provider contract tests, OIDC interoperability, security assessment, restore drills, schema upgrade/rollback procedures and operational alerting.
2. **Identity lifecycle:** Expand the tested SCIM subset and real IdP compatibility; extend group-change reconciliation and IdP logout integration beyond the implemented delegated administration and explicit multiple memberships. Explicit session revocation already exists.
3. **Resilience and scale:** Measure pool/lock capacity, exercise circuit breakers under load/chaos, define end-to-end idempotency policy and test regional recovery. Health-aware next-request selection is not safe replay of an ambiguous request.
4. **Governance:** configurable retention and immutable audit export, data residency, secrets-manager integration, approved guardrail hooks, DLP/redaction and secure plugin boundaries.
5. **Developer workflows:** Build curated datasets, approval workflows, cost/latency comparisons and broader SDK compatibility coverage beyond the pinned Python acceptance matrix, on the existing prompt versions and offline reports. Paired deterministic gates do not measure general model quality.
6. **Optional optimization:** tenant-isolated caching with privacy policy, batching, weighted routing and multimodal/vendor-specific endpoints. Cache semantics and data isolation must precede cache hit-rate claims.

See [architecture](architecture.md), [security](security.md), [evaluation](evaluation.md) and [relay](local-relay.md) for operational details and exclusions.

### Identity administration increment

Explicit administrator-verified OIDC identity linking and per-user/all-browser-session revocation are implemented and audited. Identity linking never guesses from matching email and does not replace existing links. Identity linking/session revocation are distinct from the separate SCIM subset, IdP-session revocation and automatic group-change reconciliation. Provider model-map updates now use the same size and alias validation as provider creation.
