"""Run inside nonroot fixture gateway; output no values or credential-bearing errors."""
import os
from pathlib import Path
import subprocess
import sys

NAMES = ('DATABASE_URL', 'GATEWAY_MASTER_KEY', 'GATEWAY_SIGNING_KEY',
         'BOOTSTRAP_TOKEN', 'METRICS_TOKEN', 'OIDC_CLIENT_SECRET')


def check():
    assert os.geteuid() == 10001
    root = Path('/run/secrets/thunderbolt')
    assert os.environ['GATEWAY_SECRETS_DIR'] == str(root)
    assert (root / '..data').is_symlink()
    for name in NAMES:
        assert name not in os.environ
        path = root / name
        assert os.environ[name + '_FILE'] == str(path) and path.is_symlink()
        stat = path.stat()
        assert stat.st_uid == 0 and stat.st_gid == 10001 and stat.st_mode & 0o777 == 0o440
    result = subprocess.run([sys.executable, '-m', 'gateway.check'], capture_output=True, timeout=20)
    assert result.returncode == 0
    # The direct value is synthetic; the real file remains mounted. Reject
    # ambiguous input rather than silently choosing one source.
    code = '''from gateway.config import get_settings
try:
    get_settings()
except ValueError:
    raise SystemExit(0)
raise SystemExit(1)
'''
    result = subprocess.run([sys.executable, '-c', code], capture_output=True, timeout=20,
                            env=dict(os.environ, GATEWAY_MASTER_KEY='conflicting-test-only-value'))
    assert result.returncode == 0
    print('Mounted-secret nonroot preflight and conflicting-source rejection passed')


if __name__ == '__main__':
    try:
        check()
    except Exception:
        print('Mounted-secret acceptance failed; credential diagnostics suppressed')
        raise SystemExit(1) from None
