"""Official SDK over real ASGI gateway; only provider network is simulated."""
import base64
import json
import struct

import httpx
import pytest
from sqlalchemy import select

from gateway import db
from gateway.main import app
from test_gateway import mock_upstream, setup_inference

openai = pytest.importorskip('openai')
httpx2 = pytest.importorskip('httpx2')


def sdk(key):
    return openai.AsyncOpenAI(api_key=key, base_url='http://localhost:8080/v1', max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app)))


def completion():
    return {'id': 'chatcmpl-test', 'object': 'chat.completion', 'created': 1, 'model': 'model-1',
        'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': 'hello'}, 'finish_reason': 'stop'}],
        'usage': {'prompt_tokens': 3, 'completion_tokens': 2, 'total_tokens': 5}}


async def ledger(count=1):
    async with db.session_factory() as session:
        rows = (await session.scalars(select(db.Usage))).all()
        assert len(rows) == count
        return rows


async def test_sdk_chat_and_request_ids(client, admin_headers):
    _, key, _ = await setup_inference(client, admin_headers)
    seen = []
    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=completion())
    await mock_upstream(handler)
    async with sdk(key['key']) as api:
        response = await api.chat.completions.with_raw_response.create(model='chat',
            messages=[{'role': 'user', 'content': 'hi'}], max_completion_tokens=16)
        result = response.parse()
        if hasattr(result, '__await__'):
            result = await result
        assert result.choices[0].message.content == 'hello'
        assert result.usage.total_tokens == 5
        assert result._request_id == response.headers['x-request-id']
        assert response.headers['x-reservation-id'] != result._request_id
    assert len(seen) == 1 and seen[0]['max_completion_tokens'] == 16
    assert (await ledger())[0].cost_micros == 7


async def test_sdk_stream_chunks_and_usage(client, admin_headers):
    _, key, _ = await setup_inference(client, admin_headers)
    base = {'id': 'chatcmpl-stream', 'object': 'chat.completion.chunk', 'created': 1, 'model': 'model-1'}
    events = [base | {'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': 'hello'}, 'finish_reason': None}]},
        base | {'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]},
        base | {'choices': [], 'usage': {'prompt_tokens': 3, 'completion_tokens': 2, 'total_tokens': 5}}]
    async def handler(request):
        assert json.loads(request.content)['stream_options']['include_usage'] is True
        content = ''.join('data: ' + json.dumps(event) + '\n\n' for event in events) + 'data: [DONE]\n\n'
        return httpx.Response(200, content=content, headers={'content-type': 'text/event-stream'})
    await mock_upstream(handler)
    async with sdk(key['key']) as api:
        stream = await api.chat.completions.create(model='chat', messages=[{'role': 'user', 'content': 'hi'}],
            stream=True, stream_options={'include_usage': True}, max_tokens=16)
        async with stream:
            chunks = [chunk async for chunk in stream]
    assert ''.join(c.choices[0].delta.content or '' for c in chunks if c.choices) == 'hello'
    assert chunks[-1].usage.total_tokens == 5
    assert not (await ledger())[0].estimated


@pytest.mark.parametrize('encoding', [None, 'float'])
async def test_sdk_embeddings(client, admin_headers, encoding):
    _, key, _ = await setup_inference(client, admin_headers)
    def handler(request):
        body = json.loads(request.content)
        expected = encoding or 'base64'
        assert body['encoding_format'] == expected
        vector = [0.25, -0.5]
        if expected == 'base64':
            vector = base64.b64encode(struct.pack('<ff', *vector)).decode()
        return httpx.Response(200, json={'object': 'list', 'model': 'model-1',
            'data': [{'object': 'embedding', 'index': 0, 'embedding': vector}],
            'usage': {'prompt_tokens': 3, 'total_tokens': 3}})
    await mock_upstream(handler)
    async with sdk(key['key']) as api:
        kwargs = {'encoding_format': encoding} if encoding else {}
        result = await api.embeddings.create(model='chat', input=['hello'], **kwargs)
        assert result.data[0].embedding == [0.25, -0.5]
        assert result.usage.prompt_tokens == 3
    assert (await ledger())[0].cost_micros == 3


async def test_sdk_responses_output_text(client, admin_headers):
    _, key, _ = await setup_inference(client, admin_headers)
    await mock_upstream(lambda request: httpx.Response(200, json=completion()))
    async with sdk(key['key']) as api:
        result = await api.responses.create(model='chat', instructions='Be concise', input='hi', store=False,
                                            max_output_tokens=16)
        assert result.output_text == 'hello'
        assert result.status == 'completed' and result.store is False
        assert result.usage.total_tokens == 5
    assert (await ledger())[0].cost_micros == 7


@pytest.mark.parametrize('case,exception,status', [('auth', openai.AuthenticationError, 401),
    ('budget', openai.RateLimitError, 429), ('stateful', openai.BadRequestError, 400)])
async def test_sdk_local_errors_do_not_dispatch(client, admin_headers, case, exception, status):
    _, key, _ = await setup_inference(client, admin_headers, budget=0 if case == 'budget' else 1)
    await mock_upstream(lambda request: pytest.fail('Rejected request dispatched'))
    async with sdk('invalid' if case == 'auth' else key['key']) as api:
        with pytest.raises(exception) as caught:
            if case == 'stateful':
                await api.responses.create(model='chat', input='hi', store=True)
            else:
                await api.chat.completions.create(model='chat', messages=[{'role': 'user', 'content': 'hi'}])
        assert caught.value.status_code == status and caught.value.request_id
    await ledger(0)


async def test_sdk_upstream_error_no_retry_or_secret(client, admin_headers):
    _, key, _ = await setup_inference(client, admin_headers)
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(500, json={'error': 'upstream-private-data'})
    await mock_upstream(handler)
    async with sdk(key['key']) as api:
        with pytest.raises(openai.InternalServerError) as caught:
            await api.chat.completions.create(model='chat', messages=[{'role': 'user', 'content': 'hi'}])
        assert caught.value.status_code == 502
        assert 'upstream-private-data' not in str(caught.value)
    assert len(calls) == 1
    assert (await ledger())[0].estimated
