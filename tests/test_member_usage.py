"""Member history is bounded, cursor-scoped, read-only, and excludes private identifiers."""
from datetime import datetime, timedelta, timezone
from uuid import UUID
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, func, select
from starlette.requests import Request

from gateway import db, exports, member_usage
from gateway.account import browser_member
from gateway.main import app
from gateway.security import digest, identify, new_key

NOW = datetime(2030, 6, 12, 12, tzinfo=timezone.utc)


async def fixture(client, monkeypatch):
    monkeypatch.setattr(db, 'now', lambda: NOW)
    monkeypatch.setattr(db, 'database_now', AsyncMock(return_value=NOW))
    async with db.transaction() as session:
        team = db.Team(name='Shared team')
        session.add(team)
        await session.flush()
        first = db.User(email='first@example.test', team_id=team.id)
        second = db.User(email='second@example.test', team_id=team.id)
        session.add_all([first, second])
        await session.flush()
        raw, key = new_key(first.id, team.id, 'credential', permissions=['inference', 'account'])
        session.add(key)
        for user, cookie in ((first, 'first-cookie'), (second, 'second-cookie')):
            session.add(db.BrowserSession(digest=digest(cookie), user_id=user.id, csrf='csrf',
                                         expires_at=NOW + timedelta(days=1)))
        await session.flush()
    client.cookies.set('gateway_session', 'first-cookie')
    return first, second, key, raw


def row(user, key, n, *, time=None, model='chat', estimated=False):
    return db.Usage(id=str(UUID(int=n)), reservation_id='private-reservation-' + str(n),
                    user_id=user.id, team_id=user.team_id, key_id=key.id,
                    period='2030-06', model=model, provider_id='private-provider-id',
                    input_tokens=11, output_tokens=13, cost_micros=n * 10, estimated=estimated,
                    created_at=time or NOW - timedelta(days=1))


async def test_only_own_settled_rows_with_explicit_safe_fields(client, monkeypatch):
    first, second, key, raw = await fixture(client, monkeypatch)
    async with db.transaction() as session:
        session.add_all([row(first, key, 1), row(first, key, 2, estimated=True), row(second, key, 3)])
        session.add(db.Reservation(user_id=first.id, key_id=key.id, team_id=first.team_id,
                                   period='2030-06', amount_micros=1000))
    response = await client.get('/account/api/usage')
    assert response.status_code == 200, response.text
    result = response.json()
    assert [item['id'] for item in result['records']] == [str(UUID(int=2)), str(UUID(int=1))]
    assert result['records'][0]['estimated'] is True
    assert result['records'][0]['cost_micros'] == 20
    assert result['next_cursor'] is None
    assert result['since'] == (NOW - timedelta(days=30)).isoformat()
    assert result['until'] == NOW.isoformat()
    assert set(result['records'][0]) == {'id', 'created_at', 'model', 'input_tokens', 'output_tokens', 'cost_micros', 'estimated'}
    for forbidden in (raw, first.id, second.id, first.team_id, key.id, 'private-provider-id', 'private-reservation'):
        assert forbidden not in response.text
    async with db.session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(db.Usage)) == 3
        assert await session.scalar(select(func.count()).select_from(db.Reservation)) == 1


async def test_keyset_ties_and_fixed_window(client, monkeypatch):
    first, second, key, _ = await fixture(client, monkeypatch)
    async with db.transaction() as session:
        session.add_all([row(first, key, i) for i in range(1, 6)])
    first_page = (await client.get('/account/api/usage', params={'limit': 2})).json()
    assert [r['id'] for r in first_page['records']] == [str(UUID(int=5)), str(UUID(int=4))]
    async with db.transaction() as session:
        session.add(row(first, key, 6, time=NOW + timedelta(seconds=1)))
    monkeypatch.setattr(db, 'database_now', AsyncMock(return_value=NOW + timedelta(hours=1)))
    seen = first_page['records'][:]
    cursor = first_page['next_cursor']
    while cursor:
        result = (await client.get('/account/api/usage', params={'cursor': cursor, 'limit': 2})).json()
        assert result['until'] == NOW.isoformat()
        seen.extend(result['records'])
        cursor = result['next_cursor']
    assert [r['id'] for r in seen] == [str(UUID(int=i)) for i in (5, 4, 3, 2, 1)]


async def test_window_bounds_and_timezone_normalization(client, monkeypatch):
    first, _, key, _ = await fixture(client, monkeypatch)
    start, end = NOW - timedelta(days=2), NOW - timedelta(days=1)
    async with db.transaction() as session:
        session.add_all([row(first, key, 1, time=start - timedelta(microseconds=1)),
                         row(first, key, 2, time=start), row(first, key, 3, time=end)])
    result = (await client.get('/account/api/usage', params={
        'since': start.astimezone(timezone(timedelta(hours=-5))).isoformat(), 'until': end.isoformat()})).json()
    assert result['since'] == start.isoformat()
    assert [item['id'] for item in result['records']] == [str(UUID(int=2))]


@pytest.mark.parametrize('params,status', [
    ({'limit': 0}, 422), ({'limit': 101}, 422), ({'limit': 'oops'}, 422),
    ({'since': '0001-01-01T00:00:00+01:00'}, 400),
    ({'until': '9999-12-31T23:59:59-01:00'}, 400),
    ({'until': '0001-01-01T00:00:00Z'}, 400),
    ({'since': 'bad'}, 400), ({'since': '2030-06-01'}, 400),
    ({'since': '2030-06-13T00:00:00Z'}, 400), ({'until': '2031-01-01T00:00:00Z'}, 400),
    ({'since': '2030-01-01T00:00:00Z'}, 400), ({'cursor': 'not-a-cursor'}, 400),
    ({'cursor': 'x' * 2049}, 422),
])
async def test_invalid_or_unbounded_queries_rejected(client, monkeypatch, params, status):
    await fixture(client, monkeypatch)
    assert (await client.get('/account/api/usage', params=params)).status_code == status


async def test_cursor_tampering_cross_user_and_cross_endpoint_rejected(client, monkeypatch):
    first, _, key, _ = await fixture(client, monkeypatch)
    async with db.transaction() as session:
        session.add_all([row(first, key, 1), row(first, key, 2)])
    cursor = (await client.get('/account/api/usage', params={'limit': 1})).json()['next_cursor']
    assert (await client.get('/account/api/usage', params={'cursor': cursor, 'since': NOW.isoformat()})).status_code == 400
    corrupted = ('A' if cursor[0] != 'A' else 'B') + cursor[1:]
    assert (await client.get('/account/api/usage', params={'cursor': corrupted})).status_code == 400
    value = member_usage.decode_cursor(cursor, first.id)
    assert (await client.get('/account/api/usage', params={'cursor': exports.encode(value)})).status_code == 400
    client.cookies.set('gateway_session', 'second-cookie')
    assert (await client.get('/account/api/usage', params={'cursor': cursor})).status_code == 400
    assert (await client.get('/account/api/usage')).json()['records'] == []


async def test_browser_only_disabled_and_revoked_sessions(client, monkeypatch):
    first, _, key, raw = await fixture(client, monkeypatch)
    auth = {'Authorization': 'Bearer ' + raw}
    assert (await client.get('/account/api/usage', headers=auth)).status_code == 403
    async with db.transaction() as session:
        agent_raw, agent = new_key(first.id, first.team_id, 'agent', kind='agent')
        session.add(agent)
    import jwt
    from gateway.config import get_settings
    actual_now = int(datetime.now(timezone.utc).timestamp())
    token = jwt.encode({'sub': first.id, 'kid': agent.id, 'gen': 0, 'iat': actual_now,
                        'exp': actual_now + 300, 'aud': 'llm-gateway', 'iss': 'llm-gateway'},
                       get_settings().gateway_signing_key, algorithm='HS256')
    assert (await client.get('/account/api/usage', headers={'Authorization': 'Bearer ' + token})).status_code == 403
    async with db.transaction() as session:
        (await session.get(db.User, first.id)).enabled = False
    assert (await client.get('/account/api/usage')).status_code == 401
    client.cookies.clear()
    assert (await client.get('/account/api/usage')).status_code == 401


async def test_read_revalidates_revoked_dependency_snapshot(client, monkeypatch):
    first, _, key, _ = await fixture(client, monkeypatch)
    scope = {'type': 'http', 'method': 'GET', 'headers': [(b'cookie', b'gateway_session=first-cookie')]}
    snapshot = await identify(Request(scope), allow_session=True)
    async with db.transaction() as session:
        session.add(row(first, key, 1))
        await session.execute(delete(db.BrowserSession).where(db.BrowserSession.user_id == first.id))
    app.dependency_overrides[browser_member] = lambda: snapshot
    try:
        response = await client.get('/account/api/usage')
        assert response.status_code == 401
        assert 'private' not in response.text
    finally:
        app.dependency_overrides.pop(browser_member, None)


async def test_empty_history_and_replica_clock_skew(client, monkeypatch):
    first, _, key, _ = await fixture(client, monkeypatch)
    monkeypatch.setattr(db, 'now', lambda: NOW - timedelta(days=365))
    result = (await client.get('/account/api/usage')).json()
    assert result['until'] == NOW.isoformat()
    assert result['records'] == [] and result['next_cursor'] is None
