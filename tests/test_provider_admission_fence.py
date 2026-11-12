"""Deterministic configuration changes between route choice and reservation."""
import asyncio

import httpx
import pytest
from sqlalchemy import func, select

from gateway import db, policy
from tests.test_gateway import mock_upstream, setup_inference


@pytest.mark.parametrize('change', ['enabled', 'models', 'pricing', 'key', 'priority', 'base_url', 'kind'])
async def test_stale_provider_never_reserved_or_dispatched(client, admin_headers, monkeypatch, change):
    headers, _, provider = await setup_inference(client, admin_headers)
    entered, release = asyncio.Event(), asyncio.Event()
    original = policy.reserve
    async def paused(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)
    monkeypatch.setattr(policy, 'reserve', paused)
    dispatched = []
    await mock_upstream(lambda request: dispatched.append(request) or httpx.Response(200, json={'choices': []}))
    task = asyncio.create_task(client.post('/v1/chat/completions', headers=headers,
        json={'model': 'chat', 'messages': [{'role': 'user', 'content': 'hello'}], 'max_tokens': 5}))
    await asyncio.wait_for(entered.wait(), 5)
    try:
        patch = {'enabled': {'enabled': False}, 'models': {'models': {'other': {'upstream_model': 'other', 'input_cost_per_million': 1, 'output_cost_per_million': 2}}},
                 'pricing': {'models': {'chat': {'upstream_model': 'model-1', 'input_cost_per_million': 99, 'output_cost_per_million': 2}}},
                 'key': {'api_key': 'replacement-secret'}, 'priority': {'priority': 4}}
        if change in patch:
            response = await client.patch('/admin/api/providers/'+provider['id'], headers=admin_headers, json=patch[change])
            assert response.status_code == 200, response.text
        else:
            # Base URL and kind are immutable through PATCH; operator SQL/migration
            # under the same control-plane gate must still be fenced.
            async with db.transaction() as s:
                current = await s.get(db.Provider, provider['id'])
                setattr(current, change, 'https://other.example/v1' if change == 'base_url' else 'anthropic')
    finally:
        release.set()
    response = await task
    assert response.status_code == 409, response.text
    assert dispatched == [] and 'replacement-secret' not in response.text
    async with db.session_factory() as s:
        for model in (db.Reservation, db.RateBucket, db.Usage):
            assert await s.scalar(select(func.count()).select_from(model)) == 0


async def test_already_admitted_request_finishes_with_captured_configuration(client, admin_headers, monkeypatch):
    headers, _, provider = await setup_inference(client, admin_headers)
    admitted, release = asyncio.Event(), asyncio.Event()
    original = policy.reserve
    async def paused(*args, **kwargs):
        result = await original(*args, **kwargs)
        admitted.set()
        await release.wait()
        return result
    monkeypatch.setattr(policy, 'reserve', paused)
    seen = []
    def upstream(request):
        seen.append(request)
        return httpx.Response(200, json={'choices': [], 'usage': {'prompt_tokens': 1, 'completion_tokens': 1}})
    await mock_upstream(upstream)
    task = asyncio.create_task(client.post('/v1/chat/completions', headers=headers,
        json={'model': 'chat', 'messages': [{'role': 'user', 'content': 'hello'}], 'max_tokens': 5}))
    await asyncio.wait_for(admitted.wait(), 5)
    await client.patch('/admin/api/providers/'+provider['id'], headers=admin_headers, json={'enabled': False})
    release.set()
    assert (await task).status_code == 200
    assert len(seen) == 1 and seen[0].headers['authorization'] == 'Bearer provider-secret'


def test_snapshot_is_immutable_and_does_not_print_sensitive_configuration():
    provider = db.Provider(id='p', enabled=True, kind='openai', base_url='https://example.com',
                           encrypted_key='ciphertext-sensitive', priority=0,
                           models={'chat': {'input_cost_per_million': 1}})
    snapshot = policy.provider_snapshot(provider, 'chat')
    provider.models['chat']['input_cost_per_million'] = 9
    assert policy.provider_snapshot(provider, 'chat') != snapshot
    assert 'ciphertext-sensitive' not in repr(snapshot)


@pytest.mark.parametrize('unrelated_change', [False, True])
async def test_unchanged_selected_route_admits(client, admin_headers, monkeypatch, unrelated_change):
    headers, _, provider = await setup_inference(client, admin_headers)
    original = policy.reserve
    async def paused(*args, **kwargs):
        if unrelated_change:
            models = {**provider['models'], 'other': {'upstream_model': 'other', 'input_cost_per_million': 5, 'output_cost_per_million': 5}}
            assert (await client.patch('/admin/api/providers/'+provider['id'], headers=admin_headers,
                                       json={'models': models})).status_code == 200
        return await original(*args, **kwargs)
    monkeypatch.setattr(policy, 'reserve', paused)
    seen = []
    await mock_upstream(lambda request: seen.append(request) or httpx.Response(200, json={'choices': []}))
    response = await client.post('/v1/chat/completions', headers=headers,
        json={'model': 'chat', 'messages': [{'role': 'user', 'content': 'hello'}]})
    assert response.status_code == 200 and len(seen) == 1


async def test_stale_route_releases_half_open_probe(client, admin_headers, monkeypatch):
    from datetime import timedelta
    headers, _, provider = await setup_inference(client, admin_headers)
    async with db.transaction() as s:
        s.add(db.ProviderHealth(provider_id=provider['id'], model_alias='chat', state='open', failures=3,
                                epoch=1, open_until=db.now()-timedelta(seconds=60)))
    original = policy.reserve
    async def paused(*args, **kwargs):
        async with db.transaction() as s:
            health = await s.get(db.ProviderHealth, (provider['id'], 'chat'))
            assert health.state == 'probing' and health.probe_id
            (await s.get(db.Provider, provider['id'])).base_url = 'https://changed.example/v1'
        return await original(*args, **kwargs)
    monkeypatch.setattr(policy, 'reserve', paused)
    seen = []
    await mock_upstream(lambda request: seen.append(request) or httpx.Response(200, json={'choices': []}))
    response = await client.post('/v1/chat/completions', headers=headers,
        json={'model': 'chat', 'messages': [{'role': 'user', 'content': 'hello'}]})
    assert response.status_code == 409 and seen == []
    async with db.session_factory() as s:
        health = await s.get(db.ProviderHealth, (provider['id'], 'chat'))
        assert health.state == 'open' and health.probe_id is None
