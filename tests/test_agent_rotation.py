import json
import os
import secrets
import uuid
from datetime import timedelta

import httpx
import pytest
from fastapi import HTTPException
from starlette.requests import Request
from sqlalchemy import select

from gateway import db, policy
from gateway.relay_journal import CredentialJournal, RotationError
from gateway.security import identify


def secret():
    return "lg_" + secrets.token_urlsafe(32)


async def agent(client, admin_headers):
    users = (await client.get("/admin/api/users", headers=admin_headers)).json()
    response = await client.post("/admin/api/keys", headers=admin_headers, json={
        "user_id": users[0]["id"], "name": "device", "kind": "agent", "allowed_models": ["chat"], "rpm": 11})
    assert response.status_code == 201
    return response.json()


def headers(value):
    return {"Authorization": "Bearer " + value}


async def prepare(client, old, candidate, operation):
    return await client.post("/auth/agent/rotation/prepare", headers=headers(old),
                             json={"operation_id": operation, "next_credential": candidate})


async def commit(client, candidate, operation):
    return await client.post("/auth/agent/rotation/commit", headers=headers(candidate), json={"operation_id": operation})


async def test_rotation_preserves_identity_policy_and_fences_access(client, admin_headers):
    key = await agent(client, admin_headers)
    token = (await client.post("/auth/agent/exchange", headers=headers(key["key"]))).json()["access_token"]
    request = Request({"type": "http", "method": "GET", "headers": [(b"authorization", ("Bearer " + token).encode())]})
    stale_identity = await identify(request)
    candidate, operation = secret(), str(uuid.uuid4())
    assert (await prepare(client, key["key"], candidate, operation)).status_code == 200
    assert (await prepare(client, key["key"], candidate, operation)).status_code == 200
    assert (await prepare(client, key["key"], secret(), str(uuid.uuid4()))).status_code == 409
    assert (await commit(client, key["key"], operation)).status_code == 409
    completed = await commit(client, candidate, operation)
    assert completed.status_code == 200
    assert completed.json()["key_id"] == key["id"] and completed.json()["generation"] == 1
    assert (await commit(client, candidate, operation)).json() == completed.json()
    assert (await client.post("/auth/agent/exchange", headers=headers(key["key"]))).status_code == 401
    assert (await client.get("/v1/models", headers=headers(token))).status_code == 401
    with pytest.raises(HTTPException) as error:
        await policy.reserve(stale_identity, 1, 1, "chat")
    assert error.value.status_code == 401
    renewed = await client.post("/auth/agent/exchange", headers=headers(candidate))
    assert renewed.status_code == 200
    async with db.session_factory() as session:
        stored = await session.get(db.APIKey, key["id"])
        assert stored.allowed_models == ["chat"] and stored.rpm == 11
        assert stored.permissions == ["inference"] and stored.user_id == key["user_id"]
        assert stored.expires_at.isoformat().replace("+00:00", "") == key["expires_at"].replace("Z", "").replace("+00:00", "")
        assert stored.digest != candidate and stored.digest != key["key"]
        assert (await session.scalars(select(db.AgentRotation))).all() == []
        audits = (await session.scalars(select(db.Audit))).all()
        assert all(candidate not in json.dumps(a.details) and key["key"] not in json.dumps(a.details) for a in audits)


@pytest.mark.parametrize("invalid", ["revoked", "expired", "disabled"])
async def test_pending_commit_fails_closed_for_invalid_identity(client, admin_headers, invalid):
    key = await agent(client, admin_headers)
    candidate, operation = secret(), str(uuid.uuid4())
    assert (await prepare(client, key["key"], candidate, operation)).status_code == 200
    async with db.transaction() as session:
        stored = await session.get(db.APIKey, key["id"])
        if invalid == "revoked":
            stored.revoked = True
        elif invalid == "expired":
            stored.expires_at = db.now() - timedelta(seconds=1)
        else:
            user = await session.get(db.User, stored.user_id)
            user.enabled = False
    assert (await commit(client, candidate, operation)).status_code == 401


async def test_expiry_replay_and_one_pending_limit(client, admin_headers):
    key = await agent(client, admin_headers)
    candidate, operation = secret(), str(uuid.uuid4())
    assert (await prepare(client, key["key"], candidate, operation)).status_code == 200
    assert (await commit(client, candidate, str(uuid.uuid4()))).status_code == 401
    async with db.transaction() as session:
        pending = await session.get(db.AgentRotation, key["id"])
        pending.expires_at = db.now() - timedelta(seconds=1)
    assert (await commit(client, candidate, operation)).status_code == 410
    assert (await prepare(client, key["key"], candidate, operation)).status_code == 410
    next_candidate, next_operation = secret(), str(uuid.uuid4())
    assert (await prepare(client, key["key"], next_candidate, next_operation)).status_code == 200
    assert (await commit(client, candidate, operation)).status_code == 401
    assert (await commit(client, next_candidate, next_operation)).status_code == 200


class DropResponse:
    def __init__(self, client, endpoint):
        self.client, self.endpoint = client, endpoint
        self.dropped = False

    async def post(self, url, **kwargs):
        response = await self.client.post(url, **kwargs)
        if url.endswith(self.endpoint) and response.status_code == 200 and not self.dropped:
            self.dropped = True
            raise httpx.ReadError("simulated dropped response")
        return response


@pytest.mark.parametrize("endpoint", ["/prepare", "/commit"])
async def test_journal_recovers_dropped_response_after_restart(client, admin_headers, tmp_path, endpoint):
    key = await agent(client, admin_headers)
    directory = tmp_path / "journal"
    directory.mkdir(mode=0o700)
    journal = CredentialJournal(directory, "http://localhost:8080", key["key"])
    journal._write({**journal.state, "last_rotated": 0})
    with pytest.raises(RotationError):
        await journal.credential(DropResponse(client, endpoint), 600)
    candidate = journal.state["pending"]["credential"]
    journal.close()
    # No environment bootstrap secret needed after initialization.
    restarted = CredentialJournal(directory, "http://localhost:8080", "")
    try:
        assert await restarted.credential(client, 600) == candidate
        assert restarted.state["pending"] is None
        assert (await client.post("/auth/agent/exchange", headers=headers(candidate))).status_code == 200
        assert os.stat(directory / "state.json").st_mode & 0o777 == 0o600
    finally:
        restarted.close()


def test_journal_lock_symlinks_permissions_and_gateway_binding(tmp_path):
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    root = secret()
    journal = CredentialJournal(directory, "https://gateway.example", root)
    try:
        with pytest.raises(OSError):
            CredentialJournal(directory, "https://gateway.example", root)
    finally:
        journal.close()
    with pytest.raises(RotationError, match="gateway mismatch"):
        CredentialJournal(directory, "https://other.example", root)
    os.chmod(directory / "state.json", 0o644)
    with pytest.raises(RotationError):
        CredentialJournal(directory, "https://gateway.example", root)
    os.chmod(directory / "state.json", 0o600)
    symlink = tmp_path / "link"
    symlink.symlink_to(directory, target_is_directory=True)
    with pytest.raises(OSError):
        CredentialJournal(symlink, "https://gateway.example", root)


async def test_journal_durability_failure_never_sends_secret(client, admin_headers, tmp_path, monkeypatch):
    key = await agent(client, admin_headers)
    directory = tmp_path / "journal"
    directory.mkdir(mode=0o700)
    journal = CredentialJournal(directory, "http://localhost:8080", key["key"])
    journal._write({**journal.state, "last_rotated": 0})
    class NoNetwork:
        async def post(self, *args, **kwargs):
            pytest.fail("Cannot send rotation before durable journal")
    monkeypatch.setattr(os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("disk failure")))
    try:
        with pytest.raises(RotationError):
            await journal.credential(NoNetwork(), 600)
    finally:
        journal.close()


async def test_expired_journal_pending_recovers_without_losing_current_root(client, admin_headers, tmp_path):
    key = await agent(client, admin_headers)
    directory = tmp_path / "journal"
    directory.mkdir(mode=0o700)
    journal = CredentialJournal(directory, "http://localhost:8080", key["key"])
    journal._write({**journal.state, "last_rotated": 0})
    try:
        with pytest.raises(RotationError):
            await journal.credential(DropResponse(client, "/prepare"), 600)
        async with db.transaction() as session:
            pending = await session.get(db.AgentRotation, key["id"])
            pending.expires_at = db.now() - timedelta(seconds=1)
        with pytest.raises(RotationError, match="expired"):
            await journal.credential(client, 600)
        assert journal.state["pending"] is None and journal.state["current"] == key["key"]
        replacement = await journal.credential(client, 600)
        assert replacement != key["key"]
        assert (await client.post("/auth/agent/exchange", headers=headers(replacement))).status_code == 200
    finally:
        journal.close()


async def test_relay_broker_renews_jwt_using_rotated_root(client, admin_headers, tmp_path):
    from gateway.relay import RelaySettings, TokenBroker
    directory = tmp_path / "journal"
    directory.mkdir(mode=0o700)
    key = await agent(client, admin_headers)
    settings = RelaySettings(central_url="http://localhost:8080", credential=key["key"],
                             local_token="local-" + "x" * 32, journal_directory=str(directory))
    broker = TokenBroker(settings, client)
    try:
        first = await broker.token()
        broker._journal._write({**broker._journal.state, "last_rotated": 0})
        broker._refresh_at = 0
        second = await broker.token()
        assert second != first
        assert (await client.get("/v1/models", headers=headers(first))).status_code == 401
        assert (await client.get("/v1/models", headers=headers(second))).status_code == 200
    finally:
        broker._journal.close()


async def test_candidate_collisions_are_conflicts_and_operations_scoped_to_key(client, admin_headers):
    first, second = await agent(client, admin_headers), await agent(client, admin_headers)
    operation, candidate = str(uuid.uuid4()), secret()
    assert (await prepare(client, first["key"], second["key"], operation)).status_code == 409
    assert (await prepare(client, first["key"], candidate, operation)).status_code == 200
    assert (await prepare(client, second["key"], candidate, str(uuid.uuid4()))).status_code == 409
    # UUID equality alone does not couple independently authenticated devices.
    assert (await prepare(client, second["key"], secret(), operation)).status_code == 200


async def test_rotation_validation_does_not_echo_candidate(client, admin_headers):
    key = await agent(client, admin_headers)
    value = "secret-candidate-invalid"
    response = await prepare(client, key["key"], value, str(uuid.uuid4()))
    assert response.status_code == 422 and value not in response.text


def test_journal_fifo_rejected_without_blocking(tmp_path):
    directory = tmp_path / "journal"
    directory.mkdir(mode=0o700)
    os.mkfifo(directory / "state.json", mode=0o600)
    with pytest.raises(RotationError, match="regular"):
        CredentialJournal(directory, "https://gateway.example", secret())
