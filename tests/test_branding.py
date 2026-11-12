"""Display-only rebrand must not migrate installations or enrollments."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_ui_brand_and_banner():
    for name in ('index', 'teams', 'portal', 'enroll', 'local-login'):
        html = (ROOT / f'gateway/static/{name}.html').read_text()
        assert 'Brrrtgate' in html
        assert 'Thunderbolt' not in html
    html = (ROOT / 'gateway/static/index.html').read_text()
    assert '/admin/static/brrrtgate-banner.png' in html
    assert 'Brrrrt it into production' in html
    for name in ('brrrtgate', 'thunderbolt'):
        assert (ROOT / f'gateway/static/{name}-banner.png').read_bytes().startswith(b'\x89PNG\r\n\x1a\n')


def test_legacy_installation_identifiers_preserved():
    assert 'issuer=Thunderbolt' in (ROOT / 'gateway/local_mfa.py').read_text()
    assert '/run/secrets/thunderbolt' in (ROOT / 'deploy/helm/llm-gateway/templates/_helpers.tpl').read_text()
    assert '"uid": "thunderbolt-overview"' in (ROOT / 'deploy/monitoring/grafana-dashboard.json').read_text()
    assert 'name = "automated-llm-proxy"' in (ROOT / 'pyproject.toml').read_text()
