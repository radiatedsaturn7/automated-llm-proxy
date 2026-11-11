import asyncio
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select

from gateway import db
from gateway.main import app
from gateway.security import digest


async def setup_inference(client, admin_headers, budget=1, rpm=60, kind="api", allowed=None):
    t = await client.post("/admin/api/teams", headers=admin_headers, json={"name": "engineering", "budget_usd": budget})
    assert t.status_code == 201, t.text
    u = await client.post("/admin/api/users", headers=admin_headers, json={"email": "dev@example.com", "team_id": t.json()["id"], "budget_usd": budget})
    p = await client.post("/admin/api/providers", headers=admin_headers, json={"name": "mock", "kind": "openai", "base_url": "https://provider.example/v1", "api_key": "provider-secret", "models": {"chat": {"upstream_model": "model-1", "input_cost_per_million": 1, "output_cost_per_million": 2}}})
    assert p.status_code == 201, p.text
    k = await client.post("/admin/api/keys", headers=admin_headers, json={"name": "app", "user_id": u.json()["id"], "allowed_models": allowed or [], "rpm": rpm, "kind": kind})
    assert k.status_code == 201, k.text
    return {"Authorization": "Bearer " + k.json()["key"]}, k.json(), p.json()


async def mock_upstream(handler):
    await app.state.upstream.aclose()
    app.state.upstream = httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_bootstrap_once_and_access(client, admin_headers):
    assert (await client.get("/admin/api/users")).status_code == 401
    assert (await client.post("/bootstrap", json={"email": "a@example.com", "token": "bootstrap-" + "x" * 40})).status_code == 409
    users = (await client.get("/admin/api/users", headers=admin_headers)).json()
    assert len(users) == 1
    assert "oidc_subject" not in users[0]


async def test_inference_accounting_and_secret_redaction(client, admin_headers):
    headers, _, _ = await setup_inference(client, admin_headers)
    seen = []
    def handler(req):
        seen.append(req)
        return httpx.Response(200, json={"id": "c1", "choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5}})
    await mock_upstream(handler)
    r = await client.post("/v1/chat/completions", headers=headers, json={"model": "chat", "messages": [{"role": "user", "content": "secret prompt"}], "max_tokens": 10})
    assert r.status_code == 200, r.text
    assert seen[0].headers["authorization"] == "Bearer provider-secret"
    usage = (await client.get("/admin/api/usage", headers=admin_headers)).json()
    assert usage[0]["cost_micros"] == 20
    assert not usage[0]["estimated"]
    providers = await client.get("/admin/api/providers", headers=admin_headers)
    audit = await client.get("/admin/api/audit", headers=admin_headers)
    assert "provider-secret" not in providers.text
    assert "secret prompt" not in audit.text
    assert (await client.get("/admin/api/users", headers=headers)).status_code == 403


async def test_budget_concurrent_admission(client, admin_headers):
    headers, _, _ = await setup_inference(client, admin_headers, budget="0.0013")
    entered = asyncio.Event()
    release = asyncio.Event()
    async def handler(req):
        entered.set()
        await release.wait()
        return httpx.Response(200, json={"choices": []})
    await mock_upstream(handler)
    body = {"model": "chat", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 100}
    task = asyncio.create_task(client.post("/v1/chat/completions", headers=headers, json=body))
    await asyncio.wait_for(entered.wait(), 5)
    other = await client.post("/v1/chat/completions", headers=headers, json=body)
    assert other.status_code == 429, other.text
    release.set()
    assert (await task).status_code == 200
    usage = (await client.get("/admin/api/usage", headers=admin_headers)).json()
    assert usage[0]["estimated"]


async def test_rate_limits_and_model_allowlist(client, admin_headers):
    headers, _, _ = await setup_inference(client, admin_headers, rpm=1, allowed=["chat"])
    await mock_upstream(lambda req: httpx.Response(200, json={"choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1}}))
    body = {"model": "chat", "messages": [{"role": "user", "content": "hi"}]}
    assert (await client.post("/v1/chat/completions", headers=headers, json=body)).status_code == 200
    assert (await client.post("/v1/chat/completions", headers=headers, json=body)).status_code == 429
    assert (await client.post("/v1/chat/completions", headers=headers, json={**body, "model": "other"})).status_code == 403
    assert (await client.get("/v1/models", headers=headers)).json()["data"][0]["id"] == "chat"


async def test_rotation_revokes_old_key_and_preserves_expiry(client, admin_headers):
    headers, key, _ = await setup_inference(client, admin_headers)
    rotated = await client.post(f"/admin/api/keys/{key['id']}/rotate", headers=admin_headers)
    assert rotated.status_code == 200
    from datetime import datetime
    from gateway.security import aware
    assert aware(datetime.fromisoformat(rotated.json()["expires_at"])) == aware(datetime.fromisoformat(key["expires_at"]))
    assert (await client.get("/v1/models", headers=headers)).status_code == 401
    new_headers = {"Authorization": "Bearer " + rotated.json()["key"]}
    assert (await client.get("/v1/models", headers=new_headers)).status_code == 200
    await client.post(f"/admin/api/keys/{rotated.json()['id']}/revoke", headers=admin_headers)
    assert (await client.get("/v1/models", headers=new_headers)).status_code == 401


async def test_agent_tokens_revoked_immediately(client, admin_headers):
    headers, key, _ = await setup_inference(client, admin_headers, kind="agent")
    assert (await client.get("/v1/models", headers=headers)).status_code == 401
    r = await client.post("/auth/agent/exchange", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["expires_in"] <= 300
    access = {"Authorization": "Bearer " + r.json()["access_token"]}
    assert (await client.get("/v1/models", headers=access)).status_code == 200
    assert (await client.get("/admin/api/keys", headers=access)).status_code == 403
    await client.post(f"/admin/api/keys/{key['id']}/revoke", headers=admin_headers)
    assert (await client.get("/v1/models", headers=access)).status_code == 401
    assert (await client.post("/auth/agent/exchange", headers=headers)).status_code == 401


async def test_upstream_4xx_release_and_5xx_conservative(client, admin_headers):
    headers, _, _ = await setup_inference(client, admin_headers)
    await mock_upstream(lambda req: httpx.Response(400, json={"error": "SECRET-should-not-leak"}))
    body = {"model": "chat", "messages": [{"role": "user", "content": "hi"}]}
    r = await client.post("/v1/chat/completions", headers=headers, json=body)
    assert r.status_code == 502 and "SECRET" not in r.text
    assert (await client.get("/admin/api/usage", headers=admin_headers)).json() == []
    await mock_upstream(lambda req: httpx.Response(500))
    assert (await client.post("/v1/chat/completions", headers=headers, json=body)).status_code == 502
    assert (await client.get("/admin/api/usage", headers=admin_headers)).json()[0]["estimated"]


async def test_streaming_usage_accounted(client, admin_headers):
    headers, _, _ = await setup_inference(client, admin_headers)
    await mock_upstream(lambda req: httpx.Response(200, text='data: {"choices":[{"delta":{"content":"hello"}}]}\n\ndata: {"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":2}}\n\ndata: [DONE]\n\n', headers={"content-type": "text/event-stream"}))
    r = await client.post("/v1/chat/completions", headers=headers, json={"model": "chat", "messages": [{"role": "user", "content": "hi"}], "stream": True})
    assert r.status_code == 200 and "[DONE]" in r.text
    assert (await client.get("/admin/api/usage", headers=admin_headers)).json()[0]["cost_micros"] == 7


async def test_request_limits_and_payload_validation(client, admin_headers):
    headers, _, _ = await setup_inference(client, admin_headers)
    assert (await client.post("/v1/chat/completions", headers=headers, content=b"x" * 1_048_577)).status_code == 413
    for body in [{}, {"model": "chat", "messages": []}, {"model": "chat", "messages": [{"role": "user", "content": [{"type": "image_url"}]}]}, {"model": "chat", "messages": [{"role": "user", "content": "hi"}], "max_tokens": -1}]:
        assert (await client.post("/v1/chat/completions", headers=headers, json=body)).status_code == 400
    assert (await client.get("/metrics")).status_code == 401
    assert (await client.get("/metrics", headers={"Authorization": "Bearer metrics-" + "x" * 40})).status_code == 200
    assert (await client.get("/health/ready")).status_code == 200


async def test_disabled_team_and_expired_key(client, admin_headers):
    headers, key, _ = await setup_inference(client, admin_headers)
    async with db.transaction() as s:
        k = await s.get(db.APIKey, key["id"])
        k.expires_at = db.now() - timedelta(seconds=1)
    assert (await client.get("/v1/models", headers=headers)).status_code == 401


async def test_session_csrf_and_evaluation(client, admin_headers):
    async with db.transaction() as s:
        user = await s.scalar(select(db.User).where(db.User.role == "admin"))
        s.add(db.BrowserSession(digest=digest("session"), user_id=user.id, csrf="csrf", expires_at=db.now() + timedelta(hours=1)))
    client.cookies.set("gateway_session", "session")
    assert (await client.get("/auth/session")).json()["csrf_token"] == "csrf"
    assert (await client.post("/admin/api/teams", json={"name": "CSRF"})).status_code == 403
    assert (await client.post("/admin/api/teams", headers={"X-CSRF-Token": "csrf", "Origin": "https://evil.example"}, json={"name": "CSRF"})).status_code == 403
    assert (await client.post("/admin/api/teams", headers={"X-CSRF-Token": "csrf"}, json={"name": "CSRF"})).status_code == 201
    r = await client.post("/admin/api/evaluate", headers=admin_headers, json={"cases": [{"output": "a", "expected": "a"}], "rules": ["exact_match"]})
    assert r.status_code == 200, r.text
    assert (await client.post("/auth/logout", headers={"X-CSRF-Token": "csrf"})).status_code == 303
    assert not (await client.get("/auth/session")).json()["authenticated"]
