"""Read-only preflight checks. Does not contact providers or identity services."""
import asyncio
import json
from cryptography.fernet import InvalidToken
from sqlalchemy import select, text
from . import db
from .config import get_settings
from .migrate import HEAD, validate_head, versions
from .security import decrypt


async def inspect_installation():
    checks = {"configuration": True, "schema": False, "provider_credentials": False, "mfa_seeds": False}
    async with db.engine.connect() as connection:
        async with connection.begin():
            if connection.dialect.name == "postgresql":
                await connection.execute(text("SET TRANSACTION READ ONLY"))
            applied = (await connection.execute(select(versions.c.version).order_by(versions.c.version))).scalars().all()
            if applied != list(range(1, HEAD + 1)):
                return {"ok": False, "checks": checks, "reason": "Schema version requires migration"}
            await connection.run_sync(validate_head)
            from .encryption import matches
            if not matches((await connection.execute(select(db.PolicyLock.master_key_fingerprint))).scalar()):
                return {"ok": False, "checks": checks, "reason": "Installation encryption key does not match database"}
            checks["schema"] = True
            count = 0
            async with connection.stream(select(db.Provider.encrypted_key)) as rows:
                async for row in rows:
                    try:
                        decrypt(row[0])
                    except (InvalidToken, UnicodeError, ValueError, TypeError):
                        return {"ok": False, "checks": checks, "reason": "Stored provider credential cannot be decrypted"}
                    count += 1
            checks["provider_credentials"] = True
            async with connection.stream(select(db.LocalMFA.seed, db.LocalMFA.pending_seed)) as rows:
                async for row in rows:
                    for value in row:
                        if value is not None:
                            try:
                                import base64
                                if len(base64.b32decode(decrypt(value))) != 20:
                                    raise ValueError("Invalid seed")
                            except (InvalidToken, UnicodeError, ValueError, TypeError):
                                return {"ok": False, "checks": checks, "reason": "Stored MFA seed cannot be decrypted or is invalid"}
            checks["mfa_seeds"] = True
    return {"ok": True, "checks": checks, "providers_checked": count, "schema_revision": HEAD,
            "scope": "Local configuration, database schema and credential decryption only; no upstream or SSO validation"}


async def run():
    try:
        get_settings()
        db.init_engine()
        return await asyncio.wait_for(inspect_installation(), timeout=30)
    except Exception:
        # Driver/config exceptions can embed passwords, URLs and input secrets.
        return {"ok": False, "reason": "Preflight failed; check configuration, database availability and schema"}
    finally:
        if db.engine is not None:
            await db.engine.dispose()


def main():
    result = asyncio.run(run())
    print(json.dumps(result))
    raise SystemExit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
