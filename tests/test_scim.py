from gateway.scim import USER, GROUP, PREFIX

PATCH = PREFIX + "api:messages:2.0:PatchOp"


async def add_user(client, headers, name="person@example.com"):
    response = await client.post(
        "/scim/v2/Users", headers=headers, json={"schemas": [USER], "userName": name}
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_scim_lifecycle_filter_offboarding_revokes_keys(client, admin_headers):
    assert (await client.get("/scim/v2/Users")).status_code == 401
    user = await add_user(client, admin_headers)
    key = (
        await client.post(
            "/admin/api/keys", headers=admin_headers, json={"name": "client", "user_id": user["id"]}
        )
    ).json()["key"]
    headers = {"Authorization": "Bearer " + key}
    assert (await client.get("/scim/v2/Users", headers=headers)).status_code == 403
    result = await client.get(
        "/scim/v2/Users",
        headers=admin_headers,
        params={"filter": 'userName eq "person@example.com"', "count": 1},
    )
    assert result.json()["totalResults"] == 1
    assert result.headers["content-type"].startswith("application/scim+json")
    path = "/scim/v2/Users/" + user["id"]
    assert (await client.delete(path, headers=admin_headers)).status_code == 204
    assert not (await client.get(path, headers=admin_headers)).json()["active"]
    assert (await client.get("/v1/models", headers=headers)).status_code == 401
    patch = {"schemas": [PATCH], "Operations": [{"op": "replace", "path": "active", "value": True}]}
    assert (await client.patch(path, headers=admin_headers, json=patch)).status_code == 200
    # Reprovisioning must not revive credentials revoked during offboarding.
    assert (await client.get("/v1/models", headers=headers)).status_code == 401


async def test_scim_groups_single_membership_and_admin_safety(client, admin_headers):
    user = await add_user(client, admin_headers)
    body = {"schemas": [GROUP], "displayName": "provisioned", "members": [{"value": user["id"]}]}
    group = await client.post("/scim/v2/Groups", headers=admin_headers, json=body)
    assert group.status_code == 201, group.text
    assert group.json()["members"][0]["value"] == user["id"]
    assert (
        await client.post("/scim/v2/Groups", headers=admin_headers, json={**body, "displayName": "other"})
    ).status_code == 409
    patch = {"schemas": [PATCH], "Operations": [{"op": "replace", "path": "members", "value": []}]}
    result = await client.patch("/scim/v2/Groups/" + group.json()["id"], headers=admin_headers, json=patch)
    assert result.status_code == 200, result.text
    assert result.json()["members"] == []
    users = (await client.get("/admin/api/users", headers=admin_headers)).json()
    admin_id = next(u["id"] for u in users if u["role"] == "admin")
    assert (await client.delete("/scim/v2/Users/" + admin_id, headers=admin_headers)).status_code == 403
    patch = {"schemas": [PATCH], "Operations": [{"op": "replace", "path": "active", "value": False}]}
    assert (
        await client.patch("/scim/v2/Users/" + admin_id, headers=admin_headers, json=patch)
    ).status_code == 403


async def test_scim_unsupported_fields_fail_explicitly(client, admin_headers):
    assert (
        await client.post(
            "/scim/v2/Users",
            headers=admin_headers,
            json={"schemas": [USER], "userName": "a@b.com", "unsupportedAttribute": "untracked"},
        )
    ).status_code == 400
    assert (
        await client.get("/scim/v2/Users", headers=admin_headers, params={"filter": 'userName co "example"'})
    ).status_code == 400
    assert (
        await client.get("/scim/v2/Users", headers=admin_headers, params={"count": 101})
    ).status_code == 400
    assert (
        await client.post(
            "/scim/v2/Users", headers=admin_headers, json={"schemas": [USER], "userName": "bad"}
        )
    ).status_code == 400
    await add_user(client, admin_headers)
    assert (
        await client.post(
            "/scim/v2/Users",
            headers=admin_headers,
            json={"schemas": [USER], "userName": "person@example.com"},
        )
    ).status_code == 409


async def test_external_id_roundtrip_and_uniqueness(client, admin_headers):
    body = {"schemas": [USER], "userName": "first@example.com", "externalId": "idp-123"}
    result = await client.post("/scim/v2/Users", headers=admin_headers, json=body)
    assert result.status_code == 201, result.text
    assert result.json()["externalId"] == "idp-123"
    result = await client.post(
        "/scim/v2/Users", headers=admin_headers, json={**body, "userName": "second@example.com"}
    )
    assert result.status_code == 409
