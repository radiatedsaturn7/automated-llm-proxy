from datetime import timedelta

from sqlalchemy import select

from gateway import db
from gateway.config import get_settings
from gateway.security import digest


async def user(client, headers, email):
    r = await client.post('/admin/api/users', headers=headers, json={'email': email})
    assert r.status_code == 201
    return r.json()['id']


async def add_session(user_id, raw):
    async with db.transaction() as s:
        s.add(db.BrowserSession(digest=digest(raw), user_id=user_id, csrf='csrf', expires_at=db.now() + timedelta(hours=1)))


async def test_explicit_identity_link(client, admin_headers, monkeypatch):
    uid = await user(client, admin_headers, 'member@example.com')
    other = await user(client, admin_headers, 'other@example.com')
    monkeypatch.setenv('OIDC_ISSUER', 'https://idp.example.com')
    get_settings.cache_clear()
    body = {'issuer': 'https://idp.example.com', 'subject': 'verified-subject', 'identity_verified': True}
    await add_session(uid, 'old-session')
    path = f'/admin/api/users/{uid}/identity'
    assert (await client.post(path, json=body)).status_code == 401
    assert (await client.post(path, headers=admin_headers, json={**body, 'identity_verified': False})).status_code == 422
    assert (await client.post(path, headers=admin_headers, json={**body, 'issuer': 'https://wrong.example'})).status_code == 400
    response = await client.post(path, headers=admin_headers, json=body)
    assert response.status_code == 200, response.text
    assert response.json()['changed']
    assert not (await client.post(path, headers=admin_headers, json=body)).json()['changed']
    assert (await client.post(f'/admin/api/users/{other}/identity', headers=admin_headers, json=body)).status_code == 409
    assert (await client.post(path, headers=admin_headers, json={**body, 'subject': 'replacement'})).status_code == 409
    async with db.session_factory() as s:
        assert (await s.get(db.User, uid)).oidc_subject == 'https://idp.example.com|verified-subject'
        assert await s.get(db.BrowserSession, digest('old-session')) is None
    rows = (await client.get('/admin/api/users', headers=admin_headers)).json()
    assert next(r for r in rows if r['id'] == uid)['identity_linked']
    assert 'verified-subject' not in str(rows)
    audit = (await client.get('/admin/api/audit', headers=admin_headers)).json()
    assert any(r['action'] == 'user.identity.link' for r in audit)


async def test_session_revocation_scope_and_confirmation(client, admin_headers):
    uid = await user(client, admin_headers, 'member@example.com')
    other = await user(client, admin_headers, 'other@example.com')
    await add_session(uid, 'one')
    await add_session(uid, 'two')
    await add_session(other, 'three')
    path = f'/admin/api/users/{uid}/sessions/revoke'
    assert (await client.post(path, headers=admin_headers, json={})).status_code == 422
    assert (await client.post(path, headers=admin_headers, json={'confirm': True})).json() == {'revoked_sessions': 2}
    async with db.session_factory() as s:
        assert await s.get(db.BrowserSession, digest('three')) is not None
    assert (await client.post('/admin/api/sessions/revoke-all', json={'confirm': True})).status_code == 401
    response = await client.post('/admin/api/sessions/revoke-all', headers=admin_headers, json={'confirm': True})
    assert response.json() == {'revoked_sessions': 1}
    assert (await client.get('/admin/api/users', headers=admin_headers)).status_code == 200
    async with db.session_factory() as s:
        assert (await s.scalars(select(db.BrowserSession))).all() == []


async def test_provider_patch_validates_model_map(client, admin_headers):
    valid = {'upstream_model': 'test', 'input_cost_per_million': 0, 'output_cost_per_million': 0}
    for models in [{}, {'': valid}, {'x' * 129: valid}, {f'm{i}': valid for i in range(101)}]:
        response = await client.patch('/admin/api/providers/nonexistent', headers=admin_headers, json={'models': models})
        assert response.status_code == 422, response.text
    response = await client.patch('/admin/api/providers/nonexistent', headers=admin_headers, json={'models': {'valid': valid}})
    assert response.status_code == 404


async def test_member_forbidden_and_concurrent_identity_collision(client, admin_headers, monkeypatch):
    import asyncio
    uid = await user(client, admin_headers, 'member@example.com')
    other = await user(client, admin_headers, 'other@example.com')
    key = await client.post('/admin/api/keys', headers=admin_headers, json={'name': 'member', 'user_id': uid})
    headers = {'Authorization': 'Bearer ' + key.json()['key']}
    monkeypatch.setenv('OIDC_ISSUER', 'https://idp.example.com')
    get_settings.cache_clear()
    body = {'issuer': 'https://idp.example.com', 'subject': 'one-subject', 'identity_verified': True}
    for path, payload in [(f'/admin/api/users/{uid}/identity', body), (f'/admin/api/users/{uid}/sessions/revoke', {'confirm': True}), ('/admin/api/sessions/revoke-all', {'confirm': True})]:
        assert (await client.post(path, headers=headers, json=payload)).status_code == 403
    responses = await asyncio.gather(*(client.post(f'/admin/api/users/{u}/identity', headers=admin_headers, json=body) for u in [uid, other]))
    assert sorted(r.status_code for r in responses) == [200, 409]
