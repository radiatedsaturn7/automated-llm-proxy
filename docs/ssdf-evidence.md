# NIST SSDF source evidence and remaining work

This maps selected **SSDF 1.1 practice groups** in
[NIST SP 800-218](https://csrc.nist.gov/pubs/sp/800/218/final) to reviewable project
artifacts. It is not a task-by-task assessment, certification, supplier
attestation or claim that a development organization conforms to SSDF. Source
controls are only part of a secure development lifecycle. An accountable release
owner must assess applicability and retain exact-version evidence.

Status terms follow [the applicability matrix](compliance-applicability.md).
**Implemented** below means the named artifact/control exists, not that the
whole SSDF practice is fulfilled. **Not assessed** means organizational evidence
has not been established. Tests named below are test definitions, not a claim
that the latest head has passed them.

| SSDF practice | Implemented source contribution | Gap / responsibility |
|---|---|---|
| PO.1 Define security requirements | [Security boundaries](security.md), [readiness gates](security-readiness.md), [data inventory](data-inventory.md), [permission model](credential-permissions.md) | **Operator responsibility / not assessed:** accountable requirements owners, legal/customer obligations, threat-model maintenance, risk acceptance and change approval. Documentation alone is not a completed threat assessment. |
| PO.2 Implement roles and responsibilities | Reviewable source and release evidence records | **Not assessed:** named security ownership, staff training, separation of duties, incident coverage and escalation. Do not treat an AI contributor as organizational assurance. |
| PO.3 Implement supporting toolchains | [CI](../.github/workflows/ci.yml), [locked dependency process](dependencies.md), lint/test/dependency-audit commands | **Operator responsibility:** repository protections, reviewer requirements, runner isolation and token permissions must be verified in the actual hosting settings. Workflow files do not establish those settings. |
| PO.4 Define criteria for security checks | [Readiness checklist](security-readiness.md), [release evidence ledger](release-evidence.md), explicit CI jobs | **Not assessed:** complete security acceptance criteria, waiver process, independent review and enforcement on every release branch. A green suite is not comprehensive security acceptance. |
| PO.5 Implement secure development environments | Disposable test service definitions and isolated test credentials in CI | **Operator responsibility / not assessed:** developer endpoints, runner/organization administration, secrets custody, network isolation, access reviews and environment hardening. |
| PS.1 Protect code from unauthorized access/tampering | Git history and reviewable pull-request workflow | **Not assessed:** effective branch protection, signing requirements, maintainer access reviews, recovery controls and restricted release authority. Inspect actual GitHub configuration. |
| PS.2 Provide a mechanism to verify release integrity | [Dependency hash locks](dependencies.md) protect selected install inputs | **Not supported:** complete signed release artifacts and verifiable build provenance. Dependency hashes do not authenticate a published container or prove its build origin. |
| PS.3 Archive and protect each software release | [CI evidence bundle](dependencies.md) records selected manifests/installed versions and CI SHA, with finite artifact retention | **Operator responsibility / not supported:** durable controlled release archive, full SBOM, build provenance, verification material and retained remediation decisions. CI artifacts are not a permanent archive. |
| PW.1 Design software to meet security requirements | [Architecture](architecture.md), [shared policy concurrency](policy-concurrency.md), [secret operations](encryption-key-operations.md), scoped credentials | **Not assessed:** complete design threat models, abuse cases and traceability from every security requirement to design and tests. |
| PW.2 Review software design | Security-focused regression tests and documented trust boundaries | **Not assessed:** independent design review, formal findings, reviewer qualifications and resolution evidence. No external audit is claimed. |
| PW.4 Reuse well-secured software when feasible | Dependency locking and `pip-audit`; [dependency limitations](dependencies.md) | **Operator responsibility:** vulnerability triage, dependency/image/license provenance, update cadence and unsupported dependency policy. Audit output only covers known findings at scan time. |
| PW.5 Create source code using secure coding practices | Explicit permission checks, bounded JSON parsing, input validation and sanitized errors; [security overview](security.md) | **Not assessed:** complete secure-coding standard and organization-wide enforcement. Selected controls do not imply all defects are eliminated. |
| PW.6 Configure compilation/build processes | [Dockerfile](../Dockerfile), locked package install and deployment validation | **Not supported / operator responsibility:** fully pinned reproducible build inputs, signed images and complete build attestation. Base images/actions and other toolchain inputs need separate review. |
| PW.7 Review/analyze human-readable code | Ruff lint, dependency audit and focused authorization/regression tests in CI | **Not assessed:** independent source review coverage, comprehensive static analysis, secrets scanning and vulnerability discovery program. Tests are not substitutes for source review. |
| PW.8 Test executable code | [Test suite](../tests), real disposable PostgreSQL, Keycloak/browser and two-replica fixtures in [CI](../.github/workflows/ci.yml); [restore drill](restore-drill.md) | **Operator responsibility:** exact-release results and actual deployment/provider acceptance, adversarial assessment, capacity/failover tests and infrastructure scanning. Synthetic providers do not certify live vendor behavior. |
| PW.9 Configure secure defaults | Default-off local auth/member issuance, bounded credentials, non-root container and documented TLS/secret requirements | **Operator responsibility:** enforce production TLS, trusted IdP/group mapping, DB/backup protection, egress and deployment settings. Examples are not a hardened installation certificate. |
| RV.1 Identify and confirm vulnerabilities | CI dependency audit and [reporting guidance](security.md) | **Not assessed:** staffed intake, monitoring and response targets. Verify private vulnerability reporting is enabled; do not assume documentation enables it. |
| RV.2 Assess, prioritize and remediate vulnerabilities | Regression tests can accompany fixes; release ledger can identify tested commits | **Operator responsibility / not assessed:** triage ownership, severity policy, affected-version analysis, patch support window, advisories, customer notification and statutory reporting. |
| RV.3 Analyze vulnerabilities to identify root causes | Focused regression tests provide a place to retain a defect's reproducible case | **Not assessed:** formal root-cause review, systemic corrective actions, trend tracking and verification that equivalent defects were removed. |

## Evidence collection for an actual release

1. Record the source commit, PR head and CI tested SHA separately when they
   differ. Preserve full job logs, test counts/skips, dependency findings and
   approved exceptions. Historical ledger entries do not attest later changes.
2. Record the image digest, builder and complete build inputs. Produce and
   verify SBOM/provenance/signatures when available; mark missing evidence
   explicitly rather than relabeling the dependency manifest as an SBOM.
3. Assign an owner and disposition to each gap above. Attach actual repository
   access/protection, incident, vulnerability and support-policy evidence where
   organizational controls are required, without publishing secrets.
4. Execute deployment-specific security, recovery and load acceptance in an
   authorized test environment. Review residual risks before sensitive use.
5. Retain the resulting evidence under an access-controlled retention policy,
   including resolved findings and release rollback information. The operator
   must supply this storage and process; this document does not create it.
