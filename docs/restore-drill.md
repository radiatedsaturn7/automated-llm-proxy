# Disposable PostgreSQL restore acceptance

CI's existing PostgreSQL job runs `tests/ops/restore_drill.py` after the regular
suite. This is an explicit operations script, not an automatically collected pytest
test and not a production backup utility. It requires both `CI=true` and
`DISPOSABLE_RESTORE_DRILL=1`; only the fixed loopback PostgreSQL test-service URL,
port 5432, database `gateway_test` and role `gateway` are accepted. Remote targets,
other databases/roles/ports and URL options are refused.

The drill creates two uniquely named temporary databases without modifying the
original test database. It migrates the source and creates synthetic users, team,
model-restricted API key, encrypted dummy provider credential, prompt version,
settled usage and a conservatively charged pending reservation. No live provider
or identity service is contacted. PostgreSQL 16's official Docker image supplies
both `pg_dump` and `pg_restore`, avoiding client/server major-version mismatches.
The custom-format dump is restored into the separate empty target, with errors
fatal and owner/ACL restoration disabled for this single-role fixture.

Acceptance then checks the restored schema without running migrations, provider
credential decryption with the matching installation master key, failure with a
wrong master key, existing-key authentication, member/admin authorization, prompt
version data, team/user budgets, model restrictions, settled cost and pending
reservation preservation. An exhausted restored user budget must still reject a
request before upstream dispatch. Utility output is captured, not logged; no
secret-bearing dump is uploaded as an artifact. The dump is mode 0600 inside a
private temporary directory and removed on exit. Generated databases are removed
in cleanup; CI also destroys the disposable service after the job.

This proves logical recovery of the tested schema and synthetic application data
when the corresponding encryption/signing configuration is available. It does not
establish an RPO/RTO, production-size throughput, WAL/PITR recovery, managed-cloud
snapshot recovery, external identity recovery, cross-version compatibility, or
complete role/ACL backup. Production backups need separate encrypted storage,
access controls, independent escrow of installation keys, off-site retention and
regular drills on production-representative datasets. Never restore an old database
into live traffic without reconciling revoked credentials, pending reservations
and provider billing. Restoring stale state can re-enable credentials or lose usage.

Local pytest covers opt-in/target refusal and fixture assertion logic only. The
actual dump/restore acceptance requires CI Docker plus disposable PostgreSQL and
must be reported separately from those local tests.
