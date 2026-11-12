"""Real CLI/Uvicorn SIGTERM acceptance with disposable SQLite and mocked providers.

Only the browser/client side uses real TCP. The upstream transport is deliberately
mocked inside the child; no provider endpoint, account or real secret is accessed.
"""
import concurrent.futures
from contextlib import contextmanager
import os
from pathlib import Path
import signal
import socket
import sqlite3
import subprocess
import sys
import time

from cryptography.fernet import Fernet
import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
HARNESS = r'''
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import runpy
import httpx
from gateway.main import app

root = Path(os.environ['DRAIN_TEST_DIR'])
original = app.router.lifespan_context

class HeldStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'
        (root / 'stream-started').touch()
        while not (root / 'release').exists():
            await asyncio.sleep(.02)
        yield b'data: {"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":2}}\n\n'
        yield b'data: [DONE]\n\n'
    async def aclose(self):
        (root / 'upstream-closed').touch()

async def handler(request):
    with (root / 'dispatches').open('a') as output:
        output.write('dispatch\n')
    if json.loads(request.content).get('stream'):
        return httpx.Response(200, stream=HeldStream(), headers={'content-type': 'text/event-stream'})
    while not (root / 'release').exists():
        await asyncio.sleep(.02)
    return httpx.Response(200, json={'choices': [{'message': {'role': 'assistant', 'content': 'hello'},
        'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 3, 'completion_tokens': 2}})

@asynccontextmanager
async def mocked_lifespan(instance):
    async with original(instance):
        await instance.state.upstream.aclose()
        instance.state.upstream = httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False)
        yield
    (root / 'lifespan-completed').touch()

app.router.lifespan_context = mocked_lifespan
runpy.run_module('gateway.cli', run_name='__main__')
'''


def wait_until(predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.02)
    raise AssertionError('Disposable gateway condition did not complete within deadline')


@contextmanager
def gateway_process(tmp_path, grace):
    if os.name != 'posix':
        pytest.skip('SIGTERM process acceptance requires POSIX')
    with socket.socket() as reserved:
        reserved.bind(('127.0.0.1', 0))
        port = reserved.getsockname()[1]
    database = tmp_path / 'gateway.sqlite'
    env = {
        'PATH': os.environ.get('PATH', ''), 'PYTHONPATH': str(ROOT),
        'DRAIN_TEST_DIR': str(tmp_path), 'PYTHONUNBUFFERED': '1',
        'DATABASE_URL': f'sqlite+aiosqlite:///{database}',
        'ENVIRONMENT': 'development', 'PUBLIC_URL': f'http://127.0.0.1:{port}',
        'GATEWAY_MASTER_KEY': Fernet.generate_key().decode(),
        'GATEWAY_SIGNING_KEY': 'disposable-signing-' + 's' * 40,
        'BOOTSTRAP_TOKEN': 'disposable-bootstrap-' + 'b' * 40,
        'ALLOW_PRIVATE_PROVIDER_HOSTS': 'true', 'SHUTDOWN_GRACE_SECONDS': str(grace),
    }
    subprocess.run([sys.executable, '-m', 'gateway.migrate'], cwd=ROOT, env=env,
                   check=True, timeout=20, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    process = subprocess.Popen(
        [sys.executable, '-c', HARNESS, '--host', '127.0.0.1', '--port', str(port)],
        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    base = f'http://127.0.0.1:{port}'
    try:
        with httpx.Client(base_url=base, trust_env=False, timeout=1) as client:
            def ready():
                assert process.poll() is None, 'Disposable gateway exited before readiness'
                try:
                    return client.get('/health/ready').status_code == 200
                except httpx.HTTPError:
                    return False
            wait_until(ready)
            response = client.post('/bootstrap', json={
                'email': 'drain-admin@example.invalid', 'token': env['BOOTSTRAP_TOKEN']})
            assert response.status_code == 201
            admin = {'Authorization': 'Bearer ' + response.json()['key']}
            provider = client.post('/admin/api/providers', headers=admin, json={
                'name': 'disposable-mock', 'kind': 'openai', 'base_url': 'https://mock.invalid/v1',
                'api_key': 'not-a-real-key', 'models': {'chat': {'upstream_model': 'mock',
                    'input_cost_per_million': 1, 'output_cost_per_million': 1}}})
            assert provider.status_code == 201
        yield process, base, admin, database
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def request(base, headers, stream):
    with httpx.Client(base_url=base, trust_env=False, timeout=10) as client:
        try:
            response = client.post('/v1/chat/completions', headers=headers, json={
                'model': 'chat', 'messages': [{'role': 'user', 'content': 'synthetic drain input'}],
                'max_tokens': 8, 'stream': stream})
            return response.status_code, response.content
        except httpx.HTTPError:
            return None, b''


def listener_stopped(base):
    # A fresh connection must stop succeeding, not merely an existing keepalive.
    try:
        with httpx.Client(base_url=base, trust_env=False, timeout=.5) as client:
            client.get('/health/live')
        return False
    except httpx.ConnectError:
        return True


def accounting(database, estimated):
    with sqlite3.connect(f'file:{database}?mode=ro', uri=True) as connection:
        reservations = connection.execute('SELECT id, status, amount_micros FROM reservations').fetchall()
        usage = connection.execute('SELECT reservation_id, estimated, cost_micros FROM usage').fetchall()
    assert len(reservations) == len(usage) == 1
    reservation, row = reservations[0], usage[0]
    assert reservation[1] == 'settled' and row[0] == reservation[0]
    assert bool(row[1]) is estimated
    assert row[2] == (reservation[2] if estimated else 5)
    assert reservation[2] > 5


@pytest.mark.parametrize('stream', [False, True])
def test_sigterm_completes_inflight_request_and_stops_new_connections(tmp_path, stream):
    with gateway_process(tmp_path, grace=5) as (process, base, headers, database):
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            response = pool.submit(request, base, headers, stream)
            wait_until(lambda: (tmp_path / ('stream-started' if stream else 'dispatches')).exists())
            process.send_signal(signal.SIGTERM)
            wait_until(lambda: listener_stopped(base), timeout=2)
            assert process.poll() is None, 'Gateway must retain the in-flight request while draining'
            (tmp_path / 'release').touch()
            status, body = response.result(timeout=8)
            assert status == 200
            assert b'[DONE]' in body if stream else b'hello' in body
            # Uvicorn re-raises captured SIGTERM after orderly shutdown.
            assert process.wait(timeout=8) in (0, -signal.SIGTERM)
            assert (tmp_path / 'lifespan-completed').exists()
        accounting(database, estimated=False)
        assert (tmp_path / 'dispatches').read_text().splitlines() == ['dispatch']
        if stream:
            assert (tmp_path / 'upstream-closed').exists()


@pytest.mark.parametrize('stream', [False, True])
def test_sigterm_expired_grace_settles_once_conservatively(tmp_path, stream):
    with gateway_process(tmp_path, grace=1) as (process, base, headers, database):
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            response = pool.submit(request, base, headers, stream)
            wait_until(lambda: (tmp_path / ('stream-started' if stream else 'dispatches')).exists())
            process.send_signal(signal.SIGTERM)
            wait_until(lambda: listener_stopped(base), timeout=2)
            status, body = response.result(timeout=8)
            if stream:
                assert status is None or b'[DONE]' not in body
            else:
                assert status is None or status >= 500
            # Uvicorn re-raises captured SIGTERM after orderly shutdown.
            assert process.wait(timeout=8) in (0, -signal.SIGTERM)
            assert (tmp_path / 'lifespan-completed').exists()
        accounting(database, estimated=True)
        assert (tmp_path / 'dispatches').read_text().splitlines() == ['dispatch']
        if stream:
            assert (tmp_path / 'upstream-closed').exists()
