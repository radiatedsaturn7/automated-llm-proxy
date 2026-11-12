import pytest
from tests.conftest import client as client, admin_headers as admin_headers


@pytest.mark.parametrize("scope", ["inference", "account", "team_admin", "provisioning", "agent"])
async def test_admin_owned_service_key_cannot_read_global_content(client, admin_headers, scope):
    users = (await client.get("/admin/api/users", headers=admin_headers)).json()
    uid = next(u["id"] for u in users if u["role"] == "admin")
    body = {"user_id": uid, "name": scope, "permissions": [scope] if scope != "agent" else ["inference"]}
    if scope == "agent":
        body["kind"] = "agent"
    created = await client.post("/admin/api/keys", headers=admin_headers, json=body)
    assert created.status_code == 201, created.text
    key = created.json()["key"]
    if scope == "agent":
        key = (await client.post("/auth/agent/exchange", headers={"Authorization": "Bearer " + key})).json()[
            "access_token"
        ]
    headers = {"Authorization": "Bearer " + key}
    for path in [
        "/admin/api/prompt-registry/templates",
        "/admin/api/prompt-registry/templates/fake/versions",
        "/admin/api/prompt-registry/evaluations",
        "/admin/api/prompt-registry/evaluations/fake",
        "/admin/api/exports/audit",
        "/admin/api/exports/usage",
    ]:
        response = await client.get(path, headers=headers)
        assert response.status_code == 403, (scope, path, response.text)
    response = await client.post(
        "/admin/api/evaluation-comparisons",
        headers=headers,
        json={"cases": [{"baseline": "a", "candidate": "a", "expected": "a"}]},
    )
    assert response.status_code == 403, response.text


async def test_export_cursor_is_not_capability_or_cross_admin_ticket(client, admin_headers):
    first = await client.get("/admin/api/exports/audit", headers=admin_headers, params={"limit": 1})
    # Ensure enough audit entries to produce a continuation.
    second = await client.post(
        "/admin/api/users", headers=admin_headers, json={"email": "secondadmin@example.com", "role": "admin"}
    )
    uid = second.json()["id"]
    created = await client.post(
        "/admin/api/keys",
        headers=admin_headers,
        json={"user_id": uid, "name": "admin-two", "permissions": ["admin"]},
    )
    headers = {"Authorization": "Bearer " + created.json()["key"]}
    first = await client.get("/admin/api/exports/audit", headers=admin_headers, params={"limit": 1})
    cursor = first.json()["next_cursor"]
    assert cursor
    assert (await client.get("/admin/api/exports/audit", params={"cursor": cursor})).status_code == 401
    assert (
        await client.get("/admin/api/exports/audit", headers=headers, params={"cursor": cursor})
    ).status_code == 400
