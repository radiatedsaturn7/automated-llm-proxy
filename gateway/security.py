import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import timedelta, timezone

import jwt
from cryptography.fernet import Fernet
from fastapi import HTTPException, Request
from sqlalchemy import select

from . import db
from .config import get_settings


def digest(value: str):
    return hmac.new(get_settings().gateway_signing_key.encode(), value.encode(), hashlib.sha256).hexdigest()


def aware(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def encrypt(value):
    return Fernet(get_settings().gateway_master_key.encode()).encrypt(value.encode()).decode()


def decrypt(value):
    return Fernet(get_settings().gateway_master_key.encode()).decrypt(value.encode()).decode()


@dataclass
class Identity:
    user: db.User
    key: db.APIKey | None = None
    session: db.BrowserSession | None = None


async def identify(request: Request, allow_session=False):
    auth = request.headers.get("authorization", "")
    if db.session_factory is None:
        db.init_engine()
    async with db.session_factory() as session:
        key = None
        browser_session = None
        if auth.startswith("Bearer "):
            token = auth[7:]
            if token.startswith("lg_"):
                key = await session.scalar(select(db.APIKey).where(db.APIKey.digest == digest(token)))
                if key and key.kind != "api":
                    key = None
            else:
                try:
                    claims = jwt.decode(token, get_settings().gateway_signing_key, algorithms=["HS256"],
                                        audience="llm-gateway", issuer="llm-gateway", options={"require": ["exp", "iat", "sub", "kid"]})
                    key = await session.get(db.APIKey, claims["kid"])
                    if key is None or key.kind != "agent" or key.user_id != claims["sub"]:
                        key = None
                except jwt.PyJWTError:
                    pass
            if key is None or key.revoked or aware(key.expires_at) <= db.now():
                raise HTTPException(401, "Invalid or expired credential")
            user = await session.get(db.User, key.user_id)
        elif allow_session and request.cookies.get("gateway_session"):
            browser_session = await session.get(db.BrowserSession, digest(request.cookies["gateway_session"]))
            if not browser_session or aware(browser_session.expires_at) <= db.now():
                raise HTTPException(401, "Session expired")
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                if not hmac.compare_digest(request.headers.get("x-csrf-token", ""), browser_session.csrf):
                    raise HTTPException(403, "CSRF token required")
                origin = request.headers.get("origin")
                if origin and origin.rstrip("/") != get_settings().public_url.rstrip("/"):
                    raise HTTPException(403, "Invalid origin")
            user = await session.get(db.User, browser_session.user_id)
        else:
            raise HTTPException(401, "Bearer credential required")
        if user is None or not user.enabled:
            raise HTTPException(401, "User disabled")
        team_id = key.team_id if key else user.team_id
        if key and team_id != user.team_id:
            raise HTTPException(401, "Credential team no longer matches user")
        if team_id:
            team = await session.get(db.Team, team_id)
            if team is None or not team.enabled:
                raise HTTPException(403, "Team disabled")
        return Identity(user, key, browser_session)


def require_permission(identity, permission):
    if identity.key:
        effective = ["inference"] if identity.key.kind == "agent" else identity.key.permissions
        if not isinstance(effective, list) or permission not in effective:
            raise HTTPException(403, "Credential permission required: " + permission)


async def inference_identity(request: Request):
    identity = await identify(request)
    require_permission(identity, "inference")
    return identity


async def provisioning(request: Request):
    identity = await identify(request, allow_session=True)
    if identity.user.role != "admin":
        raise HTTPException(403, "Administrator owner required for provisioning")
    if identity.key and (identity.key.kind == "agent" or not isinstance(identity.key.permissions, list)
                         or not {"admin", "provisioning"}.intersection(identity.key.permissions)):
        raise HTTPException(403, "Provisioning credential required")
    return identity


async def admin(request: Request):
    identity = await identify(request, allow_session=True)
    if identity.user.role != "admin":
        raise HTTPException(403, "Administrator role required")
    require_permission(identity, "admin")
    # Local relays are data-plane credentials, never administrator credentials.
    if identity.key and identity.key.kind == "agent":
        raise HTTPException(403, "Agent tokens cannot administer gateway")
    return identity


def new_key(user_id, team_id, name, expires_in_days=30, kind="api", **policy):
    policy.setdefault("permissions", ["inference"])
    raw = "lg_" + secrets.token_urlsafe(32)
    key = db.APIKey(name=name, prefix=raw[:11], digest=digest(raw), user_id=user_id, team_id=team_id,
                    kind=kind, expires_at=db.now() + timedelta(days=expires_in_days), **policy)
    return raw, key
