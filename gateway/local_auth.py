"""Explicitly provisioned, default-off local login. No registration or email reset."""
import asyncio
import hashlib
import hmac
import secrets
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, SecretStr
from sqlalchemy import delete, select

from . import db
from .config import get_settings
from .security import Identity, admin, aware, digest

router = APIRouter()
_hash_slots = asyncio.Semaphore(2)


async def derive(function, *args):
    # Bound memory/CPU work per replica; do not queue unbounded hash jobs.
    try:
        await asyncio.wait_for(_hash_slots.acquire(), timeout=0.01)
    except TimeoutError:
        raise HTTPException(503, "Login temporarily unavailable")
    try:
        task = asyncio.create_task(asyncio.to_thread(function, *args))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise
    finally:
        _hash_slots.release()


class PasswordBody(BaseModel):
    password: SecretStr = Field(min_length=15, max_length=128)


class LoginBody(BaseModel):
    email: str = Field(min_length=1, max_length=320)
    password: SecretStr = Field(min_length=1, max_length=128)


def enabled():
    if not get_settings().local_password_enabled:
        raise HTTPException(404, "Local login disabled")


def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    value = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=32768, r=8, p=3,
                           maxmem=64 * 1024 * 1024).hex()
    return f"scrypt-v1${salt}${value}"


def verify(password, encoded):
    # A dummy derivation for unprovisioned accounts avoids a cheap timing oracle.
    candidate = encoded or "scrypt-v1$" + "00" * 16 + "$" + "00" * 64
    try:
        version, salt, _ = candidate.split("$")
        if version != "scrypt-v1":
            return False
        return hmac.compare_digest(password_hash(password, salt), candidate)
    except (ValueError, TypeError):
        return False


async def throttle(email, ip):
    # Fixed-size tables bound attacker-controlled cardinality. Hash collisions
    # conservatively share a limit. Never trust client-supplied forwarded headers.
    denied = False
    async with db.transaction() as s:
        for namespace, value, limit in (("user", email, 5), ("ip", ip, 30)):
            bucket = namespace + ":" + str(int(digest(namespace + ":" + value)[:8], 16) % 4096)
            row = await s.get(db.LoginThrottle, bucket)
            if row is None:
                row = db.LoginThrottle(bucket=bucket, started_at=db.now(), attempts=0)
                s.add(row)
            elif aware(row.started_at) + timedelta(minutes=15) <= db.now():
                row.started_at, row.attempts = db.now(), 0
            denied |= row.attempts >= limit
            row.attempts = min(row.attempts + 1, limit)
    if denied:
        raise HTTPException(429, "Login temporarily unavailable", headers={"Retry-After": "900"})


@router.get("/auth/local/options")
async def options():
    return {"enabled": get_settings().local_password_enabled}


@router.put("/admin/api/users/{user_id}/password", status_code=204)
async def provision(user_id: str, body: PasswordBody, identity: Identity = Depends(admin)):
    enabled()
    encoded = await derive(password_hash, body.password.get_secret_value())
    async with db.transaction() as s:
        user = await s.get(db.User, user_id)
        if not user:
            raise HTTPException(404, "User not found")
        if user.scim_managed or user.oidc_subject or user.scim_external_id:
            raise HTTPException(409, "Federated identities cannot use local passwords")
        row = await s.get(db.LocalPassword, user_id)
        if row:
            row.password_hash = encoded
        else:
            s.add(db.LocalPassword(user_id=user_id, password_hash=encoded))
        await s.execute(delete(db.BrowserSession).where(db.BrowserSession.user_id == user_id))
        await db.audit(s, identity.user.id, "local_password.reset", user_id)


@router.delete("/admin/api/users/{user_id}/password", status_code=204)
async def revoke(user_id: str, identity: Identity = Depends(admin)):
    # Revocation remains available even while login is disabled.
    async with db.transaction() as s:
        await s.execute(delete(db.LocalPassword).where(db.LocalPassword.user_id == user_id))
        await s.execute(delete(db.BrowserSession).where(db.BrowserSession.user_id == user_id))
        await db.audit(s, identity.user.id, "local_password.revoke", user_id)


@router.post("/auth/local/login")
async def login(body: LoginBody, request: Request):
    enabled()
    cfg = get_settings()
    if request.headers.get("content-type", "").split(";", 1)[0].lower() != "application/json":
        raise HTTPException(415, "JSON required")
    # Prevent login CSRF, including when no authenticated session exists yet.
    if request.headers.get("origin", "").rstrip("/") != cfg.public_url.rstrip("/"):
        raise HTTPException(403, "Same-origin login required")
    email = body.email.strip().lower()
    await throttle(email, request.client.host if request.client else "unknown")
    async with db.session_factory() as s:
        user = await s.scalar(select(db.User).where(db.User.email == email))
        row = await s.get(db.LocalPassword, user.id) if user else None
        encoded = row.password_hash if row else None
        user_id = user.id if user else None
    valid = await derive(verify, body.password.get_secret_value(), encoded)
    raw, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    async with db.transaction() as s:
        user = await s.get(db.User, user_id) if user_id else None
        row = await s.get(db.LocalPassword, user_id) if user_id else None
        if (not valid or not user or not row or row.password_hash != encoded or not user.enabled
                or user.scim_managed or user.oidc_subject or user.scim_external_id
                or not await db.active_membership(s, user, user.team_id)):
            raise HTTPException(401, "Invalid credentials")
        old = request.cookies.get("gateway_session")
        if old:
            await s.execute(delete(db.BrowserSession).where(db.BrowserSession.digest == digest(old)))
        s.add(db.BrowserSession(digest=digest(raw), user_id=user.id, csrf=csrf,
                               expires_at=db.now() + timedelta(seconds=cfg.local_session_seconds)))
        await db.audit(s, user.id, "local_password.login")
        target = "/admin" if user.role == "admin" else "/portal"
    response = JSONResponse({"redirect": target})
    response.set_cookie("gateway_session", raw, httponly=True, secure=cfg.public_url.startswith("https://"),
                        samesite="lax", max_age=cfg.local_session_seconds, path="/")
    return response
