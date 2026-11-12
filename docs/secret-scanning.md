# Secret scanning

The **Secret scan / gitleaks** GitHub Actions job runs the official Gitleaks
8.30.1 CLI, with the Linux x64 release archive pinned to its SHA-256 digest.
It runs on pull requests, main pushes, and manual dispatch. The checkout fetches
complete history, and the scanner uses `--all` over fetched refs, not only the PR
diff. This covers deleted secrets in fetched commit history; it does not claim to
scan inaccessible refs, uncommitted developer files, runtime secrets or external
systems.

The bundled default rule set is enabled via `.gitleaks.toml`. No test-directory,
file-path or whole-rule exclusion exists. Inline `gitleaks:allow` annotations are
ignored by CI. A synthetic detection/redaction self-test runs before scanning.
Logs and the temporary JSON report use `--redact=100`; the report is intentionally
not uploaded as a publicly downloadable artifact. A finding fails the job.

## One historical synthetic exception

`.gitleaksignore` contains exactly one historical finding fingerprint:
`2f7b8fa3e7635e18b0b284c5aec565968f7a5097:tests/test_guardrails.py:private-key:25`.
That test deliberately passes an invalid PEM-looking value with body `abc` to
verify guardrail detection. It is not a usable key. Pinning this exact finding
keeps future keys and future commits subject to scanning. Do not replace this
with a blanket tests/ path, private-key rule, or commit exclusion.

## Responding to a real finding

Treat a real exposed credential as compromised: revoke/rotate it through the
owning service, then remove the value and follow that service's incident process.
Removing a file alone does not remove the credential from Git history. Do not
add real credentials to the ignore file or paste raw findings into public issues.
Secret scanning is defense-in-depth, not a security audit or proof that no secrets
exist. Updates to the CLI must update and verify the archive digest and rerun the
self-test. There is no paid scanner service or token required.

Run locally with the verified CLI on PATH:

```sh
python tests/security/check-gitleaks.py
gitleaks git . --config=.gitleaks.toml --gitleaks-ignore-path=.gitleaksignore \
  --ignore-gitleaks-allow --redact=100 --no-banner --no-color --log-opts="--all"
```
