"""Pool capacity and PostgreSQL deadlines: never retry an uncertain write."""
import asyncio

import pytest
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, TimeoutError

from gateway import db
import gateway.migrate as migrations
from gateway.config import Settings, get_settings
from gateway.migrate import migrate


def test_bounds(client):
    values = get_settings().model_dump()
    for change in ({'db_pool_size': 0}, {'db_max_overflow': -1},
                   {'db_pool_timeout_seconds': 0}, {'db_statement_timeout_ms': 0},
                   {'db_lock_timeout_ms': 60000, 'db_statement_timeout_ms': 1000}):
        with pytest.raises(ValidationError):
            Settings(**(values | change))


def test_engine_configuration(client, monkeypatch):
    seen = {}
    original = db.engine, db.session_factory
    def fake(url, **kwargs):
        seen.update(kwargs)
        return original[0]
    monkeypatch.setattr(db, 'create_async_engine', fake)
    try:
        db.init_engine('postgresql+asyncpg://example/db')
        assert seen['pool_size'] == 10
        assert seen['max_overflow'] == 20
        assert seen['pool_timeout'] == 30
        assert seen['pool_recycle'] == 1800
        assert seen['connect_args']['server_settings'] == {'statement_timeout': '30000', 'lock_timeout': '5000'}
        seen.clear()
        db.init_engine('sqlite+aiosqlite:///:memory:')
        assert 'pool_size' not in seen
        assert seen['connect_args'] == {'timeout': 30}
    finally:
        db.engine, db.session_factory = original


async def test_postgres_deadlines_and_migration_restore(client, monkeypatch):
    if db.engine.dialect.name != 'postgresql':
        pytest.skip('PostgreSQL required')
    settings = get_settings()
    monkeypatch.setattr(settings, 'db_statement_timeout_ms', 200)
    monkeypatch.setattr(settings, 'db_lock_timeout_ms', 100)
    await db.engine.dispose()
    db.init_engine()
    async with db.engine.connect() as conn:
        with pytest.raises(DBAPIError):
            await conn.execute(text('SELECT pg_sleep(1)'))
        await conn.rollback()
        assert await conn.scalar(text('SHOW statement_timeout')) == '200ms'
    async with db.engine.begin() as holder:
        await holder.execute(text('SELECT * FROM policy_lock FOR UPDATE'))
        with pytest.raises(DBAPIError):
            async with db.transaction(inference=True):
                pass
    # Migration performs schema validation beyond a request-sized deadline. Its
    # independent local setting must not leak to pooled runtime connections.
    original_upgrade = migrations._upgrade
    def verify_migration_limits(connection, adopt):
        assert connection.scalar(text("SHOW statement_timeout")) == "0"
        assert connection.scalar(text("SHOW lock_timeout")) == "0"
        return original_upgrade(connection, adopt)
    monkeypatch.setattr(migrations, "_upgrade", verify_migration_limits)
    await migrate()
    async with db.engine.connect() as conn:
        assert await conn.scalar(text('SHOW statement_timeout')) == '200ms'
        assert await conn.scalar(text('SHOW lock_timeout')) == '100ms'


async def test_postgres_bounded_pool(client, monkeypatch):
    if db.engine.dialect.name != 'postgresql':
        pytest.skip('PostgreSQL required')
    settings = get_settings()
    monkeypatch.setattr(settings, 'db_pool_size', 1)
    monkeypatch.setattr(settings, 'db_max_overflow', 0)
    monkeypatch.setattr(settings, 'db_pool_timeout_seconds', .1)
    await db.engine.dispose()
    db.init_engine()
    async with db.engine.connect():
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(2):
                async with db.engine.connect():
                    pytest.fail('pool exceeded configured capacity')
    async with db.engine.connect() as conn:
        assert await conn.scalar(text('SELECT 1')) == 1
