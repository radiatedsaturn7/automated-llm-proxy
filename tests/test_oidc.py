"""OIDC flow integration with locally signed ID tokens and mocked IdP transport."""
import json
import time
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from gateway import auth
from gateway.config import get_settings


@pytest.fixture
async def oidc(client, monkeypatch):
    monkeypatch.setenv("OIDC_ISSUER", "https://id.example")
    monkeypatch.setenv("OIDC_CLIENT_ID", "gateway")
    monkeypatch.setenv("OIDC_CLIENT_SECRET", "idp-secret")
    monkeypatch.setenv("OIDC_GROUP_MAPPING", '{"operators":{"role":"admin"}}')
    get_settings.cache_clear()
    auth.clear_oidc_cache()
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update(kid="signing-key", use="sig", alg="RS256")
    control = {"nonce": "", "email": "new@example.com", "issuer": "https://id.example"}
    original_client = httpx.AsyncClient

    def handler(request):
        if request.url.path == "/.well-known/openid-configuration":
            return httpx.Response(200, json={"issuer": control["issuer"],
                "authorization_endpoint": "https://id.example/authorize", "token_endpoint": "https://id.example/token",
                "jwks_uri": "https://id.example/keys"})
        if request.url.path == "/keys":
            return httpx.Response(200, json={"keys": control.get("jwks", [jwk])})
        if request.url.path == "/token":
            form = parse_qs(request.content.decode())
            assert form["client_secret"] == ["idp-secret"]
            assert len(form["code_verifier"][0]) >= 43
            claims = {"iss": "https://id.example", "aud": "gateway", "iat": int(time.time()),
                      "exp": int(time.time()) + 300, "sub": "subject-1", "nonce": control["nonce"],
                      "email": control["email"], "email_verified": True, "groups": ["operators"]}
            claims.update(control.get("claims", {}))
            return httpx.Response(200, json={"id_token": jwt.encode(claims, control.get("signing_key", key), algorithm="RS256", headers={"kid": control.get("kid", "signing-key")})})
        pytest.fail(f"Unexpected IdP path {request.url.path}")

    monkeypatch.setattr(auth.httpx, "AsyncClient", lambda **kwargs: original_client(
        **{**kwargs, "transport": httpx.MockTransport(handler)}))
    return control


async def start(client, oidc):
    response = await client.get("/auth/oidc/start")
    assert response.status_code == 307
    params = parse_qs(urlparse(response.headers["location"]).query)
    assert params["code_challenge_method"] == ["S256"]
    oidc["nonce"] = params["nonce"][0]
    return params["state"][0]


async def test_signed_oidc_new_user_session_and_state_replay(client, oidc):
    state = await start(client, oidc)
    response = await client.get("/auth/oidc/callback", params={"state": state, "code": "code"})
    assert response.status_code == 303, response.text
    session = (await client.get("/auth/session")).json()
    assert session["authenticated"] and session["role"] == "admin"
    assert session["email"] == "new@example.com" and session["csrf_token"]
    # Even restoring the state cookie cannot redeem the consumed state.
    client.cookies.set("gateway_oidc_state", state, path="/auth/oidc")
    assert (await client.get("/auth/oidc/callback", params={"state": state, "code": "code"})).status_code == 400


async def test_oidc_existing_email_never_auto_links(client, admin_headers, oidc):
    oidc["email"] = "admin@example.com"
    state = await start(client, oidc)
    response = await client.get("/auth/oidc/callback", params={"state": state, "code": "code"})
    assert response.status_code == 409
    assert not (await client.get("/auth/session")).json()["authenticated"]


@pytest.mark.parametrize("claims", [{"nonce": "wrong"}, {"aud": "other-client"}, {"iss": "https://evil.example"},
                                    {"email_verified": False}, {"groups": []}])
async def test_oidc_invalid_identity_rejected(client, oidc, claims):
    state = await start(client, oidc)
    oidc["claims"] = claims
    response = await client.get("/auth/oidc/callback", params={"state": state, "code": "code"})
    assert response.status_code in {401, 403}
    assert not (await client.get("/auth/session")).json()["authenticated"]
    # Failed exchanges also consume state.
    oidc["claims"] = {}
    assert (await client.get("/auth/oidc/callback", params={"state": state, "code": "code"})).status_code == 400


async def test_oidc_discovery_issuer_mismatch(client, oidc):
    oidc["issuer"] = "https://evil.example"
    assert (await client.get("/auth/oidc/start")).status_code == 503


async def test_oidc_state_requires_matching_browser_cookie(client, oidc):
    state = await start(client, oidc)
    client.cookies.clear()
    assert (await client.get("/auth/oidc/callback", params={"state": state, "code": "code"})).status_code == 400


async def test_nested_group_claim_and_bounded_session(client, oidc, monkeypatch):
    from datetime import timedelta
    from sqlalchemy import select
    from gateway import db
    from gateway.security import aware
    monkeypatch.setenv('OIDC_GROUP_CLAIM', 'realm_access.roles')
    monkeypatch.setenv('OIDC_SESSION_SECONDS', '300')
    get_settings.cache_clear()
    oidc['claims'] = {'groups': [], 'realm_access': {'roles': ['operators']}}
    state = await start(client, oidc)
    response = await client.get('/auth/oidc/callback', params={'state': state, 'code': 'code'})
    assert response.status_code == 303, response.text
    assert 'Max-Age=300' in response.headers['set-cookie']
    async with db.transaction() as s:
        session = await s.scalar(select(db.BrowserSession))
        assert 295 <= (aware(session.expires_at) - db.now()).total_seconds() <= 300
        session.expires_at = db.now() - timedelta(seconds=1)
    assert not (await client.get('/auth/session')).json()['authenticated']


async def test_nested_group_nonobject_rejected(client, oidc, monkeypatch):
    monkeypatch.setenv('OIDC_GROUP_CLAIM', 'realm_access.roles')
    get_settings.cache_clear()
    oidc['claims'] = {'realm_access': ['operators']}
    state = await start(client, oidc)
    assert (await client.get('/auth/oidc/callback', params={'state': state, 'code': 'code'})).status_code == 403


async def test_new_signing_kid_refreshes_cached_jwks(client, oidc):
    state = await start(client, oidc)
    assert (await client.get('/auth/oidc/callback', params={'state': state, 'code': 'first'})).status_code == 303
    rotated = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(rotated.public_key()))
    jwk.update(kid='rotated-key', use='sig', alg='RS256')
    oidc.update(signing_key=rotated, kid='rotated-key', jwks=[jwk])
    state = await start(client, oidc)
    response = await client.get('/auth/oidc/callback', params={'state': state, 'code': 'second'})
    assert response.status_code == 303, response.text


@pytest.mark.parametrize('logout_sid,expected', [('idp-session', 401), (None, 401), ('different-session', 303)])
async def test_logout_during_callback_does_not_resurrect_session(client, oidc, monkeypatch, logout_sid, expected):
    from datetime import timedelta
    from gateway import db
    original = auth.oidc_json
    oidc['claims'] = {'sid': 'idp-session'}

    async def exchange_with_logout(url, **kwargs):
        result = await original(url, **kwargs)
        if url.endswith('/token'):
            claims = jwt.decode(result['id_token'], options={'verify_signature': False})
            async with db.transaction() as s:
                s.add(db.OIDCLogoutReplay(digest='logout', issuer='https://id.example', subject='subject-1',
                                         sid=logout_sid, issued_at=claims['iat'],
                                         expires_at=db.now()+timedelta(seconds=600)))
        return result
    monkeypatch.setattr(auth, 'oidc_json', exchange_with_logout)
    state = await start(client, oidc)
    response = await client.get('/auth/oidc/callback', params={'state': state, 'code': 'code'})
    assert response.status_code == expected, response.text
    assert (await client.get('/auth/session')).json()['authenticated'] == (expected == 303)


async def test_oidc_token_ages_out_while_waiting_for_writer_gate(client, oidc, monkeypatch):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from gateway import db
    original = db.transaction
    clock = {'offset': 0}
    monkeypatch.setattr(auth, 'time', SimpleNamespace(time=lambda: time.time()+clock['offset'], monotonic=time.monotonic))
    state = await start(client, oidc)
    calls = 0

    @asynccontextmanager
    async def delayed_gate(*args, **kwargs):
        nonlocal calls
        calls += 1
        async with original(*args, **kwargs) as session:
            if calls == 2:  # First consumes state; second creates session.
                clock['offset'] = 601
            yield session
    monkeypatch.setattr(db, 'transaction', delayed_gate)
    response = await client.get('/auth/oidc/callback', params={'state': state, 'code': 'code'})
    assert response.status_code == 401, response.text
    assert not (await client.get('/auth/session')).json()['authenticated']
