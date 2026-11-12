import asyncio
from contextlib import asynccontextmanager
import uuid
import secrets
from tests.conftest import client as client, admin_headers as admin_headers
from tests.test_scim import add_user
from tests.test_scim_groups import group
from gateway import db


async def test_delete_recreate_cannot_commit_pending_agent_rotation(client, admin_headers):
    user = await add_user(client, admin_headers)
    gid = await group(client, admin_headers, [user["id"]])
    key = (
        await client.post(
            "/admin/api/keys",
            headers=admin_headers,
            json={"name": "device", "user_id": user["id"], "kind": "agent"},
        )
    ).json()
    operation = str(uuid.uuid4())
    candidate = "lg_" + secrets.token_urlsafe(32)
    old = {"Authorization": "Bearer " + key["key"]}
    new = {"Authorization": "Bearer " + candidate}
    assert (
        await client.post(
            "/auth/agent/rotation/prepare",
            headers=old,
            json={"operation_id": operation, "next_credential": candidate},
        )
    ).status_code == 200
    assert (await client.delete("/scim/v2/Groups/" + gid, headers=admin_headers)).status_code == 204
    replacement = await group(client, admin_headers, [user["id"]])
    assert replacement != gid
    assert (await client.post("/auth/agent/exchange", headers=old)).status_code == 401
    assert (
        await client.post("/auth/agent/rotation/commit", headers=new, json={"operation_id": operation})
    ).status_code == 401


async def test_late_admin_grant_prevents_delete_under_gate(client, admin_headers, monkeypatch):
    user = await add_user(client, admin_headers)
    gid = await group(client, admin_headers, [user["id"]])
    original = db.transaction
    entered, release = asyncio.Event(), asyncio.Event()

    @asynccontextmanager
    async def paused(*args, **kwargs):
        entered.set()
        await release.wait()
        async with original(*args, **kwargs) as session:
            yield session

    monkeypatch.setattr(db, "transaction", paused)
    task = asyncio.create_task(client.delete("/scim/v2/Groups/" + gid, headers=admin_headers))
    await asyncio.wait_for(entered.wait(), 5)
    async with original() as session:
        session.add(db.TeamMembership(user_id=user["id"], team_id=gid, role="admin"))
    release.set()
    assert (await task).status_code == 403
    async with db.session_factory() as session:
        assert (await session.get(db.Team, gid)).deleted_at is None
        assert (await session.get(db.User, user["id"])).team_id == gid
