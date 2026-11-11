from datetime import timedelta

from gateway import db
from gateway.security import digest


async def setup(client, admin_headers):
    teams = [(await client.post("/admin/api/teams", headers=admin_headers, json={"name": name})).json() for name in ["primary", "secondary", "other"]]
    users = [(await client.post("/admin/api/users", headers=admin_headers, json={"email": email, "team_id": teams[0]["id"]})).json() for email in ["delegate@example.com", "target@example.com"]]
    path = "/teams/api/" + teams[1]["id"]
    for user, role in zip(users, ["admin", "member"]):
        response = await client.put(path + "/members/" + user["id"], headers=admin_headers, json={"role": role})
        assert response.status_code == 200, response.text
    key = (await client.post("/admin/api/keys", headers=admin_headers, json={
        "user_id": users[0]["id"], "team_id": teams[1]["id"], "name": "delegate",
        "permissions": ["team_admin"], "expires_in_days": 365,
    })).json()
    return teams, users, path, {"Authorization": "Bearer " + key["key"]}


async def test_delegate_isolation_removal_and_no_privilege_escalation(client, admin_headers):
    teams, users, path, auth = await setup(client, admin_headers)
    assert [t["id"] for t in (await client.get("/teams/api", headers=auth)).json()] == [teams[1]["id"]]
    assert len((await client.get("/teams/api", headers=admin_headers)).json()) == 3
    assert (await client.get(path + "/members", headers=auth)).status_code == 200
    assert (await client.get("/teams/api/" + teams[2]["id"] + "/members", headers=auth)).status_code == 403
    assert (await client.get("/admin/api/users", headers=auth)).status_code == 403
    assert (await client.put(path + "/members/" + users[1]["id"], headers=auth, json={"role": "admin"})).status_code == 403
    body = {"user_id": users[1]["id"], "name": "inference"}
    result = await client.post(path + "/keys", headers=auth, json=body)
    assert result.status_code == 201, result.text
    key = result.json()
    member_auth = {"Authorization": "Bearer " + key["key"]}
    assert (await client.get("/v1/models", headers=member_auth)).status_code == 200
    assert (await client.post(path + "/keys", headers=auth, json={**body, "permissions": ["admin"]})).status_code == 403
    assert (await client.delete(path + "/members/" + users[1]["id"], headers=auth)).status_code == 200
    assert (await client.get("/v1/models", headers=member_auth)).status_code == 401
    assert (await client.post(path + "/keys/" + key["id"] + "/rotate", headers=auth)).status_code == 409
    assert (await client.put(path + "/members/" + users[1]["id"], headers=auth, json={})).status_code == 200
    assert (await client.get("/v1/models", headers=member_auth)).status_code == 401
    assert (await client.put(path + "/members/" + users[0]["id"], headers=admin_headers, json={"role": "member"})).status_code == 200
    assert (await client.get(path + "/members", headers=auth)).status_code == 403


async def test_bounds_and_session_team_selection(client, admin_headers):
    teams, users, path, _ = await setup(client, admin_headers)
    key = (await client.post("/admin/api/keys", headers=admin_headers, json={
        "user_id": users[0]["id"], "team_id": teams[1]["id"], "name": "limited",
        "permissions": ["team_admin"], "expires_in_days": 2, "allowed_models": ["chat"], "rpm": 5, "tpm": 1000,
    })).json()
    auth = {"Authorization": "Bearer " + key["key"]}
    body = {"user_id": users[1]["id"], "name": "new", "expires_in_days": 1, "allowed_models": ["chat"], "rpm": 5, "tpm": 1000}
    assert (await client.post(path + "/keys", headers=auth, json=body)).status_code == 201
    for patch in [{"allowed_models": []}, {"rpm": 6}, {"tpm": 1001}, {"expires_in_days": 3}]:
        assert (await client.post(path + "/keys", headers=auth, json={**body, **patch})).status_code == 403
    async with db.transaction() as s:
        s.add(db.BrowserSession(digest=digest("team-cookie"), user_id=users[0]["id"], csrf="csrf", expires_at=db.now()+timedelta(hours=1)))
    client.cookies.set("gateway_session", "team-cookie")
    async with db.transaction() as s:
        (await s.get(db.Team, teams[0]["id"])).enabled = False
    assert (await client.get(path + "/members")).status_code == 200
    assert (await client.get("/teams/api/" + teams[0]["id"] + "/members")).status_code == 404
    assert (await client.post(path + "/keys", json=body)).status_code == 403


async def test_agent_membership_rechecked_at_exchange_commit_and_reserve(client, admin_headers):
    import secrets
    import uuid
    import pytest
    from fastapi import HTTPException
    from gateway import policy
    from gateway.security import Identity

    _, users, path, auth = await setup(client, admin_headers)
    result = await client.post(path + "/keys", headers=auth, json={"user_id": users[1]["id"], "name": "agent", "kind": "agent"})
    key = result.json()
    root = {"Authorization": "Bearer " + key["key"]}
    token = (await client.post("/auth/agent/exchange", headers=root)).json()["access_token"]
    operation, candidate = str(uuid.uuid4()), "lg_" + secrets.token_urlsafe(32)
    assert (await client.post("/auth/agent/rotation/prepare", headers=root, json={"operation_id": operation, "next_credential": candidate})).status_code == 200
    async with db.session_factory() as s:
        identity = Identity(await s.get(db.User, users[1]["id"]), await s.get(db.APIKey, key["id"]), generation=0)
    assert (await client.delete(path + "/members/" + users[1]["id"], headers=auth)).status_code == 200
    assert (await client.post("/auth/agent/exchange", headers=root)).status_code == 401
    assert (await client.get("/v1/models", headers={"Authorization": "Bearer " + token})).status_code == 401
    assert (await client.post("/auth/agent/rotation/commit", headers={"Authorization": "Bearer " + candidate}, json={"operation_id": operation})).status_code == 401
    with pytest.raises(HTTPException):
        await policy.reserve(identity, 10, 10, "chat")


async def test_user_budget_global_across_selected_teams(client, admin_headers):
    import pytest
    from fastapi import HTTPException
    from gateway import policy
    from gateway.security import Identity

    teams, users, path, auth = await setup(client, admin_headers)
    keys = []
    for team in teams[:2]:
        response = await client.post("/admin/api/keys", headers=admin_headers, json={
            "user_id": users[1]["id"], "team_id": team["id"], "name": team["name"]})
        keys.append(response.json())
    async with db.transaction() as s:
        user = await s.get(db.User, users[1]["id"])
        user.budget_micros = 100
    identities = []
    async with db.session_factory() as s:
        for key in keys:
            identities.append(Identity(await s.get(db.User, users[1]["id"]), await s.get(db.APIKey, key["id"])))
    await policy.reserve(identities[0], 60, 10, "chat")
    with pytest.raises(HTTPException) as error:
        await policy.reserve(identities[1], 60, 10, "chat")
    assert error.value.status_code == 429


async def test_stale_identity_cannot_mutate_after_role_revoked(client, admin_headers):
    import pytest
    from fastapi import HTTPException
    from sqlalchemy import select
    from gateway.security import Identity
    from gateway.teams import authorize

    teams, users, path, _ = await setup(client, admin_headers)
    async with db.session_factory() as s:
        key = await s.scalar(select(db.APIKey).where(db.APIKey.user_id == users[0]['id']))
        stale = Identity(await s.get(db.User, users[0]['id']), key)
    assert (await client.put(path + '/members/' + users[0]['id'], headers=admin_headers,
                             json={'role': 'member'})).status_code == 200
    async with db.transaction() as s:
        with pytest.raises(HTTPException) as error:
            await authorize(s, stale, teams[1]['id'])
        assert error.value.status_code == 403
