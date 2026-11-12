"""Independent encryption-key lifecycle regression probes; no external secrets."""
import json

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import inspect, select
from sqlalchemy.exc import SQLAlchemyError

from gateway import db
from gateway.config import get_settings
from gateway.migrate import migrate, versions, version_metadata
from gateway.migrations import v0012
from tests.test_migrations import migration_engine as migration_engine


@pytest.mark.parametrize("field", ["provider", "seed", "pending_seed"])
async def test_primary_only_initialization_rejects_fallback_encrypted_last_row(migration_engine, monkeypatch, field):
    """Even a readable fallback cannot bless the wrong primary during v13 adoption."""
    old_key = get_settings().gateway_master_key
    new_key = Fernet.generate_key().decode()
    encrypted = Fernet(old_key.encode()).encrypt(b"JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP").decode()
    async with migration_engine.begin() as conn:
        await conn.run_sync(v0012.metadata.create_all)
        await conn.run_sync(version_metadata.create_all)
        await conn.execute(versions.insert(), [{"version": n, "applied_at": db.now()} for n in range(1, 13)])
        await conn.execute(v0012.metadata.tables["policy_lock"].insert().values(id=1))
        if field == "provider":
            await conn.execute(v0012.metadata.tables["providers"].insert().values(
                id="last", name="last", kind="openai", base_url="https://example.invalid", encrypted_key=encrypted,
                models={}, enabled=True, priority=0))
        else:
            await conn.execute(v0012.metadata.tables["users"].insert().values(
                id="user", email="user@example.com", role="member", enabled=True))
            await conn.execute(v0012.metadata.tables["local_mfa"].insert().values(
                user_id="user", last_counter=-1, attempts=0, **{field: encrypted}))
    monkeypatch.setenv("GATEWAY_MASTER_KEY", new_key)
    monkeypatch.setenv("GATEWAY_MASTER_DECRYPT_KEYS", json.dumps([old_key]))
    get_settings.cache_clear()
    with pytest.raises(Exception):
        await migrate()
    async with migration_engine.connect() as conn:
        assert max((await conn.execute(select(versions.c.version))).scalars()) == 12
        columns = await conn.run_sync(lambda c: inspect(c).get_columns("policy_lock"))
        assert "master_key_fingerprint" not in {c["name"] for c in columns}
    monkeypatch.setenv("GATEWAY_MASTER_KEY", old_key)
    monkeypatch.setenv("GATEWAY_MASTER_DECRYPT_KEYS", "[]")
    get_settings.cache_clear()
    assert await migrate() == 13


@pytest.mark.parametrize("inference", [False, True])
async def test_fallback_does_not_authorize_runtime_with_wrong_primary(client, monkeypatch, inference):
    old_key = get_settings().gateway_master_key
    monkeypatch.setenv("GATEWAY_MASTER_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("GATEWAY_MASTER_DECRYPT_KEYS", json.dumps([old_key]))
    get_settings.cache_clear()
    entered = False
    with pytest.raises(SQLAlchemyError, match="encryption key"):
        async with db.transaction(inference=inference):
            entered = True
    assert not entered


async def seed_rotation_rows():
    from gateway.security import encrypt
    async with db.transaction() as s:
        s.add(db.Provider(id="a-provider", name="before-final-factor", base_url="https://example.invalid",
                          encrypted_key=encrypt("test-only-upstream-secret"), models={}))
        s.add(db.User(id="z-user", email="factor@example.com", role="member"))
        await s.flush()
        s.add(db.LocalMFA(user_id="z-user", seed=encrypt("JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"),
                          pending_seed=encrypt("JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"), last_counter=-1, attempts=0))


async def ciphertext_snapshot():
    async with db.engine.connect() as c:
        return ((await c.execute(select(db.Provider.encrypted_key))).scalar_one(),
                tuple((await c.execute(select(db.LocalMFA.seed, db.LocalMFA.pending_seed))).one()),
                (await c.execute(select(db.PolicyLock.master_key_fingerprint))).scalar_one())


async def test_corrupt_final_ciphertext_rolls_back_prior_rotations(client, monkeypatch):
    from sqlalchemy import update
    from gateway.rotate_master import rotate
    from gateway.encryption import fingerprint
    from cryptography.fernet import InvalidToken
    await seed_rotation_rows()
    async with db.transaction() as s:
        await s.execute(update(db.LocalMFA).values(pending_seed="corrupt-last-field"))
    before = await ciphertext_snapshot()
    old = get_settings().gateway_master_key
    old_fp = fingerprint()
    monkeypatch.setenv("GATEWAY_MASTER_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("GATEWAY_MASTER_DECRYPT_KEYS", json.dumps([old]))
    get_settings.cache_clear()
    with pytest.raises(InvalidToken):
        await rotate(old_fp, execute=True)
    assert await ciphertext_snapshot() == before


async def test_successful_rotation_allows_fallback_retirement_and_fences_stale_runtime(client, monkeypatch):
    from gateway.rotate_master import rotate
    from gateway.encryption import fingerprint
    from gateway.security import decrypt
    await seed_rotation_rows()
    before = await ciphertext_snapshot()
    old = get_settings().gateway_master_key
    new = Fernet.generate_key().decode()
    old_fp = fingerprint()
    monkeypatch.setenv("GATEWAY_MASTER_KEY", new)
    monkeypatch.setenv("GATEWAY_MASTER_DECRYPT_KEYS", json.dumps([old]))
    get_settings.cache_clear()
    assert not (await rotate(old_fp))["executed"]
    assert await ciphertext_snapshot() == before
    assert (await rotate(old_fp, execute=True))["executed"]
    after = await ciphertext_snapshot()
    assert after[2] == fingerprint(new) != old_fp
    monkeypatch.setenv("GATEWAY_MASTER_DECRYPT_KEYS", "[]")
    get_settings.cache_clear()
    assert decrypt(after[0]) == "test-only-upstream-secret"
    for value in after[1]:
        assert decrypt(value) == "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"
    async with db.transaction():
        pass
    # An old process able to decrypt new ciphertext via fallback still cannot write.
    monkeypatch.setenv("GATEWAY_MASTER_KEY", old)
    monkeypatch.setenv("GATEWAY_MASTER_DECRYPT_KEYS", json.dumps([new]))
    get_settings.cache_clear()
    assert decrypt(after[0]) == "test-only-upstream-secret"
    with pytest.raises(SQLAlchemyError, match="encryption key"):
        async with db.transaction():
            pytest.fail("stale-primary writer admitted")


def test_rotation_cli_does_not_render_driver_secret_errors(monkeypatch, capsys):
    from gateway import rotate_master
    secret = "private-driver-error-DO-NOT-PRINT"

    async def fail(*args, **kwargs):
        raise RuntimeError(secret)

    monkeypatch.setattr(rotate_master, "rotate", fail)
    monkeypatch.setattr(db, "engine", None)
    monkeypatch.setattr("sys.argv", ["rotate-master", "--expected-current-fingerprint", "a" * 64])
    with pytest.raises(SystemExit) as exc:
        rotate_master.main()
    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert secret not in captured.out + captured.err
    assert json.loads(captured.out)["ok"] is False
