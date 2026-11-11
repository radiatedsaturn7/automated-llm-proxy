# Versioned database migrations

Application replicas never create or change schema. Run `python -m gateway.migrate`
once as a deployment job before starting the matching application release. It is
idempotent, but schema migration still requires a tested backup and a maintenance
plan. Production requires PostgreSQL. SQLite is for local development only.

The packaged `gateway/migrations/v0001.py` is an immutable initial schema snapshot,
independent of the current ORM models. `gateway_schema_versions` records ordered
revision numbers and UTC application timestamps. A PostgreSQL transaction-level
advisory lock serializes migration jobs across replicas. SQLite uses `BEGIN
IMMEDIATE`. Schema changes and revision bookkeeping run in the same transaction.
The initial revision creates all required tables and the policy-serialization row.
The runner refuses unknown/newer, noncontiguous, partial, or structurally divergent
schema rather than claiming it is ready.

## Adopt an existing pre-versioning installation

1. Stop writers and take a verified, restorable database backup.
2. Review that the database is the original v1 installation, not a custom schema.
3. Run `python -m gateway.migrate --adopt-v1` using the matching release and database.
4. Check the successful revision message, restart application replicas and verify
   `/health/ready` and a restricted test request.

Adoption does not recreate tables, alter data or repair a partial schema. It checks
the table set, columns/types/lengths/nullability/defaults, primary keys, unique
constraints, foreign-key targets, indexes, and required policy-lock row. Only an
exact structural match is stamped. This is a schema check, not validation of all
existing business data. Unknown extra tables or columns also cause refusal. Use
manual reviewed migration for custom schemas; do not bypass the check by inserting
a version row. If migration is interrupted, inspect the failure and rerun; never
manually discard accounting records to force an upgrade.

## Future revisions and rollback

Add an ordered module to `gateway/migrations/`, with explicit `upgrade(connection)`
operations, register it in `REVISIONS`, and update head-schema validation and tests.
Never modify an already released revision or import live ORM metadata into one.
For rolling releases use expand/contract migrations: additive schema first,
compatible application rollout next, removals only after old replicas are gone.
Breaking changes require an explicitly scheduled maintenance window. The framework
does not automatically infer migrations or certify rolling-version compatibility.

Automatic downgrade is deliberately disabled: dropping accounting, policy or
credential tables is not a safe rollback. Restore a verified backup and its matching
application release under an operator-reviewed plan. Restoring an old backup can
resurrect revoked credentials or lose usage records: reconcile provider billing and
revocations before reopening traffic. The runner's `downgrade()` refuses without
mutating schema. Tests cover fresh/idempotent/concurrent upgrades, explicit legacy
adoption, drift/newer-version refusal and non-destructive downgrade refusal.
