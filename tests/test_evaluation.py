import pytest

from gateway.evaluation import evaluate


def test_summary_and_no_prompt_retention():
    result = evaluate([
        {"input": "private prompt", "expected": "hello", "output": "hello"},
        {"expected": "hello", "output": "hello world"},
    ], ["exact_match", "contains", "nonempty"])
    assert result["case_count"] == 2
    assert result["passed"] == 1
    assert result["pass_rate"] == 0.5
    assert result["rules"]["contains"]["passed"] == 2
    assert "private prompt" not in str(result)
    assert "hello" not in str(result)


def test_json_validation_is_strict():
    result = evaluate([{ "output": s} for s in ['{"ok":true}', '[1,2]', 'NaN', 'Infinity', '{oops}']], ["json_valid"])
    assert result["passed"] == 2


@pytest.mark.parametrize("cases,rules", [
    ([], ["nonempty"]),
    ([{"output": "ok"}], ["regex"]),
    ([{"output": "ok"}], ["nonempty", "nonempty"]),
    ([{"output": "ok"}], ["exact_match"]),
    ([{"output": "ok", "expected": ""}], ["contains"]),
    ([{"output": "x" * 65537}], ["nonempty"]),
    ([{"output": 3}], ["nonempty"]),
    ([{"output": "ok"}] * 1001, ["nonempty"]),
    ([{"output": "x" * 65536}] * 31, ["nonempty"]),
])
def test_invalid_inputs_rejected(cases, rules):
    with pytest.raises(ValueError):
        evaluate(cases, rules)
