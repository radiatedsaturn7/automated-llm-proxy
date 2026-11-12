"""Bounded browser issuance cannot multiply admission limits or revive credentials."""
import asyncio
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select, update

from gateway import db, policy
from gateway.security import Identity
from test_member_usage import fixture as base_fixture, NOW
from datetime import timedelta


async def fixture(client, monkeypatch):
    result = await base_fixture(client, monkeypatch)
    async with db.transaction() as session:
        await session.execute(update(db.APIKey).values(expires_at=NOW + timedelta(days=90)))
    return result

CSRF = {'X-CSRF-Token': 'csrf'}
POLICY = {'enabled': True, 'team_id': None, 'allowed_models': ['chat'],
          'permissions': ['inference'], 'rpm': 2, 'max_active_keys': 2, 'max_total_keys': 4}


async def setup(client, monkeypatch, admin_headers, **values):
    first, second, key, raw = await fixture(client, monkeypatch)
    body = {**POLICY, 'team_id': first.team_id, **values}
    result = await client.put(f'/admin/api/users/{first.id}/key-policy', json=body, headers=admin_headers)
    assert result.status_code == 200, result.text
    return first, second, key, raw, body


async def issue(client, operation=None, **extra):
    return await client.post('/account/api/keys', json={'name': 'Laptop', 'operation_id': operation or str(uuid4()), **extra}, headers=CSRF)


async def test_default_disabled_and_browser_only(client, monkeypatch):
    _, _, _, raw = await fixture(client, monkeypatch)
    assert not (await client.get('/account/api/key-policy')).json()['can_create']
    assert (await issue(client)).status_code == 409
    assert (await client.post('/account/api/keys', json={'name':'x','operation_id':str(uuid4())})).status_code == 403
    client.cookies.clear()
    assert (await client.get('/account/api/key-policy', headers={'Authorization':'Bearer '+raw})).status_code == 403


async def test_issue_fixed_scope_and_immutable_receipt(client, monkeypatch, admin_headers):
    first, second, _, _, _ = await setup(client, monkeypatch, admin_headers)
    operation = str(uuid4())
    result = await issue(client, operation)
    assert result.status_code == 201, result.text
    value = result.json()
    assert value['key'] and not value['replayed']
    key = value['credential']
    assert key['self_service'] and key['allowed_models'] == ['chat']
    assert key['permissions'] == ['inference'] and key['rpm'] == 2
    replay = (await issue(client, operation)).json()
    assert replay['key'] is None and replay['replayed']
    assert replay['credential']['id'] == key['id']
    assert (await issue(client, operation, name='Other')).status_code == 409
    client.cookies.set('gateway_session', 'second-cookie')
    assert (await issue(client, operation)).status_code == 409
    client.cookies.set('gateway_session', 'first-cookie')
    rotated = await client.post(f"/account/api/keys/{key['id']}/rotate", headers=CSRF)
    assert rotated.status_code == 200, rotated.text
    receipt = (await issue(client, operation)).json()
    assert receipt['credential']['id'] == key['id'] and receipt['credential']['revoked']
    assert receipt['key'] is None
    async with db.session_factory() as session:
        replacement = await session.get(db.APIKey, rotated.json()['id'])
        assert replacement.self_service and replacement.issuance_operation_id is None
        assert value['key'] not in replacement.digest


@pytest.mark.parametrize('extra', [{'permissions':['admin']}, {'allowed_models':[]}, {'team_id':None}, {'rpm':999}, {'expires_in_days':90}, {'kind':'agent'}])
async def test_member_cannot_choose_scope(client, monkeypatch, admin_headers, extra):
    await setup(client, monkeypatch, admin_headers)
    assert (await issue(client, **extra)).status_code == 422


@pytest.mark.parametrize('extra', [{'allowed_models':[]}, {'permissions':['admin']}, {'permissions':['account']}, {'max_active_keys':11}, {'max_total_keys':1001}, {'expires_in_days':91}])
async def test_invalid_admin_templates(client, monkeypatch, admin_headers, extra):
    first, _, _, _ = await fixture(client, monkeypatch)
    result = await client.put(f'/admin/api/users/{first.id}/key-policy', json={**POLICY,'team_id':first.team_id,**extra}, headers=admin_headers)
    assert result.status_code == 422


async def test_concurrent_idempotency_and_active_cap(client, monkeypatch, admin_headers):
    await setup(client, monkeypatch, admin_headers, max_active_keys=1)
    operation = str(uuid4())
    results = await asyncio.gather(issue(client, operation), issue(client, operation))
    assert [r.status_code for r in results] == [201,201]
    assert sum(bool(r.json()['key']) for r in results) == 1
    assert (await issue(client)).status_code == 409


async def test_lifetime_cap_survives_rotation_and_rolls_back(client, monkeypatch, admin_headers):
    await setup(client, monkeypatch, admin_headers, max_active_keys=1, max_total_keys=2)
    key = (await issue(client)).json()['credential']
    first = await client.post(f"/account/api/keys/{key['id']}/rotate", headers=CSRF)
    assert first.status_code == 200, first.text
    second = await client.post(f"/account/api/keys/{first.json()['id']}/rotate", headers=CSRF)
    assert second.status_code == 409
    async with db.session_factory() as session:
        assert not (await session.get(db.APIKey, first.json()['id'])).revoked
    await client.post(f"/account/api/keys/{first.json()['id']}/revoke", headers=CSRF)
    assert (await issue(client)).status_code == 409


async def test_policy_changes_revoke_only_self_origin(client, monkeypatch, admin_headers):
    first, _, original, _, body = await setup(client, monkeypatch, admin_headers)
    key = (await issue(client)).json()['credential']
    url = f'/admin/api/users/{first.id}/key-policy'
    same = (await client.put(url,json=body,headers=admin_headers)).json()
    assert not same['changed']
    changed = (await client.put(url,json={**body,'rpm':1},headers=admin_headers)).json()
    assert changed['changed'] and changed['existing_self_service_keys_revoked']
    async with db.session_factory() as session:
        assert (await session.get(db.APIKey,key['id'])).revoked
        assert not (await session.get(db.APIKey,original.id)).revoked


async def test_shared_rate_pool_survives_revoke_and_rotation(client, monkeypatch, admin_headers):
    first, _, _, _, _ = await setup(client, monkeypatch, admin_headers)
    key1 = (await issue(client)).json()['credential']
    key2 = (await issue(client)).json()['credential']
    async def identity(key_id):
        async with db.session_factory() as session:
            key = await session.get(db.APIKey,key_id)
            return Identity(user=first,key=key)
    await policy.reserve(await identity(key1['id']), 1, 1, 'chat')
    await policy.reserve(await identity(key2['id']), 1, 1, 'chat')
    rotated = await client.post(f"/account/api/keys/{key1['id']}/rotate",headers=CSRF)
    assert rotated.status_code == 200
    with pytest.raises(HTTPException) as caught:
        await policy.reserve(await identity(rotated.json()['id']),1,1,'chat')
    assert caught.value.status_code == 429
    await client.post(f"/account/api/keys/{key2['id']}/revoke",headers=CSRF)
    third = await issue(client)
    assert third.status_code == 201
    with pytest.raises(HTTPException):
        await policy.reserve(await identity(third.json()['credential']['id']),1,1,'chat')
    async with db.session_factory() as session:
        buckets = (await session.scalars(select(db.RateBucket))).all()
        assert len(buckets) == 1 and buckets[0].requests == 2


async def test_distinct_operations_race_respects_active_cap(client, monkeypatch, admin_headers):
    await setup(client, monkeypatch, admin_headers, max_active_keys=1)
    results = await asyncio.gather(issue(client), issue(client))
    assert sorted(r.status_code for r in results) == [201, 409]


async def test_admin_rotation_cannot_exceed_version_cap(client, monkeypatch, admin_headers):
    await setup(client, monkeypatch, admin_headers, max_active_keys=1, max_total_keys=1)
    key = (await issue(client)).json()['credential']
    result = await client.post(f"/admin/api/keys/{key['id']}/rotate", headers=admin_headers)
    assert result.status_code == 409, result.text
    async with db.session_factory() as session:
        assert not (await session.get(db.APIKey, key['id'])).revoked


async def test_shared_budget_and_daily_allowance_remain_enforced(client, monkeypatch, admin_headers):
    first, _, _, _, _ = await setup(client, monkeypatch, admin_headers)
    a = (await issue(client)).json()['credential']
    b = (await issue(client)).json()['credential']
    async with db.transaction() as session:
        user = await session.get(db.User, first.id)
        user.budget_micros = 5
        session.add(db.UserAllowance(user_id=first.id, daily_request_limit=1, monthly_reset_limit=0))
    async with db.session_factory() as session:
        ka, kb = await session.get(db.APIKey, a['id']), await session.get(db.APIKey, b['id'])
    await policy.reserve(Identity(user=first, key=ka), 4, 1, 'chat')
    with pytest.raises(HTTPException, match='budget') as caught:
        await policy.reserve(Identity(user=first, key=kb), 4, 1, 'chat')
    assert caught.value.status_code == 429
    with pytest.raises(HTTPException) as caught:
        await policy.reserve(Identity(user=first, key=kb), 0, 1, 'chat')
    assert caught.value.status_code == 429 and 'allowance' in caught.value.detail.lower()


async def test_lowered_policy_keeps_consumed_pool(client, monkeypatch, admin_headers):
    first, _, _, _, body = await setup(client, monkeypatch, admin_headers)
    key = (await issue(client)).json()['credential']
    async with db.session_factory() as session:
        item = await session.get(db.APIKey, key['id'])
    await policy.reserve(Identity(user=first,key=item),1,1,'chat')
    await client.put(f'/admin/api/users/{first.id}/key-policy',json={**body,'rpm':1},headers=admin_headers)
    new = (await issue(client)).json()['credential']
    async with db.session_factory() as session:
        item = await session.get(db.APIKey,new['id'])
    with pytest.raises(HTTPException) as caught:
        await policy.reserve(Identity(user=first,key=item),1,1,'chat')
    assert caught.value.status_code == 429


async def test_team_rotation_preserves_origin_and_cap(client, monkeypatch, admin_headers):
    first, _, _, _, _ = await setup(client, monkeypatch, admin_headers, max_active_keys=1, max_total_keys=2)
    key = (await issue(client)).json()['credential']
    result = await client.post(f"/teams/api/{first.team_id}/keys/{key['id']}/rotate", headers=admin_headers)
    assert result.status_code == 200, result.text
    assert result.json()['self_service']
    assert (await client.post(f"/teams/api/{first.team_id}/keys/{result.json()['id']}/rotate", headers=admin_headers)).status_code == 409


async def test_removed_membership_blocks_issuance(client, monkeypatch, admin_headers):
    first, _, _, _, _ = await setup(client, monkeypatch, admin_headers)
    async with db.transaction() as session:
        user = await session.get(db.User,first.id)
        user.team_id = None
    assert not (await client.get('/account/api/key-policy')).json()['can_create']
    assert (await issue(client)).status_code == 409


async def test_issuance_revalidates_revoked_session_snapshot(client, monkeypatch, admin_headers):
    from sqlalchemy import delete
    from starlette.requests import Request
    from gateway.main import app
    from gateway.account import browser_member
    from gateway.security import identify
    first, _, _, _, _ = await setup(client, monkeypatch, admin_headers)
    snapshot = await identify(Request({'type':'http','method':'GET',
                                      'headers':[(b'cookie',b'gateway_session=first-cookie')]}),allow_session=True)
    async with db.transaction() as session:
        await session.execute(delete(db.BrowserSession).where(db.BrowserSession.user_id==first.id))
    app.dependency_overrides[browser_member] = lambda: snapshot
    try:
        assert (await issue(client)).status_code == 401
    finally:
        app.dependency_overrides.pop(browser_member,None)
