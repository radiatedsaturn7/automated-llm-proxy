import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from gateway import db, scim
from gateway.scim import GROUP
from test_scim import add_user
from test_scim_groups import group
from test_oidc import oidc  # noqa: F401 - shared mocked IdP fixture


async def test_delete_recreate_history_and_no_resurrection(client, admin_headers):
    primary = await add_user(client, admin_headers)
    secondary = await add_user(client, admin_headers, 'second@example.com')
    gid = await group(client, admin_headers, [primary['id']])
    async with db.transaction() as session:
        session.add(db.Team(id='other', name='other', enabled=True))
        await session.flush()
        (await session.get(db.User, secondary['id'])).team_id = 'other'
        session.add(db.TeamMembership(user_id=secondary['id'], team_id=gid, role='member'))
        for user in (primary, secondary):
            session.add(db.BrowserSession(digest=user['id'], user_id=user['id'], csrf='c', expires_at=db.now()+timedelta(hours=1)))
    keys = []
    for uid, team in ((primary['id'],gid),(secondary['id'],gid),(secondary['id'],'other')):
        response = await client.post('/admin/api/keys', headers=admin_headers,
            json={'name':'key', 'user_id':uid, 'team_id':team})
        assert response.status_code == 201, response.text
        keys.append(response.json())
    async with db.transaction() as session:
        session.add(db.Usage(reservation_id='history', user_id=primary['id'], team_id=gid,
            key_id=keys[0]['id'], model='chat', provider_id='p', period='2026-11', input_tokens=1,
            output_tokens=1,cost_micros=2,estimated=False))
        (await session.get(db.Team,gid)).enabled = False  # already disabled still needs cleanup
    path = '/scim/v2/Groups/'+gid
    assert (await client.delete('/scim/v2/Groups/unknown',headers=admin_headers)).status_code == 404
    assert (await client.delete(path,headers=admin_headers)).status_code == 204
    assert (await client.delete(path,headers=admin_headers)).status_code == 204
    assert (await client.get(path,headers=admin_headers)).status_code == 404
    assert (await client.get('/scim/v2/Groups',headers=admin_headers)).json()['totalResults'] == 1  # unrelated other
    assert (await client.get('/scim/v2/Groups',headers=admin_headers,params={'filter':'externalId eq "external"'})).json()['totalResults'] == 0
    assert (await client.put(path,headers=admin_headers,json={'schemas':[GROUP],'displayName':'revive'})).status_code == 404
    assert (await client.patch('/admin/api/teams/'+gid,headers=admin_headers,json={'enabled':True})).status_code == 404
    historical = (await client.get('/admin/api/teams?include_deleted=true',headers=admin_headers)).json()
    archived = next(t for t in historical if t['id']==gid)
    assert archived['name']=='managed' and archived['scim_external_id']=='external'
    assert archived['deleted_at'] and archived['enabled'] is False
    assert all(t['id']!=gid for t in (await client.get('/admin/api/teams',headers=admin_headers)).json())
    replacement = await client.post('/scim/v2/Groups',headers=admin_headers,json={'schemas':[GROUP],
        'displayName':'managed','externalId':'external','members':[{'value':primary['id']}]})
    assert replacement.status_code == 201, replacement.text
    assert replacement.json()['id']!=gid
    async with db.session_factory() as session:
        assert (await session.get(db.APIKey,keys[0]['id'])).revoked
        assert (await session.get(db.APIKey,keys[1]['id'])).revoked
        assert not (await session.get(db.APIKey,keys[2]['id'])).revoked
        assert await session.get(db.TeamMembership,(secondary['id'],gid)) is None
        assert await session.scalar(select(func.count()).select_from(db.BrowserSession))==0
        usage = await session.scalar(select(db.Usage))
        assert usage.team_id==gid and usage.cost_micros==2
        assert await session.scalar(select(func.count()).select_from(db.Audit).where(db.Audit.action=='scim.group.delete'))==1
    for key in keys[:2]:
        assert (await client.get('/v1/models',headers={'Authorization':'Bearer '+key['key']})).status_code==401
    # Defense in depth: out-of-band enabled flag does not revive a tombstone.
    async with db.transaction() as session:
        (await session.get(db.Team,gid)).enabled=True
        user = await session.get(db.User,secondary['id'])
        assert not await db.active_membership(session,user,gid)
    assert (await client.post('/admin/api/users',headers=admin_headers,
        json={'email':'new@example.com','team_id':gid})).status_code==404
    assert (await client.put('/teams/api/'+gid+'/members/'+secondary['id'],headers=admin_headers,
        json={'role':'member'})).status_code==404


@pytest.mark.parametrize('kind',['primary','secondary','delegated'])
async def test_delete_protects_admin_groups(client,admin_headers,kind):
    gid=await group(client,admin_headers)
    member=await add_user(client,admin_headers)
    async with db.transaction() as session:
        admin=await session.scalar(select(db.User).where(db.User.role=='admin'))
        if kind=='primary':
            admin.team_id=gid
            (await session.scalar(select(db.APIKey).where(db.APIKey.user_id==admin.id))).team_id=gid
        else:
            session.add(db.TeamMembership(user_id=admin.id if kind=='secondary' else member['id'],
                team_id=gid,role='member' if kind=='secondary' else 'admin'))
    assert (await client.delete('/scim/v2/Groups/'+gid,headers=admin_headers)).status_code==403
    async with db.session_factory() as session:
        assert (await session.get(db.Team,gid)).deleted_at is None


async def test_concurrent_delete_one_audit_and_collision_retry(client,admin_headers,monkeypatch):
    gid=await group(client,admin_headers)
    async with db.transaction() as session:
        session.add(db.Team(name='deleted-collision',enabled=True))
    original=db.uid
    attempts=iter(['collision',original()])
    monkeypatch.setattr(db,'uid',lambda:next(attempts))
    results=await asyncio.gather(*(client.delete('/scim/v2/Groups/'+gid,headers=admin_headers) for _ in range(2)))
    assert [r.status_code for r in results]==[204,204]
    async with db.session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(db.Audit).where(db.Audit.action=='scim.group.delete'))==1


async def test_delete_failure_rolls_back_members_keys_history(client,admin_headers,monkeypatch):
    user=await add_user(client,admin_headers)
    gid=await group(client,admin_headers,[user['id']])
    key=(await client.post('/admin/api/keys',headers=admin_headers,json={'name':'key','user_id':user['id']})).json()
    async with db.transaction() as session:
        session.add(db.Team(name='deleted-collision',enabled=True))
    monkeypatch.setattr(db,'uid',lambda:'collision')
    result=await client.delete('/scim/v2/Groups/'+gid,headers=admin_headers)
    assert result.status_code==409
    async with db.session_factory() as session:
        assert (await session.get(db.User,user['id'])).team_id==gid
        assert not (await session.get(db.APIKey,key['id'])).revoked
        assert (await session.get(db.Team,gid)).deleted_at is None


async def test_stale_provisioning_scope_cannot_delete(client,admin_headers,monkeypatch):
    gid=await group(client,admin_headers)
    original=scim.revalidate_provisioning
    async def invalidated(session,identity):
        key=await session.get(db.APIKey,identity.key.id)
        key.permissions=['inference']
        await session.flush()
        return await original(session,identity)
    monkeypatch.setattr(scim,'revalidate_provisioning',invalidated)
    assert (await client.delete('/scim/v2/Groups/'+gid,headers=admin_headers)).status_code==403
    async with db.session_factory() as session:
        assert (await session.get(db.Team,gid)).deleted_at is None


async def test_oidc_mapping_cannot_resurrect_deleted_enabled_team(client, admin_headers, oidc, monkeypatch):  # noqa: F811
    import json
    from gateway import auth
    from gateway.config import get_settings
    from test_oidc import start
    gid = await group(client, admin_headers)
    assert (await client.delete('/scim/v2/Groups/'+gid, headers=admin_headers)).status_code == 204
    async with db.transaction() as session:
        (await session.get(db.Team,gid)).enabled = True
    monkeypatch.setenv('OIDC_GROUP_MAPPING',json.dumps({'operators':{'role':'member','team_id':gid}}))
    get_settings.cache_clear()
    auth.clear_oidc_cache()
    state = await start(client, oidc)
    result = await client.get('/auth/oidc/callback',params={'state':state,'code':'test'})
    assert result.status_code == 403
    assert not (await client.get('/auth/session')).json()['authenticated']
