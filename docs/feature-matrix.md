# Scope, enterprise requirements and roadmap

This is a working foundation under active development, **not** a fully enterprise-ready product. Source availability does not eliminate model-provider, database or hosting charges. “Included” describes implementation scope, not independent assurance, an SLA or a performance benchmark. Run the repository tests and review the API before adoption.

| Capability | Initial scope | Important boundary / next work |
|---|---|---|
| Stateless replicas | External PostgreSQL-backed identity, policy, sessions and accounting | Local SQLite is development-only; external state is essential |
| Users and teams | Administrator/member roles; user and team budgets | One team per user; no delegated team-admin or arbitrary RBAC |
| Virtual credentials | Expiry, revocation, rotation, model scopes, request/token allowances | Rotation immediately revokes old key; no grace window |
| Provider abstraction | OpenAI-compatible, Azure OpenAI v1 and Anthropic text adapters | Not full vendor API parity; validate supported request types |
| Spending control | Shared reservations and UTC monthly accounting | Prices operator-maintained; crash reconciliation required; not invoice reconciliation |
| SSO | OIDC with explicit group mapping and browser sessions | SCIM Users/Groups provisioning subset and explicit offboarding; no SAML, local passwords or IdP certification |
| Admin UI and API | Create resources, change budgets/enablement, inspect usage/audit, rotate keys | Member portal manages own existing credentials; no arbitrary delegated team-admin roles |
| Local relay | Authenticated loopback forwarding and short-lived access-token renewal | Does not rotate provider keys or its long-lived agent credential automatically |
| Evaluation | Bounded deterministic checks of supplied outputs | Immutable prompt registry and saved offline evaluation reports; no automatic model calls or LLM judges |
| Deployment | Container and deployment examples for simple and replicated setups | Infrastructure must be supplied/validated; examples are not certification |
| Audit | Administrative and usage metadata in database | No immutable audit storage, retention scheduler or SIEM delivery guarantee |
| Monitoring | Health/readiness and metrics integration in application/deployment | Correlation IDs, bounded route metrics, structured metadata logs, example dashboards/alerts; SLO ownership remains operator work |
| Advanced reliability | Bounded requests and explicit failure handling | No claim of automatic cross-provider semantic equivalence or exactly-once inference |
| Extensibility | Explicit source-level provider adapters | No untrusted plugin loader or isolation sandbox |
| Governance | Model scopes and privileged provider configuration | No DLP, moderation suite, compliance attestation or data residency enforcement |

## Research informing priorities

Reviewed official sources on 2026-11-11. These are capability references, not claims of feature parity, endorsements, copied implementations or promises that every competitor feature is free.

- [LiteLLM virtual keys](https://docs.litellm.ai/docs/proxy/virtual_keys): credential-based model access and spend controls. This motivates scoped keys backed by shared persistence rather than per-replica counters.
- [LiteLLM enterprise capabilities](https://docs.litellm.ai/docs/enterprise) and [SSO permissions](https://docs.litellm.ai/docs/proxy/access_management/sso_permissions): centralized identity, administrative boundaries and group mappings are separate from merely supporting bearer tokens.
- [Portkey's gateway repository](https://github.com/Portkey-AI/gateway): provider abstraction, routing, guardrails and reliability are distinct concerns. Reliable failover must account for duplicate billing and partial streams, not simply retry every failure.
- [Envoy AI Gateway observability documentation](https://aigateway.envoyproxy.io/docs/0.4/capabilities/observability/) (now redirects to Agent Router): token/latency metrics, tracing and metadata logging inform operational visibility requirements. This project does not claim equivalent distributed tracing.
- [Envoy Gateway threat model](https://gateway.envoyproxy.io/docs/tasks/security/threat-model/): security also depends on surrounding deployment and infrastructure, not just API authentication.

## Recommended next milestones

1. **Assurance before breadth:** PostgreSQL concurrency/load tests, provider contract tests, OIDC interoperability, security assessment, restore drills, schema upgrade/rollback procedures and operational alerting.
2. **Identity lifecycle:** SCIM or explicit provisioning automation, delegated administration, multiple memberships, service accounts, group-change reconciliation, session administration and IdP logout integration.
3. **Resilience and scale:** measured pool/lock capacity, provider circuit breakers and health-aware routing, explicit retry/idempotency policy, bounded streaming accounting and regional recovery tests.
4. **Governance:** configurable retention and immutable audit export, data residency, secrets-manager integration, approved guardrail hooks, DLP/redaction and secure plugin boundaries.
5. **Developer workflows:** prompt versioning, datasets, reproducible evaluation runs, model-quality comparison, approval gates, cost/latency regressions and an SDK compatibility matrix.
6. **Optional optimization:** tenant-isolated caching with privacy policy, batching, weighted routing and multimodal/vendor-specific endpoints. Cache semantics and data isolation must precede cache hit-rate claims.

See [architecture](architecture.md), [security](security.md), [evaluation](evaluation.md) and [relay](local-relay.md) for operational details and exclusions.

### Identity administration increment

Explicit administrator-verified OIDC identity linking and per-user/all-browser-session revocation are implemented and audited. Identity linking never guesses from matching email and does not replace existing links. These controls are not SCIM, IdP-session revocation or automatic group-change reconciliation. Provider model-map updates now use the same size and alias validation as provider creation.
