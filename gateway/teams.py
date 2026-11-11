"""Explicit team-scoped delegation. All decisions rechecked under policy gate."""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select, update

from . import db, schemas
from .admin import record
from .security import Identity, aware, identify, new_key

router = APIRouter(prefix="/teams/api")


async def caller(request: Request):
    return await identify(request, allow_session=True)


async def authorize_actor(s, identity):
    user = await s.get(db.User, identity.user.id)
    if not user or not user.enabled:
        raise HTTPException(401, "User unavailable")
    permissions = None
    if identity.key:
        key = await s.get(db.APIKey, identity.key.id)
        if (not key or key.revoked or aware(key.expires_at) <= db.now() or key.kind != "api"
                or not await db.active_membership(s, user, key.team_id)):
            raise HTTPException(401, "Credential changed")
        permissions = key.permissions
        identity.key = key
    else:
        browser = await s.get(db.BrowserSession, identity.session.digest)
        if not browser or aware(browser.expires_at) <= db.now():
            raise HTTPException(401, "Session changed")
    identity.user = user
    global_admin = user.role == "admin" and (permissions is None or "admin" in permissions)
    return global_admin


async def authorize(s, identity, team_id):
    global_admin = await authorize_actor(s, identity)
    user = identity.user
    permissions = identity.key.permissions if identity.key else None
    team = await s.get(db.Team, team_id)
    if not team or not team.enabled:
        raise HTTPException(404, "Active team not found")
    if global_admin:
        return True
    if identity.key and (identity.key.team_id != team_id or "team_admin" not in permissions):
        raise HTTPException(403, "Team administration permission required")
    membership = await s.get(db.TeamMembership, (user.id, team_id))
    if not membership or membership.role != "admin":
        raise HTTPException(403, "Explicit team administrator membership required")
    return False


def delegation_bounds(identity, models, rpm, tpm, expiry):
    if identity.key:
        key = identity.key
        if ("admin" not in key.permissions or identity.user.role != "admin") and (
                aware(expiry) > aware(key.expires_at) or rpm > key.rpm or tpm > key.tpm
                or (key.allowed_models and (not models or not set(models) <= set(key.allowed_models)))):
            raise HTTPException(403, "Delegation exceeds caller credential limits")


class MembershipBody(schemas.Strict):
    role: Literal["member", "admin"] = "member"


@router.get("/{team_id}/members")
async def members(team_id: str, identity: Identity = Depends(caller)):
    async with db.transaction(inference=True) as s:
        await authorize(s, identity, team_id)
        explicit = (await s.scalars(select(db.TeamMembership).where(db.TeamMembership.team_id == team_id).limit(1000))).all()
        roles = {m.user_id: m.role for m in explicit}
        primary = (await s.scalars(select(db.User).where(db.User.team_id == team_id).limit(1000))).all()
        for user in primary:
            roles.setdefault(user.id, "member")
        primary_ids = {u.id for u in primary}
        return [{"user_id": uid, "role": role, "primary": uid in primary_ids} for uid, role in roles.items()]


@router.put("/{team_id}/members/{user_id}")
async def put_member(team_id: str, user_id: str, body: MembershipBody, identity: Identity = Depends(caller)):
    async with db.transaction() as s:
        global_admin = await authorize(s, identity, team_id)
        user = await s.get(db.User, user_id)
        if not user or not user.enabled:
            raise HTTPException(404, "Active user not found")
        current = await s.get(db.TeamMembership, (user_id, team_id))
        if not global_admin and (body.role != "member" or (current and current.role == "admin")):
            raise HTTPException(403, "Only global administrators can change delegated administrators")
        if current:
            current.role = body.role
        else:
            s.add(db.TeamMembership(user_id=user_id, team_id=team_id, role=body.role))
        await db.audit(s, identity.user.id, "team.membership.update", team_id, user_id=user_id, role=body.role)
        return {"user_id": user_id, "team_id": team_id, "role": body.role}


@router.delete("/{team_id}/members/{user_id}")
async def remove_member(team_id: str, user_id: str, identity: Identity = Depends(caller)):
    async with db.transaction() as s:
        global_admin = await authorize(s, identity, team_id)
        member = await s.get(db.TeamMembership, (user_id, team_id))
        user = await s.get(db.User, user_id)
        if not member:
            raise HTTPException(404, "Explicit membership not found")
        if not global_admin and member.role == "admin":
            raise HTTPException(403, "Only global administrators can remove delegated administrators")
        if user and user.team_id == team_id:
            raise HTTPException(409, "Primary membership must be managed through the identity source")
        await s.delete(member)
        await s.execute(update(db.APIKey).where(db.APIKey.user_id == user_id, db.APIKey.team_id == team_id).values(revoked=True))
        await db.audit(s, identity.user.id, "team.membership.remove", team_id, user_id=user_id)
        return {"removed": True}


@router.post("/{team_id}/keys", status_code=201)
async def issue(team_id: str, body: schemas.KeyCreate, identity: Identity = Depends(caller)):
    async with db.transaction() as s:
        await authorize(s, identity, team_id)
        if body.permissions != ["inference"] or body.team_id not in (None, team_id):
            raise HTTPException(403, "Delegated keys permit inference only in this team")
        user = await s.get(db.User, body.user_id)
        if not await db.active_membership(s, user, team_id):
            raise HTTPException(403, "Active membership required")
        raw, key = new_key(user.id, team_id, body.name, body.expires_in_days, body.kind,
                           permissions=["inference"], allowed_models=body.allowed_models, rpm=body.rpm, tpm=body.tpm)
        delegation_bounds(identity, key.allowed_models, key.rpm, key.tpm, key.expires_at)
        s.add(key)
        await s.flush()
        await db.audit(s, identity.user.id, "team.key.create", key.id, team_id=team_id)
        return {**record(key), "key": raw}


async def managed_key(s, identity, team_id, key_id):
    await authorize(s, identity, team_id)
    key = await s.get(db.APIKey, key_id)
    if not key or key.team_id != team_id or key.permissions != ["inference"]:
        raise HTTPException(404, "Team inference key not found")
    return key


@router.post("/{team_id}/keys/{key_id}/revoke")
async def revoke(team_id: str, key_id: str, identity: Identity = Depends(caller)):
    async with db.transaction() as s:
        key = await managed_key(s, identity, team_id, key_id)
        key.revoked = True
        await db.audit(s, identity.user.id, "team.key.revoke", key.id, team_id=team_id)
        return {"revoked": True}


@router.post("/{team_id}/keys/{key_id}/rotate")
async def rotate(team_id: str, key_id: str, identity: Identity = Depends(caller)):
    async with db.transaction() as s:
        key = await managed_key(s, identity, team_id, key_id)
        if (key.revoked or aware(key.expires_at) <= db.now()
                or not await db.active_membership(s, await s.get(db.User, key.user_id), team_id)):
            raise HTTPException(409, "Credential or membership unavailable")
        raw, replacement = new_key(key.user_id, team_id, key.name, kind=key.kind,
                                   permissions=key.permissions, allowed_models=key.allowed_models, rpm=key.rpm, tpm=key.tpm)
        delegation_bounds(identity, key.allowed_models, key.rpm, key.tpm, key.expires_at)
        replacement.expires_at = key.expires_at
        key.revoked = True
        s.add(replacement)
        await s.flush()
        await db.audit(s, identity.user.id, "team.key.rotate", key.id, replacement_id=replacement.id)
        return {**record(replacement), "key": raw}


@router.get("")
async def managed_teams(identity: Identity = Depends(caller)):
    async with db.transaction(inference=True) as s:
        global_admin = await authorize_actor(s, identity)
        query = select(db.Team).where(db.Team.enabled.is_(True))
        if not global_admin:
            if identity.key and "team_admin" not in identity.key.permissions:
                return []
            query = query.join(db.TeamMembership, db.TeamMembership.team_id == db.Team.id).where(
                db.TeamMembership.user_id == identity.user.id, db.TeamMembership.role == "admin")
            if identity.key:
                query = query.where(db.Team.id == identity.key.team_id)
        found = (await s.scalars(query.order_by(db.Team.id).limit(1000))).all()
        return [{"id": team.id, "name": team.name, "budget_micros": team.budget_micros} for team in found]


@router.get("/{team_id}/keys")
async def managed_keys(team_id: str, identity: Identity = Depends(caller)):
    async with db.transaction(inference=True) as s:
        await authorize(s, identity, team_id)
        keys = (await s.scalars(select(db.APIKey).where(db.APIKey.team_id == team_id)
                               .order_by(db.APIKey.created_at.desc(), db.APIKey.id).limit(1000))).all()
        return [record(key) for key in keys if key.permissions == ["inference"]]
