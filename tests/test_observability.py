import json
import logging
import uuid

from gateway.observability import ObservabilityMiddleware


async def test_correlation_and_no_secrets_in_logs(client, caplog):
    caplog.set_level(logging.INFO, logger='gateway.http')
    response = await client.get('/never-exists/secret-path?password=secret-query', headers={
        'Authorization': 'Bearer secret-auth', 'X-Request-ID': 'secret-client-id'})
    assert response.status_code == 404
    request_id = response.headers['x-request-id']
    assert str(uuid.UUID(request_id)) == request_id
    messages = [json.loads(r.message) for r in caplog.records if r.name == 'gateway.http']
    assert messages[-1]['request_id'] == request_id
    assert messages[-1]['route'] == 'unmatched'
    assert messages[-1]['response_complete']
    assert 'secret-' not in json.dumps(messages)


async def test_normalized_metrics_and_validation_body_exclusion(client, admin_headers, caplog):
    caplog.set_level(logging.INFO, logger='gateway.http')
    response = await client.patch('/admin/api/users/private-user-id', headers=admin_headers, json={'budget_usd': 'secret-body'})
    assert response.status_code == 422
    response = await client.get('/metrics', headers={'Authorization': 'Bearer metrics-' + 'x' * 40})
    assert response.status_code == 200
    assert 'gateway_http_requests_total{method="PATCH",route="/admin/api/users/{user_id}",status="422"}' in response.text
    logs = [r.message for r in caplog.records if r.name == 'gateway.http']
    assert 'secret-body' not in str(logs) and 'private-user-id' not in str(logs)


async def invoke(app, method='GET'):
    sent = []
    async def send(message):
        sent.append(message)
    async def receive():
        return {'type': 'http.request', 'body': b''}
    scope = {'type': 'http', 'method': method, 'path': '/unknown', 'headers': [], 'query_string': b''}
    await ObservabilityMiddleware(app)(scope, receive, send)
    return scope, sent


async def test_inference_reservation_preserved_and_unknown_method_bounded(caplog):
    caplog.set_level(logging.INFO, logger='gateway.http')
    reservation = str(uuid.uuid4())
    async def app(scope, receive, send):
        await send({'type': 'http.response.start', 'status': 200, 'headers': [(b'X-Reservation-ID', reservation.encode())]})
        await send({'type': 'http.response.body', 'body': b'hello', 'more_body': False})
    scope, sent = await invoke(app, method='secret-method')
    headers = dict(sent[0]['headers'])
    assert headers[b'x-reservation-id'].decode() == reservation
    assert headers[b'x-request-id'].decode() == scope['state']['request_id']
    assert 'secret-method' not in caplog.text
    assert '"method":"OTHER"' in caplog.text


async def test_unexpected_error_sanitized_and_correlated(caplog):
    caplog.set_level(logging.INFO, logger='gateway.http')
    async def app(scope, receive, send):
        raise RuntimeError('secret-exception-prompt')
    scope, sent = await invoke(app)
    assert sent[0]['status'] == 500
    assert dict(sent[0]['headers'])[b'x-request-id'].decode() == scope['state']['request_id']
    assert 'secret-exception-prompt' not in str(sent) + caplog.text


async def test_inference_returns_correlation_and_reservation(client, admin_headers):
    import httpx
    from tests.test_gateway import setup_inference, mock_upstream
    headers, _, _ = await setup_inference(client, admin_headers)
    await mock_upstream(lambda req: httpx.Response(200, json={'choices': [], 'usage': {'prompt_tokens': 1, 'completion_tokens': 1}}))
    response = await client.post('/v1/chat/completions', headers=headers, json={'model': 'chat', 'messages': [{'role': 'user', 'content': 'hello'}], 'max_tokens': 10})
    assert response.status_code == 200, response.text
    correlation = str(uuid.UUID(response.headers['x-request-id']))
    reservation = str(uuid.UUID(response.headers['x-reservation-id']))
    assert correlation != reservation
    usage = (await client.get('/admin/api/usage', headers=admin_headers)).json()
    assert usage[0]['reservation_id'] == reservation


def test_cli_disables_raw_access_logging(monkeypatch):
    import sys
    from gateway import cli
    calls = []
    monkeypatch.setattr(sys, 'argv', ['llm-gateway', '--host', '127.0.0.1'])
    monkeypatch.setattr(cli.uvicorn, 'run', lambda *args, **kwargs: calls.append(kwargs))
    cli.main()
    assert calls[0]['access_log'] is False and calls[0]['proxy_headers'] is False
    config = calls[0]['log_config']
    assert config['formatters']['metadata']['format'] == '%(message)s'
    assert config['loggers']['gateway.http']['level'] == 'INFO'


async def test_cancellation_propagates_and_partial_stream_stays_incomplete(caplog):
    import asyncio
    import pytest
    caplog.set_level(logging.INFO, logger='gateway.http')
    async def cancelled(scope, receive, send):
        raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await invoke(cancelled)
    assert '"status":"499"' in caplog.text
    assert '"response_complete":false' in caplog.text
    async def partial(scope, receive, send):
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b'partial', 'more_body': True})
        raise ValueError('secret-stream-error')
    with pytest.raises(RuntimeError, match='Gateway response interrupted') as exc:
        await invoke(partial)
    assert exc.value.__suppress_context__
    assert 'secret-stream-error' not in caplog.text
    assert '"response_complete":false' in caplog.text
