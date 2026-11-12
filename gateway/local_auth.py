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
from .account import browser_member
from .config import get_settings
from .security import Identity, admin, aware, digest, revalidate_admin

router = APIRouter()
_hash_slots = asyncio.Semaphore(2)
_hash_jobs = set()


async def derive(function, *args):
    # Bound memory/CPU work per replica; do not queue unbounded hash jobs.
    try:
        await asyncio.wait_for(_hash_slots.acquire(), timeout=0.01)
    except TimeoutError:
        raise HTTPException(503, "Login temporarily unavailable")
    slots = _hash_slots
    try:
        # A directly retained executor future is never canceled with the caller.
        # Repeated task cancellation must not free capacity while scrypt still runs.
        job = asyncio.get_running_loop().run_in_executor(None, function, *args)
    except BaseException:
        slots.release()
        raise
    _hash_jobs.add(job)

    def completed(done):
        _hash_jobs.discard(done)
        slots.release()
        # Retrieve detached failures without logging passwords or hash inputs.
        if not done.cancelled():
            done.exception()

    job.add_done_callback(completed)
    return await asyncio.shield(job)


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
    return {"enabled": get_settings().local_password_enabled, "mfa_policy": get_settings().local_mfa_policy}


@router.put("/admin/api/users/{user_id}/password", status_code=204)
async def provision(user_id: str, body: PasswordBody, identity: Identity = Depends(admin)):
    enabled()
    encoded = await derive(password_hash, body.password.get_secret_value())
    async with db.transaction() as s:
        await revalidate_admin(s, identity)
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
        factor = await s.get(db.LocalMFA, user_id)
        if factor:
            factor.grant_digest, factor.grant_expires = None, None
            factor.challenge_digest, factor.challenge_expires = None, None
            factor.pending_seed, factor.password_binding = None, None
        await s.execute(delete(db.BrowserSession).where(db.BrowserSession.user_id == user_id))
        await db.audit(s, identity.user.id, "local_password.reset", user_id)


@router.delete("/admin/api/users/{user_id}/password", status_code=204)
async def revoke(user_id: str, identity: Identity = Depends(admin)):
    # Revocation remains available even while login is disabled.
    async with db.transaction() as s:
        await revalidate_admin(s, identity)
        await s.execute(delete(db.LocalPassword).where(db.LocalPassword.user_id == user_id))
        factor = await s.get(db.LocalMFA, user_id)
        if factor:
            factor.grant_digest, factor.grant_expires = None, None
            factor.challenge_digest, factor.challenge_expires = None, None
            factor.pending_seed, factor.password_binding = None, None
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
    async with db.transaction() as s:
        user = await s.get(db.User, user_id) if user_id else None
        row = await s.get(db.LocalPassword, user_id) if user_id else None
        if (not valid or not user or not row or row.password_hash != encoded or not user.enabled
                or user.scim_managed or user.oidc_subject or user.scim_external_id
                or not await db.active_membership(s, user, user.team_id)):
            raise HTTPException(401, "Invalid credentials")
        from .local_mfa import challenge
        factor = await s.get(db.LocalMFA, user.id)
        if factor and factor.seed:
            token = challenge(factor, encoded)
            return {"mfa_required": True, "challenge": token, "expires_in": 300}
        if cfg.local_mfa_policy == "required":
            raise HTTPException(403, "MFA enrollment required")
        return await issue_session(s, user, request)


async def issue_session(s, user, request, mfa=False):
    cfg = get_settings()
    raw, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    old = request.cookies.get("gateway_session")
    if old:
        await s.execute(delete(db.BrowserSession).where(db.BrowserSession.digest == digest(old)))
    s.add(db.BrowserSession(digest=digest(raw), user_id=user.id, csrf=csrf,
                           expires_at=db.now() + timedelta(seconds=cfg.local_session_seconds), mfa_verified=mfa))
    await db.audit(s, user.id, "local_password.login")
    target = "/admin" if user.role == "admin" else "/portal"
    response = JSONResponse({"redirect": target})
    response.set_cookie("gateway_session", raw, httponly=True, secure=cfg.public_url.startswith("https://"),
                        samesite="lax", max_age=cfg.local_session_seconds, path="/")
    return response


class ChangePasswordBody(BaseModel):
    current_password: SecretStr = Field(min_length=1, max_length=128)
    new_password: SecretStr = Field(min_length=15, max_length=128)
    code: str | None = Field(default=None, pattern=r"^[0-9]{6}$")


# Browser-only: a scoped API key must never become an account takeover primitive.
@router.post("/account/api/password", status_code=204)
async def change_password(body: ChangePasswordBody, request: Request,
                          identity: Identity = Depends(browser_member)):
    from .local_mfa import origin, eligible, totp
    from .security import decrypt, revalidate_identity
    origin(request)
    if identity.session.oidc_issuer:
        raise HTTPException(409, "Manage your password at your identity provider")
    await throttle(identity.user.email, request.client.host if request.client else "unknown")
    async with db.session_factory() as s:
        password = await s.get(db.LocalPassword, identity.user.id)
        encoded = password.password_hash if password else None
    valid = await derive(verify, body.current_password.get_secret_value(), encoded)
    if not valid:
        raise HTTPException(401, "Current credentials are invalid")
    replacement = await derive(password_hash, body.new_password.get_secret_value())
    async with db.transaction() as s:
        current = await revalidate_identity(s, identity)
        password = await s.get(db.LocalPassword, current.user.id)
        if (not eligible(current.user) or not password or password.password_hash != encoded
                or current.session.oidc_issuer
                or not await db.active_membership(s, current.user, current.user.team_id)):
            raise HTTPException(401, "Authorization changed")
        factor = await s.get(db.LocalMFA, current.user.id)
        if factor and factor.seed:
            seed = decrypt(factor.seed)
            counter = int(db.now().timestamp()) // 30
            matches = [step for step in (counter - 1, counter, counter + 1)
                       if step > factor.last_counter
                       and hmac.compare_digest(totp(seed, step), body.code or "")]
            if not matches:
                raise HTTPException(401, "Current credentials are invalid")
            factor.last_counter = max(matches)
        password.password_hash = replacement
        if factor:
            factor.grant_digest, factor.grant_expires = None, None
            factor.challenge_digest, factor.challenge_expires = None, None
            factor.pending_seed, factor.password_binding = None, None
        # Reauthentication is explicit, including this browser. Keep active MFA.
        await s.execute(delete(db.BrowserSession).where(db.BrowserSession.user_id == current.user.id))
        await db.audit(s, current.user.id, "account.password.change")
    from fastapi.responses import Response
    response = Response(status_code=204)
    response.delete_cookie("gateway_session", path="/", httponly=True,
                           secure=get_settings().public_url.startswith("https://"), samesite="lax")
    return response
