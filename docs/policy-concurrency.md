# Admission concurrency and policy consistency

PostgreSQL inference transactions no longer take an exclusive global lock. They
acquire a shared (`FOR SHARE`) policy gate, then lock subjects in a fixed order:
**Team → User → APIKey → Reservation (finalization only)**. Teamless users skip
the team row. Admission checks balances and creates a reservation in that same
transaction. Finalization locks the same subjects, refreshes the reservation under
a row lock, and settles it once. Transactions use explicit `READ COMMITTED`, so
budget queries after waiting for locks see prior committed usage/reservations.

Unrelated teams can perform admission and settlement concurrently. Requests in
one team still serialize during short accounting transactions, including when a
team has no budget; user/key rate-limit updates remain protected. Provider network
requests and response streaming occur outside these transactions.

Control-plane writes keep the exclusive policy gate. This includes administrative
budget changes, revocations, membership changes, OIDC provisioning and reservation
reconciliation. They cannot interleave with an admission/settlement decision.
Revocation does not cancel a provider request already admitted. New admission
checks the current credential again after acquiring the gate and subject locks.

## Invariants for future code

- Always acquire the policy gate before subject locks. Do not upgrade a shared
  gate to exclusive inside a transaction.
- All policy/control-plane mutations must use the exclusive `db.transaction()`.
- Only inference accounting uses `db.transaction(inference=True)`; it must then
  lock policy subjects using `db.lock_policy_subjects()` in its fixed order.
- Read subject identifiers after acquiring the gate. Never trust cached membership
  to select locks. Refresh locked ORM rows before checking their mutable fields.
- Do not perform network calls while holding policy transactions.
- Never directly edit policy/accounting tables outside this protocol during live
  traffic. Migration jobs require their separate deployment/maintenance plan.

This is not lock-free accounting or a throughput guarantee. Hot teams serialize;
control-plane writes momentarily block all admission/settlement; monthly aggregate
queries still scan the relevant usage and pending reservations. Those are the
remaining scaling limits. A future sharded balance ledger requires migrations and
its own recovery/consistency design. PostgreSQL is required for multi-replica use;
SQLite keeps `BEGIN IMMEDIATE` serialization for development.

`tests/test_policy_scaling.py` checks duplicate settlement and mixed accounting
operations on both backends. With `TEST_DATABASE_URL` set to a disposable PostgreSQL
database, it additionally checks independent-team progress, same-team budget
visibility after lock waits, and revocation serialization. The shared test fixture
resets that database. These tests check correctness and progress, not performance.

## Admission time and clock synchronization

After acquiring the team/user/key subject locks, admission samples the database
wall clock once. PostgreSQL uses `clock_timestamp()`, not transaction-start time,
so waiting for a lock cannot retain an outdated admission timestamp. This same UTC
sample determines credential expiry at admission, the monthly budget period and
the per-key minute rate bucket. Settlement retains the reservation's original
period even if it completes in a later month. SQLite development installations
use SQLite's current timestamp (second precision).

Replica-local clock skew therefore cannot split financial admission across
budget periods or rate buckets. This is not a replacement for synchronized clocks
(NTP): initial authentication, JWT validation/issuance, and record timestamps still
use application clocks. A fast application clock may reject a credential early;
passing initial authentication never skips the database-time admission expiry
check. Fixed-window rate limits still allow bursts straddling a minute boundary;
this change does not turn them into sliding-window limits. Keep database hosts'
clocks synchronized as well, including across database failover.

Rate-bucket maintenance also samples the database clock inside its exclusive
transaction, retaining at least one day of buckets. A fast maintenance replica
cannot delete the active minute bucket and reopen its allowance. Other transient
session/authentication cleanup continues to use application time.
