# Deployment and operations

The **application is stateless; the system is not state-free**. Teams, identities,
key hashes, encrypted provider credentials, budgets and audit events live in an
external database. Replicas share that database and the same cryptographic keys.
There are no sticky sessions or application data volumes. SQLite is for a single
local process only; use PostgreSQL for shared production state.

These files are a starting point for deployment, not proof of enterprise readiness.
They do not create cloud accounts, an HA database, TLS certificates, DNS, managed
secrets, an ingress controller or backup infrastructure. No infrastructure is
provisioned until an operator explicitly runs Docker/Helm/Terraform.

## Local installation

Prerequisites: Docker Engine with Compose v2. Create a private `.env` file (never
commit it), set restrictive permissions, and generate new values:

```sh
umask 077
python - <<'PY'
import base64, os, secrets
password = secrets.token_urlsafe(32)
with open('.env', 'x') as f:
    f.write(f'POSTGRES_PASSWORD={password}\n')
    f.write(f'DATABASE_URL=postgresql+asyncpg://gateway:{password}@postgres:5432/gateway\n')
    f.write('GATEWAY_MASTER_KEY=' + base64.urlsafe_b64encode(os.urandom(32)).decode() + '\n')
    f.write('GATEWAY_SIGNING_KEY=' + secrets.token_urlsafe(48) + '\n')
    f.write('BOOTSTRAP_TOKEN=' + secrets.token_urlsafe(48) + '\n')
PY
docker compose up --build -d
curl --fail http://127.0.0.1:8080/health/ready
```

Compose waits for PostgreSQL and then runs the migration before starting the
server. The gateway is bound only to host loopback. The admin interface is at
`http://127.0.0.1:8080/admin`. Consult the main README for initial administration.
Pass optional OIDC and provider settings through the gateway environment; never
put provider credentials in the image. `docker compose down` preserves database
contents. **Do not use `down -v` unless you intend to delete the database.**

Changing the PostgreSQL password in `.env` does not change an existing PostgreSQL
role password. Rotate it in PostgreSQL first using a controlled operation, then
update the application connection configuration. URI-encode special characters
in DATABASE_URL; the generated example uses URL-safe characters.

## Container build

```sh
docker build -t YOUR_REGISTRY/automated-llm-proxy:0.1.0 .
# Push only to a registry you control and intend to publish to.
docker push YOUR_REGISTRY/automated-llm-proxy:0.1.0
```

The image runs as UID/GID 10001. Kubernetes and Compose mount the application root
filesystem read-only with a small writable `/tmp`. No cloud/API keys are baked
into the image. Pin a verified image digest for production. Base image tags and
third-party actions should also be pinned and maintained by your release process.

## Kubernetes / Helm

Requires Kubernetes 1.26+, Helm 3, an existing PostgreSQL database, and an existing
Secret in the release namespace containing these keys:

- `DATABASE_URL`: PostgreSQL SQLAlchemy async URL.
- `GATEWAY_MASTER_KEY`: URL-safe base64 encoding of 32 random bytes, used to encrypt
  stored credentials. All replicas and migrations must use the same value.
- `GATEWAY_SIGNING_KEY`: at least 32 random characters, shared by every replica.
- `BOOTSTRAP_TOKEN`: at least 32 random characters, used for initial setup.
- `METRICS_TOKEN`: at least 32 random characters, used to protect metrics.

Use your approved secrets manager/operator to create that Secret. Do not place
secret values in `values.yaml`, Helm `--set`, shell history or Terraform state.
The chart only references an existing Secret and does not generate or copy it.
Helm defaults to `ENVIRONMENT=production`; set `publicUrl` to the actual HTTPS
address, matching your IdP redirect URI. Configure additional authentication settings with `extraEnv`; for secret values
use `valueFrom.secretKeyRef` rather than literal `value`.

```sh
helm lint deploy/helm/llm-gateway --set existingSecret=gateway-secrets
helm upgrade --install gateway deploy/helm/llm-gateway \
  --namespace llm-gateway \
  --set existingSecret=gateway-secrets \
  --set image.repository=YOUR_REGISTRY/automated-llm-proxy \
  --set image.tag=0.1.0 \
  --set publicUrl=https://YOUR_GATEWAY_HOST \
  --wait --timeout 10m
```

The namespace and Secret must exist first. Pre-install/pre-upgrade migration jobs
run before new server replicas. They use the same image/Secret and have no service
account token. Run only one release migration against a database at a time. A
successful migration is not automatically undone by a Helm rollback; use
expand/contract, backward-compatible schema changes and test rollback against a
restored backup. Helm `--atomic` rolls back workload resources, not database data.

For high availability, add `-f deploy/helm/llm-gateway/values-ha.yaml` and deploy
across multiple nodes/zones. The example runs at least three replicas with a
PodDisruptionBudget and CPU HPA. HPA requires metrics-server. Anti-affinity is a
preference, not a guarantee: enforce zone placement where your infrastructure
requires it. A PDB only controls voluntary disruptions, not node/zone failure.
Database HA, connection limits/pooling, provider outages and network failures
remain part of end-to-end availability. Load test connection concurrency, long
SSE streams, budget locking and failover before choosing production limits.

### Network and TLS

Ingress is disabled by default. Enable it only with a configured TLS certificate
and the intended host. Configure controller-specific timeout/buffering settings
for streamed responses; there is no universal annotation that works for every
controller. Do not expose database, metrics or administration endpoints broadly.
Restrict admin/metrics at your ingress or network boundary as well as application
access controls. The image does not trust forwarded headers by default; if your
installation needs them, explicitly trust only the known reverse-proxy addresses.

NetworkPolicy is opt-in because private provider APIs, databases, OIDC discovery
and IPv6 routes vary by organization. Its sample permits DNS, public IPv4 HTTPS,
and PostgreSQL pods labeled `app.kubernetes.io/name=postgresql` in the same
namespace. **Replace those egress peers with your actual database, provider and
identity endpoints before enabling it.** It is not an SSRF protection boundary or
an FQDN allowlist. Ingress defaults to any source on port 8080; narrow it to your
ingress/scraper peers. Your CNI must enforce NetworkPolicy. Pre-install migration
pods can run before the release's NetworkPolicy exists; enforce baseline policies
at namespace level if isolation during hooks is required.

## Terraform example

`deploy/terraform` wraps the local Helm chart using the Kubernetes credentials in
an existing kubeconfig. It does **not** create a cluster or managed PostgreSQL. It
accepts the name of an existing Secret rather than its contents; never add raw
credentials as Terraform variables. Use a protected remote Terraform backend in
team environments. Review the plan before apply.

```sh
terraform -chdir=deploy/terraform init
terraform -chdir=deploy/terraform plan \
  -var existing_secret=gateway-secrets \
  -var image_repository=YOUR_REGISTRY/automated-llm-proxy \
  -var public_url=https://YOUR_GATEWAY_HOST
```

Choose the namespace and kube context explicitly. Public ingress requires a TLS
Secret. Use a managed PostgreSQL service with zone failover, backups/PITR and a
connection pool appropriate to your replica count. Provider lockfiles should be
committed after your organization verifies the selected provider checksums.

## Operations checklist

- Readiness checks database reachability; liveness must not depend on provider or
  database uptime. A healthy process does not prove that any configured LLM works.
- Monitor request/error rates, provider latency, first-token latency, budget
  denials, rate limiting, database pool waits, migration failures and pod restarts.
- Set request/stream limits and ingress idle timeouts consistently. Long-running
  streams may be cut off during pod termination; clients must tolerate this.
- Back up the database and encryption key together, stored separately under
  restricted access. Run a restore drill; unrecoverable keys make stored provider
  credentials unreadable.
- Rotate provider/API credentials using supported application flows. Do not
  casually replace the master encryption key: existing ciphertext must be
  re-encrypted first. Changing the signing key invalidates issued tokens.
- After bootstrap, remove/disable bootstrap access according to the application
  instructions. Never use bootstrap credentials as client API keys.
- Audit records are ordinary database rows, not tamper-proof storage. Export to
  a restricted append-only SIEM/object store for stronger retention guarantees.
- Do not log prompt contents or provider secrets by default. Review applicable
  privacy, retention and data-residency requirements before enabling collection.
- Validate HA and recovery on your real IdP, provider APIs and database. The
  included unit/manifest checks are not load tests, penetration tests, compliance
  attestations or production certification.

## References

- Kubernetes Deployments: https://kubernetes.io/docs/concepts/workloads/controllers/deployment/
- Pod disruption budgets: https://kubernetes.io/docs/tasks/run-application/configure-pdb/
- Network policies: https://kubernetes.io/docs/concepts/services-networking/network-policies/
- Helm hooks: https://helm.sh/docs/topics/charts_hooks/
- PostgreSQL backup/recovery: https://www.postgresql.org/docs/current/backup.html
- Terraform Helm provider: https://registry.terraform.io/providers/hashicorp/helm/latest/docs

## Full-stack browser smoke test

The `compose-browser` CI job builds the real container, starts PostgreSQL, runs the
migration container, waits for readiness, and uses Chromium/Playwright against
the served admin UI. It bootstraps a disposable installation, logs in, creates a
team, user and scoped API key, rotates/revokes that key, checks member/admin access
separation, lists models, runs an offline evaluation, checks audit records, and
signs out. No provider is configured and no paid inference request is made. This
complements adapter/streaming mock tests; it does not test a real identity provider,
external model service, browser SSO, load, or Kubernetes failover.

To run locally, use a **fresh disposable database**, start Compose using the setup
above, and export the same `BOOTSTRAP_TOKEN` into the test process:

```sh
npm ci --prefix tests/browser
(cd tests/browser && npx playwright install --with-deps chromium)
BOOTSTRAP_TOKEN="your-disposable-bootstrap-token" npm test --prefix tests/browser
```

Optional `GATEWAY_TEST_URL` selects a different test server. `CHROMIUM_PATH` can
select an installed Chromium executable. Do not target a production or previously
bootstrapped instance: the test intentionally creates durable records and bootstrap
must succeed exactly once. Retries are disabled because repeating against a dirty
database would hide installation failures. CI tears down its volumes afterward;
do not copy that destructive cleanup step onto a real installation.

Browser traces, videos and screenshots are disabled to avoid preserving one-time
administrator credentials. Test secrets are generated per run, masked in CI, and
never committed. The application image's nonroot/read-only settings remain enabled
throughout this test. A passing smoke test is not a production-readiness guarantee.
