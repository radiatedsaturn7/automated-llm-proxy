from datetime import timedelta
from sqlalchemy import select, func
from gateway import db
from gateway.maintenance import cleanup


async def test_cleanup_is_bounded_and_preserves_accounting(client, admin_headers):
    async with db.transaction() as session:
        user = await session.scalar(select(db.User))
        for n in range(3):
            session.add(db.OIDCState(digest=str(n), nonce="nonce", verifier="verifier", expires_at=db.now() - timedelta(hours=1)))
        session.add(db.BrowserSession(digest="old", user_id=user.id, csrf="x", expires_at=db.now()-timedelta(hours=1)))
        session.add(db.BrowserSession(digest="active", user_id=user.id, csrf="x", expires_at=db.now()+timedelta(hours=1)))
        session.add(db.RateBucket(id="key:202001010000", requests=1, tokens=1))
        session.add(db.RateBucket(id="key:"+db.now().strftime("%Y%m%d%H%M"), requests=1, tokens=1))
        session.add(db.Reservation(user_id=user.id, key_id="key", period="2020-01", amount_micros=10))
    preview = await cleanup(batch_size=2)
    assert not preview["applied"]
    assert preview["eligible_in_batch"] == {"oidc_states": 2, "browser_sessions": 1, "rate_buckets": 1}
    assert (await cleanup(apply=True, batch_size=2))["eligible_in_batch"] == preview["eligible_in_batch"]
    async with db.session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(db.OIDCState)) == 1
        assert await session.get(db.BrowserSession, "active")
        assert await session.scalar(select(func.count()).select_from(db.Reservation)) == 1
        assert await session.scalar(select(func.count()).select_from(db.APIKey)) == 1
    assert (await cleanup(apply=True))["eligible_in_batch"]["oidc_states"] == 1
