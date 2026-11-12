"""Regression probes for current authentication lifecycle boundaries."""
import pytest
from tests.test_oidc import oidc as oidc, start


async def test_signed_login_group_loss_invalidates_existing_session(client, oidc):
    state = await start(client, oidc)
    accepted = await client.get('/auth/oidc/callback', params={'state': state, 'code': 'first'})
    assert accepted.status_code == 303
    assert (await client.get('/auth/session')).json()['role'] == 'admin'

    # The same verified IdP subject explicitly returns no authorized group.
    # Reauthentication is the documented group-reconciliation boundary.
    oidc['claims'] = {'groups': []}
    state = await start(client, oidc)
    denied = await client.get('/auth/oidc/callback', params={'state': state, 'code': 'second'})
    assert denied.status_code == 403
    assert (await client.get('/auth/session')).json()['authenticated'] is False


@pytest.mark.parametrize("existing_user", [True, False])
async def test_queued_valid_callback_cannot_restore_authority_after_verified_denial(
    client, admin_headers, oidc, monkeypatch, existing_user,
):
    import asyncio
    from contextlib import asynccontextmanager
    from contextvars import ContextVar
    import time

    from sqlalchemy import select
    from gateway import db

    if existing_user:
        first = await start(client, oidc)
        assert (await client.get('/auth/oidc/callback', params={'state': first, 'code': 'initial'})).status_code == 303
    async with db.session_factory() as session:
        user_id = await session.scalar(select(db.User.id).where(db.User.oidc_subject == 'https://id.example|subject-1'))

    # Old token is already cryptographically verified before we let a newer
    # signed group denial commit. Distinct timestamps avoid second precision ambiguity.
    oidc['claims'] = {'iat': int(time.time()) - 10}
    old_state = await start(client, oidc)
    original = db.transaction
    paused, release = asyncio.Event(), asyncio.Event()
    calls = 0
    is_stale = ContextVar("stale_oidc_callback", default=False)

    @asynccontextmanager
    async def gated(*args, **kwargs):
        nonlocal calls
        if is_stale.get():
            calls += 1
            if calls == 2:  # State consumption first; authorization/session write second.
                paused.set()
                await release.wait()
        async with original(*args, **kwargs) as session:
            yield session

    monkeypatch.setattr(db, 'transaction', gated)
    async def stale_request():
        is_stale.set(True)
        return await client.get('/auth/oidc/callback', params={'state': old_state, 'code': 'stale'},
                                headers={'Cookie': 'gateway_oidc_state=' + old_state})
    stale_task = asyncio.create_task(stale_request())
    try:
        await asyncio.wait_for(paused.wait(), 5)
        oidc['claims'] = {'groups': []}
        new_state = await start(client, oidc)
        denied = await client.get('/auth/oidc/callback', params={'state': new_state, 'code': 'new-denial'})
        assert denied.status_code == 403
        # Even explicit local reactivation must not accept an older IdP decision.
        if existing_user:
            enabled = await client.patch('/admin/api/users/' + user_id, headers=admin_headers, json={'enabled': True})
            assert enabled.status_code == 200, enabled.text
    finally:
        release.set()
    stale = await asyncio.wait_for(stale_task, 5)
    assert stale.status_code in {401, 403}, stale.text
    async with db.session_factory() as session:
        if existing_user:
            sessions = (await session.scalars(select(db.BrowserSession).where(db.BrowserSession.user_id == user_id))).all()
            assert sessions == []
        else:
            assert await session.scalar(select(db.User.id).where(db.User.oidc_subject == 'https://id.example|subject-1')) is None
