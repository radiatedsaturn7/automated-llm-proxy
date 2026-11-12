import asyncio
from types import SimpleNamespace

import pytest

from gateway import overload


@pytest.fixture
def config(monkeypatch):
    cfg = SimpleNamespace(max_inflight_requests=1, max_inflight_operations=1)
    monkeypatch.setattr(overload, 'get_settings', lambda: cfg)
    return cfg


def scope(path='/v1/chat/completions', method='POST', version='1.1'):
    return {'type': 'http', 'path': path, 'method': method, 'http_version': version}


async def forbidden_receive():
    raise AssertionError('Rejected request must not read its body')


async def invoke(app, request=None):
    messages = []

    async def send(message):
        messages.append(message)

    await app(request or scope(), forbidden_receive, send)
    return messages


async def test_blocked_upload_rejects_without_reading_or_dispatch(config):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def inner(request, receive, send):
        calls.append(request)
        entered.set()
        await release.wait()  # Represents upload still waiting for its first byte.

    app = overload.OverloadMiddleware(inner, lambda: ())
    held = asyncio.create_task(invoke(app))
    try:
        await entered.wait()
        messages = await invoke(app)
        assert messages[0]['status'] == 503
        headers = dict(messages[0]['headers'])
        assert headers[b'cache-control'] == b'no-store'
        assert headers[b'x-content-type-options'] == b'nosniff'
        assert headers[b'retry-after'] == b'1' and headers[b'connection'] == b'close'
        assert len(calls) == 1
    finally:
        release.set()
        await held
    assert app.active['application'] == 0


@pytest.mark.parametrize('finish', ['normal', 'cancel', 'error'])
async def test_stream_holds_slot_until_completion_and_always_releases(config, finish):
    entered, release = asyncio.Event(), asyncio.Event()

    async def inner(request, receive, send):
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b'chunk', 'more_body': True})
        entered.set()
        await release.wait()
        if finish == 'error':
            raise ValueError('synthetic')
        await send({'type': 'http.response.body', 'body': b'', 'more_body': False})

    app = overload.OverloadMiddleware(inner, lambda: ())
    for _ in range(2):
        entered.clear()
        release.clear()
        task = asyncio.create_task(invoke(app))
        await entered.wait()
        assert (await invoke(app))[0]['status'] == 503
        if finish == 'cancel':
            task.cancel()
        else:
            release.set()
        outcome = await asyncio.gather(task, return_exceptions=True)
        assert isinstance(outcome[0], asyncio.CancelledError) if finish == 'cancel' else True
        assert isinstance(outcome[0], ValueError) if finish == 'error' else True
        assert app.active == {'application': 0, 'operations': 0}


@pytest.mark.parametrize('path,method', [('/health/live', 'POST'), ('/health/live/', 'GET'),
    ('/health/live/other', 'GET'), ('/health/ready', 'HEAD'), ('/metrics/', 'GET'), ('/metrics', 'OPTIONS')])
async def test_no_path_or_method_capacity_bypass(config, path, method):
    app = overload.OverloadMiddleware(None, lambda: ())
    app.active['application'] = 1
    assert (await invoke(app, scope(path, method)))[0]['status'] == 503


@pytest.mark.parametrize('path', sorted(overload.OPS))
async def test_operations_have_separate_bounded_capacity(config, path):
    seen = []

    async def inner(request, receive, send):
        seen.append(request['path'])
        assert app.active['operations'] == 1

    app = overload.OverloadMiddleware(inner, lambda: ())
    app.active['application'] = 1
    await invoke(app, scope(path, 'GET'))
    assert seen == [path]
    app.active['operations'] = 1
    assert (await invoke(app, scope(path, 'GET')))[0]['status'] == 503


async def test_accounting_backlog_and_http2(config):
    app = overload.OverloadMiddleware(None, lambda: [object(), object()])
    messages = await invoke(app, scope(version='2'))
    assert messages[0]['status'] == 503
    assert b'connection' not in dict(messages[0]['headers'])
    assert app.active['application'] == 0


async def test_non_http_passthrough(config):
    seen = []

    async def inner(request, receive, send):
        seen.append(request['type'])

    app = overload.OverloadMiddleware(inner, lambda: ())
    await app({'type': 'lifespan'}, None, None)
    assert seen == ['lifespan']


async def test_cancelled_http_retained_accounting_blocks_until_cleanup(config):
    entered, never = asyncio.Event(), asyncio.Event()
    backlog = set()

    async def inner(request, receive, send):
        entered.set()
        try:
            await never.wait()
        finally:
            backlog.update({'settlement', 'watchdog'})

    app = overload.OverloadMiddleware(inner, lambda: backlog)
    task = asyncio.create_task(invoke(app))
    await entered.wait()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert app.active['application'] == 0
    assert (await invoke(app))[0]['status'] == 503
    backlog.clear()

    async def healthy(request, receive, send):
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})

    app.app = healthy
    assert (await invoke(app))[0]['status'] == 200


async def test_real_stack_overload_precedes_body_buffering_and_preserves_metrics(client, monkeypatch):
    from gateway.main import app
    assert (await client.get("/health/live")).status_code == 200
    stack = app.middleware_stack
    while not isinstance(stack, overload.OverloadMiddleware):
        stack = stack.app
    monkeypatch.setattr(stack, 'active', {'application': 128, 'operations': 0})
    response = await client.post('/v1/chat/completions', content=b'x' * 1_048_577)
    assert response.status_code == 503  # Not the inner body limiter's 413 or auth's 401.
    assert response.headers.get('x-request-id')
    assert (await client.get('/health/live')).status_code == 200
    assert (await client.get('/metrics')).status_code == 401
    metrics = await client.get('/metrics', headers={'Authorization': 'Bearer metrics-' + 'x' * 40})
    assert metrics.status_code == 200
    assert 'gateway_overload_rejections_total' in metrics.text


@pytest.mark.parametrize('field,value', [('max_inflight_requests', 0), ('max_inflight_requests', 10001),
    ('max_inflight_operations', 0), ('max_inflight_operations', 129)])
async def test_capacity_configuration_bounds(client, field, value):
    from pydantic import ValidationError
    from gateway.config import get_settings
    current = get_settings()
    with pytest.raises(ValidationError):
        type(current).model_validate(current.model_dump() | {field: value})
