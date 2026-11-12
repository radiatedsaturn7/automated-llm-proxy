# CI supply-chain update policy

Remote GitHub Actions are pinned to complete 40-character upstream Git commits,
with human-readable release tags alongside. The existing `ci.yml` moving tags
were resolved against official upstream Git repositories on 2026-11-12, retaining
their current major release families:

| Action | Release | Commit |
| --- | --- | --- |
| actions/checkout | v4.4.0 | 11d5960a326750d5838078e36cf38b85af677262 |
| actions/setup-python | v5.6.0 | a26af69be951a213d495a4c3e4e4022e16d87065 |
| actions/upload-artifact | v4.6.2 | ea165f8d65b6e75b540449e92b4886f43607fa02 |
| actions/setup-node | v4.4.0 | 49933ea5288caeca8642d1e84afbd3f7d6820020 |
| azure/setup-helm | v4.3.0 | b9e51907a09c216f16ebe8536097933489208112 |
| hashicorp/setup-terraform | v3.1.2 | b9cd54a3c349d3f38e8881555d616ced269862dd |

Sibling security workflows already use immutable pins and are unchanged.
Every checkout disables persisted Git credentials; jobs do not need Git writes.
Existing read-only workflow permissions and test behavior remain unchanged.
UV 0.13.6, Helm 3.17.3 and Terraform 1.10.5 stay explicit tool releases.
Python 3.12 and Node 22 remain maintained runtime-series selectors, not claims of
bit-for-bit environment reproducibility. CI PostgreSQL 16-alpine and Prometheus v3.2.1 are pinned to verified Docker Hub
multi-platform manifest digests (including linux/amd64); response content hashes
were checked against registry digest headers. Hosted runners and deployment
images still need their own lifecycle controls; action pinning does not cover them.
The CI service image pins are intentionally reviewed separately from root
Dockerfile updates; check them periodically for security fixes.

Dependabot proposes bounded weekly update PRs for actions, the uv lockfile,
scanner requirements, browser-test npm dependencies, Docker base images and
Terraform. It does not enable auto-merge. Review release notes, pin provenance,
lockfile changes and all tests before merging. For uv runtime changes, regenerate
`requirements.lock` with the existing CI export procedure in the same PR: a
stale export is deliberately rejected. Bot support for an ecosystem is not proof
an update was opened; check the repository's Dependabot update logs.

When updating a GitHub Action, verify the release commit in its official upstream
repository, retain full SHA syntax and rerun CI. `tests/test_ci_action_pins.py`
rejects new mutable action references and persisted checkout credentials. Pins
reduce tag-retargeting risk; they are not proof the upstream code is harmless or
a security/compliance certification.
