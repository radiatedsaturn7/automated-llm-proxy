"""Safety gates and fixture assertions only; pytest never performs dump/restore."""
import importlib.util
from pathlib import Path

import pytest

from gateway.main import app

spec = importlib.util.spec_from_file_location('restore_drill', Path(__file__).parent / 'ops' / 'restore_drill.py')
drill = importlib.util.module_from_spec(spec)
spec.loader.exec_module(drill)


def test_requires_explicit_ci(monkeypatch):
    monkeypatch.delenv('CI', raising=False)
    monkeypatch.delenv('DISPOSABLE_RESTORE_DRILL', raising=False)
    with pytest.raises(ValueError):
        drill.disposable_url()


@pytest.mark.parametrize('url', [
    'postgresql+asyncpg://gateway:p@db.example:5432/gateway_test',
    'postgresql+asyncpg://gateway:p@localhost:5432/production',
    'postgresql+asyncpg://gateway:p@localhost:5432/gateway_test?ssl=require',
    'postgresql+asyncpg://admin:p@localhost:5432/gateway_test',
    'postgresql+asyncpg://gateway:p@localhost:5544/gateway_test',
    'sqlite+aiosqlite:///test.db',
])
def test_refuses_nonfixture_database(monkeypatch, url):
    monkeypatch.setenv('CI', 'true')
    monkeypatch.setenv('DISPOSABLE_RESTORE_DRILL', '1')
    monkeypatch.setenv('TEST_DATABASE_URL', url)
    with pytest.raises(ValueError):
        drill.disposable_url()


async def test_population_and_verification_assertions(client):
    # Reuse the ordinary isolated fixture; no subprocess, dump, CREATE/DROP DATABASE.
    await app.state.upstream.aclose()
    state = await drill.populate()
    await drill.verify(state)
