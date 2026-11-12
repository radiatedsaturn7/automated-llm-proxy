"""Offline synthetic key rotation, invoked only after bounded replica acceptance."""
import base64
import hashlib
import json
import os
import secrets


def fingerprint(key):
    return hashlib.sha256(b'thunderbolt/master-key/v1\x00' + base64.urlsafe_b64decode(key)).hexdigest()


def run_rotation(docker, http, call, control, admin, inference_key):
    old = os.environ['GATEWAY_MASTER_KEY']
    new = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    new_environment = dict(os.environ, GATEWAY_MASTER_KEY=new, GATEWAY_MASTER_DECRYPT_KEYS=json.dumps([old]))
    reverse_environment = dict(os.environ, GATEWAY_MASTER_KEY=old, GATEWAY_MASTER_DECRYPT_KEYS=json.dumps([new]))

    def rotate(environment, current, execute=False):
        args = ['compose', 'run', '--no-deps', '--rm', '-T', '--entrypoint', 'python', 'gateway',
                '-m', 'gateway.rotate_master', '--expected-current-fingerprint', fingerprint(current),
                '--timeout-seconds', '60', '--max-rows', '1000']
        if execute:
            args.append('--execute')
        # Only nonsecret fingerprint/limits enter argv; keys enter inherited
        # environment consumed by Compose, never printed or added to this report.
        docker(*args, env=environment)

    def counts():
        sql = 'SELECT (SELECT count(*) FROM reservations), (SELECT count(*) FROM usage), (SELECT count(*) FROM teams)'
        return docker('compose', 'exec', '-T', 'postgres', 'psql', '-U', 'gateway', '-d', 'gateway', '-Atc', sql).strip()

    # There are no in-flight requests. The earlier crash's pending reservation
    # intentionally remains uncertain and must survive both transformations.
    before = counts()
    upstream = control()['counts']
    rotate(new_environment, old)
    assert all(http(i, '/health/ready')['status'] == 200 for i in (0, 1))
    assert counts() == before
    rotate(new_environment, old, execute=True)
    for replica in (0, 1):
        assert http(replica, '/health/ready')['status'] == 503
        assert http(replica, '/admin/api/teams', admin,
                    {'name': 'must-not-write-stale', 'budget_usd': 1})['status'] == 503
        assert call(replica, inference_key, f'stale-master:{replica}')['status'] == 503
    assert counts() == before
    assert control()['counts'] == upstream
    # Start only one target-primary replica, without rerunning migration.
    docker('compose', 'up', '-d', '--no-deps', '--force-recreate', '--wait', '--wait-timeout', '60',
           'gateway', env=new_environment)
    assert http(0, '/health/ready')['status'] == 200
    assert http(1, '/health/ready')['status'] == 503
    assert call(0, inference_key, 'rotated-primary')['status'] == 200
    # Reverse migration re-encrypts persisted state; merely changing env would
    # not suffice. The untouched old replica becomes valid only after reversal.
    rotate(reverse_environment, new, execute=True)
    assert http(0, '/health/ready')['status'] == 503
    assert http(1, '/health/ready')['status'] == 200
    assert call(1, inference_key, 'reversed-primary')['status'] == 200
    docker('compose', 'up', '-d', '--no-deps', '--force-recreate', '--wait', '--wait-timeout', '60', 'gateway')
    assert all(http(i, '/health/ready')['status'] == 200 for i in (0, 1))
    final = control()['counts']
    assert final == upstream | {'rotated-primary': 1, 'reversed-primary': 1}
    # No provider replay or pending reservation release during rotation.
    pending = docker('compose', 'exec', '-T', 'postgres', 'psql', '-U', 'gateway', '-d', 'gateway', '-Atc',
                     "SELECT count(*) FROM reservations WHERE status = 'pending'").strip()
    assert pending == '1'
    return {'dry_run_preserved_admission': True, 'stale_replicas_rejected': 2,
            'stale_writes_or_reservations': 0, 'stale_upstream_dispatches': 0,
            'new_primary_provider_call': True, 'reverse_rotation_provider_call': True,
            'pending_reservation_preserved': True, 'scope': 'Offline disposable rotation; not zero-downtime migration'}
