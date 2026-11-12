"""Rendered chart contract; install Helm in CI to execute these tests."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml

HELM = os.environ.get("HELM_BIN") or shutil.which("helm")
pytestmark = pytest.mark.skipif(not HELM, reason="Helm not installed")
CHART = Path(__file__).resolve().parents[1] / "deploy/helm/llm-gateway"


def render(*settings, success=True):
    args = [HELM, "template", "test", str(CHART), "--set", "existingSecret=test-secret"]
    for setting in settings:
        args.extend(["--set", setting])
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    assert (result.returncode == 0) == success, result.stderr
    return list(yaml.safe_load_all(result.stdout)) if success else []


def test_disabled_by_default():
    assert not any(doc.get("kind") == "CronJob" for doc in render())


def test_cleanup_security_and_bounded_command():
    docs = render("maintenance.enabled=true", "maintenance.batchSize=37", "imagePullSecrets[0].name=pull-secret")
    cron = next(doc for doc in docs if doc["kind"] == "CronJob")
    assert len(cron["metadata"]["name"]) <= 52
    spec = cron["spec"]
    assert spec["concurrencyPolicy"] == "Forbid"
    assert spec["timeZone"] == "Etc/UTC"
    job = spec["jobTemplate"]["spec"]
    assert job["activeDeadlineSeconds"] == 300
    assert job["ttlSecondsAfterFinished"] == 86400
    pod = job["template"]["spec"]
    assert pod["automountServiceAccountToken"] is False
    assert pod["securityContext"]["runAsNonRoot"]
    assert pod["imagePullSecrets"] == [{"name": "pull-secret"}]
    container = pod["containers"][0]
    assert container["command"] == ["python", "-m", "gateway.maintenance", "--apply", "--batch-size", "37"]
    assert container["envFrom"] == [{"secretRef": {"name": "test-secret"}}]
    assert container["securityContext"]["readOnlyRootFilesystem"]
    assert container["securityContext"]["capabilities"]["drop"] == ["ALL"]
    assert job["template"]["metadata"]["labels"]["app.kubernetes.io/component"] == "maintenance"


@pytest.mark.parametrize("setting", ["maintenance.batchSize=0", "maintenance.batchSize=10001", "maintenance.backoffLimit=4", "maintenance.activeDeadlineSeconds=0", "maintenance.schedule=@hourly", "maintenance.failedJobsHistoryLimit=11"])
def test_invalid_cleanup_values_rejected(setting):
    render(setting, success=False)


def test_guardrail_defaults_and_opt_in_render_all_workloads():
    for enabled in [False, True]:
        docs = render('maintenance.enabled=true', f'guardrails.enabled={str(enabled).lower()}', 'guardrails.detectors[0]=email', 'guardrails.action=redact')
        workloads = [doc for doc in docs if doc['kind'] in {'Deployment', 'CronJob', 'Job'}]
        assert len(workloads) == 3
        for workload in workloads:
            spec = workload['spec']
            if workload['kind'] == 'CronJob':
                spec = spec['jobTemplate']['spec']
            env = {item['name']: item.get('value') for item in spec['template']['spec']['containers'][0]['env']}
            assert env['GUARDRAILS_ENABLED'] == str(enabled).lower()
            assert env['GUARDRAILS_ACTION'] == 'redact'
            assert env['GUARDRAILS_DETECTORS'] == '["email"]'
            assert env['GUARDRAILS_MAX_CHARACTERS'] == '262144'


@pytest.mark.parametrize('setting', ['guardrails.detectors[0]=custom', 'guardrails.action=ignore', 'guardrails.maxCharacters=0'])
def test_invalid_guardrails_rejected(setting):
    render(setting, success=False)


def test_enrollment_chart_defaults_and_finite_opt_in():
    docs = render()
    deployment = next(x for x in docs if x['kind'] == 'Deployment')
    env = {x['name']: x.get('value') for x in deployment['spec']['template']['spec']['containers'][0]['env']}
    assert env['ENROLLMENT_ENABLED'] == 'false'
    assert env['ENROLLMENT_ALLOWED_MODELS'] == '[]'
    docs = render('enrollment.enabled=true', 'enrollment.allowedModels[0]=chat')
    deployment = next(x for x in docs if x['kind'] == 'Deployment')
    env = {x['name']: x.get('value') for x in deployment['spec']['template']['spec']['containers'][0]['env']}
    assert env['ENROLLMENT_ALLOWED_MODELS'] == '["chat"]'
    assert env['ENROLLMENT_KEY_DAYS'] == '30'


@pytest.mark.parametrize('setting', ['enrollment.enabled=true','enrollment.keyDays=91','enrollment.rpm=0','enrollment.maxPending=10001'])
def test_invalid_enrollment_policy_rejected(setting):
    render(setting, success=False)


def test_mounted_secrets_shared_by_app_migration_and_cleanup():
    docs = render('existingSecret=', 'mountedSecrets.existingSecret=install-v7',
                  'mountedSecrets.keys.OIDC_CLIENT_SECRET=oidc-client',
                  'mountedSecrets.keys.GATEWAY_MASTER_DECRYPT_KEYS=decrypt-fallbacks', 'maintenance.enabled=true',
                  'localPassword.enabled=true', 'localPassword.mfaPolicy=required')
    pods = []
    for doc in docs:
        if doc['kind'] in ('Deployment', 'Job'):
            pods.append(doc['spec']['template']['spec'])
        elif doc['kind'] == 'CronJob':
            pods.append(doc['spec']['jobTemplate']['spec']['template']['spec'])
    assert len(pods) == 3
    for pod in pods:
        assert pod['securityContext']['fsGroup'] == 10001
        container = pod['containers'][0]
        assert 'envFrom' not in container
        env = {x['name']: x['value'] for x in container['env']}
        assert env['GATEWAY_SECRETS_DIR'] == '/run/secrets/thunderbolt'
        assert env['LOCAL_PASSWORD_ENABLED'] == 'true' and env['LOCAL_MFA_POLICY'] == 'required'
        names = {'DATABASE_URL', 'GATEWAY_MASTER_KEY', 'GATEWAY_SIGNING_KEY', 'BOOTSTRAP_TOKEN',
                 'METRICS_TOKEN', 'OIDC_CLIENT_SECRET', 'GATEWAY_MASTER_DECRYPT_KEYS'}
        assert not names.intersection(env)
        for name in names:
            assert env[name + '_FILE'] == '/run/secrets/thunderbolt/' + name
        mount = next(x for x in container['volumeMounts'] if x['name'] == 'installation-secrets')
        assert mount['readOnly'] and mount['mountPath'] == env['GATEWAY_SECRETS_DIR']
        secret = next(x['secret'] for x in pod['volumes'] if x['name'] == 'installation-secrets')
        assert secret['secretName'] == 'install-v7' and secret['defaultMode'] == 0o440
        assert {x['path'] for x in secret['items']} == names
        assert next(x['key'] for x in secret['items'] if x['path'] == 'OIDC_CLIENT_SECRET') == 'oidc-client'


@pytest.mark.parametrize('settings', [
    ['mountedSecrets.existingSecret=also-set'],
    ['existingSecret='],
    ['existingSecret=', 'mountedSecrets.existingSecret=files', 'mountedSecrets.keys.UNKNOWN=x'],
    ['existingSecret=', 'mountedSecrets.existingSecret=files', 'mountedSecrets.keys.DATABASE_URL=../escape'],
    ['existingSecret=', 'mountedSecrets.existingSecret=files', 'extraEnv[0].name=DATABASE_URL', 'extraEnv[0].value=bad'],
    ['existingSecret=', 'mountedSecrets.existingSecret=files', 'extraEnv[0].name=GATEWAY_MASTER_KEY_FILE', 'extraEnv[0].value=/other'],
    ['extraEnv[0].name=LOCAL_MFA_POLICY', 'extraEnv[0].value=optional'],
    ['extraEnv[0].name=GATEWAY_SECRETS_DIR', 'extraEnv[0].value=/other'],
    ['localPassword.mfaPolicy=disabled'],
])
def test_secret_and_mfa_configuration_rejects_conflicts(settings):
    render(*settings, success=False)


def test_local_password_defaults_disabled_without_secret_volume():
    deployment = next(doc for doc in render() if doc['kind'] == 'Deployment')
    pod = deployment['spec']['template']['spec']
    env = {x['name']: x['value'] for x in pod['containers'][0]['env']}
    assert env['LOCAL_PASSWORD_ENABLED'] == 'false' and env['LOCAL_MFA_POLICY'] == 'optional'
    assert all(x['name'] != 'installation-secrets' for x in pod['volumes'])


def test_shutdown_budget_and_prestop_rendered():
    deployment = next(doc for doc in render() if doc['kind'] == 'Deployment')
    pod = deployment['spec']['template']['spec']
    container = pod['containers'][0]
    env = {x['name']: x.get('value') for x in container['env']}
    assert pod['terminationGracePeriodSeconds'] == 420
    assert env['SHUTDOWN_GRACE_SECONDS'] == '360'
    assert container['lifecycle']['preStop']['exec']['command'] == ['python', '-c', 'import time; time.sleep(10)']
    deployment = next(doc for doc in render('preStopDelaySeconds=0', 'shutdownGraceSeconds=5',
                                          'terminationGracePeriodSeconds=35') if doc['kind'] == 'Deployment')
    assert 'lifecycle' not in deployment['spec']['template']['spec']['containers'][0]


@pytest.mark.parametrize('settings', [
    ['terminationGracePeriodSeconds=399'],
    ['preStopDelaySeconds=-1'], ['preStopDelaySeconds=121'],
    ['shutdownGraceSeconds=0'], ['shutdownGraceSeconds=3601'],
    ['shutdownGraceSeconds=0.5'], ['terminationGracePeriodSeconds=4001'],
    ['extraEnv[0].name=SHUTDOWN_GRACE_SECONDS', 'extraEnv[0].value=1'],
])
def test_invalid_shutdown_budget_fails_closed(settings):
    render(*settings, success=False)


def test_azure_workload_identity_defaults_and_opt_in():
    default = next(d for d in render() if d['kind'] == 'Deployment')['spec']['template']
    assert 'azure.workload.identity/use' not in default['metadata']['labels']
    docs = render('azureWorkloadIdentity.enabled=true',
                  'azureWorkloadIdentity.allowedEndpoints[0]=https://example.openai.azure.com/openai/v1')
    deployment = next(d for d in docs if d['kind'] == 'Deployment')['spec']['template']
    assert deployment['metadata']['labels']['azure.workload.identity/use'] == 'true'
    assert deployment['spec']['automountServiceAccountToken'] is False
    env = {e['name']: e.get('value') for e in deployment['spec']['containers'][0]['env']}
    assert env['AZURE_WORKLOAD_IDENTITY_ENABLED'] == 'true'
    assert env['AZURE_WORKLOAD_IDENTITY_ALLOWED_ENDPOINTS'] == '["https://example.openai.azure.com/openai/v1"]'


@pytest.mark.parametrize('setting', [
    'azureWorkloadIdentity.enabled=true',
    'azureWorkloadIdentity.allowedEndpoints[0]=https://evil.example/openai/v1',
    'azureWorkloadIdentity.allowedEndpoints[0]=http://example.openai.azure.com/openai/v1',
])
def test_azure_workload_identity_invalid_chart_values(setting):
    render(setting, success=False)


def test_azure_workload_identity_duplicate_env_rejected():
    render('extraEnv[0].name=AZURE_WORKLOAD_IDENTITY_ENABLED', 'extraEnv[0].value=true', success=False)
