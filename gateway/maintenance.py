"""Bounded operational cleanup. Never deletes usage, audit, keys or reservations."""
import argparse
import asyncio
import json
from datetime import timedelta

from sqlalchemy import delete, func, select, or_
from . import db


async def cleanup(*, apply=False, batch_size=1000):
    if not 1 <= batch_size <= 10000:
        raise ValueError("batch_size must be between 1 and 10000")
    cutoff = db.now()
    # Retain at least a day of rate buckets. Budget/accounting history is untouched.
    bucket_cutoff = (cutoff - timedelta(days=1)).strftime("%Y%m%d%H%M")
    targets = [
        (db.OIDCState, db.OIDCState.digest, db.OIDCState.expires_at < cutoff),
        (db.BrowserSession, db.BrowserSession.digest, db.BrowserSession.expires_at < cutoff),
        (db.RateBucket, db.RateBucket.id, func.substr(db.RateBucket.id, func.length(db.RateBucket.id) - 11) < bucket_cutoff),
    ]
    counts = {}
    async with db.transaction() as session:
        for model, key, predicate in targets:
            ids = list((await session.scalars(select(key).where(predicate).order_by(key).limit(batch_size))).all())
            counts[model.__tablename__] = len(ids)
            if apply and ids:
                await session.execute(delete(model).where(key.in_(ids)))
        factors = list((await session.scalars(select(db.LocalMFA).where(or_(
            db.LocalMFA.grant_expires < cutoff, db.LocalMFA.challenge_expires < cutoff
        )).order_by(db.LocalMFA.user_id).limit(batch_size))).all())
        if apply:
            from .security import aware
            for factor in factors:
                if factor.grant_expires and aware(factor.grant_expires) < cutoff:
                    factor.grant_digest, factor.grant_expires = None, None
                if factor.challenge_expires and aware(factor.challenge_expires) < cutoff:
                    factor.challenge_digest, factor.challenge_expires = None, None
                    factor.pending_seed, factor.password_binding = None, None
                    factor.attempts = 0
        if apply:
            await db.audit(session, "maintenance", "maintenance.cleanup", counts=counts)
    return {"applied": apply, "batch_size": batch_size, "eligible_in_batch": counts, "mfa_transient_rows": len(factors)}


async def run(args):
    db.init_engine()
    try:
        print(json.dumps(await cleanup(apply=args.apply, batch_size=args.batch_size)))
    finally:
        await db.engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Delete expired transient records; default is dry-run")
    parser.add_argument("--batch-size", type=int, default=1000)
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
