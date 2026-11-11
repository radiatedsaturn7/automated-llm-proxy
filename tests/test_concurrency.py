"""Admission correctness stress tests, not a throughput benchmark."""
import asyncio
import pytest
from sqlalchemy import select
from gateway import db
from gateway import policy
from gateway.security import Identity


async def make_identity(client, admin_headers):
    team = (await client.post('/admin/api/teams', headers=admin_headers, json={'name':'load','budget_usd':'0.001'})).json()
    user = (await client.post('/admin/api/users', headers=admin_headers, json={'email':'load@example.com','team_id':team['id']})).json()
    key = (await client.post('/admin/api/keys', headers=admin_headers, json={'name':'load','user_id':user['id'],'rpm':1000})).json()
    async with db.session_factory() as session:
        k = await session.get(db.APIKey, key['id'])
        u = await session.get(db.User, user['id'])
    return Identity(user=u,key=k)


async def test_fifty_concurrent_reservations_cannot_overspend_team(client, admin_headers):
    identity = await make_identity(client, admin_headers)
    async def admit():
        from fastapi import HTTPException
        try:
            return await policy.reserve(identity, 100, 1, 'test')
        except HTTPException as exc:
            assert exc.status_code == 429
            return None
    results = await asyncio.gather(*(admit() for _ in range(50)))
    assert len([x for x in results if x]) == 10
    async with db.session_factory() as session:
        pending = list((await session.scalars(select(db.Reservation))).all())
        assert sum(r.amount_micros for r in pending) == 1000


@pytest.mark.parametrize('tokens',[2**31, 10**60, -1, True])
async def test_unrepresentable_provider_usage_is_estimated(client, admin_headers, tokens):
    identity = await make_identity(client, admin_headers)
    rid = await policy.reserve(identity, 100, 1, 'test')
    await policy.finalize(rid, 'test', 'provider', {'input_cost_per_million':1,'output_cost_per_million':1},
                          {'prompt_tokens':tokens,'completion_tokens':1})
    async with db.session_factory() as session:
        usage = await session.scalar(select(db.Usage).where(db.Usage.reservation_id==rid))
        assert usage.estimated
        assert usage.cost_micros == 100


async def test_liveness_and_admin_assets_available(client):
    assert (await client.get('/health/live')).status_code == 200
    assert (await client.get('/health/ready')).status_code == 200
    assert (await client.get('/admin')).status_code == 200
    assert (await client.get('/admin/static/app.js')).status_code == 200


async def test_cross_team_concurrent_keys_share_one_user_budget(client, admin_headers):
    """Different team locks must still serialize on the shared user's allowance."""
    from fastapi import HTTPException

    teams = []
    for name in ('north', 'south'):
        response = await client.post('/admin/api/teams', headers=admin_headers,
                                     json={'name': name, 'budget_usd': '1'})
        assert response.status_code == 201, response.text
        teams.append(response.json())
    response = await client.post('/admin/api/users', headers=admin_headers,
                                json={'email': 'shared@example.com', 'team_id': teams[0]['id'],
                                      'budget_usd': '0.001'})
    assert response.status_code == 201, response.text
    user = response.json()
    response = await client.put(f"/teams/api/{teams[1]['id']}/members/{user['id']}",
                                headers=admin_headers, json={'role': 'member'})
    assert response.status_code == 200, response.text
    identities = []
    for team in teams:
        response = await client.post('/admin/api/keys', headers=admin_headers,
                                     json={'name': team['name'], 'user_id': user['id'],
                                           'team_id': team['id'], 'rpm': 1000})
        assert response.status_code == 201, response.text
        async with db.session_factory() as session:
            identities.append(Identity(user=await session.get(db.User, user['id']),
                                       key=await session.get(db.APIKey, response.json()['id'])))

    async def admit(index):
        try:
            return await policy.reserve(identities[index % 2], 100, 1, 'test')
        except HTTPException as exc:
            assert exc.status_code == 429
            assert exc.detail == 'User monthly budget exceeded'
            return None

    results = await asyncio.gather(*(admit(i) for i in range(50)))
    assert sum(result is not None for result in results) == 10
    async with db.session_factory() as session:
        pending = list((await session.scalars(select(db.Reservation).where(
            db.Reservation.user_id == user['id']))).all())
        assert sum(row.amount_micros for row in pending) == 1000
        assert {row.team_id for row in pending} <= {team['id'] for team in teams}
