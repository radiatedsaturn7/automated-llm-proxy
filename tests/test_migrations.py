import asyncio
import os

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.ext.asyncio import create_async_engine

from gateway import db
from gateway.migrate import MigrationError, downgrade, migrate, versions, version_metadata
from gateway.migrations import v0001


@pytest.fixture
async def migration_engine(tmp_path, monkeypatch):
    url = os.environ.get("TEST_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/migration.db")
    kwargs = {"connect_args": {"timeout": 30}} if url.startswith("sqlite") else {}
    engine = create_async_engine(url, **kwargs)
    if os.environ.get("TEST_DATABASE_URL"):
        async with engine.begin() as conn:
            await conn.run_sync(v0001.Base.metadata.drop_all)
            await conn.run_sync(version_metadata.drop_all)
    monkeypatch.setattr(db, "engine", engine)
    yield engine
    await engine.dispose()


async def table_names(engine):
    async with engine.connect() as conn:
        return set(await conn.run_sync(lambda c: inspect(c).get_table_names()))


async def legacy(engine):
    async with engine.begin() as conn:
        await conn.run_sync(v0001.upgrade)


async def test_fresh_upgrade_idempotent_preserves_data(migration_engine):
    assert await migrate() == 1
    async with migration_engine.begin() as conn:
        await conn.execute(v0001.Team.__table__.insert().values(id="test", name="preserved", enabled=True))
    assert await migrate() == 1
    async with migration_engine.connect() as conn:
        assert (await conn.execute(select(versions.c.version))).scalars().all() == [1]
        assert (await conn.execute(text("SELECT name FROM teams"))).scalar() == "preserved"


async def test_concurrent_upgrades_serialized(migration_engine):
    assert await asyncio.gather(migrate(), migrate()) == [1, 1]


async def test_legacy_requires_explicit_adoption(migration_engine):
    await legacy(migration_engine)
    with pytest.raises(MigrationError, match="--adopt-v1"):
        await migrate()
    assert "gateway_schema_versions" not in await table_names(migration_engine)
    assert await migrate(adopt_v1=True) == 1
    assert await migrate() == 1


@pytest.mark.parametrize("mutation", ["ALTER TABLE teams ADD COLUMN unexpected TEXT", "DROP INDEX ix_audit_created_at",
                                      "DELETE FROM policy_lock"])
async def test_adoption_rejects_drift_without_stamping(migration_engine, mutation):
    await legacy(migration_engine)
    async with migration_engine.begin() as conn:
        await conn.execute(text(mutation))
    with pytest.raises(MigrationError):
        await migrate(adopt_v1=True)
    assert "gateway_schema_versions" not in await table_names(migration_engine)


async def test_unknown_newer_schema_refused(migration_engine):
    await migrate()
    async with migration_engine.begin() as conn:
        await conn.execute(text("UPDATE gateway_schema_versions SET version=999"))
    with pytest.raises(MigrationError, match="Unknown"):
        await migrate()


async def test_downgrade_refuses_without_changing_schema(migration_engine):
    await migrate()
    before = await table_names(migration_engine)
    with pytest.raises(MigrationError, match="downgrade is disabled"):
        downgrade(0)
    assert await table_names(migration_engine) == before


async def test_live_models_match_immutable_revision(migration_engine):
    # Protect against adding a live ORM field without a new migration.
    from gateway.migrate import validate_v1
    async with migration_engine.begin() as conn:
        await conn.run_sync(db.Base.metadata.create_all)
        await conn.execute(db.PolicyLock.__table__.insert().values(id=1))
        await conn.run_sync(validate_v1)


async def test_postgres_serial_defaults_validate_and_tampering_rejected(migration_engine):
    if migration_engine.dialect.name != "postgresql":
        pytest.skip("Set TEST_DATABASE_URL for PostgreSQL SERIAL regression")
    await legacy(migration_engine)
    assert await migrate(adopt_v1=True) == 1
    async with migration_engine.begin() as conn:
        await conn.execute(text("ALTER TABLE policy_lock ALTER COLUMN id SET DEFAULT 42"))
    with pytest.raises(MigrationError, match="Schema column differs"):
        await migrate()
