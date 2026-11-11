"""Absolute nonstream response deadlines and no-decompression inference policy."""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select
from starlette.requests import Request

from gateway import db, inference
from gateway.config import Settings, get_settings
from gateway.main import app
from test_gateway import setup_inference, mock_upstream
from test_stream_limits import ScriptedStream, StalledStream

PAYLOAD = {'model': 'chat', 'messages': [{'role': 'user', 'content': 'hi'}]}
RESULT = json.dumps({'choices': [{'message': {'role': 'assistant', 'content': 'ok'}, 'finish_reason': 'stop'}],
                     'usage': {'prompt_tokens': 1, 'completion_tokens': 1}}).encode()


async def assert_settled(provider_id, estimated=True, outcome='failure'):
    async with db.session_factory() as session:
        rows = (await session.scalars(select(db.Usage))).all()
        assert len(rows) == 1 and rows[0].estimated == estimated
        reservation = await session.get(db.Reservation, rows[0].reservation_id)
        assert reservation.status == 'settled'
        if estimated:
            assert rows[0].cost_micros == reservation.amount_micros
        health = await session.get(db.ProviderHealth, (provider_id, 'chat'))
        assert (health.last_outcome if health else None) == outcome


@pytest.mark.parametrize('last_time,success', [(0.99, True), (1.0, False), (1.1, False)])
async def test_continuous_body_absolute_deadline(client, admin_headers, monkeypatch, last_time, success):
    headers, _, provider = await setup_inference(client, admin_headers)
    monkeypatch.setattr(get_settings(), 'provider_response_duration_seconds', 1)
    clock = {'now': 0}
    monkeypatch.setattr(inference, 'time', SimpleNamespace(monotonic=lambda: clock['now']))
    chunks = [RESULT[:10], RESULT[10:20], RESULT[20:]]
    upstream = ScriptedStream(chunks, lambda i: clock.update(now=[0.4, 0.7, last_time][i]))
    await mock_upstream(lambda req: httpx.Response(200, stream=upstream))
    response = await client.post('/v1/chat/completions', headers=headers, json=PAYLOAD)
    assert response.status_code == (200 if success else 502)
    assert upstream.closed
    await assert_settled(provider['id'], not success, 'success' if success else 'failure')


@pytest.mark.parametrize('phase', ['headers', 'body'])
async def test_stalled_response_is_timed_out(client, admin_headers, monkeypatch, phase):
    headers, _, provider = await setup_inference(client, admin_headers)
    monkeypatch.setattr(get_settings(), 'provider_response_duration_seconds', 0.02)
    upstream = StalledStream()
    cancelled = asyncio.Event()
    async def handler(req):
        if phase == 'headers':
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
        return httpx.Response(200, stream=upstream)
    await mock_upstream(handler)
    response = await client.post('/v1/chat/completions', headers=headers, json=PAYLOAD)
    assert response.status_code == 502
    assert cancelled.is_set() if phase == 'headers' else upstream.closed and upstream.cancelled.is_set()
    await assert_settled(provider['id'])


@pytest.mark.parametrize('encoding', ['gzip', 'br', 'deflate', 'identity, gzip', 'gzip, identity', ''])
@pytest.mark.parametrize('streaming', [True, False])
async def test_encoded_responses_never_read(client, admin_headers, encoding, streaming):
    headers, _, provider = await setup_inference(client, admin_headers)
    class NoRead(httpx.AsyncByteStream):
        closed = False
        async def __aiter__(self):
            pytest.fail('Compressed response must not be read/decompressed')
            yield b''
        async def aclose(self):
            self.closed = True
    upstream = NoRead()
    def handler(req):
        assert req.headers['accept-encoding'] == 'identity'
        return httpx.Response(200, stream=upstream, headers={'Content-Encoding': encoding})
    await mock_upstream(handler)
    response = await client.post('/v1/chat/completions', headers=headers, json={**PAYLOAD, 'stream': streaming})
    assert response.status_code == 502 and upstream.closed
    await assert_settled(provider['id'])


async def test_case_insensitive_identity_encoding(client, admin_headers):
    headers, _, provider = await setup_inference(client, admin_headers)
    upstream = ScriptedStream([RESULT])
    await mock_upstream(lambda req: httpx.Response(200, stream=upstream, headers={'Content-Encoding': ' IdEnTiTy '}))
    response = await client.post('/v1/chat/completions', headers=headers, json=PAYLOAD)
    assert response.status_code == 200 and upstream.closed
    await assert_settled(provider['id'], False, 'success')


async def test_nonstream_cancel_during_body_preserves_cancellation(client, admin_headers):
    headers, _, provider = await setup_inference(client, admin_headers)
    upstream = StalledStream()
    await mock_upstream(lambda req: httpx.Response(200, stream=upstream))
    async def receive():
        return {'type': 'http.request', 'body': json.dumps(PAYLOAD).encode(), 'more_body': False}
    request = Request({'type': 'http', 'method': 'POST', 'path': '/v1/chat/completions', 'app': app,
                       'headers': [(k.lower().encode(), v.encode()) for k, v in headers.items()]}, receive)
    task = asyncio.create_task(inference.infer(request, 'chat/completions'))
    await upstream.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert upstream.closed and upstream.cancelled.is_set()
    await assert_settled(provider['id'], outcome=None)


@pytest.mark.parametrize('value', [0, -1, 3601])
def test_deadline_setting_bounds(client, value):
    with pytest.raises(ValueError) as error:
        Settings(**{**get_settings().model_dump(), "provider_response_duration_seconds": value})
    assert [e["loc"] for e in error.value.errors()] == [("provider_response_duration_seconds",)]


def test_probe_lease_covers_response_deadline(client):
    assert Settings(**{**get_settings().model_dump(), "provider_response_duration_seconds": 1000}).effective_circuit_probe_seconds >= 1030


async def test_headers_consume_body_budget(client, admin_headers, monkeypatch):
    headers, _, provider = await setup_inference(client, admin_headers)
    monkeypatch.setattr(get_settings(), 'provider_response_duration_seconds', 1)
    clock = {'now': 0}
    monkeypatch.setattr(inference, 'time', SimpleNamespace(monotonic=lambda: clock['now']))
    upstream = ScriptedStream([RESULT], lambda i: clock.update(now=1.01))
    def handler(req):
        clock['now'] = 0.9
        return httpx.Response(200, stream=upstream)
    await mock_upstream(handler)
    response = await client.post('/v1/chat/completions', headers=headers, json=PAYLOAD)
    assert response.status_code == 502 and upstream.closed
    await assert_settled(provider['id'])


async def test_repeated_content_encoding_rejected(client, admin_headers):
    headers, _, provider = await setup_inference(client, admin_headers)
    upstream = StalledStream()
    await mock_upstream(lambda req: httpx.Response(200, stream=upstream,
                        headers=[('content-encoding', 'identity'), ('content-encoding', 'gzip')]))
    response = await client.post('/v1/chat/completions', headers=headers, json=PAYLOAD)
    assert response.status_code == 502 and upstream.closed and not upstream.started.is_set()
    await assert_settled(provider['id'])


@pytest.mark.parametrize('kind', ['openai', 'azure_openai', 'anthropic', 'gemini'])
async def test_identity_requested_across_adapters(client, admin_headers, kind):
    headers, _, registered = await setup_inference(client, admin_headers)
    async with db.transaction() as session:
        provider = await session.get(db.Provider, registered['id'])
        provider.kind = kind
    upstream = StalledStream()
    def handler(req):
        assert req.headers['accept-encoding'] == 'identity'
        return httpx.Response(200, stream=upstream, headers={'content-encoding': 'gzip'})
    await mock_upstream(handler)
    response = await client.post('/v1/chat/completions', headers=headers, json=PAYLOAD)
    assert response.status_code == 502 and upstream.closed and not upstream.started.is_set()
    await assert_settled(registered['id'])


async def test_eof_after_deadline_is_not_success(client, admin_headers, monkeypatch):
    headers, _, registered = await setup_inference(client, admin_headers)
    monkeypatch.setattr(get_settings(), 'provider_response_duration_seconds', 1)
    clock = {'now': 0}
    monkeypatch.setattr(inference, 'time', SimpleNamespace(monotonic=lambda: clock['now']))
    class LateEof(ScriptedStream):
        async def __aiter__(self):
            yield RESULT
            clock['now'] = 1
    upstream = LateEof([])
    await mock_upstream(lambda req: httpx.Response(200, stream=upstream))
    response = await client.post('/v1/chat/completions', headers=headers, json=PAYLOAD)
    assert response.status_code == 502 and upstream.closed
    await assert_settled(registered['id'])
