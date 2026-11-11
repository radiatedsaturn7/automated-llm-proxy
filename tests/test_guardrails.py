from dataclasses import FrozenInstanceError
import json
from types import SimpleNamespace

import pytest

from gateway.guardrails import BUILTINS, GuardrailRejected, preflight


def settings(action="block", **kwargs):
    return SimpleNamespace(guardrails_enabled=True, guardrails_action=action,
                           guardrails_detectors=json.dumps(list(BUILTINS)),
                           guardrails_max_characters=262144, **kwargs)


def chat(text):
    return {"model": "demo", "messages": [{"role": "user", "content": text}]}


@pytest.mark.parametrize("text,detector", [
    ("person@example.test", "email"),
    ("sk-" + "a" * 32, "secrets"),
    ("123-45-6789", "us_ssn"),
    ("4111 1111 1111 1111", "payment_card"),
    ("-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----", "private_key"),
])
def test_block_metadata_never_contains_match(text, detector):
    with pytest.raises(GuardrailRejected) as caught:
        preflight(chat(text), "chat", settings())
    assert caught.value.counts == {detector: 1}
    assert text not in str(caught.value)
    assert text not in repr(caught.value.__dict__)


def test_redaction_does_not_mutate_input_and_removes_truncated_key():
    source = chat("hello person@example.test")
    result = preflight(source, "chat", settings("redact"))
    assert result.payload["messages"][0]["content"] == "hello [REDACTED]"
    assert source["messages"][0]["content"].endswith("example.test")
    result = preflight(chat("-----BEGIN RSA PRIVATE KEY-----\nsecret remains"), "chat", settings("redact"))
    assert result.payload["messages"][0]["content"] == "[REDACTED]"


def test_tools_remain_valid_json_and_structural_identifiers_unchanged():
    payload = chat(None)
    payload["messages"][0]["tool_calls"] = [{"id": "call1", "type": "function", "function": {"name": "send", "arguments": json.dumps({"target": "person@example.test", "n": 3})}}]
    result = preflight(payload, "chat", settings("redact"))
    function = result.payload["messages"][0]["tool_calls"][0]["function"]
    assert function["name"] == "send"
    assert json.loads(function["arguments"]) == {"target": "[REDACTED]", "n": 3}


@pytest.mark.parametrize("value", ['{"bad":', '{"n":NaN}', '"' + "x" * 262145 + '"'])
def test_invalid_tool_arguments_fail_closed(value):
    payload = chat(None)
    payload["messages"][0]["tool_calls"] = [{"function": {"arguments": value}}]
    with pytest.raises(GuardrailRejected):
        preflight(payload, "chat", settings())


def test_embeddings_text_and_tokenized_rejection():
    assert preflight({"input": ["person@example.test"]}, "embeddings", settings("redact")).payload["input"] == ["[REDACTED]"]
    with pytest.raises(GuardrailRejected):
        preflight({"input": [1, 2]}, "embeddings", settings())


def test_limits_and_nontext_content():
    for content in ["x" * 262145, [{"type": "image_url", "image_url": {"url": "https://example.test"}}], [[[[None]]]] * 10001]:
        with pytest.raises(GuardrailRejected):
            preflight(chat(content), "chat", settings())


def test_metadata_and_sensitive_keys_block_even_redact():
    payload = chat("fine")
    payload["tools"] = [{"description": "person@example.test"}]
    with pytest.raises(GuardrailRejected):
        preflight(payload, "chat", settings("redact"))
    with pytest.raises(GuardrailRejected):
        preflight(chat({"person@example.test": "data"}), "chat", settings("redact"))


def test_disabled_identity_and_immutable_registry():
    cfg = settings()
    cfg.guardrails_enabled = False
    payload = chat("person@example.test")
    assert preflight(payload, "chat", cfg).payload is payload
    with pytest.raises(TypeError):
        BUILTINS["evil"] = None
    with pytest.raises(FrozenInstanceError):
        BUILTINS["email"].id = "evil"


def test_detector_protocol_only_spans_and_luhn_negative():
    for detector in BUILTINS.values():
        assert detector.spans("harmless text") == []
    assert BUILTINS["payment_card"].spans("4111 1111 1111 1112") == []


@pytest.mark.parametrize("detectors", ['[]', '["unknown"]', '["email","email"]', '{}', '[1]'])
def test_config_rejects_unreviewed_detectors(monkeypatch, detectors):
    from cryptography.fernet import Fernet
    from gateway.config import Settings
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        Settings(gateway_master_key=Fernet.generate_key().decode(), gateway_signing_key="s" * 40,
                 bootstrap_token="b" * 40, guardrails_detectors=detectors)


def test_nested_and_unicode_tool_content_is_valid():
    payload = chat([{"type": "text", "text": "Contact person@example.test ☃"}])
    result = preflight(payload, "chat", settings("redact"))
    assert result.payload["messages"][0]["content"][0]["text"] == "Contact [REDACTED] ☃"


@pytest.mark.parametrize("action", ["block", "redact"])
def test_legacy_function_metadata_and_numeric_identifier_fail_closed(action):
    payload = chat(None)
    payload["messages"][0]["function_call"] = {"name": "sk-" + "a" * 32, "arguments": '{}'}
    with pytest.raises(GuardrailRejected):
        preflight(payload, "chat", settings(action))
    payload["messages"][0]["function_call"] = {"name": "f", "arguments": '{"number":4111111111111111}'}
    with pytest.raises(GuardrailRejected):
        preflight(payload, "chat", settings(action))


def test_dense_redaction_preserves_every_delimiter_and_caps_metadata():
    source = "person@example.test;" * 11000
    result = preflight(chat(source), "chat", settings("redact"))
    assert result.payload["messages"][0]["content"] == "[REDACTED];" * 11000
    assert result.counts["email"] == 10000
