import asyncio
import time
import ipaddress
import base64
import hashlib
import hmac
import json
import secrets
from datetime import timedelta
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
import jwt
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select, delete, or_

from sqlalchemy.exc import IntegrityError
from . import db
from .config import get_settings
from .security import aware, digest, identify

router = APIRouter()
OIDC_HTTP_DEADLINE_SECONDS = 15


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
        if not await db.active_membership(s, user, key.team_id):
            raise HTTPException(401, "User disabled or team changed")
        if key.team_id:
            team = await s.get(db.Team, key.team_id)
            if not team or not team.enabled:
                raise HTTPException(403, "Team disabled")
        lifetime = min(300, int((aware(key.expires_at) - db.now()).total_seconds()))
        if lifetime < 1:
            raise HTTPException(401, "Credential expired")
        token = jwt.encode({"sub": user.id, "kid": key.id, "aud": "llm-gateway", "iss": "llm-gateway",
                            "gen": key.generation, "iat": db.now(), "exp": db.now() + timedelta(seconds=lifetime), "jti": db.uid()},
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
    response = RedirectResponse("/admin" if identity.user.role == "admin" else "/portal", status_code=303)
    response.delete_cookie("gateway_session", path="/")
    return response


# Replica-local caches contain public metadata only. No expired entry is used on failure.
_oidc_cache = {}
_oidc_cache_lock = asyncio.Lock()
_oidc_rotation_attempts = {}


def clear_oidc_cache():
    _oidc_cache.clear()
    _oidc_rotation_attempts.clear()


def validate_oidc_url(value):
    cfg = get_settings()
    try:
        url = urlparse(value)
        issuer = urlparse(cfg.oidc_issuer)
        allowed = {f"https://{issuer.netloc}", *json.loads(cfg.oidc_trusted_endpoint_origins)}
        if url.scheme != "https" or not url.hostname or url.username or url.password or url.fragment or url.port not in (None, 443):
            raise ValueError("Unsafe URL")
        if f"https://{url.netloc}" not in allowed or url.hostname.lower() == "localhost":
            raise ValueError("Untrusted endpoint origin")
        try:
            address = ipaddress.ip_address(url.hostname)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise ValueError("Private address")
    except (TypeError, ValueError) as exc:
        raise HTTPException(503, "Unsafe OIDC endpoint metadata") from exc
    return value


async def oidc_json(url, *, form=None):
    validate_oidc_url(url)
    try:
        async with asyncio.timeout(OIDC_HTTP_DEADLINE_SECONDS):
            async with httpx.AsyncClient(timeout=10, follow_redirects=False, trust_env=False,
                                         headers={"Accept-Encoding": "identity"}) as client:
                async with client.stream("POST" if form is not None else "GET", url, data=form) as response:
                    if response.status_code != 200:
                        raise HTTPException(503, "OIDC endpoint unavailable")
                    if response.headers.get("content-encoding", "identity").strip().lower() != "identity":
                        raise HTTPException(503, "Compressed OIDC responses are unsupported")
                    chunks = bytearray()
                    async for chunk in response.aiter_bytes():
                        chunks.extend(chunk)
                        if len(chunks) > 262144:
                            raise HTTPException(503, "OIDC response too large")
                    value = json.loads(chunks)
                    if not isinstance(value, dict):
                        raise ValueError("Expected JSON object")
                    return value
    except (httpx.HTTPError, ValueError, TimeoutError) as exc:
        raise HTTPException(503, "OIDC endpoint unavailable") from exc


async def cached_oidc_json(url, *, kind, force=False):
    # Bound single-flight lock wait as well as the network operation. An offline
    # IdP must not create a serial queue of indefinitely waiting login requests.
    try:
        async with asyncio.timeout(OIDC_HTTP_DEADLINE_SECONDS):
            return await _cached_oidc_json(url, kind=kind, force=force)
    except TimeoutError:
        raise HTTPException(503, "OIDC metadata temporarily unavailable") from None


async def _cached_oidc_json(url, *, kind, force=False):
    cfg = get_settings()
    cache_key = (cfg.oidc_issuer, cfg.oidc_trusted_endpoint_origins, cfg.oidc_cache_seconds, kind, url)
    async with _oidc_cache_lock:
        now = time.monotonic()
        entry = _oidc_cache.get(cache_key)
        if entry and entry[0] > now and not force:
            return entry[1]
        if force:
            # At most one unknown-kid refresh per ten seconds per replica/issuer.
            if now - _oidc_rotation_attempts.get(cache_key, float("-inf")) < 10:
                if entry and entry[0] > now:
                    return entry[1]
                raise HTTPException(503, "OIDC refresh temporarily unavailable")
            _oidc_rotation_attempts[cache_key] = now
        value = await oidc_json(url)
        if kind == "discovery":
            if value.get("issuer") != cfg.oidc_issuer:
                raise HTTPException(503, "OIDC issuer mismatch")
            for field in ["authorization_endpoint", "token_endpoint", "jwks_uri"]:
                validate_oidc_url(value.get(field, ""))
        elif not isinstance(value.get("keys"), list) or not 1 <= len(value["keys"]) <= 100 or not all(isinstance(k, dict) for k in value["keys"]):
            raise HTTPException(503, "Invalid OIDC signing keys")
        if len(_oidc_cache) >= 32:
            _oidc_cache.clear()
            _oidc_rotation_attempts.clear()
        _oidc_cache[cache_key] = (time.monotonic() + cfg.oidc_cache_seconds, value)
        return value


async def discovery():
    cfg = get_settings()
    if not cfg.oidc_issuer or not cfg.oidc_client_id:
        raise HTTPException(503, "OIDC not configured")
    return await cached_oidc_json(cfg.oidc_issuer.rstrip("/") + "/.well-known/openid-configuration", kind="discovery")


def group_claim(claims):
    value = claims
    for part in get_settings().oidc_group_claim.split("."):
        if not isinstance(value, dict) or part not in value:
            return []
        value = value[part]
    return value


async def oidc_signing_key(token, metadata):
    jwks = await cached_oidc_json(metadata["jwks_uri"], kind="jwks")
    header = jwt.get_unverified_header(token)
    if not isinstance(header.get("kid"), str) or not 1 <= len(header["kid"]) <= 256:
        raise ValueError("Signing key identifier required")
    if header.get("alg") not in {"RS256", "ES256"}:
        raise ValueError("Unsupported signature")
    candidates = [k for k in jwks["keys"] if k.get("kid") == header.get("kid") and k.get("use", "sig") == "sig"]
    if not candidates:
        jwks = await cached_oidc_json(metadata["jwks_uri"], kind="jwks", force=True)
        candidates = [k for k in jwks["keys"] if k.get("kid") == header.get("kid") and k.get("use", "sig") == "sig"]
    if len(candidates) != 1 or candidates[0].get("alg", header["alg"]) != header["alg"]:
        raise ValueError("Unknown signing key")
    if "key_ops" in candidates[0] and (not isinstance(candidates[0]["key_ops"], list) or "verify" not in candidates[0]["key_ops"]):
        raise ValueError("Key not allowed for verification")
    key = jwt.PyJWK.from_dict(candidates[0], algorithm=header["alg"]).key
    return key, header["alg"]


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
    response = RedirectResponse(metadata["authorization_endpoint"] + ("&" if "?" in metadata["authorization_endpoint"] else "?") + urlencode(params))
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
    token_response = await oidc_json(metadata["token_endpoint"], form={"grant_type": "authorization_code", "code": code,
        "client_id": cfg.oidc_client_id, "client_secret": cfg.oidc_client_secret, "code_verifier": verifier,
        "redirect_uri": cfg.public_url.rstrip("/") + "/auth/oidc/callback"})
    token = token_response.get("id_token", "")
    try:
        key, algorithm = await oidc_signing_key(token, metadata)
        claims = jwt.decode(token, key, algorithms=[algorithm], audience=cfg.oidc_client_id,
                            issuer=cfg.oidc_issuer, options={"require": ["exp", "iat", "sub", "nonce"]})
        if type(claims["iat"]) is not int or not 0 <= time.time() - claims["iat"] <= 300:
            raise ValueError("Stale identity token")
        if not hmac.compare_digest(str(claims["nonce"]), nonce):
            raise ValueError("Nonce mismatch")
        if isinstance(claims["aud"], list) and len(claims["aud"]) > 1 and claims.get("azp") != cfg.oidc_client_id:
            raise ValueError("Authorized party mismatch")
        if claims.get("email_verified") is not True or not isinstance(claims.get("email"), str):
            raise ValueError("Verified email required")
        if "sid" in claims and (not isinstance(claims["sid"], str) or not 1 <= len(claims["sid"]) <= 512):
            raise ValueError("Invalid session identifier")
        email = claims["email"]
        if len(email) > 320 or "@" not in email or any(c.isspace() for c in email) or len(cfg.oidc_issuer + "|" + claims["sub"]) > 512:
            raise ValueError("Invalid identity fields")
    except (ValueError, TypeError, KeyError, jwt.PyJWTError) as exc:
        raise HTTPException(401, "Invalid identity token") from exc
    groups = group_claim(claims)
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
        # Gate waits must not extend JWT validity or outlive logout tombstones.
        try:
            jwt.decode(token, key, algorithms=[algorithm], audience=cfg.oidc_client_id,
                       issuer=cfg.oidc_issuer, options={"require": ["exp", "iat", "sub", "nonce"]})
            if not 0 <= time.time() - claims["iat"] <= 300:
                raise ValueError("Stale identity token")
        except (ValueError, jwt.PyJWTError) as exc:
            raise HTTPException(401, "Invalid identity token") from exc
        # Both logout and callback hold the shared DB policy writer gate.
        # A logout delivered during the IdP exchange therefore prevents a stale
        # token from resurrecting a revoked session, regardless of replica.
        logged_out = await s.scalar(select(db.OIDCLogoutReplay.digest).where(
            db.OIDCLogoutReplay.issuer == cfg.oidc_issuer,
            db.OIDCLogoutReplay.expires_at > db.now(),
            db.OIDCLogoutReplay.issued_at >= claims["iat"],
            or_(db.OIDCLogoutReplay.subject.is_(None), db.OIDCLogoutReplay.subject == claims["sub"]),
            or_(db.OIDCLogoutReplay.sid.is_(None), db.OIDCLogoutReplay.sid == claims.get("sid")),
        ).limit(1))
        if logged_out:
            raise HTTPException(401, "Identity session has been logged out")
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
                               oidc_issuer=cfg.oidc_issuer, oidc_subject=claims["sub"], oidc_sid=claims.get("sid"),
                               expires_at=db.now() + timedelta(seconds=cfg.oidc_session_seconds)))
        await db.audit(s, user.id, "oidc.login")
    response = RedirectResponse("/admin" if user.role == "admin" else "/portal", status_code=303)
    response.set_cookie("gateway_session", raw, httponly=True, secure=cfg.public_url.startswith("https://"),
                        samesite="lax", max_age=cfg.oidc_session_seconds, path="/")
    response.delete_cookie("gateway_oidc_state", path="/auth/oidc")
    return response


@router.post("/auth/oidc/backchannel-logout")
async def oidc_backchannel_logout(request: Request):
    """Signed IdP logout, with durable single-use IDs across gateway replicas."""
    cfg = get_settings()
    # Bound before parsing; tokens, credentials and bodies are never audited.
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/x-www-form-urlencoded":
        raise HTTPException(400, "Expected logout_token form")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 32768:
            raise HTTPException(400, "Logout token too large")
    try:
        form = parse_qs(body.decode("ascii"), strict_parsing=True, max_num_fields=8)
        if set(form) != {"logout_token"} or len(form["logout_token"]) != 1:
            raise ValueError("Expected one logout token")
        token = form["logout_token"][0]
        metadata = await discovery()
        key, algorithm = await oidc_signing_key(token, metadata)
        claims = jwt.decode(token, key, algorithms=[algorithm], audience=cfg.oidc_client_id,
                            issuer=cfg.oidc_issuer, options={"require": ["iss", "aud", "iat", "jti"]})
        if type(claims["iat"]) is not int or not 0 <= time.time() - claims["iat"] <= 300:
            raise ValueError("Stale logout token")
        if "nonce" in claims:
            raise ValueError("Logout token must not contain nonce")
        if claims.get("events") != {"http://schemas.openid.net/event/backchannel-logout": {}}:
            raise ValueError("Logout event required")
        for field in ("jti", "sid", "sub"):
            if field in claims and (not isinstance(claims[field], str) or not 1 <= len(claims[field]) <= 512):
                raise ValueError("Invalid logout identifier")
        if "sid" not in claims and "sub" not in claims:
            raise ValueError("Session or subject required")
    except (ValueError, TypeError, KeyError, UnicodeError, jwt.PyJWTError) as exc:
        raise HTTPException(400, "Invalid logout token") from exc
    replay_id = digest(json.dumps([cfg.oidc_issuer, claims["jti"]]))
    try:
        async with db.transaction() as s:
            try:
                jwt.decode(token, key, algorithms=[algorithm], audience=cfg.oidc_client_id,
                           issuer=cfg.oidc_issuer, options={"require": ["iss", "aud", "iat", "jti"]})
                if not 0 <= time.time() - claims["iat"] <= 300:
                    raise ValueError("Stale logout token")
            except (ValueError, jwt.PyJWTError) as exc:
                raise HTTPException(400, "Invalid logout token") from exc
            # Unique primary key arbitrates concurrent requests across replicas.
            # Expired records are safe to remove because token age is bounded.
            await s.execute(delete(db.OIDCLogoutReplay).where(db.OIDCLogoutReplay.expires_at < db.now()))
            s.add(db.OIDCLogoutReplay(digest=replay_id, expires_at=db.now() + timedelta(seconds=600),
                                     issuer=cfg.oidc_issuer, subject=claims.get("sub"), sid=claims.get("sid"),
                                     issued_at=claims["iat"]))
            await s.flush()
            filters = [db.BrowserSession.oidc_issuer == cfg.oidc_issuer]
            if "sid" in claims:
                filters.append(db.BrowserSession.oidc_sid == claims["sid"])
            if "sub" in claims:
                filters.append(db.BrowserSession.oidc_subject == claims["sub"])
            await s.execute(delete(db.BrowserSession).where(*filters))
            await db.audit(s, "oidc", "oidc.backchannel_logout")
    except IntegrityError:
        # A replay must not revoke sessions established since the first delivery.
        async with db.transaction() as s:
            if not await s.get(db.OIDCLogoutReplay, replay_id):
                raise
    return Response(status_code=200, headers={"Cache-Control": "no-store"})
