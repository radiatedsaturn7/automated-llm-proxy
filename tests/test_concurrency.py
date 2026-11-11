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
