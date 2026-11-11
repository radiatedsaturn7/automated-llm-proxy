# Thunderbolt

**Brrrrt it into production**

![Thunderbolt A-10 shark-mouth artwork](gateway/static/thunderbolt-banner.png)

Apache-2.0 LLM gateway with a stateless application tier and external SQL policy/accounting state. An initial implementation for evaluation and hardening, **not a claim of production readiness or compliance certification**.

## Working features

- OpenAI-compatible text chat, SSE streaming, embeddings, and model discovery; native Anthropic text chat adapter.
- Explicit provider/model configuration, model allowlists, priority routing, encrypted provider credentials.
- Users, teams, monthly USD budgets, concurrent request reservations, per-key minute request/token admission limits.
- Hashed API keys, expiration, immediate revocation, explicit rotation, short-lived local-relay access tokens.
- OIDC authorization-code SSO with PKCE, signature/issuer/audience/nonce checks, group-to-team/role mapping, server-side sessions and CSRF protection.
- Admin web UI/API, metadata-only audit records, usage records, protected Prometheus metrics, health/readiness endpoints.
- Deterministic prompt-output evaluation without paid model calls.
- Docker Compose, Helm (including HA settings), Terraform Helm deployment, CI checks.

**State lives outside replicas**, not nowhere: users, keys, budgets, sessions and audit records require durable shared state. PostgreSQL is required in production; SQLite is single-process development only. Provider request bodies and responses are not saved by the gateway.

## Try locally

Python 3.12+, [uv](https://docs.astral.sh/uv/), and no provider account are needed to run the UI and tests.

```sh
uv sync --frozen --extra dev
export GATEWAY_MASTER_KEY="$(uv run python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
export GATEWAY_SIGNING_KEY="$(uv run python -c 'import secrets; print(secrets.token_urlsafe(48))')"
export BOOTSTRAP_TOKEN="$(uv run python -c 'import secrets; print(secrets.token_urlsafe(48))')"
export DATABASE_URL='sqlite+aiosqlite:///./gateway.db'
export PUBLIC_URL='http://localhost:8080'
uv run python -m gateway.migrate
uv run python -m gateway.cli --host 127.0.0.1 --port 8080
```

Open http://localhost:8080/admin. Use the bootstrap token once to create the first administrator and receive a seven-day admin key. Store that key securely; do not use the bootstrap secret as an API credential. Keep encryption/signing keys stable across restarts and replicas. Create a team/user, configure a provider with a model alias and per-million token USD prices, then create a restricted user key. Provider calls incur your provider's charges.

API documentation: `/docs`. Inference uses `Authorization: Bearer <user-api-key>` and `/v1/chat/completions`, `/v1/embeddings`, `/v1/models`. Never distribute provider keys to end users. A provider base URL includes its API prefix, e.g. `https://api.openai.com/v1` or `https://api.anthropic.com/v1`.

```sh
uv run pytest -q
uv run ruff check .
```

## Deployment and guides

- [Deployment, Compose, Helm, Terraform](deploy/README.md)
- [Versioned migrations and legacy adoption](docs/migrations.md)
- [Prompt registry and evaluation reports](docs/prompts.md)
- [Member self-service](docs/self-service.md)
- [Observability and monitoring](docs/observability.md)
- [Provider compatibility](docs/providers.md)
- [SCIM provisioning subset](docs/provisioning.md)
- [Operational cleanup and recovery](docs/operations.md)
- [Architecture and accounting semantics](docs/architecture.md)
- [Security model and production checklist](docs/security.md)
- [Feature matrix, research and roadmap](docs/feature-matrix.md)
- [Local relay and automatic access-token renewal](docs/local-relay.md)
- [Deterministic evaluations](docs/evaluation.md)

## Important boundaries

Budgets are admission controls using conservative reservations and provider-reported usage, **not a contractual hard billing cap**. Configure accurate prices and upstream account limits. Interrupted/unknown outcomes remain conservatively charged; reconcile only after checking provider billing. Token admission is estimated. Inference uses shared coordination and deterministic team/user/key row locks; unrelated teams can proceed concurrently. Same-team budget admission still serializes and must be benchmarked before scaling. No automatic retries after dispatch, to avoid duplicate billing. Priority routing is not health-aware failover.

OIDC groups are refreshed on login, not continuously. Sessions last up to eight hours; disable a user for immediate gateway revocation. No built-in password authentication, SAML, full SCIM interoperability certification, device authorization, credential-vault rotation, arbitrary plugin execution, multimodal adapter, distributed cache, semantic routing, or LLM-as-judge evaluation is claimed. The relay renews gateway access tokens; its bootstrap agent credential still requires secure provisioning and eventual renewal.

Schema changes use serialized versioned migrations. Databases created by the initial release require an explicit, validated `--adopt-v1` step after backup; see the migration guide. HA app replicas do not create an HA database. External TLS, database HA/backups, secret management, egress controls, billing/audit archival, load/chaos tests, and an independent security review remain operator requirements. Dependency versions are recorded in `uv.lock`; container runtime packages use hashed `requirements.lock`.

## License

Apache License 2.0. No paid enterprise feature tier. Contributions welcome; do not commit secrets or customer prompts. Report security defects privately to the repository maintainer rather than including exploit details or secrets in public issues.
