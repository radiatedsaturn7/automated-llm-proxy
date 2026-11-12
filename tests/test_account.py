from datetime import timedelta
from sqlalchemy import select
from gateway import db
from gateway.security import digest


async def fixtures(client, admin_headers):
    user = (
        await client.post(
            "/admin/api/users", headers=admin_headers, json={"email": "member@example.com", "budget_usd": 10}
        )
    ).json()
    keys = []
    for name in ["one", "two"]:
        result = await client.post(
            "/admin/api/keys",
            headers=admin_headers,
            json={
                "name": name,
                "user_id": user["id"],
                "allowed_models": ["chat"],
                "rpm": 5,
                "permissions": ["inference", "account"],
            },
        )
        keys.append(result.json())
    return user, keys


async def test_key_auth_cannot_access_sibling_or_escalate(client, admin_headers):
    _, keys = await fixtures(client, admin_headers)
    auth = {"Authorization": "Bearer " + keys[0]["key"]}
    result = (await client.get("/account/api/keys", headers=auth)).json()
    assert [k["id"] for k in result] == [keys[0]["id"]]
    assert "digest" not in str(result)
    assert (
        await client.post("/account/api/keys/" + keys[1]["id"] + "/rotate", headers=auth)
    ).status_code == 404
    result = await client.post("/account/api/keys/" + keys[0]["id"] + "/rotate", headers=auth)
    assert result.status_code == 200, result.text
    assert result.json()["allowed_models"] == ["chat"]
    assert result.json()["rpm"] == 5
    assert result.json()["expires_at"].split("+")[0] == keys[0]["expires_at"].split("+")[0]
    assert (await client.get("/account/api/profile", headers=auth)).status_code == 401


async def test_session_selfservice_csrf_ownership_and_no_create(client, admin_headers):
    user, keys = await fixtures(client, admin_headers)
    async with db.transaction() as session:
        session.add(
            db.BrowserSession(
                digest=digest("portal-cookie"),
                user_id=user["id"],
                csrf="csrf",
                expires_at=db.now() + timedelta(hours=1),
            )
        )
        admin_id = await session.scalar(select(db.APIKey.id).where(db.APIKey.user_id != user["id"]))
    client.cookies.set("gateway_session", "portal-cookie")
    result = await client.get("/account/api/profile")
    assert result.json()["budget_micros"] == 10000000
    assert result.json()["csrf_token"] == "csrf"
    assert len((await client.get("/account/api/keys")).json()) == 2
    assert (await client.post("/account/api/keys/" + keys[0]["id"] + "/revoke")).status_code == 403
    headers = {"X-CSRF-Token": "csrf"}
    assert (
        await client.post("/account/api/keys/" + admin_id + "/revoke", headers=headers)
    ).status_code == 404
    assert (
        await client.post("/account/api/keys/" + keys[0]["id"] + "/revoke", headers=headers)
    ).status_code == 200
    assert (
        await client.post("/account/api/keys", headers=headers, json={"name": "unlimited", "operation_id": "d58c5eb1-0545-4c32-9f8e-00865a6466ee"})
    ).status_code == 409
    assert (await client.get("/portal")).status_code == 200
