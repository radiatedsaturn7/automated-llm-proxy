"""Policy locking regressions. PostgreSQL cases run with TEST_DATABASE_URL.

Use a disposable database: the shared client fixture resets schema between tests.
These are correctness/progress checks, not throughput or latency benchmarks.
"""
import asyncio

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from gateway import db, policy
from gateway.security import Identity, new_key


async def identity():
    async with db.transaction() as session:
        team = db.Team(name=db.uid(), budget_micros=1000)
        session.add(team)
        await session.flush()
        user = db.User(email=f"{db.uid()}@test.example", team_id=team.id, budget_micros=1000)
        session.add(user)
        await session.flush()
        _, key = new_key(user.id, team.id, "scaling", rpm=1000)
        session.add(key)
        await session.flush()
    return Identity(user, key)


def require_postgres():
    if db.engine.dialect.name != "postgresql":
        pytest.skip("Requires TEST_DATABASE_URL PostgreSQL for real row-lock concurrency")


async def test_duplicate_finalize_is_idempotent(client):
    owner = await identity()
    reservation = await policy.reserve(owner, 100, 10, "model")
    route = {"input_cost_per_million": 1, "output_cost_per_million": 1}
    await asyncio.gather(*(policy.finalize(reservation, "model", "provider", route,
                                          {"prompt_tokens": 2, "completion_tokens": 3}) for _ in range(10)))
    async with db.session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(db.Usage)) == 1
        assert await session.scalar(select(func.sum(db.Usage.cost_micros))) == 5


async def test_unrelated_team_admission_progresses_while_other_team_locked(client):
    require_postgres()
    first, second = await identity(), await identity()
    async with db.transaction(inference=True) as session:
        await db.lock_policy_subjects(session, first.key.team_id, first.user.id, first.key.id)
        # A global exclusive admission lock would deadlock here until timeout.
        assert await asyncio.wait_for(policy.reserve(second, 100, 1, "model"), 5)


async def test_same_team_admission_waits_and_sees_committed_budget(client):
    require_postgres()
    owner = await identity()
    task = None
    try:
        async with db.transaction(inference=True) as session:
            await db.lock_policy_subjects(session, owner.key.team_id, owner.user.id, owner.key.id)
            session.add(db.Reservation(user_id=owner.user.id, team_id=owner.key.team_id,
                                      key_id=owner.key.id, period=db.now().strftime("%Y-%m"), amount_micros=1000))
            await session.flush()
            task = asyncio.create_task(policy.reserve(owner, 100, 1, "model"))
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(task), 0.15)
        with pytest.raises(HTTPException) as denied:
            await asyncio.wait_for(task, 5)
        assert denied.value.status_code == 429
    finally:
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_admin_revoke_waits_for_admission_gate_and_then_takes_effect(client):
    require_postgres()
    owner = await identity()

    async def revoke():
        async with db.transaction() as session:
            key = await session.get(db.APIKey, owner.key.id)
            key.revoked = True

    task = None
    try:
        async with db.transaction(inference=True) as session:
            await db.lock_policy_subjects(session, owner.key.team_id, owner.user.id, owner.key.id)
            task = asyncio.create_task(revoke())
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(task), 0.15)
        await asyncio.wait_for(task, 5)
        with pytest.raises(HTTPException) as denied:
            await policy.reserve(owner, 100, 1, "model")
        assert denied.value.status_code == 401
    finally:
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_finalize_and_reserve_use_same_lock_order(client):
    owner = await identity()
    reservations = [await policy.reserve(owner, 100, 1, "model") for _ in range(5)]
    route = {"input_cost_per_million": 1, "output_cost_per_million": 1}
    await asyncio.wait_for(asyncio.gather(
        *(policy.finalize(r, "model", "provider", route, release=True) for r in reservations),
        *(policy.reserve(owner, 100, 1, "model") for _ in range(5))), 15)
    async with db.session_factory() as session:
        assert await session.scalar(select(func.sum(db.Reservation.amount_micros)).where(
            db.Reservation.status == "pending")) == 500
