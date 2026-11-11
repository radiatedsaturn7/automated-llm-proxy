import json

import httpx
import pytest
from sqlalchemy import func, select

from gateway import db
from gateway.config import get_settings
from gateway.main import app
from test_gateway import mock_upstream, setup_inference


SECRET = 'sk-' + 'a' * 32


def configure(monkeypatch, action):
    monkeypatch.setenv('GUARDRAILS_ENABLED', 'true')
    monkeypatch.setenv('GUARDRAILS_ACTION', action)
    get_settings.cache_clear()


async def no_reservations():
    async with db.session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(db.Reservation)) == 0


async def test_block_before_transmission_or_reservation(client, admin_headers, monkeypatch):
    headers, _, _ = await setup_inference(client, admin_headers)
    configure(monkeypatch, 'block')
    calls = []
    await mock_upstream(lambda request: calls.append(request) or httpx.Response(200, json={}))
    response = await client.post('/v1/chat/completions', headers=headers, json={'model':'chat','messages':[{'role':'user','content':SECRET}]})
    assert response.status_code == 403
    assert SECRET not in response.text
    assert not calls
    await no_reservations()
    audit = await client.get('/admin/api/audit', headers=admin_headers)
    assert SECRET not in audit.text
    assert 'guardrail.block' in audit.text
    assert '"secrets":1' in audit.text


@pytest.mark.parametrize('endpoint', ['chat/completions', 'embeddings'])
async def test_redacted_only_upstream_and_normal_accounting(client, admin_headers, monkeypatch, endpoint):
    headers, _, _ = await setup_inference(client, admin_headers)
    configure(monkeypatch, 'redact')
    sent = []
    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={'choices':[], 'data':[], 'usage':{'prompt_tokens':10,'completion_tokens':0}})
    await mock_upstream(handler)
    body = {'model':'chat', 'input':SECRET} if endpoint == 'embeddings' else {'model':'chat','messages':[{'role':'user','content':SECRET}], 'max_tokens':10}
    response = await client.post('/v1/'+endpoint, headers=headers, json=body)
    assert response.status_code == 200, response.text
    assert SECRET not in json.dumps(sent)
    assert '[REDACTED]' in json.dumps(sent)
    usage = (await client.get('/admin/api/usage', headers=admin_headers)).json()
    assert len(usage) == 1
    assert usage[0]['cost_micros'] == 10
    audit = await client.get('/admin/api/audit', headers=admin_headers)
    assert SECRET not in audit.text
    assert 'guardrail.redact' in audit.text


async def test_default_off_is_unchanged(client, admin_headers):
    headers, _, _ = await setup_inference(client, admin_headers)
    assert not get_settings().guardrails_enabled
    sent = []
    await mock_upstream(lambda request: sent.append(json.loads(request.content)) or httpx.Response(200, json={'choices':[]}))
    response = await client.post('/v1/chat/completions', headers=headers, json={'model':'chat','messages':[{'role':'user','content':SECRET}]})
    assert response.status_code == 200
    assert sent[0]['messages'][0]['content'] == SECRET


@pytest.mark.parametrize('action', ['block','redact'])
async def test_audit_failure_never_dispatches(client, admin_headers, monkeypatch, action):
    headers, _, _ = await setup_inference(client, admin_headers)
    configure(monkeypatch, action)
    sent = []
    await mock_upstream(lambda request: sent.append(request) or httpx.Response(200, json={}))
    async def failed_audit(*args, **kwargs):
        raise RuntimeError('simulated unavailable audit database')
    monkeypatch.setattr(db, 'audit', failed_audit)
    # Production response sanitization; ASGI test default otherwise re-raises server exceptions.
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url='http://localhost:8080') as browser:
        response = await browser.post('/v1/chat/completions', headers=headers, json={'model':'chat','messages':[{'role':'user','content':SECRET}]})
    assert response.status_code == 500
    assert SECRET not in response.text
    assert 'simulated' not in response.text
    assert not sent
    await no_reservations()
