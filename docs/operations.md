# Operations

## Transient-record cleanup

Run `python -m gateway.maintenance` to preview up to 1,000 expired OIDC states, expired browser sessions, and rate buckets older than one day per table. `--apply` performs the cleanup and writes a metadata audit event; `--batch-size` accepts 1–10,000. Run repeatedly until zero records are eligible. The job uses the same database policy lock as live traffic; keep batches modest and schedule during low traffic. Run from a short-lived task with the same configured database and secrets as the gateway, never from every app replica.

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

Each run deletes **only** expired OIDC states, expired browser sessions, and
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
