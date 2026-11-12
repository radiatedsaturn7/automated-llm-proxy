"""Bounded, explicit two-replica acceptance test. No production targets or paid calls."""
import concurrent.futures
from collections import Counter
import json
import math
import os
import subprocess
import time
import urllib.error
import urllib.request

BASES = ('http://127.0.0.1:8180', 'http://127.0.0.1:8181')
COMPOSE = 'compose.yaml:tests/browser/compose.mock.yaml:tests/ops/compose.replicas.yaml'
STAGE = 'safety_gate'


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def local_opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())


def require_disposable():
    if (os.environ.get('DISPOSABLE_REPLICA_DRILL') != '1' or os.environ.get('COMPOSE_FILE') != COMPOSE
            or os.environ.get('COMPOSE_PROJECT_NAME') != 'thunderbolt-replica-drill'):
        raise ValueError('Explicit disposable replica stack required')
    if not os.environ.get('BOOTSTRAP_TOKEN'):
        raise ValueError('Disposable installation credentials required')


def docker(*args, input=None, env=None):
    result = subprocess.run(['docker', *args], input=input, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, check=False, timeout=180, env=env)
    if result.returncode:
        raise RuntimeError('Disposable container command failed')
    return result.stdout.decode()


def control(action='status'):
    # Control channel stays inside test-only mock container; TLS verifies its CA.
    code = "import ssl,urllib.request; r=urllib.request.Request('https://mock-provider:8443/control/" + action + "',method='" + ('GET' if action == 'status' else 'POST') + "'); print(urllib.request.urlopen(r,context=ssl.create_default_context(cafile='/tls/server.crt'),timeout=3).read().decode())"
    return json.loads(docker('compose', 'exec', '-T', 'mock-provider', 'python', '-c', code))


def http(replica, path, key=None, body=None, method=None):
    started = time.monotonic()
    request = urllib.request.Request(BASES[replica] + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={'Content-Type': 'application/json', **({'Authorization': 'Bearer ' + key} if key else {})},
        method=method)
    try:
        with local_opener().open(request, timeout=55) as response:
            status, content = response.status, response.read()
    except urllib.error.HTTPError as exc:
        status, content = exc.code, exc.read()
    except (urllib.error.URLError, TimeoutError, ConnectionError):
        return {'status': 0, 'seconds': time.monotonic() - started, 'data': None}
    try:
        data = json.loads(content)
    except (ValueError, UnicodeError):
        data = None
    return {'status': status, 'seconds': time.monotonic() - started, 'data': data}


def required(replica, path, key=None, body=None, method=None, statuses=(200, 201)):
    result = http(replica, path, key, body, method)
    if result['status'] not in statuses:
        raise AssertionError('Disposable API operation failed')
    return result['data']


def call(replica, key, marker):
    return http(replica, '/v1/chat/completions', key, {'model': 'chat',
        'messages': [{'role': 'user', 'content': marker}], 'max_tokens': 4})


def summary(results):
    def percentiles(values):
        values = sorted(values)
        return {name: round(values[max(0, math.ceil(len(values) * q) - 1)] * 1000, 2) if values else None
                for name, q in [('p50_ms', .5), ('p95_ms', .95), ('p99_ms', .99)]}
    return {'requests': len(results), 'statuses': dict(Counter(str(x['status']) for x in results)),
            'latency_all': percentiles([x['seconds'] for x in results]),
            'latency_success': percentiles([x['seconds'] for x in results if x['status'] == 200])}


def resources():
    ids = docker('compose', 'ps', '-q', 'gateway', 'gateway-peer', 'postgres').split()
    if len(ids) != 3:
        raise AssertionError('Expected two gateway containers and PostgreSQL')
    rows = docker('stats', '--no-stream', '--format', '{{json .}}', *ids).splitlines()
    return [{name: item[name] for name in ('Name', 'CPUPerc', 'MemUsage', 'PIDs')}
            for item in map(json.loads, rows)]


def run():
    global STAGE
    require_disposable()
    STAGE = 'setup'
    report = {'scope': 'Disposable two-process PostgreSQL acceptance; not a capacity benchmark', 'phases': {}}
    before = [json.loads(docker('inspect', container))[0]['State']['Pid'] for container in
              docker('compose', 'ps', '-q', 'gateway', 'gateway-peer').split()]
    assert len(before) == 2 and len(set(before)) == 2 and all(before)
    report['distinct_app_processes'] = True
    admin = required(0, '/bootstrap', body={'email': 'load-admin@example.invalid', 'token': os.environ['BOOTSTRAP_TOKEN']})['key']
    required(1, '/admin/api/providers', admin, {'name': 'load-mock', 'kind': 'openai',
        'base_url': 'https://mock-provider:8443/v1', 'api_key': 'integration-only-not-a-real-key',
        'models': {'chat': {'upstream_model': 'mock', 'input_cost_per_million': 1, 'output_cost_per_million': 1}}})

    def subject(name, budget, rpm=120, team=None):
        team = team or required(0, '/admin/api/teams', admin, {'name': name, 'budget_usd': budget})['id']
        user = required(1, '/admin/api/users', admin, {'email': name + '@example.invalid', 'team_id': team})['id']
        key = required(0, '/admin/api/keys', admin, {'name': name, 'user_id': user, 'team_id': team,
                       'allowed_models': ['chat'], 'rpm': rpm, 'tpm': 100000})
        return team, key

    def rows(resource):
        return required(1, '/admin/api/' + resource + '?limit=1000', admin)

    # Simultaneously held admissions prove pending budget sharing across users,
    # keys and replicas rather than merely observing final settled usage.
    STAGE = 'shared_budget'
    team, first = subject('budget-first', '0.004')
    _, second = subject('budget-second', '0.004', team=team)
    control('hold')
    count = 40
    with concurrent.futures.ThreadPoolExecutor(max_workers=count) as pool:
        futures = [pool.submit(call, i % 2, [first, second][i % 2]['key'], f'hold:budget:{i}') for i in range(count)]
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            admitted = sum(n for marker, n in control()['counts'].items() if marker.startswith('hold:budget:'))
            if admitted + sum(f.done() for f in futures) == count:
                break
            time.sleep(.1)
        else:
            raise AssertionError('Budget admission did not complete in bounded time')
        pending = [r for r in rows('reservations') if r['team_id'] == team and r['status'] == 'pending']
        assert 0 < len(pending) == admitted < count
        assert sum(r['amount_micros'] for r in pending) <= 4000
        control('release')
        results = [f.result(timeout=10) for f in futures]
    assert Counter(r['status'] for r in results) == {200: admitted, 429: count - admitted}
    assert sum(r['cost_micros'] for r in rows('usage') if r['team_id'] == team) == admitted * 5
    report['phases']['budget'] = summary(results) | {'pending_admissions': admitted, 'pending_micros': sum(r['amount_micros'] for r in pending)}

    STAGE = 'shared_rate_limit'
    # Avoid crossing the intentionally fixed UTC minute rate window.
    if time.time() % 60 > 40:
        time.sleep(60 - time.time() % 60 + .1)
    _, rate = subject('rate', 10, rpm=6)
    with concurrent.futures.ThreadPoolExecutor(max_workers=32) as pool:
        results = list(pool.map(lambda i: call(i % 2, rate['key'], f'rate:{i}'), range(32)))
    assert Counter(r['status'] for r in results) == {200: 6, 429: 26}
    report['phases']['shared_rpm'] = summary(results)
    report['resources_before_loss'] = resources()

    # Kill after mock records dispatch, not before admission. Preserve uncertainty.
    STAGE = 'worker_loss'
    crash_team, crash = subject('crash', 10)
    control('hold')
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        lost = pool.submit(call, 0, crash['key'], 'hold:crash')
        deadline = time.monotonic() + 15
        while control()['counts'].get('hold:crash', 0) != 1:
            assert time.monotonic() < deadline
            time.sleep(.1)
        pending = [r for r in rows('reservations') if r['key_id'] == crash['id']]
        assert len(pending) == 1 and pending[0]['status'] == 'pending'
        amount = pending[0]['amount_micros']
        required(1, '/admin/api/teams/' + crash_team, admin, {'budget_usd': f'{amount / 1000000:.6f}'}, method='PATCH')
        docker('compose', 'kill', '-s', 'SIGKILL', 'gateway')
        lost_result = lost.result(timeout=10)
        assert lost_result['status'] == 0
    denied = call(1, crash['key'], 'crash-must-not-replay')
    assert denied['status'] == 429
    assert control()['counts'].get('hold:crash') == 1
    control('release')
    # Independent successful traffic on surviving peer, then restarted process.
    _, survivor = subject_on_peer(admin)
    healthy = [call(1, survivor['key'], f'survivor:{i}') for i in range(4)]
    assert all(r['status'] == 200 for r in healthy)
    STAGE = 'restart_and_accounting'
    docker('compose', 'up', '-d', '--wait', '--wait-timeout', '90', 'gateway')
    healthy += [call(i % 2, survivor['key'], f'recovered:{i}') for i in range(4)]
    assert all(r['status'] == 200 for r in healthy)
    restored_pending = [r for r in rows('reservations') if r['id'] == pending[0]['id']]
    assert len(restored_pending) == 1 and restored_pending[0]['status'] == 'pending'
    assert restored_pending[0]['amount_micros'] == amount
    counts = control()['counts']
    assert counts.get('hold:crash') == 1 and 'crash-must-not-replay' not in counts
    assert all(n == 1 for n in counts.values())
    usage = rows('usage')
    assert len(usage) == admitted + 6 + 8
    assert all(not row['estimated'] and row['cost_micros'] == 5 for row in usage)
    reservations = rows('reservations')
    assert Counter(row['status'] for row in reservations) == {'settled': len(usage), 'pending': 1}
    report['phases']['worker_loss'] = {'lost_request_status': 0, 'pending_charge_micros': amount,
        'replayed_upstream_requests': 0, 'surviving_and_recovered': summary(healthy)}
    report['accounting'] = {'settled_requests': len(usage), 'pending_requests': 1, 'settled_micros': sum(r['cost_micros'] for r in usage)}
    report['resources_after_recovery'] = resources()
    STAGE = 'offline_master_key_rotation'
    from rotation_drill import run_rotation
    report['master_key_rotation'] = run_rotation(docker, http, call, control, admin, survivor['key'])
    report['ok'] = True
    return report


def subject_on_peer(admin):
    team = required(1, '/admin/api/teams', admin, {'name': 'survivor', 'budget_usd': 10})['id']
    user = required(1, '/admin/api/users', admin, {'email': 'survivor@example.invalid', 'team_id': team})['id']
    key = required(1, '/admin/api/keys', admin, {'name': 'survivor', 'user_id': user, 'team_id': team,
                   'allowed_models': ['chat'], 'rpm': 120, 'tpm': 100000})
    return team, key


if __name__ == '__main__':
    try:
        print(json.dumps(run(), sort_keys=True))
    except Exception:
        # Do not expose credentials, API response bodies or mock input content.
        print(json.dumps({'ok': False, 'stage': STAGE, 'reason': 'Disposable replica acceptance failed; diagnostics suppressed'}))
        raise SystemExit(1) from None
