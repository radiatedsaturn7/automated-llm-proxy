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


async def test_user_put_replacement_and_admin_guard(client, admin_headers):
    user = await add_user(client, admin_headers)
    path = "/scim/v2/Users/" + user["id"]
    result = await client.put(
        path,
        headers=admin_headers,
        json={
            "schemas": [USER],
            "id": user["id"],
            "userName": "renamed@example.com",
            "externalId": "replacement",
            "active": False,
        },
    )
    assert result.status_code == 200, result.text
    assert result.json()["externalId"] == "replacement" and not result.json()["active"]
    result = await client.put(
        path, headers=admin_headers, json={"schemas": [USER], "userName": "renamed@example.com"}
    )
    assert result.json()["active"] and "externalId" not in result.json()
    assert (
        await client.put(
            path, headers=admin_headers, json={"schemas": [USER], "userName": "x@y.com", "id": "wrong"}
        )
    ).status_code == 400
    admins = (await client.get("/admin/api/users", headers=admin_headers)).json()
    admin_id = next(u["id"] for u in admins if u["role"] == "admin")
    assert (
        await client.put(
            "/scim/v2/Users/" + admin_id,
            headers=admin_headers,
            json={"schemas": [USER], "userName": "overwrite@example.com"},
        )
    ).status_code == 403


async def test_group_add_remove_rename_and_permanent_credential_revocation(client, admin_headers):
    from datetime import timedelta
    from gateway import db
    from gateway.security import digest

    user = await add_user(client, admin_headers)
    group = (
        await client.post(
            "/scim/v2/Groups", headers=admin_headers, json={"schemas": [GROUP], "displayName": "original"}
        )
    ).json()
    path = "/scim/v2/Groups/" + group["id"]

    async def patch(operations):
        return await client.patch(
            path, headers=admin_headers, json={"schemas": [PATCH], "Operations": operations}
        )

    result = await patch([{"op": "Add", "path": "members", "value": [{"value": user["id"]}]}])
    assert result.status_code == 200, result.text
    key = (
        await client.post(
            "/admin/api/keys", headers=admin_headers, json={"name": "client", "user_id": user["id"]}
        )
    ).json()["key"]
    async with db.transaction() as session:
        session.add(
            db.BrowserSession(
                digest=digest("session"),
                user_id=user["id"],
                csrf="csrf",
                expires_at=db.now() + timedelta(hours=1),
            )
        )
    result = await patch(
        [
            {"op": "replace", "path": "displayName", "value": "renamed"},
            {"op": "remove", "path": f'members[value eq "{user["id"]}"]'},
        ]
    )
    assert result.status_code == 200, result.text
    assert result.json()["displayName"] == "renamed" and result.json()["members"] == []
    assert (
        await patch([{"op": "add", "path": "members", "value": [{"value": user["id"]}]}])
    ).status_code == 200
    assert (await client.get("/v1/models", headers={"Authorization": "Bearer " + key})).status_code == 401
    async with db.session_factory() as session:
        assert await session.get(db.BrowserSession, digest("session")) is None


async def test_group_patch_atomic_rollback_and_invalid_paths(client, admin_headers):
    group = (
        await client.post(
            "/scim/v2/Groups", headers=admin_headers, json={"schemas": [GROUP], "displayName": "original"}
        )
    ).json()
    path = "/scim/v2/Groups/" + group["id"]
    for bad in [
        {"op": "remove", "path": 'members[value co "a"]'},
        {"op": "replace", "path": [], "value": []},
        {"op": "replace", "path": "displayName", "value": ""},
    ]:
        response = await client.patch(
            path,
            headers=admin_headers,
            json={
                "schemas": [PATCH],
                "Operations": [{"op": "replace", "path": "displayName", "value": "must-rollback"}, bad],
            },
        )
        assert response.status_code == 400, response.text
        assert (await client.get(path, headers=admin_headers)).json()["displayName"] == "original"


async def test_bounded_discovery(client, admin_headers):
    for kind in ["ResourceTypes", "Schemas"]:
        response = await client.get("/scim/v2/" + kind, headers=admin_headers)
        assert response.status_code == 200
        assert response.json()["totalResults"] == 2
        for item in response.json()["Resources"]:
            detail = await client.get("/scim/v2/" + kind + "/" + item["id"], headers=admin_headers)
            assert detail.status_code == 200 and detail.json() == item
        assert (
            await client.get("/scim/v2/" + kind, headers=admin_headers, params={"count": "999999"})
        ).status_code == 400
    assert (await client.get("/scim/v2/ResourceTypes")).status_code == 401


async def test_group_rename_cannot_modify_admin_team(client, admin_headers):
    from gateway import db

    users = (await client.get("/admin/api/users", headers=admin_headers)).json()
    admin_id = next(u["id"] for u in users if u["role"] == "admin")
    group = (
        await client.post(
            "/scim/v2/Groups", headers=admin_headers, json={"schemas": [GROUP], "displayName": "protected"}
        )
    ).json()
    async with db.transaction() as session:
        account = await session.get(db.User, admin_id)
        account.team_id = group["id"]
    # The old bootstrap key is invalid after a direct team change, so create a valid scoped admin key.
    from gateway.security import new_key

    async with db.transaction() as session:
        raw, key = new_key(admin_id, group["id"], "protected-admin")
        session.add(key)
    headers = {"Authorization": "Bearer " + raw}
    response = await client.patch(
        "/scim/v2/Groups/" + group["id"],
        headers=headers,
        json={
            "schemas": [PATCH],
            "Operations": [{"op": "replace", "path": "displayName", "value": "hijack"}],
        },
    )
    assert response.status_code == 403
