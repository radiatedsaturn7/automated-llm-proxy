import json
from types import SimpleNamespace

import httpx
import pytest

from gateway.config import get_settings
from gateway.guardrails import postflight, GuardrailRejected
from test_gateway import setup_inference, mock_upstream

SECRET = "sk-" + "a" * 32


def cfg(action="block", **changes):
    values = dict(
        guardrails_output_enabled=True,
        guardrails_output_action=action,
        guardrails_detectors='["secrets","email"]',
        guardrails_max_characters=262144,
    )
    values.update(changes)
    return SimpleNamespace(**values)


def output(text="hello"):
    return {
        "id": "c1",
        "object": "chat.completion",
        "model": "chat",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


def test_redaction_and_metadata_unchanged():
    original = output(SECRET)
    result = postflight(original, cfg("redact"))
    assert result.payload["choices"][0]["message"]["content"] == "[REDACTED]"
    assert original["choices"][0]["message"]["content"] == SECRET
    assert result.payload["usage"] == original["usage"]
    original["id"] = SECRET
    with pytest.raises(GuardrailRejected):
        postflight(original, cfg("redact"))


def test_refusal_and_tools_redacted_safely():
    original = output(None)
    message = original["choices"][0]["message"]
    message["refusal"] = SECRET
    message["tool_calls"] = [
        {
            "id": "call1",
            "type": "function",
            "function": {"name": "fn", "arguments": json.dumps({"to": "person@example.test"})},
        }
    ]
    result = postflight(original, cfg("redact")).payload["choices"][0]["message"]
    assert result["refusal"] == "[REDACTED]"
    assert json.loads(result["tool_calls"][0]["function"]["arguments"]) == {"to": "[REDACTED]"}
    message["tool_calls"][0]["function"]["arguments"] = '{"broken":'
    with pytest.raises(GuardrailRejected):
        postflight(original, cfg("redact"))


@pytest.mark.parametrize("change", ["audio", "unknown", "logprobs", "multimodal", "limit", "tool_type"])
def test_fail_closed_unsupported(change):
    data = output()
    if change == "audio":
        data["choices"][0]["message"]["audio"] = {"data": "abc"}
    if change == "unknown":
        data["extra"] = "abc"
    if change == "logprobs":
        data["choices"][0]["logprobs"] = {"content": []}
    if change == "multimodal":
        data["choices"][0]["message"]["content"] = [{"type": "image_url", "url": "abc"}]
    if change == "limit":
        data["choices"][0]["message"]["content"] = "a" * 262145
    if change == "tool_type":
        data["choices"][0]["message"]["tool_calls"] = [
            {"type": "custom", "id": "c", "custom": {"input": "abc"}}
        ]
    with pytest.raises(GuardrailRejected):
        postflight(data, cfg())


@pytest.mark.parametrize("action,status", [("block", 403), ("redact", 200)])
@pytest.mark.parametrize("path", ["/v1/chat/completions", "/v1/responses"])
async def test_billed_usage_and_no_content_audit(client, admin_headers, monkeypatch, action, status, path):
    headers, _, _ = await setup_inference(client, admin_headers)
    monkeypatch.setenv("GUARDRAILS_OUTPUT_ENABLED", "true")
    monkeypatch.setenv("GUARDRAILS_OUTPUT_ACTION", action)
    get_settings.cache_clear()
    await mock_upstream(lambda req: httpx.Response(200, json=output(SECRET)))
    body = {"model": "chat", "messages": [{"role": "user", "content": "hello"}], "max_tokens": 10}
    if path.endswith("responses"):
        body = {"model": "chat", "input": "hello", "max_output_tokens": 10}
    response = await client.post(path, headers=headers, json=body)
    assert response.status_code == status, response.text
    assert SECRET not in response.text
    usage = (await client.get("/admin/api/usage", headers=admin_headers)).json()
    assert len(usage) == 1 and usage[0]["cost_micros"] == 20 and not usage[0]["estimated"]
    audit = await client.get("/admin/api/audit", headers=admin_headers)
    assert SECRET not in audit.text
    assert "guardrail.output_" + ("block" if action == "block" else "redact") in audit.text


async def test_stream_rejected_before_provider_and_reservation(client, admin_headers, monkeypatch):
    headers, _, _ = await setup_inference(client, admin_headers)
    monkeypatch.setenv("GUARDRAILS_OUTPUT_ENABLED", "true")
    get_settings.cache_clear()

    def handler(req):
        pytest.fail("Upstream must not be called")

    await mock_upstream(handler)
    response = await client.post(
        "/v1/chat/completions",
        headers=headers,
        json={"model": "chat", "messages": [{"role": "user", "content": "hello"}], "stream": True},
    )
    assert response.status_code == 400
    assert (await client.get("/admin/api/usage", headers=admin_headers)).json() == []
    assert (await client.get("/admin/api/reservations", headers=admin_headers)).json() == []


def test_disabled_is_passthrough():
    data = {"unrecognized": SECRET}
    assert postflight(data, cfg(guardrails_output_enabled=False)).payload is data


@pytest.mark.parametrize("upstream_status", [200, 400, 500])
async def test_no_upstream_error_or_header_leak(client, admin_headers, monkeypatch, upstream_status):
    headers, _, _ = await setup_inference(client, admin_headers)
    monkeypatch.setenv("GUARDRAILS_OUTPUT_ENABLED", "true")
    get_settings.cache_clear()
    data = output() if upstream_status == 200 else {"error": {"message": SECRET}}
    await mock_upstream(
        lambda req: httpx.Response(upstream_status, json=data, headers={"x-request-id": SECRET})
    )
    r = await client.post(
        "/v1/chat/completions",
        headers=headers,
        json={"model": "chat", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == (200 if upstream_status == 200 else 502)
    assert SECRET not in r.text and SECRET not in str(r.headers)
    assert "x-provider-request-id" not in r.headers


async def test_unsupported_output_still_charged(client, admin_headers, monkeypatch):
    headers, _, _ = await setup_inference(client, admin_headers)
    monkeypatch.setenv("GUARDRAILS_OUTPUT_ENABLED", "true")
    get_settings.cache_clear()
    data = output()
    data["choices"][0]["message"]["audio"] = {"data": SECRET}
    await mock_upstream(lambda req: httpx.Response(200, json=data))
    r = await client.post(
        "/v1/chat/completions",
        headers=headers,
        json={"model": "chat", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 403 and SECRET not in r.text
    usage = (await client.get("/admin/api/usage", headers=admin_headers)).json()
    assert len(usage) == 1 and usage[0]["cost_micros"] == 20


def embedding_output():
    return {
        "object": "list",
        "model": "chat",
        "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2]}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 0, "total_tokens": 10},
    }


@pytest.mark.parametrize(
    "mutation",
    [
        lambda data: data.update(extra=SECRET),
        lambda data: data["data"][0].update(text=SECRET),
        lambda data: data.update(model=SECRET),
        lambda data: data["data"][0].update(embedding=[SECRET]),
        lambda data: data["data"][0].update(embedding=[float("inf")]),
        lambda data: data["data"][0].update(embedding=[True]),
        lambda data: data["usage"].update(extra=SECRET),
    ],
)
def test_embedding_guardrails_fail_closed(mutation):
    data = embedding_output()
    mutation(data)
    with pytest.raises(GuardrailRejected):
        postflight(data, cfg("redact"), endpoint="embeddings")


def test_embedding_vectors_not_dlp_scanned():
    data = embedding_output()
    data["data"][0]["embedding"] = [4111111111111111, 0.2]
    assert postflight(data, cfg(), endpoint="embeddings").payload == data


@pytest.mark.parametrize("bad", [False, True])
async def test_embedding_output_metadata_billed_and_guarded(client, admin_headers, monkeypatch, bad):
    headers, _, _ = await setup_inference(client, admin_headers)
    monkeypatch.setenv("GUARDRAILS_OUTPUT_ENABLED", "true")
    get_settings.cache_clear()
    data = embedding_output()
    if bad:
        data["data"][0]["text"] = SECRET
    await mock_upstream(lambda req: httpx.Response(200, json=data))
    r = await client.post("/v1/embeddings", headers=headers, json={"model": "chat", "input": "hello"})
    assert r.status_code == (403 if bad else 200), r.text
    assert SECRET not in r.text
    usage = (await client.get("/admin/api/usage", headers=admin_headers)).json()
    assert len(usage) == 1 and usage[0]["cost_micros"] == 10 and not usage[0]["estimated"]


async def test_base64_embedding_rejected_predispatch(client, admin_headers, monkeypatch):
    headers, _, _ = await setup_inference(client, admin_headers)
    monkeypatch.setenv("GUARDRAILS_OUTPUT_ENABLED", "true")
    get_settings.cache_clear()

    def handler(req):
        pytest.fail("must not dispatch")

    await mock_upstream(handler)
    r = await client.post(
        "/v1/embeddings",
        headers=headers,
        json={"model": "chat", "input": "hello", "encoding_format": "base64"},
    )
    assert r.status_code == 400
    assert (await client.get("/admin/api/reservations", headers=admin_headers)).json() == []


def test_output_tool_overflow_is_policy_rejection():
    data = output(None)
    data["choices"][0]["message"]["tool_calls"] = [
        {"id": "c", "type": "function", "function": {"name": "fn", "arguments": '{"n":1e309}'}}
    ]
    with pytest.raises(GuardrailRejected) as error:
        postflight(data, cfg("redact"))
    assert error.value.reason == "invalid_tool_arguments"


async def test_output_tool_overflow_billed_as_local_policy(client, admin_headers, monkeypatch):
    headers, _, _ = await setup_inference(client, admin_headers)
    monkeypatch.setenv("GUARDRAILS_OUTPUT_ENABLED", "true")
    get_settings.cache_clear()
    data = output(None)
    data["choices"][0]["message"]["tool_calls"] = [
        {"id": "c", "type": "function", "function": {"name": "fn", "arguments": '{"n":1e309}'}}
    ]
    await mock_upstream(lambda req: httpx.Response(200, json=data))
    r = await client.post(
        "/v1/chat/completions",
        headers=headers,
        json={"model": "chat", "messages": [{"role": "user", "content": "hello"}]},
    )
    assert r.status_code == 403
    audit = (await client.get("/admin/api/audit", headers=admin_headers)).text
    assert "guardrail.output_block" in audit and "invalid_tool_arguments" in audit
    usage = (await client.get("/admin/api/usage", headers=admin_headers)).json()
    assert len(usage) == 1 and usage[0]["cost_micros"] == 20 and not usage[0]["estimated"]
