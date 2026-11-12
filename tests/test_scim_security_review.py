import asyncio
import pytest
from gateway import db, scim
from tests.conftest import client as client, admin_headers as admin_headers
from tests.test_scim import add_user
from tests.test_scim_groups import group
from gateway.scim import GROUP, PREFIX

async def test_remove_readd_does_not_resurrect_key_or_duplicate(client, admin_headers):
    user = await add_user(client, admin_headers)
    gid = await group(client, admin_headers, [user['id']])
    async with db.transaction() as session:
        session.add(db.TeamMembership(user_id=user['id'], team_id=gid, role='member'))
    response = await client.post('/admin/api/keys', headers=admin_headers, json={
        'name':'old','user_id':user['id'],'team_id':gid})
    key = response.json()
    response = await client.patch('/scim/v2/Groups/'+gid, headers=admin_headers,json={
        'schemas':[PREFIX+'api:messages:2.0:PatchOp'], 'Operations':[
            {'op':'replace','path':'members','value':[]},
            {'op':'add','path':'members','value':[{'value':user['id']}]}]})
    assert response.status_code == 200, response.text
    async with db.session_factory() as session:
        assert (await session.get(db.User,user['id'])).team_id == gid
        assert (await session.get(db.APIKey,key['id'])).revoked
        assert await session.get(db.TeamMembership,(user['id'],gid)) is None

@pytest.mark.parametrize('method', ['put','patch'])
async def test_late_delegated_admin_protection(client, admin_headers, monkeypatch, method):
    user = await add_user(client,admin_headers)
    gid = await group(client,admin_headers,[user['id']])
    entered,release=asyncio.Event(),asyncio.Event()
    original=scim.payload
    async def pause(*args):
        result=await original(*args)
        entered.set()
        await release.wait()
        return result
    monkeypatch.setattr(scim,'payload',pause)
    body=({'schemas':[GROUP],'displayName':'unauthorized'} if method=='put' else {
        'schemas':[PREFIX+'api:messages:2.0:PatchOp'],'Operations':[{'op':'replace','path':'members','value':[]}]})
    task=asyncio.create_task(getattr(client,method)('/scim/v2/Groups/'+gid,headers=admin_headers,json=body))
    await asyncio.wait_for(entered.wait(),5)
    async with db.transaction() as session:
        session.add(db.TeamMembership(user_id=user['id'],team_id=gid,role='admin'))
    release.set()
    response=await task
    assert response.status_code==403,response.text
    async with db.session_factory() as session:
        assert (await session.get(db.Team,gid)).name=='managed'
        assert (await session.get(db.User,user['id'])).team_id==gid
