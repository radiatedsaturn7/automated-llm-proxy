import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select

from gateway import db
from gateway.providers import StreamTranslator, translate_response, upstream_request
from gateway.security import encrypt
from test_gateway import mock_upstream, setup_inference

CHAT = {'model': 'chat', 'messages': [{'role': 'user', 'content': 'hi'}], 'max_tokens': 64}
ROUTE = {'upstream_model': 'gemini-test'}


def provider():
    return SimpleNamespace(kind='gemini', base_url='https://generativelanguage.googleapis.com/v1beta', encrypted_key=encrypt('provider-secret'))


def reply(finish='STOP', text='hello'):
    candidate = {'index': 0, 'content': {'parts': [{'text': text}]}}
    if finish:
        candidate['finishReason'] = finish
    return {'candidates': [candidate], 'responseId': 'gemini-response',
            'usageMetadata': {'promptTokenCount': 10, 'candidatesTokenCount': 3, 'thoughtsTokenCount': 2}}


async def test_native_request(client):
    url, headers, body = upstream_request(provider(), ROUTE, CHAT | {'stream': True, 'messages': [
        {'role': 'system', 'content': 'concise'}, {'role': 'user', 'content': 'hi'},
        {'role': 'assistant', 'content': 'hello'}], 'stop': ['END'], 'temperature': .3}, 'chat/completions')
    assert url.endswith('/models/gemini-test:streamGenerateContent?alt=sse')
    assert headers['x-goog-api-key'] == 'provider-secret' and 'secret' not in url
    assert body['systemInstruction'] == {'parts': [{'text': 'concise'}]}
    assert body['contents'][1]['role'] == 'model'
    assert body['generationConfig']['maxOutputTokens'] == 64
    assert body['generationConfig']['stopSequences'] == ['END']


@pytest.mark.parametrize('model', ['../escape', 'models/../escape', 'a/b', 'x?key=secret', 'x%2fescape', 'x#bad', 'x:generateContent', ' x'])
async def test_model_path_rejected(client, model):
    with pytest.raises(HTTPException):
        upstream_request(provider(), {'upstream_model': model}, CHAT, 'chat/completions')


@pytest.mark.parametrize('extra', [{'tools': []}, {'reasoning_effort': 'high'}, {'max_completion_tokens': 10},
                                  {'response_format': {'type': 'json_object'}}, {'stop': [1]}, {'top_p': 2}])
async def test_unsupported_parameters(client, extra):
    with pytest.raises(HTTPException):
        upstream_request(provider(), ROUTE, CHAT | extra, 'chat/completions')


async def test_text_response_and_thought_usage(client):
    data = reply()
    data['candidates'][0]['content']['parts'].insert(0, {'text': 'private reasoning', 'thought': True})
    translated = translate_response(provider(), data, 'chat')
    assert translated['choices'][0]['message']['content'] == 'hello'
    assert translated['usage'] == {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15}
    assert translate_response(provider(), {'promptFeedback': {'blockReason': 'SAFETY'}}, 'chat')['choices'][0]['finish_reason'] == 'content_filter'
    for broken in [reply(None), {'error': {'message': 'secret'}}, {'candidates': []}, reply('MALFORMED_FUNCTION_CALL')]:
        with pytest.raises(ValueError):
            translate_response(provider(), broken, 'chat')


async def test_embedding_mapping(client):
    url, headers, body = upstream_request(provider(), {'upstream_model': 'models/gemini-embedding-test'},
                                         {'model': 'embed', 'input': ['a', 'b'], 'dimensions': 2}, 'embeddings')
    assert url.endswith('/models/gemini-embedding-test:batchEmbedContents')
    assert len(body['requests']) == 2 and body['requests'][0]['outputDimensionality'] == 2
    assert body['requests'][0]['model'] == 'models/gemini-embedding-test'
    data = translate_response(provider(), {'embeddings': [{'values': [.2, .3]}, {'values': [.1, .4]}]}, 'embed')
    assert data['data'][1]['index'] == 1 and data['data'][1]['embedding'] == [.1, .4]
    assert data.get('usage') is None  # no invented provider usage
    for payload in [{'encoding_format': 'base64'}, {'dimensions': True}, {'input': ['a'] * 101}]:
        with pytest.raises(HTTPException):
            upstream_request(provider(), ROUTE, {'model': 'embed', 'input': 'a'} | payload, 'embeddings')
    with pytest.raises(ValueError):
        translate_response(provider(), {'embeddings': [{'values': [float('nan')]}]}, 'embed')


async def test_native_stream_and_truncation(client):
    event = reply(None, 'hel')
    final = reply('STOP', 'lo')
    translator = StreamTranslator(provider(), 'chat')
    response = httpx.Response(200, content=('data: ' + json.dumps(event) + '\n\ndata: ' + json.dumps(final) + '\n\n').encode())
    chunks = [chunk async for chunk in translator.iterate(response)]
    assert translator.complete and translator.usage['completion_tokens'] == 5
    assert chunks[-1] == b'data: [DONE]\n\n'
    for suffix in ['', 'data: [DONE]\n\n']:
        translator = StreamTranslator(provider(), 'chat')
        response = httpx.Response(200, content=('data: ' + json.dumps(event) + '\n\n' + suffix).encode())
        with pytest.raises(ValueError):
            _ = [chunk async for chunk in translator.iterate(response)]
        assert not translator.complete


async def test_gateway_native_chat_accounting(client, admin_headers):
    headers, _, registered = await setup_inference(client, admin_headers)
    async with db.transaction() as session:
        row = await session.get(db.Provider, registered['id'])
        row.kind = 'gemini'
        row.base_url = provider().base_url
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=reply())
    await mock_upstream(handler)
    result = await client.post('/v1/chat/completions', headers=headers, json=CHAT)
    assert result.status_code == 200, result.text
    assert seen[0].headers['x-goog-api-key'] == 'provider-secret'
    assert seen[0].url.path.endswith(':generateContent')
    async with db.session_factory() as session:
        usage = (await session.scalars(select(db.Usage))).one()
        assert usage.input_tokens == 10 and usage.output_tokens == 5 and not usage.estimated


async def native_route(client, admin_headers):
    headers, _, registered = await setup_inference(client, admin_headers)
    async with db.transaction() as session:
        row = await session.get(db.Provider, registered['id'])
        row.kind, row.base_url = 'gemini', provider().base_url
    return headers


async def test_gateway_native_embedding(client, admin_headers):
    headers = await native_route(client, admin_headers)
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={'embeddings': [{'values': [.1, .3]}, {'values': [.4, .2]}]})
    await mock_upstream(handler)
    result = await client.post('/v1/embeddings', headers=headers, json={'model': 'chat', 'input': ['first', 'second']})
    assert result.status_code == 200, result.text
    assert seen[0].url.path.endswith(':batchEmbedContents') and len(seen) == 1
    assert result.json()['data'][1]['embedding'] == [.4, .2]
    async with db.session_factory() as session:
        usage = (await session.scalars(select(db.Usage))).one()
        assert usage.estimated and usage.output_tokens == 0


@pytest.mark.parametrize('mode', ['complete', 'truncated', 'error'])
async def test_gateway_native_stream(client, admin_headers, mode):
    headers = await native_route(client, admin_headers)
    payload = reply(None, 'partial')
    wire = 'data: ' + json.dumps(payload) + '\n\n'
    if mode == 'complete':
        wire += 'data: ' + json.dumps(reply()) + '\n\n'
    elif mode == 'error':
        wire += 'data: {"error":{"message":"provider-secret"}}\n\n'
    await mock_upstream(lambda req: httpx.Response(200, content=wire.encode(), headers={'content-type': 'text/event-stream'}))
    result = await client.post('/v1/chat/completions', headers=headers, json=CHAT | {'stream': True})
    assert result.status_code == 200
    assert 'provider-secret' not in result.text
    assert ('[DONE]' in result.text) == (mode == 'complete')
    async with db.session_factory() as session:
        usage = (await session.scalars(select(db.Usage))).one()
        assert usage.estimated == (mode != 'complete')


async def test_unsupported_native_parts_and_usage(client):
    malformed = reply()
    malformed['candidates'][0]['content']['parts'] = [{'functionCall': {'name': 'x'}}]
    with pytest.raises(ValueError):
        translate_response(provider(), malformed, 'chat')
    invalid_usage = reply()
    invalid_usage['usageMetadata']['thoughtsTokenCount'] = True
    assert translate_response(provider(), invalid_usage, 'chat')['usage'] is None
    blocked = reply('SAFETY', 'blocked text')
    assert translate_response(provider(), blocked, 'chat')['choices'][0]['message']['content'] == ''


async def test_wrong_operation_or_embedding_count(client):
    p = provider()
    with pytest.raises(ValueError):
        translate_response(p, {'embeddings': [{'values': [.1]}]}, 'chat', endpoint='chat/completions')
    with pytest.raises(ValueError):
        translate_response(p, reply(), 'embed', endpoint='embeddings')
    for payload in [{'input': ['a', 'b']}, {'input': 'a', 'dimensions': 2}]:
        with pytest.raises(ValueError):
            translate_response(p, {'embeddings': [{'values': [.1]}]}, 'embed', endpoint='embeddings', payload=payload)


async def test_terminal_missing_usage_does_not_reuse_partial_counts(client):
    translator = StreamTranslator(provider(), 'chat')
    translator.event(reply(None))
    final = reply()
    del final['usageMetadata']
    translator.event(final)
    assert translator.complete and translator.usage is None


async def test_gemini_stream_line_bound(client):
    translator = StreamTranslator(provider(), 'chat')
    response = httpx.Response(200, content=b'data: ' + b'x' * 1_048_577)
    with pytest.raises(ValueError):
        _ = [chunk async for chunk in translator.iterate(response)]
