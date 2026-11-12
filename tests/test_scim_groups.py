import asyncio

import pytest
from sqlalchemy import select

from gateway import db, scim
from gateway.scim import GROUP, PREFIX
from test_scim import add_user


async def group(client, headers, members=()):
    result = await client.post('/scim/v2/Groups', headers=headers, json={
        'schemas': [GROUP], 'displayName': 'managed', 'externalId': 'external',
        'members': [{'value': uid} for uid in members]})
    assert result.status_code == 201, result.text
    return result.json()['id']


async def test_replace_omissions_and_rollback(client, admin_headers):
    user = await add_user(client, admin_headers)
    gid = await group(client, admin_headers, [user['id']])
    path = '/scim/v2/Groups/' + gid
    body = {'schemas': [GROUP], 'displayName': 'changed', 'members': [{'value': 'unknown'}]}
    assert (await client.put(path, headers=admin_headers, json=body)).status_code == 400
    original = (await client.get(path, headers=admin_headers)).json()
    assert original['displayName'] == 'managed' and original['members']
    replacement = {'schemas': [GROUP], 'id': gid, 'displayName': 'changed'}
    result = await client.put(path, headers=admin_headers, json=replacement)
    assert result.status_code == 200 and result.json()['members'] == []
    assert 'externalId' not in result.json()
    assert (await client.put(path, headers=admin_headers, json=replacement)).status_code == 200
    for invalid in ({'id': 'wrong'}, {'budget_usd': 50}, {'active': True}, {'members': None}, {'displayName': ' '}):
        assert (await client.put(path, headers=admin_headers, json={**replacement, **invalid})).status_code == 400


@pytest.mark.parametrize('kind', ['primary', 'secondary', 'delegated'])
async def test_admin_group_protection(client, admin_headers, kind):
    gid = await group(client, admin_headers)
    member = await add_user(client, admin_headers)
    async with db.transaction() as session:
        admin = await session.scalar(select(db.User).where(db.User.role == 'admin'))
        if kind == 'primary':
            admin.team_id = gid
            # Keep credential bound to the administrator's active primary team.
            await session.flush()
            key = await session.scalar(select(db.APIKey).where(db.APIKey.user_id == admin.id))
            key.team_id = gid
        else:
            session.add(db.TeamMembership(user_id=admin.id if kind=='secondary' else member['id'],
                team_id=gid, role='member' if kind=='secondary' else 'admin'))
    path = '/scim/v2/Groups/' + gid
    assert (await client.put(path, headers=admin_headers, json={'schemas': [GROUP], 'displayName': 'no'})).status_code == 403
    assert (await client.patch(path, headers=admin_headers, json={'schemas': [PREFIX+'api:messages:2.0:PatchOp'], 'Operations': [{'op': 'replace', 'path': 'displayName', 'value': 'no'}]})).status_code == 403


async def test_live_scope_revoked_after_dependency(client, admin_headers, monkeypatch):
    gid = await group(client, admin_headers)
    users = (await client.get('/admin/api/users', headers=admin_headers)).json()
    aid = next(u['id'] for u in users if u['role']=='admin')
    response = await client.post('/admin/api/keys', headers=admin_headers,
        json={'name': 'scim-only', 'user_id': aid, 'permissions': ['provisioning']})
    data = response.json()
    entered, release = asyncio.Event(), asyncio.Event()
    original = scim.payload

    async def paused(request, allowed):
        body = await original(request, allowed)
        entered.set()
        await release.wait()
        return body
    monkeypatch.setattr(scim, 'payload', paused)
    pending = asyncio.create_task(client.put('/scim/v2/Groups/'+gid,
        headers={'Authorization':'Bearer '+data['key']},
        json={'schemas':[GROUP], 'displayName':'must-not-change'}))
    await entered.wait()
    async with db.transaction() as session:
        (await session.get(db.APIKey, data['id'])).revoked = True
    release.set()
    result = await pending
    assert result.status_code in {401,403}
    async with db.session_factory() as session:
        assert (await session.get(db.Team, gid)).name == 'managed'


async def test_primary_removal_clears_duplicate_not_unrelated(client, admin_headers):
    user = await add_user(client, admin_headers)
    gid = await group(client, admin_headers, [user['id']])
    async with db.transaction() as session:
        session.add(db.Team(id='other', name='other', enabled=True))
        await session.flush()
        session.add(db.TeamMembership(user_id=user['id'], team_id=gid, role='member'))
        session.add(db.TeamMembership(user_id=user['id'], team_id='other', role='member'))
    path = '/scim/v2/Groups/'+gid
    result = await client.put(path, headers=admin_headers, json={'schemas':[GROUP], 'displayName':'managed'})
    assert result.status_code == 200
    async with db.session_factory() as session:
        assert await session.get(db.TeamMembership, (user['id'], gid)) is None
        assert await session.get(db.TeamMembership, (user['id'], 'other')) is not None
    denied = await client.post('/admin/api/keys', headers=admin_headers,
        json={'name':'no-new-access', 'user_id':user['id'], 'team_id':gid})
    assert denied.status_code == 400


async def test_disabled_and_uniqueness_failure_preserve_membership(client, admin_headers):
    user = await add_user(client, admin_headers)
    gid = await group(client, admin_headers, [user['id']])
    async with db.transaction() as session:
        session.add(db.Team(name='occupied', enabled=True))
    path = '/scim/v2/Groups/'+gid
    assert (await client.put(path, headers=admin_headers,
        json={'schemas':[GROUP], 'displayName':'occupied'})).status_code == 409
    assert (await client.get(path, headers=admin_headers)).json()['members'][0]['value'] == user['id']
    async with db.transaction() as session:
        (await session.get(db.Team, gid)).enabled = False
    assert (await client.put(path, headers=admin_headers,
        json={'schemas':[GROUP], 'displayName':'managed'})).status_code == 409


async def test_later_patch_failure_restores_duplicate_binding_and_keys(client, admin_headers):
    user = await add_user(client, admin_headers)
    gid = await group(client, admin_headers, [user['id']])
    async with db.transaction() as session:
        session.add(db.TeamMembership(user_id=user['id'], team_id=gid, role='member'))
    issued = await client.post('/admin/api/keys', headers=admin_headers,
        json={'name':'kept', 'user_id':user['id'], 'team_id':gid})
    key_id = issued.json()['id']
    body = {'schemas':[PREFIX+'api:messages:2.0:PatchOp'], 'Operations':[
        {'op':'replace', 'path':'members', 'value':[]},
        {'op':'replace', 'path':'unsupported', 'value':'reject'}]}
    assert (await client.patch('/scim/v2/Groups/'+gid, headers=admin_headers, json=body)).status_code == 400
    async with db.session_factory() as session:
        assert (await session.get(db.User, user['id'])).team_id == gid
        assert await session.get(db.TeamMembership, (user['id'], gid)) is not None
        assert not (await session.get(db.APIKey, key_id)).revoked
