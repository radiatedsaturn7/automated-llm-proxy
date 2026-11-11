# Scope, enterprise requirements and roadmap

This is a working foundation under active development, **not** a fully enterprise-ready product. Source availability does not eliminate model-provider, database or hosting charges. “Included” describes implementation scope, not independent assurance, an SLA or a performance benchmark. Run the repository tests and review the API before adoption.

| Capability | Implemented scope | Important boundary / next work |
|---|---|---|
| Stateless replicas | External PostgreSQL-backed identity, policy, sessions and accounting | Local SQLite is development-only; external state is essential |
| Users and teams | Administrator/member roles; user and team budgets | One team per user; no delegated team-admin or arbitrary RBAC |
| Virtual credentials | Explicit inference/admin/account/provisioning permissions, expiry, revocation, rotation, model scopes and rate allowances | Permissions cannot elevate user role; rotation preserves authorization and expiry |
| Provider abstraction | OpenAI-compatible, Azure OpenAI v1 and Anthropic text adapters | Not full vendor API parity; validate supported request types |
| Spending control | Shared reservations and UTC monthly accounting | Prices operator-maintained; crash reconciliation required; not invoice reconciliation |
| SSO | OIDC with explicit group mapping and browser sessions | SCIM Users/Groups provisioning subset and explicit offboarding; no SAML, local passwords or IdP certification |
| Admin UI and API | Create resources, change budgets/enablement, inspect usage/audit, rotate keys | Member portal manages own existing credentials; no arbitrary delegated team-admin roles |
| Local relay | Authenticated loopback forwarding and short-lived access-token renewal; opt-in durable agent-secret rollover | Requires protected client journal for rollover; original expiry remains; no provider-key rotation or device flow |
| Evaluation | Immutable plaintext prompt registry, saved deterministic reports, paired baseline/candidate regression gates | Explicitly saved templates are stored; no paid automatic calls, LLM judges, safety certification or deployment gate enforcement |
| Deployment | Container and deployment examples for simple and replicated setups | Infrastructure must be supplied/validated; examples are not certification |
| Audit | Metadata records and actor-bound signed export cursors; bounded browser JSON downloads | No immutable storage or automatic SIEM delivery; exports are fixed windows, not database snapshots |
| Monitoring | Health/readiness and metrics integration in application/deployment | Correlation IDs, bounded route metrics, structured metadata logs, example dashboards/alerts; SLO ownership remains operator work |
| Advanced reliability | DB-shared per-model circuits, next-request routing, leased half-open probes and stale-completion fencing | No same-request replay, automatic model equivalence, exactly-once inference or failover SLA |
| Extensibility | Explicit source-level provider adapters | No untrusted plugin loader or isolation sandbox |
| Governance | Model scopes and privileged provider configuration | No DLP, moderation suite, compliance attestation or data residency enforcement |

## Verification status

The fifth increment merged as `3d9a14a`. CI runs `43064080373` and `43064057313` on its head `9f3bdc4` passed Python/lint/dependency audit, PostgreSQL tests, deployment validation and the container browser suite. Browser logs confirm the real admin/prompt flow with a TLS mock provider and two mocked-session portal security cases. No paid provider inference was needed for those tests. They are correctness checks, not vendor interoperability certification or production capacity benchmarks.

The sixth increment implements shared circuit breakers (schema revision 5), journaled agent-secret rollover (revision 6), bounded export UI and paired offline evaluation comparisons. At the local acceptance checkpoint, **178 tests passed and five PostgreSQL-specific tests were skipped**; Ruff and Helm validation also passed. This is not a claim that the sixth-increment PostgreSQL or browser tests have run. Branch CI must pass before merge; inspect the relevant commit's checks for current evidence. The export browser tests exercise pagination, limits, authentication failure, cancellation and stale-generation handling. The PostgreSQL circuit tests include concurrent half-open probe election; their presence in source alone is not execution evidence.

## Research informing priorities

Reviewed official sources on 2026-11-11. These are capability references, not claims of feature parity, endorsements, copied implementations or promises that every competitor feature is free.

- [LiteLLM virtual keys](https://docs.litellm.ai/docs/proxy/virtual_keys): credential-based model access and spend controls. This motivates scoped keys backed by shared persistence rather than per-replica counters.
- [LiteLLM enterprise capabilities](https://docs.litellm.ai/docs/enterprise) and [SSO permissions](https://docs.litellm.ai/docs/proxy/access_management/sso_permissions): centralized identity, administrative boundaries and group mappings are separate from merely supporting bearer tokens.
- [Portkey's gateway repository](https://github.com/Portkey-AI/gateway): provider abstraction, routing, guardrails and reliability are distinct concerns. Reliable failover must account for duplicate billing and partial streams, not simply retry every failure.
- [Envoy AI Gateway observability documentation](https://aigateway.envoyproxy.io/docs/0.4/capabilities/observability/) (now redirects to Agent Router): token/latency metrics, tracing and metadata logging inform operational visibility requirements. This project does not claim equivalent distributed tracing.
- [Envoy Gateway threat model](https://gateway.envoyproxy.io/docs/tasks/security/threat-model/): security also depends on surrounding deployment and infrastructure, not just API authentication.

## Recommended next milestones

1. **Assurance before breadth:** PostgreSQL concurrency/load tests, provider contract tests, OIDC interoperability, security assessment, restore drills, schema upgrade/rollback procedures and operational alerting.
2. **Identity lifecycle:** Expand the tested SCIM subset and real IdP compatibility; add delegated administration, multiple memberships, group-change reconciliation and IdP logout integration. Explicit session revocation already exists.
3. **Resilience and scale:** Measure pool/lock capacity, exercise circuit breakers under load/chaos, define end-to-end idempotency policy and test regional recovery. Health-aware next-request selection is not safe replay of an ambiguous request.
4. **Governance:** configurable retention and immutable audit export, data residency, secrets-manager integration, approved guardrail hooks, DLP/redaction and secure plugin boundaries.
5. **Developer workflows:** Build curated datasets, approval workflows, cost/latency comparisons and an SDK compatibility matrix on the existing prompt versions and offline reports. Paired deterministic gates do not measure general model quality.
6. **Optional optimization:** tenant-isolated caching with privacy policy, batching, weighted routing and multimodal/vendor-specific endpoints. Cache semantics and data isolation must precede cache hit-rate claims.

See [architecture](architecture.md), [security](security.md), [evaluation](evaluation.md) and [relay](local-relay.md) for operational details and exclusions.

### Identity administration increment

Explicit administrator-verified OIDC identity linking and per-user/all-browser-session revocation are implemented and audited. Identity linking never guesses from matching email and does not replace existing links. Identity linking/session revocation are distinct from the separate SCIM subset, IdP-session revocation and automatic group-change reconciliation. Provider model-map updates now use the same size and alias validation as provider creation.
