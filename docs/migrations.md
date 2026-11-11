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

## Revision 2: SCIM external identifiers

Revision 2 adds nullable `scim_external_id VARCHAR(255)` columns and unique indexes
to `users` and `teams`. Existing rows retain their IDs, memberships, credentials,
and data; their external identifier initially remains null. Multiple null IDs are
allowed. Non-null IDs are unique within Users and within Groups independently.

Run the standard migration command for a versioned v1 installation. An original
unversioned v1 installation still requires `--adopt-v1`: its v1 structure is
validated before adoption, then revision 2 runs. Both operations share one database
transaction. A pre-existing unversioned v2/custom schema is intentionally not
implicitly adopted. Index creation is transactional but not concurrent: schedule
an appropriate maintenance window on populated installations. The test suite
checks v1 data preservation, null handling, uniqueness and drift refusal before
any v2 alteration.

## Revision 3: explicit prompt registry

Revision 3 creates prompt-template, immutable prompt-version and offline evaluation
report tables. It leaves v1/v2 policy, accounting and SCIM data untouched. Only
administrators explicitly submitting templates store prompt content; inference
traffic is not retained. See [prompts.md](prompts.md) for plaintext-storage and
retention limitations. Existing versioned v2 schemas are validated before upgrade.

## Revision 4: explicit credential permissions

Revision 4 adds non-null JSON `api_keys.permissions` with an inference-only database
default. Its explicit legacy backfill preserves existing authority by owner role
and credential kind; key secrets, revocation, expiry, budgets and model policies are
unchanged. See [credential-permissions.md](credential-permissions.md) before rollout
and review broad legacy administrator credentials after upgrading.

## Revision 5: shared provider health

Revision 5 creates `provider_health`, keyed by provider and model alias, for shared
circuit state, failure counts, epoch fencing and expiring half-open probe leases.
Existing providers, credentials and accounting records are unchanged; new routes
start closed on their first admission. See [provider-health.md](provider-health.md)
for outcome classification, operator resets and bounded-lease limitations.

## Revision 6: durable agent root rotation

Revision 6 adds an integer generation counter (existing credentials start at zero),
last committed operation ID, and one pending rotation row per logical agent key.
Only credential digests are stored server-side. Prior access JWTs without a generation
claim are treated as generation zero until expiry or the first root rotation fences
them. Existing keys retain IDs, secrets, permissions and expiry. The opt-in relay
journal requires local durable secret storage; see [local-relay.md](local-relay.md).

## Revision 7: accounting and lifecycle indexes

Revision 7 replaces redundant prefix indexes with composite budget/export indexes
and adds targeted credential/session lifecycle access paths. It does not change
records or accounting. Creation is transactional, **not concurrent/online**; plan
a maintenance window and disk capacity for large tables. See
[database-indexes.md](database-indexes.md) for rationale, remaining query limits,
and PostgreSQL plan verification.

## Revision 8: explicit secondary memberships

Revision 8 creates `team_memberships` with a composite user/team identity and
member/admin role. Existing primary memberships, user roles and credentials remain
unchanged; no delegated administrator authority is backfilled. Keys may select an
active explicit secondary membership after upgrade. SCIM/OIDC continue to manage
the primary relationship. See [team-delegation.md](team-delegation.md) before rollout,
particularly removal/revocation and independent secondary reconciliation rules.
