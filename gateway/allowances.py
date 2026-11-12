"""Optional user request allowances; resets never change actual cost or rate limits."""
from datetime import date, timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import Field

from . import db
from .account import member
from .schemas import Strict
from .security import Identity, admin, revalidate_admin, revalidate_identity

router = APIRouter()


class AllowanceConfig(Strict):
    daily_request_limit: int | None = Field(default=None, ge=1, le=100000000)
    monthly_reset_limit: int = Field(default=0, ge=0, le=4)


class ResetRequest(Strict):
    operation_id: UUID
    expected_day: date


def status(row, now):
    day, month = now.strftime("%Y-%m-%d"), now.strftime("%Y-%m")
    limit = row.daily_request_limit if row else None
    used = row.requests_used if row and row.usage_day == day else 0
    reset_limit = row.monthly_reset_limit if row else 0
    resets_used = len(row.reset_operations) if row and row.reset_month == month else 0
    next_month = (now.replace(day=28) + timedelta(days=4)).replace(day=1)
    return {"enabled": limit is not None, "day": day, "month": month,
            "daily_request_limit": limit, "requests_used": used,
            "requests_remaining": None if limit is None else max(0, limit - used),
            "monthly_reset_limit": reset_limit, "resets_used": resets_used,
            "resets_remaining": max(0, reset_limit - resets_used),
            "allowance_renews_at": (now + timedelta(days=1)).strftime("%Y-%m-%dT00:00:00Z"),
            "resets_renew_at": next_month.strftime("%Y-%m-%dT00:00:00Z")}


async def consume(session, user_id, admitted_at):
    # Called only under policy.reserve's user lock; same transaction as reservation.
    row = await session.get(db.UserAllowance, user_id)
    if not row or row.daily_request_limit is None:
        return
    current = status(row, admitted_at)
    if current["requests_used"] >= row.daily_request_limit:
        raise HTTPException(429, "Daily request allowance exhausted; wait for renewal or use an available reset",
                            headers={"Retry-After": str(max(1, int((
                                (admitted_at + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
                                - admitted_at).total_seconds())))})
    row.usage_day, row.requests_used = current["day"], current["requests_used"] + 1


async def browser(identity: Identity = Depends(member)):
    if not identity.session:
        raise HTTPException(403, "Browser session required")
    return identity


@router.get("/account/api/usage-allowance")
async def own_allowance(identity: Identity = Depends(browser)):
    async with db.transaction(inference=True) as session:
        current = await revalidate_identity(session, identity)
        return status(await session.get(db.UserAllowance, current.user.id), await db.database_now(session))


@router.get("/admin/api/users/{user_id}/usage-allowance")
async def get_allowance(user_id: str, identity: Identity = Depends(admin)):
    async with db.transaction(inference=True) as session:
        await revalidate_admin(session, identity)
        if not await session.get(db.User, user_id):
            raise HTTPException(404, "User not found")
        return status(await session.get(db.UserAllowance, user_id), await db.database_now(session))


@router.put("/admin/api/users/{user_id}/usage-allowance")
async def configure(user_id: str, body: AllowanceConfig, identity: Identity = Depends(admin)):
    async with db.transaction() as session:
        await revalidate_admin(session, identity)
        if not await session.get(db.User, user_id):
            raise HTTPException(404, "User not found")
        row = await session.get(db.UserAllowance, user_id)
        if row is None:
            row = db.UserAllowance(user_id=user_id, requests_used=0, reset_operations=[])
            session.add(row)
        row.daily_request_limit, row.monthly_reset_limit = body.daily_request_limit, body.monthly_reset_limit
        # Never reset counters or credits when changing/disabling/re-enabling policy.
        await db.audit(session, identity.user.id, "allowance.configure", user_id,
                       daily_request_limit=body.daily_request_limit, monthly_reset_limit=body.monthly_reset_limit)
        return status(row, await db.database_now(session))


@router.post("/account/api/reset-usage")
async def reset_usage(body: ResetRequest, identity: Identity = Depends(browser)):
    async with db.transaction() as session:
        current = await revalidate_identity(session, identity)
        row = await session.get(db.UserAllowance, current.user.id)
        now = await db.database_now(session)
        view = status(row, now)
        operation_id = str(body.operation_id)
        operations = row.reset_operations if row and row.reset_month == view["month"] else []
        previous = next((op for op in operations if op["id"] == operation_id), None)
        if previous:
            if previous["day"] != body.expected_day.isoformat():
                raise HTTPException(409, "Reset operation was already used for another day")
            return {**view, "reset_applied": True, "replayed": True, "operation_id": operation_id}
        if body.expected_day.isoformat() != view["day"]:
            raise HTTPException(409, "Allowance day changed; refresh before resetting")
        if not view["enabled"] or not view["resets_remaining"]:
            raise HTTPException(409, "No usage reset available")
        if view["requests_remaining"] != 0:
            raise HTTPException(409, "Request allowance is not exhausted")
        row.usage_day, row.requests_used = view["day"], 0
        row.reset_month = view["month"]
        # Assign a new JSON value so SQLAlchemy persists it. At most four entries.
        row.reset_operations = operations + [{"id": operation_id, "day": view["day"]}]
        await db.audit(session, current.user.id, "account.usage.reset", current.user.id,
                       operation_id=operation_id, day=view["day"], requests_replenished=view["requests_used"])
        return {**status(row, now), "reset_applied": True, "replayed": False, "operation_id": operation_id}
