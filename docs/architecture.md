# Architecture and operating boundaries

This is an initial implementation, not a claim of enterprise certification or measured production capacity. See [feature matrix](feature-matrix.md) for supported scope and gaps.

## Stateless application, durable control plane

```text
SDK -> TLS ingress -> gateway replicas -> configured model providers
                         |
                         +-> shared PostgreSQL: identity, policy, accounting, audit
Browser -> TLS ingress -> admin API/UI -> external OIDC identity provider
SDK -> loopback relay -> short-lived token exchange -> gateway replicas
```

Stateless means no required replica-local durable data, not “no state anywhere.” Budgets, credentials, sessions and audit records necessarily persist. Production replicas must share the same PostgreSQL database, encryption key, signing key and OIDC configuration. SQLite is for one-process local development, not HA. HTTP connection pools and relay access-token caches are disposable in-memory state.

Users retain a source-managed primary team and may have explicit secondary memberships. Each key selects exactly one active team; user budgets remain shared across teams. Explicit team administrators have constrained team-local authority, not a general authorization policy language. See team-delegation.md for lifecycle and credential ceilings. Administrators provision model aliases, provider credentials and prices. Clients receive gateway keys, never provider credentials. API keys have expiry, revocation, model scope and rate limits. Agent credentials are a separate kind and exchange for short-lived inference tokens; they cannot administer the gateway.

## Accounting and concurrency

Spending is represented in integer millionths of a dollar. User and team budgets are UTC-calendar-month limits, not rolling or prepaid billing accounts. A request reserves estimated upper-bound cost before contacting a provider. Pending reservations continue counting if a process dies. Releasing them requires an administrator to check provider billing; blindly expiring reservations could let charged requests evade budgets.

On PostgreSQL, admission and settlement take a shared policy gate plus ordered subject row locks; independent teams can progress concurrently, while hot shared users/teams still serialize. Control-plane mutations take the exclusive policy gate to fence revocation and configuration changes. Increasing replica count does not remove hot-subject or control-plane contention. See [the locking protocol](policy-concurrency.md) and benchmark real traffic and database contention before selecting capacity targets. Price configuration, provider usage formats and supported request formats affect accounting accuracy; it is not an authoritative invoice ledger.

## Availability

Deploy replicas behind TLS termination and a load balancer. Run schema setup as a controlled deployment step. Keep database backups, point-in-time recovery, secret recovery and failover procedures outside the application. A replicated gateway with a single unprotected database is not a highly available service. Do not treat sample Helm or Terraform definitions as a tested disaster-recovery plan.

Do not retry inference blindly after a timeout: the provider may already have generated and billed an answer. Streaming disconnects are partial results, not safe replay points. Provider-specific request support is intentionally narrower than every vendor's full API.

## Management interface

`/admin` serves static HTML, CSS and JavaScript without third-party frontend dependencies. Manual administrator API keys remain in page memory, not browser storage. OIDC uses a server-side session with an HttpOnly cookie and CSRF protection for mutations. The UI supports creation, budget and enablement changes, credential lifecycle, usage/audit inspection and deterministic evaluation. Advanced provider updates and bulk provisioning use the API. Lists are bounded; use `limit` and `offset` for larger exports.

The initial admin panel is not a delegated team portal, billing portal or comprehensive monitoring suite. [Local relay details](local-relay.md) explain its credential lifecycle and limitations.
