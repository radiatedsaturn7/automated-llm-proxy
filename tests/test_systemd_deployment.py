"""Native VM kit contracts; syntax checks do not install or start services."""
import configparser
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
KIT = ROOT / 'deploy/systemd'


def unit(name):
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str
    parser.read(KIT / name)
    return parser


def test_native_units_are_separate_nonroot_and_source_readonly():
    for name in ('brrrtgate.service', 'brrrtgate-migrate.service'):
        doc = unit(name)
        service = doc['Service']
        assert service['User'] == service['Group'] == 'brrrtgate'
        assert service['ProtectSystem'] == 'strict'
        assert service['ProtectHome'] == service['NoNewPrivileges'] == 'yes'
        assert service['CapabilityBoundingSet'] == ''
        assert service['AmbientCapabilities'] == ''
        assert 'ReadWritePaths' not in service
        assert service['WorkingDirectory'] == '/opt/brrrtgate/current'
        assert service['EnvironmentFile'] == '/etc/brrrtgate/gateway.env'
        assert service['ExecStart'].startswith('/opt/brrrtgate/current/.venv/bin/python -m gateway.')
    app = unit('brrrtgate.service')
    assert '--host 127.0.0.1 --port 8080 --shutdown-grace-seconds 360' in app['Service']['ExecStart']
    assert int(app['Service']['TimeoutStopSec']) >= 360 + 30
    assert app['Service']['KillSignal'] == 'SIGTERM'
    assert app['Service']['KillMode'] == 'mixed'
    assert 'migrate' not in str(dict(app['Unit']))
    assert 'ExecStartPre' not in app['Service']
    migration = unit('brrrtgate-migrate.service')
    assert migration['Service']['Type'] == 'oneshot'
    assert 'Install' not in migration and 'Restart' not in migration['Service']


def test_config_uses_allowlisted_file_secrets():
    from gateway.secret_files import ALLOWED
    values = dict(line.split('=', 1) for line in (KIT / 'gateway.env.example').read_text().splitlines()
                  if line and not line.startswith('#'))
    assert values['ENVIRONMENT'] == 'production'
    assert values['PUBLIC_URL'].startswith('https://')
    assert values['GATEWAY_SECRETS_DIR'] == '/etc/brrrtgate/secrets'
    for key, value in values.items():
        assert key not in ALLOWED
        if key.endswith('_FILE'):
            assert key[:-5] in ALLOWED
            assert value.startswith('/etc/brrrtgate/secrets/')
    assert 'DATABASE_URL_FILE' in values


def test_systemd_unit_syntax_without_installation(tmp_path):
    tool = shutil.which('systemd-analyze')
    if not tool:
        pytest.skip('systemd-analyze unavailable; deployment CI runs this validation')
    true = shutil.which('true')
    assert true
    files = []
    for source in KIT.glob('*.service'):
        # Verify directives/dependencies without requiring a real installation.
        # Only substitute the executable path; never execute the service command.
        text = source.read_text().replace('/opt/brrrtgate/current/.venv/bin/python', true)
        target = tmp_path / source.name
        target.write_text(text)
        files.append(str(target))
    result = subprocess.run([tool, 'verify', *files], capture_output=True, text=True, timeout=30)
    if result.returncode and "Failed to create directory '/run/systemd/'" in result.stderr:
        # Restricted build containers may forbid the system manager scratch dir.
        # User-manager verification parses the same unit syntax without writes
        # to host /run. Deployment CI normally exercises system-manager verification.
        runtime = tmp_path / 'runtime'
        runtime.mkdir(mode=0o700)
        result = subprocess.run([tool, '--user', 'verify', *files], capture_output=True,
                                text=True, timeout=30,
                                env={**os.environ, 'XDG_RUNTIME_DIR': str(runtime)})
    assert result.returncode == 0, result.stderr
    assert 'Unknown' not in result.stderr and 'Failed to parse' not in result.stderr
