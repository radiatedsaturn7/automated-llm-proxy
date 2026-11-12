# Data inventory and retention boundaries

This is a source-level inventory, not a complete deployment record of processing
activities. Operators must add ingress, observability agents, database services,
backups, identity/model providers and local endpoints. Data described as metadata
may still be personal or confidential. User IDs and hashed values are not
necessarily anonymous. The current schema is in [`gateway/db.py`](../gateway/db.py).

## Data flow

An authenticated client sends content to the gateway, which validates policy and
reserves estimated spend, then forwards the supported request to the configured
model provider. The result is returned and usage settled. Normal inference does
not intentionally persist raw prompt/response bodies in the application DB.
That is not a guarantee of zero retention by infrastructure or providers, and it
is distinct from explicitly stored prompt templates below. Streaming and request
processing necessarily hold content in process memory. Core dumps, debug tooling,
APM body capture and reverse-proxy logs require separate operator controls.

## Stores and handling

| Store / data | Purpose and source | Access / protection | Retention and rights limitations |
|---|---|---|---|
| Users, teams, memberships and SCIM links | Email, subject/external IDs, names, role, team, enablement and budgets (`User`, `Team`, `TeamMembership`) | Authorized administrative/provisioning APIs; member/team views are scoped. DB operator is trusted. | No general hard-delete/subject-erasure API. SCIM user deletion deactivates; group deletion tombstones and retains historical metadata. Offboarding is not erasure. |
| API and agent credentials | Keyed digests, prefixes, names, owners, scopes, expiry, revocation, generation and issuance IDs (`APIKey`, `AgentEnrollment`, `AgentRotation`) | New secrets displayed once; raw API keys are not recoverably stored. Enrollment and rollover intentionally use bounded replay state. | Revoked/expired key records remain. Rotating/revoking a credential does not delete usage/audit history or endpoint copies. |
| Passwords/MFA and throttle state | Salted password hashes; encrypted TOTP seeds; challenge/grant digests, counters, bounded hashed throttle buckets (`LocalPassword`, `LocalMFA`, `LoginThrottle`) | Default-off local auth, administrative provisioning, proof required for self-service change; [local passwords](local-passwords.md). | Expired MFA setup/challenge fields can be cleared by maintenance. Active seeds remain; no comprehensive account-erasure process. Hashed throttle identifiers are not a promise of anonymization. |
| Login/session/replay state | OIDC nonce and PKCE verifier, session digest, CSRF, user/issuer/subject/session IDs and expiry; logout replay fences | Short-lived protocol state, server-side authorization/revocation and expiry; never export as audit data | Bounded maintenance removes eligible expired OIDC states/sessions/replay records when explicitly run. Expiry authorization is separate from physical deletion. Do not erase active replay fences to satisfy an ad hoc purge. |
| Provider configuration and credentials | Base URL, model aliases/prices, priority and encrypted API credential (`Provider`); health/circuit metadata (`ProviderHealth`) | Privileged configuration; encryption key held outside DB; [secret operations](encryption-key-operations.md) | No automatic provider-config retention/purge. Disabling a provider is not deletion. External-provider account retention and billing are outside the gateway. |
| Reservations and settled usage | User/team/key/provider IDs, model alias, tokens, estimated/settled cost, status, timestamps (`Reservation`, `Usage`) | Shared accounting transactions; limited own-usage portal and privileged administrative export | No automatic deletion. Pending reservations remain conservatively charged until explicit reconciliation. **Deleting current accounting can reopen budget**; no generic retention command is supplied. |
| Request allowances and member key policies | Daily allowance counts, current-month reset operation IDs, templates/limits (`UserAllowance`, `UserKeyPolicy`) | Admin changes policy; browser members can exercise only granted actions | Bounded current-period retry state is not total billing history. Old audit events remain. Resetting an allowance never erases spend. |
| Audit DB | Actor, action, target, timestamp and bounded action-specific metadata (`Audit`) | Administrative export; [exports](audit-export.md). Prompt/response bodies and raw credentials are not intended audit payloads. | No automatic audit retention/legal hold/WORM policy. Do not assume IDs/model names are nonpersonal. DB administrators can modify rows. |
| Prompt registry | Explicitly submitted plaintext template content/variables, versions and creator metadata (`PromptTemplate`, `PromptVersion`) | Administrator-only; [prompt registry](prompts.md) | Archive retains immutable versions. No hard-delete or automatic retention. Never store regulated data or credentials without an approved policy. |
| Stored evaluation reports | Rules, pass/fail results, lengths/token estimates, actor/version/time (`EvaluationRun`) | Administrative registry access | Raw submitted cases/expected/generated text are not saved by this report path; source datasets may exist elsewhere. Reports themselves persist with no automatic purge. |
| HTTP logs and metrics | Generated request ID, router template, method, status, duration/completion; aggregate counters/histograms | [`gateway/observability.py`](../gateway/observability.py) avoids request bodies, raw queries/headers and user IDs as metric labels | Runtime log/metrics retention is operator-owned. Other application/server/ingress/APM components must be assessed separately; this middleware is not universal log sanitization. |
| Local relay/enrollment journals | Durable local credential/rollover state where enabled | Protected endpoint-owned files; [relay](local-relay.md) | Intentionally local state. Gateway session revocation does not delete endpoint files; offboarding must revoke credential and clean authorized endpoint stores. |
| Audit exports and evaluation datasets/reports | Operator-created JSONL/checkpoint/seal files; user-created input datasets and output reports | [collector](audit-collector.md), [seals](audit-sealing.md), [runner](evaluation-runner.md) | Local retention and external recipients are operator-owned. Exporting does not remove DB originals. Snapshot seals are not deletion or immutable-retention mechanisms. |
| Backups/replicas/snapshots | Copies of the foregoing | Encryption/access/key escrow and recovery under operator control; [restore drill](restore-drill.md) | No product-enforced backup retention/subject purge. Restoring old state can resurrect identities/keys; reconcile revocations and rights decisions before serving traffic. |

## Maintenance is not a privacy erasure tool

[`gateway/maintenance.py`](../gateway/maintenance.py) defaults to dry run and
bounds each batch. It targets eligible expired OIDC states, browser sessions,
logout replay records, old minute rate buckets and transient MFA fields. It
**does not delete users, credentials, usage, reservations, audit rows or prompt
history**. Scheduling is optional, so expired records can remain physically
present when maintenance is not run. Tests of this tool do not certify a legal
retention policy.

## What rights support exists, and what does not

Implemented: scoped account views, [bounded own usage](member-usage.md), privileged
metadata exports, password/session controls, credential revocation and user
turn-off. These are useful inputs, not a complete access/portability/rectification
or erasure workflow. The member history window is a query limit, **not a retention
period**. A JSONL export is not automatically a complete subject-access response.

Not supported: a verified subject-rights case workflow, comprehensive subject
inventory/export, authorized cross-store erasure/pseudonymization, configurable
usage/audit/prompt retention, legal-hold enforcement or backup deletion tracking.
Do not promise those capabilities to customers until implemented and tested.

An operator handling a request must first verify identity/authority, determine
applicable exceptions and legal holds, inventory all stores and processors, and
record an approved action plan. Do not remove active security fences, current
budget records or pending reservations in an ad hoc SQL script. Review referential
integrity, billing, retention obligations and backup restoration behavior. No
production deletion is authorized or performed by this document.

## Deployment inventory template

For each store, record its owner, purpose/legal basis, data classifications,
provider/region, authorized readers/writers, encryption/key owner, retention
trigger/duration, deletion/hold procedure, backup policy, onward recipients,
rights-response mechanism and tested evidence. Keep secret values and actual
personal data out of that inventory. Revalidate after schema/provider/logging
changes. Link the record to the [applicability decision](compliance-applicability.md).
