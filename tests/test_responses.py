import json

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from gateway import db
from gateway.config import get_settings
from gateway.responses import chat_payload, response_envelope
from test_gateway import mock_upstream, setup_inference


def chat_response(finish='stop'):
    return {'choices': [{'message': {'role': 'assistant', 'content': 'answer'}, 'finish_reason': finish}],
            'usage': {'prompt_tokens': 4, 'completion_tokens': 2}}


@pytest.mark.parametrize('extra', [{'store': True}, {'store': 0}, {'stream': True}, {'stream': 'false'},
                                  {'previous_response_id': None}, {'tools': []}, {'metadata': {}},
                                  {'parallel_tool_calls': False}, {'text': {'format': {'type': 'json_object'}}}])
def test_unsupported_options(extra):
    with pytest.raises(HTTPException):
        chat_payload({'model': 'chat', 'input': 'hello'} | extra)


def test_text_inputs():
    result = chat_payload({'model': 'chat', 'instructions': 'be brief', 'input': [
        {'role': 'user', 'type': 'message', 'content': [{'type': 'input_text', 'text': 'hi'}]},
        {'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'hello'}]},
        {'role': 'user', 'content': 'continue'}], 'max_output_tokens': 32, 'store': False})
    assert result['messages'][0] == {'role': 'system', 'content': 'be brief'}
    assert result['messages'][2] == {'role': 'assistant', 'content': 'hello'}
    assert result['max_tokens'] == 32 and result['stream'] is False


@pytest.mark.parametrize('value', [[], 1, [{'role': 'tool', 'content': 'x'}],
                                  [{'role': 'user', 'content': [{'type': 'input_image', 'image_url': 'x'}]}],
                                  [{'role': 'user', 'content': [{'type': 'input_text', 'text': 'x', 'other': True}]}]])
def test_invalid_input(value):
    with pytest.raises(HTTPException):
        chat_payload({'model': 'chat', 'input': value})


def test_output_envelope_and_rejections():
    for finish, expected in [('stop', 'completed'), ('length', 'incomplete'), ('content_filter', 'incomplete')]:
        result = response_envelope(chat_response(finish), 'alias')
        assert result['status'] == expected
        assert result['store'] is False and result['model'] == 'alias'
        assert result['output'][0]['content'][0]['text'] == 'answer'
        assert result['usage'] == {'input_tokens': 4, 'output_tokens': 2, 'total_tokens': 6}
    data = chat_response()
    data['choices'][0]['message'] = {'role': 'assistant', 'content': None, 'refusal': 'Cannot comply'}
    assert response_envelope(data, 'alias')['output'][0]['content'] == [{'type': 'refusal', 'refusal': 'Cannot comply'}]
    data['choices'][0]['message'] = {'role': 'assistant', 'content': None, 'tool_calls': [{'secret': 'x'}]}
    with pytest.raises(ValueError):
        response_envelope(data, 'alias')


async def test_responses_asgi_accounting(client, admin_headers):
    headers, _, _ = await setup_inference(client, admin_headers)
    seen = []
    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=chat_response(), headers={'x-request-id': 'upstream-123'})
    await mock_upstream(handler)
    response = await client.post('/v1/responses', headers=headers, json={'model': 'chat', 'input': 'hi', 'instructions': 'be brief', 'max_output_tokens': 32})
    assert response.status_code == 200, response.text
    assert response.json()['object'] == 'response' and response.json()['store'] is False
    assert response.json()['output'][0]['content'][0]['text'] == 'answer'
    assert response.headers['x-provider-request-id'] == 'upstream-123'
    assert response.headers['x-reservation-id'] != response.headers['x-request-id']
    assert len(seen) == 1 and seen[0]['messages'][0]['content'] == 'be brief'
    assert 'input' not in seen[0] and 'instructions' not in seen[0]
    async with db.session_factory() as session:
        usage = (await session.scalars(select(db.Usage))).one()
        assert usage.input_tokens == 4 and usage.output_tokens == 2 and not usage.estimated
        assert await session.scalar(select(func.count()).select_from(db.Reservation)) == 1
    assert (await client.get('/v1/responses/' + response.json()['id'], headers=headers)).status_code == 404


async def test_auth_and_rejection_do_not_dispatch(client, admin_headers):
    headers, _, _ = await setup_inference(client, admin_headers)
    def never(request):
        pytest.fail('rejected request dispatched')
    await mock_upstream(never)
    assert (await client.post('/v1/responses', json={'model': 'chat', 'input': 'hi'})).status_code == 401
    assert (await client.post('/v1/responses', headers=headers, json={'model': 'chat', 'input': 'hi', 'store': True})).status_code == 400
    assert (await client.post('/v1/responses', headers=headers, json={'model': 'chat', 'input': 'hi', 'max_output_tokens': True})).status_code == 400
    async with db.session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(db.Reservation)) == 0


async def test_response_invalid_output_sanitized_after_accounting(client, admin_headers):
    headers, _, _ = await setup_inference(client, admin_headers)
    data = chat_response()
    data['choices'][0]['message']['content'] = {'secret': 'do-not-return'}
    await mock_upstream(lambda req: httpx.Response(200, json=data))
    result = await client.post('/v1/responses', headers=headers, json={'model': 'chat', 'input': 'hi'})
    assert result.status_code == 502 and 'do-not-return' not in result.text
    async with db.session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(db.Usage)) == 1


async def test_guardrail_applies_to_instructions_and_input(client, admin_headers, monkeypatch):
    headers, _, _ = await setup_inference(client, admin_headers)
    monkeypatch.setattr(get_settings(), 'guardrails_enabled', True)
    monkeypatch.setattr(get_settings(), 'guardrails_action', 'block')
    monkeypatch.setattr(get_settings(), 'guardrails_detectors', '["email"]')
    await mock_upstream(lambda req: pytest.fail('guardrail rejected request dispatched'))
    for value in ({'instructions': 'private@example.com', 'input': 'hello'}, {'input': 'private@example.com'}):
        result = await client.post('/v1/responses', headers=headers, json={'model': 'chat'} | value)
        assert result.status_code == 403, result.text


async def test_original_body_bound_and_provider_error(client, admin_headers, monkeypatch):
    headers, _, _ = await setup_inference(client, admin_headers)
    monkeypatch.setattr(get_settings(), 'max_request_bytes', 1024)
    seen = []
    def upstream(request):
        seen.append(request)
        return httpx.Response(429, json={'error': {'message': 'secret-provider-message'}})
    await mock_upstream(upstream)
    response = await client.post('/v1/responses', headers=headers, json={'model': 'chat', 'input': 'x' * 1024})
    assert response.status_code == 413 and not seen
    response = await client.post('/v1/responses', headers=headers, json={'model': 'chat', 'input': 'hi'})
    assert response.status_code == 429 and 'secret-provider-message' not in response.text
    assert len(seen) == 1
