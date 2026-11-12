"""Repository-local invariants for immutable actions and no persisted checkout token."""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]


def test_all_remote_workflow_actions_are_commit_pinned():
    seen = 0
    for path in (ROOT / '.github/workflows').glob('*.y*ml'):
        for match in re.finditer(r'^\s*(?:-\s*)?uses:\s*(\S+)', path.read_text(), re.M):
            action = match[1]
            if action.startswith('./'):
                continue
            assert re.fullmatch(r'[\w.-]+/[\w./-]+@[0-9a-f]{40}', action), (path, action)
            seen += 1
    assert seen >= 6


def test_every_checkout_disables_persistent_credentials():
    for path in (ROOT / '.github/workflows').glob('*.y*ml'):
        text = path.read_text()
        for match in re.finditer(r'uses: actions/checkout@[^\n]+\n(.*?)(?=\n      -|\Z)', text, re.S):
            assert 'persist-credentials: false' in match[1], path


def test_dependency_updates_are_bounded_and_not_automerged():
    config = (ROOT / '.github/dependabot.yml').read_text()
    for ecosystem in ['github-actions', 'uv', 'pip', 'npm', 'docker', 'terraform']:
        assert f'package-ecosystem: {ecosystem}\n' in config
    assert config.count('interval: weekly') == 6
    assert config.count('open-pull-requests-limit:') == 6
    for path in (ROOT / '.github/workflows').glob('*.y*ml'):
        assert '--auto' not in path.read_text()


def test_ci_container_images_are_digest_pinned():
    text = (ROOT / '.github/workflows/ci.yml').read_text()
    assert re.search(r'image: postgres:16-alpine@sha256:[0-9a-f]{64}', text)
    refs = re.findall(r'prom/prometheus:[^\s]+', text)
    assert len(refs) == 2
    assert all(re.fullmatch(r'prom/prometheus:v3\.2\.1@sha256:[0-9a-f]{64}', ref) for ref in refs)
