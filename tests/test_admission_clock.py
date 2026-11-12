"""Accounting periods use one shared wall-clock sample after subject locks."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from gateway import db, policy
from gateway.security import Identity, new_key

JAN_END = datetime(2030, 1, 31, 23, 59, 59, tzinfo=timezone.utc)


async def owner(*, rpm=1000, user_budget=1000, team_budget=1000):
    async with db.transaction() as s:
        team = db.Team(name=db.uid(), budget_micros=team_budget)
        s.add(team)
        await s.flush()
        user = db.User(email=f"{db.uid()}@example.test", team_id=team.id, budget_micros=user_budget)
        s.add(user)
        await s.flush()
        _, key = new_key(user.id, team.id, "clock", rpm=rpm)
        key.expires_at = JAN_END + timedelta(days=365)
        s.add(key)
        await s.flush()
    return Identity(user, key)


@pytest.mark.parametrize("limit", ["rpm", "user", "team"])
async def test_replica_clock_skew_cannot_split_limits(client, monkeypatch, limit):
    identity = await owner(rpm=1 if limit == "rpm" else 1000,
                           user_budget=100 if limit == "user" else 1000,
                           team_budget=100 if limit == "team" else 1000)
    clock = AsyncMock(return_value=JAN_END)
    monkeypatch.setattr(db, "database_now", clock)
    monkeypatch.setattr(db, "now", lambda: JAN_END)
    await policy.reserve(identity, 100, 10, "model")
    monkeypatch.setattr(db, "now", lambda: JAN_END + timedelta(seconds=2))
    with pytest.raises(HTTPException) as denied:
        await policy.reserve(identity, 100, 10, "model")
    assert denied.value.status_code == 429
    assert ("Rate" if limit == "rpm" else limit.title()) in denied.value.detail
    assert clock.await_count == 2
    async with db.session_factory() as s:
        assert await s.scalar(select(func.count()).select_from(db.Reservation)) == 1
        assert await s.scalar(select(func.count()).select_from(db.RateBucket)) == 1


async def test_shared_rollover_and_settlement_preserve_original_period(client, monkeypatch):
    identity = await owner(rpm=1, user_budget=100, team_budget=100)
    clock = AsyncMock(side_effect=[JAN_END, JAN_END + timedelta(seconds=2)])
    monkeypatch.setattr(db, "database_now", clock)
    monkeypatch.setattr(db, "now", lambda: JAN_END)  # Replica never rolls over.
    first = await policy.reserve(identity, 100, 1, "model")
    second = await policy.reserve(identity, 100, 1, "model")
    await policy.finalize(first, "model", "provider", {}, usage=None)
    assert clock.await_count == 2
    async with db.session_factory() as s:
        assert (await s.get(db.Reservation, second)).period == "2030-02"
        assert (await s.scalar(select(db.Usage))).period == "2030-01"
        buckets = list((await s.scalars(select(db.RateBucket.id))).all())
        assert sorted(buckets) == [identity.key.id + ":203001312359", identity.key.id + ":203002010000"]


async def test_clock_sample_waits_for_subject_locks_and_rejects_expired_key(client, monkeypatch):
    identity = await owner()
    async with db.transaction() as s:
        (await s.get(db.APIKey, identity.key.id)).expires_at = JAN_END + timedelta(seconds=1)
    entered, release = asyncio.Event(), asyncio.Event()
    real_lock = db.lock_policy_subjects

    async def delayed_lock(*args):
        result = await real_lock(*args)
        entered.set()
        await release.wait()
        return result

    clock = AsyncMock(return_value=JAN_END + timedelta(seconds=1))
    monkeypatch.setattr(db, "lock_policy_subjects", delayed_lock)
    monkeypatch.setattr(db, "database_now", clock)
    monkeypatch.setattr(db, "now", lambda: JAN_END)
    task = asyncio.create_task(policy.reserve(identity, 1, 1, "model"))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        clock.assert_not_awaited()
        release.set()
        with pytest.raises(HTTPException) as denied:
            await asyncio.wait_for(task, 5)
        assert denied.value.status_code == 401
        clock.assert_awaited_once()
        async with db.session_factory() as s:
            assert await s.scalar(select(func.count()).select_from(db.Reservation)) == 0
            assert await s.scalar(select(func.count()).select_from(db.RateBucket)) == 0
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("dialect,expected", [("postgresql", "clock_timestamp()"), ("sqlite", "CURRENT_TIMESTAMP")])
async def test_database_clock_uses_wall_clock_not_transaction_start(dialect, expected):
    session = SimpleNamespace(get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name=dialect)),
                              scalar=AsyncMock(return_value=JAN_END.replace(tzinfo=None)))
    assert await db.database_now(session) == JAN_END
    assert str(session.scalar.call_args.args[0]) == "SELECT " + expected + (" AS clock_timestamp_1" if dialect == "postgresql" else " AS current_timestamp_1")


async def test_real_database_clock_returns_utc(client):
    async with db.session_factory() as s:
        value = await db.database_now(s)
    assert value.utcoffset() == timedelta(0)
    assert abs((value - datetime.now(timezone.utc)).total_seconds()) < 30


async def test_expired_by_database_rejected_before_provider_dispatch(client, admin_headers, monkeypatch):
    from test_gateway import mock_upstream, setup_inference
    headers, credential, _ = await setup_inference(client, admin_headers)
    local_now = db.now()
    async with db.transaction() as s:
        key = await s.get(db.APIKey, credential["id"])
        key.expires_at = local_now + timedelta(minutes=1)
    monkeypatch.setattr(db, "database_now", AsyncMock(return_value=local_now + timedelta(minutes=2)))
    seen = []

    def upstream(request):
        seen.append(request)
        raise AssertionError("Expired credential must not dispatch")

    await mock_upstream(upstream)
    response = await client.post("/v1/chat/completions", headers=headers, json={
        "model": "chat", "messages": [{"role": "user", "content": "hello"}], "max_tokens": 10})
    assert response.status_code == 401
    assert seen == []
    async with db.session_factory() as s:
        assert await s.scalar(select(func.count()).select_from(db.Reservation)) == 0


async def test_fast_cleanup_clock_preserves_live_rate_bucket(client, monkeypatch):
    from gateway.maintenance import cleanup
    identity = await owner(rpm=1)
    monkeypatch.setattr(db, "database_now", AsyncMock(return_value=JAN_END))
    await policy.reserve(identity, 1, 1, "model")
    old_id = identity.key.id + ":" + (JAN_END - timedelta(days=2)).strftime("%Y%m%d%H%M")
    boundary_id = identity.key.id + ":" + (JAN_END - timedelta(days=1)).strftime("%Y%m%d%H%M")
    async with db.transaction() as s:
        s.add_all([db.RateBucket(id=old_id, requests=1, tokens=1),
                   db.RateBucket(id=boundary_id, requests=1, tokens=1)])
    monkeypatch.setattr(db, "now", lambda: JAN_END + timedelta(days=2))
    assert (await cleanup())["eligible_in_batch"]["rate_buckets"] == 1
    assert (await cleanup(apply=True))["eligible_in_batch"]["rate_buckets"] == 1
    async with db.session_factory() as s:
        assert await s.get(db.RateBucket, old_id) is None
        assert await s.get(db.RateBucket, boundary_id) is not None
    with pytest.raises(HTTPException) as denied:
        await policy.reserve(identity, 1, 1, "model")
    assert denied.value.status_code == 429
