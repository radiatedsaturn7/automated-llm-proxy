"""Default-off bounded browser key issuance from an administrator-owned policy."""
from datetime import timedelta
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import Field, model_validator
from sqlalchemy import func, select, update

from . import db
from .account import browser_member
from .admin import record
from .schemas import Strict
from .security import Identity, admin, new_key, revalidate_admin, revalidate_identity

router = APIRouter()


class KeyPolicy(Strict):
    enabled: bool = False
    team_id: str | None = Field(max_length=36)
    allowed_models: list[str] = Field(default_factory=list, max_length=100)
    permissions: list[Literal["inference", "account"]] = Field(default_factory=lambda: ["inference"], min_length=1, max_length=2)
    expires_in_days: int = Field(default=30, ge=1, le=90)
    rpm: int = Field(default=60, ge=1, le=100000)
    tpm: int = Field(default=100000, ge=1, le=100000000)
    max_active_keys: int = Field(default=3, ge=1, le=10)
    max_total_keys: int = Field(default=100, ge=1, le=1000)

    @model_validator(mode="after")
    def bounded_scope(self):
        if self.enabled and not self.allowed_models:
            raise ValueError("Enabled policy requires explicit model aliases")
        if (len(set(self.allowed_models)) != len(self.allowed_models)
                or any(not alias or "*" in alias or len(alias) > 128 or any(ord(c) < 32 for c in alias)
                       for alias in self.allowed_models)):
            raise ValueError("Model aliases must be unique, nonempty bounded strings")
        if "inference" not in self.permissions or len(set(self.permissions)) != len(self.permissions):
            raise ValueError("Inference permission required; duplicate permissions forbidden")
        if self.max_active_keys > self.max_total_keys:
            raise ValueError("Active key limit cannot exceed total version limit")
        return self


class CreateKey(Strict):
    operation_id: UUID
    name: str = Field(min_length=1, max_length=120)


def policy_fields(row):
    if row is None:
        return KeyPolicy(team_id=None).model_dump()
    return {name: getattr(row, name) for name in KeyPolicy.model_fields}


async def counts(session, user_id, now):
    predicate = (db.APIKey.user_id == user_id, db.APIKey.self_service.is_(True))
    total = await session.scalar(select(func.count()).select_from(db.APIKey).where(*predicate))
    active = await session.scalar(select(func.count()).select_from(db.APIKey).where(
        *predicate, db.APIKey.revoked.is_(False), db.APIKey.expires_at > now))
    return active, total


async def status(session, user, row, now):
    values = policy_fields(row)
    active, total = await counts(session, user.id, now)
    reason = None
    if not values['enabled']:
        reason = 'Self-service key creation is disabled'
    elif not await db.active_membership(session, user, values['team_id']):
        reason = 'Policy team membership is unavailable'
    elif total >= values['max_total_keys']:
        reason = 'Lifetime credential version limit reached; contact an administrator'
    elif active >= values['max_active_keys']:
        reason = 'Active key limit reached; revoke an unused key first'
    return {**values, 'active_keys': active, 'total_keys': total,
            'can_create': reason is None, 'disabled_reason': reason}


@router.get('/account/api/key-policy')
async def own_policy(identity: Identity = Depends(browser_member)):
    async with db.transaction(inference=True) as session:
        current = await revalidate_identity(session, identity)
        return await status(session, current.user, await session.get(db.UserKeyPolicy, current.user.id),
                            await db.database_now(session))


@router.get('/admin/api/users/{user_id}/key-policy')
async def get_policy(user_id: str, identity: Identity = Depends(admin)):
    async with db.transaction(inference=True) as session:
        await revalidate_admin(session, identity)
        user = await session.get(db.User, user_id)
        if user is None:
            raise HTTPException(404, 'User not found')
        return await status(session, user, await session.get(db.UserKeyPolicy, user.id), await db.database_now(session))


@router.put('/admin/api/users/{user_id}/key-policy')
async def configure(user_id: str, body: KeyPolicy, identity: Identity = Depends(admin)):
    async with db.transaction() as session:
        await revalidate_admin(session, identity)
        user = await session.get(db.User, user_id)
        if user is None:
            raise HTTPException(404, 'User not found')
        if body.enabled and not await db.active_membership(session, user, body.team_id):
            raise HTTPException(409, 'Active policy team membership required')
        row = await session.get(db.UserKeyPolicy, user.id)
        changed = policy_fields(row) != body.model_dump()
        if row is None:
            row = db.UserKeyPolicy(user_id=user.id)
            session.add(row)
        for name, value in body.model_dump().items():
            setattr(row, name, value)
        if changed:
            # Old keys must not retain authorization after narrowing/disabling policy.
            await session.execute(update(db.APIKey).where(db.APIKey.user_id == user.id,
                                  db.APIKey.self_service.is_(True)).values(revoked=True))
        await db.audit(session, identity.user.id, 'member_key.policy', user.id,
                       enabled=body.enabled, changed=changed, existing_self_service_keys_revoked=changed)
        return {**await status(session, user, row, await db.database_now(session)),
                'changed': changed, 'existing_self_service_keys_revoked': changed}


@router.post('/account/api/keys', status_code=201)
async def create(body: CreateKey, identity: Identity = Depends(browser_member)):
    async with db.transaction() as session:
        current = await revalidate_identity(session, identity)
        operation = str(body.operation_id)
        prior = await session.scalar(select(db.APIKey).where(db.APIKey.issuance_operation_id == operation))
        if prior is not None:
            if prior.user_id != current.user.id or not prior.self_service or prior.name != body.name:
                raise HTTPException(409, 'Issuance operation conflict')
            # Raw secrets are never stored recoverably; a retry only returns its receipt.
            return {'credential': record(prior), 'key': None, 'operation_id': operation, 'replayed': True}
        policy = await session.get(db.UserKeyPolicy, current.user.id)
        now = await db.database_now(session)
        view = await status(session, current.user, policy, now)
        if not view['can_create']:
            raise HTTPException(409, view['disabled_reason'])
        raw, key = new_key(current.user.id, policy.team_id, body.name,
                           expires_in_days=policy.expires_in_days, permissions=policy.permissions,
                           allowed_models=policy.allowed_models, rpm=policy.rpm, tpm=policy.tpm)
        key.self_service, key.issuance_operation_id = True, operation
        key.expires_at, key.created_at = now + timedelta(days=policy.expires_in_days), now
        session.add(key)
        await session.flush()
        await db.audit(session, current.user.id, 'member_key.create', key.id, operation_id=operation)
        return {'credential': record(key), 'key': raw, 'operation_id': operation, 'replayed': False}


async def inherit_member_origin(session, previous, replacement):
    """All existing admin/member/team rotation paths call this through policy helper."""
    policy = await session.get(db.UserKeyPolicy, previous.user_id)
    if not policy or not policy.enabled:
        raise HTTPException(409, 'Self-service key policy is disabled')
    now = await db.database_now(session)
    _, total = await counts(session, previous.user_id, now)
    if total >= min(policy.max_total_keys, 1000):
        raise HTTPException(409, 'Lifetime credential version limit reached; contact an administrator')
    replacement.self_service = True
    replacement.created_at = now
    # Operation ID stays on the original revoked version as an immutable retry fence.
    # All versions use one stable per-user rate pool, so there is nothing to copy.
