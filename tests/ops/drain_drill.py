"""SIGTERM acceptance on disposable PostgreSQL replicas, never production targets."""
import concurrent.futures
import json
import time


def run_drain(docker, http, call, control, admin, key):
    def rows(resource):
        result = http(1, '/admin/api/' + resource + '?limit=1000', admin)
        assert result['status'] == 200
        return result['data']

    def until(predicate, seconds=15):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.1)
        raise AssertionError('Disposable drain deadline exceeded')

    def running(container):
        return json.loads(docker('inspect', container))[0]['State']['Running']

    original_pending = {row['id']: row['amount_micros'] for row in rows('reservations')
                        if row['status'] == 'pending'}
    phases = []
    for expired in (False, True):
        marker = 'hold:drain:' + ('deadline' if expired else 'complete')
        previous = {row['id'] for row in rows('reservations')}
        container = docker('compose', 'ps', '-q', 'gateway').strip()
        assert container and running(container)
        control('hold')
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                response = pool.submit(call, 0, key, marker)
                until(lambda: control()['counts'].get(marker, 0) == 1)
                admitted = [row for row in rows('reservations') if row['id'] not in previous]
                assert len(admitted) == 1 and admitted[0]['status'] == 'pending'
                reservation = admitted[0]
                started = time.monotonic()
                docker('compose', 'kill', '-s', 'SIGTERM', 'gateway')
                until(lambda: http(0, '/health/live')['status'] == 0, seconds=3)
                # Readiness and independent inference on the surviving replica
                # must work while the first replica drains its held request.
                assert http(1, '/health/ready')['status'] == 200
                assert call(1, key, 'drain-peer:' + str(expired))['status'] == 200
                if not expired:
                    assert running(container), 'In-flight request must remain during drain'
                    control('release')
                outcome = response.result(timeout=15)
                if expired:
                    assert outcome['status'] == 0 or outcome['status'] >= 500
                else:
                    assert outcome['status'] == 200
                until(lambda: not running(container))
                state = json.loads(docker('inspect', container))[0]['State']
                assert state['ExitCode'] in (0, 143) and not state['OOMKilled']
                elapsed = time.monotonic() - started
            # API reads on the live peer prove durable, shared PostgreSQL state.
            settled = [row for row in rows('reservations') if row['id'] == reservation['id']]
            usage = [row for row in rows('usage') if row['reservation_id'] == reservation['id']]
            assert len(settled) == len(usage) == 1
            assert settled[0]['status'] == 'settled'
            assert bool(usage[0]['estimated']) is expired
            expected = reservation['amount_micros'] if expired else 5
            assert usage[0]['cost_micros'] == expected
            assert control()['counts'].get(marker) == 1
            remaining = {row['id']: row['amount_micros'] for row in rows('reservations')
                         if row['status'] == 'pending'}
            assert remaining == original_pending, 'SIGKILL uncertainty must remain reserved'
            phases.append({'expired_grace': expired, 'status': outcome['status'],
                           'settled_micros': expected, 'estimated': expired,
                           'seconds': round(elapsed, 3), 'upstream_dispatches': 1})
        finally:
            control('release')
            docker('compose', 'up', '-d', '--wait', '--wait-timeout', '90', 'gateway')
        assert http(0, '/health/ready')['status'] == 200
    return {'phases': phases, 'preserved_pending_reservations': len(original_pending)}
