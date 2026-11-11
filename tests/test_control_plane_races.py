"""Revocations ordered before the writer gate fence queued control-plane writes."""
from contextlib import asynccontextmanager
from datetime import timedelta

import pytest
from sqlalchemy import select, func, delete

from gateway import db
from gateway.security import digest


def change_before_gate(monkeypatch, change):
    original = db.transaction
    pending = True

    @asynccontextmanager
    async def gate(*args, **kwargs):
        nonlocal pending
        if pending:
            pending = False
            async with original() as s:
                await change(s)
        async with original(*args, **kwargs) as s:
            yield s
    monkeypatch.setattr(db, 'transaction', gate)


@pytest.mark.parametrize('mutation', ['revoke', 'demote', 'disable', 'rotate', 'generation', 'permission', 'expire'])
async def test_queued_admin_cannot_mint_after_authorization_change(client, admin_headers, monkeypatch, mutation):
    uid = (await client.get('/admin/api/users', headers=admin_headers)).json()[0]['id']
    async def change(s):
        key = await s.scalar(select(db.APIKey).where(db.APIKey.digest == digest(admin_headers['Authorization'][7:])))
        user = await s.get(db.User, uid)
        if mutation == 'revoke':
            key.revoked = True
        elif mutation == 'demote':
            user.role = 'member'
        elif mutation == 'disable':
            user.enabled = False
        elif mutation == 'rotate':
            key.digest = digest('replacement-secret')
        elif mutation == 'generation':
            key.generation += 1
        elif mutation == 'permission':
            key.permissions = ['inference']
        else:
            key.expires_at = db.now() - timedelta(seconds=1)
    change_before_gate(monkeypatch, change)
    r = await client.post('/admin/api/keys', headers=admin_headers,
                         json={'user_id': uid, 'name':'after-revoke','permissions':['admin']})
    assert r.status_code == 403, r.text
    async with db.session_factory() as s:
        assert await s.scalar(select(func.count()).select_from(db.APIKey).where(db.APIKey.name == 'after-revoke')) == 0


@pytest.mark.parametrize('path,payload', [
    ('/admin/api/teams', {'name':'forbidden'}),
    ('/admin/api/users', {'email':'forbidden@example.com'}),
    ('/admin/api/prompt-registry/templates', {'name':'forbidden','content':'hello'}),
    ('/admin/api/evaluation-comparisons', {'cases':[{'expected':'x','baseline':'x','candidate':'x'}]}),
    ('/scim/v2/Users', {'schemas':['urn:ietf:params:scim:schemas:core:2.0:User'],'userName':'forbidden@example.com'}),
    ('/scim/v2/Groups', {'schemas':['urn:ietf:params:scim:schemas:core:2.0:Group'],'displayName':'forbidden'}),
])
async def test_control_plane_writers_recheck_key(client, admin_headers, monkeypatch, path, payload):
    async def change(s):
        key = await s.scalar(select(db.APIKey).where(db.APIKey.digest == digest(admin_headers['Authorization'][7:])))
        key.revoked = True
    change_before_gate(monkeypatch, change)
    r = await client.post(path, headers=admin_headers, json=payload)
    assert r.status_code in {401,403}, r.text


async def test_browser_session_revoked_before_write(client, admin_headers, monkeypatch):
    uid = (await client.get('/admin/api/users', headers=admin_headers)).json()[0]['id']
    raw = 'test-session'
    async with db.transaction() as s:
        s.add(db.BrowserSession(digest=digest(raw), user_id=uid, csrf='test-csrf', expires_at=db.now()+timedelta(hours=1)))
    client.cookies.set('gateway_session', raw)
    async def change(s):
        await s.execute(delete(db.BrowserSession))
    change_before_gate(monkeypatch, change)
    r=await client.post('/admin/api/keys',headers={'x-csrf-token':'test-csrf'},json={'user_id':uid,'name':'forbidden'})
    assert r.status_code==403, r.text


async def test_member_rotation_fences_changed_digest(client, admin_headers, monkeypatch):
    user=(await client.post('/admin/api/users',headers=admin_headers,json={'email':'member@example.com'})).json()
    key=(await client.post('/admin/api/keys',headers=admin_headers,json={'user_id':user['id'],'name':'own','permissions':['account']})).json()
    async def change(s):
        row=await s.get(db.APIKey,key['id'])
        row.digest=digest('rotated-secret')
    change_before_gate(monkeypatch,change)
    r=await client.post('/account/api/keys/'+key['id']+'/rotate',headers={'Authorization':'Bearer '+key['key']})
    assert r.status_code==401,r.text


async def test_team_delegate_permission_removed_while_queued(client, admin_headers, monkeypatch):
    team=(await client.post('/admin/api/teams',headers=admin_headers,json={'name':'team'})).json()
    user=(await client.post('/admin/api/users',headers=admin_headers,json={'email':'delegate@example.com','team_id':team['id']})).json()
    path='/teams/api/'+team['id']
    assert (await client.put(path+'/members/'+user['id'],headers=admin_headers,json={'role':'admin'})).status_code==200
    key=(await client.post('/admin/api/keys',headers=admin_headers,json={'user_id':user['id'],'name':'delegate','permissions':['team_admin']})).json()
    async def change(s):
        row=await s.get(db.APIKey,key['id'])
        row.permissions=['inference']
    change_before_gate(monkeypatch,change)
    r=await client.post(path+'/keys',headers={'Authorization':'Bearer '+key['key']},json={'user_id':user['id'],'name':'forbidden','expires_in_days':1})
    assert r.status_code==403,r.text
