"""Opt-in SSO device enrollment. Credentials never leave device except TLS auth."""
from datetime import timedelta
import hmac
import json
import re
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import Field, field_validator
from sqlalchemy import delete, func, or_, select

from . import db, schemas
from .config import get_settings
from .security import Identity, aware, digest, identify, revalidate_identity

router = APIRouter(prefix="/auth/agent/enrollment", tags=["agent-enrollment"])


class Operation(schemas.Strict):
    request_id: UUID

    @field_validator("request_id")
    @classmethod
    def random_id(cls, value):
        if value.version != 4:
            raise ValueError("UUID4 request required")
        return value


class Start(Operation):
    candidate_credential: str = Field(pattern=r"^lg_[A-Za-z0-9_-]{43}$")
    device_name: str = Field(min_length=1, max_length=120)


class Approval(Operation):
    team_id: str = Field(min_length=1, max_length=36)


def configuration():
    settings = get_settings()
    if not settings.enrollment_enabled:
        raise HTTPException(404, "Enrollment disabled")
    return settings


def policy(settings):
    return {"models": json.loads(settings.enrollment_allowed_models), "expires_in_days": settings.enrollment_key_days,
            "rpm": settings.enrollment_rpm, "tpm": settings.enrollment_tpm}


async def browser(request: Request):
    configuration()
    identity = await identify(request, allow_session=True)
    if not identity.session or not identity.user.oidc_subject:
        raise HTTPException(403, "SSO browser session required")
    return identity


async def current_user(s, identity):
    current = await revalidate_identity(s, identity)
    if not current.session or not current.user.oidc_subject:
        raise HTTPException(401, "SSO session unavailable")
    return current.user


def bearer(request):
    value = request.headers.get("authorization", "")
    if not re.fullmatch(r"Bearer lg_[A-Za-z0-9_-]{43}", value):
        raise HTTPException(401, "Invalid enrollment proof")
    return value[7:]


def public(row):
    return {"request_id": row.approval_code, "device_name": row.device_name, "state": row.state,
            "expires_at": row.expires_at, "policy": row.policy}


async def lookup(s, request_id):
    row = await s.scalar(select(db.AgentEnrollment).where(db.AgentEnrollment.approval_code == str(request_id)))
    if not row or aware(row.expires_at) <= db.now():
        raise HTTPException(410, "Enrollment unavailable or expired")
    return row


@router.post("/start")
async def start(body: Start):
    settings = configuration()
    candidate = digest(body.candidate_credential)
    async with db.transaction() as s:
        row = await s.get(db.AgentEnrollment, str(body.request_id))
        if row:
            if not hmac.compare_digest(row.candidate_digest, candidate):
                raise HTTPException(409, "Enrollment request conflict")
            if aware(row.expires_at) <= db.now():
                raise HTTPException(410, "Enrollment expired")
            # Idempotent recovery must not change the device label or granted policy.
            return {**public(row), "approval_code": row.approval_code, "poll_interval": 5}
        previous = await s.scalar(select(db.APIKey).where(db.APIKey.enrollment_request_id == str(body.request_id)))
        if previous:
            if hmac.compare_digest(previous.digest, candidate):
                raise HTTPException(410, "Approval record expired; recover existing credential")
            raise HTTPException(409, "Enrollment request already assigned")
        if (await s.scalar(select(db.APIKey.id).where(db.APIKey.digest == candidate))
                or await s.scalar(select(db.AgentEnrollment.request_id).where(db.AgentEnrollment.candidate_digest == candidate))
                or await s.scalar(select(db.AgentRotation.key_id).where(db.AgentRotation.pending_digest == candidate))):
            raise HTTPException(409, "Credential already registered")
        await s.execute(delete(db.AgentEnrollment).where(db.AgentEnrollment.expires_at <= db.now()))
        count = await s.scalar(select(func.count()).select_from(db.AgentEnrollment))
        if count >= settings.enrollment_max_pending:
            raise HTTPException(429, "Enrollment capacity reached", headers={"Retry-After": "60"})
        row = db.AgentEnrollment(request_id=str(body.request_id), approval_code=str(uuid4()), candidate_digest=candidate,
                                 device_name=body.device_name, policy=policy(settings), state="pending",
                                 expires_at=db.now() + timedelta(minutes=10))
        s.add(row)
        await s.flush()
        return {**public(row), "approval_code": row.approval_code, "poll_interval": 5}


@router.post("/inspect")
async def inspect(body: Operation, identity: Identity = Depends(browser)):
    settings = configuration()
    async with db.transaction(inference=True) as s:
        user = await current_user(s, identity)
        row = await lookup(s, body.request_id)
        if row.user_id and row.user_id != user.id:
            raise HTTPException(409, "Enrollment already assigned")
        if row.state == "pending" and row.policy != policy(settings):
            raise HTTPException(409, "Enrollment policy changed; start a new request")
        member_teams = select(db.TeamMembership.team_id).where(db.TeamMembership.user_id == user.id)
        teams = (await s.scalars(select(db.Team).where(db.Team.enabled.is_(True), or_(
            db.Team.id == user.team_id, db.Team.id.in_(member_teams))).order_by(db.Team.id).limit(1000))).all()
        return {**public(row), "teams": [{"id": t.id, "name": t.name} for t in teams]}


@router.post("/approve")
async def approve(body: Approval, identity: Identity = Depends(browser)):
    settings = configuration()
    async with db.transaction() as s:
        user = await current_user(s, identity)
        row = await lookup(s, body.request_id)
        if not await db.active_membership(s, user, body.team_id):
            raise HTTPException(403, "Active selected membership required")
        if row.state == "approved":
            if row.user_id != user.id or row.team_id != body.team_id:
                raise HTTPException(409, "Enrollment already assigned")
            key = await s.get(db.APIKey, row.key_id)
            if not key or key.revoked or aware(key.expires_at) <= db.now():
                raise HTTPException(409, "Approved credential unavailable")
            return {"state": "approved", "key_id": key.id, "team_id": row.team_id}
        if row.state != "pending" or row.policy != policy(settings):
            raise HTTPException(409, "Enrollment cannot be approved")
        if (await s.scalar(select(db.APIKey.id).where(db.APIKey.digest == row.candidate_digest))
                or await s.scalar(select(db.AgentRotation.key_id).where(db.AgentRotation.pending_digest == row.candidate_digest))):
            raise HTTPException(409, "Credential already registered")
        key = db.APIKey(enrollment_request_id=row.request_id, name=row.device_name, prefix="lg_enrolled", digest=row.candidate_digest,
                        user_id=user.id, team_id=body.team_id, kind="agent", permissions=["inference"],
                        allowed_models=row.policy["models"], rpm=row.policy["rpm"], tpm=row.policy["tpm"],
                        expires_at=db.now() + timedelta(days=row.policy["expires_in_days"]))
        s.add(key)
        await s.flush()
        row.state, row.user_id, row.team_id, row.key_id = "approved", user.id, body.team_id, key.id
        await db.audit(s, user.id, "agent.enrollment.approve", key.id, team_id=body.team_id)
        return {"state": "approved", "key_id": key.id, "team_id": body.team_id}


@router.post("/deny")
async def deny(body: Operation, identity: Identity = Depends(browser)):
    async with db.transaction() as s:
        user = await current_user(s, identity)
        row = await lookup(s, body.request_id)
        if row.state == "denied" and row.user_id == user.id:
            return {"state": "denied"}
        if row.state != "pending":
            raise HTTPException(409, "Enrollment already resolved")
        row.state, row.user_id = "denied", user.id
        await db.audit(s, user.id, "agent.enrollment.deny", row.request_id)
        return {"state": "denied"}


@router.post("/poll")
async def poll(body: Operation, request: Request):
    configuration()
    candidate = digest(bearer(request))
    # Reject random proofs without acquiring the global administration writer gate.
    async with db.session_factory() as s:
        proof = await s.scalar(select(db.AgentEnrollment.candidate_digest).where(
            db.AgentEnrollment.request_id == str(body.request_id)))
        if not proof or not hmac.compare_digest(proof, candidate):
            raise HTTPException(401, "Invalid enrollment proof")
    async with db.transaction() as s:
        row = await s.get(db.AgentEnrollment, str(body.request_id))
        if not row or not hmac.compare_digest(row.candidate_digest, candidate):
            raise HTTPException(401, "Invalid enrollment proof")
        if aware(row.expires_at) <= db.now():
            raise HTTPException(410, "Enrollment expired")
        if row.last_poll and (db.now() - aware(row.last_poll)).total_seconds() < 5:
            raise HTTPException(429, "Poll interval exceeded", headers={"Retry-After": "5"})
        row.last_poll = db.now()
        if row.state == "approved":
            key = await s.get(db.APIKey, row.key_id)
            user = await s.get(db.User, row.user_id)
            if (not key or key.revoked or aware(key.expires_at) <= db.now()
                    or not hmac.compare_digest(key.digest, candidate)
                    or not await db.active_membership(s, user, row.team_id)):
                raise HTTPException(401, "Approved enrollment unavailable")
            return {"state": "approved", "request_id": row.request_id, "key_id": key.id,
                    "team_id": row.team_id, "user_id": row.user_id, "expires_at": key.expires_at}
        return {"state": row.state, "request_id": row.request_id, "poll_interval": 5}


@router.post("/recover")
async def recover(body: Operation, request: Request):
    configuration()
    candidate = digest(bearer(request))
    async with db.transaction(inference=True) as s:
        key = await s.scalar(select(db.APIKey).where(db.APIKey.enrollment_request_id == str(body.request_id)))
        if (not key or not hmac.compare_digest(key.digest, candidate) or key.kind != "agent"
                or key.permissions != ["inference"] or key.revoked or aware(key.expires_at) <= db.now()
                or not await db.active_membership(s, await s.get(db.User, key.user_id), key.team_id)):
            raise HTTPException(401, "Approved enrollment unavailable")
        return {"state": "approved", "request_id": str(body.request_id), "key_id": key.id,
                "team_id": key.team_id, "user_id": key.user_id, "expires_at": key.expires_at}
