import asyncio
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import func, select

from gateway import db, policy, provider_health
from gateway.config import get_settings
from gateway.main import app
from test_gateway import mock_upstream, setup_inference

CHAT = {'model': 'chat', 'messages': [{'role': 'user', 'content': 'hello'}], 'max_tokens': 1}
GOOD = {'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': 'ok'}, 'finish_reason': 'stop'}],
        'usage': {'prompt_tokens': 1, 'completion_tokens': 1}}


async def no_charge():
    async with db.session_factory() as s:
        for model in (db.Reservation, db.Usage, db.RateBucket):
            assert await s.scalar(select(func.count()).select_from(model)) == 0


@pytest.mark.parametrize('constant', ['NaN', 'Infinity', '-Infinity', '1e999'])
@pytest.mark.parametrize('endpoint,raw', [
    ('chat/completions', '{"model":"chat","messages":[{"role":"user","content":"hi"}],"seed":VALUE}'),
    ('chat/completions', '{"model":"chat","messages":[{"role":"user","content":"hi"}],"tools":[{"type":"function","function":{"name":"f","parameters":{"enum":[VALUE]}}}]}'),
    ('embeddings', '{"model":"chat","input":"hi","dimensions":VALUE}'),
    ('responses', '{"model":"chat","input":"hi","top_p":VALUE}'),
])
async def test_invalid_numbers_never_charge_or_poison_route(client, admin_headers, monkeypatch, constant, endpoint, raw):
    headers, _, pid = await setup_inference(client, admin_headers)
    monkeypatch.setattr(get_settings(), 'circuit_failure_threshold', 1)
    seen = []
    def upstream(request):
        seen.append(request)
        return httpx.Response(200, json=GOOD)
    await mock_upstream(upstream)
    response = await client.post('/v1/' + endpoint, headers={**headers, 'content-type': 'application/json'},
                                 content=raw.replace('VALUE', constant))
    assert response.status_code == 400, response.text
    assert seen == []
    await no_charge()
    async with db.session_factory() as s:
        assert await s.get(db.ProviderHealth, (pid['id'], 'chat')) is None
    assert (await client.post('/v1/chat/completions', headers=headers, json=CHAT)).status_code == 200
    assert len(seen) == 1


@pytest.mark.parametrize('raw', [
    b'{', b'{"model":"chat", "model":"other"}', b'\xff',
    b'{"model":"chat","messages":[{"role":"user","content":"\\ud800"}]}',
    b'[' * 70 + b'0' + b']' * 70, b'[' * 2000 + b'0' + b']' * 2000,
    b'{"model":"chat","input":"hello","\\ud800":1}',
])
@pytest.mark.parametrize('endpoint', ['chat/completions', 'embeddings', 'responses'])
async def test_malformed_unicode_and_nesting_rejected(client, admin_headers, raw, endpoint):
    headers, _, _ = await setup_inference(client, admin_headers)
    result = await client.post('/v1/' + endpoint, headers={**headers, 'content-type': 'application/json'}, content=raw)
    assert result.status_code == 400, result.text
    await no_charge()


async def open_probe(provider_id):
    await provider_health.choose('chat', 'chat/completions')
    async with db.transaction() as s:
        row = await s.get(db.ProviderHealth, (provider_id, 'chat'))
        row.state = 'open'
        row.open_until = db.now() - timedelta(seconds=1)


async def assert_abandoned(provider_id):
    async with db.session_factory() as s:
        row = await s.get(db.ProviderHealth, (provider_id, 'chat'))
        assert row.state == 'open' and row.probe_id is None and row.failures == 0


async def test_local_build_failure_abandons_probe_without_reserving(client, admin_headers, monkeypatch):
    headers, _, pid = await setup_inference(client, admin_headers)
    await open_probe(pid['id'])
    await mock_upstream(lambda _: httpx.Response(200, json=GOOD))
    def broken(*args, **kwargs):
        raise ValueError('synthetic private build failure')
    with monkeypatch.context() as patch:
        patch.setattr(app.state.upstream, 'build_request', broken)
        result = await client.post('/v1/chat/completions', headers=headers, json=CHAT)
    assert result.status_code == 400 and 'private' not in result.text
    await no_charge()
    await assert_abandoned(pid['id'])
    assert (await client.post('/v1/chat/completions', headers=headers, json=CHAT)).status_code == 200


async def test_cancelled_predispatch_abandons_probe(client, admin_headers, monkeypatch):
    headers, _, pid = await setup_inference(client, admin_headers)
    await open_probe(pid['id'])
    entered = asyncio.Event()
    async def paused(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()
    with monkeypatch.context() as patch:
        patch.setattr(policy, 'reserve', paused)
        task = asyncio.create_task(client.post('/v1/chat/completions', headers=headers, json=CHAT))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            task.cancel()
            outcome = await asyncio.gather(task, return_exceptions=True)
            assert isinstance(outcome[0], asyncio.CancelledError)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    await asyncio.gather(*tuple(app.state.accounting_tasks))
    await no_charge()
    await assert_abandoned(pid['id'])


async def test_ambiguous_send_failure_remains_conservative(client, admin_headers, monkeypatch):
    headers, _, pid = await setup_inference(client, admin_headers)
    monkeypatch.setattr(get_settings(), 'circuit_failure_threshold', 1)
    seen = []
    def failed(request):
        seen.append(request)
        raise httpx.ReadError('upstream disconnected')
    await mock_upstream(failed)
    result = await client.post('/v1/chat/completions', headers=headers, json=CHAT)
    assert result.status_code == 502 and len(seen) == 1
    async with db.session_factory() as s:
        usage = await s.scalar(select(db.Usage))
        assert usage.estimated and usage.cost_micros > 0
        assert (await s.get(db.ProviderHealth, (pid['id'], 'chat'))).state == 'open'


@pytest.mark.parametrize('endpoint,field', [('chat/completions', 'temperature'), ('responses', 'top_p')])
async def test_unrepresentable_numeric_parameter_is_client_error(client, admin_headers, endpoint, field):
    headers, _, _ = await setup_inference(client, admin_headers)
    payload = dict(CHAT) if endpoint == 'chat/completions' else {'model': 'chat', 'input': 'hi'}
    payload[field] = 10 ** 400
    result = await client.post('/v1/' + endpoint, headers=headers, json=payload)
    assert result.status_code == 400
    await no_charge()


async def test_valid_unicode_surrogate_pair_is_preserved(client, admin_headers):
    headers, _, _ = await setup_inference(client, admin_headers)
    seen = []
    def upstream(request):
        import json
        seen.append(json.loads(request.content)['messages'][0]['content'])
        return httpx.Response(200, json=GOOD)
    await mock_upstream(upstream)
    result = await client.post('/v1/chat/completions', headers={**headers, 'content-type': 'application/json'},
                              content=b'{"model":"chat","messages":[{"role":"user","content":"\\ud83d\\ude00"}]}')
    assert result.status_code == 200
    assert seen == ['\U0001f600']
