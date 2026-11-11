from datetime import timedelta

import pytest
from gateway import db
from gateway.security import digest
from tests.test_account import fixtures


async def browser(client, uid):
    async with db.transaction() as s:
        s.add(
            db.BrowserSession(
                digest=digest("member-session"),
                user_id=uid,
                csrf="member-csrf",
                expires_at=db.now() + timedelta(hours=1),
            )
        )
    client.cookies.set("gateway_session", "member-session")
    return {"X-CSRF-Token": "member-csrf"}


async def test_agent_cannot_access_member_control_plane(client, admin_headers):
    user, keys = await fixtures(client, admin_headers)
    agent = (
        await client.post(
            "/admin/api/keys",
            headers=admin_headers,
            json={"name": "agent", "kind": "agent", "user_id": user["id"]},
        )
    ).json()
    token = (
        await client.post("/auth/agent/exchange", headers={"Authorization": "Bearer " + agent["key"]})
    ).json()["access_token"]
    headers = {"Authorization": "Bearer " + token}
    for path in ["/account/api/profile", "/account/api/keys"]:
        assert (await client.get(path, headers=headers)).status_code == 403
    for action in ["rotate", "revoke"]:
        assert (
            await client.post(f"/account/api/keys/{keys[0]['id']}/{action}", headers=headers)
        ).status_code == 403


@pytest.mark.parametrize("state", ["expired", "revoked", "old-team"])
async def test_browser_cannot_rotate_inactive_scopes(client, admin_headers, state):
    user, keys = await fixtures(client, admin_headers)
    headers = await browser(client, user["id"])
    async with db.transaction() as s:
        key = await s.get(db.APIKey, keys[0]["id"])
        if state == "expired":
            key.expires_at = db.now() - timedelta(seconds=1)
        elif state == "revoked":
            key.revoked = True
        else:
            team = db.Team(name="previous-team")
            s.add(team)
            await s.flush()
            key.team_id = team.id
    response = await client.post(f"/account/api/keys/{keys[0]['id']}/rotate", headers=headers)
    assert response.status_code == 409


async def test_cross_user_scope_csrf_origin_and_logout(client, admin_headers):
    user, keys = await fixtures(client, admin_headers)
    other = (
        await client.post(
            "/admin/api/users", headers=admin_headers, json={"email": "other-portal@example.com"}
        )
    ).json()
    other_key = (
        await client.post(
            "/admin/api/keys", headers=admin_headers, json={"name": "other", "user_id": other["id"]}
        )
    ).json()
    headers = await browser(client, user["id"])
    for action in ["rotate", "revoke"]:
        assert (
            await client.post(f"/account/api/keys/{other_key['id']}/{action}", headers=headers)
        ).status_code == 404
    assert (
        await client.post(
            f"/account/api/keys/{keys[0]['id']}/rotate", headers={**headers, "Origin": "https://evil.example"}
        )
    ).status_code == 403
    assert (await client.post("/auth/logout", headers=headers)).status_code == 303
    assert (await client.get("/account/api/profile")).status_code == 401
    client.cookies.set("gateway_session", "member-session")
    assert (await client.get("/account/api/keys")).status_code == 401
