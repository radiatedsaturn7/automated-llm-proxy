"""Guard publication boundaries without running the remote scanner in tests."""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]


def test_scorecard_only_publishes_trusted_default_branch():
    source = (ROOT / '.github/workflows/scorecard.yml').read_text()
    assert 'branches: [main]' in source
    assert 'schedule:' in source
    assert 'pull_request:' not in source
    assert 'pull_request_target:' not in source
    assert 'workflow_dispatch:' not in source
    assert not re.search(r'^\s+(run|env|defaults|services|container):', source, re.M)
    assert 'persist-credentials: false' in source
    assert 'repo_token:' not in source
    assert 'permissions: read-all' in source
    assert 'contents: read' in source
    assert 'id-token: write' in source
    assert 'security-events: write' in source


def test_scorecard_actions_are_pinned_and_evidence_retained():
    source = (ROOT / '.github/workflows/scorecard.yml').read_text()
    uses = re.findall(r'uses: (\S+)', source)
    assert len(uses) == 4
    assert all(re.fullmatch(r'[\w/-]+@[a-f0-9]{40}', item) for item in uses)
    assert 'publish_results: true' in source
    assert source.count('if: always()') == 2
    assert 'retention-days: 90' in source
    assert 'if-no-files-found: error' in source
