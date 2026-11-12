"""Structural contract; PromQL behavior is tested by promtool in deployment CI."""
import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_overload_panels_have_fixed_aggregations_and_distinct_positions():
    document = json.loads((ROOT / 'deploy/monitoring/grafana-dashboard.json').read_text())
    panels = {p['id']: p for p in document['panels']}
    assert len(panels) == len(document['panels'])
    active, rejected = panels[5], panels[6]
    assert active['targets'][0]['expr'] == 'sum by (pool) (gateway_inflight_requests{job="thunderbolt"})'
    assert rejected['targets'][0]['expr'] == 'sum by (pool, reason) (rate(gateway_overload_rejections_total{job="thunderbolt"}[5m]))'
    assert active['targets'][0]['legendFormat'] == '{{pool}}'
    assert rejected['targets'][0]['legendFormat'] == '{{pool}} / {{reason}}'
    assert active['gridPos'] != rejected['gridPos']


def test_overload_rules_and_behavior_tests_are_wired():
    rules = yaml.safe_load((ROOT / 'deploy/monitoring/prometheus-alerts.yaml').read_text())
    alerts = {rule['alert']: rule for group in rules['groups'] for rule in group['rules']}
    for name, pool, threshold in [('ThunderboltApplicationOverload', 'application', 20),
                                  ('ThunderboltOperationsOverload', 'operations', 5)]:
        rule = alerts[name]
        assert rule['for'] == '5m'
        assert rule['expr'] == (f'sum by (instance) (increase(gateway_overload_rejections_total{{job="thunderbolt",'
                                f'pool="{pool}"}}[5m])) > {threshold}')
    tests = yaml.safe_load((ROOT / 'deploy/monitoring/prometheus-alerts.test.yaml').read_text())
    assert tests['rule_files'] == ['prometheus-alerts.yaml']
    assert len(tests['tests']) == 8
    workflow = (ROOT / '.github/workflows/ci.yml').read_text()
    assert 'test rules /rules/prometheus-alerts.test.yaml' in workflow
