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
            return httpx.Response(200, json={"keys": [jwk]})
        if request.url.path == "/token":
            form = parse_qs(request.content.decode())
            assert form["client_secret"] == ["idp-secret"]
            assert len(form["code_verifier"][0]) >= 43
            claims = {"iss": "https://id.example", "aud": "gateway", "iat": int(time.time()),
                      "exp": int(time.time()) + 300, "sub": "subject-1", "nonce": control["nonce"],
                      "email": control["email"], "email_verified": True, "groups": ["operators"]}
            claims.update(control.get("claims", {}))
            return httpx.Response(200, json={"id_token": jwt.encode(claims, key, algorithm="RS256", headers={"kid": "signing-key"})})
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
