"""Local harness contracts; actual multi-container evidence belongs to CI."""
import concurrent.futures
import importlib.util
import json
from pathlib import Path
import threading
import time
import urllib.request

import pytest
import yaml


def module(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).parent / 'ops' / (name + '.py'))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def test_gate_requires_isolated_project(monkeypatch):
    drill = module('replica_drill')
    monkeypatch.setenv('DISPOSABLE_REPLICA_DRILL', '1')
    monkeypatch.setenv('COMPOSE_FILE', drill.COMPOSE)
    monkeypatch.setenv('BOOTSTRAP_TOKEN', 'test-only')
    monkeypatch.delenv('COMPOSE_PROJECT_NAME', raising=False)
    with pytest.raises(ValueError):
        drill.require_disposable()
    monkeypatch.setenv('COMPOSE_PROJECT_NAME', 'my-live-project')
    with pytest.raises(ValueError):
        drill.require_disposable()
    monkeypatch.setenv('COMPOSE_PROJECT_NAME', 'thunderbolt-replica-drill')
    drill.require_disposable()
    assert drill.BASES == ('http://127.0.0.1:8180', 'http://127.0.0.1:8181')


def test_summary_separates_expected_rejections_and_success_latency():
    drill = module('replica_drill')
    report = drill.summary([{'status': 200, 'seconds': .1}, {'status': 429, 'seconds': .01},
                            {'status': 200, 'seconds': .2}])
    assert report['statuses'] == {'200': 2, '429': 1}
    assert report['latency_all'] == {'p50_ms': 100, 'p95_ms': 200, 'p99_ms': 200}
    assert report['latency_success']['p50_ms'] == 100
    assert drill.summary([])['latency_all']['p95_ms'] is None


def test_compose_peers_are_isolated_and_no_restart():
    class Loader(yaml.SafeLoader):
        pass
    Loader.add_constructor('!override', lambda loader, node: loader.construct_sequence(node))
    path = Path(__file__).parent / 'ops' / 'compose.replicas.yaml'
    config = yaml.load(path.read_text(), Loader=Loader)['services']
    assert config['gateway']['restart'] == config['gateway-peer']['restart'] == 'no'
    assert config['gateway']['ports'] == ['127.0.0.1:8180:8080']
    assert config['gateway-peer']['ports'] == ['127.0.0.1:8181:8080']
    assert config['gateway-peer']['image'] == 'automated-llm-proxy:smoke'
    assert 'DATABASE_URL' in config['gateway-peer']['environment']


def test_mock_hold_counts_once_and_releases():
    mock = module('load_provider')
    server = mock.ThreadingHTTPServer(('127.0.0.1', 0), mock.Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    base = f'http://127.0.0.1:{server.server_port}'
    def request(path, body=None):
        req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None,
            headers={'Authorization': 'Bearer integration-only-not-a-real-key'})
        with urllib.request.urlopen(req, timeout=3) as response:
            return json.loads(response.read())
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(request, '/v1/chat/completions',
                {'model': 'mock', 'messages': [{'role': 'user', 'content': 'hold:unit'}]})
            deadline = time.monotonic() + 2
            while not request('/control/status')['counts']:
                assert time.monotonic() < deadline
                time.sleep(.01)
            assert not pending.done()
            assert request('/control/status')['counts'] == {'hold:unit': 1}
            request('/control/release', {})
            assert pending.result(timeout=2)['usage']['total_tokens'] == 5
            assert request('/control/status')['counts'] == {'hold:unit': 1}
    finally:
        mock.release.set()
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def test_harness_never_follows_redirect_or_environment_proxy(monkeypatch):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    drill = module('replica_drill')
    visited = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            visited.append(self.path)
            self.send_response(302)
            self.send_header('Location', '/credential-leak')
            self.end_headers()
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    monkeypatch.setenv('http_proxy', 'http://127.0.0.1:1')
    monkeypatch.setenv('no_proxy', '')
    monkeypatch.setattr(drill, 'BASES', (f'http://127.0.0.1:{server.server_port}',))
    try:
        assert drill.http(0, '/start', key='test-only')['status'] == 302
        assert visited == ['/start']
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
