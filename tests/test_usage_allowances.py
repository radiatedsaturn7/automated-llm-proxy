"""Self-service product allowances never erase cost, reservations or rate controls."""
import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from gateway import db, policy
from gateway.security import Identity, digest, new_key

DAY = datetime(2030, 1, 30, 12, tzinfo=timezone.utc)
CSRF = {"X-CSRF-Token": "allowance-csrf"}


async def fixture(client, admin_headers, monkeypatch, *, limit=1, resets=4, rpm=1000, budget=100000):
    monkeypatch.setattr(db, "database_now", AsyncMock(return_value=DAY))
    monkeypatch.setattr(db, "now", lambda: DAY)
    async with db.transaction() as s:
        for admin_key in (await s.scalars(select(db.APIKey))).all():
            admin_key.expires_at = DAY + timedelta(days=365)
    user = (await client.post('/admin/api/users', headers=admin_headers,
                             json={"email": "allowance@example.test"})).json()
    async with db.transaction() as s:
        stored = await s.get(db.User, user['id'])
        stored.budget_micros = budget
        raw, key = new_key(user['id'], None, 'test', permissions=['inference', 'account'], rpm=rpm)
        s.add(key)
        s.add(db.BrowserSession(digest=digest('allowance-browser'), user_id=user['id'],
                               csrf=CSRF['X-CSRF-Token'], expires_at=DAY + timedelta(days=60)))
        await s.flush()
    client.cookies.set('gateway_session', 'allowance-browser')
    configured = await client.put(f"/admin/api/users/{user['id']}/usage-allowance", headers=admin_headers,
                                  json={"daily_request_limit": limit, "monthly_reset_limit": resets})
    assert configured.status_code == 200, configured.text
    return Identity(stored, key), raw


async def reset(client, *, op=None, day=DAY, headers=None):
    return await client.post('/account/api/reset-usage', headers=CSRF if headers is None else headers,
                             json={'operation_id': op or str(uuid4()), 'expected_day': day.strftime('%Y-%m-%d')})


async def reserve(identity, amount=1):
    return await policy.reserve(identity, amount, 10, 'chat')


async def test_quota_exhaustion_four_resets_and_no_accounting_erasure(client, admin_headers, monkeypatch):
    identity, _ = await fixture(client, admin_headers, monkeypatch, limit=1)
    for index in range(5):
        await reserve(identity)
        with pytest.raises(HTTPException) as denied:
            await reserve(identity)
        assert denied.value.status_code == 429
        assert 'allowance' in denied.value.detail
        result = await reset(client)
        assert result.status_code == (200 if index < 4 else 409), result.text
    async with db.session_factory() as s:
        assert await s.scalar(select(func.count()).select_from(db.Reservation)) == 5
        assert await s.scalar(select(func.sum(db.Reservation.amount_micros))) == 5
        assert (await s.scalar(select(db.RateBucket))).requests == 5
        assert await s.scalar(select(func.count()).select_from(db.Audit).where(db.Audit.action == 'account.usage.reset')) == 4
    view = (await client.get('/account/api/usage-allowance')).json()
    assert view['resets_remaining'] == 0 and view['requests_remaining'] == 0


@pytest.mark.parametrize('limit_kind', ['user', 'team', 'rpm', 'tpm'])
async def test_reset_never_bypasses_hard_limits(client, admin_headers, monkeypatch, limit_kind):
    identity, _ = await fixture(client, admin_headers, monkeypatch, limit=1,
                                budget=1 if limit_kind == 'user' else 100000,
                                rpm=1 if limit_kind == 'rpm' else 1000)
    async with db.transaction() as s:
        if limit_kind == 'tpm':
            (await s.get(db.APIKey, identity.key.id)).tpm = 10
        if limit_kind == 'team':
            team = db.Team(name='limited-team', budget_micros=1)
            s.add(team)
            await s.flush()
            (await s.get(db.User, identity.user.id)).team_id = team.id
            (await s.get(db.APIKey, identity.key.id)).team_id = team.id
    reservation = await reserve(identity)
    if limit_kind == 'user':
        await policy.finalize(reservation, 'chat', 'provider', {}, usage=None)
    assert (await reset(client)).status_code == 200
    with pytest.raises(HTTPException) as denied:
        await reserve(identity)
    assert denied.value.status_code == 429
    assert ('budget' if limit_kind in ('user', 'team') else 'Rate') in denied.value.detail
    async with db.session_factory() as s:
        row = await s.get(db.UserAllowance, identity.user.id)
        assert row.requests_used == 0  # Failed hard admission never spends allowance.
        assert await s.scalar(select(func.count()).select_from(db.Reservation)) == 1
        if limit_kind == 'user':
            assert await s.scalar(select(func.sum(db.Usage.cost_micros))) == 1


async def test_disabled_default_and_admin_constraints(client, admin_headers, monkeypatch):
    identity, _ = await fixture(client, admin_headers, monkeypatch, limit=None, resets=0)
    assert not (await client.get('/account/api/usage-allowance')).json()['enabled']
    for _ in range(3):
        await reserve(identity)
    assert (await reset(client)).status_code == 409
    path = f'/admin/api/users/{identity.user.id}/usage-allowance'
    for payload in ({'daily_request_limit': 0}, {'monthly_reset_limit': 5}, {'monthly_reset_limit': -1}, {'unknown': 1}):
        assert (await client.put(path, headers=admin_headers, json=payload)).status_code == 422
    assert (await client.put(path, headers=CSRF, json={'daily_request_limit': 99})).status_code == 403


async def test_key_rotation_and_sibling_keys_share_allowance(client, admin_headers, monkeypatch):
    identity, raw = await fixture(client, admin_headers, monkeypatch, limit=1)
    await reserve(identity)
    response = await client.post(f'/account/api/keys/{identity.key.id}/rotate', headers=CSRF)
    assert response.status_code == 200
    async with db.transaction() as s:
        replacement = await s.get(db.APIKey, response.json()['id'])
        _, sibling = new_key(identity.user.id, None, 'sibling', kind='agent')
        s.add(sibling)
        await s.flush()
    for key in (replacement, sibling):
        with pytest.raises(HTTPException) as denied:
            await policy.reserve(Identity(identity.user, key, generation=0 if key.kind == 'agent' else None), 1, 1, 'chat')
        assert 'allowance' in denied.value.detail


async def test_browser_only_csrf_and_scope(client, admin_headers, monkeypatch):
    identity, raw = await fixture(client, admin_headers, monkeypatch)
    await reserve(identity)
    assert (await reset(client, headers={})).status_code == 403
    assert (await reset(client, headers={'Authorization': 'Bearer ' + raw})).status_code == 403
    assert (await client.get('/account/api/usage-allowance', headers={'Authorization': 'Bearer ' + raw})).status_code == 403
    assert (await reset(client, headers={**CSRF, 'Origin': 'https://attacker.test'})).status_code == 403
    assert (await client.get('/admin/api/users/missing/usage-allowance', headers=admin_headers)).status_code == 404
    assert (await client.get(f'/admin/api/users/{identity.user.id}/usage-allowance', headers=CSRF)).status_code == 403
    async with db.transaction() as s:
        (await s.get(db.User, identity.user.id)).enabled = False
    assert (await reset(client)).status_code == 401


async def test_retries_and_concurrent_resets_consume_once(client, admin_headers, monkeypatch):
    identity, _ = await fixture(client, admin_headers, monkeypatch, limit=1)
    await reserve(identity)
    op = str(uuid4())
    results = await asyncio.gather(*(reset(client, op=op) for _ in range(8)))
    assert all(r.status_code == 200 for r in results)
    assert sum(not r.json()['replayed'] for r in results) == 1
    assert all(r.json()['resets_used'] == 1 for r in results)
    await reserve(identity)
    assert (await reset(client, op=op)).json()['requests_used'] == 1  # Retry does not clear newer use.
    results = await asyncio.gather(*(reset(client) for _ in range(8)))
    assert sum(r.status_code == 200 for r in results) == 1
    assert (await client.get('/account/api/usage-allowance')).json()['resets_used'] == 2


async def test_config_changes_preserve_consumed_grants(client, admin_headers, monkeypatch):
    identity, _ = await fixture(client, admin_headers, monkeypatch, limit=1)
    await reserve(identity)
    await reset(client)
    await reserve(identity)
    path = f'/admin/api/users/{identity.user.id}/usage-allowance'
    for limit, resets in ((None, 0), (2, 4), (2, 0), (2, 4)):
        response = await client.put(path, headers=admin_headers,
                                   json={'daily_request_limit': limit, 'monthly_reset_limit': resets})
        assert response.status_code == 200
        assert response.json()['requests_used'] == 1
        assert response.json()['resets_used'] == 1


async def test_utc_rollover_stale_retries_and_clock_skew(client, admin_headers, monkeypatch):
    identity, _ = await fixture(client, admin_headers, monkeypatch, limit=1)
    await reserve(identity)
    op = str(uuid4())
    await reset(client, op=op)
    await reserve(identity)
    monkeypatch.setattr(db, 'now', lambda: DAY + timedelta(days=2))
    assert (await client.get('/account/api/usage-allowance')).json()['requests_remaining'] == 0
    next_day = DAY + timedelta(days=1)
    monkeypatch.setattr(db, 'database_now', AsyncMock(return_value=next_day))
    view = (await client.get('/account/api/usage-allowance')).json()
    assert view['requests_remaining'] == 1 and view['resets_used'] == 1
    await reserve(identity)
    assert (await reset(client)).status_code == 409  # A fresh operation with stale day is refused.
    assert (await reset(client, op=op)).json()['replayed']  # Old retry cannot clear new day.
    assert (await client.get('/account/api/usage-allowance')).json()['requests_used'] == 1
    assert (await reset(client, day=next_day)).status_code == 200
    february = DAY + timedelta(days=2)
    monkeypatch.setattr(db, 'database_now', AsyncMock(return_value=february))
    view = (await client.get('/account/api/usage-allowance')).json()
    assert view['resets_used'] == 0 and view['resets_remaining'] == 4
    await reserve(identity)
    assert (await reset(client, op=op)).status_code == 409  # Previous-month retry never spends a new credit.
    assert (await reset(client, day=february)).json()['resets_used'] == 1


async def test_concurrent_admission_atomic_and_exhaustion_rolls_back(client, admin_headers, monkeypatch):
    identity, _ = await fixture(client, admin_headers, monkeypatch, limit=2)
    async def attempt():
        try:
            await reserve(identity)
            return True
        except HTTPException:
            return False
    assert sum(await asyncio.gather(*(attempt() for _ in range(8)))) == 2
    async with db.session_factory() as s:
        assert await s.scalar(select(func.count()).select_from(db.Reservation)) == 2
        assert (await s.scalar(select(db.RateBucket))).requests == 2
        assert (await s.get(db.UserAllowance, identity.user.id)).requests_used == 2


async def test_partial_allowance_cannot_burn_reset_credit(client, admin_headers, monkeypatch):
    identity, _ = await fixture(client, admin_headers, monkeypatch, limit=2)
    assert (await reset(client)).status_code == 409
    await reserve(identity)
    assert (await reset(client)).status_code == 409
    view = (await client.get('/account/api/usage-allowance')).json()
    assert view['requests_used'] == 1 and view['resets_used'] == 0


async def test_request_cannot_target_other_user_or_increase_allowance(client, admin_headers, monkeypatch):
    identity, _ = await fixture(client, admin_headers, monkeypatch, limit=1)
    await reserve(identity)
    payload = {'operation_id': str(uuid4()), 'expected_day': DAY.date().isoformat()}
    for extra in ({'user_id': 'other'}, {'daily_request_limit': 999999}, {'monthly_reset_limit': 4}):
        response = await client.post('/account/api/reset-usage', headers=CSRF, json={**payload, **extra})
        assert response.status_code == 422
    view = (await client.get('/account/api/usage-allowance')).json()
    assert view['requests_remaining'] == 0 and view['resets_used'] == 0


async def test_revoked_session_snapshot_cannot_redeem(client, admin_headers, monkeypatch):
    from gateway import allowances
    from gateway.main import app
    from gateway.security import identify
    from starlette.requests import Request
    from sqlalchemy import delete
    identity, _ = await fixture(client, admin_headers, monkeypatch, limit=1)
    await reserve(identity)
    scope = {'type': 'http', 'method': 'GET', 'headers': [(b'cookie', b'gateway_session=allowance-browser')]}
    snapshot = await identify(Request(scope), allow_session=True)
    async with db.transaction() as s:
        await s.execute(delete(db.BrowserSession).where(db.BrowserSession.user_id == identity.user.id))
    app.dependency_overrides[allowances.browser] = lambda: snapshot
    try:
        assert (await reset(client)).status_code == 401
    finally:
        app.dependency_overrides.pop(allowances.browser, None)
    async with db.session_factory() as s:
        row = await s.get(db.UserAllowance, identity.user.id)
        assert row.requests_used == 1 and not row.reset_operations
