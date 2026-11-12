# Operations

## Transient-record cleanup

Run `python -m gateway.maintenance` to preview up to 1,000 expired OIDC states, expired OIDC logout/authorization tombstones, expired browser sessions, and rate buckets older than one day per table. `--apply` performs the cleanup and writes a metadata audit event; `--batch-size` accepts 1–10,000. Run repeatedly until zero records are eligible. The job uses the same database policy lock as live traffic; keep batches modest and schedule during low traffic. Run from a short-lived task with the same configured database and secrets as the gateway, never from every app replica.

This command **never deletes keys, audit history, usage, or reservations**. In particular, unresolved reservations are not released by time: an interrupted upstream call may still have incurred charges. Release them through the explicitly acknowledged administrator reconciliation endpoint only after provider billing verification. Archival/export of audit and billing data, retention obligations and database backup policy remain deployment responsibilities.

## Recovery

Store database backups together with the matching encryption/signing key versions in protected secret management. Losing the master key loses access to encrypted upstream credentials; changing signing keys invalidates key digests and sessions, not just access tokens. Rotation of those root secrets requires a planned migration, not an environment-variable flip. Practice restores before production rollout.

## Read-only deployment preflight

Run `python -m gateway.check` with the intended gateway configuration before
starting replicas. It exits 0 on success and 1 on failure, emitting bounded JSON
without secrets. It verifies configuration, exact current schema and decryptability
of stored provider credentials. PostgreSQL runs the inspection in a read-only
transaction. It does not migrate, bootstrap, send model requests, verify a live
provider credential, or test SSO. A 30-second total deadline fails closed. Run only
with authorized access to database and deployment secrets; this is an operator
command, not a public diagnostics endpoint. Schema drift and failed configuration
are reported generically to avoid printing credentials from driver exceptions.

## Optional Kubernetes cleanup schedule

The Helm chart leaves `maintenance.enabled: false` by default. On Kubernetes
1.27 or newer, opt in with `--set maintenance.enabled=true`. The CronJob runs
at minute 17 of each hour in UTC, using the same image, configured environment,
and pre-existing Secret as the gateway. It does not run migrations or bootstrap
an administrator. Finish the schema migration successfully before enabling it;
no additional secret or cluster API permission is needed. The service account
token is not mounted. Treat access to the workload as access to its database
credentials, just as for the gateway itself.

Each run deletes **only** expired OIDC states, expired OIDC logout/authorization tombstones, expired browser sessions, and
rate buckets older than one day, up to `maintenance.batchSize` (default 1,000,
maximum 10,000) **per table**. It does not delete usage, audit history, keys,
reservations, prompt data, or provider circuit state. Each applied run appends
an audit event. This is not an audit retention policy or unbounded catch-up job.

`concurrencyPolicy: Forbid` prevents overlapping runs of this CronJob. Default
limits are a 300-second start deadline, 300-second active deadline, one retry,
one successful and three failed jobs retained, and a one-day finished-job TTL.
Use `maintenance.suspend: true` to pause future scheduling; this does not cancel
an already running job. Review failed-job logs and database contention. Multiple
Helm releases pointing at one database still require operator coordination;
Forbid is not a cross-release lock. Use small batches under load, and verify
that configured NetworkPolicy egress permits the actual database destination.
The pod has maintenance component labels, so it is never selected by the
inference Service or PodDisruptionBudget.

## Readiness and upgrades

`/health/ready` checks connectivity, the policy-lock row and the exact migration
ledger expected by this binary. An old replica against a newer schema, or a
partially migrated installation, returns 503 while `/health/live` stays live.
This is a cheap routing probe, not a full schema-drift inspection; run the
read-only preflight above before admitting a release. Plan schema upgrades with
a maintenance window unless the specific migration documents rolling-version
compatibility. No migration runs automatically on application startup.

Configuration fails closed on unknown `ENVIRONMENT` values: only `development`
and `production` are accepted. Do not use aliases such as `prod` or `staging`;
use production mode for staging installations needing production safeguards.
`PUBLIC_URL` must have an HTTP(S) hostname and cannot contain credentials, query
parameters or fragments. Production still requires HTTPS and PostgreSQL.

## Bounded two-replica acceptance drill

CI runs `tests/ops/replica_drill.py` against a fresh, isolated Compose project after
browser acceptance. It starts two actual gateway processes sharing one PostgreSQL
service and a test-only TLS upstream. The fixture is bounded: 40 simultaneous
cross-user/team-budget requests, 32 cross-replica requests sharing a six-RPM key,
one deliberately interrupted request, and eight survivor/recovery requests.
Expected 429 rejections are reported separately from successes. The harness verifies
held reservations cannot exceed the shared team budget, one shared rate window
admits exactly six requests, settled usage is counted once, and no duplicate
upstream dispatch occurs.

A worker is killed with SIGKILL only after the simulated upstream records its
request. Its pending reservation must remain charged, a retry against the exhausted
budget must not dispatch, and an independent key must still work on the surviving
worker. The killed container is restarted and both workers are exercised again.
This proves explicit client selection of the surviving replica, not automatic
load-balancer failover. It does not refund or replay uncertain generations.

The emitted JSON includes per-phase status counts and nearest-rank p50/p95/p99
latencies, successful-only latencies, accounting invariants and point-in-time Docker
CPU/memory/PID snapshots. These snapshots are not peak resource measurements.
Latency includes the deliberately held upstream in the budget phase. Results are
functional acceptance evidence on disposable CI hardware, not enterprise capacity,
availability, latency or recovery-time guarantees. Database failover, network
partitions and multi-region operation are not tested by this drill.

Operators may run the same bounded test on a disposable local machine with Docker
Compose 2.24.4+ (for `!override`) and Python 3.12. Never point it at a production
installation. Prepare disposable `.env` secrets as in the Compose quickstart,
build `automated-llm-proxy:local`, generate the test TLS fixture with
`sh tests/browser/mock-provider/generate-tls.sh`, and then run:

```sh
export COMPOSE_FILE=compose.yaml:tests/browser/compose.mock.yaml:tests/ops/compose.replicas.yaml
export COMPOSE_PROJECT_NAME=thunderbolt-replica-drill
# Environment must contain the same disposable BOOTSTRAP_TOKEN used by Compose.
# Do not print or paste that token into logs.
trap 'docker compose down --volumes --remove-orphans' EXIT
docker compose up --build -d --wait --wait-timeout 180 gateway gateway-peer
DISPOSABLE_REPLICA_DRILL=1 python tests/ops/replica_drill.py
```

The script refuses a different project name or Compose configuration and only sends
HTTP to fixed loopback ports 8180/8181. It requires a fresh database (bootstrap must
succeed), never accepts arbitrary remote target URLs, and emits no API credentials
or response contents. The test mock has unauthenticated hold/release controls within
the disposable network only; never use its override in a real deployment. CI removes
the test project, volumes and TLS secrets even on failure. Normal pytest only tests
harness safety/format/control contracts; actual two-container evidence comes from
the explicit Compose acceptance step.

### Offline encryption-key rotation acceptance

The bounded replica fixture additionally exercises the offline rotation CLI with
new synthetic keys after all existing load/accounting assertions complete. It
drains its client requests (the intentionally uncertain crash reservation remains
pending), verifies dry-run does not change admission, commits ciphertext and
primary-fingerprint transformation, and checks **both stale replicas** return
503 for readiness, a control-plane write and inference. Database counts and mock
request counters must remain unchanged for those denied operations.

One replica is then recreated with the target primary and decrypt-only fallback,
without rerunning migrations; it must decrypt the existing provider credential
and complete inference. An explicit reverse CLI rotation restores original-key
ciphertexts and authority, permitting the untouched old replica to serve again.
The switched replica is recreated back on the original configuration. The crash
reservation remains pending throughout. Reports include boolean/count evidence,
not keys. Rotation keys enter subprocess environments, never command arguments
or logs; fixture cleanup destroys the database and containers.

This is a disposable maintenance-window proof, not zero-downtime rotation,
cryptographic erasure, a backup restoration exercise or production recovery SLA.
Follow [encryption-key operations](encryption-key-operations.md) for draining,
version compatibility, backup/key escrow and operator prerequisites. The report's
original HA accounting totals precede the two additional rotation-probe requests.


## Request upload deadline

All HTTP requests must finish delivering their body within
`REQUEST_BODY_TIMEOUT_SECONDS` (default **30 seconds**, supported range 0.1–120).
This is an absolute receive deadline starting when the ASGI request enters the
body middleware, not a per-chunk idle timer. A slow trickle does not extend it.
Exceeding it returns HTTP 408 without dispatching authentication, database
writes or a provider request. `MAX_REQUEST_BYTES` separately bounds the complete
body (default 1 MiB); oversize chunks return 413 before being copied into the
application's body buffer. A disconnected client is not dispatched. HTTP/1.x rejections explicitly close
the connection rather than leaving an unfinished upload on keepalive; HTTP/2
responses do not emit a connection-specific header.

The deadline ends once the full body has been received; it does not shorten
inference/streaming timeouts or charge/retry a rejected request. Cancellation
from the server still propagates. Configure this non-secret value through your
deployment environment or Helm `extraEnv`, for example:

```yaml
extraEnv:
  - name: REQUEST_BODY_TIMEOUT_SECONDS
    value: "15"
```

Enforce matching header/body deadlines, maximum connections, request size and
per-client limits at the ingress/load balancer. The app cannot bound time spent
receiving headers before ASGI dispatch, memory buffered by the ASGI server, or
aggregate connections. This is not standalone denial-of-service protection.
Clients receiving a 408 should establish a fresh connection; do not automatically
replay ambiguous provider requests based on unrelated provider timeouts.

## Graceful process and pod shutdown

The supported `python -m gateway.cli` entry point sets Uvicorn's request-drain
window to `SHUTDOWN_GRACE_SECONDS` (default **360 seconds**, integer 1–3600).
`--shutdown-grace-seconds` explicitly overrides that environment value. Invalid
values fail before the server starts. Directly running another ASGI server or
`uvicorn gateway.main:app` bypasses this CLI setting: configure that server's
shutdown behavior yourself.

On SIGTERM, Uvicorn closes its listeners and idle connections, then lets existing
requests finish within the request-drain window. It cancels unfinished request
tasks when the window expires. This is not a guarantee that an upstream call was
never processed. Interrupted inference is not replayed automatically; existing
conservative accounting/uncertain reservations still apply. The lifespan then
waits for cancelled HTTP tasks to finish scheduling cleanup, then drains retained
accounting tasks before closing upstream/database clients. Do
not replace this ordering with immediate resource disposal or blanket refunds.

The Helm chart defaults to a **10-second preStop delay**, **360-second request
drain**, and **420-second total pod termination grace**. The pod remains serving
during preStop to allow terminating-endpoint changes to propagate; no public
shutdown endpoint is introduced. The delay is a configurable allowance, not a
proof that your ingress has stopped routing traffic. Kubernetes counts preStop
inside the total termination grace. Chart rendering rejects totals smaller than
`preStopDelaySeconds + shutdownGraceSeconds + 30`, and rejects a competing
`SHUTDOWN_GRACE_SECONDS` in `extraEnv`. Use the dedicated chart values together.
Set `preStopDelaySeconds: 0` only when your traffic-drain arrangement permits it.

Compose defaults the app's `stop_grace_period` to **420s** and forwards
`SHUTDOWN_GRACE_SECONDS` (default 360). If overriding
`GATEWAY_STOP_GRACE_PERIOD`, retain enough time for request drain and accounting;
Compose does not enforce the Helm relationship. External `docker stop -t`, job
supervisors, node eviction and service-manager deadlines can override these
allowances.

The 30-second chart minimum is a configuration safety margin, **not a bound on
database finalization**. Size the remaining window for your configured DB
pool/lock/statement deadlines and traffic. If DB access is broken or shutdown
exceeds the platform deadline, SIGKILL can leave conservative pending
reservations. Reconcile against provider billing; never automatically release
ambiguous charges. Raising provider/stream duration above the request-drain
window intentionally permits truncation during shutdown. Test your chosen
window, ingress propagation, long streams and database failures on the actual
deployment. Disposable signal tests do not prove Kubernetes zero-downtime,
managed-database failover, or a production SLA.
