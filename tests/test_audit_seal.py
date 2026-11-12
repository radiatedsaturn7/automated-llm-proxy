import hashlib
import json
import os

import pytest

from gateway.audit_collect import Output
from gateway.audit_seal import operate


@pytest.fixture
def bundle(tmp_path):
    out, custody = tmp_path / "export", tmp_path / "custody"
    out.mkdir(mode=0o700)
    custody.mkdir(mode=0o700)
    key = custody / "key"
    key.write_bytes(os.urandom(32))
    key.chmod(0o600)
    spec = {
        "gateway": "https://gateway.example",
        "since": "2026-01-01T00:00:00+00:00",
        "until": "2026-01-02T00:00:00+00:00",
    }
    rows = [
        dict(
            id=str(i),
            actor_id="test",
            action="synthetic",
            target_id=None,
            created_at="2026-01-01T01:00:00+00:00",
        )
        for i in range(3)
    ]
    data = b"".join(json.dumps(r).encode() + b"\n" for r in rows)
    writer = Output(out, spec)
    writer.page(data, None)
    writer.close()
    return out, custody / "anchor", key


def test_roundtrip_no_overwrite_and_readonly(bundle):
    out, anchor, key = bundle
    assert operate(*bundle)["records"] == 3
    before = {p.name: p.read_bytes() for p in out.iterdir()}
    assert operate(*bundle, verify=True)["verified"]
    assert before == {p.name: p.read_bytes() for p in out.iterdir()}
    saved = anchor.read_bytes()
    with pytest.raises(FileExistsError):
        operate(*bundle)
    assert anchor.read_bytes() == saved
    assert not list(anchor.parent.glob(".seal-*"))


@pytest.mark.parametrize("mutation", ["alter", "delete", "reorder", "truncate", "append", "rollback"])
def test_export_and_forged_checkpoint_cannot_replace_anchor(bundle, mutation):
    out, anchor, key = bundle
    operate(*bundle)
    data = (out / "audit.jsonl").read_bytes()
    lines = data.splitlines(keepends=True)
    changed = {
        "alter": data.replace(b"synthetic", b"changed"),
        "delete": b"".join(lines[1:]),
        "reorder": b"".join(reversed(lines)),
        "truncate": data[:-1],
        "append": data + lines[0],
        "rollback": b"",
    }[mutation]
    (out / "audit.jsonl").write_bytes(changed)
    cp = json.loads((out / "checkpoint.json").read_bytes())
    cp.update(offset=len(changed), hash=hashlib.sha256(changed).hexdigest())
    (out / "checkpoint.json").write_text(json.dumps(cp))
    with pytest.raises(ValueError):
        operate(*bundle, verify=True)
    assert (out / "audit.jsonl").read_bytes() == changed


def test_anchor_authentication_and_wrong_key(bundle):
    out, anchor, key = bundle
    operate(*bundle)
    saved = anchor.read_bytes()
    manifest = json.loads(saved)
    manifest["snapshot"]["records"] = 0
    anchor.write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        operate(*bundle, verify=True)
    anchor.write_bytes(saved)
    key.write_bytes(os.urandom(32))
    with pytest.raises(ValueError):
        operate(*bundle, verify=True)


@pytest.mark.parametrize(
    "bad",
    ["partial", "tail", "mode", "symlink", "hardlink", "keyinside", "anchorinside", "longkey", "duplicate"],
)
def test_failclosed_inputs(bundle, bad):
    out, anchor, key = bundle
    if bad == "partial":
        p = out / "checkpoint.json"
        cp = json.loads(p.read_bytes())
        cp["complete"] = False
        p.write_text(json.dumps(cp))
    elif bad == "tail":
        with (out / "audit.jsonl").open("ab") as f:
            f.write(b"partial")
    elif bad == "mode":
        key.chmod(0o644)
    elif bad == "symlink":
        other = key.with_name("alias")
        other.symlink_to(key)
        key = other
    elif bad == "hardlink":
        os.link(key, key.with_name("alias"))
    elif bad == "keyinside":
        key = out / "key"
        key.write_bytes(os.urandom(32))
        key.chmod(0o600)
    elif bad == "anchorinside":
        anchor = out / "anchor"
    elif bad == "longkey":
        key.write_bytes(os.urandom(33))
    elif bad == "duplicate":
        p = out / "checkpoint.json"
        p.write_bytes(p.read_bytes().replace(b"{", b'{"version":1,', 1))
    before = (out / "audit.jsonl").read_bytes()
    with pytest.raises((ValueError, OSError)):
        operate(out, anchor, key)
    assert (out / "audit.jsonl").read_bytes() == before
    assert not anchor.exists()


def test_interrupted_write_never_publishes_anchor(bundle, monkeypatch):
    def fail(*args):
        raise OSError("synthetic")

    monkeypatch.setattr(os, "write", fail)
    with pytest.raises(OSError):
        operate(*bundle)
    assert not bundle[1].exists()
    assert not list(bundle[1].parent.glob(".seal-*"))


@pytest.mark.parametrize("data", [b"{}\n", b'{"id":"a","id":"b"}\n', b" " * 8193 + b"\n"])
def test_reject_malformed_or_oversized_record_with_matching_checkpoint(bundle, data):
    out, anchor, key = bundle
    (out / "audit.jsonl").write_bytes(data)
    cp = json.loads((out / "checkpoint.json").read_bytes())
    cp.update(offset=len(data), hash=hashlib.sha256(data).hexdigest())
    (out / "checkpoint.json").write_text(json.dumps(cp))
    with pytest.raises(ValueError):
        operate(*bundle)
    assert not anchor.exists()


def test_cli_static_failure_does_not_echo_secret_or_paths(bundle, monkeypatch, capsys):
    import sys

    from gateway.audit_seal import main

    out, anchor, key = bundle
    monkeypatch.setenv("AUDIT_SEAL_KEY_FILE", str(key))
    key.write_bytes(b"synthetic-secret-that-must-not-be-echoed")
    monkeypatch.setattr(
        sys, "argv", ["audit_seal", "seal", "--output-dir", str(out), "--anchor", str(anchor)]
    )
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 1
    output = capsys.readouterr().out
    assert json.loads(output)["ok"] is False
    assert "synthetic-secret" not in output and str(key) not in output


def test_collector_lock_blocks_seal(bundle):
    import fcntl

    with (bundle[0] / "lock").open("rb") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            operate(*bundle)
    assert not bundle[1].exists()


def test_empty_completed_window_is_supported(bundle):
    out, anchor, key = bundle
    (out / "audit.jsonl").write_bytes(b"")
    cp = json.loads((out / "checkpoint.json").read_bytes())
    cp.update(offset=0, hash=hashlib.sha256(b"").hexdigest())
    (out / "checkpoint.json").write_text(json.dumps(cp))
    assert operate(*bundle)["records"] == 0
    assert operate(*bundle, verify=True)["bytes"] == 0
