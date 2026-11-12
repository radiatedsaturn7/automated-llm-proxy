import importlib.util
import os
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / 'deploy/helm/llm-gateway'
HELM = os.environ.get('HELM_BIN') or shutil.which('helm')
spec = importlib.util.spec_from_file_location('check_iac', ROOT / 'tools/security/check_iac_report.py')
check_iac = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check_iac)


def report():
    return {'check_type': 'kubernetes', 'summary': {'failed': 0, 'skipped': 0, 'parsing_errors': 0,
            'passed': 444, 'resource_count': 17}, 'results': {'failed_checks': [], 'skipped_checks': [],
            'parsing_errors': [], 'passed_checks': [{'check_id': 'fixture'}]}}


def test_report_requires_real_kubernetes_coverage():
    assert check_iac.report_ok(report())
    for key in ['failed', 'skipped', 'parsing_errors']:
        data = report()
        data['summary'][key] = 1
        assert not check_iac.report_ok(data)
    for key, value in [('resource_count', 0), ('resource_count', 16), ('passed', 0)]:
        data = report()
        data['summary'][key] = value
        assert not check_iac.report_ok(data)
    assert not check_iac.report_ok({'check_type': 'terraform', 'summary': {'passed': 0}})
    assert not check_iac.report_ok({})


@pytest.mark.skipif(not HELM, reason='Helm not installed')
@pytest.mark.parametrize('override', ['namespace', 'existingSecret=env-secret',
    'mountedSecrets.existingSecret=', 'image.digest=', 'image.pullPolicy=IfNotPresent',
    'networkPolicy.enabled=false', 'environment=development', 'ingress.enabled=true',
    'extraEnv[0].name=OTHER_SECRET,extraEnv[0].valueFrom.secretKeyRef.name=secret,extraEnv[0].valueFrom.secretKeyRef.key=key'])
def test_security_profile_rejects_insecure_overrides(override):
    args = [HELM, 'template', 'secure', str(CHART), '--namespace',
            'default' if override == 'namespace' else 'gateway', '-f', str(CHART / 'values-security.yaml'),
            '--set', 'mountedSecrets.existingSecret=fixture', '--set', 'image.digest=sha256:' + 'a' * 64]
    if override != 'namespace':
        args += ['--set', override]
    proc = subprocess.run(args, capture_output=True, text=True)
    assert proc.returncode != 0
    assert any(message in proc.stderr for message in ('securityProfile', 'Use either', 'existingSecret'))


@pytest.mark.skipif(not HELM, reason='Helm not installed')
def test_both_security_profiles_render_namespaces_and_mounted_secrets(tmp_path):
    proc = subprocess.run(['bash', 'tools/security/render_iac.sh', str(tmp_path)], cwd=ROOT,
                          env={**os.environ, 'HELM_BIN': HELM}, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    docs = []
    for path in tmp_path.glob('*.yaml'):
        docs += [d for d in yaml.safe_load_all(path.read_text()) if d]
    assert len(docs) == 13
    assert all(d['metadata']['namespace'] == 'brrrtgate-security' for d in docs)
    assert {d['kind'] for d in docs} >= {'Deployment', 'Job', 'CronJob', 'NetworkPolicy', 'HorizontalPodAutoscaler'}
    for d in docs:
        if d['kind'] == 'NetworkPolicy':
            assert d['spec']['ingress'] == [] and d['spec']['egress'] == []
    text = '\n'.join(p.read_text() for p in tmp_path.glob('*.yaml'))
    assert 'envFrom:' not in text
    assert 'imagePullPolicy: Always' in text
    assert 'GATEWAY_SECRETS_DIR' in text
