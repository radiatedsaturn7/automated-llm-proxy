from datetime import timedelta
from gateway import db


async def test_export_paginated_signed_cursor_and_window(client, admin_headers):
    async with db.transaction() as session:
        for index in range(5):
            await db.audit(session, "test", f"export.test.{index}")
    result = await client.get("/admin/api/exports/audit", headers=admin_headers, params={"limit": 2})
    assert result.status_code == 200, result.text
    first = result.json()
    assert len(first["records"]) == 2 and first["next_cursor"]
    ids = [row["id"] for row in first["records"]]
    cursor = first["next_cursor"]
    while cursor:
        response = await client.get(
            "/admin/api/exports/audit", headers=admin_headers, params={"limit": 2, "cursor": cursor}
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["until"] == first["until"]
        ids.extend(row["id"] for row in data["records"])
        cursor = data["next_cursor"]
    assert len(ids) == len(set(ids)) == 6  # bootstrap plus five events
    tampered = first["next_cursor"][:-8] + "AAAAAAAA"
    assert (
        await client.get("/admin/api/exports/audit", headers=admin_headers, params={"cursor": tampered})
    ).status_code == 400
    assert (
        await client.get(
            "/admin/api/exports/usage", headers=admin_headers, params={"cursor": first["next_cursor"]}
        )
    ).status_code == 400


async def test_export_limits_and_access(client, admin_headers):
    assert (await client.get("/admin/api/exports/audit")).status_code == 401
    assert (
        await client.get(
            "/admin/api/exports/audit", headers=admin_headers, params={"since": "2020-01-01T00:00:00"}
        )
    ).status_code == 400
    old = (db.now() - timedelta(days=32)).isoformat()
    assert (
        await client.get("/admin/api/exports/audit", headers=admin_headers, params={"since": old})
    ).status_code == 400
    assert (
        await client.get("/admin/api/exports/audit", headers=admin_headers, params={"limit": 1001})
    ).status_code == 422
    assert (await client.get("/admin/api/exports/usage", headers=admin_headers)).json()["records"] == []
