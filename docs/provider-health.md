# Shared provider circuit breaker

The gateway selects **one** enabled provider for each model alias before reserving
budget and dispatching a request. Providers are ordered by ascending `priority`,
then provider ID. After repeated failures, later requests skip an open route and
may select the next configured provider for that alias. The gateway **never retries
an already-dispatched inference request**, including one that timed out or returned
an error. The existing conservative accounting policy is unchanged.

Administrators must deliberately configure equivalent models and accurate prices
on fallback providers. A shared alias does not guarantee equivalent model behavior,
region, data handling or cost. No provider is discovered or added automatically.
Azure/OpenAI embeddings route only to adapters that support embeddings.

## Shared state and configuration

Schema revision 5 adds `provider_health`, keyed by provider ID and model alias.
All replicas consult the external database; no local in-memory circuit state is
required. PostgreSQL row locks elect one half-open probe per route and lease.
Database time determines deadlines. SQLite remains development-only.

Environment settings:

- `CIRCUIT_BREAKER_ENABLED=true`: set false to restore deterministic priority-only
  selection, without discarding persisted health history.
- `CIRCUIT_FAILURE_THRESHOLD=3`: consecutive counted failure outcomes open a route.
- `CIRCUIT_COOLDOWN_SECONDS=30`: minimum wait before a probe can be admitted.
- `CIRCUIT_PROBE_SECONDS=150`: configured minimum probe lease. The effective lease
  is `max(CIRCUIT_PROBE_SECONDS, PROVIDER_TIMEOUT_SECONDS + 30)`, so a 600-second
  provider timeout yields at least a 630-second lease. Size for expected request
  duration; an expired lease allows recovery after a crashed worker. Provider
  timeout is a read-idle timeout, not a total stream deadline. A request
  still executing after its lease expires can overlap a replacement probe. These
  are distinct requests, never automatic replay of one inference request.

Each successful response/complete stream closes the circuit and resets failures.
Transport errors, malformed responses and 5xx increment failures. A 429 opens the
route immediately; integer `Retry-After` is respected up to one hour (date-form or
invalid values use the configured cooldown). Provider 401/403 opens the route with
`configuration_error` for operator inspection. Redirects count as failures; they
are never followed. Other rejected client requests do not increment failures.
A rejected half-open probe reopens for a cooldown rather than declaring the
provider healthy. No provider response bodies or credentials enter health records.

Concurrent completions carry an epoch/probe ticket. A completion from an older
open/probe/configuration generation cannot close a newer circuit. A budget or
payload rejection before dispatch releases its probe lease. Client disconnection
is not treated as a provider failure; an interrupted probe lease expires normally.
Health reporting after dispatch is best-effort and never triggers a provider
retry or changes the billing result. Admission fails closed if the required
shared database state cannot be read.

If no route is available, the request receives `503` with bounded-next-deadline
`Retry-After`, without inference dispatch or budget reservation. `/v1/models` still
lists configured aliases; it is not a live health guarantee.

## Operations API

Administrator role **and** administrator credential scope are required:

- `GET /admin/api/providers/{provider_id}/health`: current per-model state,
  normalized last outcome, deadlines, epoch and probe identifier.
- `POST /admin/api/providers/{provider_id}/health/reset`: reset circuits and fence
  old in-flight outcomes. The action is audited.

Updating provider configuration also resets its health state and fences old
outcomes. Fix a bad credential/model configuration before resetting repeatedly.
The existing provider `enabled` flag is authoritative; resetting health never
enables a disabled provider. Open circuits are a reliability signal, not evidence
of malicious behavior or an authorization control.

Tests cover deterministic next-request fallback, no same-request retry, unchanged
billing, shared half-open election, stale completion fencing, expired leases,
configuration reset, budget-denied probes, and disabled breaker behavior. The
PostgreSQL-only test verifies distinct backend sessions and a single winner among
25 concurrent probe contenders when executed by the PostgreSQL CI job. Local
SQLite runs skip that test; consult the release CI result for execution evidence. This is correctness coverage, not a
throughput or failover SLA benchmark.
