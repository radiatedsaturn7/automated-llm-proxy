import asyncio

import httpx
import pytest

from gateway.relay import create_relay_app
from test_relay import settings


class TrackedStream(httpx.AsyncByteStream):
    def __init__(self):
        self.entered = False
        self.closed = 0

    async def __aiter__(self):
        self.entered = True
        yield b'data: first\n\n'

    async def aclose(self):
        self.closed += 1


async def invoke(app, send):
    scope = {'type': 'http', 'asgi': {'version': '3.0', 'spec_version': '2.4'},
             'http_version': '1.1', 'method': 'POST', 'scheme': 'http',
             'path': '/v1/chat/completions', 'raw_path': b'/v1/chat/completions',
             'query_string': b'', 'server': ('127.0.0.1', 8090), 'client': ('127.0.0.1', 1234),
             'headers': [(b'authorization', b'Bearer ' + b'l' * 32), (b'content-type', b'application/json')]}
    async def receive():
        return {'type': 'http.request', 'body': b'{}', 'more_body': False}
    await app(scope, receive, send)


async def make_app(stream):
    async def handler(request):
        if request.url.path == '/auth/agent/exchange':
            return httpx.Response(200, json={'access_token': 'synthetic', 'expires_in': 300})
        return httpx.Response(200, stream=stream)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return create_relay_app(settings(), client), client


async def test_cancel_before_stream_entry_closes_upstream():
    stream = TrackedStream()
    app, client = await make_app(stream)
    async def send(message):
        if message['type'] == 'http.response.start':
            raise asyncio.CancelledError
    try:
        async with app.router.lifespan_context(app):
            with pytest.raises(asyncio.CancelledError):
                await invoke(app, send)
        assert not stream.entered
        assert stream.closed == 1
    finally:
        await client.aclose()


@pytest.mark.parametrize('stage', ['headers', 'body', 'complete'])
async def test_close_once_on_disconnect_or_completion(stage):
    stream = TrackedStream()
    app, client = await make_app(stream)
    async def send(message):
        if stage == 'headers' and message['type'] == 'http.response.start':
            raise OSError('disconnected')
        if stage == 'body' and message['type'] == 'http.response.body':
            raise OSError('disconnected')
    try:
        async with app.router.lifespan_context(app):
            if stage == 'complete':
                await invoke(app, send)
            else:
                from starlette.requests import ClientDisconnect
                with pytest.raises(ClientDisconnect):
                    await invoke(app, send)
        assert stream.closed == 1
    finally:
        await client.aclose()


async def test_cancel_during_httpx_natural_close_is_retained_and_drained():
    started, release = asyncio.Event(), asyncio.Event()
    class SlowClose(TrackedStream):
        async def aclose(self):
            started.set()
            await release.wait()
            await super().aclose()
    stream = SlowClose()
    app, client = await make_app(stream)
    async def send(message):
        pass
    try:
        life = app.router.lifespan_context(app)
        await life.__aenter__()
        task = asyncio.create_task(invoke(app, send))
        try:
            await asyncio.wait_for(started.wait(), 5)
            task.cancel()
            # More than one cancellation must not cancel retained stream cleanup.
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            assert stream.closed == 0 and len(app.state.cleanup_tasks) == 1
            drain = asyncio.create_task(life.__aexit__(None, None, None))
            await asyncio.sleep(0)
            assert not drain.done()
            release.set()
            await asyncio.wait_for(drain, 5)
            assert stream.closed == 1 and not app.state.cleanup_tasks
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    finally:
        await client.aclose()


async def test_header_construction_failure_still_closes():
    stream = TrackedStream()
    app, client = await make_app(stream)
    original_send = client.send
    async def send(*args, **kwargs):
        response = await original_send(*args, **kwargs)
        if response.request.url.path != '/auth/agent/exchange':
            response.headers['content-type'] = 'text/\u2603'  # Cannot encode as ASGI Latin-1.
        return response
    client.send = send
    async def downstream(message):
        pass
    try:
        async with app.router.lifespan_context(app):
            with pytest.raises(UnicodeEncodeError):
                await invoke(app, downstream)
        assert stream.closed == 1
    finally:
        await client.aclose()


async def test_lifespan_waits_for_cancelled_request_unwind_before_cleanup():
    stream = TrackedStream()
    app, client = await make_app(stream)
    entered, unwinding, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def send(message):
        if message['type'] == 'http.response.start':
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                unwinding.set()
                await release.wait()
    life = app.router.lifespan_context(app)
    await life.__aenter__()
    request = asyncio.create_task(invoke(app, send))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        request.cancel()
        await asyncio.wait_for(unwinding.wait(), 5)
        drain = asyncio.create_task(life.__aexit__(None, None, None))
        await asyncio.sleep(0)
        assert not drain.done() and stream.closed == 0
        release.set()
        await asyncio.wait_for(drain, 5)
        assert stream.closed == 1
    finally:
        release.set()
        await asyncio.gather(request, return_exceptions=True)
        await client.aclose()


async def test_cleanup_failure_log_redacts_transport_exception(caplog):
    class FailedClose(TrackedStream):
        async def aclose(self):
            raise RuntimeError('synthetic-sensitive-provider-detail')
    stream = FailedClose()
    app, client = await make_app(stream)
    async def send(message):
        pass
    try:
        async with app.router.lifespan_context(app):
            await invoke(app, send)
        assert 'Central response cleanup failed' in caplog.text
        assert 'synthetic-sensitive-provider-detail' not in caplog.text
    finally:
        await client.aclose()
