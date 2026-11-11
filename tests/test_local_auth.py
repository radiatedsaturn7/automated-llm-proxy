import pytest
from sqlalchemy import select, func
from gateway import db
from gateway.config import get_settings

PASSWORD = 'a-long-local-passphrase-123'
ORIGIN = {'Origin': 'http://localhost:8080'}


async def setup(client, admin_headers, monkeypatch):
    monkeypatch.setenv('LOCAL_PASSWORD_ENABLED', 'true')
    get_settings.cache_clear()
    users = (await client.get('/admin/api/users', headers=admin_headers)).json()
    uid = users[0]['id']
    r = await client.put(f'/admin/api/users/{uid}/password', headers=admin_headers, json={'password': PASSWORD})
    assert r.status_code == 204, r.text
    return uid


async def test_default_off(client):
    r = await client.post('/auth/local/login', headers=ORIGIN, json={'email':'a@b.com','password':PASSWORD})
    assert r.status_code == 404


async def test_login_reset_revoke_and_csrf(client, admin_headers, monkeypatch):
    uid = await setup(client, admin_headers, monkeypatch)
    payload = {'email':'admin@example.com', 'password':PASSWORD}
    assert (await client.post('/auth/local/login', json=payload)).status_code == 403
    r = await client.post('/auth/local/login', headers=ORIGIN, json=payload)
    assert r.status_code == 200
    assert 'HttpOnly' in r.headers['set-cookie']
    assert (await client.get('/auth/session')).json()['authenticated']
    assert (await client.post('/auth/logout')).status_code == 403
    assert (await client.put(f'/admin/api/users/{uid}/password', headers=admin_headers, json={'password':PASSWORD+'2'})).status_code == 204
    assert not (await client.get('/auth/session')).json()['authenticated']
    assert (await client.post('/auth/local/login', headers=ORIGIN, json=payload)).status_code == 401
    payload['password'] += '2'
    assert (await client.post('/auth/local/login', headers=ORIGIN, json=payload)).status_code == 200
    assert (await client.delete(f'/admin/api/users/{uid}/password', headers=admin_headers)).status_code == 204
    assert not (await client.get('/auth/session')).json()['authenticated']


async def test_generic_failure_and_persistent_throttle(client, admin_headers, monkeypatch):
    await setup(client, admin_headers, monkeypatch)
    for i in range(5):
        r = await client.post('/auth/local/login', headers=ORIGIN, json={'email':'absent@example.com','password':'wrong'})
        assert r.status_code == 401
        assert r.json()['detail'] == 'Invalid credentials'
    r = await client.post('/auth/local/login', headers=ORIGIN, json={'email':'absent@example.com','password':'wrong'})
    assert r.status_code == 429
    async with db.session_factory() as s:
        assert await s.scalar(select(func.count()).select_from(db.LoginThrottle)) == 2


async def test_federated_accounts_and_weak_password_rejected(client, admin_headers, monkeypatch):
    uid = await setup(client, admin_headers, monkeypatch)
    assert (await client.put(f'/admin/api/users/{uid}/password', headers=admin_headers, json={'password':'short'})).status_code == 422
    async with db.transaction() as s:
        user = await s.get(db.User, uid)
        user.oidc_subject = 'issuer|subject'
    assert (await client.put(f'/admin/api/users/{uid}/password', headers=admin_headers, json={'password':PASSWORD})).status_code == 409
    assert (await client.post('/auth/local/login', headers=ORIGIN, json={'email':'admin@example.com','password':PASSWORD})).status_code == 401


async def test_disabled_user_and_team(client, admin_headers, monkeypatch):
    uid = await setup(client, admin_headers, monkeypatch)
    async with db.transaction() as s:
        user = await s.get(db.User, uid)
        user.enabled = False
    r = await client.post('/auth/local/login', headers=ORIGIN, json={'email':'admin@example.com','password':PASSWORD})
    assert r.status_code == 401


async def test_concurrent_throttle_limit(client, admin_headers, monkeypatch):
    import asyncio
    from gateway.local_auth import throttle
    from fastapi import HTTPException
    await setup(client, admin_headers, monkeypatch)
    async def attempt():
        try:
            await throttle('concurrent@example.com', '127.0.0.1')
            return True
        except HTTPException:
            return False
    assert sum(await asyncio.gather(*(attempt() for _ in range(10)))) == 5


async def test_reset_during_password_verification(client, admin_headers, monkeypatch):
    from gateway import local_auth
    uid = await setup(client, admin_headers, monkeypatch)
    original = local_auth.derive
    async def reset(function, *args):
        result = await original(function, *args)
        async with db.transaction() as s:
            row = await s.get(db.LocalPassword, uid)
            row.password_hash = 'changed'
        return result
    monkeypatch.setattr(local_auth, 'derive', reset)
    assert (await client.post('/auth/local/login', headers=ORIGIN, json={'email':'admin@example.com','password':PASSWORD})).status_code == 401


async def test_login_requires_json_media_type(client, admin_headers, monkeypatch):
    import json
    await setup(client, admin_headers, monkeypatch)
    r = await client.post('/auth/local/login', headers=ORIGIN,
                          content=json.dumps({'email':'admin@example.com','password':PASSWORD}))
    assert r.status_code in {415, 422}


def test_scrypt_salted_and_constant_format():
    from gateway.local_auth import password_hash, verify
    first, second = password_hash(PASSWORD), password_hash(PASSWORD)
    assert first != second
    assert PASSWORD not in first
    assert verify(PASSWORD, first)
    assert not verify('wrong', first)
    assert not verify(PASSWORD, None)


async def test_hash_overload_rejected(monkeypatch):
    import asyncio
    import pytest
    from fastapi import HTTPException
    from gateway import local_auth
    monkeypatch.setattr(local_auth, '_hash_slots', asyncio.Semaphore(0))
    with pytest.raises(HTTPException) as error:
        await local_auth.derive(lambda: None)
    assert error.value.status_code == 503


async def test_scim_without_external_id_cannot_gain_password(client, admin_headers, monkeypatch):
    from gateway.scim import USER
    await setup(client, admin_headers, monkeypatch)
    r = await client.post('/scim/v2/Users', headers=admin_headers,
                          json={'schemas':[USER], 'userName':'managed@example.com'})
    assert r.status_code == 201
    uid = r.json()['id']
    r = await client.put(f'/admin/api/users/{uid}/password', headers=admin_headers, json={'password':PASSWORD})
    assert r.status_code == 409


async def test_scim_takeover_erases_local_credential(client, admin_headers, monkeypatch):
    from gateway.scim import PREFIX
    await setup(client, admin_headers, monkeypatch)
    r = await client.post('/admin/api/users', headers=admin_headers, json={'email':'local@example.com'})
    assert r.status_code == 201
    uid = r.json()['id']
    assert (await client.put(f'/admin/api/users/{uid}/password', headers=admin_headers, json={'password':PASSWORD})).status_code == 204
    assert (await client.post('/auth/local/login', headers=ORIGIN, json={'email':'local@example.com','password':PASSWORD})).status_code == 200
    r = await client.patch(f'/scim/v2/Users/{uid}', headers=admin_headers,
                          json={'schemas':[PREFIX+'api:messages:2.0:PatchOp'], 'Operations':[{'op':'replace','path':'active','value':True}]})
    assert r.status_code == 200, r.text
    async with db.session_factory() as s:
        assert await s.get(db.LocalPassword, uid) is None
        assert (await s.get(db.User, uid)).scim_managed
    assert not (await client.get('/auth/session')).json()['authenticated']



@pytest.mark.parametrize('change', ['demote', 'disable', 'revoke', 'rotate', 'expire', 'permissions'])
async def test_caller_authorization_rechecked_after_hash(client, admin_headers, monkeypatch, change):
    from datetime import timedelta
    from gateway import local_auth
    await setup(client, admin_headers, monkeypatch)
    target = (await client.post('/admin/api/users', headers=admin_headers,
                               json={'email': 'target@example.com'})).json()['id']
    original = local_auth.derive

    async def revoke_during_hash(function, *args):
        result = await original(function, *args)
        async with db.transaction() as s:
            user = await s.scalar(select(db.User).where(db.User.email == 'admin@example.com'))
            key = await s.scalar(select(db.APIKey).where(db.APIKey.user_id == user.id))
            if change == 'demote':
                user.role = 'member'
            elif change == 'disable':
                user.enabled = False
            elif change == 'revoke':
                key.revoked = True
            elif change == 'rotate':
                key.digest = 'rotated'
            elif change == 'expire':
                key.expires_at = db.now() - timedelta(seconds=1)
            else:
                key.permissions = ['inference']
        return result
    monkeypatch.setattr(local_auth, 'derive', revoke_during_hash)
    response = await client.put(f'/admin/api/users/{target}/password', headers=admin_headers,
                                json={'password': PASSWORD})
    assert response.status_code == 403, response.text
    async with db.session_factory() as s:
        assert await s.get(db.LocalPassword, target) is None


async def test_caller_session_revoked_during_hash(client, admin_headers, monkeypatch):
    from sqlalchemy import delete
    from gateway import local_auth
    await setup(client, admin_headers, monkeypatch)
    target = (await client.post('/admin/api/users', headers=admin_headers,
                               json={'email': 'target@example.com'})).json()['id']
    assert (await client.post('/auth/local/login', headers=ORIGIN,
                              json={'email': 'admin@example.com', 'password': PASSWORD})).status_code == 200
    csrf = (await client.get('/auth/session')).json()['csrf_token']
    original = local_auth.derive

    async def revoke_during_hash(function, *args):
        result = await original(function, *args)
        async with db.transaction() as s:
            await s.execute(delete(db.BrowserSession))
        return result
    monkeypatch.setattr(local_auth, 'derive', revoke_during_hash)
    response = await client.put(f'/admin/api/users/{target}/password', headers={**ORIGIN, 'x-csrf-token': csrf},
                                json={'password': PASSWORD})
    assert response.status_code == 403, response.text
    async with db.session_factory() as s:
        assert await s.get(db.LocalPassword, target) is None
