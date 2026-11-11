import secrets
from datetime import timedelta
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from gateway import db
from gateway.config import get_settings
from gateway.enroll import enroll, EnrollmentError
from gateway.relay_journal import CredentialJournal, RotationError
from gateway.security import digest


def secret():
    return 'lg_' + secrets.token_urlsafe(32)


async def context(client, admin_headers, monkeypatch):
    monkeypatch.setenv('ENROLLMENT_ENABLED', 'true')
    monkeypatch.setenv('ENROLLMENT_ALLOWED_MODELS', '["chat"]')
    get_settings.cache_clear()
    team = (await client.post('/admin/api/teams', headers=admin_headers, json={'name': 'enroll-team'})).json()
    user = (await client.post('/admin/api/users', headers=admin_headers, json={'email': 'enroll@example.com', 'team_id': team['id']})).json()
    async with db.transaction() as s:
        (await s.get(db.User, user['id'])).oidc_subject = 'issuer|enroll-user'
        s.add(db.BrowserSession(digest=digest('enroll-cookie'), user_id=user['id'], csrf='csrf', expires_at=db.now()+timedelta(hours=1)))
    client.cookies.set('gateway_session', 'enroll-cookie')
    return team, user, {'X-CSRF-Token': 'csrf'}


async def start(client, credential=None, request_id=None):
    root = credential or secret()
    request_id = request_id or str(uuid4())
    response = await client.post('/auth/agent/enrollment/start', json={'candidate_credential': root, 'request_id': request_id, 'device_name': '<b>device</b>'})
    assert response.status_code == 200, response.text
    return request_id, root, response.json()['approval_code']


async def poll(client, request_id, root):
    return await client.post('/auth/agent/enrollment/poll', json={'request_id': request_id}, headers={'Authorization': 'Bearer '+root})


async def test_default_disabled_and_configuration(client, monkeypatch):
    response = await client.post('/auth/agent/enrollment/start', json={'request_id': str(uuid4()), 'candidate_credential': secret(), 'device_name': 'test'})
    assert response.status_code == 404
    monkeypatch.setenv('ENROLLMENT_ENABLED', 'true')
    monkeypatch.setenv('ENROLLMENT_ALLOWED_MODELS', '[]')
    get_settings.cache_clear()
    with pytest.raises(ValueError):
        get_settings()
    get_settings.cache_clear()


async def test_approval_scopes_csrf_idempotency_and_digest_only(client, admin_headers, monkeypatch):
    team, user, csrf = await context(client, admin_headers, monkeypatch)
    request_id, root, code = await start(client)
    assert code != request_id
    inspected = await client.post('/auth/agent/enrollment/inspect', headers=csrf, json={'request_id': code})
    assert inspected.status_code == 200
    assert inspected.json()['policy']['models'] == ['chat']
    assert inspected.json()['teams'] == [{'id': team['id'], 'name': team['name']}]
    body = {'request_id': code, 'team_id': team['id']}
    assert (await client.post('/auth/agent/enrollment/approve', json=body)).status_code == 403
    assert (await client.post('/auth/agent/enrollment/approve', headers=admin_headers, json=body)).status_code == 403
    approved = await client.post('/auth/agent/enrollment/approve', headers=csrf, json=body)
    assert approved.status_code == 200, approved.text
    assert (await client.post('/auth/agent/enrollment/approve', headers=csrf, json=body)).json() == approved.json()
    assert (await poll(client, request_id, secret())).status_code == 401
    result = await poll(client, request_id, root)
    assert result.status_code == 200 and result.json()['key_id'] == approved.json()['key_id']
    assert (await poll(client, request_id, root)).status_code == 429
    token = await client.post('/auth/agent/exchange', headers={'Authorization': 'Bearer '+root})
    assert token.status_code == 200
    async with db.session_factory() as s:
        row = await s.get(db.AgentEnrollment, request_id)
        key = await s.get(db.APIKey, approved.json()['key_id'])
        assert row.candidate_digest == digest(root) == key.digest
        assert key.user_id == user['id'] and key.permissions == ['inference'] and key.kind == 'agent'
        assert key.allowed_models == ['chat'] and key.rpm == 60 and key.tpm == 100000
        assert root not in str(row.__dict__) and root not in str(key.__dict__)


@pytest.mark.parametrize('change', ['disabled', 'team_disabled', 'membership_changed', 'policy_changed'])
async def test_approval_rechecks_current_state(client, admin_headers, monkeypatch, change):
    team, user, csrf = await context(client, admin_headers, monkeypatch)
    _, _, code = await start(client)
    async with db.transaction() as s:
        if change == 'disabled':
            (await s.get(db.User, user['id'])).enabled = False
        if change == 'team_disabled':
            (await s.get(db.Team, team['id'])).enabled = False
        if change == 'membership_changed':
            (await s.get(db.User, user['id'])).team_id = None
    if change == 'policy_changed':
        monkeypatch.setenv('ENROLLMENT_ALLOWED_MODELS', '["different"]')
        get_settings.cache_clear()
    result = await client.post('/auth/agent/enrollment/approve', headers=csrf, json={'request_id': code, 'team_id': team['id']})
    assert result.status_code in {401,403,409}


async def test_pending_capacity_expiry_denial_and_no_approval_code_reuse(client, admin_headers, monkeypatch):
    _, _, csrf = await context(client, admin_headers, monkeypatch)
    monkeypatch.setenv('ENROLLMENT_MAX_PENDING', '1')
    get_settings.cache_clear()
    request_id, root, code = await start(client)
    conflict = await client.post('/auth/agent/enrollment/start', json={'request_id': request_id, 'candidate_credential': secret(), 'device_name': 'conflict'})
    assert conflict.status_code == 409
    full = await client.post('/auth/agent/enrollment/start', json={'request_id': str(uuid4()), 'candidate_credential': secret(), 'device_name': 'full'})
    assert full.status_code == 429
    assert (await client.post('/auth/agent/enrollment/deny', headers=csrf, json={'request_id': code})).status_code == 200
    assert (await poll(client, request_id, root)).json()['state'] == 'denied'
    async with db.transaction() as s:
        (await s.get(db.AgentEnrollment, request_id)).expires_at = db.now()-timedelta(seconds=1)
    assert (await poll(client, request_id, root)).status_code == 410
    await start(client)  # safely removes expired record
    async with db.transaction() as s:
        rows = (await s.scalars(select(db.AgentEnrollment))).all()
        rows[0].expires_at = db.now()-timedelta(seconds=1)
    _, _, new_code = await start(client, request_id=request_id)
    assert new_code != code
    assert (await client.post('/auth/agent/enrollment/inspect', headers=csrf, json={'request_id': code})).status_code == 410


@pytest.mark.parametrize('lost', ['start', 'poll'])
async def test_journal_response_loss_recovery_and_relay_block(client, admin_headers, monkeypatch, tmp_path, lost):
    team, _, csrf = await context(client, admin_headers, monkeypatch)
    directory = tmp_path/'journal'
    directory.mkdir(mode=0o700)
    journal = CredentialJournal(str(directory), 'http://localhost:8080', '', enrollment_name='device')
    root = journal.state['current']
    with pytest.raises(RotationError):
        await journal.credential(client, 86400)
    dropped = False
    async def transport(request):
        nonlocal dropped
        response = await client.send(request)
        if request.url.path.endswith('/start'):
            code = response.json()['approval_code']
            approved = await client.post('/auth/agent/enrollment/approve', headers=csrf, json={'request_id': code, 'team_id': team['id']})
            assert approved.status_code == 200
        if request.url.path.endswith('/'+lost) and not dropped:
            dropped = True
            raise httpx.ReadError('secret upstream details '+root)
        return response
    notices = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport), base_url='http://localhost:8080') as proxy:
        with pytest.raises(EnrollmentError) as error:
            await enroll(journal, proxy, notices.append)
        assert root not in str(error.value)
        journal.close()
        journal = CredentialJournal(str(directory), 'http://localhost:8080', '', enrollment_name='ignored')
        async with db.transaction() as s:
            row = await s.get(db.AgentEnrollment, journal.state['enrollment']['request_id'])
            row.last_poll = None
        result = await enroll(journal, proxy, notices.append)
    assert result['state'] == 'approved'
    assert journal.state['enrollment'] is None and journal.state['current'] == root
    assert root not in str(notices)
    assert await journal.credential(client, 86400) == root
    journal.close()


async def test_late_approved_recovery_after_cleanup_and_id_cannot_be_reused(client, admin_headers, monkeypatch, tmp_path):
    team, _, csrf = await context(client, admin_headers, monkeypatch)
    directory = tmp_path/'late'
    directory.mkdir(mode=0o700)
    journal = CredentialJournal(str(directory), 'http://localhost:8080', '', enrollment_name='late')
    pending, root = journal.state['enrollment'], journal.state['current']
    request_id, _, code = await start(client, root, pending['request_id'])
    approved = await client.post('/auth/agent/enrollment/approve', headers=csrf, json={'request_id': code, 'team_id': team['id']})
    async with db.transaction() as s:
        await s.delete(await s.get(db.AgentEnrollment, request_id))
    duplicate = await client.post('/auth/agent/enrollment/start', json={'request_id': request_id, 'candidate_credential': secret(), 'device_name': 'conflict'})
    assert duplicate.status_code == 409
    result = await enroll(journal, client, lambda _: None)
    assert result['key_id'] == approved.json()['key_id'] and journal.state['enrollment'] is None
    async with db.transaction() as s:
        (await s.get(db.APIKey, result['key_id'])).revoked = True
    recover = await client.post('/auth/agent/enrollment/recover', headers={'Authorization': 'Bearer '+root}, json={'request_id': request_id})
    assert recover.status_code == 401
    journal.close()


@pytest.mark.parametrize('competing', ['approve', 'deny'])
async def test_atomic_approval_transitions(client, admin_headers, monkeypatch, competing):
    import asyncio
    from sqlalchemy import func
    team, _, csrf = await context(client, admin_headers, monkeypatch)
    _, _, code = await start(client)
    approve_body = {'request_id': code, 'team_id': team['id']}
    responses = await asyncio.gather(
        client.post('/auth/agent/enrollment/approve', headers=csrf, json=approve_body),
        client.post('/auth/agent/enrollment/'+competing, headers=csrf,
                    json=approve_body if competing == 'approve' else {'request_id': code}))
    if competing == 'approve':
        assert [r.status_code for r in responses] == [200,200]
        assert responses[0].json()['key_id'] == responses[1].json()['key_id']
    else:
        assert sorted(r.status_code for r in responses) == [200,409]
    async with db.session_factory() as s:
        assert await s.scalar(select(func.count()).select_from(db.APIKey).where(db.APIKey.kind == 'agent')) <= 1


async def test_failed_journal_fsync_sends_nothing(client, monkeypatch, tmp_path):
    directory = tmp_path/'unsafe'
    directory.mkdir(mode=0o700)
    monkeypatch.setattr('gateway.relay_journal.os.fsync', lambda _: (_ for _ in ()).throw(OSError('disk unavailable')))
    with pytest.raises(OSError):
        CredentialJournal(str(directory), 'https://gateway.example', '', enrollment_name='new')


async def test_recovery_fenced_by_rotation_and_current_identity(client, admin_headers, monkeypatch):
    team, user, csrf = await context(client, admin_headers, monkeypatch)
    request_id, root, code = await start(client)
    assert (await client.post('/auth/agent/enrollment/approve', headers=csrf,
                              json={'request_id': code, 'team_id': team['id']})).status_code == 200
    candidate, operation = secret(), str(uuid4())
    prepared = await client.post('/auth/agent/rotation/prepare', headers={'Authorization': 'Bearer '+root},
                                 json={'operation_id': operation, 'next_credential': candidate})
    assert prepared.status_code == 200
    assert (await client.post('/auth/agent/rotation/commit', headers={'Authorization': 'Bearer '+candidate},
                              json={'operation_id': operation})).status_code == 200
    async def recover(value):
        return await client.post('/auth/agent/enrollment/recover', headers={'Authorization': 'Bearer '+value},
                                  json={'request_id': request_id})
    assert (await recover(root)).status_code == 401
    assert (await recover(candidate)).status_code == 200
    async with db.transaction() as s:
        (await s.get(db.Team, team['id'])).enabled = False
    assert (await recover(candidate)).status_code == 401
    async with db.transaction() as s:
        (await s.get(db.Team, team['id'])).enabled = True
        (await s.get(db.User, user['id'])).enabled = False
    assert (await recover(candidate)).status_code == 401
