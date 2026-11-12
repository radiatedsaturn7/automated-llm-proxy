"""Replacement secrets never refresh a credential's consumed rate allowance."""
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from gateway import db, policy
from gateway.security import Identity
from test_gateway import mock_upstream, setup_inference


async def identity(key_id):
    async with db.session_factory() as s:
        key = await s.get(db.APIKey, key_id)
        return Identity(await s.get(db.User, key.user_id), key)


@pytest.mark.parametrize("path", ["account", "admin", "team"])
@pytest.mark.parametrize("limit", ["rpm", "tpm"])
async def test_rotation_carries_consumed_allowance_and_resets_next_minute(
        client, admin_headers, monkeypatch, path, limit):
    headers, key, _ = await setup_inference(client, admin_headers)
    async with db.transaction() as s:
        row = await s.get(db.APIKey, key["id"])
        row.rpm, row.tpm = (1, 100000) if limit == "rpm" else (1000, 1)
        if path == "account":
            row.permissions = ["inference", "account"]
    fixed = db.now().replace(second=10, microsecond=0)
    clock = AsyncMock(return_value=fixed)
    monkeypatch.setattr(db, "database_now", clock)
    previous = await identity(key["id"])
    await policy.reserve(previous, 1, 1, "chat")
    seen = []

    def upstream(request):
        seen.append(request)
        raise AssertionError("Rate-limited rotation must not dispatch")

    await mock_upstream(upstream)
    for _ in range(2):
        if path == "team":
            url = f'/teams/api/{previous.key.team_id}/keys/{previous.key.id}/rotate'
        else:
            url = f'/{path}/api/keys/{previous.key.id}/rotate'
        result = await client.post(url, headers=headers if path == "account" else admin_headers)
        assert result.status_code == 200, result.text
        replacement = result.json()
        headers = {"Authorization": "Bearer " + replacement["key"]}
        current = await identity(replacement["id"])
        assert current.key.expires_at == previous.key.expires_at
        with pytest.raises(HTTPException) as stale:
            await policy.reserve(previous, 1, 1, "chat")
        assert stale.value.status_code == 401
        with pytest.raises(HTTPException) as limited:
            await policy.reserve(current, 1, 1, "chat")
        assert limited.value.status_code == 429
        response = await client.post('/v1/chat/completions', headers=headers, json={
            "model": "chat", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 1})
        assert response.status_code == 429, response.text
        assert seen == []
        previous = current
    clock.return_value = fixed + timedelta(minutes=1)
    assert await policy.reserve(current, 1, 1, "chat")


async def test_rotation_after_boundary_does_not_copy_old_window(client, admin_headers, monkeypatch):
    headers, key, _ = await setup_inference(client, admin_headers, rpm=1)
    fixed = db.now().replace(second=59, microsecond=0)
    clock = AsyncMock(return_value=fixed)
    monkeypatch.setattr(db, "database_now", clock)
    await policy.reserve(await identity(key['id']), 1, 1, 'chat')
    clock.return_value = fixed + timedelta(seconds=1)
    result = await client.post(f'/admin/api/keys/{key["id"]}/rotate', headers=admin_headers)
    assert result.status_code == 200, result.text
    replacement = await identity(result.json()['id'])
    async with db.session_factory() as s:
        assert list((await s.scalars(select(db.RateBucket).where(
            db.RateBucket.id.startswith(replacement.key.id + ':')))).all()) == []
    assert await policy.reserve(replacement, 1, 1, 'chat')


async def test_allowance_copy_failure_rolls_back_rotation(client, admin_headers, monkeypatch):
    headers, key, _ = await setup_inference(client, admin_headers)
    async def fail(*args):
        raise HTTPException(503, "Test failure copying allowance")
    monkeypatch.setattr(policy, "inherit_rotation_allowance", fail)
    response = await client.post(f'/admin/api/keys/{key["id"]}/rotate', headers=admin_headers)
    assert response.status_code == 503
    async with db.session_factory() as s:
        previous = await s.get(db.APIKey, key["id"])
        assert not previous.revoked
        rows = list((await s.scalars(select(db.APIKey).where(db.APIKey.user_id == previous.user_id))).all())
        assert [row.id for row in rows] == [key["id"]]
    assert (await client.get('/v1/models', headers=headers)).status_code == 200
