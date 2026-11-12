"""Fixture contracts only; running nonroot containers is verified separately in CI."""
import importlib.util
import os
from pathlib import Path

import pytest
import yaml

from gateway.secret_files import read_secret

ROOT = Path(__file__).resolve().parents[1]


def projection_module():
    spec = importlib.util.spec_from_file_location('oidc_projection', ROOT / 'tests/oidc/project_secrets.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_projection_layout_reads_and_is_not_overwritten(tmp_path):
    module = projection_module()
    source, target = tmp_path / 'source', tmp_path / 'installation'
    source.mkdir(mode=0o700)
    target.mkdir(mode=0o700)
    for name in module.NAMES:
        (source / name).write_text('synthetic-value')
        (source / name).chmod(0o600)
    try:
        module.project(source, target, uid=os.geteuid(), gid=os.getegid())
        assert (target / '..data').is_symlink()
        for name in module.NAMES:
            assert (target / name).is_symlink()
            assert (target / name).stat().st_mode & 0o777 == 0o440
            assert read_secret(str(target), str(target / name)) == 'synthetic-value'
        with pytest.raises(FileExistsError):
            module.project(source, target, uid=os.geteuid(), gid=os.getegid())
    finally:
        target.chmod(0o700)
        if (target / '..installation-v1').exists():
            (target / '..installation-v1').chmod(0o700)


def test_fixture_only_passes_files_to_both_gateway_processes():
    services = yaml.safe_load((ROOT / 'tests/oidc/compose.yaml').read_text())['services']
    for name in ('migrate', 'gateway'):
        service = services[name]
        env = service['environment']
        assert env['ENVIRONMENT'] == 'production'
        assert env['PUBLIC_URL'] == 'https://gateway.test'
        assert env['OIDC_ISSUER'] == 'https://idp.test/realms/gateway-ci'
        assert env['GATEWAY_SECRETS_DIR'] == '/run/secrets/thunderbolt'
        assert 'user' not in service  # Inherits Dockerfile nonroot USER 10001:10001.
        assert service['read_only'] is True
        assert 'installation-secrets:/run/secrets/thunderbolt:ro' in service['volumes']
        for key in projection_module().NAMES:
            assert key not in env and env[key + '_FILE'] == '/run/secrets/thunderbolt/' + key
    assert services['secret-init']['user'] == '0:0'
    assert services['secret-init']['network_mode'] == 'none'
    assert services['migrate']['depends_on']['secret-init']['condition'] == 'service_completed_successfully'
    assert services['gateway']['depends_on']['migrate']['condition'] == 'service_completed_successfully'


def test_workflow_runs_mounted_check_before_identity_flows():
    workflow = yaml.safe_load((ROOT / '.github/workflows/ci.yml').read_text())
    steps = workflow['jobs']['real-oidc']['steps']
    check = next(i for i, step in enumerate(steps) if 'check_mounted.py' in step.get('run', ''))
    browser = next(i for i, step in enumerate(steps) if 'tests/oidc/playwright.config.mjs' in step.get('run', ''))
    assert check < browser and steps[check]['timeout-minutes'] == 2
    assert '--volumes' in steps[-1]['run'] and steps[-1]['if'] == 'always()'
