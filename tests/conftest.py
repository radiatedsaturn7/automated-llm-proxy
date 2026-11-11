import os
import pytest
import httpx
from cryptography.fernet import Fernet

from gateway import db
from gateway.config import get_settings
from gateway.main import app
from gateway.migrate import migrate


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", os.environ.get("TEST_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/test.db"))
    monkeypatch.setenv("GATEWAY_MASTER_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("GATEWAY_SIGNING_KEY", "test-signing-" + "x" * 40)
    monkeypatch.setenv("BOOTSTRAP_TOKEN", "bootstrap-" + "x" * 40)
    monkeypatch.setenv("METRICS_TOKEN", "metrics-" + "x" * 40)
    monkeypatch.setenv("PUBLIC_URL", "http://localhost:8080")
    monkeypatch.setenv("ALLOW_PRIVATE_PROVIDER_HOSTS", "true")
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.delenv("OIDC_ISSUER", raising=False)
    monkeypatch.delenv("OIDC_GROUP_MAPPING", raising=False)
    get_settings.cache_clear()
    if db.engine:
        await db.engine.dispose()
    db.init_engine()
    if os.environ.get("TEST_DATABASE_URL"):
        async with db.engine.begin() as connection:
            await connection.run_sync(db.Base.metadata.drop_all)
    await migrate()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost:8080") as c:
            yield c
    get_settings.cache_clear()


@pytest.fixture
async def admin_headers(client):
    r = await client.post("/bootstrap", json={"email": "admin@example.com", "token": "bootstrap-" + "x" * 40})
    assert r.status_code == 201, r.text
    return {"Authorization": "Bearer " + r.json()["key"]}
