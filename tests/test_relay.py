import asyncio

import httpx
import pytest

from gateway.relay import RelaySettings, TokenBroker, create_relay_app


def settings(**kwargs):
    return RelaySettings(central_url=kwargs.pop("central_url", "https://gateway.example"), credential="c" * 32, local_token="l" * 32, **kwargs)


def run(coro):
    return asyncio.run(coro)


@pytest.mark.parametrize("url", ["http://example.com", "https://user:pass@example.com", "https://example.com?key=secret", "file:///tmp/test", "https://example.com#token"])
def test_invalid_central_urls(url):
    with pytest.raises(ValueError):
        settings(central_url=url)


def test_bind_requires_explicit_opt_in():
    with pytest.raises(ValueError):
        settings(bind="0.0.0.0")
    settings(bind="0.0.0.0", allow_remote_bind=True)
    settings(central_url="http://127.0.0.1:8080")


def test_local_token_and_allowlist():
    async def check():
        calls = []
        async def upstream(request):
            calls.append(request)
            return httpx.Response(500)
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as central:
            app = create_relay_app(settings(), central)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://relay") as local:
                assert (await local.get("/health/live")).status_code == 200
                assert (await local.get("/v1/models")).status_code == 401
                assert (await local.get("/admin")).status_code == 404
                assert (await local.get("/v1/models?x=1", headers={"Authorization": "Bearer " + "l" * 32})).status_code == 400
                assert not calls
    run(check())


def test_concurrent_refresh_is_single_flight():
    async def check():
        calls = 0
        async def upstream(request):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.01)
            return httpx.Response(200, json={"access_token": "short-lived-token", "expires_in": 300, "token_type": "Bearer"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as central:
            broker = TokenBroker(settings(), central)
            assert await asyncio.gather(*(broker.token() for _ in range(20))) == ["short-lived-token"] * 20
            assert calls == 1
            assert await asyncio.gather(*(broker.token(rejected_token="short-lived-token") for _ in range(1))) == ["short-lived-token"]
            assert calls == 2
    run(check())


def test_auth_refresh_once_and_stream_headers():
    async def check():
        exchanges = 0
        inference = 0
        async def upstream(request):
            nonlocal exchanges, inference
            if request.url.path == "/auth/agent/exchange":
                assert request.headers["authorization"] == "Bearer " + "c" * 32
                exchanges += 1
                return httpx.Response(200, json={"access_token": f"access-{exchanges}", "expires_in": 300})
            inference += 1
            assert request.headers["authorization"] == f"Bearer access-{inference}"
            assert "x-dangerous" not in request.headers
            if inference == 1:
                return httpx.Response(401)
            return httpx.Response(200, content=b'data: {"ok":true}\n\ndata: [DONE]\n\n', headers={"Content-Type": "text/event-stream", "Set-Cookie": "secret=yes", "X-Request-Id": "test-id"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as central:
            app = create_relay_app(settings(), central)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://relay") as local:
                response = await local.post("/v1/chat/completions", json={"model": "example", "stream": True}, headers={"Authorization": "Bearer " + "l" * 32, "X-Dangerous": "secret"})
                assert response.status_code == 200
                assert "[DONE]" in response.text
                assert "set-cookie" not in response.headers
                assert response.headers["x-request-id"] == "test-id"
                assert exchanges == inference == 2
    run(check())


def test_exchange_failure_does_not_use_stale_token():
    async def check():
        from fastapi import HTTPException
        async def upstream(request):
            return httpx.Response(401, json={"secret": "must-not-be-returned"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as central:
            broker = TokenBroker(settings(), central)
            broker._token = "old"
            broker._refresh_at = 0
            with pytest.raises(HTTPException) as exc:
                await broker.token()
            assert exc.value.status_code == 503
            assert broker._token is None
    run(check())


def test_limit_and_type_reject_before_upstream():
    async def check():
        async def upstream(request):
            raise AssertionError("must not call upstream")
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as central:
            app = create_relay_app(settings(max_body_bytes=10), central)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://relay") as local:
                headers = {"Authorization": "Bearer " + "l" * 32}
                assert (await local.post("/v1/embeddings", content="x" * 11, headers=headers)).status_code == 413
                assert (await local.post("/v1/embeddings", content="x", headers=headers)).status_code == 415
    run(check())


def test_5xx_is_not_replayed():
    async def check():
        inference = 0
        async def upstream(request):
            nonlocal inference
            if request.url.path == "/auth/agent/exchange":
                return httpx.Response(200, json={"access_token": "access", "expires_in": 300})
            inference += 1
            return httpx.Response(503, json={"error": "unavailable"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as central:
            app = create_relay_app(settings(), central)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://relay") as local:
                response = await local.post("/v1/embeddings", json={"model": "test"}, headers={"Authorization": "Bearer " + "l" * 32})
                assert response.status_code == 503
                assert inference == 1
    run(check())


def test_responses_route_uses_local_auth_and_short_lived_token():
    async def check():
        async def upstream(request):
            if request.url.path == '/auth/agent/exchange':
                return httpx.Response(200, json={'access_token': 'ephemeral', 'expires_in': 300})
            assert request.url.path == '/v1/responses'
            assert request.headers['authorization'] == 'Bearer ephemeral'
            return httpx.Response(200, json={'object': 'response', 'output': []})
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as central:
            app = create_relay_app(settings(), central)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://relay') as local:
                assert (await local.post('/v1/responses', json={})).status_code == 401
                response = await local.post('/v1/responses', json={'model':'example', 'input':'hello'},
                                            headers={'Authorization':'Bearer '+'l'*32})
                assert response.status_code == 200
    run(check())
