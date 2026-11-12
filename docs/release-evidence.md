# Internal release evidence

This ledger records verified repository increments, not an external security audit,
certification, production deployment, or capacity guarantee. Each listed run passed
all five jobs for its exact pull-request head before merge: Python/lint/dependency
audit, PostgreSQL/restore, disposable two-replica/browser acceptance, real disposable
Keycloak, and deployment checks. Tests use synthetic provider responses and secrets.

| PR | Tested head | CI run | Verified change |
| --- | --- | --- | --- |
| [24](https://github.com/radiatedsaturn7/automated-llm-proxy/pull/24) | `61bb77a079d5074de1db3d3c04c123d25ddafc2a` | [43143002632](https://github.com/radiatedsaturn7/automated-llm-proxy/actions/runs/43143002632) | Locked runtime/test dependencies and export drift checks |
| [25](https://github.com/radiatedsaturn7/automated-llm-proxy/pull/25) | `6bc01fdff6a58438d306db86e20749dd1cc8dad2` | [43144056054](https://github.com/radiatedsaturn7/automated-llm-proxy/actions/runs/43144056054) | Absolute body receive deadline and HTTP/1 rejection close |
| [26](https://github.com/radiatedsaturn7/automated-llm-proxy/pull/26) | `35829d3c5e72ffded6544ff83bbb5e0cdb7a6d5f` | [43146594749](https://github.com/radiatedsaturn7/automated-llm-proxy/actions/runs/43146594749) | Graceful shutdown accounting, CLI/Helm termination budgets and real signal tests |
| [27](https://github.com/radiatedsaturn7/automated-llm-proxy/pull/27) | `fbee2cd09699af53a450548d01462dd98ffbca6d` | [43148075005](https://github.com/radiatedsaturn7/automated-llm-proxy/actions/runs/43148075005) | Shared database admission/cleanup clock |
| [28](https://github.com/radiatedsaturn7/automated-llm-proxy/pull/28) | `e2d5a190d0308260145ad5f7d35079bbd4767fb7` | [43149797431](https://github.com/radiatedsaturn7/automated-llm-proxy/actions/runs/43149797431) | Consumed rate allowance survives key rotation |
| [29](https://github.com/radiatedsaturn7/automated-llm-proxy/pull/29) | `efe7273ac791ccc40f7fe7d79efd7146ff60f434` | [43150015190](https://github.com/radiatedsaturn7/automated-llm-proxy/actions/runs/43150015190) | Bounded application/operational HTTP admission and accounting backlog gate |
| [30](https://github.com/radiatedsaturn7/automated-llm-proxy/pull/30) | `75097a89b904c6b202abca729956f7f9ceae6214` | [43151332659](https://github.com/radiatedsaturn7/automated-llm-proxy/actions/runs/43151332659) | Overload alert/dashboard coverage and synthetic promtool rule evaluation |
| [31](https://github.com/radiatedsaturn7/automated-llm-proxy/pull/31) | `99394849021f81b8f0078e7f2d0491adefb572f7` | [43151672848](https://github.com/radiatedsaturn7/automated-llm-proxy/actions/runs/43151672848) | Strict inference JSON and predispatch accounting/circuit isolation |

## How to use this evidence

- Verify the deployed image digest and source revision. A passing earlier head does
  not validate subsequent code, a different dependency set, or an operator's config.
- Read job results, skipped checks and artifacts rather than inferring coverage from
  test filenames. Dependency inventories have bounded artifact retention; preserve
  required release evidence in the operator's controlled records before expiry.
- The two-replica drill includes graceful drain/deadline cancellation, SIGKILL
  uncertainty, survivor inference and master-key rotation. It does not simulate
  managed-database failover, ingress propagation or a multi-region partition.
- Synthetic provider/SDK contracts do not establish real-vendor billing equivalence,
  all model feature support, general evaluation quality, or performance SLOs.
- [Release gates](security-readiness.md#open-risks-and-priority-acceptance-work) still
  require deployment-specific testing, owners and explicit residual-risk decisions.
  No external review or production rollout is represented by this ledger.

Historical round-17 increment merged as `eaffbe45151a9eb9c260615d55eed2c25738c6bd`.
The corresponding integrated local run passed 935 tests with 8 skipped, including
rendered Helm checks. Local counts are not a substitute for the linked CI jobs.


## Subsequent verified build hardening

This is a selected ledger, not a claim that every intervening PR is inventoried.
Both increments below passed all five CI jobs plus the separate SAST, secret,
parser-property and IaC workflows for the listed exact heads before merging.

| PR | Tested head | CI run | Merge | Verified change |
| --- | --- | --- | --- | --- |
| [59](https://github.com/radiatedsaturn7/automated-llm-proxy/pull/59) | `d39a99040f32f7cb7c83434ff21f268b81bd44eb` | [43322178729](https://github.com/radiatedsaturn7/automated-llm-proxy/actions/runs/43322178729) | `bb95b442563e8de855aaff5642c789c587671f08` | Explicit container builder inputs, excluding unrelated local files from builder layers |
| [60](https://github.com/radiatedsaturn7/automated-llm-proxy/pull/60) | `52d51a627c6e860a67ddf4929a9689e93e3aff5c` | [43324186181](https://github.com/radiatedsaturn7/automated-llm-proxy/actions/runs/43324186181) | `290037e8a19fd8d0a6c6cd7730a38a4dcb6cfadc` | Hash-locked/audited builder-only backend; no implicit build-isolation dependency resolution |

**Unresolved image gate:** draft [PR45](https://github.com/radiatedsaturn7/automated-llm-proxy/pull/45)
remains a release blocker, not part of these successful controls.
[Run 43321013868](https://github.com/radiatedsaturn7/automated-llm-proxy/actions/runs/43321013868)
reported 59 High package matches across 17 distinct CVEs, zero Critical, against its
recorded merge/image identity before PRs 59/60. This is historical evidence, not a
scan of the newer merged image. No blanket exemption, clean-image claim,
external audit or certification follows from the green application checks.

## Dependency artifacts on unsuccessful runs

The Python job prepares source revision and lock copies before installation,
records installed packages before test gates, and attempts to upload available
evidence even after a failure. Its outcome manifest distinguishes `success`,
`failure`, `skipped` and cancellation states; a missing report is not a clean scan.
Runtime dependency auditing can still run after tests fail if installation
succeeded. It does not make the job pass, ignore audit errors or run after
cancellation. Build-audit JSON describes the separate build lock, not runtime OS
packages; runtime-audit JSON covers the installed Python test/runtime environment.

Artifacts are best effort: a killed runner, failed checkout/setup or interrupted
upload can prevent collection. Retention remains 14 days. Preserve release records
in the operator's controlled archive before expiry and record missing evidence.
The manifest includes only named gate outcomes, not arbitrary environment values.
No passwords, provider prompts/responses, or operator credentials should be
introduced into these disposable CI jobs.
