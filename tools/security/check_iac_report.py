"""Reject missing coverage, findings, skips and parser errors in Checkov Kubernetes JSON."""
import json
import sys
from pathlib import Path


def report_ok(report):
    if not isinstance(report, dict) or report.get('check_type') != 'kubernetes':
        return False
    summary = report.get('summary')
    results = report.get('results')
    if not isinstance(summary, dict) or not isinstance(results, dict):
        return False
    return (
        all(summary.get(key) == 0 for key in ('failed', 'skipped', 'parsing_errors'))
        and type(summary.get('passed')) is int and summary['passed'] > 0
        and type(summary.get('resource_count')) is int and summary['resource_count'] >= 17
        and all(results.get(key) == [] for key in ('failed_checks', 'skipped_checks', 'parsing_errors'))
        and isinstance(results.get('passed_checks'), list) and bool(results['passed_checks'])
    )


def main():
    try:
        passed = report_ok(json.loads(Path(sys.argv[1]).read_text()))
    except (OSError, ValueError, IndexError):
        passed = False
    if not passed:
        print('IaC gate failed: findings, skips, errors or insufficient Kubernetes coverage.')
        return 1
    print('IaC gate passed for rendered security-profile fixtures only.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
