import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

SPEC = importlib.util.spec_from_file_location('image_components', Path(__file__).parents[1] / 'tools/security/image_components.py')
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


def test_status_does_not_export_environment_or_arbitrary_fields():
    assert probe.proc_status('Uid:\t10001\nName:\tsecret\nCapEff:\t0\nENV:secret') == {'Uid': '10001', 'CapEff': '0'}


def test_program_absence_is_explicit(monkeypatch):
    monkeypatch.setattr(probe.shutil, 'which', lambda _: None)
    assert probe.program_info('perl') == {'present': False}
    assert probe.module_probe('Pod::Text') == {'status': 'perl-unavailable'}


def test_program_permission_evidence(monkeypatch, tmp_path):
    file = tmp_path / 'tool'
    file.write_text('fixture')
    file.chmod(0o4755)
    monkeypatch.setattr(probe.shutil, 'which', lambda _: str(file))
    info = probe.program_info('mount')
    assert info['setuid'] and not info['setgid'] and info['present']


@pytest.mark.parametrize('module', probe.MODULES)
def test_module_missing_and_loadable_are_distinct(monkeypatch, module):
    monkeypatch.setattr(probe.shutil, 'which', lambda _: '/usr/bin/perl')
    def missing(args, **kwargs):
        assert args == ['/usr/bin/perl', '-M' + module, '-e', 'exit 0']
        assert kwargs['timeout'] == 10 and not kwargs.get('shell', False)
        return SimpleNamespace(returncode=2, stderr="Can't locate " + module.replace('::', '/') + '.pm in @INC')
    monkeypatch.setattr(probe.subprocess, 'run', missing)
    assert probe.module_probe(module)['status'] == 'not-found-in-default-inc'
    monkeypatch.setattr(probe.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=0, stderr=''))
    assert probe.module_probe(module)['status'] == 'loadable'


def test_unknown_and_broken_probe_fail_closed(monkeypatch):
    with pytest.raises(ValueError):
        probe.module_probe('untrusted; command')
    monkeypatch.setattr(probe.shutil, 'which', lambda _: '/usr/bin/perl')
    monkeypatch.setattr(probe.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=1, stderr='permission denied'))
    with pytest.raises(RuntimeError):
        probe.module_probe('Pod::Text')


def test_image_probe_is_readonly_offline_and_not_an_ignore():
    workflow = (Path(__file__).parents[1] / '.github/workflows/image-security.yml').read_text()
    assert '--network none --read-only' in workflow
    assert '--mount type=bind,src="$PWD/tools/security/image_components.py",dst=/image_components.py,readonly' in workflow
    assert 'grype sbom:evidence/sbom.syft.json --fail-on high' in workflow
    assert '--only-fixed' not in workflow and '--ignore' not in workflow


@pytest.mark.parametrize('failure', [OSError('no executable'), probe.subprocess.TimeoutExpired('perl', 10)])
def test_probe_io_or_timeout_is_not_absence(monkeypatch, failure):
    monkeypatch.setattr(probe.shutil, 'which', lambda _: '/usr/bin/perl')
    def broken(*args, **kwargs):
        raise failure
    monkeypatch.setattr(probe.subprocess, 'run', broken)
    with pytest.raises(type(failure)):
        probe.module_probe('Pod::Text')


def test_missing_nested_dependency_is_not_missing_target(monkeypatch):
    monkeypatch.setattr(probe.shutil, 'which', lambda _: '/usr/bin/perl')
    monkeypatch.setattr(probe.subprocess, 'run', lambda *a, **k: SimpleNamespace(
        returncode=2, stderr="Can't locate Nested/Dependency.pm in @INC"))
    with pytest.raises(RuntimeError):
        probe.module_probe('Pod::Text')


def test_diagnostic_cannot_prevent_vulnerability_scan():
    workflow = (Path(__file__).parents[1] / '.github/workflows/image-security.yml').read_text()
    assert workflow.index('Gate known High') < workflow.index('Inspect final runtime')
    assert "if: ${{ !cancelled() && steps.image-build.outcome == 'success' }}" in workflow
    assert 'id: image-build' in workflow
