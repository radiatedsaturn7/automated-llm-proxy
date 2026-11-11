"""Adversarial regressions identified by independent security review."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy.exc import SQLAlchemyError

from gateway import db
from gateway.providers import StreamTranslator, translate_response


@pytest.mark.parametrize("usage", [None, {}, {"input_tokens": 12}])
def test_anthropic_missing_usage_not_fabricated_as_free(usage):
    result = translate_response(SimpleNamespace(kind="anthropic"), {"content": [], "usage": usage, "stop_reason": "end_turn"}, "chat")
    assert result["usage"]["completion_tokens"] is None


async def test_truncated_stream_does_not_count_as_complete():
    translator = StreamTranslator(SimpleNamespace(kind="openai"), "chat")
    response = httpx.Response(200, content=b'data: {"usage":{"prompt_tokens":1,"completion_tokens":1}}\n\n')
    with pytest.raises(ValueError, match="without completion"):
        _ = [chunk async for chunk in translator.iterate(response)]
    assert not translator.complete


async def test_stream_comments_cannot_grow_line_buffer_unbounded():
    translator = StreamTranslator(SimpleNamespace(kind="openai"), "chat")
    response = httpx.Response(200, content=b":" + b"x" * 1_048_577)
    with pytest.raises(ValueError, match="line too large"):
        _ = [chunk async for chunk in translator.iterate(response)]


async def test_anthropic_completion_requires_message_stop():
    translator = StreamTranslator(SimpleNamespace(kind="anthropic"), "chat")
    response = httpx.Response(200, content=(
        b'data: {"type":"message_start","message":{"usage":{"input_tokens":10}}}\n\n'
        b'data: {"type":"message_delta","usage":{"output_tokens":4},"delta":{"stop_reason":"end_turn"}}\n\n'
        b'data: {"type":"message_stop"}\n\n'))
    chunks = [chunk async for chunk in translator.iterate(response)]
    assert translator.complete
    assert translator.usage["completion_tokens"] == 4
    assert chunks[-1] == b"data: [DONE]\n\n"


async def test_postgres_missing_policy_lock_fails_closed(monkeypatch):
    class Context:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *args):
            pass

    session = SimpleNamespace(begin=lambda: Context(), scalar=AsyncMock(return_value=None))
    monkeypatch.setattr(db, "session_factory", lambda: Context())
    monkeypatch.setattr(db, "engine", SimpleNamespace(url=SimpleNamespace(drivername="postgresql+asyncpg")))
    with pytest.raises(SQLAlchemyError, match="Policy lock missing"):
        async with db.transaction():
            pytest.fail("Must not admit work without policy lock")


async def test_validation_errors_never_echo_secrets(client, admin_headers):
    secret = "secret-" * 1400  # Over provider key length limit.
    response = await client.post("/admin/api/providers", headers=admin_headers, json={
        "name": "test", "api_key": secret, "base_url": "https://example.com", "models": {}})
    assert response.status_code == 422
    assert "secret-" not in response.text
    assert "input" not in response.json()["detail"][0]


@pytest.mark.parametrize("number", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_generation_parameters_rejected(client, number):
    from fastapi import HTTPException
    from gateway.inference import validate_payload
    with pytest.raises(HTTPException) as error:
        validate_payload({"model": "chat", "messages": [{"role": "user", "content": "hello"}],
                          "temperature": number}, "chat/completions")
    assert error.value.status_code == 400


async def test_interrupted_stream_partial_usage_charged_conservatively(client, admin_headers):
    from gateway.main import app
    response = await client.post("/admin/api/providers", headers=admin_headers, json={
        "name": "stream-review", "kind": "openai", "base_url": "https://example.com/v1",
        "api_key": "secret", "models": {"chat": {"upstream_model": "upstream", "input_cost_per_million": 1,
                                                    "output_cost_per_million": 1}}})
    assert response.status_code == 201
    await app.state.upstream.aclose()
    app.state.upstream = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(
        200, text='data: {"usage":{"prompt_tokens":1,"completion_tokens":0}}\n\n')))
    response = await client.post("/v1/chat/completions", headers=admin_headers, json={
        "model": "chat", "messages": [{"role": "user", "content": "hello"}], "stream": True})
    assert "Upstream stream interrupted" in response.text
    usage = (await client.get("/admin/api/usage", headers=admin_headers)).json()
    assert len(usage) == 1 and usage[0]["estimated"]
    assert usage[0]["cost_micros"] > 1
