"""Opt-in billed paired evaluation through a gateway; dry-run and redacted reports by default."""
import argparse
import asyncio
from dataclasses import dataclass
import ipaddress
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit

import httpx

from .evaluation import SUPPORTED_RULES, evaluate

MAX_DATASET_BYTES = 1_048_576
MAX_RESPONSE_BYTES = 262144


@dataclass(frozen=True)
class RunSettings:
    gateway_url: str
    baseline: str
    candidate: str
    rules: tuple[str, ...] = ("exact_match",)
    max_tokens: int = 128
    concurrency: int = 2
    timeout: float = 60
    min_candidate_pass_rate: float = 1.0
    max_regressions: int = 0

    def __post_init__(self):
        url = urlsplit(self.gateway_url)
        try:
            local = url.hostname == "localhost" or ipaddress.ip_address(url.hostname or "").is_loopback
        except ValueError:
            local = False
        if (url.scheme not in {"https", "http"} or not url.hostname or url.username or url.password
                or url.query or url.fragment or (url.scheme == "http" and not local)):
            raise ValueError("Gateway URL requires HTTPS except loopback, without credentials/query/fragment")
        if not all(isinstance(alias, str) and 1 <= len(alias) <= 128 for alias in (self.baseline, self.candidate)):
            raise ValueError("Model aliases must be 1 to 128 characters")
        if not 1 <= self.max_tokens <= 4096 or not 1 <= self.concurrency <= 8 or not 1 <= self.timeout <= 300:
            raise ValueError("Token, concurrency or timeout bounds invalid")
        if not 0 <= self.min_candidate_pass_rate <= 1 or type(self.max_regressions) is not int or not 0 <= self.max_regressions <= 100:
            raise ValueError("Gate pass rate must be 0..1 and max regressions 0..100")
        if not self.rules or len(set(self.rules)) != len(self.rules) or not set(self.rules) <= SUPPORTED_RULES:
            raise ValueError("Choose unique supported deterministic rules")


def load_cases(path):
    with Path(path).open("rb") as source:
        data = source.read(MAX_DATASET_BYTES + 1)
    if len(data) > MAX_DATASET_BYTES:
        raise ValueError("Dataset exceeds 1 MiB")
    try:
        cases = json.loads(data)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise ValueError("Dataset must be valid JSON") from None
    return validate_cases(cases)


def validate_cases(cases):
    if not isinstance(cases, list) or not 1 <= len(cases) <= 100:
        raise ValueError("Dataset must contain 1 to 100 cases")
    total = 0
    for case in cases:
        if not isinstance(case, dict) or set(case) != {"input", "expected"}:
            raise ValueError("Each case must contain only input and expected")
        if any(not isinstance(case[field], str) or len(case[field]) > 8192 for field in ("input", "expected")):
            raise ValueError("Case fields must be strings of at most 8192 characters")
        total += len(json.dumps(case, ensure_ascii=False).encode())
    if total > MAX_DATASET_BYTES:
        raise ValueError("Dataset exceeds 1 MiB")
    return cases


async def run(settings, cases, *, execute=False, api_key=None, client=None):
    cases = validate_cases(cases)
    # Check evaluator preconditions (e.g. contains requires nonempty expected)
    # before any billed requests, without retaining these dummy results.
    evaluate([{"expected": case["expected"], "output": ""} for case in cases], settings.rules)
    plan = {"executed": False, "case_count": len(cases), "baseline_model": settings.baseline,
            "candidate_model": settings.candidate, "planned_requests": len(cases) * 2,
            "max_output_tokens_per_request": settings.max_tokens, "concurrency": settings.concurrency,
            "rules": list(settings.rules), "gate_policy": {"min_candidate_pass_rate": settings.min_candidate_pass_rate,
                "max_regressions": settings.max_regressions}, "warning": "Execution may incur provider charges for both models; gateway budgets apply."}
    if not execute:
        return plan  # No client constructed, credential consumed or network call made.
    if not isinstance(api_key, str) or not api_key or len(api_key) > 8192 or any(ord(c) < 33 or ord(c) > 126 for c in api_key):
        raise ValueError("Execution requires an inference-scoped gateway API credential")
    owned = client is None
    client = client or httpx.AsyncClient(timeout=httpx.Timeout(settings.timeout, connect=min(settings.timeout, 10)),
        limits=httpx.Limits(max_connections=settings.concurrency, max_keepalive_connections=settings.concurrency),
        follow_redirects=False, trust_env=False)
    semaphore = asyncio.Semaphore(settings.concurrency)

    async def infer(index, case, alias):
        # Output text is retained in memory only until its deterministic score is
        # computed. Never include upstream bodies/errors in returned diagnostics.
        async with semaphore:
            response = None
            try:
                payload = {"model": alias, "messages": [{"role": "user", "content": case["input"]}],
                           "max_tokens": settings.max_tokens, "stream": False}
                # Wall-clock deadline also bounds slowly trickling responses.
                async with asyncio.timeout(settings.timeout):
                    request = client.build_request("POST", settings.gateway_url.rstrip("/") + "/v1/chat/completions",
                        headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"}, json=payload)
                    response = await client.send(request, stream=True, follow_redirects=False)
                    if response.status_code != 200:
                        return {"index": index, "error": "gateway_http_error", "status": response.status_code}
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(content) + len(chunk) > MAX_RESPONSE_BYTES:
                            return {"index": index, "error": "response_size_limit"}
                        content.extend(chunk)
                    data = json.loads(content)
                    output = data["choices"][0]["message"]["content"]
                    if not isinstance(output, str) or len(output) > 65536:
                        return {"index": index, "error": "invalid_model_response"}
                    score = evaluate([{"expected": case["expected"], "output": output}], settings.rules)["results"][0]
                    return {**score, "index": index}
            except (TimeoutError, httpx.TimeoutException):
                return {"index": index, "error": "timeout_outcome_unknown"}
            except httpx.HTTPError:
                return {"index": index, "error": "transport_outcome_unknown"}
            except (ValueError, TypeError, KeyError, IndexError, RecursionError):
                return {"index": index, "error": "invalid_model_response"}
            finally:
                if response is not None:
                    try:
                        await asyncio.wait_for(response.aclose(), min(settings.timeout, 2))
                    except Exception:  # nosec B110 # Best-effort teardown must preserve dispatch accounting; never log transport secrets.
                        pass  # Cleanup cannot override a scored or unknown dispatch outcome.

    try:
        jobs = [infer(index, case, alias) for alias in (settings.baseline, settings.candidate)
                for index, case in enumerate(cases)]
        results = await asyncio.gather(*jobs)
    finally:
        if owned:
            try:
                await asyncio.wait_for(client.aclose(), min(settings.timeout, 2))
            except Exception:  # nosec B110 # Best-effort teardown only; no retry of potentially billed inference.
                pass  # Never emit transport teardown diagnostics or retry inference.
    baseline, candidate = results[:len(cases)], results[len(cases):]
    regressions = []
    comparable = 0
    for before, after in zip(baseline, candidate, strict=True):
        if "error" in before or "error" in after:
            continue
        comparable += 1
        worsened = [rule for rule in settings.rules if before["checks"][rule] and not after["checks"][rule]]
        if worsened:
            regressions.append({"index": before["index"], "rules": worsened})

    def summarize(rows):
        valid = [row for row in rows if "error" not in row]
        return {"completed": len(valid), "errors": len(rows) - len(valid), "passed": sum(row["passed"] for row in valid),
                "pass_rate_completed": sum(row["passed"] for row in valid) / len(valid) if valid else None, "cases": rows}
    candidate_pass_rate = sum(row.get("passed", False) for row in candidate) / len(cases)
    accepted = (comparable == len(cases) and candidate_pass_rate >= settings.min_candidate_pass_rate
                and len(regressions) <= settings.max_regressions)
    return {**plan, "executed": True, "gate": {"accepted": accepted, "candidate_pass_rate_all_cases": candidate_pass_rate}, "baseline": summarize(baseline), "candidate": summarize(candidate),
            "comparable_pairs": comparable, "regressions": regressions, "regression_count": len(regressions),
            "has_errors": comparable != len(cases), "content_retained": False,
            "note": "Scores test deterministic output checks, not factuality or semantic quality. No ambiguous inference retries."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", help="Local JSON list of input/expected cases (max 1 MiB)")
    parser.add_argument("--gateway", required=True, help="Central gateway base URL, without /v1 suffix")
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--rules", nargs="+", default=["exact_match"], choices=sorted(SUPPORTED_RULES))
    parser.add_argument("--max-tokens", type=int, default=128)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument("--min-candidate-pass-rate", type=float, default=1.0)
    parser.add_argument("--max-regressions", type=int, default=0)
    parser.add_argument("--execute", action="store_true", help="Authorize billed provider requests through the gateway")
    args = parser.parse_args()
    try:
        settings = RunSettings(args.gateway, args.baseline, args.candidate, tuple(args.rules), args.max_tokens,
                               args.concurrency, args.timeout, args.min_candidate_pass_rate, args.max_regressions)
        cases = load_cases(args.dataset)
        if args.execute:
            print("WARNING: executing up to two billed model requests per case; gateway budgets apply.", file=sys.stderr)
        report = asyncio.run(run(settings, cases, execute=args.execute, api_key=os.environ.get("EVAL_GATEWAY_KEY")))
    except (ValueError, OSError):
        parser.exit(2, "Evaluation configuration or dataset invalid; no report generated.\n")
    print(json.dumps(report, separators=(",", ":")))
    if report.get("executed") and not report["gate"]["accepted"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
