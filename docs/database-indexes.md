# Database access paths (schema revision 7)

These indexes match predicates already present in the gateway. They do not change
budget semantics or make accounting constant-time.

| Table | Index columns | Existing query served |
| --- | --- | --- |
| usage | user_id, period | User monthly spending sum |
| usage | team_id, period | Team monthly spending sum |
| reservations | user_id, period, status | User monthly pending reservation sum |
| reservations | team_id, period, status | Team monthly pending reservation sum |
| audit; usage | created_at, id | Time-window exports and stable chronological pagination |
| api_keys | user_id, created_at, id | Self-service key listing for one owner |
| browser_sessions | user_id | Administrative session revocation for one user |
| browser_sessions; oidc_states | expires_at, digest | Select expired transient rows for cleanup |

The four original single-column subject indexes and the single-column audit time
index are replaced by composites with the same leading columns, not retained as
redundant copies. Existing period-only indexes remain unchanged. Digest primary
keys already serve authentication lookups; no second digest-only indexes are added.
The budget indexes avoid scanning other subjects/months/statuses, but summation
still reads matching rows. They are not covering balance ledgers or a substitute
for the accounting lock protocol.

Cleanup currently orders selected expiration candidates by digest. The new expiry
index can filter selective candidates but does not eliminate that sort. PostgreSQL
may prefer a sequential scan for large expired populations. Rate-bucket cleanup
uses a suffix expression on its composite string ID and remains non-sargable; no
ineffective ordinary index was added for that query. Query statistics, cardinality,
retention and workload determine actual plans. Additional indexes increase storage,
write amplification and vacuum work; none are added for hypothetical analytics.

## Deployment and verification

Revision 7 uses ordinary **transactional CREATE INDEX**, not `CREATE INDEX
CONCURRENTLY`. Stop or drain writers, take/verify a backup, and reserve a maintenance
window appropriate to table size and storage throughput. Index creation can block
writes, consume significant temporary/disk space, and run longer than a default
job deadline. Size and rehearse the migration on production-like data before
rollout. Replacements are built before their old prefix indexes are dropped;
transaction rollback restores the prior schema if any step fails. No rows are
changed. Automatic destructive downgrade remains disabled.

Run `python -m gateway.migrate`, then verify readiness and inspect representative
`EXPLAIN (ANALYZE, BUFFERS)` plans in a safe staging environment. The test suite
checks exact index columns, removal of replaced indexes, idempotent upgrades and
preservation of usage/pending reservations. With `TEST_DATABASE_URL` set to a
disposable PostgreSQL database, `tests/test_index_paths.py` additionally loads
20,000 synthetic rows per accounting/export table, runs ANALYZE, and verifies
selective budget/export queries choose the intended indexes without forced planner
settings. This is access-path evidence, not a throughput benchmark or a guarantee
that every production query will use an index.
