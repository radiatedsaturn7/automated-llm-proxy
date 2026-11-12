import json

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from gateway import db
from gateway.config import Settings, get_settings
from gateway.encryption import fingerprint
from gateway.rotate_master import rotate
from gateway.security import encrypt


async def seed():
    async with db.transaction() as session:
        session.add(db.Provider(id="p", name="p", kind="openai", base_url="https://example.com", encrypted_key=encrypt("provider-secret"), models={}))


def configure(monkeypatch, primary, fallbacks):
    monkeypatch.setenv("GATEWAY_MASTER_KEY", primary)
    monkeypatch.setenv("GATEWAY_MASTER_DECRYPT_KEYS", json.dumps(fallbacks))
    get_settings.cache_clear()


async def snapshot():
    async with db.session_factory() as session:
        return (await session.scalar(select(db.Provider.encrypted_key)),
                await session.scalar(select(db.PolicyLock.master_key_fingerprint)))


async def test_dryrun_execute_fence_reverse(client, monkeypatch):
    await seed()
    old = get_settings().gateway_master_key
    target = Fernet.generate_key().decode()
    before = await snapshot()
    configure(monkeypatch, target, [old])
    result = await rotate(fingerprint(old))
    assert result["executed"] is False
    assert await snapshot() == before
    assert (await client.get("/health/ready")).status_code == 503
    with pytest.raises(SQLAlchemyError):
        async with db.transaction():
            pass
    await rotate(fingerprint(old), execute=True)
    after = await snapshot()
    assert Fernet(target.encode()).decrypt(after[0].encode()) == b"provider-secret"
    assert after[1] == fingerprint(target)
    assert (await client.get("/health/ready")).status_code == 200
    await rotate(fingerprint(target), execute=True)
    assert await snapshot() == after
    configure(monkeypatch, old, [target])
    await rotate(fingerprint(target), execute=True)
    assert Fernet(old.encode()).decrypt((await snapshot())[0].encode()) == b"provider-secret"


async def test_maxrows_rolls_back(client, monkeypatch):
    await seed()
    async with db.transaction() as session:
        session.add(db.Provider(id="z", name="z", base_url="https://example.com", encrypted_key=encrypt("second"), models={}))
    old = get_settings().gateway_master_key
    before = await snapshot()
    configure(monkeypatch, Fernet.generate_key().decode(), [old])
    with pytest.raises(ValueError, match="bound"):
        await rotate(fingerprint(old), execute=True, max_rows=1)
    assert await snapshot() == before


def test_effective_duplicates_rejected(client):
    key = get_settings().gateway_master_key
    with pytest.raises(ValueError, match="Invalid installation"):
        Settings(gateway_master_decrypt_keys=json.dumps([key + "\n"]))


async def test_deadline_rolls_back_partial_updates(client, monkeypatch):
    import asyncio
    from gateway import rotate_master
    await seed()
    old = get_settings().gateway_master_key
    before = await snapshot()
    configure(monkeypatch, Fernet.generate_key().decode(), [old])
    real_sleep = asyncio.sleep

    async def slow_checkpoint(_):
        await real_sleep(2)
    monkeypatch.setattr(rotate_master.asyncio, "sleep", slow_checkpoint)
    with pytest.raises(TimeoutError):
        await rotate(fingerprint(old), execute=True, timeout_seconds=1)
    assert await snapshot() == before


async def test_waiting_stale_writer_is_fenced_after_rotation(client, monkeypatch):
    import asyncio
    from gateway import encryption, rotate_master
    await seed()
    old = get_settings().gateway_master_key
    configure(monkeypatch, Fernet.generate_key().decode(), [old])
    in_rotation, release = asyncio.Event(), asyncio.Event()
    real_sleep = asyncio.sleep

    async def checkpoint(_):
        in_rotation.set()
        await release.wait()
    monkeypatch.setattr(rotate_master.asyncio, "sleep", checkpoint)
    # Simulate a separate stale replica's primary comparison, independent of
    # the operator process target primary. Only application gate uses matches().
    monkeypatch.setattr(encryption, "matches", lambda value: value == fingerprint(old))
    rotating = asyncio.create_task(rotate(fingerprint(old), execute=True))
    await in_rotation.wait()

    async def stale_writer():
        async with db.transaction() as session:
            session.add(db.Provider(id="stale", name="stale", base_url="https://example.com",
                                    encrypted_key=Fernet(old.encode()).encrypt(b"old-write").decode(), models={}))
    writing = asyncio.create_task(stale_writer())
    await real_sleep(0.05)
    assert not writing.done()
    release.set()
    await rotating
    with pytest.raises(SQLAlchemyError):
        await writing
    async with db.session_factory() as session:
        assert await session.get(db.Provider, "stale") is None


async def test_inspection_wrong_primary_is_readonly(client, monkeypatch):
    from gateway.rotate_master import inspect_encryption
    await seed()
    old = get_settings().gateway_master_key
    before = await snapshot()
    target = Fernet.generate_key().decode()
    configure(monkeypatch, target, [])  # cannot decrypt old ciphertext
    result = await inspect_encryption(timeout_seconds=5)
    assert result == {"ok": True, "inspection": True,
                      "database_fingerprint": fingerprint(old),
                      "configured_fingerprint": fingerprint(target), "primary_matches": False}
    assert await snapshot() == before
    configure(monkeypatch, old, [])
    assert (await inspect_encryption())["primary_matches"] is True
    assert await snapshot() == before


def test_inspection_cli_modes(monkeypatch, capsys):
    from gateway.rotate_master import main
    monkeypatch.setattr("sys.argv", ["rotate_master", "--inspect", "--execute"])
    with pytest.raises(SystemExit) as result:
        main()
    assert result.value.code == 2
    assert "not allowed" in capsys.readouterr().err
    monkeypatch.setattr("sys.argv", ["rotate_master", "--inspect", "--expected-current-fingerprint", "a" * 64])
    with pytest.raises(SystemExit) as result:
        main()
    assert result.value.code == 2
