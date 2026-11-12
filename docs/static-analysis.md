# Python static security analysis

The `Security` workflow runs Bandit **1.9.4** in an isolated Python 3.12 job on
pull requests, main pushes, manual dispatch and weekly. Scanner dependencies are
pinned separately in `tools/security/requirements.txt`; they do not enter the
runtime image. Action revisions are pinned, repository permission is read-only,
and checkout credentials are not persisted. Fork PRs need no secrets. This is
not a `pull_request_target` workflow and never executes with provider credentials.

## Gate and evidence

All production Python under `gateway/` (including migrations and operator CLIs)
and Python security tooling under `tools/security/` are scanned. **Every
unsuppressed finding, including low severity/confidence, blocks the job.** Scan
errors, absent/invalid reports, and zero scanned lines also fail closed. There
are no excluded check IDs, severity filters or accepted baseline files.
Test fixtures are outside the scan scope: they intentionally contain dummy
credentials and assertions. JavaScript, Helm, Terraform, dependencies and runtime
configuration are **not** covered by Bandit. Existing tests and dependency audit
are complementary, not replacements.

Artifacts `security-evidence-<tested SHA>` retain JSON findings, an additional
unfiltered report including inline exceptions, exact scanner version, installed
scanner package inventory and tested SHA for 14 days. Download and retain release
evidence longer if your policy requires it. A failed scanner installation also
fails the job; no successful scan is claimed when evidence is absent.

A repository administrator must separately require the check **Python SAST
(Bandit)** in branch protection/rulesets. Adding this workflow does not establish
that protection. A clean result is not an independent audit, penetration test,
certification or proof of compliance.

## Reviewed exceptions

Exceptions must use an individual rule ID and a local rationale. Broad `nosec`
comments and global rule disables are prohibited. Changes to exceptions need
security-minded review; the unfiltered artifact makes them inspectable.

- `auth.py`, B105: OAuth `token_type: Bearer` is a protocol label, not a secret.
- `evaluation.py`, B105: token estimation description is public metadata, not a
  credential.
- `cli.py`, B104: central gateway listens on all container interfaces by design.
  Operators must enforce ingress restriction and TLS (or choose `--host
  127.0.0.1` for a host-only deployment). Local relay binding is separate.
- `eval_runner.py`, two B110 sites: best-effort **teardown only** after dispatch.
  Cleanup errors cannot overwrite a scored/unknown billable outcome, trigger an
  inference retry, or expose transport secrets through logs. Cancellation is not
  swallowed by these `Exception` handlers. Existing evaluator cleanup tests
  exercise transport failure and cancellation.

The initial scan at base `27d0c6b` reported six findings: these five intentional
cases and a relay assertion. The assertion was replaced with an explicit 502
invariant guard, retained under optimized Python. The final gated scan has zero
unsuppressed findings. This records triage, not a blanket baseline waiver.

The Azure workload-identity integration adds one B110 exception in
`azure_identity.py`: bounded credential teardown must not prevent subsequent
HTTP/DB shutdown or log SDK secrets. This brings the reviewed exceptions to six;
the original pre-Azure scan history above remains unchanged.

## Reproduce

```sh
python -m venv .venv-security
.venv-security/bin/pip install -r tools/security/requirements.txt
.venv-security/bin/python -m bandit -r gateway tools/security -f json -o bandit.json
python tools/security/check_report.py bandit.json
```

On a finding, inspect rule/CWE and data flow, repair the cause and add an
appropriate regression test. Only add a targeted exception after documenting
why the flagged behavior is safe. Review scanner upgrades deliberately and rerun
both the gate and unfiltered scan. Pinned dependencies improve repeatability;
they do not auto-update for newly disclosed scanner vulnerabilities.
