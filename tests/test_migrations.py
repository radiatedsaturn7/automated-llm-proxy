import asyncio
import os

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import inspect, select, text
from sqlalchemy.ext.asyncio import create_async_engine

from gateway import db
from gateway.config import get_settings
from gateway.migrate import MigrationError, downgrade, migrate, versions, version_metadata
from gateway.migrations import HEAD, v0001


@pytest.fixture
async def migration_engine(tmp_path, monkeypatch):
    monkeypatch.setenv("GATEWAY_MASTER_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("GATEWAY_SIGNING_KEY", "migration-test-" + "x" * 40)
    monkeypatch.setenv("BOOTSTRAP_TOKEN", "migration-test-" + "x" * 40)
    monkeypatch.setenv("ENVIRONMENT", "development")
    get_settings.cache_clear()
    url = os.environ.get("TEST_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/migration.db")
    kwargs = {"connect_args": {"timeout": 30}} if url.startswith("sqlite") else {}
    engine = create_async_engine(url, **kwargs)
    if os.environ.get("TEST_DATABASE_URL"):
        async with engine.begin() as conn:
            await conn.run_sync(db.Base.metadata.drop_all)
            await conn.run_sync(version_metadata.drop_all)
    monkeypatch.setattr(db, "engine", engine)
    yield engine
    await engine.dispose()
    get_settings.cache_clear()


async def table_names(engine):
    async with engine.connect() as conn:
        return set(await conn.run_sync(lambda c: inspect(c).get_table_names()))


async def legacy(engine):
    async with engine.begin() as conn:
        await conn.run_sync(v0001.upgrade)


async def test_fresh_upgrade_idempotent_preserves_data(migration_engine):
    assert await migrate() == HEAD
    async with migration_engine.begin() as conn:
        await conn.execute(v0001.Team.__table__.insert().values(id="test", name="preserved", enabled=True))
    assert await migrate() == HEAD
    async with migration_engine.connect() as conn:
        assert (await conn.execute(select(versions.c.version))).scalars().all() == list(range(1, HEAD + 1))
        assert (await conn.execute(text("SELECT name FROM teams"))).scalar() == "preserved"


async def test_concurrent_upgrades_serialized(migration_engine):
    assert await asyncio.gather(migrate(), migrate()) == [HEAD, HEAD]


async def test_legacy_requires_explicit_adoption(migration_engine):
    await legacy(migration_engine)
    with pytest.raises(MigrationError, match="--adopt-v1"):
        await migrate()
    assert "gateway_schema_versions" not in await table_names(migration_engine)
    assert await migrate(adopt_v1=True) == HEAD
    assert await migrate() == HEAD


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
        await conn.execute(text("UPDATE gateway_schema_versions SET version=999 WHERE version=2"))
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
    from gateway.migrate import validate_head
    async with migration_engine.begin() as conn:
        await conn.run_sync(db.Base.metadata.create_all)
        await conn.execute(db.PolicyLock.__table__.insert().values(id=1))
        await conn.run_sync(validate_head)


async def test_postgres_serial_defaults_validate_and_tampering_rejected(migration_engine):
    if migration_engine.dialect.name != "postgresql":
        pytest.skip("Set TEST_DATABASE_URL for PostgreSQL SERIAL regression")
    await legacy(migration_engine)
    assert await migrate(adopt_v1=True) == HEAD
    async with migration_engine.begin() as conn:
        await conn.execute(text("ALTER TABLE policy_lock ALTER COLUMN id SET DEFAULT 42"))
    with pytest.raises(MigrationError, match="Schema column differs"):
        await migrate()


async def test_versioned_v1_upgrade_preserves_rows_and_external_id_uniqueness(migration_engine):
    from datetime import datetime, timezone
    from sqlalchemy.exc import IntegrityError
    await legacy(migration_engine)
    async with migration_engine.begin() as conn:
        await conn.run_sync(version_metadata.create_all)
        await conn.execute(versions.insert().values(version=1, applied_at=datetime.now(timezone.utc)))
        await conn.execute(v0001.Team.__table__.insert().values(id="legacy", name="old", enabled=True))
        await conn.execute(v0001.User.__table__.insert().values(id="legacy-user", email="old@example.com",
                                                               team_id="legacy", role="member", enabled=True))
    assert await migrate() == HEAD
    async with migration_engine.begin() as conn:
        assert (await conn.execute(select(db.User.scim_external_id).where(db.User.id == "legacy-user"))).scalar() is None
        await conn.execute(db.User.__table__.update().where(db.User.id == "legacy-user").values(scim_external_id="idp-user"))
        # Multiple unprovisioned/null identifiers remain valid.
        await conn.execute(db.User.__table__.insert().values(id="new", email="new@example.com", role="member", enabled=True))
        await conn.execute(db.User.__table__.insert().values(id="new2", email="new2@example.com", role="member", enabled=True))
        # Resource namespaces are independent (group external IDs may match users).
        await conn.execute(db.Team.__table__.update().where(db.Team.id == "legacy").values(scim_external_id="idp-user"))
    with pytest.raises(IntegrityError):
        async with migration_engine.begin() as conn:
            await conn.execute(db.User.__table__.update().where(db.User.id == "new").values(scim_external_id="idp-user"))
    with pytest.raises(IntegrityError):
        async with migration_engine.begin() as conn:
            await conn.execute(db.Team.__table__.insert().values(id="duplicate", name="duplicate", enabled=True,
                                                                scim_external_id="idp-user"))
    assert await migrate() == HEAD


async def test_versioned_v1_drift_refuses_upgrade_before_altering(migration_engine):
    from datetime import datetime, timezone
    await legacy(migration_engine)
    async with migration_engine.begin() as conn:
        await conn.run_sync(version_metadata.create_all)
        await conn.execute(versions.insert().values(version=1, applied_at=datetime.now(timezone.utc)))
        await conn.execute(text("ALTER TABLE users ADD COLUMN unexpected TEXT"))
    with pytest.raises(MigrationError, match="columns differ"):
        await migrate()
    async with migration_engine.connect() as conn:
        columns = await conn.run_sync(lambda c: [col["name"] for col in inspect(c).get_columns("teams")])
        assert "scim_external_id" not in columns
        assert (await conn.execute(select(versions.c.version))).scalars().all() == [1]


async def test_versioned_v2_upgrade_preserves_scim_identifiers(migration_engine):
    from datetime import datetime, timezone
    from gateway.migrations import v0002
    async with migration_engine.begin() as conn:
        await conn.run_sync(v0001.upgrade)
        await conn.run_sync(v0002.upgrade)
        await conn.run_sync(version_metadata.create_all)
        for number in (1, 2):
            await conn.execute(versions.insert().values(version=number, applied_at=datetime.now(timezone.utc)))
        await conn.execute(v0002.metadata.tables["teams"].insert().values(
            id="preserved-scim", name="preserved", enabled=True, scim_external_id="idp-group"))
    assert await migrate() == HEAD
    async with migration_engine.connect() as conn:
        assert (await conn.execute(select(db.Team.scim_external_id))).scalar() == "idp-group"
        assert (await conn.execute(select(db.PromptTemplate.id))).scalars().all() == []


async def test_v4_backfills_legacy_authority_and_preserves_key_data(migration_engine):
    from datetime import datetime, timedelta, timezone
    from gateway.migrations import v0002, v0003
    async with migration_engine.begin() as conn:
        for revision in (v0001, v0002, v0003):
            await conn.run_sync(revision.upgrade)
        await conn.run_sync(version_metadata.create_all)
        for number in (1, 2, 3):
            await conn.execute(versions.insert().values(version=number, applied_at=datetime.now(timezone.utc)))
        users, keys = v0003.metadata.tables["users"], v0003.metadata.tables["api_keys"]
        for role in ("admin", "member"):
            await conn.execute(users.insert().values(id=role, email=role + "@example.com", role=role, enabled=True))
            for kind in ("api", "agent"):
                key_id = role + kind
                await conn.execute(keys.insert().values(id=key_id, name=key_id, prefix="old-prefix", digest=key_id,
                    user_id=role, kind=kind, expires_at=datetime.now(timezone.utc) + timedelta(days=5),
                    revoked=False, allowed_models=["restricted-model"], rpm=17, tpm=9876, created_at=datetime.now(timezone.utc)))
    assert await migrate() == HEAD
    async with migration_engine.connect() as conn:
        rows = (await conn.execute(select(db.APIKey.__table__))).mappings().all()
        for row in rows:
            expected = ["inference"] if row["kind"] == "agent" else (
                ["admin", "inference", "account", "provisioning"] if row["user_id"] == "admin" else ["inference", "account"])
            assert row["permissions"] == expected
            assert row["allowed_models"] == ["restricted-model"] and row["rpm"] == 17
            assert row["digest"] == row["id"] and row["prefix"] == "old-prefix"


async def test_v7_index_upgrade_preserves_accounting(migration_engine):
    from datetime import datetime, timezone
    from gateway.migrations import v0006
    async with migration_engine.begin() as conn:
        await conn.run_sync(v0006.metadata.create_all)
        await conn.execute(v0006.metadata.tables["policy_lock"].insert().values(id=1))
        await conn.run_sync(version_metadata.create_all)
        for number in range(1, 7):
            await conn.execute(versions.insert().values(version=number, applied_at=datetime.now(timezone.utc)))
        await conn.execute(v0006.metadata.tables["usage"].insert().values(
            id="usage-preserve", reservation_id="r-preserve", user_id="u", team_id="t", key_id="k",
            model="m", provider_id="p", period="2026-11", input_tokens=13, output_tokens=2,
            cost_micros=17, estimated=False, created_at=datetime.now(timezone.utc)))
        await conn.execute(v0006.metadata.tables["reservations"].insert().values(
            id="pending-preserve", user_id="u", team_id="t", key_id="k", period="2026-11",
            amount_micros=100, status="pending", created_at=datetime.now(timezone.utc)))
    assert await migrate() == HEAD
    async with migration_engine.connect() as conn:
        assert (await conn.execute(select(db.Usage.cost_micros))).scalars().all() == [17]
        assert (await conn.execute(select(db.Reservation.amount_micros))).scalars().all() == [100]
    assert await migrate() == HEAD


async def test_v8_preserves_legacy_primary_without_privilege_backfill(migration_engine):
    from datetime import datetime, timezone
    from gateway.migrations import v0007
    async with migration_engine.begin() as conn:
        await conn.run_sync(v0007.metadata.create_all)
        await conn.execute(v0007.metadata.tables['policy_lock'].insert().values(id=1))
        await conn.run_sync(version_metadata.create_all)
        for number in range(1, 8):
            await conn.execute(versions.insert().values(version=number, applied_at=datetime.now(timezone.utc)))
        await conn.execute(v0007.metadata.tables['teams'].insert().values(id='primary', name='old', enabled=True))
        await conn.execute(v0007.metadata.tables['users'].insert().values(
            id='legacy', email='old@example.com', team_id='primary', role='member', enabled=True))
    assert await migrate() == HEAD
    async with migration_engine.connect() as conn:
        assert (await conn.execute(select(db.User.team_id))).scalars().all() == ['primary']
        assert (await conn.execute(select(db.TeamMembership))).all() == []


async def test_v9_identity_upgrade_fails_closed(migration_engine):
    from gateway.migrations import REVISIONS, v0009
    async with migration_engine.begin() as conn:
        await conn.run_sync(version_metadata.create_all)
        for number, revision in REVISIONS:
            if number > 9:
                break
            await conn.run_sync(revision.upgrade)
            await conn.execute(versions.insert().values(version=number, applied_at=db.now()))
        await conn.execute(v0009.metadata.tables['users'].insert().values(
            id='legacy-user', email='legacy@example.com', role='member', enabled=True))
        await conn.execute(v0009.metadata.tables['browser_sessions'].insert().values(
            digest='a' * 64, user_id='legacy-user', csrf='test-csrf', expires_at=db.now()))
    assert await migrate() == HEAD
    async with migration_engine.connect() as conn:
        assert (await conn.execute(text("SELECT scim_managed FROM users WHERE id='legacy-user'"))).scalar()
        assert (await conn.execute(text('SELECT COUNT(*) FROM browser_sessions'))).scalar() == 0
        assert (await conn.execute(text('SELECT COUNT(*) FROM local_passwords'))).scalar() == 0
