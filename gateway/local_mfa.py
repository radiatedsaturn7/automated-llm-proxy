"""Bounded local TOTP: no recovery bypass, no bearer credential in the browser URL."""

import base64
import hashlib
import hmac
import secrets
import struct
from datetime import timedelta
from urllib.parse import quote

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, Field, SecretStr
from sqlalchemy import delete, select

from . import db
from .config import get_settings
from .local_auth import router, enabled, derive, verify, throttle
from .security import Identity, admin, aware, digest, encrypt, decrypt, revalidate_admin


class EnrollmentBody(BaseModel):
    email: str = Field(min_length=1, max_length=320)
    password: SecretStr = Field(min_length=1, max_length=128)
    setup_grant: SecretStr = Field(min_length=20, max_length=128)


class CodeBody(BaseModel):
    challenge: SecretStr = Field(min_length=20, max_length=128)
    code: str = Field(pattern=r"^[0-9]{6}$")


def origin(request):
    enabled()
    if request.headers.get("content-type", "").split(";")[0].lower() != "application/json":
        raise HTTPException(415, "JSON required")
    if request.headers.get("origin", "").rstrip("/") != get_settings().public_url.rstrip("/"):
        raise HTTPException(403, "Same-origin login required")


def totp(seed, counter):
    key = base64.b32decode(seed)
    value = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = value[-1] & 15
    return f"{(int.from_bytes(value[offset : offset + 4], 'big') & 0x7FFFFFFF) % 1000000:06d}"


def eligible(user):
    return user and user.enabled and not (user.scim_managed or user.oidc_subject or user.scim_external_id)


def challenge(row, encoded, pending=None):
    raw = secrets.token_urlsafe(32)
    row.challenge_digest, row.challenge_expires = digest(raw), db.now() + timedelta(minutes=5)
    row.pending_seed, row.password_binding, row.attempts = pending, digest(encoded), 0
    return raw


@router.post("/admin/api/users/{user_id}/mfa/setup-grant")
async def setup_grant(user_id: str, identity: Identity = Depends(admin)):
    enabled()
    raw = secrets.token_urlsafe(32)
    async with db.transaction() as s:
        await revalidate_admin(s, identity)
        user = await s.get(db.User, user_id)
        if not eligible(user) or not await s.get(db.LocalPassword, user_id):
            raise HTTPException(409, "Provision an active local identity first")
        row = await s.get(db.LocalMFA, user_id)
        if row and row.seed:
            raise HTTPException(409, "MFA already enrolled")
        if not row:
            row = db.LocalMFA(user_id=user_id, last_counter=-1, attempts=0)
            s.add(row)
        row.grant_digest, row.grant_expires = digest(raw), db.now() + timedelta(minutes=5)
        row.challenge_digest, row.pending_seed = None, None
        await db.audit(s, identity.user.id, "local_mfa.setup_grant", user_id)
    return {"setup_grant": raw, "expires_in": 300}


@router.delete("/admin/api/users/{user_id}/mfa", status_code=204)
async def reset(user_id: str, identity: Identity = Depends(admin)):
    async with db.transaction() as s:
        await revalidate_admin(s, identity)
        for model in (db.LocalMFA, db.LocalPassword, db.BrowserSession):
            await s.execute(delete(model).where(model.user_id == user_id))
        await db.audit(s, identity.user.id, "local_mfa.reset", user_id)


@router.post("/auth/local/mfa/enroll/start")
async def enroll(body: EnrollmentBody, request: Request):
    origin(request)
    email = body.email.strip().lower()
    await throttle(email, request.client.host if request.client else "unknown")
    async with db.session_factory() as s:
        user = await s.scalar(select(db.User).where(db.User.email == email))
        password = await s.get(db.LocalPassword, user.id) if user else None
        encoded, uid = password.password_hash if password else None, user.id if user else None
    valid = await derive(verify, body.password.get_secret_value(), encoded)
    seed = base64.b32encode(secrets.token_bytes(20)).decode()
    async with db.transaction() as s:
        user = await s.get(db.User, uid) if uid else None
        password = await s.get(db.LocalPassword, uid) if uid else None
        row = await s.get(db.LocalMFA, uid) if uid else None
        if (
            not valid
            or not eligible(user)
            or not password
            or password.password_hash != encoded
            or not row
            or row.seed
            or not row.grant_digest
            or not row.grant_expires
            or aware(row.grant_expires) <= db.now()
            or not hmac.compare_digest(row.grant_digest, digest(body.setup_grant.get_secret_value()))
            or not await db.active_membership(s, user, user.team_id)
        ):
            raise HTTPException(401, "Invalid enrollment credentials")
        row.grant_digest = None
        raw = challenge(row, encoded, encrypt(seed))
        await db.audit(s, uid, "local_mfa.enrollment_started")
    return {
        "challenge": raw,
        "secret": seed,
        "expires_in": 300,
        "otpauth_uri": f"otpauth://totp/{quote('Thunderbolt:' + email, safe='')}?secret={seed}&issuer=Thunderbolt&algorithm=SHA1&digits=6&period=30",
    }


@router.post("/auth/local/mfa/verify")
async def finish(body: CodeBody, request: Request):
    origin(request)
    await throttle(
        "mfa:" + digest(body.challenge.get_secret_value()),
        request.client.host if request.client else "unknown",
    )
    failure = False
    response = None
    async with db.transaction() as s:
        row = await s.scalar(
            select(db.LocalMFA).where(
                db.LocalMFA.challenge_digest == digest(body.challenge.get_secret_value())
            )
        )
        user = await s.get(db.User, row.user_id) if row else None
        password = await s.get(db.LocalPassword, row.user_id) if row else None
        if (
            not row
            or not eligible(user)
            or not password
            or row.password_binding != digest(password.password_hash)
            or not row.challenge_expires
            or aware(row.challenge_expires) <= db.now()
            or row.attempts >= 5
            or not await db.active_membership(s, user, user.team_id)
        ):
            raise HTTPException(401, "Invalid or expired MFA challenge")
        row.attempts += 1
        seed = decrypt(row.pending_seed or row.seed)
        current = int(db.now().timestamp()) // 30
        matches = [
            step
            for step in (current - 1, current, current + 1)
            if step > row.last_counter and hmac.compare_digest(totp(seed, step), body.code)
        ]
        if not matches:
            failure = True
        else:
            row.last_counter = max(matches)
            if row.pending_seed:
                row.seed, row.pending_seed = row.pending_seed, None
                await s.execute(delete(db.BrowserSession).where(db.BrowserSession.user_id == user.id))
                await db.audit(s, user.id, "local_mfa.enrolled")
            row.challenge_digest = None
            from .local_auth import issue_session

            response = await issue_session(s, user, request, mfa=True)
    if failure:
        raise HTTPException(401, "Invalid or expired MFA challenge")
    return response
