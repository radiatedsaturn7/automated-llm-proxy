"""Fail-fast structural checks for the committed Grafana dashboard (stdlib only)."""
import json
from pathlib import Path


def validate_dashboard(path):
    doc = json.loads(Path(path).read_text())
    assert isinstance(doc['schemaVersion'], int) and doc['schemaVersion'] >= 1
    assert isinstance(doc['title'], str) and doc['title']
    assert isinstance(doc['uid'], str) and 1 <= len(doc['uid']) <= 40
    panels = doc['panels']
    assert panels and len({p['id'] for p in panels}) == len(panels)
    for panel in panels:
        assert panel['type'] in {'timeseries', 'stat', 'table', 'gauge', 'bargauge'}
        assert panel['title'] and panel['datasource']['type'] == 'prometheus'
        assert panel['datasource']['uid'] == '${DS_PROMETHEUS}'
        assert panel['gridPos']['h'] > 0 and panel['gridPos']['w'] > 0
        targets = panel['targets']
        assert targets and len({t['refId'] for t in targets}) == len(targets)
        for target in targets:
            assert isinstance(target['expr'], str) and target['expr'].strip()
    assert any(item['name'] == 'DS_PROMETHEUS' for item in doc['__inputs'])


if __name__ == '__main__':
    validate_dashboard('deploy/monitoring/grafana-dashboard.json')
    print('Grafana dashboard structural checks passed')
