# Compliance applicability and shared responsibility

This is an engineering applicability aid, **not a legal opinion, certification,
SOC 2 report, ISO certificate, regulatory filing, or promise of compliant use**.
It describes source capabilities and known gaps as of 2026-11-12. An operator
must assess the actual jurisdiction, contracts, data, sector, deployment and
purpose. Passing CI does not answer those questions. Record the exact release
commit/image digest and an accountable owner for each deployment decision.

## Status vocabulary

- **Implemented:** a specific source capability exists, with the linked scope.
  This does not mean an entire regulatory obligation is satisfied.
- **Operator responsibility:** deployment, organization, contract or process
  evidence must be supplied by the party running/distributing the service.
- **Not supported:** the product does not currently provide the named workflow.
- **Not assessed:** no determination or independent assurance has been made.

The upstream project publishes Apache-2.0 software. A customer running it is the
service operator. A party offering a hosted Brrrtgate service may have additional
processor/service-provider responsibilities; a commercial distributor may have
manufacturer or supplier responsibilities. The same company can hold multiple
roles. Calling an application open source does not exempt a hosted business or
commercial distribution from otherwise applicable duties.

## Applicability decisions, not blanket claims

| Area | When to assess | Source contribution | Status / missing evidence |
|---|---|---|---|
| EU/UK and other privacy laws | Personal data in identities, usage metadata, prompts or responses; relevant territorial scope | Scoped access, explicit administrative exports, minimized routine telemetry; [inventory](data-inventory.md) | **Operator responsibility / not assessed:** determine controller/processor roles, legal basis, notices, processing agreements, transfers, subprocessors and rights handling. **Not supported:** complete subject export/erasure workflow, retention/legal-hold engine or enforceable regional routing. |
| HIPAA | Deployment handling protected health information for a covered entity or business associate | Auth, access controls, audit metadata, encryption of selected secrets | **Not assessed:** no HIPAA-ready claim. Operator must establish applicability, risk analysis and safeguards, and appropriate business associate agreements across hosting/model providers. The project cannot provide a BAA for services it does not operate. Do not admit PHI until those decisions and safeguards are independently accepted. |
| SOC 2 / ISO/IEC 27001 | Customer assurance requirements or organizational certification objectives | Tests, access/credential controls, operational documentation and bounded export tools are possible evidence inputs | **Not assessed:** no report, certification, auditor opinion, complete control framework or operating-effectiveness period. Organizational controls, policies, personnel, vendors and evidence remain outside this codebase. These are not automatically legal prerequisites for every installation. |
| Secure software procurement / NIST SSDF | Procurement clauses, customer requirements or supplier risk program | [SSDF evidence map](ssdf-evidence.md), locked dependencies and exact-head tests | **Implemented (scoped artifacts) / operator responsibility:** no conformance attestation. Formal process ownership, durable release provenance, complete SBOM and independent assurance remain gaps. |
| EU Cyber Resilience Act (CRA) | Software made available on the EU market within the Act's scope, including commercial distribution and relevant open-source roles | Security controls, documented limitations, maintenance and dependency evidence | **Not assessed:** determine economic-activity/manufacturer/open-source-steward role, product classification and exclusions before claiming scope or exemption. Commercial activity does not become exempt merely because code is Apache-licensed. Technical documentation, vulnerability handling, support period and reporting arrangements require accountable ownership. |
| AI-specific, sectoral and contractual obligations | Actual deployment/use of an AI system, high-impact decisions, regulated customers or geographic restrictions | Gateway does not execute model tools or assess end-use legality | **Not assessed:** identify the role and obligations for the actual system, including customer applications. Gateway tests do not certify model safety, lawful training data, explainability, human oversight or a regulated use case. |
| Open-source licensing | Redistribution of this software, dependencies, images and modified distributions | Repository Apache-2.0 license and dependency lock records | **Operator responsibility:** review notices, dependency/image licenses and applicable obligations. A dependency version list is not a license audit; no trademark or patent clearance is claimed. |

## Implemented controls and their boundaries

| Control family | Evidence | Boundary / accountable party |
|---|---|---|
| Authentication and authorization | [`gateway/auth.py`](../gateway/auth.py), [`gateway/security.py`](../gateway/security.py), [credential permissions](credential-permissions.md), [provisioning](provisioning.md) | OIDC and local optional MFA do not certify every IdP. Operator supplies correct trusted groups, MFA policy, offboarding and identity verification. |
| Secrets and storage | [`gateway/security.py`](../gateway/security.py), [mounted secrets](secret-files.md), [encryption operations](encryption-key-operations.md) | Provider/MFA secret encryption is not full-database encryption. Identity, accounting and explicit prompt templates remain sensitive. Operator owns KMS/secret custody, DB/backup encryption, TLS and access. |
| Tenant/member boundaries | [`gateway/account.py`](../gateway/account.py), [`gateway/member_usage.py`](../gateway/member_usage.py), [`gateway/teams.py`](../gateway/teams.py) | Application-level scope is not physical customer isolation. Privileged administrators and DB operators remain trusted. |
| Audit and monitoring | [`gateway/observability.py`](../gateway/observability.py), [audit export](audit-export.md), [offline seals](audit-sealing.md) | Audit records are mutable by privileged DB operators. A local HMAC seal authenticates a completed snapshot against an external key/anchor; it does not prove completeness, nonrepudiation or WORM retention. |
| Data transmission controls | [`gateway/providers.py`](../gateway/providers.py), [provider scope](providers.md), [guardrails](guardrails.md) | HTTPS/URL validation and heuristics are not data-residency guarantees or comprehensive DLP. Operator must constrain egress, approve providers/contracts/regions and assess provider retention. |
| Availability and recovery | [restore drill](restore-drill.md), [operations](operations.md), [release gates](security-readiness.md) | Synthetic drills do not establish production RTO/RPO, managed-database failover or a contractual SLA. |

## Deployment decision record

Before sensitive production use, the operator should record:

1. The publisher, distributor and service-operator roles; customer/data categories,
   jurisdictions, intended uses and applicable obligations, reviewed by appropriate
   legal/security owners. Do not put actual personal data in this record.
2. Approved providers/regions, identity provider, hosting/subprocessors, contracts,
   security responsibilities, retention/deletion and incident-notification owners.
3. Exact commit, built image digest, configuration baseline, external infrastructure
   controls, independent findings, acceptance tests and residual-risk decisions.
4. A tested data-rights and retention plan covering live DB, backups, exports,
   endpoint journals, user-saved datasets and upstream services. Until product
   workflows exist, do not promise automatic complete deletion or portability.
5. A vulnerability intake and triage contact, supported release policy, incident
   process, patch delivery and any legally required reporting process. A GitHub
   issue template or an AI-generated checklist does not establish these operations.

Use [security readiness](security-readiness.md) for technical release gates and
[the data inventory](data-inventory.md) for the actual data stores. Reassess after
changes to product functions, business model, contracts or deployment.

## Primary references

- [GDPR, Regulation (EU) 2016/679](https://eur-lex.europa.eu/eli/reg/2016/679/oj):
  in particular scope/roles, principles, rights, controller/processor duties,
  security and transfers. These are applicability inputs, not a product approval.
- [HHS HIPAA Security Rule](https://www.hhs.gov/hipaa/for-professionals/security/index.html)
  and [business associates](https://www.hhs.gov/hipaa/for-professionals/privacy/guidance/business-associates/index.html).
- [AICPA SOC suite](https://www.aicpa-cima.com/resources/landing/system-and-organization-controls-soc-suite-of-services)
  and [ISO/IEC 27001](https://www.iso.org/standard/27001).
- [NIST SP 800-218, SSDF 1.1](https://csrc.nist.gov/pubs/sp/800/218/final).
- [CRA, Regulation (EU) 2024/2847](https://eur-lex.europa.eu/eli/reg/2024/2847/oj).
  Its staged application dates and role definitions must be evaluated against the
  distributor's facts; this document does not make an exemption determination.

- [EDPB controller/processor responsibilities](https://www.edpb.europa.eu/sme/learn-the-basics/data-controller-or-data-processor_en)
  and [Commission CRA open-source guidance](https://digital-strategy.ec.europa.eu/en/policies/cra-open-source)
  provide role-specific guidance alongside the legislation.
