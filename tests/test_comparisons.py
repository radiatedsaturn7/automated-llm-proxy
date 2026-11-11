import json
from sqlalchemy import select
from gateway import db


async def test_paired_gate_reports_regressions_without_retaining_text(client, admin_headers):
    body = {"cases": [{"expected": "private sentinel", "baseline": "private sentinel", "candidate": "wrong"},
                      {"expected": "ok", "baseline": "wrong", "candidate": "ok"}],
            "minimum_pass_rate": 0.5}
    result = await client.post('/admin/api/evaluation-comparisons', headers=admin_headers, json=body)
    assert result.status_code == 200, result.text
    data = result.json()
    assert data['regression_indices'] == [0]
    assert data['improvement_indices'] == [1]
    assert data['pass_rate_delta'] == 0
    assert data['gate']['accepted'] is False
    assert 'private sentinel' not in result.text
    body['maximum_regressions'] = 1
    assert (await client.post('/admin/api/evaluation-comparisons', headers=admin_headers, json=body)).json()['gate']['accepted']
    async with db.session_factory() as session:
        rows = (await session.scalars(select(db.Audit))).all()
        assert 'private sentinel' not in json.dumps([row.details for row in rows])


async def test_comparison_authorization_validation_and_noncomparison_rule(client, admin_headers):
    body = {"cases": [{"baseline": "", "candidate": "yes"}], "rules": ["nonempty"]}
    assert (await client.post('/admin/api/evaluation-comparisons', json=body)).status_code == 401
    assert (await client.post('/admin/api/evaluation-comparisons', headers=admin_headers, json=body)).json()['gate']['accepted']
    body['rules'] = ['execute_code']
    assert (await client.post('/admin/api/evaluation-comparisons', headers=admin_headers, json=body)).status_code == 400
    body['maximum_regressions'] = -1
    assert (await client.post('/admin/api/evaluation-comparisons', headers=admin_headers, json=body)).status_code == 422
