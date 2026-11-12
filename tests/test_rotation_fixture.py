"""Rotation harness command safety, not a substitute for live-container CI."""
import base64
import importlib.util
import json
from pathlib import Path

from gateway.encryption import fingerprint


def test_fixture_passes_keys_only_by_environment_and_reverses(monkeypatch):
    path = Path(__file__).parent / 'ops/rotation_drill.py'
    spec = importlib.util.spec_from_file_location('rotation_fixture', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    old = base64.urlsafe_b64encode(bytes(range(32))).decode()
    monkeypatch.setenv('GATEWAY_MASTER_KEY', old)
    assert module.fingerprint(old) == fingerprint(old)
    state = {'primary': old, 'replicas': [old, old], 'calls': {}}
    rotations = []
    def docker(*args, env=None):
        if 'gateway.rotate_master' in args:
            target = env['GATEWAY_MASTER_KEY']
            fallbacks = json.loads(env['GATEWAY_MASTER_DECRYPT_KEYS'])
            assert old not in args and target not in args and all(key not in args for key in fallbacks)
            assert args[args.index('--expected-current-fingerprint') + 1] == fingerprint(state['primary'])
            assert state['primary'] in fallbacks
            rotations.append('--execute' in args)
            if '--execute' in args:
                state['primary'] = target
        elif '--force-recreate' in args:
            assert '--no-deps' in args
            state['replicas'][0] = (env or {'GATEWAY_MASTER_KEY': old})['GATEWAY_MASTER_KEY']
        elif 'psql' in args:
            return '1' if 'WHERE status' in args[-1] else '20|19|4'
        else:
            raise AssertionError('Unexpected fixture command')
        return ''
    def http(replica, *args):
        return {'status': 200 if state['replicas'][replica] == state['primary'] else 503}
    def call(replica, key, marker):
        response = http(replica)
        if response['status'] == 200:
            state['calls'][marker] = state['calls'].get(marker, 0) + 1
        return response
    result = module.run_rotation(docker, http, call, lambda: {'counts': dict(state['calls'])}, 'admin', 'inference')
    assert rotations == [False, True, True]
    assert state['primary'] == old and state['replicas'] == [old, old]
    assert result['stale_writes_or_reservations'] == 0
