"""Cheap per-probe ledger validation; full drift inspection remains preflight."""
from sqlalchemy import delete, insert

from gateway import db
from gateway.migrate import HEAD, versions


async def test_readiness_rejects_incomplete_migration_ledger(client):
    assert (await client.get('/health/ready')).status_code == 200
    async with db.engine.begin() as conn:
        await conn.execute(delete(versions).where(versions.c.version == 1))
    response = await client.get('/health/ready')
    assert response.status_code == 503
    assert response.json() == {'detail': 'Database/schema unavailable'}
    assert (await client.get('/health/live')).status_code == 200


async def test_old_replica_rejects_newer_schema_revision(client):
    async with db.engine.begin() as conn:
        await conn.execute(insert(versions).values(version=HEAD + 1, applied_at=db.now()))
    assert (await client.get('/health/ready')).status_code == 503
    assert (await client.get('/health/live')).status_code == 200


async def test_unhashable_message_roles_return_validation_error(client, admin_headers):
    for role in ([], {}, 12, None):
        for endpoint, field in (('/v1/chat/completions', 'messages'), ('/v1/responses', 'input')):
            response = await client.post(endpoint, headers=admin_headers,
                                         json={'model': 'chat', field: [{'role': role, 'content': 'hello'}]})
            assert response.status_code == 400, response.text


async def test_unhashable_responses_part_type_returns_validation_error(client, admin_headers):
    response = await client.post('/v1/responses', headers=admin_headers, json={
        'model': 'chat', 'input': [{'role': 'user', 'content': [{'type': [], 'text': 'hello'}]}]})
    assert response.status_code == 400
