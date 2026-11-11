"""Offline Anthropic client-tool protocol compatibility, no tool execution."""
import copy
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

from gateway import providers

PROVIDER = SimpleNamespace(kind='anthropic', base_url='https://api.anthropic.com/v1', encrypted_key='secret')
ROUTE = {'upstream_model': 'claude-test'}
TOOL = {'type': 'function', 'function': {'name': 'weather', 'description': 'Get weather',
        'parameters': {'type': 'object', 'properties': {'city': {'type': 'string'}}}}}
CALL = {'id': 'call_1', 'type': 'function', 'function': {'name': 'weather', 'arguments': '{"city":"Boston"}'}}
CHAT = {'model': 'alias', 'max_tokens': 128, 'messages': [{'role': 'user', 'content': 'Weather?'}], 'tools': [TOOL]}


@pytest.fixture(autouse=True)
def no_credentials(monkeypatch):
    monkeypatch.setattr(providers, 'decrypt', lambda value: 'test-key')


def request(payload):
    return providers.upstream_request(PROVIDER, ROUTE, payload, 'chat/completions')[2]


@pytest.mark.parametrize('choice,native', [('auto', {'type': 'auto'}), ('required', {'type': 'any'}),
                                         ('none', {'type': 'none'}),
                                         ({'type': 'function', 'function': {'name': 'weather'}}, {'type': 'tool', 'name': 'weather'})])
def test_tool_definitions_and_choice(choice, native):
    body = request({**CHAT, 'tool_choice': choice})
    assert body['tools'] == [{'name': 'weather', 'description': 'Get weather', 'input_schema': TOOL['function']['parameters']}]
    assert body['tool_choice'] == native
    assert body['model'] == 'claude-test'


def test_parallel_result_round_trip():
    call2 = {**CALL, 'id': 'call_2'}
    messages = [*CHAT['messages'], {'role': 'assistant', 'content': 'Checking', 'tool_calls': [CALL, call2]},
                {'role': 'tool', 'tool_call_id': 'call_2', 'content': 'rain'},
                {'role': 'tool', 'tool_call_id': 'call_1', 'content': 'sun'}]
    body = request({**CHAT, 'messages': messages, 'parallel_tool_calls': False})
    assert body['tool_choice'] == {'type': 'auto', 'disable_parallel_tool_use': True}
    assert body['messages'][1]['content'][1] == {'type': 'tool_use', 'id': 'call_1', 'name': 'weather', 'input': {'city': 'Boston'}}
    assert [x['tool_use_id'] for x in body['messages'][2]['content']] == ['call_2', 'call_1']


@pytest.mark.parametrize('mutation', [
    lambda p: p.update(tool_choice={'type': 'function', 'function': {'name': 'missing'}}),
    lambda p: p['tools'][0]['function'].update(strict=True),
    lambda p: p['tools'].append(p['tools'][0]),
    lambda p: p['tools'][0].update(type='web_search'),
    lambda p: p['tools'][0]['function'].update(parameters={'type': 'array'}),
    lambda p: p['tools'][0]['function'].update(name='bad name'),
    lambda p: p['messages'][0].update(name='silently-ignored'),
    lambda p: p.update(seed=42),
    lambda p: p.update(parallel_tool_calls='false'),
])
def test_unsupported_fields_fail_closed(mutation):
    payload = copy.deepcopy(CHAT)
    mutation(payload)
    with pytest.raises(HTTPException) as err:
        request(payload)
    assert err.value.status_code == 400


@pytest.mark.parametrize('arguments', ['[]', 'null', '{"x":1e309}', '{"x":NaN}', '{"x":1,"x":2}', '{', '"text"'])
def test_invalid_argument_json(arguments):
    call = {**CALL, 'function': {**CALL['function'], 'arguments': arguments}}
    with pytest.raises(HTTPException):
        request({**CHAT, 'messages': [*CHAT['messages'], {'role': 'assistant', 'content': None, 'tool_calls': [call]},
                                    {'role': 'tool', 'tool_call_id': 'call_1', 'content': 'ok'}]})


@pytest.mark.parametrize('tail', [[], [{'role': 'user', 'content': 'continue'}],
    [{'role': 'tool', 'tool_call_id': 'wrong', 'content': 'ok'}],
    [{'role': 'tool', 'tool_call_id': 'call_1', 'content': 'ok'}, {'role': 'tool', 'tool_call_id': 'call_1', 'content': 'duplicate'}]])
def test_dangling_or_wrong_results(tail):
    with pytest.raises(HTTPException):
        request({**CHAT, 'messages': [*CHAT['messages'], {'role': 'assistant', 'content': None, 'tool_calls': [CALL]}, *tail]})


def response_data():
    return {'id': 'message', 'content': [{'type': 'text', 'text': 'Checking'},
            {'type': 'tool_use', 'id': 'call_1', 'name': 'weather', 'input': {'city': 'Boston'}}],
            'stop_reason': 'tool_use', 'usage': {'input_tokens': 20, 'output_tokens': 10}}


def test_tool_response_preserves_usage_and_ids():
    response = providers.translate_response(PROVIDER, response_data(), 'alias')
    assert response['choices'][0]['finish_reason'] == 'tool_calls'
    assert response['choices'][0]['message']['tool_calls'] == [CALL]
    assert response['choices'][0]['message']['content'] == 'Checking'
    assert response['usage']['total_tokens'] == 30


@pytest.mark.parametrize('mutation', [lambda d: d.update(stop_reason='end_turn'), lambda d: d.update(stop_reason=None),
    lambda d: d['content'][1].update(input=[]), lambda d: d['content'].append(d['content'][1]),
    lambda d: d['content'][1].update(type='server_tool_use'), lambda d: d.update(stop_reason='pause_turn')])
def test_bad_tool_response_rejected(mutation):
    data = response_data()
    mutation(data)
    with pytest.raises(ValueError):
        providers.translate_response(PROVIDER, data, 'alias')


def events():
    return [
        {'type': 'message_start', 'message': {'id': 'message', 'content': [], 'usage': {'input_tokens': 20}}},
        {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}},
        {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': 'Checking'}},
        {'type': 'content_block_stop', 'index': 0},
        {'type': 'content_block_start', 'index': 1, 'content_block': {'type': 'tool_use', 'id': 'call_1', 'name': 'weather', 'input': {}}},
        {'type': 'content_block_delta', 'index': 1, 'delta': {'type': 'input_json_delta', 'partial_json': '{"city":'}},
        {'type': 'content_block_delta', 'index': 1, 'delta': {'type': 'input_json_delta', 'partial_json': '"Boston"}'}},
        {'type': 'content_block_stop', 'index': 1},
        {'type': 'message_delta', 'delta': {'stop_reason': 'tool_use'}, 'usage': {'output_tokens': 10}},
        {'type': 'message_stop'}]


async def stream(sequence):
    translator = providers.StreamTranslator(PROVIDER, 'alias')
    response = httpx.Response(200, content=''.join('data: '+json.dumps(event)+'\n\n' for event in sequence).encode())
    chunks = [chunk async for chunk in translator.iterate(response)]
    return translator, chunks


async def test_stream_tools_indexed_independently_of_text_blocks():
    translator, chunks = await stream(events())
    result = [json.loads(chunk.decode()[6:]) for chunk in chunks[:-1]]
    calls = [item['choices'][0]['delta']['tool_calls'][0] for item in result if 'tool_calls' in item['choices'][0]['delta']]
    assert all(call['index'] == 0 for call in calls)
    assert calls[0]['id'] == 'call_1'
    assert ''.join(call['function']['arguments'] for call in calls) == CALL['function']['arguments']
    assert result[-1]['choices'][0]['finish_reason'] == 'tool_calls'
    assert translator.usage['total_tokens'] == 30 and translator.complete
    assert chunks[-1] == b'data: [DONE]\n\n'


@pytest.mark.parametrize('mutation', [lambda e: e.pop(), lambda e: e.pop(7),
    lambda e: e[5].update(index=99), lambda e: e[6]['delta'].update(partial_json='broken'),
    lambda e: e[4]['content_block'].update(type='thinking'),
    lambda e: e[8]['delta'].update(stop_reason='pause_turn'),
    lambda e: e.insert(8, {'type': 'surprise'}), lambda e: e.pop(0)])
async def test_bad_streams_do_not_complete(mutation):
    sequence = events()
    mutation(sequence)
    with pytest.raises(ValueError):
        await stream(sequence)


async def test_done_cannot_replace_native_message_stop():
    sequence = events()[:-1]
    response = httpx.Response(200, content=(''.join('data: '+json.dumps(e)+'\n\n' for e in sequence)+'data: [DONE]\n\n').encode())
    translator = providers.StreamTranslator(PROVIDER, 'alias')
    with pytest.raises(ValueError):
        _ = [chunk async for chunk in translator.iterate(response)]
    assert not translator.complete


@pytest.mark.parametrize('streaming', [False, True])
async def test_tools_gateway_dispatch_and_accounting(client, admin_headers, streaming):
    from sqlalchemy import select
    from gateway import db
    from test_gateway import setup_inference, mock_upstream
    headers, _, registered = await setup_inference(client, admin_headers)
    async with db.transaction() as session:
        provider = await session.get(db.Provider, registered['id'])
        provider.kind = 'anthropic'
    sent = []
    def handler(req):
        sent.append(json.loads(req.content))
        if streaming:
            return httpx.Response(200, headers={'content-type': 'text/event-stream'},
                content=''.join('data: '+json.dumps(event)+'\n\n' for event in events()).encode())
        return httpx.Response(200, json=response_data())
    await mock_upstream(handler)
    response = await client.post('/v1/chat/completions', headers=headers,
                                 json={**CHAT, 'model': 'chat', 'stream': streaming})
    assert response.status_code == 200, response.text
    assert sent[0]['tools'][0]['name'] == 'weather'
    if streaming:
        assert 'tool_calls' in response.text and 'data: [DONE]' in response.text
    else:
        assert response.json()['choices'][0]['message']['tool_calls'] == [CALL]
    async with db.session_factory() as session:
        usage = await session.scalar(select(db.Usage))
        assert usage.input_tokens == 20 and usage.output_tokens == 10


async def test_two_streamed_tools_keep_distinct_indices_and_empty_arguments():
    sequence = events()
    sequence[8:8] = [
        {'type': 'content_block_start', 'index': 2, 'content_block': {'type': 'tool_use', 'id': 'call_2', 'name': 'weather', 'input': {}}},
        {'type': 'content_block_stop', 'index': 2},
    ]
    _, chunks = await stream(sequence)
    calls = [json.loads(c.decode()[6:])['choices'][0]['delta'].get('tool_calls', []) for c in chunks[:-1]]
    second = [call for batch in calls for call in batch if call['index'] == 1]
    assert second[0]['id'] == 'call_2'
    assert ''.join(call['function']['arguments'] for call in second) == '{}'


async def test_nonfinite_stream_arguments_rejected():
    sequence = events()
    sequence[5]['delta']['partial_json'] = '{"n":'
    sequence[6]['delta']['partial_json'] = '1e309}'
    with pytest.raises(ValueError):
        await stream(sequence)


@pytest.mark.parametrize('raw', ['{"type":"message_start","type":"message_stop"}',
                                 '{"type":"message_start","message":{"usage":{"input_tokens":1e309}}}'])
async def test_ambiguous_or_nonfinite_sse_json_rejected(raw):
    translator = providers.StreamTranslator(PROVIDER, 'alias')
    response = httpx.Response(200, content=('data: '+raw+'\n\n').encode())
    with pytest.raises(ValueError):
        _ = [chunk async for chunk in translator.iterate(response)]
