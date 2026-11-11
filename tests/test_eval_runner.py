import asyncio
import json

import httpx
import pytest

from gateway.eval_runner import RunSettings, load_cases, main, run


def settings(**changes):
    return RunSettings(**{"gateway_url": "https://gateway.example", "baseline": "baseline",
                          "candidate": "candidate", **changes})


async def test_dry_run_never_calls_network_or_requires_credential():
    class Never:
        def build_request(self, *args, **kwargs):
            pytest.fail("Dry-run must not access client")
    report = await run(settings(), [{"input": "private-input", "expected": "private-expected"}], client=Never())
    assert not report["executed"] and report["planned_requests"] == 2
    assert "private-" not in json.dumps(report)


async def test_paired_execution_regressions_redaction_and_concurrency():
    active, peak = 0, 0
    calls = []
    async def handler(request):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.005)
            body = json.loads(request.content)
            calls.append(body)
            assert request.headers["authorization"] == "Bearer private-key"
            assert request.url == "https://gateway.example/v1/chat/completions"
            assert body["stream"] is False and body["max_tokens"] == 128
            output = "private-expected" if body["model"] == "baseline" else "private-wrong"
            return httpx.Response(200, json={"choices": [{"message": {"content": output}}]})
        finally:
            active -= 1
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        report = await run(settings(concurrency=2), [{"input": "private-input", "expected": "private-expected"}] * 4,
                           execute=True, api_key="private-key", client=client)
    assert len(calls) == 8 and peak == 2
    assert report["regression_count"] == 4 and report["comparable_pairs"] == 4
    assert report["baseline"]["passed"] == 4 and report["candidate"]["passed"] == 0
    assert "private-" not in json.dumps(report)


@pytest.mark.parametrize("status", [302, 401, 429, 500])
async def test_http_errors_never_retry_or_expose_bodies(status):
    calls = 0
    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(status, json={"secret": "private-upstream-error"}, headers={"location": "https://elsewhere.example"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
        report = await run(settings(), [{"input": "secret", "expected": "yes"}], execute=True, api_key="key", client=client)
    assert calls == 2
    assert report["has_errors"] and report["comparable_pairs"] == 0
    assert "private-" not in json.dumps(report)


async def test_dropped_responses_never_replay_inference():
    calls = 0
    def handler(request):
        nonlocal calls
        calls += 1
        raise httpx.ReadError("private diagnostic potentially containing prompts")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        report = await run(settings(), [{"input": "secret", "expected": "yes"}], execute=True, api_key="key", client=client)
    assert calls == 2
    assert report["baseline"]["cases"][0]["error"] == "transport_outcome_unknown"
    assert "private" not in json.dumps(report)


@pytest.mark.parametrize("data", [b"not json", b"x" * 262145, b'{"choices":[]}'])
async def test_invalid_and_oversized_responses_are_bounded(data):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=data))) as client:
        report = await run(settings(), [{"input": "secret", "expected": "yes"}], execute=True, api_key="key", client=client)
    assert report["has_errors"] and report["candidate"]["errors"] == 1


async def test_wall_clock_timeout_bounds_trickling_or_stalled_upstream():
    async def handler(request):
        await asyncio.sleep(5)
        pytest.fail("Timeout must cancel wait without retry")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        report = await asyncio.wait_for(run(settings(timeout=1), [{"input": "secret", "expected": "yes"}],
            execute=True, api_key="key", client=client), 3)
    assert report["baseline"]["cases"][0]["error"] == "timeout_outcome_unknown"


@pytest.mark.parametrize("url", ["http://public.example", "https://user:secret@example.com", "https://gateway.example?secret=x"])
def test_gateway_destination_validation(url):
    with pytest.raises(ValueError):
        settings(gateway_url=url)


def test_dataset_file_limits_and_strict_shape(tmp_path):
    source = tmp_path / "cases.json"
    source.write_bytes(b"x" * 1_048_577)
    with pytest.raises(ValueError, match="1 MiB"):
        load_cases(source)
    source.write_text('[{"input":"x","expected":"y","hidden":"unexpected"}]')
    with pytest.raises(ValueError, match="only"):
        load_cases(source)


def test_cli_defaults_to_dry_run_without_key(tmp_path, monkeypatch, capsys):
    source = tmp_path / "cases.json"
    source.write_text('[{"input":"secret","expected":"yes"}]')
    monkeypatch.delenv("EVAL_GATEWAY_KEY", raising=False)
    monkeypatch.setattr("sys.argv", ["eval_runner", str(source), "--gateway", "https://gateway.example",
                                     "--baseline", "before", "--candidate", "after"])
    main()
    assert json.loads(capsys.readouterr().out)["executed"] is False


async def test_equally_bad_models_fail_default_quality_gate():
    def handler(_):
        return httpx.Response(200, json={"choices": [{"message": {"content": "wrong"}}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        report = await run(settings(), [{"input": "q", "expected": "correct"}], execute=True, api_key="key", client=client)
        assert report["regression_count"] == 0 and not report["gate"]["accepted"]
        opted_out = await run(settings(min_candidate_pass_rate=0), [{"input": "q", "expected": "correct"}],
                              execute=True, api_key="key", client=client)
        assert opted_out["gate"]["accepted"]


async def test_response_cleanup_errors_do_not_override_scored_outcome():
    class BrokenClose(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"choices":[{"message":{"content":"yes"}}]}'
        async def aclose(self):
            raise httpx.ReadError("private cleanup details")
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=BrokenClose()))) as client:
        report = await run(settings(), [{"input": "q", "expected": "yes"}], execute=True, api_key="key", client=client)
    # httpx may close on end-of-body inside iteration, making completion unknown;
    # either way no cleanup exception or raw diagnostic escapes.
    assert "private" not in json.dumps(report)
    assert report["executed"]


async def test_owned_client_teardown_error_sanitized(monkeypatch):
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(
        200, json={"choices": [{"message": {"content": "yes"}}]})))
    original_close = client.aclose
    async def broken_close():
        await original_close()
        raise RuntimeError("private teardown details")
    monkeypatch.setattr(client, "aclose", broken_close)
    monkeypatch.setattr("gateway.eval_runner.httpx.AsyncClient", lambda **kwargs: client)
    report = await run(settings(), [{"input": "q", "expected": "yes"}], execute=True, api_key="key")
    assert report["gate"]["accepted"]
    assert "private" not in json.dumps(report)


async def test_runner_through_real_gateway_enforces_accounting_acl_and_budgets(client, admin_headers):
    from gateway.main import app

    team_response = await client.post("/admin/api/teams", headers=admin_headers,
                                      json={"name": "eval-integration", "budget_usd": 1})
    assert team_response.status_code == 201
    team = team_response.json()
    user_response = await client.post("/admin/api/users", headers=admin_headers,
        json={"email": "eval@example.com", "team_id": team["id"], "budget_usd": 1})
    assert user_response.status_code == 201
    user = user_response.json()
    provider_response = await client.post("/admin/api/providers", headers=admin_headers, json={
        "name": "eval-mock", "kind": "openai", "base_url": "https://provider.example/v1", "api_key": "private-provider-key",
        "models": {alias: {"upstream_model": alias + "-upstream", "input_cost_per_million": 1,
                           "output_cost_per_million": 1} for alias in ("baseline", "candidate")}})
    assert provider_response.status_code == 201
    async def key(aliases):
        response = await client.post("/admin/api/keys", headers=admin_headers, json={
            "name": "eval-key", "user_id": user["id"], "permissions": ["inference"], "allowed_models": aliases})
        assert response.status_code == 201
        return response.json()["key"]

    credential = await key(["baseline", "candidate"])
    calls = []
    def provider(request):
        assert request.headers["authorization"] == "Bearer private-provider-key"
        calls.append(json.loads(request.content)["model"])
        return httpx.Response(200, json={"choices": [{"message": {"content": "yes"}}],
                                       "usage": {"prompt_tokens": 10, "completion_tokens": 1}})
    await app.state.upstream.aclose()
    app.state.upstream = httpx.AsyncClient(transport=httpx.MockTransport(provider))
    config = settings(gateway_url="http://localhost:8080")
    cases = [{"input": "private-test-input", "expected": "yes"}]
    # client is the shared FastAPI ASGITransport fixture, not a mocked gateway.
    report = await run(config, cases, execute=True, api_key=credential, client=client)
    assert report["gate"]["accepted"] and report["comparable_pairs"] == 1
    assert sorted(calls) == ["baseline-upstream", "candidate-upstream"]
    usage = (await client.get("/admin/api/usage", headers=admin_headers)).json()
    reservations = (await client.get("/admin/api/reservations", headers=admin_headers)).json()
    assert len(usage) == len(reservations) == 2
    assert {entry["model"] for entry in usage} == {"baseline", "candidate"}
    assert all(entry["cost_micros"] == 11 and entry["team_id"] == team["id"] for entry in usage)
    assert all(entry["status"] == "settled" for entry in reservations)
    assert "private-" not in json.dumps(report)

    restricted = await key(["baseline"])
    denied = await run(config, cases, execute=True, api_key=restricted, client=client)
    assert denied["candidate"]["cases"][0]["status"] == 403 and not denied["gate"]["accepted"]
    assert len(calls) == 3  # Forbidden model never reaches provider.
    await client.patch("/admin/api/teams/" + team["id"], headers=admin_headers, json={"budget_usd": 0})
    over_team = await run(config, cases, execute=True, api_key=credential, client=client)
    assert all(over_team[side]["cases"][0]["status"] == 429 for side in ("baseline", "candidate"))
    await client.patch("/admin/api/teams/" + team["id"], headers=admin_headers, json={"budget_usd": 1})
    await client.patch("/admin/api/users/" + user["id"], headers=admin_headers, json={"budget_usd": 0})
    over_user = await run(config, cases, execute=True, api_key=credential, client=client)
    assert all(over_user[side]["cases"][0]["status"] == 429 for side in ("baseline", "candidate"))
    assert len(calls) == 3  # Neither budget denial dispatched or retried inference.
