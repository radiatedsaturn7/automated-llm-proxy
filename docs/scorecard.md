# OpenSSF Scorecard

[OpenSSF Scorecard](https://github.com/ossf/scorecard) checks observable
open-source project security practices. This repository runs its official action
on trusted `main` pushes and weekly, stores SARIF evidence in the workflow's
artifacts, and uploads findings to GitHub code scanning. Public repository
results are also published to the [Scorecard API](https://api.scorecard.dev).

The workflow uses GitHub's short-lived default token, not a personal access
token. Job-scoped `security-events: write` permits SARIF upload; `id-token: write`
authenticates publication to Scorecard. It does not grant cloud-provider access.
No repository scripts execute in that privileged job. Action references are
commit-pinned; the official Scorecard action itself invokes its vendor's
versioned container image. Pins do not eliminate upstream supply-chain risk.

## Read the evidence, not just the number

- A completed workflow is not a passing security assessment. Review individual
  findings, unknown/unavailable checks, and the analyzed commit/date.
- A score is not a compliance certification, an independent audit, a popularity
  measure, or evidence that application logic is secure.
- Scorecard observes repository settings/history as well as files. Genuine
  reviewer participation, rulesets, release practices and security policy cannot
  be replaced with cosmetic README claims or fabricated activity.
- Keep real risks in the remediation backlog. Do not weaken tests or conceal
  findings merely to improve a number.
- SARIF artifacts expire after 90 days. Operators needing longer audit evidence
  must archive them with the commit, workflow run URL and retention policy.

## Publication and badge

The pinned v2.5.0 action can finish analysis even if its public result upload
fails. Inspect its publication logs and check the API's analyzed commit before
claiming the public badge is current:

```sh
curl --fail --silent --show-error \
  https://api.scorecard.dev/projects/github.com/radiatedsaturn7/automated-llm-proxy
```

Only add a numeric badge after public results actually exist. If public
publication is delayed or unavailable, the workflow artifact and GitHub findings
remain the evidence; do not invent a score. The badge may lag the repository.

## Repository-owner setup

Use a GitHub repository ruleset targeting `main` with pull requests, required
CI checks, conversation resolution, and protection from force pushes/deletion.
The standard GitHub token can observe repository rulesets. Classic branch
protection details may be unavailable without extra privileges; don't grant a
broad administrator PAT just to improve visibility or the score.

For a solo maintainer, requiring an independent approving reviewer can block all
merges. Establish a real second reviewer before imposing that requirement; record
the resulting review gap honestly in the meantime. Do not select this scheduled
main-only workflow as a required PR check: it does not run on pull requests.

GitHub code scanning must be available for the repository. This public project
uses the free public-repository path; no paid plan, external cloud deployment,
or private customer data is needed. If GitHub or the Scorecard API rejects an
upload, preserve the artifact and diagnose the reported permission/policy issue.

Sources: [official action](https://github.com/ossf/scorecard-action),
[checks](https://github.com/ossf/scorecard/blob/main/docs/checks.md).
