"""Explicit CI-only logical restore drill. Not a pytest test or production backup tool."""
import asyncio
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import uuid

from cryptography.fernet import Fernet
import httpx
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from gateway import db
from gateway.config import get_settings
from gateway.main import app
from gateway.migrate import migrate
from gateway.security import decrypt


def disposable_url():
    if os.environ.get('CI') != 'true' or os.environ.get('DISPOSABLE_RESTORE_DRILL') != '1':
        raise ValueError('This drill requires explicit disposable CI opt-in')
    url = make_url(os.environ.get('TEST_DATABASE_URL', ''))
    if (url.drivername != 'postgresql+asyncpg' or url.host not in {'localhost', '127.0.0.1'}
            or url.port != 5432 or url.database != 'gateway_test' or url.username != 'gateway'
            or not url.password or url.query):
        raise ValueError('Only the fixed loopback CI PostgreSQL test service is permitted')
    return url


def command(arguments, *, env, stdin=None, stdout=subprocess.PIPE):
    result = subprocess.run(arguments, env=env, stdin=stdin, stdout=stdout,
                            stderr=subprocess.PIPE, timeout=180, check=False)
    if result.returncode:
        # Never log driver diagnostics, credentials or SQL fixture contents.
        raise RuntimeError('Disposable database utility failed')
    return result.stdout


def preflight(env, success):
    result = subprocess.run([sys.executable, '-m', 'gateway.check'], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=45, check=False)
    report = json.loads(result.stdout)
    if success:
        assert result.returncode == 0 and report.get('ok') and report.get('providers_checked') == 1
    else:
        assert result.returncode == 1 and report.get('ok') is False
        assert report.get('checks', {}).get('schema') is True
        assert report.get('checks', {}).get('provider_credentials') is False


async def switch(url):
    if db.engine is not None:
        await db.engine.dispose()
    os.environ['DATABASE_URL'] = url.render_as_string(hide_password=False)
    get_settings.cache_clear()
    db.init_engine()


async def populate():
    await migrate()
    state = {}
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost:8080') as client:
            async def post(path, body, headers=None):
                result = await client.post(path, json=body, headers=headers)
                assert result.status_code in {200, 201}, 'Fixture creation failed'
                return result.json()
            admin = await post('/bootstrap', {'email': 'restore-admin@example.invalid', 'token': os.environ['BOOTSTRAP_TOKEN']})
            state['admin'] = {'Authorization': 'Bearer ' + admin['key']}
            team = await post('/admin/api/teams', {'name': 'restore-team', 'budget_usd': 10}, state['admin'])
            user = await post('/admin/api/users', {'email': 'restore-member@example.invalid', 'team_id': team['id'], 'budget_usd': 10}, state['admin'])
            provider = await post('/admin/api/providers', {'name': 'restore-provider', 'kind': 'openai',
                'base_url': 'https://provider.invalid/v1', 'api_key': 'disposable-provider-' + secrets.token_urlsafe(24),
                'models': {'chat': {'upstream_model': 'test-model', 'input_cost_per_million': 1, 'output_cost_per_million': 2}}}, state['admin'])
            key = await post('/admin/api/keys', {'name': 'restore-member', 'user_id': user['id'], 'allowed_models': ['chat']}, state['admin'])
            state.update(team=team['id'], user=user['id'], provider=provider['id'], key=key['id'],
                         member={'Authorization': 'Bearer ' + key['key']})
            prompt = await post('/admin/api/prompt-registry/templates', {'name': 'restore-prompt', 'content': 'Hello {{name}}'}, state['admin'])
            state['prompt'] = prompt['template']['id']
            await app.state.upstream.aclose()
            app.state.upstream = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200,
                json={'choices': [], 'usage': {'prompt_tokens': 10, 'completion_tokens': 5}})))
            await post('/v1/chat/completions', {'model': 'chat', 'messages': [{'role': 'user', 'content': 'fixture only'}]}, state['member'])
            async with db.transaction() as session:
                row = await session.get(db.Provider, state['provider'])
                state['ciphertext'], state['provider_digest'] = row.encrypted_key, decrypt(row.encrypted_key)
                session.add(db.Reservation(id='restore-pending', user_id=state['user'], team_id=state['team'],
                    key_id=state['key'], period=db.now().strftime('%Y-%m'), amount_micros=33, status='pending'))
                (await session.get(db.User, state['user'])).budget_micros = 0
    return state


async def verify(state):
    # No migrate call here: restored schema and data must stand on their own.
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost:8080') as client:
            assert (await client.get('/admin/api/users', headers=state['admin'])).status_code == 200
            assert (await client.get('/admin/api/users', headers=state['member'])).status_code == 403
            assert (await client.get('/v1/models', headers=state['member'])).status_code == 200
            prompt = await client.get('/admin/api/prompt-registry/templates/' + state['prompt'] + '/versions/1', headers=state['admin'])
            assert prompt.status_code == 200 and prompt.json()['content'] == 'Hello {{name}}'
            await app.state.upstream.aclose()
            def no_dispatch(request):
                raise AssertionError('Restored exhausted budget must not dispatch')
            app.state.upstream = httpx.AsyncClient(transport=httpx.MockTransport(no_dispatch))
            denied = await client.post('/v1/chat/completions', headers=state['member'],
                                      json={'model': 'chat', 'messages': [{'role': 'user', 'content': 'must not dispatch'}]})
            assert denied.status_code == 429
            async with db.session_factory() as session:
                provider = await session.get(db.Provider, state['provider'])
                assert provider.encrypted_key == state['ciphertext']
                assert decrypt(provider.encrypted_key) == state['provider_digest']
                assert (await session.get(db.Team, state['team'])).budget_micros == 10_000_000
                assert (await session.get(db.User, state['user'])).budget_micros == 0
                pending = await session.get(db.Reservation, 'restore-pending')
                assert pending.status == 'pending' and pending.amount_micros == 33
                usage = (await session.scalars(select(db.Usage))).one()
                assert usage.cost_micros == 20 and not usage.estimated
                assert (await session.get(db.APIKey, state['key'])).allowed_models == ['chat']


async def run():
    url = disposable_url()
    nonce = uuid.uuid4().hex
    names = [f'gateway_drill_{nonce}_source', f'gateway_drill_{nonce}_restored']
    control = create_async_engine(url.set(database='postgres'), isolation_level='AUTOCOMMIT')
    created = []
    os.environ.update(GATEWAY_MASTER_KEY=Fernet.generate_key().decode(),
        GATEWAY_SIGNING_KEY=secrets.token_urlsafe(48), BOOTSTRAP_TOKEN=secrets.token_urlsafe(48),
        ENVIRONMENT='development', ALLOW_PRIVATE_PROVIDER_HOSTS='true', PUBLIC_URL='http://localhost:8080',
        GUARDRAILS_ENABLED='false')
    try:
        async with control.connect() as conn:
            for name in names:
                await conn.execute(text(f'CREATE DATABASE "{name}"'))
                created.append(name)
        await switch(url.set(database=names[0]))
        state = await populate()
        pg_env = dict(os.environ, PGHOST='127.0.0.1', PGPORT='5432', PGUSER=url.username, PGPASSWORD=url.password)
        docker = ['docker', 'run', '--rm', '--network', 'host', '-i']
        for name in ('PGHOST', 'PGPORT', 'PGUSER', 'PGPASSWORD'):
            docker += ['--env', name]
        docker += ['postgres:16-alpine']
        with tempfile.TemporaryDirectory(prefix='thunderbolt-restore-') as directory:
            backup = Path(directory) / 'fixture.dump'
            with backup.open('xb') as out:
                backup.chmod(0o600)
                command(docker + ['pg_dump', '--format=custom', '--no-owner', '--no-acl', '--dbname', names[0]], env=pg_env, stdout=out)
            assert backup.stat().st_size > 0
            with backup.open('rb') as source:
                command(docker + ['pg_restore', '--exit-on-error', '--no-owner', '--no-acl', '--dbname', names[1]], env=pg_env, stdin=source)
        await switch(url.set(database=names[1]))
        preflight(dict(os.environ), True)
        preflight(dict(os.environ, GATEWAY_MASTER_KEY=Fernet.generate_key().decode()), False)
        await verify(state)
    finally:
        if db.engine is not None:
            await db.engine.dispose()
        async with control.connect() as conn:
            for name in reversed(created):
                await conn.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        await control.dispose()
    return {'ok': True, 'scope': 'Disposable PostgreSQL16 logical backup/restore, preflight, authorization and accounting'}


if __name__ == '__main__':
    try:
        print(json.dumps(asyncio.run(run())))
    except Exception:
        print(json.dumps({'ok': False, 'reason': 'Disposable restore acceptance drill failed; diagnostics suppressed'}))
        raise SystemExit(1) from None
