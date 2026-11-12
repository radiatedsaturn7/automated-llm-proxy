"""Failure-evidence regression tripwires; actual Actions execution is checked in CI."""
import json
import os
from pathlib import Path
import shlex
import subprocess


def python_job():
    return Path('.github/workflows/ci.yml').read_text().split('  python:\n', 1)[1].split('  deployments:\n', 1)[0]


def test_evidence_precedes_gates_and_survives_failure():
    job = python_job()
    assert job.index('Prepare exact-source') < job.index('id: dependencies')
    assert job.index('installed.json') < job.index('id: runtime_lock')
    assert 'if: always()\n        uses: actions/upload-artifact@' in job
    assert 'continue-on-error' not in job
    assert "if: ${{ !cancelled() && steps.dependencies.outcome == 'success' }}" in job
    assert '--output=dependency-evidence/runtime-audit.json' in job
    assert '--output=dependency-evidence/build-audit.json' in job


def test_outcome_manifest_distinguishes_failure_skips_and_success(tmp_path):
    job = python_job()
    line = next(line.strip() for line in job.splitlines() if line.strip().startswith('run: python -c '))
    command = shlex.split(line.removeprefix('run: '))
    states = dict(dependencies='success', runtime_lock='success', build_backend='failure',
                  lint='skipped', tests='skipped', dependency_audit='success')
    env = dict(os.environ, **{'EVIDENCE_' + key.upper(): value for key, value in states.items()})
    env['EVIDENCE_UNRELATED_SECRET'] = 'must-not-be-recorded'
    (tmp_path / 'dependency-evidence').mkdir()
    subprocess.run(command, cwd=tmp_path, env=env, check=True, timeout=10)
    report = json.loads((tmp_path / 'dependency-evidence/gate-outcomes.json').read_text())
    assert report == states
