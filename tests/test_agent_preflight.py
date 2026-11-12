from contextlib import asynccontextmanager
import pytest
from gateway import db

@pytest.mark.parametrize('endpoint,payload', [
 ('/auth/agent/exchange', {}),
 ('/auth/agent/rotation/prepare', {'operation_id':'11111111-1111-4111-8111-111111111111', 'next_credential':'lg_'+'z'*43}),
 ('/auth/agent/rotation/commit', {'operation_id':'11111111-1111-4111-8111-111111111111'}),
])
async def test_invalid_bearer_never_enters_cluster_policy_gate(client, monkeypatch, endpoint,payload):
    original = db.transaction
    gates = []
    @asynccontextmanager
    async def counted(*args, **kwargs):
        gates.append(kwargs.get('inference', False))
        async with original(*args, **kwargs) as session:
            yield session
    monkeypatch.setattr(db, 'transaction', counted)
    response = await client.post(endpoint, json=payload, headers={'Authorization':'Bearer lg_'+'x'*43})
    assert response.status_code == 401
    assert gates == [], gates


@pytest.mark.parametrize('endpoint', ['exchange', 'prepare', 'commit', 'recovery'])
async def test_revocation_after_preflight_is_authoritatively_rechecked(client, admin_headers, monkeypatch, endpoint):
    import uuid
    from gateway import agent_rotation
    from tests.test_agent_rotation import agent, commit, headers, prepare, secret
    key = await agent(client, admin_headers)
    operation, candidate = str(uuid.uuid4()), secret()
    credential = key['key']
    if endpoint in {'commit', 'recovery'}:
        assert (await prepare(client, credential, candidate, operation)).status_code == 200
        credential = candidate
        if endpoint == 'recovery':
            assert (await commit(client, credential, operation)).status_code == 200
    original = agent_rotation.preflight

    async def revoked_after_valid_proof(*args, **kwargs):
        await original(*args, **kwargs)
        async with db.transaction() as session:
            stored = await session.get(db.APIKey, key['id'])
            stored.revoked = True
    monkeypatch.setattr(agent_rotation, 'preflight', revoked_after_valid_proof)
    if endpoint == 'exchange':
        response = await client.post('/auth/agent/exchange', headers=headers(credential))
    elif endpoint == 'prepare':
        response = await prepare(client, credential, candidate, operation)
    else:
        response = await commit(client, credential, operation)
    assert response.status_code == 401, response.text
    async with db.session_factory() as session:
        stored = await session.get(db.APIKey, key['id'])
        assert stored.revoked
        assert stored.generation == (1 if endpoint == 'recovery' else 0)


@pytest.mark.parametrize('case', ['revoked', 'expired', 'disabled', 'pending_wrong_operation', 'pending_expired', 'recovery_wrong_operation'])
async def test_known_but_invalid_proofs_never_enter_writer_gate(client, admin_headers, monkeypatch, case):
    import uuid
    from datetime import timedelta
    from tests.test_agent_rotation import agent, commit, headers, prepare, secret
    key = await agent(client, admin_headers)
    operation, candidate = str(uuid.uuid4()), secret()
    credential = key['key']
    if case.startswith('pending') or case.startswith('recovery'):
        assert (await prepare(client, credential, candidate, operation)).status_code == 200
        credential = candidate
        if case.startswith('recovery'):
            assert (await commit(client, credential, operation)).status_code == 200
    async with db.transaction() as session:
        stored = await session.get(db.APIKey, key['id'])
        if case == 'revoked':
            stored.revoked = True
        elif case == 'expired':
            stored.expires_at = db.now() - timedelta(seconds=1)
        elif case == 'disabled':
            user = await session.get(db.User, stored.user_id)
            user.enabled = False
        elif case == 'pending_expired':
            pending = await session.get(db.AgentRotation, key['id'])
            pending.expires_at = db.now() - timedelta(seconds=1)
    @asynccontextmanager
    async def forbidden_gate(*args, **kwargs):
        pytest.fail('Invalid proof entered writer gate')
        yield
    monkeypatch.setattr(db, 'transaction', forbidden_gate)
    if case.startswith('pending') or case.startswith('recovery'):
        response = await commit(client, credential, str(uuid.uuid4()) if case.endswith('wrong_operation') else operation)
        assert response.status_code in {401, 409, 410}
    else:
        assert (await client.post('/auth/agent/exchange', headers=headers(credential))).status_code == 401
        assert (await prepare(client, credential, candidate, operation)).status_code == 401
