"""Back-channel logout validation and durable replay protection."""
import json
import time
from datetime import timedelta

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import select

from gateway import auth, db
from gateway.config import get_settings


@pytest.fixture
async def logout(client, monkeypatch):
    monkeypatch.setenv('OIDC_ISSUER', 'https://id.example')
    monkeypatch.setenv('OIDC_CLIENT_ID', 'gateway')
    get_settings.cache_clear()
    auth.clear_oidc_cache()
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update(kid='key', alg='RS256', use='sig')

    async def metadata(url, **kwargs):
        if url.endswith('/keys'):
            return {'keys': [jwk]}
        return {'issuer': 'https://id.example', 'authorization_endpoint': 'https://id.example/auth',
                'token_endpoint': 'https://id.example/token', 'jwks_uri': 'https://id.example/keys'}
    monkeypatch.setattr(auth, 'oidc_json', metadata)
    async with db.transaction() as s:
        user = db.User(email='a@example.com', role='member', enabled=True)
        s.add(user)
        await s.flush()
        for name, issuer, subject, sid in [('target', 'https://id.example', 'one', 'a'),
                                          ('other_session', 'https://id.example', 'one', 'b'),
                                          ('other_user', 'https://id.example', 'two', 'c'),
                                          ('other_issuer', 'https://other.example', 'one', 'a'),
                                          ('local', None, None, None)]:
            s.add(db.BrowserSession(digest=name, user_id=user.id, csrf='csrf', expires_at=db.now()+timedelta(hours=1),
                                    oidc_issuer=issuer, oidc_subject=subject, oidc_sid=sid))
    return key


def signed(key, **changes):
    claims = {'iss': 'https://id.example', 'aud': 'gateway', 'iat': int(time.time()), 'jti': 'event-1',
              'sid': 'a', 'sub': 'one', 'events': {'http://schemas.openid.net/event/backchannel-logout': {}}}
    claims.update(changes)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, key, algorithm='RS256', headers={'kid': 'key'})


async def sessions():
    async with db.transaction() as s:
        return set((await s.scalars(select(db.BrowserSession.digest))).all())


async def send(client, token):
    return await client.post('/auth/oidc/backchannel-logout', data={'logout_token': token})


async def test_logout_scoped_and_replay_durable(client, logout):
    token = signed(logout)
    response = await send(client, token)
    assert response.status_code == 200, response.text
    assert response.headers['cache-control'] == 'no-store'
    assert await sessions() == {'other_session', 'other_user', 'other_issuer', 'local'}
    async with db.transaction() as s:
        user = await s.scalar(select(db.User))
        s.add(db.BrowserSession(digest='new', user_id=user.id, csrf='csrf', expires_at=db.now()+timedelta(hours=1),
                               oidc_issuer='https://id.example', oidc_subject='one', oidc_sid='a'))
    auth.clear_oidc_cache()  # Replay protection is not process-local.
    assert (await send(client, token)).status_code == 200
    assert 'new' in await sessions()


@pytest.mark.parametrize('changes,remaining', [
    ({'sid': None}, {'other_user', 'other_issuer', 'local'}),
    ({'sub': None}, {'other_session', 'other_user', 'other_issuer', 'local'}),
    ({'sub': 'two'}, {'target', 'other_session', 'other_user', 'other_issuer', 'local'}),
])
async def test_subject_or_session_scope(client, logout, changes, remaining):
    assert (await send(client, signed(logout, **changes))).status_code == 200
    assert await sessions() == remaining


@pytest.mark.parametrize('changes', [
    {'iss': 'https://evil.example'}, {'aud': 'other'}, {'iat': int(time.time())-400},
    {'iat': int(time.time())+400}, {'iat': '123'}, {'iat': True}, {'iat': None},
    {'jti': None}, {'jti': ''}, {'jti': ['bad']}, {'nonce': 'id-token'},
    {'events': None}, {'events': {}}, {'events': {'http://schemas.openid.net/event/backchannel-logout': True}},
    {'sid': None, 'sub': None}, {'sid': ''}, {'sid': 42}, {'sub': []},
    {'exp': int(time.time())-1}, {'jti': 'a'*513},
])
async def test_invalid_tokens_never_revoke(client, logout, changes):
    before = await sessions()
    assert (await send(client, signed(logout, **changes))).status_code == 400
    assert await sessions() == before


async def test_signature_and_form_rejected(client, logout):
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    assert (await send(client, signed(other_key))).status_code == 400
    token = signed(logout)
    assert (await client.post('/auth/oidc/backchannel-logout', json={'logout_token': token})).status_code == 400
    assert (await client.post('/auth/oidc/backchannel-logout', content='logout_token='+token+'&logout_token='+token,
                             headers={'content-type': 'application/x-www-form-urlencoded'})).status_code == 400
    assert (await send(client, 'a'*33000)).status_code == 400
    assert len(await sessions()) == 5


async def test_concurrent_deliveries_are_idempotent(client, logout):
    import asyncio
    from sqlalchemy import func
    token = signed(logout)
    results = await asyncio.gather(send(client, token), send(client, token))
    assert [r.status_code for r in results] == [200, 200]
    async with db.transaction() as s:
        assert await s.scalar(select(func.count()).select_from(db.OIDCLogoutReplay)) == 1
        assert await s.scalar(select(func.count()).select_from(db.Audit).where(db.Audit.action == 'oidc.backchannel_logout')) == 1


async def test_logout_ages_out_while_waiting_for_writer_gate(client, logout, monkeypatch):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    original = db.transaction
    clock = {'offset': 0}
    monkeypatch.setattr(auth, 'time', SimpleNamespace(time=lambda: time.time()+clock['offset'], monotonic=time.monotonic))

    @asynccontextmanager
    async def delayed_gate(*args, **kwargs):
        async with original(*args, **kwargs) as session:
            clock['offset'] = 601
            yield session
    monkeypatch.setattr(db, 'transaction', delayed_gate)
    response = await send(client, signed(logout))
    assert response.status_code == 400, response.text
    assert len(await sessions()) == 5
