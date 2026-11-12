"""Offline scanner regression using synthetic nonfunctional material only."""
import json
import subprocess
import tempfile
from pathlib import Path


def run(*args, cwd):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=True)


with tempfile.TemporaryDirectory(prefix="gitleaks-selftest-") as directory:
    root = Path(directory)
    run("git", "init", cwd=root)
    run("git", "config", "user.email", "scanner-test@example.invalid", cwd=root)
    run("git", "config", "user.name", "Scanner regression", cwd=root)
    # Invalid, nonfunctional PEM payload. Assemble markers to avoid matching
    # this self-test's own source file during the repository scan.
    marker = "PRIVATE" + " KEY"
    synthetic = "-----BEGIN " + marker + "-----\n" + "A" * 96 + "\n-----END " + marker + "-----\n"
    (root / "synthetic.pem").write_text(synthetic)
    run("git", "add", "synthetic.pem", cwd=root)
    run("git", "commit", "-m", "Synthetic scanner regression", cwd=root)
    report = root / "report.json"
    result = subprocess.run(["gitleaks", "git", ".", "--redact=100", "--no-banner", "--no-color",
                             "--report-format=json", f"--report-path={report}"],
                            cwd=root, text=True, capture_output=True)
    assert result.returncode == 1, "Expected synthetic secret detection"
    findings = json.loads(report.read_text())
    assert any(item["RuleID"] == "private-key" for item in findings), "Private-key rule did not fire"
    assert "A" * 96 not in report.read_text() + result.stdout + result.stderr, "Scanner leaked fixture value"
print("Gitleaks synthetic detection and full redaction verified")
