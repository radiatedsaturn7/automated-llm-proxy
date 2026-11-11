# Offline prompt/output evaluations

`gateway.evaluation.evaluate(cases, rules)` scores outputs you already collected. It never calls an LLM, runs supplied code or regular expressions, or persists prompt/output text. API access control and audit metadata are handled by the gateway endpoint; the pure function returns only indexes, boolean scores and character/token-estimate counts.

```python
from gateway.evaluation import evaluate
report = evaluate(
    [{"input": "Answer yes", "expected": "yes", "output": "yes"}],
    ["exact_match", "contains", "nonempty"],
)
```

Rules:
- `exact_match`: case-sensitive exact string equality with `expected`.
- `contains`: case-sensitive substring check; `expected` must be nonempty.
- `json_valid`: valid JSON, rejecting nonstandard NaN/Infinity.
- `nonempty`: output contains non-whitespace characters.

Limits: 1–1000 cases, each supplied input/expected/output string at most 65,536 characters, total at most 2,000,000 characters, 1–4 distinct rules. Invalid inputs raise `ValueError`. The `approximate_tokens` metric is `ceil(character_count / 4)`, not a tokenizer, quality score or billing estimate.

These checks help regression-test formatting and known answers. They do not establish factual accuracy, safety, prompt-injection resistance or enterprise readiness. Model judges, datasets/versioning, red-team suites and online experiments are future integrations, not implemented features.

## Paired regression gates

`POST /admin/api/evaluation-comparisons` accepts up to 100 pairs of `baseline`
and `candidate` outputs sharing an `expected` value, deterministic `rules`,
`minimum_pass_rate` (0..1, default 1), and `maximum_regressions` (default 0).
It returns both reports, improved/regressed case indices, pass-rate delta and
an explicit `gate.accepted` boolean. HTTP 200 means evaluation ran, not that
its gate passed: automation must inspect that boolean. Cases must be aligned
by the caller. No model calls are made, outputs are not retained, and audit
records contain only counts, rule names and gate outcome. These checks are
not statistical significance tests, semantic grading or safety certification.
