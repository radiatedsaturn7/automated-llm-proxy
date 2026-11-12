"""Pending-key authenticated two-phase rotation. Server persists digests only."""
from datetime import timedelta
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from . import db
from .security import aware, digest

router = APIRouter(prefix="/auth/agent/rotation", tags=["agent-rotation"])


class Operation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: UUID


class Prepare(Operation):
    next_credential: str = Field(pattern=r"^lg_[A-Za-z0-9_-]{43}$", min_length=46, max_length=46)


def bearer(request):
    value = request.headers.get("authorization", "")
    if not value.startswith("Bearer lg_") or len(value) > 256:
        raise HTTPException(401, "Agent root credential required")
    return value[7:]


async def active(session, key):
    if not key or key.kind != "agent" or key.revoked or aware(key.expires_at) <= db.now():
        raise HTTPException(401, "Invalid agent credential")
    user = await session.get(db.User, key.user_id)
    if not await db.active_membership(session, user, key.team_id):
        raise HTTPException(401, "Agent identity unavailable")
    if key.team_id:
        team = await session.get(db.Team, key.team_id)
        if not team or not team.enabled:
            raise HTTPException(403, "Agent team unavailable")


async def preflight(hashed, *, operation=None):
    """Reject invalid proofs without joining the cluster-wide writer queue.

    This read is only an availability filter, never an authorization snapshot.
    Callers repeat all checks in a fresh session under the exclusive policy gate.
    Commit accepts both a pending candidate and the committed recovery secret.
    """
    async with db.session_factory() as session:
        key = await session.scalar(select(db.APIKey).where(db.APIKey.digest == hashed))
        if operation is None or key is not None:
            await active(session, key)
            if operation is not None and key.last_rotation_operation != operation:
                raise HTTPException(409, "No matching committed operation")
            return
        pending = await session.scalar(select(db.AgentRotation).where(db.AgentRotation.pending_digest == hashed))
        if not pending or pending.operation_id != operation:
            raise HTTPException(401, "Pending credential not recognized")
        key = await session.get(db.APIKey, pending.key_id)
        await active(session, key)
        if aware(pending.expires_at) <= db.now():
            raise HTTPException(410, "Pending rotation expired")
        if pending.generation != key.generation:
            raise HTTPException(409, "Agent generation changed")


@router.post("/prepare")
async def prepare(body: Prepare, request: Request):
    root = bearer(request)
    operation = str(body.operation_id)
    pending_digest = digest(body.next_credential)
    await preflight(digest(root))
    async with db.transaction() as session:
        key = await session.scalar(select(db.APIKey).where(db.APIKey.digest == digest(root)))
        await active(session, key)
        if key.digest == pending_digest or key.last_rotation_operation == operation:
            raise HTTPException(409, "Rotation operation or credential cannot be reused")
        if await session.scalar(select(db.APIKey.id).where(db.APIKey.digest == pending_digest)):
            raise HTTPException(409, "Candidate credential is already in use")
        conflicting = await session.scalar(select(db.AgentRotation.key_id).where(
            db.AgentRotation.pending_digest == pending_digest, db.AgentRotation.key_id != key.id))
        if conflicting:
            raise HTTPException(409, "Candidate credential is already pending")
        pending = await session.get(db.AgentRotation, key.id)
        if pending and pending.operation_id == operation:
            if pending.pending_digest != pending_digest or pending.generation != key.generation:
                raise HTTPException(409, "Rotation operation conflict")
            if aware(pending.expires_at) <= db.now():
                raise HTTPException(410, "Pending rotation expired")
        else:
            if pending and aware(pending.expires_at) > db.now():
                raise HTTPException(409, "Another rotation is pending")
            if pending:
                await session.delete(pending)
                await session.flush()
            pending = db.AgentRotation(key_id=key.id, operation_id=operation, pending_digest=pending_digest,
                generation=key.generation, expires_at=min(aware(key.expires_at), db.now() + timedelta(minutes=10)))
            session.add(pending)
            await session.flush()
            await db.audit(session, key.user_id, "agent.rotation.prepare", key.id, operation_id=operation)
        return {"state": "prepared", "operation_id": operation, "expires_at": aware(pending.expires_at),
                "key_id": key.id, "generation": key.generation}


@router.post("/commit")
async def commit(body: Operation, request: Request):
    candidate = bearer(request)
    hashed = digest(candidate)
    operation = str(body.operation_id)
    await preflight(hashed, operation=operation)
    async with db.transaction() as session:
        # A lost commit response is recovered using the newly current secret.
        key = await session.scalar(select(db.APIKey).where(db.APIKey.digest == hashed))
        if key:
            await active(session, key)
            if key.last_rotation_operation != operation:
                raise HTTPException(409, "No matching committed operation")
            return {"state": "committed", "operation_id": operation, "key_id": key.id, "generation": key.generation}
        pending = await session.scalar(select(db.AgentRotation).where(db.AgentRotation.pending_digest == hashed))
        if not pending or pending.operation_id != operation:
            raise HTTPException(401, "Pending credential not recognized")
        key = await session.get(db.APIKey, pending.key_id)
        await active(session, key)
        if aware(pending.expires_at) <= db.now():
            raise HTTPException(410, "Pending rotation expired")
        if pending.generation != key.generation:
            raise HTTPException(409, "Agent generation changed")
        if key.generation >= 2_147_483_647:
            raise HTTPException(409, "Agent generation exhausted; replace credential")
        key.digest, key.prefix = hashed, candidate[:11]
        key.generation += 1
        key.last_rotation_operation = operation
        await session.delete(pending)
        await db.audit(session, key.user_id, "agent.rotation.commit", key.id, operation_id=operation, generation=key.generation)
        return {"state": "committed", "operation_id": operation, "key_id": key.id, "generation": key.generation}
