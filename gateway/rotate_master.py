"""Maintenance-only atomic encryption rotation; dry run unless --execute is given."""
import argparse
import asyncio
import hmac
import json
import re

from sqlalchemy import select, text, update

from . import db
from .encryption import cipher, fingerprint, validate_plaintext
from .migrate import HEAD, validate_head, versions


async def rotate(expected_current_fingerprint, *, execute=False, max_rows=100000, timeout_seconds=300):
    if (not isinstance(expected_current_fingerprint, str)
            or not re.fullmatch(r"[a-f0-9]{64}", expected_current_fingerprint)
            or type(max_rows) is not int or not 1 <= max_rows <= 1000000
            or not 1 <= timeout_seconds <= 3600):
        raise ValueError("Invalid rotation limits or expected fingerprint")
    if db.engine is None:
        db.init_engine()
    async with asyncio.timeout(timeout_seconds):
        async with db.engine.connect() as connection:
            async with connection.begin():
                if connection.dialect.name == "sqlite":
                    await connection.execute(text("BEGIN IMMEDIATE"))
                elif connection.dialect.name == "postgresql":
                    # Same migration lock order: schema advisory gate, then policy gate.
                    await connection.execute(text("SELECT pg_advisory_xact_lock(91827364)"))
                else:
                    raise ValueError("Unsupported database")
                applied = (await connection.execute(select(versions.c.version).order_by(versions.c.version))).scalars().all()
                if applied != list(range(1, HEAD + 1)):
                    raise ValueError("Schema requires migration")
                await connection.run_sync(validate_head)
                current = (await connection.execute(select(db.PolicyLock.master_key_fingerprint)
                           .where(db.PolicyLock.id == 1).with_for_update())).scalar()
                if not isinstance(current, str) or not hmac.compare_digest(current, expected_current_fingerprint):
                    raise ValueError("Expected encryption generation does not match")
                ring = cipher()
                count = fields_checked = 0
                for model, fields, mfa in ((db.Provider, ("encrypted_key",), False),
                                           (db.LocalMFA, ("seed", "pending_seed"), True)):
                    table = model.__table__
                    pk = list(table.primary_key.columns)[0]
                    # Bounded keyset pages, avoiding driver-dependent UPDATE with an open cursor.
                    after = None
                    while True:
                        query = select(pk, *(table.c[f] for f in fields)).order_by(pk).limit(256)
                        if after is not None:
                            query = query.where(pk > after)
                        rows = (await connection.execute(query)).all()
                        if not rows:
                            break
                        for row in rows:
                            count += 1
                            if count > max_rows:
                                raise ValueError("Rotation row bound exceeded")
                            changes = {}
                            for field, value in zip(fields, row[1:]):
                                if value is not None:
                                    validate_plaintext(ring.decrypt(value.encode()), mfa=mfa)
                                    fields_checked += 1
                                    # Same-primary execution verifies without churn; caller can explicitly
                                    # rotate mixed historical ciphertext by executing with target primary.
                                    from .encryption import primary
                                    try:
                                        primary().decrypt(value.encode())
                                    except Exception:
                                        changes[field] = ring.rotate(value.encode()).decode()
                            if execute and changes:
                                await connection.execute(update(table).where(pk == row[0]).values(**changes))
                            await asyncio.sleep(0)  # deadline/cancellation applies even to empty-seed pages
                        after = rows[-1][0]
                if execute:
                    await connection.execute(update(db.PolicyLock).where(db.PolicyLock.id == 1)
                                             .values(master_key_fingerprint=fingerprint()))
                return {"ok": True, "executed": execute, "rows_checked": count,
                        "encrypted_fields_checked": fields_checked,
                        "primary_fingerprint": fingerprint()}


async def inspect_encryption(*, timeout_seconds=300):
    """Observe generation without decrypting data or changing the database."""
    if not 1 <= timeout_seconds <= 3600:
        raise ValueError("Invalid inspection timeout")
    if db.engine is None:
        db.init_engine()
    async with asyncio.timeout(timeout_seconds):
        async with db.engine.connect() as connection:
            async with connection.begin():
                if connection.dialect.name == "postgresql":
                    await connection.execute(text("SET TRANSACTION READ ONLY"))
                elif connection.dialect.name != "sqlite":
                    raise ValueError("Unsupported database")
                applied = (await connection.execute(select(versions.c.version).order_by(versions.c.version)
                                                     .limit(HEAD + 1))).scalars().all()
                if applied != list(range(1, HEAD + 1)):
                    raise ValueError("Schema requires migration")
                await connection.run_sync(validate_head)
                observed = (await connection.execute(select(db.PolicyLock.master_key_fingerprint)
                                                     .where(db.PolicyLock.id == 1))).scalar()
                if not isinstance(observed, str) or not re.fullmatch(r"[a-f0-9]{64}", observed):
                    raise ValueError("Invalid stored encryption generation")
                configured = fingerprint()
                return {"ok": True, "inspection": True, "database_fingerprint": observed,
                        "configured_fingerprint": configured,
                        "primary_matches": hmac.compare_digest(observed, configured)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-current-fingerprint")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--execute", action="store_true")
    mode.add_argument("--inspect", action="store_true", help="Read schema and fingerprints only; does not verify ciphertext")
    parser.add_argument("--max-rows", type=int, default=100000)
    parser.add_argument("--timeout-seconds", type=float, default=300)
    args = parser.parse_args()
    if args.inspect and args.expected_current_fingerprint is not None:
        parser.error("--inspect does not accept --expected-current-fingerprint")
    if not args.inspect and args.expected_current_fingerprint is None:
        parser.error("--expected-current-fingerprint is required unless --inspect is used")

    async def run():
        try:
            if args.inspect:
                return await inspect_encryption(timeout_seconds=args.timeout_seconds)
            return await rotate(args.expected_current_fingerprint, execute=args.execute,
                                max_rows=args.max_rows, timeout_seconds=args.timeout_seconds)
        finally:
            if db.engine is not None:
                await db.engine.dispose()
    try:
        result = asyncio.run(run())
    except Exception:
        print(json.dumps({"ok": False, "reason": "Rotation completion not confirmed; verify database generation and data before retry"}))
        raise SystemExit(1) from None
    print(json.dumps(result))


if __name__ == "__main__":
    main()
