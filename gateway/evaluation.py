"""Bounded, deterministic evaluation of already-collected outputs.

This module never calls models, executes supplied code, or retains prompt text.
"""
from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence

SUPPORTED_RULES = frozenset({"exact_match", "contains", "json_valid", "nonempty"})
MAX_CASES = 1000
MAX_FIELD_CHARS = 65536
MAX_TOTAL_CHARS = 2_000_000


def evaluate(cases: Sequence[Mapping], rules: Sequence[str] = ("exact_match",)) -> dict:
    if isinstance(cases, (str, bytes)) or not isinstance(cases, Sequence) or not 1 <= len(cases) <= MAX_CASES:
        raise ValueError("cases must contain 1 to 1000 objects")
    if isinstance(rules, (str, bytes)) or not isinstance(rules, Sequence) or not 1 <= len(rules) <= len(SUPPORTED_RULES):
        raise ValueError("rules must contain 1 to 4 unique supported names")
    if any(not isinstance(rule, str) or rule not in SUPPORTED_RULES for rule in rules) or len(set(rules)) != len(rules):
        raise ValueError("Unknown or duplicate evaluation rule")
    total_chars = 0
    checked: list[tuple[str, str | None]] = []
    for case in cases:
        if not isinstance(case, Mapping):
            raise ValueError("Each case must be an object")
        output = case.get("output")
        expected = case.get("expected")
        for name in ("input", "expected", "output"):
            value = case.get(name)
            if value is not None:
                if not isinstance(value, str) or len(value) > MAX_FIELD_CHARS:
                    raise ValueError("Case fields must be strings of at most 65536 characters")
                total_chars += len(value)
        if total_chars > MAX_TOTAL_CHARS:
            raise ValueError("Evaluation text exceeds the total limit")
        if not isinstance(output, str):
            raise ValueError("Every case needs a string output")
        if {"exact_match", "contains"}.intersection(rules) and not isinstance(expected, str):
            raise ValueError("Comparison rules require a string expected value")
        if "contains" in rules and not expected:
            raise ValueError("contains requires a nonempty expected value")
        checked.append((output, expected))

    results = []
    counts = dict.fromkeys(rules, 0)
    for index, (output, expected) in enumerate(checked):
        scores: dict[str, bool] = {}
        for rule in rules:
            if rule == "exact_match":
                passed = output == expected
            elif rule == "contains":
                passed = expected in output  # type: ignore[operator]
            elif rule == "nonempty":
                passed = bool(output.strip())
            else:
                try:
                    # Python normally accepts NaN/Infinity; reject them as non-standard JSON.
                    json.loads(output, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Non-finite JSON")))
                    passed = True
                except (ValueError, RecursionError):
                    passed = False
            scores[rule] = passed
            counts[rule] += int(passed)
        results.append({
            "index": index,
            "checks": scores,
            "passed": all(scores.values()),
            "output_characters": len(output),
            "approximate_tokens": math.ceil(len(output) / 4),
        })
    return {
        "case_count": len(results),
        "passed": sum(row["passed"] for row in results),
        "pass_rate": sum(row["passed"] for row in results) / len(results),
        "rules": {rule: {"passed": count, "pass_rate": count / len(results)} for rule, count in counts.items()},
        "results": results,
        "token_estimate_method": "ceil(unicode_characters / 4); not a tokenizer or billing estimate",
    }
