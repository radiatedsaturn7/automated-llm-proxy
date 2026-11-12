from datetime import timedelta

import pytest
from sqlalchemy import select

from gateway import db, local_auth
from gateway.config import get_settings
from gateway.security import digest, encrypt
from gateway.local_mfa import totp

PASSWORD = 'old-long-local-passphrase-123'
NEW = 'new-long-local-passphrase-456'
HEADERS = {'Origin': 'http://localhost:8080', 'X-CSRF-Token': 'csrf'}


async def setup(client, admin_headers, monkeypatch, mfa=False):
    monkeypatch.setenv('LOCAL_PASSWORD_ENABLED', 'true')
    get_settings.cache_clear()
    user = (await client.post('/admin/api/users', headers=admin_headers,
                              json={'email': 'member@example.com'})).json()
    uid = user['id']
    assert (await client.put(f'/admin/api/users/{uid}/password', headers=admin_headers,
                            json={'password': PASSWORD})).status_code == 204
    async with db.transaction() as s:
        for cookie in ('current', 'other'):
            s.add(db.BrowserSession(digest=digest(cookie), user_id=uid, csrf='csrf',
                                   expires_at=db.now() + timedelta(hours=1), mfa_verified=mfa))
        if mfa:
            s.add(db.LocalMFA(user_id=uid, seed=encrypt('JBSWY3DPEHPK3PXP'), last_counter=-1,
                              grant_digest=digest('pending'), password_binding='pending', attempts=0))
    client.cookies.set('gateway_session', 'current')
    return uid


def body(**kwargs):
    return {'current_password': PASSWORD, 'new_password': NEW, **kwargs}


async def test_member_password_change_revokes_browsers_not_keys(client, admin_headers, monkeypatch):
    uid = await setup(client, admin_headers, monkeypatch)
    key = (await client.post('/admin/api/keys', headers=admin_headers,
                             json={'name': 'member', 'user_id': uid, 'permissions': ['account']})).json()
    assert (await client.get('/account/api/profile')).json()['can_change_password']
    r = await client.post('/account/api/password', headers=HEADERS, json=body())
    assert r.status_code == 204, r.text
    assert 'Max-Age=0' in r.headers['set-cookie']
    for cookie in ('current', 'other'):
        client.cookies.set('gateway_session', cookie)
        assert (await client.get('/account/api/profile')).status_code == 401
    assert (await client.get('/account/api/profile', headers={'Authorization': 'Bearer ' + key['key']})).status_code == 200
    client.cookies.clear()
    for password, expected in ((PASSWORD, 401), (NEW, 200)):
        r = await client.post('/auth/local/login', headers=HEADERS,
                              json={'email':'member@example.com', 'password':password})
        assert r.status_code == expected
    async with db.session_factory() as s:
        audits = (await s.scalars(select(db.Audit))).all()
        assert PASSWORD not in str([a.__dict__ for a in audits])
        assert NEW not in str([a.__dict__ for a in audits])


@pytest.mark.parametrize('change,expected', [({'current_password': 'wrong'}, 401),
                                           ({'new_password': 'short'}, 422)])
async def test_invalid_password_no_mutation(client, admin_headers, monkeypatch, change, expected):
    uid = await setup(client, admin_headers, monkeypatch)
    assert (await client.post('/account/api/password', headers=HEADERS, json=body(**change))).status_code == expected
    async with db.session_factory() as s:
        assert local_auth.verify(PASSWORD, (await s.get(db.LocalPassword, uid)).password_hash)
        assert await s.get(db.BrowserSession, digest('other'))


async def test_password_csrf_origin_and_bearer_denied(client, admin_headers, monkeypatch):
    await setup(client, admin_headers, monkeypatch)
    assert (await client.post('/account/api/password', json=body())).status_code == 403
    assert (await client.post('/account/api/password', headers={**HEADERS,'Origin':'https://evil.test'}, json=body())).status_code == 403
    assert (await client.post('/account/api/password', headers=admin_headers, json=body())).status_code == 403
    monkeypatch.setenv('LOCAL_PASSWORD_ENABLED', 'false')
    get_settings.cache_clear()
    assert (await client.post('/account/api/password', headers=HEADERS, json=body())).status_code == 404


@pytest.mark.parametrize('field,value', [('scim_managed', True), ('oidc_subject', 'issuer|subject'),
                                         ('scim_external_id', 'external')])
async def test_federated_cannot_change_local_password(client, admin_headers, monkeypatch, field, value):
    uid = await setup(client, admin_headers, monkeypatch)
    async with db.transaction() as s:
        setattr(await s.get(db.User, uid), field, value)
    assert not (await client.get('/account/api/profile')).json()['can_change_password']
    assert (await client.post('/account/api/password', headers=HEADERS, json=body())).status_code == 401


async def test_mfa_password_change_requires_fresh_code_and_preserves_factor(client, admin_headers, monkeypatch):
    uid = await setup(client, admin_headers, monkeypatch, mfa=True)
    assert (await client.get('/account/api/profile')).json()['local_mfa_enrolled']
    assert (await client.post('/account/api/password', headers=HEADERS, json=body())).status_code == 401
    counter = int(db.now().timestamp()) // 30
    code = totp('JBSWY3DPEHPK3PXP', counter)
    async with db.transaction() as s:
        (await s.get(db.LocalMFA, uid)).last_counter = counter
    assert (await client.post('/account/api/password', headers=HEADERS, json=body(code=code))).status_code == 401
    fresh = totp('JBSWY3DPEHPK3PXP', counter + 1)
    assert (await client.post('/account/api/password', headers=HEADERS, json=body(code=fresh))).status_code == 204
    async with db.session_factory() as s:
        factor = await s.get(db.LocalMFA, uid)
        assert factor.seed and factor.last_counter == counter + 1
        assert factor.grant_digest is None and factor.password_binding is None


async def test_password_change_fenced_after_concurrent_reset(client, admin_headers, monkeypatch):
    uid = await setup(client, admin_headers, monkeypatch)
    original = local_auth.derive
    async def raced(function, *args):
        result = await original(function, *args)
        if function is local_auth.verify:
            async with db.transaction() as s:
                (await s.get(db.LocalPassword, uid)).password_hash = local_auth.password_hash('admin-reset-passphrase-123')
        return result
    monkeypatch.setattr(local_auth, 'derive', raced)
    assert (await client.post('/account/api/password', headers=HEADERS, json=body())).status_code == 401


async def test_other_sessions_revocation_is_scoped_and_preserves_current(client, admin_headers, monkeypatch):
    uid = await setup(client, admin_headers, monkeypatch)
    async with db.transaction() as s:
        owner = await s.scalar(select(db.User).where(db.User.id != uid))
        s.add(db.BrowserSession(digest=digest('different-user'), user_id=owner.id, csrf='other',
                               expires_at=db.now() + timedelta(hours=1)))
    assert (await client.post('/account/api/sessions/revoke-others')).status_code == 403
    assert (await client.post('/account/api/sessions/revoke-others', headers=admin_headers)).status_code == 403
    for _ in range(2):
        assert (await client.post('/account/api/sessions/revoke-others', headers=HEADERS)).status_code == 200
    async with db.session_factory() as s:
        assert await s.get(db.BrowserSession, digest('current'))
        assert await s.get(db.BrowserSession, digest('different-user'))
        assert not await s.get(db.BrowserSession, digest('other'))
