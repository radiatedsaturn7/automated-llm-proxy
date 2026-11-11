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
