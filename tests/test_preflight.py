from sqlalchemy import func, select
from gateway import db
from gateway.check import inspect_installation
from gateway.security import encrypt


async def test_preflight_readonly_and_no_secret_disclosure(client):
    async with db.transaction() as session:
        session.add(db.Provider(name='test',base_url='https://example.test',encrypted_key=encrypt('secret-provider-value'),models={}))
    result = await inspect_installation()
    assert result['ok'] and result['providers_checked'] == 1
    assert 'secret-provider-value' not in str(result)
    async with db.session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(db.Audit)) == 0
        assert await session.scalar(select(func.count()).select_from(db.Provider)) == 1


async def test_preflight_corrupt_ciphertext_fails_closed(client):
    async with db.transaction() as session:
        session.add(db.Provider(name='test',base_url='https://example.test',encrypted_key='secret-corrupt-ciphertext',models={}))
    result = await inspect_installation()
    assert result['ok'] is False
    assert result['checks']['schema'] is True
    assert result['checks']['provider_credentials'] is False
    assert 'secret-corrupt-ciphertext' not in str(result)
