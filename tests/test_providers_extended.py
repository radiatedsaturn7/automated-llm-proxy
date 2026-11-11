import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from starlette.requests import Request

from gateway import db
from gateway.inference import infer, validate_payload
from gateway.main import app
from gateway.providers import StreamTranslator, response_headers, upstream_request
from gateway.security import encrypt
from test_gateway import mock_upstream, setup_inference


CHAT = {"model": "chat", "messages": [{"role": "user", "content": "hi"}]}
ROUTE = {"upstream_model": "deployment-1", "input_cost_per_million": 1, "output_cost_per_million": 2}


def provider(kind):
    return SimpleNamespace(kind=kind, base_url="https://resource.openai.azure.com/openai/v1", encrypted_key=encrypt("secret"))


async def test_azure_v1_adapter(client):
    p = provider("azure_openai")
    for endpoint in ("chat/completions", "embeddings"):
        url, headers, body = upstream_request(p, ROUTE, {"model": "chat", "max_completion_tokens": 100}, endpoint)
        assert url == p.base_url + "/" + endpoint
        assert headers["api-key"] == "secret" and "Authorization" not in headers
        assert body["model"] == "deployment-1"
        assert body["max_completion_tokens"] == 100 and "max_tokens" not in body
    _, _, body = upstream_request(p, ROUTE, {**CHAT, "stream": True}, "chat/completions")
    assert body["stream_options"] == {"include_usage": True}
    translator = StreamTranslator(p, "chat")
    event = {"choices": [], "usage": {"prompt_tokens": 2, "completion_tokens": 3}}
    assert translator.event(event) == event and translator.usage == event["usage"]


async def test_azure_embedded_route_dispatch(client, admin_headers):
    headers, _, registered = await setup_inference(client, admin_headers)
    async with db.transaction() as s:
        row = await s.get(db.Provider, registered["id"])
        row.kind = "azure_openai"
        row.base_url = "https://resource.openai.azure.com/openai/v1"
    seen = []
    def handler(req):
        seen.append(req)
        return httpx.Response(200, json={"data": [{"embedding": [0.1]}], "usage": {"prompt_tokens": 2}}, headers={"apim-request-id": "azure-req-123"})
    await mock_upstream(handler)
    r = await client.post('/v1/embeddings', headers=headers, json={"model": "chat", "input": "hello"})
    assert r.status_code == 200
    assert r.headers['x-provider-request-id'] == 'azure-req-123'
    assert len(seen) == 1 and seen[0].headers['api-key'] == 'provider-secret'
    assert seen[0].url.path == '/openai/v1/embeddings'


async def test_reasoning_tool_call_round_trip(client, admin_headers):
    headers, _, _ = await setup_inference(client, admin_headers)
    messages = [
        {"role": "user", "content": "Weather?"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "weather", "arguments": '{"city":"Boston"}'}}]},
        {"role": "tool", "content": "Sunny", "tool_call_id": "call_1"},
    ]
    seen = []
    def handler(req):
        seen.append(json.loads(req.content))
        return httpx.Response(200, json={"choices": [], "usage": {"prompt_tokens": 8, "completion_tokens": 4}}, headers={"x-request-id": "req-reasoning-1"})
    await mock_upstream(handler)
    r = await client.post('/v1/chat/completions', headers=headers, json={"model": "chat", "messages": messages, "max_completion_tokens": 100, "reasoning_effort": "high", "parallel_tool_calls": False})
    assert r.status_code == 200
    assert seen[0]['messages'] == messages
    assert seen[0]['max_completion_tokens'] == 100 and 'max_tokens' not in seen[0]
    assert r.headers['x-provider-request-id'] == 'req-reasoning-1'


@pytest.mark.parametrize('message', [
    {"role": "assistant", "content": None},
    {"role": "user", "content": None, "tool_calls": []},
    {"role": "tool", "content": "ok"},
    {"role": "assistant", "content": None, "tool_calls": [{"id": "x", "type": "function", "function": {"name": "f", "arguments": {}}}]},
    {"role": "user", "content": "ok", "tool_call_id": "wrong"},
])
async def test_invalid_tool_messages_fail_before_dispatch(client, message):
    with pytest.raises(HTTPException) as exc:
        validate_payload({"model": "chat", "messages": [message]}, "chat/completions")
    assert exc.value.status_code == 400


@pytest.mark.parametrize('field,value', [('seed', 2), ('presence_penalty', 0), ('tools', []), ('max_completion_tokens', 20), ('reasoning_effort', 'high')])
async def test_anthropic_rejects_unsupported_options(client, field, value):
    with pytest.raises(HTTPException) as exc:
        upstream_request(provider('anthropic'), ROUTE, {**CHAT, 'max_tokens': 10, field: value}, 'chat/completions')
    assert exc.value.status_code == 400


async def test_request_ids_sanitized(client):
    for bad in ('secret prompt with spaces', 'a' * 129, 'bad\r\nheader'):
        result = response_headers(httpx.Response(500, headers={'x-request-id': bad}), 'gateway-id')
        assert result == {'X-Reservation-ID': 'gateway-id', 'Cache-Control': 'no-store'}


@pytest.mark.parametrize('status', [200, 302, 400, 429, 500])
async def test_upstream_errors_are_redacted_and_not_retried(client, admin_headers, status):
    headers, _, _ = await setup_inference(client, admin_headers)
    calls = []
    def handler(req):
        calls.append(req)
        return httpx.Response(status, json={'error': {'message': 'PRIVATE prompt credential'}}, headers={'x-request-id': 'req-safe-1', 'set-cookie': 'secret-cookie', 'location': 'https://evil.example/'})
    await mock_upstream(handler)
    r = await client.post('/v1/chat/completions', headers=headers, json=CHAT)
    assert r.status_code == (429 if status == 429 else 502)
    assert 'PRIVATE' not in r.text and 'secret-cookie' not in str(r.headers)
    assert r.headers['x-provider-request-id'] == 'req-safe-1'
    assert len(calls) == 1


async def test_disconnect_during_stream_settles_conservatively(client, admin_headers):
    headers, _, _ = await setup_inference(client, admin_headers)
    entered = asyncio.Event()
    closed = asyncio.Event()
    class Interrupted(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'
            entered.set()
            await asyncio.Event().wait()
        async def aclose(self):
            closed.set()
    await mock_upstream(lambda req: httpx.Response(200, stream=Interrupted()))
    async def receive():
        return {'type': 'http.request', 'body': json.dumps({**CHAT, 'stream': True}).encode(), 'more_body': False}
    scope = {'type': 'http', 'app': app, 'method': 'POST', 'path': '/v1/chat/completions', 'headers': [(b'authorization', headers['Authorization'].encode())]}
    response = await infer(Request(scope, receive), 'chat/completions')
    iterator = response.body_iterator
    assert b'hello' in await anext(iterator)
    reading = asyncio.create_task(anext(iterator))
    await asyncio.wait_for(entered.wait(), 2)
    reading.cancel()
    with pytest.raises(asyncio.CancelledError):
        await reading
    await asyncio.wait_for(closed.wait(), 2)
    if app.state.accounting_tasks:
        await asyncio.gather(*list(app.state.accounting_tasks))
    usage = (await client.get('/admin/api/usage', headers=admin_headers)).json()
    assert len(usage) == 1 and usage[0]['estimated']
    reservations = (await client.get('/admin/api/reservations', headers=admin_headers)).json()
    assert reservations[0]['status'] == 'settled'


async def test_real_tls_upstream_dispatch(client, admin_headers, tmp_path):
    """Actual local TCP+TLS, not MockTransport; no credentials leave the machine."""
    import ipaddress
    import ssl
    from datetime import datetime, timedelta, timezone
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'localhost')])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(datetime.now(timezone.utc) - timedelta(minutes=1))
            .not_valid_after(datetime.now(timezone.utc) + timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]), critical=False)
            .sign(key, hashes.SHA256()))
    cert_path, key_path = tmp_path / 'server.crt', tmp_path / 'server.key'
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    key_path.chmod(0o600)
    server_ssl = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ssl.load_cert_chain(cert_path, key_path)
    seen = []
    async def handle(reader, writer):
        try:
            header_bytes = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 3)
            head = header_bytes.decode().split('\r\n')
            fields = dict(line.split(': ', 1) for line in head[1:] if ': ' in line)
            count = int(next(v for k, v in fields.items() if k.lower() == 'content-length'))
            body = await reader.readexactly(count)
            seen.append((head[0], {k.lower(): v for k, v in fields.items()}, json.loads(body)))
            result = json.dumps({'choices': [], 'usage': {'prompt_tokens': 1, 'completion_tokens': 2}}).encode()
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nX-Request-ID: local-tls-123\r\nConnection: close\r\nContent-Length: ' + str(len(result)).encode() + b'\r\n\r\n' + result)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
    try:
        server = await asyncio.start_server(handle, '127.0.0.1', 0, ssl=server_ssl)
    except PermissionError:
        pytest.skip('Execution sandbox disallows local TCP listeners; CI executes this test')
    async with server:
        headers, _, registered = await setup_inference(client, admin_headers)
        port = server.sockets[0].getsockname()[1]
        async with db.transaction() as s:
            row = await s.get(db.Provider, registered['id'])
            row.base_url = f'https://127.0.0.1:{port}/v1'
        await app.state.upstream.aclose()
        app.state.upstream = httpx.AsyncClient(verify=ssl.create_default_context(cafile=str(cert_path)), trust_env=False)
        result = await client.post('/v1/chat/completions', headers=headers, json={**CHAT, 'max_completion_tokens': 20})
        assert result.status_code == 200, result.text
        assert result.headers['x-provider-request-id'] == 'local-tls-123'
        assert len(seen) == 1 and seen[0][0] == 'POST /v1/chat/completions HTTP/1.1'
        assert seen[0][1]['authorization'] == 'Bearer provider-secret'
        assert seen[0][2]['max_completion_tokens'] == 20


async def test_cancel_during_dispatch_settles_without_retry(client, admin_headers):
    headers, _, _ = await setup_inference(client, admin_headers)
    entered = asyncio.Event()
    calls = []
    async def handler(req):
        calls.append(req)
        entered.set()
        await asyncio.Event().wait()
    await mock_upstream(handler)
    async def receive():
        return {'type': 'http.request', 'body': json.dumps(CHAT).encode(), 'more_body': False}
    scope = {'type': 'http', 'app': app, 'method': 'POST', 'path': '/v1/chat/completions', 'headers': [(b'authorization', headers['Authorization'].encode())]}
    task = asyncio.create_task(infer(Request(scope, receive), 'chat/completions'))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    if app.state.accounting_tasks:
        await asyncio.gather(*list(app.state.accounting_tasks))
    assert len(calls) == 1
    usage = (await client.get('/admin/api/usage', headers=admin_headers)).json()
    assert len(usage) == 1 and usage[0]['estimated']


async def test_sdk_stream_options(client):
    body = {**CHAT, 'stream': True, 'stream_options': {'include_usage': True}}
    assert validate_payload(body, 'chat/completions')['stream_options'] == {'include_usage': True}
    for invalid in (None, {'include_usage': 'yes'}, {'unknown': True}):
        with pytest.raises(HTTPException):
            validate_payload({**CHAT, 'stream': True, 'stream_options': invalid}, 'chat/completions')
