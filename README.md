# Brrrtgate

**Brrrrt it into production**

![Brrrtgate A-10 shark-mouth artwork](gateway/static/brrrtgate-banner.png)

Apache-2.0 LLM gateway with a stateless application tier and external SQL policy/accounting state. An initial implementation for evaluation and hardening, **not a claim of production readiness or compliance certification**.

The project display name is now **Brrrtgate**. Existing installation identifiers remain compatible; see [branding and compatibility](docs/branding.md).

## Implemented capabilities

- OpenAI-compatible text chat, SSE streaming, embeddings and model discovery; Azure OpenAI v1, native Anthropic text-chat and Gemini text adapters; stateless text Responses compatibility.
- Explicit provider/model aliases and prices, encrypted provider credentials, model allowlists and priority routing.
- Users, primary and explicit secondary teams, scoped team administration, monthly USD budgets, concurrent reservations and per-key request/token admission limits.
- Hashed API credentials with explicit inference/admin/account/provisioning/team_admin permissions, expiration and revocation; rotation preserves scope and expiry.
- OIDC SSO with PKCE, verified identity tokens, nested group mappings, bounded metadata/JWKS caching, configurable sessions and CSRF protection; explicit identity linking and session revocation.
- A bounded SCIM Users/Groups subset, signed OIDC back-channel logout, administrator console and member self-service portal.
- Default-off administrator-provisioned local password fallback with shared throttles, optional/required TOTP and scoped session revocation.
- Privileged mutations revalidate authority under the shared writer lock; opt-in generated-output safeguards preserve provider billing and fail closed on unsupported content.
- Immutable prompt versions, literal variable previews and saved deterministic offline evaluations, without paid evaluation model calls.
- Metadata audit/usage records, signed-cursor exports, request correlation, protected Prometheus metrics and example dashboards/alerts.
- Docker Compose, Helm/HA settings, Terraform Helm deployment, and a [native Linux VM/systemd kit](docs/systemd-deployment.md), with explicit migrations and CI checks.

Also implemented: shared per-model provider circuit breakers, opt-in SSO-assisted relay enrollment and journaled agent-secret rollover, bounded browser export downloads and paired offline regression gates. See the [feature matrix](docs/feature-matrix.md) for verification evidence and limitations.

**State lives outside gateway replicas**, not nowhere: users, keys, budgets, sessions, audit and provider health need durable shared state. PostgreSQL is required in production; SQLite is single-process development only. Inference bodies/responses are not logged or retained by the gateway. Prompt templates are stored in plaintext only when an administrator explicitly saves them in the registry; protect that database and its backups.

## Try locally

Python 3.12+, [uv](https://docs.astral.sh/uv/), and no provider account are needed to run the UI and tests.

```sh
uv sync --locked --extra dev --extra sdk-test
export GATEWAY_MASTER_KEY="$(uv run --no-sync python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
export GATEWAY_SIGNING_KEY="$(uv run --no-sync python -c 'import secrets; print(secrets.token_urlsafe(48))')"
export BOOTSTRAP_TOKEN="$(uv run --no-sync python -c 'import secrets; print(secrets.token_urlsafe(48))')"
export DATABASE_URL='sqlite+aiosqlite:///./gateway.db'
export PUBLIC_URL='http://localhost:8080'
uv run --no-sync python -m gateway.migrate
uv run --no-sync python -m gateway.cli --host 127.0.0.1 --port 8080
```

Open http://localhost:8080/admin. Use the bootstrap token once to create the first administrator and receive a seven-day admin key. Store that key securely; do not use the bootstrap secret as an API credential. Keep encryption/signing keys stable across restarts and replicas. Create a team/user, configure a provider with a model alias and per-million token USD prices, then create a restricted user key. Provider calls incur your provider's charges.

API documentation: `/docs`. Inference uses `Authorization: Bearer <user-api-key>` and `/v1/chat/completions`, `/v1/embeddings`, `/v1/models`, and the documented text-only `/v1/responses` subset. Never distribute provider keys to end users. A provider base URL includes its API prefix, e.g. `https://api.openai.com/v1` or `https://api.anthropic.com/v1`.

```sh
uv run --no-sync pytest -q
uv run --no-sync ruff check .
```

## Deployment and guides

- [Deployment, Compose, Helm, Terraform](deploy/README.md)
- [Versioned migrations and legacy adoption](docs/migrations.md)
- [Prompt registry and evaluation reports](docs/prompts.md)
- [Member self-service](docs/self-service.md)
- [Optional daily request allowances and bounded usage resets](docs/usage-resets.md)
- [Credential permissions and least-privilege integrations](docs/credential-permissions.md)
- [Audit/usage export and signed pagination](docs/audit-export.md)
- [Resumable metadata-only audit collector](docs/audit-collector.md)
- [Provider health and shared circuit breakers](docs/provider-health.md)
- [Admission concurrency and lock semantics](docs/policy-concurrency.md)
- [Observability and monitoring](docs/observability.md)
- [Provider compatibility](docs/providers.md)
- [Official Python SDK acceptance](docs/sdk-compatibility.md)
- [Internal security and operational readiness](docs/security-readiness.md)
- [SCIM provisioning subset](docs/provisioning.md)
- [Optional local passwords and TOTP MFA](docs/local-passwords.md)
- [Mounted secret files](docs/secret-files.md)
- [Multi-team delegation and team administration](docs/team-delegation.md)
- [Opt-in inbound DLP and source extension boundaries](docs/guardrails.md)
- [Operational cleanup and recovery](docs/operations.md)
- [PostgreSQL backup/restore acceptance drill](docs/restore-drill.md)
- [SSO-assisted relay enrollment](docs/enrollment.md)
- [Stateless text Responses API](docs/responses.md)
- [Architecture and accounting semantics](docs/architecture.md)
- [Security model and production checklist](docs/security.md)
- [Feature matrix, research and roadmap](docs/feature-matrix.md)
- [Local relay and automatic access-token renewal](docs/local-relay.md)
- [Deterministic evaluations](docs/evaluation.md)
- [Opt-in model evaluation runner](docs/evaluation-runner.md)
- [Database access paths and migration planning](docs/database-indexes.md)

## Important boundaries

Budgets are admission controls using conservative reservations and provider-reported usage, **not a contractual hard billing cap**. Configure accurate prices and upstream account limits. Interrupted/unknown outcomes remain conservatively charged; reconcile only after checking provider billing. Token admission is estimated. Inference uses shared coordination and deterministic team/user/key row locks; unrelated teams can proceed concurrently. Same-team budget admission still serializes and must be benchmarked before scaling. No automatic retries after dispatch, to avoid duplicate billing. The shared circuit breaker skips unhealthy routes for later requests; it never replays an already-dispatched request. Equivalent models, prices and data-handling policy on fallback providers must be configured by an administrator.

OIDC groups are reconciled on verified login, not continuously. Denied mappings disable existing identities and revoke credentials; corrected access requires explicit administrator re-enablement. Accepted role/team changes revoke old sessions and keys. Session lifetime is configurable from five minutes to 24 hours (default eight hours); disable a user for immediate gateway access revocation. Already-admitted provider work is not canceled by revocation. Local TOTP is phishable; no phishing-resistant local MFA, SAML, full SCIM interoperability certification, RFC 8628 device authorization, credential-vault rotation, arbitrary plugin execution, multimodal adapter, distributed cache, semantic routing, or LLM-as-judge evaluation is claimed. The relay renews short-lived gateway tokens. Opt-in durable agent-secret rotation requires a protected local journal and remains subject to the original credential expiry; it does not rotate provider keys or eliminate initial provisioning. Environment-only relay operation retains manual agent-secret rollover. Gateway replicas stay stateless, but opting into the relay journal deliberately adds protected client-side state.

Schema changes use serialized versioned migrations. Databases created by the initial release require an explicit, validated `--adopt-v1` step after backup; see the migration guide. HA app replicas do not create an HA database. External TLS, database HA/backups, secret management, egress controls, billing/audit archival, load/chaos tests, and an independent security review remain operator requirements. Dependency versions are recorded in `uv.lock`; container runtime packages use hashed `requirements.lock`.

## License

Apache License 2.0. No paid enterprise feature tier. Contributions welcome; do not commit secrets or customer prompts. Report security defects privately to the repository maintainer rather than including exploit details or secrets in public issues.
