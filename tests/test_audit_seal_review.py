import hashlib
import hmac
import json
import os
import pytest
from tests.test_audit_seal import bundle as bundle
from gateway.audit_seal import operate, DOMAIN, canonical


@pytest.mark.parametrize(
    "mutation",
    [
        "extra_root",
        "extra_snapshot",
        "bool_version",
        "bool_records",
        "wrong_file",
        "wrong_format",
        "duplicate_root",
        "duplicate_snapshot",
    ],
)
def test_no_false_verified_for_noncanonical_manifest(bundle, mutation):
    out, anchor, key = bundle
    operate(*bundle)
    obj = json.loads(anchor.read_bytes())
    if mutation == "extra_root":
        obj["untrusted"] = "value"
    if mutation == "extra_snapshot":
        obj["snapshot"]["extra"] = "value"
    if mutation == "bool_version":
        obj["snapshot"]["version"] = True
    if mutation == "bool_records":
        obj["snapshot"]["records"] = True
    if mutation == "wrong_file":
        obj["snapshot"]["file"] = "other.jsonl"
    if mutation == "wrong_format":
        obj["snapshot"]["format"] = "other-v1"
    # Even authentic signatures over unsupported shapes must fail comparison.
    obj["hmac_sha256"] = hmac.new(
        key.read_bytes(), DOMAIN + canonical(obj["snapshot"]), hashlib.sha256
    ).hexdigest()
    raw = canonical(obj)
    if mutation == "duplicate_root":
        raw = raw.replace(b"{", b'{"snapshot":{},', 1)
    if mutation == "duplicate_snapshot":
        raw = raw.replace(b'"version":1', b'"version":1,"version":1')
    anchor.write_bytes(raw)
    before = {p.name: p.read_bytes() for p in out.iterdir()}
    with pytest.raises((ValueError, TypeError)):
        operate(*bundle, verify=True)
    assert before == {p.name: p.read_bytes() for p in out.iterdir()}


@pytest.mark.parametrize("name", ["audit.jsonl", "checkpoint.json", "lock"])
def test_linked_input_rejected_without_touching_target(bundle, name):
    out, anchor, key = bundle
    operate(*bundle)
    original = (out / name).read_bytes()
    os.link(out / name, key.parent / ("alias-" + name))
    with pytest.raises(ValueError):
        operate(*bundle, verify=True)
    assert (out / name).read_bytes() == original
