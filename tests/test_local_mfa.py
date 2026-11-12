import asyncio
import base64
from datetime import timedelta

import pytest
from sqlalchemy import delete
from gateway import db
from gateway.config import get_settings
from gateway.local_mfa import totp
from gateway.security import decrypt
from test_local_auth import setup, PASSWORD, ORIGIN


async def begin(client, admin_headers, monkeypatch):
    uid = await setup(client, admin_headers, monkeypatch)
    result = await client.post(f"/admin/api/users/{uid}/mfa/setup-grant", headers=admin_headers)
    assert result.status_code == 200, result.text
    grant = result.json()["setup_grant"]
    response = await client.post(
        "/auth/local/mfa/enroll/start",
        headers=ORIGIN,
        json={"email": "admin@example.com", "password": PASSWORD, "setup_grant": grant},
    )
    assert response.status_code == 200, response.text
    return uid, grant, response.json()


def code(seed, offset=0):
    return totp(seed, int(db.now().timestamp()) // 30 + offset)


async def finish(client, data, offset=0):
    return await client.post(
        "/auth/local/mfa/verify",
        headers=ORIGIN,
        json={"challenge": data["challenge"], "code": code(data["secret"], offset)},
    )


@pytest.mark.parametrize(
    "stamp,expected",
    [
        (59, "287082"),
        (1111111109, "081804"),
        (1111111111, "050471"),
        (1234567890, "005924"),
        (2000000000, "279037"),
        (20000000000, "353130"),
    ],
)
def test_rfc6238_vectors(stamp, expected):
    assert totp(base64.b32encode(b"12345678901234567890").decode(), stamp // 30) == expected


async def test_enrollment_one_use_grant_and_replay(client, admin_headers, monkeypatch):
    # A replay must use the same TOTP counter even if slow password hashing or
    # CI scheduling would otherwise cross the real clock's 30-second boundary.
    frozen = db.now()
    monkeypatch.setattr(db, "now", lambda: frozen)
    uid, grant, data = await begin(client, admin_headers, monkeypatch)
    repeated = await client.post(
        "/auth/local/mfa/enroll/start",
        headers=ORIGIN,
        json={"email": "admin@example.com", "password": PASSWORD, "setup_grant": grant},
    )
    assert repeated.status_code == 401
    results = await asyncio.gather(finish(client, data), finish(client, data))
    assert sorted(r.status_code for r in results) == [200, 401]
    async with db.session_factory() as s:
        row = await s.get(db.LocalMFA, uid)
        assert row.seed != data["secret"] and decrypt(row.seed) == data["secret"]
        assert row.pending_seed is None and row.challenge_digest is None
    login = await client.post(
        "/auth/local/login", headers=ORIGIN, json={"email": "admin@example.com", "password": PASSWORD}
    )
    assert login.json()["mfa_required"]
    assert "set-cookie" not in login.headers
    data["challenge"] = login.json()["challenge"]
    assert (await finish(client, data)).status_code == 401
    assert (await finish(client, data, 1)).status_code == 200


async def test_required_invalidates_existing_local_not_oidc(client, admin_headers, monkeypatch):
    uid = await setup(client, admin_headers, monkeypatch)
    login = {"email": "admin@example.com", "password": PASSWORD}
    assert (await client.post("/auth/local/login", headers=ORIGIN, json=login)).status_code == 200
    monkeypatch.setenv("LOCAL_MFA_POLICY", "required")
    get_settings.cache_clear()
    assert not (await client.get("/auth/session")).json()["authenticated"]
    assert (await client.post("/auth/local/login", headers=ORIGIN, json=login)).status_code == 403
    async with db.transaction() as s:
        from sqlalchemy import select

        row = await s.scalar(select(db.BrowserSession).where(db.BrowserSession.user_id == uid))
        row.oidc_issuer = "https://idp.test"
    assert (await client.get("/auth/session")).json()["authenticated"]


async def test_reset_removes_password_and_all_sessions(client, admin_headers, monkeypatch):
    uid, _, data = await begin(client, admin_headers, monkeypatch)
    assert (await finish(client, data)).status_code == 200
    assert (await client.delete(f"/admin/api/users/{uid}/mfa", headers=admin_headers)).status_code == 204
    assert not (await client.get("/auth/session")).json()["authenticated"]
    async with db.session_factory() as s:
        assert await s.get(db.LocalPassword, uid) is None
        assert await s.get(db.LocalMFA, uid) is None


@pytest.mark.parametrize("mutation", ["expired", "password", "disabled", "federated"])
async def test_challenge_rechecks_identity(client, admin_headers, monkeypatch, mutation):
    uid, _, data = await begin(client, admin_headers, monkeypatch)
    async with db.transaction() as s:
        factor = await s.get(db.LocalMFA, uid)
        if mutation == "expired":
            factor.challenge_expires = db.now() - timedelta(seconds=1)
        if mutation == "password":
            (await s.get(db.LocalPassword, uid)).password_hash = "different"
        if mutation == "disabled":
            (await s.get(db.User, uid)).enabled = False
        if mutation == "federated":
            (await s.get(db.User, uid)).oidc_subject = "issuer|subject"
    assert (await finish(client, data)).status_code == 401


async def test_attempts_persist_and_expire(client, admin_headers, monkeypatch):
    uid, _, data = await begin(client, admin_headers, monkeypatch)
    wrong = next(
        f"{i:06d}" for i in range(100) if f"{i:06d}" not in [code(data["secret"], j) for j in [-1, 0, 1]]
    )
    for _ in range(5):
        assert (
            await client.post(
                "/auth/local/mfa/verify", headers=ORIGIN, json={"challenge": data["challenge"], "code": wrong}
            )
        ).status_code == 401
    async with db.transaction() as s:
        assert (await s.get(db.LocalMFA, uid)).attempts == 5
        await s.execute(delete(db.LoginThrottle))
    assert (await finish(client, data)).status_code == 401


async def test_enrollment_csrf_and_password_reset_preserves_factor(client, admin_headers, monkeypatch):
    uid, _, data = await begin(client, admin_headers, monkeypatch)
    assert (
        await client.post(
            "/auth/local/mfa/verify", json={"challenge": data["challenge"], "code": code(data["secret"])}
        )
    ).status_code == 403
    assert (await finish(client, data)).status_code == 200
    assert (
        await client.put(
            f"/admin/api/users/{uid}/password", headers=admin_headers, json={"password": PASSWORD + "changed"}
        )
    ).status_code == 204
    async with db.session_factory() as s:
        assert (await s.get(db.LocalMFA, uid)).seed is not None
    login = await client.post(
        "/auth/local/login",
        headers=ORIGIN,
        json={"email": "admin@example.com", "password": PASSWORD + "changed"},
    )
    assert login.json()["mfa_required"]


async def test_expired_grant_and_cleanup_preserve_active_factor(client, admin_headers, monkeypatch):
    from gateway.maintenance import cleanup

    uid, grant, data = await begin(client, admin_headers, monkeypatch)
    assert (await finish(client, data)).status_code == 200
    async with db.transaction() as s:
        row = await s.get(db.LocalMFA, uid)
        row.grant_expires = db.now() - timedelta(seconds=1)
        row.challenge_expires = db.now() - timedelta(seconds=1)
        row.pending_seed = row.seed
    assert (await cleanup())["mfa_transient_rows"] == 1
    assert (await cleanup(apply=True))["mfa_transient_rows"] == 1
    async with db.session_factory() as s:
        row = await s.get(db.LocalMFA, uid)
        assert row.seed and row.last_counter >= 0
        assert row.pending_seed is None and row.challenge_digest is None
        assert row.grant_expires is None


async def test_assurance_revalidated_inside_mutation_transaction(client, admin_headers, monkeypatch):
    from fastapi import HTTPException
    from gateway.security import Identity, revalidate_identity
    from sqlalchemy import select

    await setup(client, admin_headers, monkeypatch)
    assert (
        await client.post(
            "/auth/local/login", headers=ORIGIN, json={"email": "admin@example.com", "password": PASSWORD}
        )
    ).status_code == 200
    async with db.session_factory() as s:
        user = await s.scalar(select(db.User))
        browser = await s.scalar(select(db.BrowserSession))
    identity = Identity(user, session=browser)
    monkeypatch.setenv("LOCAL_MFA_POLICY", "required")
    get_settings.cache_clear()
    async with db.transaction() as s:
        with pytest.raises(HTTPException) as exc:
            await revalidate_identity(s, identity)
        assert exc.value.status_code == 401


async def test_mfa_preflight_fail_closed_no_seed_echo(client, admin_headers, monkeypatch):
    from gateway.check import inspect_installation

    uid, _, data = await begin(client, admin_headers, monkeypatch)
    assert (await inspect_installation())["checks"]["mfa_seeds"]
    async with db.transaction() as s:
        (await s.get(db.LocalMFA, uid)).pending_seed = "corrupt-secret-never-output"
    report = await inspect_installation()
    assert not report["ok"] and not report["checks"]["mfa_seeds"]
    assert "corrupt-secret-never-output" not in str(report)
    assert data["secret"] not in str(report)


async def test_expired_setup_grant(client, admin_headers, monkeypatch):
    uid = await setup(client, admin_headers, monkeypatch)
    grant = (await client.post(f"/admin/api/users/{uid}/mfa/setup-grant", headers=admin_headers)).json()[
        "setup_grant"
    ]
    async with db.transaction() as s:
        (await s.get(db.LocalMFA, uid)).grant_expires = db.now() - timedelta(seconds=1)
    result = await client.post(
        "/auth/local/mfa/enroll/start",
        headers=ORIGIN,
        json={"email": "admin@example.com", "password": PASSWORD, "setup_grant": grant},
    )
    assert result.status_code == 401


async def test_parallel_enrollment_consumes_grant_once(client, admin_headers, monkeypatch):
    uid = await setup(client, admin_headers, monkeypatch)
    grant = (await client.post(f"/admin/api/users/{uid}/mfa/setup-grant", headers=admin_headers)).json()[
        "setup_grant"
    ]

    async def start():
        return await client.post(
            "/auth/local/mfa/enroll/start",
            headers=ORIGIN,
            json={"email": "admin@example.com", "password": PASSWORD, "setup_grant": grant},
        )

    results = await asyncio.gather(start(), start())
    assert sorted(r.status_code for r in results) == [200, 401]


async def test_team_disabled_and_role_change_at_verification(client, admin_headers, monkeypatch):
    uid, _, data = await begin(client, admin_headers, monkeypatch)
    async with db.transaction() as s:
        team = db.Team(name="mfa-team", budget_micros=1000000, enabled=False)
        s.add(team)
        await s.flush()
        user = await s.get(db.User, uid)
        user.team_id = team.id
    assert (await finish(client, data)).status_code == 401
    async with db.transaction() as s:
        user = await s.get(db.User, uid)
        user.team_id = None
        user.role = "member"
    result = await finish(client, data)
    assert result.status_code == 200
    assert result.json()["redirect"] == "/portal"
