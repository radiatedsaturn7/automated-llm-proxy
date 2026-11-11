import asyncio
from datetime import timedelta

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select, text

from gateway import db, provider_health
from gateway.config import get_settings
from test_gateway import mock_upstream, setup_inference

CHAT = {'model': 'chat', 'messages': [{'role': 'user', 'content': 'hello'}]}


async def primary(client, admin_headers, monkeypatch):
    headers, _, provider = await setup_inference(client, admin_headers)
    monkeypatch.setattr(get_settings(), 'circuit_failure_threshold', 1)
    return headers, provider['id']


async def expire(provider_id):
    async with db.transaction() as s:
        row = await s.get(db.ProviderHealth, (provider_id, 'chat'))
        row.open_until = db.now() - timedelta(seconds=10)
        row.probe_until = db.now() - timedelta(seconds=10)


async def state(provider_id):
    async with db.session_factory() as s:
        return await s.get(db.ProviderHealth, (provider_id, 'chat'))


async def test_next_request_fallback_not_in_request_retry(client, admin_headers, monkeypatch):
    headers, pid = await primary(client, admin_headers, monkeypatch)
    result = await client.post('/admin/api/providers', headers=admin_headers, json={
        'name': 'backup', 'base_url': 'https://backup.example/v1', 'kind': 'openai', 'api_key': 'backup-key',
        'priority': 1, 'models': {'chat': {'upstream_model': 'backup-model', 'input_cost_per_million': 1, 'output_cost_per_million': 2}}})
    assert result.status_code == 201
    seen = []
    def handler(req):
        seen.append(req.url.host)
        return httpx.Response(500) if req.url.host == 'provider.example' else httpx.Response(200, json={'choices': [], 'usage': {'prompt_tokens': 1, 'completion_tokens': 1}})
    await mock_upstream(handler)
    assert (await client.post('/v1/chat/completions', headers=headers, json=CHAT)).status_code == 502
    assert seen == ['provider.example']
    assert (await state(pid)).state == 'open'
    assert (await client.post('/v1/chat/completions', headers=headers, json=CHAT)).status_code == 200
    assert seen == ['provider.example', 'backup.example']
    usage = (await client.get('/admin/api/usage', headers=admin_headers)).json()
    assert len(usage) == 2 and sum(u['estimated'] for u in usage) == 1
    health = await client.get(f'/admin/api/providers/{pid}/health', headers=admin_headers)
    assert health.status_code == 200 and health.json()['routes'][0]['last_outcome'] == 'failure'
    assert (await client.get(f'/admin/api/providers/{pid}/health', headers=headers)).status_code == 403


async def test_single_half_open_probe_and_fenced_late_result(client, admin_headers, monkeypatch):
    _, pid = await primary(client, admin_headers, monkeypatch)
    _, old = await provider_health.choose('chat', 'chat/completions')
    await provider_health.record(old, 'failure')
    await expire(pid)
    async def choose():
        try:
            return (await provider_health.choose('chat', 'chat/completions'))[1]
        except HTTPException as exc:
            assert exc.status_code == 503 and int(exc.headers['Retry-After']) > 0
            return None
    tickets = await asyncio.gather(*(choose() for _ in range(20)))
    winners = [ticket for ticket in tickets if ticket]
    assert len(winners) == 1
    await provider_health.record(old, 'success')
    assert (await state(pid)).state == 'probing'
    await provider_health.record(winners[0], 'success')
    assert (await state(pid)).state == 'closed' and (await state(pid)).failures == 0


async def test_expired_probe_lease_is_recoverable_and_old_probe_cannot_close(client, admin_headers, monkeypatch):
    _, pid = await primary(client, admin_headers, monkeypatch)
    _, ticket = await provider_health.choose('chat', 'chat/completions')
    await provider_health.record(ticket, 'failure')
    await expire(pid)
    _, crashed_probe = await provider_health.choose('chat', 'chat/completions')
    await expire(pid)
    _, replacement = await provider_health.choose('chat', 'chat/completions')
    assert crashed_probe.probe_id != replacement.probe_id
    await provider_health.record(crashed_probe, 'success')
    assert (await state(pid)).probe_id == replacement.probe_id
    await provider_health.record(replacement, 'failure')
    assert (await state(pid)).state == 'open'


async def test_provider_edit_and_operator_reset_fence_old_results(client, admin_headers, monkeypatch):
    _, pid = await primary(client, admin_headers, monkeypatch)
    _, ticket = await provider_health.choose('chat', 'chat/completions')
    await provider_health.record(ticket, 'configuration_error')
    assert (await state(pid)).state == 'open'
    r = await client.patch(f'/admin/api/providers/{pid}', headers=admin_headers, json={'api_key': 'new-provider-key'})
    assert r.status_code == 200
    await provider_health.record(ticket, 'failure')
    assert (await state(pid)).state == 'closed'
    _, ticket2 = await provider_health.choose('chat', 'chat/completions')
    await provider_health.record(ticket2, 'failure')
    assert (await client.post(f'/admin/api/providers/{pid}/health/reset', headers=admin_headers)).status_code == 200
    assert (await state(pid)).state == 'closed'


async def test_budget_denial_releases_undispatched_probe(client, admin_headers, monkeypatch):
    headers, pid = await primary(client, admin_headers, monkeypatch)
    _, ticket = await provider_health.choose('chat', 'chat/completions')
    await provider_health.record(ticket, 'failure')
    await expire(pid)
    async with db.transaction() as s:
        team = await s.scalar(select(db.Team))
        team.budget_micros = 0
    seen = []
    await mock_upstream(lambda request: seen.append(request))
    result = await client.post('/v1/chat/completions', headers=headers, json=CHAT)
    assert result.status_code == 429 and seen == []
    row = await state(pid)
    assert row.state == 'open' and row.probe_id is None
    # Another request can probe after fixing admission, without waiting the lease.
    assert (await provider_health.choose('chat', 'chat/completions'))[1].probe_id


async def test_rejections_do_not_count_and_retry_after_is_bounded(client, admin_headers, monkeypatch):
    _, pid = await primary(client, admin_headers, monkeypatch)
    _, ticket = await provider_health.choose('chat', 'chat/completions')
    await provider_health.record(ticket, 'rejected')
    assert (await state(pid)).state == 'closed' and (await state(pid)).failures == 0
    await provider_health.record(ticket, 'rate_limited', '999999999999')
    row = await state(pid)
    assert row.state == 'open'
    assert (row.open_until.replace(tzinfo=db.now().tzinfo) - db.now()).total_seconds() <= 3600


async def test_breaker_can_be_explicitly_disabled(client, admin_headers, monkeypatch):
    _, pid = await primary(client, admin_headers, monkeypatch)
    _, ticket = await provider_health.choose('chat', 'chat/completions')
    await provider_health.record(ticket, 'failure')
    monkeypatch.setattr(get_settings(), 'circuit_breaker_enabled', False)
    selected, no_ticket = await provider_health.choose('chat', 'chat/completions')
    assert selected.id == pid and no_ticket is None


async def test_postgres_distinct_sessions_elect_one_probe(client, admin_headers, monkeypatch):
    if db.engine.dialect.name != 'postgresql':
        pytest.skip('CI postgres-tests executes shared-database election')
    _, pid = await primary(client, admin_headers, monkeypatch)
    # Verify actual separate backend connections, not an in-process asyncio lock.
    async def backend():
        async with db.session_factory() as s:
            value = await s.scalar(text('SELECT pg_backend_pid()'))
            await s.execute(text('SELECT pg_sleep(0.02)'))
            return value
    assert len(set(await asyncio.gather(*(backend() for _ in range(4))))) >= 2
    _, ticket = await provider_health.choose('chat', 'chat/completions')
    await provider_health.record(ticket, 'failure')
    await expire(pid)
    async def contender():
        try:
            return (await provider_health.choose('chat', 'chat/completions'))[1]
        except HTTPException as exc:
            assert exc.status_code == 503
            return None
    results = await asyncio.gather(*(contender() for _ in range(25)))
    assert sum(result is not None for result in results) == 1


async def test_probe_lease_covers_configured_provider_timeout(client, admin_headers, monkeypatch):
    _, pid = await primary(client, admin_headers, monkeypatch)
    cfg = get_settings()
    monkeypatch.setattr(cfg, 'provider_timeout_seconds', 600)
    monkeypatch.setattr(cfg, 'circuit_probe_seconds', 150)
    assert cfg.effective_circuit_probe_seconds == 630
    _, ticket = await provider_health.choose('chat', 'chat/completions')
    await provider_health.record(ticket, 'failure')
    await expire(pid)
    await provider_health.choose('chat', 'chat/completions')
    row = await state(pid)
    from gateway.security import aware
    assert 627 <= (aware(row.probe_until) - db.now()).total_seconds() <= 630
    monkeypatch.setattr(cfg, 'circuit_probe_seconds', 1000)
    assert cfg.effective_circuit_probe_seconds == 1000


async def test_clean_truncated_sse_opens_circuit_without_replay(client, admin_headers, monkeypatch):
    headers, pid = await primary(client, admin_headers, monkeypatch)
    calls = []
    def handler(request):
        calls.append(request)
        # Complete HTTP body but no SSE completion marker: provider outcome unknown.
        return httpx.Response(200, headers={'content-type': 'text/event-stream'},
                              text='data: {"choices":[{"delta":{"content":"partial"}}]}\n\n')
    await mock_upstream(handler)
    response = await client.post('/v1/chat/completions', headers=headers, json={**CHAT, 'stream': True})
    assert response.status_code == 200 and 'Upstream stream interrupted' in response.text
    assert len(calls) == 1 and (await state(pid)).state == 'open'
    usage = (await client.get('/admin/api/usage', headers=admin_headers)).json()
    assert len(usage) == 1 and usage[0]['estimated']
    assert (await client.post('/v1/chat/completions', headers=headers, json=CHAT)).status_code == 503
    assert len(calls) == 1
