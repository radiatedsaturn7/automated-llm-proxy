from sqlalchemy import select

from gateway import db


async def issue(client, admin_headers, permissions=None, role="admin", kind="api"):
    if role == "admin":
        user = next(u for u in (await client.get("/admin/api/users", headers=admin_headers)).json() if u["role"] == "admin")
    else:
        user = (await client.post("/admin/api/users", headers=admin_headers, json={"email": db.uid() + "@example.com"})).json()
    body = {"user_id": user["id"], "name": "scoped", "kind": kind}
    if permissions is not None:
        body["permissions"] = permissions
    result = await client.post("/admin/api/keys", headers=admin_headers, json=body)
    assert result.status_code == 201, result.text
    key = result.json()
    return {"Authorization": "Bearer " + key["key"]}, key


async def test_admin_owned_default_key_is_inference_only(client, admin_headers):
    headers, key = await issue(client, admin_headers)
    assert key["permissions"] == ["inference"]
    assert (await client.get("/v1/models", headers=headers)).status_code == 200
    for path in ["/admin/api/users", "/account/api/profile", "/scim/v2/Users"]:
        assert (await client.get(path, headers=headers)).status_code == 403


async def test_provisioning_only_key_cannot_infer_or_administer(client, admin_headers):
    headers, _ = await issue(client, admin_headers, ["provisioning"])
    assert (await client.get("/scim/v2/Users", headers=headers)).status_code == 200
    for path in ["/admin/api/users", "/account/api/profile", "/v1/models"]:
        assert (await client.get(path, headers=headers)).status_code == 403
    result = await client.post("/v1/chat/completions", headers=headers,
                               json={"model": "x", "messages": [{"role": "user", "content": "test"}]})
    assert result.status_code == 403
    async with db.session_factory() as session:
        assert (await session.scalars(select(db.Reservation))).all() == []


async def test_role_and_scope_both_required(client, admin_headers):
    headers, _ = await issue(client, admin_headers, ["admin", "provisioning", "account"], role="member")
    assert (await client.get("/account/api/profile", headers=headers)).status_code == 200
    assert (await client.get("/admin/api/users", headers=headers)).status_code == 403
    assert (await client.get("/scim/v2/Users", headers=headers)).status_code == 403
    admin_only, _ = await issue(client, admin_headers, ["admin"])
    assert (await client.get("/admin/api/users", headers=admin_only)).status_code == 200
    assert (await client.get("/scim/v2/Users", headers=admin_only)).status_code == 200
    assert (await client.get("/v1/models", headers=admin_only)).status_code == 403


async def test_rotations_preserve_exact_permissions(client, admin_headers):
    headers, key = await issue(client, admin_headers, ["account"])
    result = await client.post(f"/account/api/keys/{key['id']}/rotate", headers=headers,
                               json={"permissions": ["admin", "inference"]})
    assert result.status_code == 200
    replacement = result.json()
    assert replacement["permissions"] == ["account"]
    assert (await client.get("/account/api/profile", headers=headers)).status_code == 401
    replacement_headers = {"Authorization": "Bearer " + replacement["key"]}
    assert (await client.get("/v1/models", headers=replacement_headers)).status_code == 403
    rotated = await client.post(f"/admin/api/keys/{replacement['id']}/rotate", headers=admin_headers)
    assert rotated.status_code == 200
    assert rotated.json()["permissions"] == ["account"]


async def test_agent_never_receives_control_plane_authority(client, admin_headers):
    headers, key = await issue(client, admin_headers, kind="agent")
    # Even legacy/corrupt excess scope metadata cannot grant agent admin rights.
    async with db.transaction() as session:
        stored = await session.get(db.APIKey, key["id"])
        stored.permissions = ["admin", "account", "provisioning"]
    response = await client.post("/auth/agent/exchange", headers=headers)
    assert response.status_code == 200
    access = {"Authorization": "Bearer " + response.json()["access_token"]}
    assert (await client.get("/v1/models", headers=access)).status_code == 200
    for path in ["/admin/api/users", "/account/api/profile", "/scim/v2/Users"]:
        assert (await client.get(path, headers=access)).status_code == 403


async def test_permissions_validation(client, admin_headers):
    user = next(u for u in (await client.get("/admin/api/users", headers=admin_headers)).json() if u["role"] == "admin")
    for permissions in [[], ["unknown"], ["admin", "admin"]]:
        assert (await client.post("/admin/api/keys", headers=admin_headers, json={
            "name": "bad", "user_id": user["id"], "permissions": permissions})).status_code == 422
    assert (await client.post("/admin/api/keys", headers=admin_headers, json={
        "name": "bad-agent", "user_id": user["id"], "kind": "agent", "permissions": ["admin"]})).status_code == 422
