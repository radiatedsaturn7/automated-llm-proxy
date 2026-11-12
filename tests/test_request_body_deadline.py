import asyncio
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from gateway.config import Settings
from gateway.main import BodyLimitMiddleware


def settings(monkeypatch, timeout=0.03, limit=5):
    monkeypatch.setattr("gateway.main.get_settings", lambda: SimpleNamespace(
        request_body_timeout_seconds=timeout, max_request_bytes=limit))


async def invoke(receive, app=None, scope=None):
    sent, calls = [], []

    async def downstream(scope, receive, send):
        calls.append(await receive())
        if app:
            await app(scope, receive, send)

    async def send(message):
        sent.append(message)

    await BodyLimitMiddleware(downstream)(scope or {"type": "http"}, receive, send)
    return sent, calls


async def test_stalled_unauthenticated_body_times_out_without_dispatch(monkeypatch):
    settings(monkeypatch)
    cancelled = asyncio.Event()

    async def receive():
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    sent, calls = await asyncio.wait_for(invoke(receive), 1)
    assert cancelled.is_set() and not calls
    assert [m["status"] for m in sent if m["type"] == "http.response.start"] == [408]
    assert b"deadline" in sent[-1]["body"]


async def test_trickle_does_not_reset_total_deadline(monkeypatch):
    settings(monkeypatch, timeout=0.05, limit=1000)
    chunks = 0

    async def receive():
        nonlocal chunks
        await asyncio.sleep(0.01)
        chunks += 1
        return {"type": "http.request", "body": b"x", "more_body": True}

    sent, calls = await asyncio.wait_for(invoke(receive), 1)
    assert chunks > 0 and not calls
    assert sent[0]["status"] == 408


@pytest.mark.parametrize("chunks,status", [([b"123", b"456"], 413), ([b"123456"], 413)])
async def test_over_limit_chunks_are_not_dispatched(monkeypatch, chunks, status):
    settings(monkeypatch)
    iterator = iter(chunks)

    async def receive():
        return {"type": "http.request", "body": next(iterator), "more_body": True}

    sent, calls = await invoke(receive)
    assert sent[0]["status"] == status and not calls


async def test_completed_body_replayed_once_application_not_timed_out(monkeypatch):
    settings(monkeypatch)
    messages = iter([
        {"type": "http.request", "body": b"12", "more_body": True},
        {"type": "http.request", "body": b"345", "more_body": False},
        {"type": "http.disconnect"},
    ])

    async def receive():
        return next(messages)

    async def app(scope, receive, send):
        await asyncio.sleep(0.06)  # Longer than the body deadline, still allowed.
        assert await receive() == {"type": "http.disconnect"}
        await send({"type": "http.response.start", "status": 204, "headers": []})

    sent, calls = await invoke(receive, app)
    assert calls == [{"type": "http.request", "body": b"12345", "more_body": False}]
    assert sent[0]["status"] == 204


async def test_disconnect_does_not_dispatch_or_send(monkeypatch):
    settings(monkeypatch)

    async def receive():
        return {"type": "http.disconnect"}

    assert await invoke(receive) == ([], [])


async def test_external_cancellation_propagates_without_response(monkeypatch):
    settings(monkeypatch)

    async def receive():
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await invoke(receive)


async def test_non_http_scope_is_unchanged(monkeypatch):
    settings(monkeypatch)

    async def receive():
        return {"type": "lifespan.startup"}

    sent, calls = await invoke(receive, scope={"type": "lifespan"})
    assert not sent and calls == [{"type": "lifespan.startup"}]


@pytest.mark.parametrize("value", [0, -1, 121, float("inf"), float("nan")])
def test_request_body_timeout_bounds(value):
    from cryptography.fernet import Fernet
    with pytest.raises(ValidationError):
        Settings(gateway_master_key=Fernet.generate_key().decode(), gateway_signing_key="s" * 40,
                 bootstrap_token="b" * 40, request_body_timeout_seconds=value)


@pytest.mark.parametrize("version,close", [("1.0", True), ("1.1", True), ("2", False)])
@pytest.mark.parametrize("oversize", [True, False])
async def test_rejections_close_only_http1(monkeypatch, version, close, oversize):
    settings(monkeypatch)

    async def receive():
        if not oversize:
            await asyncio.Event().wait()
        return {"type": "http.request", "body": b"123456", "more_body": True}

    sent, calls = await invoke(receive, scope={"type": "http", "http_version": version})
    assert not calls
    assert sent[0]["status"] == (413 if oversize else 408)
    assert (dict(sent[0]["headers"]).get(b"connection") == b"close") is close
