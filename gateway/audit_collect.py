"""Explicit bounded metadata-only audit collector; no external sink or scheduler."""

import argparse
import asyncio
import fcntl
import hashlib
import json
import os
import stat
import sys
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx

from .relay import RelaySettings

MAX_OUTPUT = 256 * 1024 * 1024


class CollectionError(RuntimeError):
    pass


def timestamp(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("Timezone required")
    return parsed.astimezone(timezone.utc)


def specification(gateway, since, until):
    gateway = gateway.rstrip("/")
    RelaySettings(central_url=gateway, credential="x" * 32, local_token="y" * 32)
    start, end = timestamp(since), timestamp(until)
    if not start < end <= datetime.now(timezone.utc) or end - start > timedelta(days=31):
        raise ValueError("Past window of at most 31 days required")
    return {"gateway": gateway, "since": start.isoformat(), "until": end.isoformat()}


class Output:
    """Fixed files in an owned0700 directory. Only uncommitted tail is truncated."""

    def __init__(self, directory, spec):
        self.dirfd = self.lockfd = self.fd = None
        try:
            fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
            try:
                for part in os.path.abspath(directory).split("/"):
                    if part:
                        child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                        os.close(fd)
                        fd = child
                self.dirfd = fd
            except BaseException:
                os.close(fd)
                raise
            info = os.fstat(fd)
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise CollectionError("Output directory must be owned and mode0700")
            self.lockfd = self.open("lock", os.O_RDWR | os.O_CREAT)
            fcntl.flock(self.lockfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.fd = self.open("audit.jsonl", os.O_RDWR | os.O_CREAT)
            info = os.fstat(self.fd)
            if info.st_size > MAX_OUTPUT:
                raise CollectionError("Output capacity exceeded")
            try:
                cp = self.open("checkpoint.json", os.O_RDONLY)
            except FileNotFoundError:
                if info.st_size:
                    raise CollectionError("Existing output has no checkpoint") from None
                self.state = {
                    "version": 1,
                    "spec": spec,
                    "offset": 0,
                    "hash": hashlib.sha256(b"").hexdigest(),
                    "device": info.st_dev,
                    "inode": info.st_ino,
                    "cursor": None,
                    "complete": False,
                }
                os.fsync(self.fd)
                self.save(self.state)
            else:
                try:
                    raw = os.read(cp, 16385)
                    if len(raw) > 16384:
                        raise CollectionError("Checkpoint oversized")
                    self.state = json.loads(raw)
                finally:
                    os.close(cp)
                state = self.state
                if (
                    state.get("version") != 1
                    or state.get("spec") != spec
                    or type(state.get("offset")) is not int
                    or not 0 <= state["offset"] <= info.st_size
                    or state.get("device") != info.st_dev
                    or state.get("inode") != info.st_ino
                    or type(state.get("complete")) is not bool
                    or (
                        state.get("cursor") is not None
                        and (not isinstance(state["cursor"], str) or len(state["cursor"]) > 4096)
                    )
                ):
                    raise CollectionError("Checkpoint or output binding invalid")
                self.hasher = hashlib.sha256()
                left = state["offset"]
                while left:
                    chunk = os.read(self.fd, min(left, 65536))
                    if not chunk:
                        raise CollectionError("Output truncated")
                    self.hasher.update(chunk)
                    left -= len(chunk)
                if self.hasher.hexdigest() != state.get("hash"):
                    raise CollectionError("Committed output changed")
                # Output bytes beyond last durable checkpoint are an uncommitted
                # page, potentially a partial JSON line. Do not append over it.
                os.ftruncate(self.fd, state["offset"])
                os.fsync(self.fd)
            self.hasher = getattr(self, "hasher", hashlib.sha256())
            os.lseek(self.fd, self.state["offset"], os.SEEK_SET)
        except BaseException:
            self.close()
            raise

    def open(self, name, flags):
        fd = os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=self.dirfd)
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
        ):
            os.close(fd)
            raise CollectionError("Output files must be owned, single-link, regular mode0600 files")
        return fd

    def save(self, state):
        name = ".checkpoint-" + uuid4().hex
        fd = self.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        try:
            self.write(fd, json.dumps(state, separators=(",", ":")).encode())
            os.fsync(fd)
            os.replace(name, "checkpoint.json", src_dir_fd=self.dirfd, dst_dir_fd=self.dirfd)
            os.fsync(self.dirfd)
            self.state = state
        finally:
            os.close(fd)
            try:
                os.unlink(name, dir_fd=self.dirfd)
            except FileNotFoundError:
                pass

    @staticmethod
    def write(fd, data):
        while data:
            count = os.write(fd, data)
            if count <= 0:
                raise CollectionError("Output write failed")
            data = data[count:]

    def page(self, data, cursor):
        if getattr(self, "failed", False):
            raise CollectionError("Reopen output after an interrupted write")
        if self.state["offset"] + len(data) > MAX_OUTPUT:
            raise CollectionError("Output capacity exceeded; use a smaller window")
        self.failed = True
        self.write(self.fd, data)
        os.fsync(self.fd)
        self.hasher.update(data)
        self.save(
            {
                **self.state,
                "offset": self.state["offset"] + len(data),
                "hash": self.hasher.hexdigest(),
                "cursor": cursor,
                "complete": cursor is None,
            }
        )

        self.failed = False

    def close(self):
        for name in ("fd", "lockfd", "dirfd"):
            fd = getattr(self, name, None)
            if fd is not None:
                os.close(fd)
                setattr(self, name, None)


def records(payload, spec, limit):
    if (
        not isinstance(payload, dict)
        or payload.get("kind") != "audit"
        or timestamp(payload["since"]) != timestamp(spec["since"])
        or timestamp(payload["until"]) != timestamp(spec["until"])
        or not isinstance(payload.get("records"), list)
        or len(payload["records"]) > limit
    ):
        raise CollectionError("Invalid audit export page")
    cursor = payload.get("next_cursor")
    if cursor is not None and (
        not isinstance(cursor, str) or not 1 <= len(cursor) <= 4096 or not payload["records"]
    ):
        raise CollectionError("Invalid export cursor")
    result = []
    for row in payload["records"]:
        if not isinstance(row, dict):
            raise CollectionError("Invalid audit record")
        safe = {}
        for name, maximum in [
            ("id", 36),
            ("actor_id", 128),
            ("action", 80),
            ("target_id", 128),
            ("created_at", 64),
        ]:
            value = row.get(name)
            if name == "target_id" and value is None:
                safe[name] = None
            elif isinstance(value, str) and 0 < len(value) <= maximum:
                safe[name] = value
            else:
                raise CollectionError("Invalid audit metadata")
        created = datetime.fromisoformat(safe["created_at"])
        # Gateway timestamps are UTC; SQLite serialization loses timezone info.
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        safe["created_at"] = created.astimezone(timezone.utc).isoformat()
        if not timestamp(spec["since"]) <= created < timestamp(spec["until"]):
            raise CollectionError("Audit record outside window")
        result.append(json.dumps(safe, ensure_ascii=True, separators=(",", ":")) + "\n")
    return "".join(result).encode(), cursor, len(result)


async def collect(client, spec, credential, *, output=None, stdout=None, max_pages=10, page_size=100):
    if not 1 <= max_pages <= 1000 or not 1 <= page_size <= 1000 or not credential:
        raise CollectionError("Invalid collection limits or credential")
    cursor = output.state["cursor"] if output else None
    if output and output.state["complete"]:
        return {"complete": True, "pages": 0, "records": 0}
    total = 0
    for number in range(max_pages):
        params = (
            {"limit": page_size, "cursor": cursor}
            if cursor
            else {"limit": page_size, "since": spec["since"], "until": spec["until"]}
        )
        try:
            async with asyncio.timeout(30):
                async with client.stream(
                    "GET",
                    spec["gateway"] + "/admin/api/exports/audit",
                    params=params,
                    headers={"Authorization": "Bearer " + credential, "Accept-Encoding": "identity"},
                    follow_redirects=False,
                ) as response:
                    if response.status_code != 200:
                        raise CollectionError(
                            "Audit export rejected; cursor/actor/signing key may have changed"
                        )
                    if response.headers.get("content-encoding", "").strip().lower() not in {"", "identity"}:
                        raise CollectionError("Compressed audit exports are not supported")
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > 4 * 1024 * 1024:
                            raise CollectionError("Audit page exceeds response limit")
                    data, following, count = records(json.loads(body), spec, page_size)
            if following is not None and following == cursor:
                raise CollectionError("Export cursor did not advance")
            if output:
                output.page(data, following)
            else:
                stdout.write(data.decode())
                stdout.flush()  # NOT a durable acknowledgement from a pipe consumer.
            total += count
            cursor = following
            if cursor is None:
                return {"complete": True, "pages": number + 1, "records": total}
        except Exception:
            raise CollectionError(
                "Collection stopped safely; inspect credentials, cursor and protected output before retry"
            ) from None
    return {"complete": False, "pages": max_pages, "records": total}


async def run(args):
    spec = specification(args.gateway, args.since, args.until)
    output = Output(args.output_dir, spec) if args.output_dir else None
    try:
        async with httpx.AsyncClient(timeout=30, trust_env=False, follow_redirects=False) as client:
            result = await collect(
                client,
                spec,
                os.environ.get("AUDIT_GATEWAY_KEY", ""),
                output=output,
                stdout=sys.stdout,
                max_pages=args.max_pages,
                page_size=args.page_size,
            )
        print(json.dumps(result), file=sys.stderr)
        return 0 if result["complete"] else 3
    finally:
        if output:
            output.close()


def main():
    parser = argparse.ArgumentParser(
        description="Bounded operator audit export; no external ingestion or scheduler"
    )
    parser.add_argument("--gateway", required=True)
    parser.add_argument("--since", required=True)
    parser.add_argument("--until", required=True)
    parser.add_argument(
        "--output-dir", help="Existing private mode0700 directory; otherwise JSONL stdout without checkpoint"
    )
    parser.add_argument("--max-pages", type=int, default=10)
    parser.add_argument("--page-size", type=int, default=100)
    args = parser.parse_args()
    try:
        code = asyncio.run(run(args))
    except Exception:
        print(
            "Collection incomplete; no credentials or response bodies are included in diagnostics.",
            file=sys.stderr,
        )
        code = 2
    raise SystemExit(code)


if __name__ == "__main__":
    main()
