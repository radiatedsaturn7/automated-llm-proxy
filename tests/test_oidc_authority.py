# ruff: noqa: F811
"""Verified login-time group reconciliation must revoke stale authority."""

import asyncio

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from sqlalchemy import select

from gateway import auth, db
from gateway.config import get_settings
from gateway.security import Identity, revalidate_identity
from tests.test_oidc import oidc, start  # noqa: F401


async def login(client, oidc, claims=None):
    oidc["claims"] = claims or {}
    state = await start(client, oidc)
    return await client.get("/auth/oidc/callback", params={"state": state, "code": "login"})


async def identity(client, oidc):
    assert (await login(client, oidc)).status_code == 303
    async with db.session_factory() as s:
        user = await s.scalar(select(db.User))
        browser = await s.scalar(select(db.BrowserSession))
        return Identity(user, session=browser)


@pytest.mark.parametrize("groups", [[], None, "operators", [42], ["operators", "members"]])
async def test_verified_policy_denial_revokes_all_and_requires_explicit_reenable(
    client, oidc, monkeypatch, groups
):
    monkeypatch.setenv("OIDC_GROUP_MAPPING", '{"operators":{"role":"admin"},"members":{"role":"member"}}')
    get_settings.cache_clear()
    old = await identity(client, oidc)
    session = (await client.get("/auth/session")).json()
    key = await client.post(
        "/admin/api/keys",
        headers={"X-CSRF-Token": session["csrf_token"]},
        json={
            "name": "old",
            "user_id": old.user.id,
            "expires_in_days": 1,
            "permissions": ["inference", "admin"],
        },
    )
    assert key.status_code == 201, key.text
    assert (await login(client, oidc, {"groups": groups})).status_code == 403
    assert not (await client.get("/auth/session")).json()["authenticated"]
    async with db.transaction() as s:
        assert not (await s.get(db.User, old.user.id)).enabled
        assert (await s.scalar(select(db.APIKey))).revoked
        assert await s.scalar(select(db.BrowserSession)) is None
        with pytest.raises(HTTPException):
            await revalidate_identity(s, old)
    assert (await login(client, oidc)).status_code in (401, 403)


@pytest.mark.parametrize("bad", ["signature", "nonce", "audience", "email"])
async def test_untrusted_identity_failure_has_no_revocation_effect(client, oidc, bad):
    old = await identity(client, oidc)
    claims = {"groups": []}
    if bad == "signature":
        oidc["signing_key"] = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    if bad == "nonce":
        claims["nonce"] = "wrong"
    if bad == "audience":
        claims["aud"] = "wrong"
    if bad == "email":
        claims["email_verified"] = False
    assert (await login(client, oidc, claims)).status_code == 401
    assert (await client.get("/auth/session")).json()["authenticated"]
    async with db.transaction() as s:
        await revalidate_identity(s, old)
        assert await s.scalar(select(db.OIDCLogoutReplay)) is None


async def test_demote_revokes_and_fences_queued_old_admin_claims(client, oidc, monkeypatch):
    monkeypatch.setenv("OIDC_GROUP_MAPPING", '{"operators":{"role":"admin"},"members":{"role":"member"}}')
    get_settings.cache_clear()
    old = await identity(client, oidc)
    # Capture a legitimate old admin response and hold it before callback writer gate.
    state = await start(client, oidc)
    original = auth.oidc_json
    captured, release = asyncio.Event(), asyncio.Event()

    async def paused(url, **kwargs):
        result = await original(url, **kwargs)
        if url.endswith("/token") and kwargs.get("form", {}).get("code") == "queued":
            captured.set()
            await release.wait()
        return result

    monkeypatch.setattr(auth, "oidc_json", paused)
    task = asyncio.create_task(client.get("/auth/oidc/callback", params={"state": state, "code": "queued"}))
    await asyncio.wait_for(captured.wait(), 5)
    try:
        result = await login(client, oidc, {"groups": ["members"]})
        assert result.status_code == 303, result.text
    finally:
        release.set()
    assert (await task).status_code == 401
    async with db.transaction() as s:
        user = await s.get(db.User, old.user.id)
        assert user.role == "member" and user.enabled
        with pytest.raises(HTTPException):
            await revalidate_identity(s, old)
        assert len((await s.scalars(select(db.BrowserSession))).all()) == 1


async def test_denial_cutoff_survives_immediate_manual_reenable(client, oidc):
    old = await identity(client, oidc)
    assert (await login(client, oidc, {"groups": []})).status_code == 403
    async with db.transaction() as s:
        (await s.get(db.User, old.user.id)).enabled = True
        cutoff = (await s.scalar(select(db.OIDCLogoutReplay))).issued_at
    assert (await login(client, oidc, {"iat": cutoff})).status_code == 401
    # A strictly later fresh verified token can log in after explicit reactivation.
    import time

    while int(time.time()) <= cutoff:
        await asyncio.sleep(0.05)
    assert (await login(client, oidc)).status_code == 303


async def test_valid_team_change_revokes_keys_and_previous_sessions(client, oidc, monkeypatch):
    old = await identity(client, oidc)
    csrf = (await client.get("/auth/session")).json()["csrf_token"]
    headers = {"X-CSRF-Token": csrf}
    key = await client.post(
        "/admin/api/keys",
        headers=headers,
        json={"name": "previous-team", "user_id": old.user.id, "expires_in_days": 1},
    )
    assert key.status_code == 201
    team = (await client.post("/admin/api/teams", headers=headers, json={"name": "new-team"})).json()
    import json

    monkeypatch.setenv(
        "OIDC_GROUP_MAPPING", json.dumps({"operators": {"role": "admin", "team_id": team["id"]}})
    )
    get_settings.cache_clear()
    result = await login(client, oidc)
    assert result.status_code == 303
    async with db.transaction() as s:
        assert (await s.get(db.User, old.user.id)).team_id == team["id"]
        assert (await s.scalar(select(db.APIKey))).revoked
        with pytest.raises(HTTPException):
            await revalidate_identity(s, old)
        assert len((await s.scalars(select(db.BrowserSession))).all()) == 1
