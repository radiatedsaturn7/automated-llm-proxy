# Paired gateway evaluation runner

`python -m gateway.eval_runner` compares **real model outputs** from baseline and
candidate gateway aliases against a small explicit dataset. It is dry-run by
default. `--execute` authorizes provider requests that may incur charges for both
models. The gateway's normal model allowlists, user/team budgets, rate limits and
auditing apply. No provider-direct access or paid judge model is used.

Prepare a local JSON dataset, for example:

```json
[{"input":"Reply with exactly yes.","expected":"yes"}]
```

Preview without credentials or network traffic:

```sh
python -m gateway.eval_runner cases.json \
  --gateway https://gateway.example \
  --baseline stable-model --candidate new-model \
  --max-tokens 128 --concurrency 2 --rules exact_match nonempty
```

To execute, inject an **inference-scoped gateway key** through `EVAL_GATEWAY_KEY`
using your secret manager, then repeat the command with `--execute`. Do not put
real secrets in command-line arguments, shell history or dataset files. The runner
prints a billing warning to stderr and a JSON report to stdout. Redirect stdout to
an approved destination if you want to keep the report; nothing is saved by default.
Do not set `--gateway` to a provider endpoint: use your own gateway base URL, without
`/v1`. HTTPS is required except for loopback development. Request redirects and
environment proxy inheritance are disabled.

## Execution and limits

Every case makes one nonstreaming chat request to each alias, using its `input` as
one user message. Default maximum output is 128 tokens per request; allowed range
is 1–4096. Concurrency defaults to 2 (maximum 8). Per-request wall-clock timeout
includes connecting, response headers and response body, defaults to 60 seconds
(range 1–300). Connection cleanup has an additional at-most-two-second best-effort deadline and
never triggers a retry or exposes raw teardown errors. Responses are limited to 256 KiB and text content to 65,536 characters.
Datasets are at most 1 MiB and 100 cases, with exactly `input`/`expected` fields each
at most 8,192 characters. There are no tools, images, streaming, automatic prompt
registry fetches, scheduling or arbitrary executable templates in this runner.

**No inference is automatically retried**, including 401, 429, timeouts and 5xx.
Timeouts and transport failures are reported as unknown outcomes because a provider
may already have billed the request. Error bodies are discarded. A failed baseline
case does not suppress its candidate request, so the planned count remains two
requests per case. Maximum output tokens are not a dollar spending limit or a
bound on input/context billing. Set gateway budgets before executing.

## Report and regression semantics

Reports contain aliases, case indexes, deterministic rule scores, output lengths,
approximate token counts, completed/error totals and regression indexes/rule names.
They contain **no case input, expected text, generated output, credential or raw
upstream error body**. Text is held in process memory only long enough to score it;
the original user-created dataset file remains on disk. Sending the dataset to the
gateway/provider is an explicit data transmission when `--execute` is used.

A regression is a rule that passed for baseline but failed for candidate on the
same case. Pairs with a request/error are marked noncomparable, never silently
counted as improved. Pass rates are labeled as rates among completed cases. The explicit `gate.accepted` result also requires candidate pass rate across all
cases to meet `--min-candidate-pass-rate` (default 1.0) and regressed cases not to
exceed `--max-regressions` (default 0). Any request error rejects the gate regardless
of these thresholds. Two equally bad models therefore do not pass by default.
Exit status is 0 for a dry-run or accepted gate, 1 for a rejected gate, and 2 for
invalid configuration/data. Lowering thresholds explicitly relaxes your quality
policy; a permissive threshold is not evidence of model quality.

Checks (`exact_match`, `contains`, `json_valid`, `nonempty`) are deterministic;
model generation is not guaranteed deterministic. Repeated executions may differ.
These scores do not measure factuality, safety or semantic quality, and a clean
run is not enterprise-readiness certification. Reports are local output and are
not automatically posted into the prompt registry's evaluation history. Tests use
only MockTransport and never call paid providers.
