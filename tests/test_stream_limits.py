"""Bound endless native/compatible provider streams without real provider calls."""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

from gateway import db, providers, inference
from gateway.config import get_settings
from gateway.main import app
from test_gateway import setup_inference, mock_upstream

PROVIDER = SimpleNamespace(kind='openai')
TOKEN = b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'
USAGE = b'data: {"choices":[],"usage":{"prompt_tokens":1,"completion_tokens":1}}\n\n'
DONE = b'data: [DONE]\n\n'


class ScriptedStream(httpx.AsyncByteStream):
    def __init__(self, chunks, on_chunk=None):
        self.chunks = chunks
        self.on_chunk = on_chunk
        self.closed = False

    async def __aiter__(self):
        for index, chunk in enumerate(self.chunks):
            if self.on_chunk:
                self.on_chunk(index)
            yield chunk

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize('chunks,limits', [
    ([TOKEN, TOKEN, DONE], {'max_bytes': len(TOKEN)+1}),
    ([TOKEN, TOKEN, DONE], {'max_events': 1}),
    ([b': keepalive\n\n']*4+[DONE], {'max_events': 3}),
    ([b'x']*20+[DONE], {'max_bytes': 10}),
    ([TOKEN, DONE], {'max_events': 1}),
])
async def test_limits_stop_before_done(chunks, limits):
    translator = providers.StreamTranslator(PROVIDER, 'chat', **limits)
    response = httpx.Response(200, stream=ScriptedStream(chunks))
    received = []
    with pytest.raises(ValueError, match='limit exceeded'):
        async for chunk in translator.iterate(response):
            received.append(chunk)
    assert DONE not in received and not translator.complete


async def test_exact_boundaries_allow_normal_completion():
    chunks = [TOKEN, USAGE, DONE]
    translator = providers.StreamTranslator(PROVIDER, 'chat', max_bytes=sum(map(len, chunks)), max_events=3)
    response = httpx.Response(200, stream=ScriptedStream(chunks))
    result = [chunk async for chunk in translator.iterate(response)]
    assert result[-1] == DONE and translator.complete
    assert translator.received_bytes == sum(map(len, chunks)) and translator.received_events == 3


async def test_absolute_deadline_expires_despite_regular_activity(monkeypatch):
    clock = {'now': 0.0}
    monkeypatch.setattr(providers, 'time', SimpleNamespace(monotonic=lambda: clock['now']))
    def advance(index):
        clock['now'] = index * 0.4
    response = httpx.Response(200, stream=ScriptedStream([TOKEN]*5+[DONE], advance))
    translator = providers.StreamTranslator(PROVIDER, 'chat', duration_seconds=1)
    received = []
    with pytest.raises(ValueError, match='duration exceeded'):
        async for chunk in translator.iterate(response):
            received.append(chunk)
    assert len(received) == 3 and DONE not in received and not translator.complete


class StalledStream(httpx.AsyncByteStream):
    def __init__(self):
        self.started, self.cancelled = asyncio.Event(), asyncio.Event()
        self.closed = False

    async def __aiter__(self):
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        yield b''

    async def aclose(self):
        self.closed = True


async def test_deadline_interrupts_pending_read():
    stream = StalledStream()
    response = httpx.Response(200, stream=stream)
    translator = providers.StreamTranslator(PROVIDER, 'chat', duration_seconds=0.02)
    with pytest.raises(ValueError, match='duration exceeded'):
        _ = [chunk async for chunk in translator.iterate(response)]
    assert stream.cancelled.is_set() and not translator.complete


@pytest.mark.parametrize('limit', ['bytes', 'events', 'duration'])
async def test_gateway_limit_closes_and_conservatively_accounts_once(client, admin_headers, monkeypatch, limit):
    headers, _, registered = await setup_inference(client, admin_headers)
    cfg = get_settings()
    clock = {'now': 0.0}
    if limit == 'bytes':
        monkeypatch.setattr(cfg, 'provider_stream_max_bytes', len(TOKEN)+len(USAGE)+1)
    elif limit == 'events':
        monkeypatch.setattr(cfg, 'provider_stream_max_events', 2)
    else:
        monkeypatch.setattr(cfg, 'provider_stream_duration_seconds', 1)
        fake_time = SimpleNamespace(monotonic=lambda: clock['now'])
        monkeypatch.setattr(providers, 'time', fake_time)
        monkeypatch.setattr(inference, 'time', fake_time)
    stream = ScriptedStream([TOKEN, USAGE, TOKEN, DONE],
                            lambda i: clock.update(now=i*0.6) if limit == 'duration' else None)
    await mock_upstream(lambda req: httpx.Response(200, stream=stream, headers={'content-type': 'text/event-stream'}))
    response = await client.post('/v1/chat/completions', headers=headers,
        json={'model': 'chat', 'messages': [{'role': 'user', 'content': 'hi'}], 'stream': True})
    assert response.status_code == 200
    assert 'upstream_error' in response.text and '[DONE]' not in response.text
    assert stream.closed
    async with db.session_factory() as session:
        rows = (await session.scalars(select(db.Usage))).all()
        assert len(rows) == 1 and rows[0].estimated
        reservation = await session.get(db.Reservation, rows[0].reservation_id)
        assert rows[0].cost_micros == reservation.amount_micros and reservation.status == 'settled'
        health = await session.get(db.ProviderHealth, (registered['id'], 'chat'))
        assert health.last_outcome == 'failure'


async def test_pending_header_deadline_is_failure_and_accounted(client, admin_headers, monkeypatch):
    headers, _, registered = await setup_inference(client, admin_headers)
    monkeypatch.setattr(get_settings(), 'provider_stream_duration_seconds', 0.02)
    cancelled = asyncio.Event()
    async def handler(req):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
    await mock_upstream(handler)
    response = await client.post('/v1/chat/completions', headers=headers,
        json={'model': 'chat', 'messages': [{'role': 'user', 'content': 'hi'}], 'stream': True})
    assert response.status_code == 502 and cancelled.is_set()
    async with db.session_factory() as session:
        rows = (await session.scalars(select(db.Usage))).all()
        assert len(rows) == 1 and rows[0].estimated
        health = await session.get(db.ProviderHealth, (registered['id'], 'chat'))
        assert health.last_outcome == 'failure'


async def test_client_cancellation_remains_cancellation(client, admin_headers):
    from starlette.requests import Request
    headers, _, _ = await setup_inference(client, admin_headers)
    upstream = StalledStream()
    await mock_upstream(lambda req: httpx.Response(200, stream=upstream))
    body = json.dumps({'model': 'chat', 'messages': [{'role': 'user', 'content': 'hi'}], 'stream': True}).encode()
    async def receive():
        return {'type': 'http.request', 'body': body, 'more_body': False}
    request = Request({'type': 'http', 'method': 'POST', 'path': '/v1/chat/completions', 'app': app,
                       'headers': [(k.lower().encode(), v.encode()) for k, v in headers.items()]}, receive)
    response = await inference.infer(request, 'chat/completions')
    task = asyncio.create_task(anext(response.body_iterator))
    await upstream.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.gather(*list(app.state.accounting_tasks))
    assert upstream.cancelled.is_set() and upstream.closed
    async with db.session_factory() as session:
        rows = (await session.scalars(select(db.Usage))).all()
        assert len(rows) == 1 and rows[0].estimated


@pytest.mark.parametrize('name,value', [('provider_stream_duration_seconds', 0), ('provider_stream_duration_seconds', 3601),
    ('provider_stream_max_bytes', 1023), ('provider_stream_max_bytes', 268_435_457),
    ('provider_stream_max_events', 0), ('provider_stream_max_events', 1_000_001)])
async def test_stream_limit_settings_bounded(client, name, value):
    from pydantic import ValidationError
    from gateway.config import Settings
    values = get_settings().model_dump()
    values[name] = value
    with pytest.raises(ValidationError):
        Settings(**values)


async def test_native_message_stop_does_not_bypass_limits_before_eof():
    sequence = [
        {'type': 'message_start', 'message': {'usage': {'input_tokens': 2}}},
        {'type': 'message_delta', 'delta': {'stop_reason': 'end_turn'}, 'usage': {'output_tokens': 1}},
        {'type': 'message_stop'},
    ]
    chunks = [('data: '+json.dumps(event)+'\n\n').encode() for event in sequence] + [b': forever\n\n']
    translator = providers.StreamTranslator(SimpleNamespace(kind='anthropic'), 'chat', max_events=3)
    response = httpx.Response(200, stream=ScriptedStream(chunks))
    emitted = []
    with pytest.raises(ValueError, match='event limit exceeded'):
        async for chunk in translator.iterate(response):
            emitted.append(chunk)
    assert DONE not in emitted and not translator.complete


async def test_probe_lease_includes_stream_duration(client, monkeypatch):
    cfg = get_settings()
    monkeypatch.setattr(cfg, 'provider_stream_duration_seconds', 900)
    assert cfg.effective_circuit_probe_seconds >= 930


async def streaming_response(headers):
    from starlette.requests import Request
    body = json.dumps({'model': 'chat', 'messages': [{'role': 'user', 'content': 'hi'}], 'stream': True}).encode()
    async def receive():
        return {'type': 'http.request', 'body': body, 'more_body': False}
    return await inference.infer(Request({'type': 'http', 'method': 'POST', 'path': '/v1/chat/completions',
        'app': app, 'headers': [(k.lower().encode(), v.encode()) for k, v in headers.items()]}, receive), 'chat/completions')


@pytest.mark.parametrize('stall_at', ['headers', 'body'])
async def test_watchdog_releases_upstream_while_downstream_send_stalled(client, admin_headers, monkeypatch, stall_at):
    from unittest.mock import AsyncMock
    headers, _, registered = await setup_inference(client, admin_headers)
    deadline = asyncio.Event()
    async def wait_for_trigger(value):
        await deadline.wait()
    monkeypatch.setattr(inference, 'wait_stream_deadline', wait_for_trigger)
    record = AsyncMock(wraps=inference.provider_health.record)
    monkeypatch.setattr(inference.provider_health, 'record', record)
    upstream = ScriptedStream([TOKEN, USAGE, DONE])
    await mock_upstream(lambda req: httpx.Response(200, stream=upstream))
    response = await streaming_response(headers)
    stalled, resume = asyncio.Event(), asyncio.Event()
    sent = []
    async def send(message):
        sent.append(message)
        if ((stall_at == 'headers' and message['type'] == 'http.response.start')
                or (stall_at == 'body' and b'hello' in message.get('body', b''))):
            stalled.set()
            await resume.wait()
    delivery = asyncio.create_task(response.stream_response(send))
    await asyncio.wait_for(stalled.wait(), 1)
    deadline.set()
    await asyncio.wait_for(asyncio.gather(*list(app.state.accounting_tasks)), 1)
    assert upstream.closed and not delivery.done()
    async with db.session_factory() as session:
        usage = (await session.scalars(select(db.Usage))).all()
        assert len(usage) == 1 and usage[0].estimated
        health = await session.get(db.ProviderHealth, (registered['id'], 'chat'))
        assert health.last_outcome == 'failure'
    resume.set()
    await asyncio.wait_for(delivery, 1)
    assert all(m.get('body') != DONE for m in sent)
    assert any(b'upstream_error' in m.get('body', b'') for m in sent)
    assert record.await_count == 1
    async with db.session_factory() as session:
        assert len((await session.scalars(select(db.Usage))).all()) == 1


async def test_error_is_settled_before_error_chunk_is_yielded(client, admin_headers, monkeypatch):
    headers, _, _ = await setup_inference(client, admin_headers)
    monkeypatch.setattr(get_settings(), 'provider_stream_max_bytes', 1)
    upstream = ScriptedStream([TOKEN, DONE])
    await mock_upstream(lambda req: httpx.Response(200, stream=upstream))
    response = await streaming_response(headers)
    chunk = await anext(response.body_iterator)
    assert b'upstream_error' in chunk and upstream.closed
    # Generator is suspended at the error yield; finally has not executed yet.
    async with db.session_factory() as session:
        usage = (await session.scalars(select(db.Usage))).all()
        assert len(usage) == 1 and usage[0].estimated
    await response.body_iterator.aclose()


async def test_final_done_is_settled_before_downstream_send(client, admin_headers, monkeypatch):
    headers, _, _ = await setup_inference(client, admin_headers)
    never = asyncio.Event()
    async def wait_for_trigger(value):
        await never.wait()
    monkeypatch.setattr(inference, 'wait_stream_deadline', wait_for_trigger)
    upstream = ScriptedStream([TOKEN, USAGE, DONE])
    await mock_upstream(lambda req: httpx.Response(200, stream=upstream))
    response = await streaming_response(headers)
    while True:
        chunk = await anext(response.body_iterator)
        if chunk == DONE:
            break
    assert upstream.closed
    async with db.session_factory() as session:
        usage = (await session.scalars(select(db.Usage))).all()
        assert len(usage) == 1 and not usage[0].estimated
    await response.body_iterator.aclose()


async def test_watchdog_shutdown_snapshot_waits_for_winning_cleanup(client, admin_headers):
    headers, _, _ = await setup_inference(client, admin_headers)
    closing, release = asyncio.Event(), asyncio.Event()
    class SlowClose(ScriptedStream):
        async def aclose(self):
            closing.set()
            await release.wait()
            self.closed = True
    upstream = SlowClose([DONE])
    await mock_upstream(lambda req: httpx.Response(200, stream=upstream))
    response = await streaming_response(headers)
    # Simulate lifespan taking its snapshot before generator settlement starts.
    draining = asyncio.gather(*list(app.state.accounting_tasks))
    delivery = asyncio.create_task(anext(response.body_iterator))
    await asyncio.wait_for(closing.wait(), 1)
    assert not draining.done()
    release.set()
    assert await asyncio.wait_for(delivery, 1) == DONE
    await asyncio.wait_for(draining, 1)
    assert upstream.closed
    await response.body_iterator.aclose()


async def test_header_send_cancellation_settles_without_provider_failure(client, admin_headers, monkeypatch):
    from unittest.mock import AsyncMock
    headers, _, _ = await setup_inference(client, admin_headers)
    never = asyncio.Event()
    async def wait_for_trigger(value):
        await never.wait()
    monkeypatch.setattr(inference, 'wait_stream_deadline', wait_for_trigger)
    record = AsyncMock(wraps=inference.provider_health.record)
    monkeypatch.setattr(inference.provider_health, 'record', record)
    upstream = ScriptedStream([TOKEN, DONE])
    await mock_upstream(lambda req: httpx.Response(200, stream=upstream))
    response = await streaming_response(headers)
    entered = asyncio.Event()
    async def send(message):
        assert message['type'] == 'http.response.start'
        entered.set()
        await asyncio.Event().wait()
    async def receive():
        await asyncio.Event().wait()
    delivery = asyncio.create_task(response({'type': 'http', 'asgi': {'spec_version': '2.4'}}, receive, send))
    await asyncio.wait_for(entered.wait(), 1)
    delivery.cancel()
    with pytest.raises(asyncio.CancelledError):
        await delivery
    while app.state.accounting_tasks:
        snapshot = list(app.state.accounting_tasks)
        await asyncio.wait_for(asyncio.gather(*snapshot), 1)
        app.state.accounting_tasks.difference_update(snapshot)
    assert upstream.closed and record.await_count == 0
    async with db.session_factory() as session:
        usage = (await session.scalars(select(db.Usage))).all()
        assert len(usage) == 1 and usage[0].estimated


async def test_watchdog_close_during_read_yields_generic_error(client, admin_headers, monkeypatch):
    headers, _, _ = await setup_inference(client, admin_headers)
    trigger, reading, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def wait_for_trigger(value):
        await trigger.wait()
    monkeypatch.setattr(inference, 'wait_stream_deadline', wait_for_trigger)
    class ClosedRead(httpx.AsyncByteStream):
        async def __aiter__(self):
            reading.set()
            await closed.wait()
            raise httpx.StreamClosed()
            yield b''
        async def aclose(self):
            closed.set()
    await mock_upstream(lambda req: httpx.Response(200, stream=ClosedRead()))
    response = await streaming_response(headers)
    next_chunk = asyncio.create_task(anext(response.body_iterator))
    await asyncio.wait_for(reading.wait(), 1)
    trigger.set()
    chunk = await asyncio.wait_for(next_chunk, 1)
    assert b'upstream_error' in chunk and DONE not in chunk
    await response.body_iterator.aclose()
    async with db.session_factory() as session:
        assert len((await session.scalars(select(db.Usage))).all()) == 1


async def test_lifespan_drains_tasks_spawned_after_first_snapshot(monkeypatch):
    from fastapi import FastAPI
    from unittest.mock import AsyncMock
    from gateway import main
    fake_db = SimpleNamespace(dispose=AsyncMock())
    monkeypatch.setattr(db, 'engine', fake_db)
    monkeypatch.setattr(main, 'get_settings', lambda: SimpleNamespace(provider_timeout_seconds=120))
    instance = FastAPI()
    context = main.lifespan(instance)
    await context.__aenter__()
    trigger, child_started, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def child():
        child_started.set()
        await release.wait()
    async def parent():
        await trigger.wait()
        task = asyncio.create_task(child())
        instance.state.accounting_tasks.add(task)
        task.add_done_callback(instance.state.accounting_tasks.discard)
    task = asyncio.create_task(parent())
    instance.state.accounting_tasks.add(task)
    task.add_done_callback(instance.state.accounting_tasks.discard)
    shutdown = asyncio.create_task(context.__aexit__(None, None, None))
    await asyncio.sleep(0)  # Lifespan snapshots the parent before it spawns a child.
    trigger.set()
    await asyncio.wait_for(child_started.wait(), 1)
    await asyncio.sleep(0)
    assert not shutdown.done() and fake_db.dispose.await_count == 0
    release.set()
    await asyncio.wait_for(shutdown, 1)
    assert fake_db.dispose.await_count == 1
