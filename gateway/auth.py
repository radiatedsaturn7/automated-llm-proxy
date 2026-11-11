import base64
import hashlib
import hmac
import json
import secrets
from datetime import timedelta
from urllib.parse import urlencode, urlparse

import httpx
import jwt
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from . import db
from .config import get_settings
from .security import aware, digest, identify

router = APIRouter()


@router.post("/auth/agent/exchange")
async def exchange(request: Request):
    authorization = request.headers.get("authorization", "")
    if not authorization.startswith("Bearer lg_"):
        raise HTTPException(401, "Agent credential required")
    async with db.transaction() as s:
        key = await s.scalar(select(db.APIKey).where(db.APIKey.digest == digest(authorization[7:])))
        if not key or key.kind != "agent" or key.revoked or aware(key.expires_at) <= db.now():
            raise HTTPException(401, "Invalid agent credential")
        user = await s.get(db.User, key.user_id)
        if not user or not user.enabled or key.team_id != user.team_id:
            raise HTTPException(401, "User disabled or team changed")
        if key.team_id:
            team = await s.get(db.Team, key.team_id)
            if not team or not team.enabled:
                raise HTTPException(403, "Team disabled")
        lifetime = min(300, int((aware(key.expires_at) - db.now()).total_seconds()))
        if lifetime < 1:
            raise HTTPException(401, "Credential expired")
        token = jwt.encode({"sub": user.id, "kid": key.id, "aud": "llm-gateway", "iss": "llm-gateway",
                            "iat": db.now(), "exp": db.now() + timedelta(seconds=lifetime), "jti": db.uid()},
                           get_settings().gateway_signing_key, algorithm="HS256")
        await db.audit(s, user.id, "agent.exchange", key.id)
    return {"access_token": token, "expires_in": lifetime, "token_type": "Bearer"}


@router.get("/auth/session")
async def session_status(request: Request):
    try:
        identity = await identify(request, allow_session=True)
    except HTTPException:
        return {"authenticated": False, "oidc_enabled": bool(get_settings().oidc_issuer)}
    return {"authenticated": True, "role": identity.user.role, "email": identity.user.email,
            "csrf_token": identity.session.csrf if identity.session else None}


@router.post("/auth/logout")
async def logout(request: Request):
    identity = await identify(request, allow_session=True)
    async with db.transaction() as s:
        if identity.session:
            row = await s.get(db.BrowserSession, identity.session.digest)
            if row:
                await s.delete(row)
        await db.audit(s, identity.user.id, "session.logout")
    response = RedirectResponse("/admin", status_code=303)
    response.delete_cookie("gateway_session", path="/")
    return response


async def discovery():
    cfg = get_settings()
    if not cfg.oidc_issuer or not cfg.oidc_client_id:
        raise HTTPException(503, "OIDC not configured")
    if urlparse(cfg.oidc_issuer).scheme != "https":
        raise HTTPException(503, "OIDC issuer must use HTTPS")
    async with httpx.AsyncClient(timeout=10, follow_redirects=False, trust_env=False) as client:
        response = await client.get(cfg.oidc_issuer.rstrip("/") + "/.well-known/openid-configuration")
        response.raise_for_status()
        data = response.json()
    if data.get("issuer") != cfg.oidc_issuer:
        raise HTTPException(503, "OIDC issuer mismatch")
    for field in ["authorization_endpoint", "token_endpoint", "jwks_uri"]:
        url = urlparse(data.get(field, ""))
        if url.scheme != "https" or not url.netloc or url.username:
            raise HTTPException(503, "Unsafe OIDC metadata")
    return data


@router.get("/auth/oidc/start")
async def oidc_start():
    cfg = get_settings()
    metadata = await discovery()
    state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    async with db.transaction() as s:
        s.add(db.OIDCState(digest=digest(state), nonce=nonce, verifier=verifier,
                          expires_at=db.now() + timedelta(minutes=10)))
    params = {"client_id": cfg.oidc_client_id, "response_type": "code", "scope": "openid email profile",
              "redirect_uri": cfg.public_url.rstrip("/") + "/auth/oidc/callback", "state": state,
              "nonce": nonce, "code_challenge": challenge, "code_challenge_method": "S256"}
    response = RedirectResponse(metadata["authorization_endpoint"] + "?" + urlencode(params))
    response.set_cookie("gateway_oidc_state", state, httponly=True, secure=cfg.public_url.startswith("https://"),
                        samesite="lax", max_age=600, path="/auth/oidc")
    return response


@router.get("/auth/oidc/callback")
async def oidc_callback(request: Request, state: str = "", code: str = ""):
    cfg = get_settings()
    if not state or not code or not hmac.compare_digest(state, request.cookies.get("gateway_oidc_state", "")):
        raise HTTPException(400, "Invalid login state")
    async with db.transaction() as s:
        saved = await s.get(db.OIDCState, digest(state))
        if not saved or aware(saved.expires_at) <= db.now():
            raise HTTPException(400, "Expired login state")
        nonce, verifier = saved.nonce, saved.verifier
        await s.delete(saved)  # Single-use before external exchange, even on failure.
    metadata = await discovery()
    async with httpx.AsyncClient(timeout=10, follow_redirects=False, trust_env=False) as client:
        response = await client.post(metadata["token_endpoint"], data={"grant_type": "authorization_code", "code": code,
            "client_id": cfg.oidc_client_id, "client_secret": cfg.oidc_client_secret, "code_verifier": verifier,
            "redirect_uri": cfg.public_url.rstrip("/") + "/auth/oidc/callback"})
        if response.status_code != 200:
            raise HTTPException(401, "Identity provider rejected login")
        token = response.json().get("id_token", "")
        jwks_response = await client.get(metadata["jwks_uri"])
        jwks_response.raise_for_status()
        jwks = jwks_response.json()
    try:
        header = jwt.get_unverified_header(token)
        if header.get("alg") not in {"RS256", "ES256"}:
            raise ValueError("Unsupported signature")
        candidates = [k for k in jwks["keys"] if k.get("kid") == header.get("kid") and k.get("use", "sig") == "sig"]
        if len(candidates) != 1:
            raise ValueError("Unknown signing key")
        key = jwt.PyJWK.from_dict(candidates[0], algorithm=header["alg"]).key
        claims = jwt.decode(token, key, algorithms=[header["alg"]], audience=cfg.oidc_client_id,
                            issuer=cfg.oidc_issuer, options={"require": ["exp", "iat", "sub", "nonce"]})
        if not hmac.compare_digest(str(claims["nonce"]), nonce):
            raise ValueError("Nonce mismatch")
        if isinstance(claims["aud"], list) and len(claims["aud"]) > 1 and claims.get("azp") != cfg.oidc_client_id:
            raise ValueError("Authorized party mismatch")
        if claims.get("email_verified") is not True or not isinstance(claims.get("email"), str):
            raise ValueError("Verified email required")
    except (ValueError, KeyError, jwt.PyJWTError) as exc:
        raise HTTPException(401, "Invalid identity token") from exc
    groups = claims.get("groups", [])
    if not isinstance(groups, list) or not all(isinstance(g, str) for g in groups):
        raise HTTPException(403, "Identity groups missing")
    matches = [cfg.groups[g] for g in groups if g in cfg.groups]
    # Multiple mappings to distinct policies are ambiguous; fail closed.
    distinct = {json.dumps(m, sort_keys=True) for m in matches}
    if len(distinct) != 1:
        raise HTTPException(403, "No unambiguous authorized SSO group")
    mapping = matches[0]
    subject = cfg.oidc_issuer + "|" + claims["sub"]
    raw = secrets.token_urlsafe(32)
    async with db.transaction() as s:
        user = await s.scalar(select(db.User).where(db.User.oidc_subject == subject))
        if not user:
            if await s.scalar(select(db.User).where(db.User.email == claims["email"].lower())):
                raise HTTPException(409, "Email exists; administrator must explicitly link identity")
            user = db.User(email=claims["email"].lower(), oidc_subject=subject, role=mapping["role"], team_id=mapping.get("team_id"), enabled=True)
            s.add(user)
        if not user.enabled:
            raise HTTPException(403, "User disabled")
        if mapping.get("team_id"):
            team = await s.get(db.Team, mapping["team_id"])
            if not team or not team.enabled:
                raise HTTPException(403, "SSO team unavailable")
        user.role, user.team_id = mapping["role"], mapping.get("team_id")
        await s.flush()
        s.add(db.BrowserSession(digest=digest(raw), user_id=user.id, csrf=secrets.token_urlsafe(32),
                               expires_at=db.now() + timedelta(hours=8)))
        await db.audit(s, user.id, "oidc.login")
    response = RedirectResponse("/admin", status_code=303)
    response.set_cookie("gateway_session", raw, httponly=True, secure=cfg.public_url.startswith("https://"),
                        samesite="lax", max_age=28800, path="/")
    response.delete_cookie("gateway_oidc_state", path="/auth/oidc")
    return response
