"""Malformed upstream JSON never leaks or silently changes tool semantics."""

import httpx
import pytest
from gateway.inference import provider_json
from test_gateway import setup_inference, mock_upstream


@pytest.mark.parametrize(
    "raw",
    [
        '{"choices":[],"choices":["secret"]}',
        '{"content":[{"type":"tool_use","input":{"amount":1,"amount":2}}]}',
        '{"number":NaN}',
        '{"number":Infinity}',
        '{"number":-Infinity}',
        '{"number":1e309}',
        '{"number":-1e309}',
    ],
)
def test_strict_provider_parser(raw):
    with pytest.raises(ValueError):
        provider_json(raw)


def test_valid_provider_json():
    assert provider_json('{"n":1.25,"nested":{"n":2}}') == {"n": 1.25, "nested": {"n": 2}}


@pytest.mark.parametrize(
    "raw", ['{"secret":"hidden","secret":"other"}', '{"number":1e309}', '{"number":NaN}']
)
async def test_malformed_provider_billed_conservatively(client, admin_headers, raw):
    headers, _, _ = await setup_inference(client, admin_headers)
    await mock_upstream(lambda req: httpx.Response(200, content=raw))
    response = await client.post(
        "/v1/chat/completions",
        headers=headers,
        json={"model": "chat", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 10},
    )
    assert response.status_code == 502
    assert "hidden" not in response.text and raw not in response.text
    usage = (await client.get("/admin/api/usage", headers=admin_headers)).json()
    assert len(usage) == 1 and usage[0]["estimated"] and usage[0]["cost_micros"] > 0
