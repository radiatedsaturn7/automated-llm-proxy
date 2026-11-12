"""Regression checks for fail-closed SAST reporting, without requiring scanner dependencies."""
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("check_report", ROOT / "tools/security/check_report.py")
report_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report_module)


def clean():
    return {"errors": [], "results": [], "metrics": {"_totals": {"loc": 10}}}


def test_clean_report():
    assert report_module.report_ok(clean())


@pytest.mark.parametrize("field,value", [
    ("errors", [{"reason": "syntax error"}]), ("results", [{"issue_severity": "LOW"}]),
    ("metrics", None), ("metrics", {"_totals": []}), ("metrics", {}), ("metrics", {"_totals": {"loc": 0}}),
])
def test_incomplete_or_flagged_report_fails(field, value):
    report = clean()
    report[field] = value
    assert not report_module.report_ok(report)


@pytest.mark.parametrize("value", [None, [], {}, {"results": []}])
def test_missing_report_fields_fail(value):
    assert not report_module.report_ok(value)


def test_no_broad_or_unexplained_suppressions():
    import re
    import tokenize
    for root in (ROOT / "gateway", ROOT / "tools/security"):
        for path in root.rglob("*.py"):
            with tokenize.open(path) as source:
                for token in tokenize.generate_tokens(source.readline):
                    if token.type == tokenize.COMMENT and re.search(r"#\s*nosec", token.string):
                        assert re.fullmatch(r"# nosec B\d{3}(?:,B\d{3})* # \S.+", token.string), path


@pytest.mark.parametrize("content", ["not json", "null", "{}"])
def test_cli_rejects_invalid_report(tmp_path, monkeypatch, content):
    report = tmp_path / "report.json"
    report.write_text(content)
    monkeypatch.setattr("sys.argv", ["check_report", str(report)])
    assert report_module.main() == 1


def test_cli_rejects_missing_report(tmp_path, monkeypatch):
    monkeypatch.setattr("sys.argv", ["check_report", str(tmp_path / "missing.json")])
    assert report_module.main() == 1
