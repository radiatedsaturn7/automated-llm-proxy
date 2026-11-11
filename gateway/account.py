"""Self-service visibility and bounded credential lifecycle without scope expansion."""

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select
from . import db
from .admin import record
from .security import Identity, aware, identify, new_key, require_permission, revalidate_identity

router = APIRouter(prefix="/account/api")


async def member(request: Request):
    identity = await identify(request, allow_session=True)
    require_permission(identity, "account")
    if identity.key and identity.key.kind == "agent":
        raise HTTPException(403, "Agent access tokens cannot manage credentials")
    return identity


@router.get("/profile")
async def profile(identity: Identity = Depends(member)):
    async with db.session_factory() as session:
        period = db.now().strftime("%Y-%m")
        spent = await session.scalar(
            select(func.coalesce(func.sum(db.Usage.cost_micros), 0)).where(
                db.Usage.user_id == identity.user.id, db.Usage.period == period
            )
        )
        pending = await session.scalar(
            select(func.coalesce(func.sum(db.Reservation.amount_micros), 0)).where(
                db.Reservation.user_id == identity.user.id,
                db.Reservation.period == period,
                db.Reservation.status == "pending",
            )
        )
    return {
        "id": identity.user.id,
        "email": identity.user.email,
        "role": identity.user.role,
        "team_id": identity.user.team_id,
        "period": period,
        "budget_micros": identity.user.budget_micros,
        "spent_micros": spent,
        "pending_micros": pending,
        "csrf_token": identity.session.csrf if identity.session else None,
        "can_manage_all_own_keys": identity.session is not None,
    }


@router.get("/keys")
async def keys(identity: Identity = Depends(member)):
    query = select(db.APIKey).where(db.APIKey.user_id == identity.user.id)
    # An API credential may not enumerate or operate on more privileged sibling keys.
    if identity.key:
        query = query.where(db.APIKey.id == identity.key.id)
    async with db.session_factory() as session:
        found = (await session.scalars(query.order_by(db.APIKey.created_at.desc()).limit(100))).all()
        return [record(k) for k in found]


async def owned(session, identity, key_id):
    current = await revalidate_identity(session, identity)
    require_permission(current, "account")
    if current.key and current.key.kind != "api":
        raise HTTPException(403, "Agent access tokens cannot manage credentials")
    user = current.user
    key = await session.get(db.APIKey, key_id)
    if not key or key.user_id != user.id or (current.key and key.id != current.key.id):
        raise HTTPException(404, "Key not found")
    return user, key


@router.post("/keys/{key_id}/revoke")
async def revoke(key_id: str, identity: Identity = Depends(member)):
    async with db.transaction() as session:
        _, key = await owned(session, identity, key_id)
        key.revoked = True
        await db.audit(session, identity.user.id, "account.key.revoke", key.id)
    return {"revoked": True}


@router.post("/keys/{key_id}/rotate")
async def rotate(key_id: str, identity: Identity = Depends(member)):
    async with db.transaction() as session:
        user, key = await owned(session, identity, key_id)
        if key.revoked or aware(key.expires_at) <= db.now() or not await db.active_membership(session, user, key.team_id):
            raise HTTPException(
                409, "Key is expired, revoked or belongs to a former team; ask an administrator"
            )
        raw, replacement = new_key(
            key.user_id,
            key.team_id,
            key.name,
            kind=key.kind,
            permissions=key.permissions,
            allowed_models=key.allowed_models,
            rpm=key.rpm,
            tpm=key.tpm,
        )
        replacement.expires_at = key.expires_at
        key.revoked = True
        session.add(replacement)
        await session.flush()
        await db.audit(session, identity.user.id, "account.key.rotate", key.id, replacement_id=replacement.id)
        return {**record(replacement), "key": raw}
