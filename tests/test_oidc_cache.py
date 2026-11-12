import asyncio

import httpx
import pytest
from fastapi import HTTPException

from gateway import auth
from gateway.config import get_settings


@pytest.fixture
async def cache_config(client, monkeypatch):
    monkeypatch.setenv('OIDC_ISSUER', 'https://id.example')
    monkeypatch.setenv('OIDC_CLIENT_ID', 'gateway')
    monkeypatch.setenv('OIDC_CACHE_SECONDS', '10')
    get_settings.cache_clear()
    auth.clear_oidc_cache()


async def test_singleflight_cache_expiry_fails_closed(cache_config, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(auth.time, 'monotonic', lambda: clock[0])
    calls = []
    async def fetch(url, **kwargs):
        calls.append(url)
        return {'keys': [{'kid': str(len(calls))}]}
    monkeypatch.setattr(auth, 'oidc_json', fetch)
    url = 'https://id.example/keys'
    results = await asyncio.gather(*(auth.cached_oidc_json(url, kind='jwks') for _ in range(10)))
    assert len(calls) == 1
    assert all(r['keys'][0]['kid'] == '1' for r in results)
    clock[0] = 111
    assert (await auth.cached_oidc_json(url, kind='jwks'))['keys'][0]['kid'] == '2'
    async def fail(url, **kwargs):
        raise HTTPException(503, 'offline')
    monkeypatch.setattr(auth, 'oidc_json', fail)
    clock[0] = 122
    with pytest.raises(HTTPException) as exc:
        await auth.cached_oidc_json(url, kind='jwks')
    assert exc.value.status_code == 503


async def test_rotation_refresh_throttled(cache_config, monkeypatch):
    calls = []
    async def fetch(url, **kwargs):
        calls.append(url)
        return {'keys': [{'kid': str(len(calls))}]}
    monkeypatch.setattr(auth, 'oidc_json', fetch)
    url = 'https://id.example/keys'
    await auth.cached_oidc_json(url, kind='jwks')
    assert (await auth.cached_oidc_json(url, kind='jwks', force=True))['keys'][0]['kid'] == '2'
    await asyncio.gather(*(auth.cached_oidc_json(url, kind='jwks', force=True) for _ in range(10)))
    assert len(calls) == 2


@pytest.mark.parametrize('url', ['http://id.example/token', 'https://evil.example/token', 'https://id.example@evil.example/token', 'https://id.example:8443/token', 'https://id.example/token#fragment', 'https://127.0.0.1/token'])
async def test_untrusted_metadata_endpoint_rejected(cache_config, url):
    with pytest.raises(HTTPException):
        auth.validate_oidc_url(url)


async def test_explicit_cross_origin_trust(cache_config, monkeypatch):
    monkeypatch.setenv('OIDC_TRUSTED_ENDPOINT_ORIGINS', '["https://login.example"]')
    get_settings.cache_clear()
    assert auth.validate_oidc_url('https://login.example/token') == 'https://login.example/token'
    with pytest.raises(HTTPException):
        auth.validate_oidc_url('https://login.example.evil/token')


async def test_endpoint_redirect_size_and_json_fail_closed(cache_config, monkeypatch):
    original = httpx.AsyncClient
    for response in [httpx.Response(302, headers={'Location': 'https://evil.example'}), httpx.Response(200, content=b'x'*262145), httpx.Response(200, json=[]), httpx.Response(200, content=b'not-json')]:
        monkeypatch.setattr(auth.httpx, 'AsyncClient', lambda **kwargs: original(**{**kwargs, 'transport': httpx.MockTransport(lambda r: response)}))
        with pytest.raises(HTTPException) as exc:
            await auth.oidc_json('https://id.example/keys')
        assert exc.value.status_code == 503


async def test_compressed_metadata_rejected_before_read(cache_config, monkeypatch):
    class Unread(httpx.AsyncByteStream):
        reads = 0
        async def __aiter__(self):
            self.reads += 1
            yield b'not even a gzip stream'
    stream = Unread()
    original = httpx.AsyncClient
    def respond(request):
        assert request.headers['accept-encoding'] == 'identity'
        return httpx.Response(200, headers={'content-encoding': 'gzip'}, stream=stream)
    monkeypatch.setattr(auth.httpx, 'AsyncClient', lambda **kwargs: original(
        **{**kwargs, 'transport': httpx.MockTransport(respond)}))
    with pytest.raises(HTTPException) as exc:
        await auth.oidc_json('https://id.example/keys')
    assert exc.value.status_code == 503
    assert stream.reads == 0


async def test_trickling_metadata_has_absolute_deadline(cache_config, monkeypatch):
    class Trickle(httpx.AsyncByteStream):
        closed = False
        async def __aiter__(self):
            while True:
                await asyncio.sleep(.005)
                yield b' '
        async def aclose(self):
            self.closed = True
    stream = Trickle()
    original = httpx.AsyncClient
    monkeypatch.setattr(auth, 'OIDC_HTTP_DEADLINE_SECONDS', .025)
    monkeypatch.setattr(auth.httpx, 'AsyncClient', lambda **kwargs: original(
        **{**kwargs, 'transport': httpx.MockTransport(lambda r: httpx.Response(200, stream=stream))}))
    with pytest.raises(HTTPException) as exc:
        await asyncio.wait_for(auth.oidc_json('https://id.example/keys'), timeout=.5)
    assert exc.value.status_code == 503
    assert stream.closed


async def test_metadata_lock_wait_is_bounded(cache_config, monkeypatch):
    monkeypatch.setattr(auth, 'OIDC_HTTP_DEADLINE_SECONDS', .025)
    async with auth._oidc_cache_lock:
        with pytest.raises(HTTPException) as exc:
            await asyncio.wait_for(auth.cached_oidc_json('https://id.example/keys', kind='jwks'), timeout=.5)
    assert exc.value.status_code == 503
