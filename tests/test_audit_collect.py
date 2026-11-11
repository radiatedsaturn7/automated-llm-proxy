import io
import json
import os
from datetime import timedelta

import httpx
import pytest

from gateway import db
from gateway.audit_collect import CollectionError, Output, collect, specification


SPEC = specification("https://gateway.example", "2020-01-01T00:00:00+00:00", "2020-01-02T00:00:00+00:00")


def page(ids, cursor=None):
    return {
        "kind": "audit",
        "since": SPEC["since"],
        "until": SPEC["until"],
        "next_cursor": cursor,
        "records": [
            {
                "id": str(i),
                "actor_id": "actor",
                "action": "key.create",
                "target_id": None,
                "created_at": "2020-01-01T01:00:00+00:00",
                "details": {"secret": "do-not-export"},
            }
            for i in ids
        ],
    }


def directory(tmp_path):
    path = tmp_path / "out"
    path.mkdir(mode=0o700)
    return path


async def test_resume_bounded_pages_and_metadata_only(tmp_path):
    path = directory(tmp_path)
    seen = []

    def handler(request):
        assert request.headers["authorization"] == "Bearer private-credential"
        seen.append(request.url.params.get("cursor"))
        return httpx.Response(200, json=page([2]) if seen[-1] else page([1], "opaque"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        output = Output(str(path), SPEC)
        assert not (await collect(client, SPEC, "private-credential", output=output, max_pages=1))["complete"]
        output.close()
        output = Output(str(path), SPEC)
        assert (await collect(client, SPEC, "private-credential", output=output))["complete"]
        assert (await collect(client, SPEC, "private-credential", output=output))["pages"] == 0
        output.close()
    assert seen == [None, "opaque"]
    text = (path / "audit.jsonl").read_text()
    assert [json.loads(line)["id"] for line in text.splitlines()] == ["1", "2"]
    assert "secret" not in text and "private-credential" not in (path / "checkpoint.json").read_text()


async def test_crash_after_output_before_checkpoint_repairs_tail(tmp_path, monkeypatch):
    path = directory(tmp_path)
    output = Output(str(path), SPEC)
    monkeypatch.setattr(output, "save", lambda _: (_ for _ in ()).throw(OSError("diskfailed")))
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=page([1])))
    ) as client:
        with pytest.raises(CollectionError):
            await collect(client, SPEC, "secret", output=output)
        assert (path / "audit.jsonl").stat().st_size > 0
        output.close()
        with open(path / "audit.jsonl", "ab") as file:
            file.write(b'{"partial":')
        output = Output(str(path), SPEC)
        assert (path / "audit.jsonl").stat().st_size == 0
        assert (await collect(client, SPEC, "secret", output=output))["complete"]
        output.close()
    assert len((path / "audit.jsonl").read_text().splitlines()) == 1


async def test_stdout_no_checkpoint_and_rejection_no_body_leaks():
    stream = io.StringIO()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=page([1], "next")))
    ) as client:
        assert not (await collect(client, SPEC, "secret", stdout=stream, max_pages=1))["complete"]
    assert "details" not in stream.getvalue()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(400, text="sensitive-upstream"))
    ) as client:
        with pytest.raises(CollectionError) as error:
            await collect(client, SPEC, "secret", stdout=stream)
        assert "sensitive-upstream" not in str(error.value)


@pytest.mark.parametrize(
    "problem", ["binding", "truncation", "modification", "symlink", "fifo", "permissions", "lock"]
)
def test_private_output_binding_and_filesystem_fail_closed(tmp_path, problem):
    path = directory(tmp_path)
    output = Output(str(path), SPEC)
    output.page(b'{"id":1}\n', "next")
    if problem == "lock":
        with pytest.raises((OSError, CollectionError)):
            Output(str(path), SPEC)
        output.close()
        return
    output.close()
    if problem == "binding":
        spec = {**SPEC, "gateway": "https://other.example"}
    else:
        spec = SPEC
    target = path / "audit.jsonl"
    if problem == "truncation":
        target.write_bytes(b"")
    if problem == "modification":
        target.write_bytes(b'{"id":2}\n')
    if problem in {"symlink", "fifo"}:
        target.unlink()
        if problem == "symlink":
            target.symlink_to(tmp_path / "outside")
        else:
            os.mkfifo(target, 0o600)
    if problem == "permissions":
        target.chmod(0o644)
    with pytest.raises((OSError, CollectionError)):
        Output(str(path), spec)


async def test_gateway_signed_cursor_integration(client, admin_headers, tmp_path):
    async with db.transaction() as s:
        for index in range(3):
            await db.audit(s, "collector-test", "test.export", str(index), not_exported="private")
    end = db.now()
    spec = specification("http://localhost:8080", (end - timedelta(days=1)).isoformat(), end.isoformat())
    path = directory(tmp_path)
    output = Output(str(path), spec)
    result = await collect(client, spec, admin_headers["Authorization"][7:], output=output, page_size=1)
    output.close()
    assert result["complete"] and result["pages"] >= 3
    rows = [json.loads(line) for line in (path / "audit.jsonl").read_text().splitlines()]
    assert len([row for row in rows if row["actor_id"] == "collector-test"]) == 3
    assert all("details" not in row for row in rows)


@pytest.mark.parametrize(
    "url", ["http://remote.example", "https://user:secret@example.com", "https://example.com?token=secret"]
)
def test_remote_url_validation(url):
    with pytest.raises(ValueError):
        specification(url, SPEC["since"], SPEC["until"])


@pytest.mark.parametrize("encoding", ["gzip", "br", "identity, gzip"])
async def test_compressed_response_rejected_before_iteration(tmp_path, encoding):
    reads = []

    class NeverRead(httpx.AsyncByteStream):
        async def __aiter__(self):
            reads.append(True)
            raise AssertionError("Compressed body must never be iterated")
            yield b""

    def handler(request):
        assert request.headers["accept-encoding"] == "identity"
        return httpx.Response(200, headers={"Content-Encoding": encoding}, stream=NeverRead())

    path = directory(tmp_path)
    output = Output(str(path), SPEC)
    original = (path / "checkpoint.json").read_bytes()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(CollectionError):
            await collect(client, SPEC, "private", output=output)
    output.close()
    assert (path / "audit.jsonl").read_bytes() == b""
    assert (path / "checkpoint.json").read_bytes() == original
    assert reads == []
