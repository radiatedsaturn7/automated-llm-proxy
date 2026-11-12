"""Fail closed on findings OR an incomplete Bandit JSON report (including parse errors)."""
import json
import sys
from pathlib import Path


def report_ok(report):
    if not isinstance(report, dict):
        return False
    metrics = report.get("metrics")
    if not isinstance(metrics, dict):
        return False
    totals = metrics.get("_totals")
    if not isinstance(totals, dict):
        return False
    return (
        report.get("errors") == []
        and report.get("results") == []
        and isinstance(totals.get("loc"), int)
        and totals["loc"] > 0
    )


def main():
    try:
        report = json.loads(Path(sys.argv[1]).read_text())
        passed = report_ok(report)
    except (OSError, ValueError, TypeError, AttributeError, IndexError):
        passed = False
    if not passed:
        print("SAST blocked: findings, scan errors, or missing/invalid coverage. Review the JSON artifact.")
        return 1
    print("SAST passed: no unsuppressed findings or scan errors.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
