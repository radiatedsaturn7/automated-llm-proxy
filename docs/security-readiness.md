# Internal security and operational readiness

This is Brrrtgate's internal evidence checklist, not a certification, external
audit report, or blanket production approval. No outside audit is commissioned
by this checklist. Owners should record a deployment-specific decision, exact
commit/image digest, configuration, test run links, remaining risks and rollback
plan before admitting sensitive traffic.

## Evidence checkpoint

Latest verified candidate: PR #16 at exact head
`7acaa9848d9c9873678bd0a31750a5a05bb11589`.
[CI run 43125212538](https://github.com/radiatedsaturn7/automated-llm-proxy/actions/runs/43125212538)
passed all five jobs: **613 Python tests passed / 8 skipped**, **621 PostgreSQL
tests passed**, logical backup/restore acceptance passed, **23 Chromium tests
passed**, **2 actual Keycloak tests passed**, and deployment/monitoring checks
passed. Dependency audit found no known vulnerabilities. The SDK contracts use
pinned OpenAI Python 3.49.0 and real ASGI gateway processing with simulated
upstream responses. No paid provider calls were made.

**PR #16 merged as `7af1ab65c351cf658ff24226a4339dc4087b512c`.**
PR #15 was approved and merged as `e3b514392c2a798229af2c02889f89a71ad485ee`.
Green CI is not production acceptance or permission to merge. Later round-17
changes require their own exact-head evidence.

The repeated two-distinct-process drill reported 40 budget requests with five
accepted/35 denied and 3,370 microdollars held against a 4,000 limit; 32 rate
requests with six accepted/26 denied; one SIGKILL-interrupted request retaining
668 microdollars without replay; eight successful survivor/restart requests;
and 19 settled usage records totaling 95 microdollars plus one pending reservation.
These are functional synthetic fixture results, not production throughput.

The actual disposable Keycloak tests verified TLS, PKCE, group mapping, CSRF,
local browser logout and signed back-channel logout. They do not certify other
IdPs or the organization's configured MFA, rollover and lifecycle policies.
See [SDK contracts](sdk-compatibility.md) and the exact job logs for scope.

Evidence levels are deliberately separate:

- **Unit/integration tests:** deterministic local or in-process HTTP fixtures.
- **PostgreSQL CI:** the same suite plus database-specific concurrency tests on a
  disposable real PostgreSQL instance, not SQLite substitutes.
- **Browser CI:** Chromium against the built Compose service, including real
  bootstrap/admin/provider flows and explicitly mocked session/UI lifecycle tests.
  Mocked SSO state is not a real IdP interoperability test.
- **Real IdP CI:** separate disposable Keycloak with TLS verification enabled,
  actual browser authorization and signed logout. Two passing cases cover the
  stated flows, not every identity-provider configuration.
- **Official SDK acceptance:** pinned Python client over the actual ASGI gateway;
  provider HTTP is simulated, so this is not vendor service certification.
- **Restore drill:** a real disposable PostgreSQL dump/restore with synthetic
  application records and matching installation keys. Not a production backup drill.
- **Two-worker drill:** two actual gateway processes, shared PostgreSQL and a TLS
  mock provider; concurrency, worker loss and restart. Not database failover or
  automatic load-balancer failover.

The [CI workflow](../.github/workflows/ci.yml) defines what runs. A test file's
presence alone is not proof it executed: inspect job logs, skips and exact commit.
The subsequent round-17 workflow bounds every job to 15 minutes, or 20 minutes
for PostgreSQL including its restore drill; each full pytest step is limited to
10 minutes. These are termination bounds, not passing outcomes or a substitute
for inspecting failures. A forcibly terminated runner may not complete cleanup;
fixtures remain disposable and must never target production infrastructure.

## Implemented controls and regression evidence

Checked entries below mean implemented with repository regression coverage, not
universal security guarantees or successful acceptance in an operator's environment.

- [x] **Credential and role boundaries.** Expiring/revocable hashed gateway keys,
  separate API/agent credentials, explicit permissions and model/team scopes;
  delegated team administrators cannot elevate global authority. Admission
  revalidates credentials and policy under database locks. See
  [permissions](credential-permissions.md), [team delegation](team-delegation.md),
  [tests](../tests/test_permissions.py), [team tests](../tests/test_teams.py).
- [x] **Federated identity.** OIDC signature/issuer/audience/nonce/PKCE validation,
  explicit group mapping, deliberate identity linking, bounded metadata/JWKS
  cache, signed back-channel logout with replay/tombstone handling. SCIM is a
  documented subset. See [security](security.md), [provisioning](provisioning.md),
  [OIDC tests](../tests/test_oidc.py), [cache tests](../tests/test_oidc_cache.py),
  [logout tests](../tests/test_oidc_logout.py), [SCIM tests](../tests/test_scim.py).
- [x] **Optional local passwords.** Disabled by default; administrator provisioning,
  salted scrypt, bounded hashing concurrency, durable login throttling,
  same-origin login and federation restrictions. Repeated cancellation no longer
  releases hashing capacity before executor work finishes. See
  [local password limits](local-passwords.md), [tests](../tests/test_local_auth.py),
  [cancellation regression](../tests/test_review_hash_cancellation.py).
  Local TOTP adds administrator-granted setup, encrypted seeds, bounded challenges and database-fenced replay counters. Enrolled accounts cannot bypass it; required policy also rejects older unverified local sessions. It is not phishing-resistant MFA or a self-service recovery system. See [MFA coverage](../tests/test_local_mfa.py).
- [x] **Browser safety.** CSP/external scripts, text rendering, cookie-session CSRF,
  no local/sessionStorage secrets, one-time key display cleanup and stale-response
  guards. SSO-assisted enrollment is opt-in and requires explicit approval;
  browser never receives relay root secrets. See [browser tests](../tests/browser),
  [enrollment](enrollment.md), [self service](self-service.md).
- [x] **Accounting consistency.** Conservative reservation before dispatch,
  subject locking, duplicate-settlement protection and revocation ordering.
  Uncertain work is not automatically refunded or replayed. See
  [locking protocol](policy-concurrency.md), [tests](../tests/test_policy_scaling.py),
  [replica harness](../tests/ops/replica_drill.py), [operations](operations.md).
- [x] **Provider failure containment.** Explicit supported adapters, shared circuit
  state, no blind same-request retry, byte/event/duration bounds, identity-only
  encoding, strict provider JSON and independently timed streaming cleanup.
  Newline-dense SSE parsing uses linear offsets and periodic task yielding.
  See [provider boundaries](providers.md), [health](provider-health.md),
  [stream tests](../tests/test_stream_limits.py),
  [response-bound tests](../tests/test_response_bounds.py).
- [x] **Opt-in content controls.** Fixed bounded heuristic detectors for inbound
  content and separately enabled non-streaming output inspection. Unsupported
  output representations fail closed; output policy rejects streaming before
  dispatch. Metadata-only audit/error handling. See [limits](guardrails.md),
  [inbound integration](../tests/test_guardrails_integration.py),
  [output tests](../tests/test_output_guardrails.py).
- [x] **Operational boundaries.** Production mode requires PostgreSQL/HTTPS;
  invalid environment aliases fail closed. Readiness checks expected migrations;
  preflight checks schema/provider credential decryptability. Nonroot images,
  restricted Helm pods, optional bounded maintenance CronJob and scoped metrics.
  See [operations](operations.md), [migrations](migrations.md),
  [Helm tests](../tests/test_maintenance_helm.py), [preflight tests](../tests/test_preflight.py).
- [x] **Recovery/export tools.** Durable local relay rollover and replay fencing;
  signed bounded audit export and collector checkpoints; synthetic logical
  PostgreSQL restore acceptance. See [relay](local-relay.md),
  [collector](audit-collector.md), [restore drill](restore-drill.md).

## Open risks and priority acceptance work

### Release gates for the intended installation

- [ ] Require all required checks on the **exact release head** to pass. Resolve
  failures; investigate skipped backend-specific tests rather than counting them
  as success. Freeze tested dependencies/image digest and retain provenance.
- [ ] Run actual IdP acceptance: issuer discovery, group mapping, wrong audience,
  MFA policy at the IdP, JWKS rollover/removal, disabled user, SCIM deactivation,
  membership changes, replayed logout and sessions established during logout.
  Verify the organization's IdP emits the required claims/events. API/agent
  credentials are separate from browser logout and need their own revocation.
- [ ] Verify real provider contracts with bounded, non-sensitive requests and an
  explicit spending cap: supported API versions, tool JSON, streaming endings,
  usage totals, rate errors, model aliases and configured prices. Mock providers
  demonstrate protocol behavior, not vendor certification or invoice accuracy.
- [ ] Validate deployment ingress/TLS/proxy settings, DNS and egress firewall,
  provider destination restrictions, PostgreSQL TLS/accounts, metrics protection,
  secret mounts and management access. App URL checks cannot prevent all DNS
  rebinding or network misconfiguration. Never expose test-provider controls.
- [ ] Rehearse backup/restore with the intended database service, realistic data
  volume, matching escrowed encryption/signing secrets and restricted dump access.
  Establish RPO/RTO. Test lost-key and wrong-key recovery. Reconcile usage,
  pending reservations and revocations before restoring traffic to old state.
- [ ] Rehearse gateway/provider-key replacement and installation-secret recovery.
  Provider secrets are encrypted, not recoverable without the master key.
  Installation master/signing-key rotation is not an automatic multi-key migration;
  do not simply replace environment variables and assume existing state works.
- [ ] Exercise target-platform disruption: graceful drain, SIGKILL, failed rollout,
  schema incompatibility, connection exhaustion, database failover and partitions.
  The included worker-loss drill proves surviving-worker requests when explicitly
  selected; it does not prove ingress health routing or managed-DB failover.
- [ ] Load-test representative request sizes, streams, hot and independent teams,
  slow clients, cancellations and auth bursts. Measure steady/peak memory, CPU,
  lock waits and tail latency. Set concrete capacity limits and SLOs. Small CI
  latency samples and Docker snapshots are not capacity or SLA commitments.

### Known product boundaries requiring explicit operator acceptance

- Local TOTP is phishable and has no recovery-code/self-service reset workflow; prefer a phishing-resistant IdP policy. No SAML, complete
  SCIM certification, real-vendor enrollment certification or continuous IdP group
  refresh. Session expiry/cache TTL and revocation windows must fit the threat model.
- No WORM or cryptographically tamper-evident audit store. Collector checkpoints
  are not proof of immutable delivery. Define retention, erasure and durable export
  destination controls. Reverse proxies/APM/providers can independently log content.
- No contractual data-residency guarantees, regional policy enforcement, compliance
  attestation, database failover orchestration or multi-region consistency guarantee.
- Heuristic DLP has false positives and false negatives, including encoded/split
  values and unknown formats. It is not prompt-injection prevention. The gateway
  does not execute model tool calls; downstream applications must validate them.
- Hot teams still serialize accounting; control-plane writes take an exclusive
  policy gate. Pending reservations conservatively consume budget after uncertain
  failure. Operators need billing-aware reconciliation, not timeout-based refunds.
- The opt-in relay journal intentionally contains local durable secret state;
  stateless gateway replicas do not make endpoints stateless. Endpoint compromise
  requires credential revocation and incident handling.
- Explicit prompt-registry content and evaluation datasets/results can contain
  sensitive material even though normal inference audit avoids prompt bodies.
  Apply appropriate access, backup, retention and data-classification policy.

## Acceptance record

For each unchecked step, record owner, date, exact environment/image/configuration,
evidence link, result, residual risk and remediation/rollback. Keep credentials,
raw user content and secret-bearing dumps out of that record. A deferred item
must have an explicit risk owner; do not silently convert it into a passing check.
Internal readiness decisions do not imply an external audit has occurred.

For the current single-master-key limits and a cautious recovery/compromise
checklist, see [encryption key operations](encryption-key-operations.md). Mounted
secret delivery is not automatic key rotation; a future fenced re-encryption
mechanism is a design proposal, not an available command.
