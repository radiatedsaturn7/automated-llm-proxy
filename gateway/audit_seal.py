"""Offline authenticated snapshots of completed audit exports, not source attestations."""

import argparse
import fcntl
import hashlib
import hmac
import json
import os
import stat
from contextlib import contextmanager
from uuid import uuid4

from .audit_collect import MAX_OUTPUT, records, specification

DOMAIN = b"thunderbolt/audit-seal/v1\x00"
MAX_LINE = 8192


class SealError(ValueError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SealError("Duplicate JSON member")
        result[key] = value
    return result


def parse(raw):
    return json.loads(raw, object_pairs_hook=unique)


@contextmanager
def directory(path):
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in os.path.abspath(path).split("/"):
            if part:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise SealError("Private owned directory required")
        yield fd
    finally:
        os.close(fd)


@contextmanager
def regular(dirfd, name, flags=os.O_RDONLY):
    fd = os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=dirfd)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
        ):
            raise SealError("Private owned single-link file required")
        yield fd
    finally:
        os.close(fd)


def small(fd, maximum):
    raw = os.read(fd, maximum + 1)
    if len(raw) > maximum:
        raise SealError("Input exceeds limit")
    return raw


def external(output, path):
    output, path = os.path.abspath(output), os.path.abspath(path)
    if os.path.commonpath([output, path]) == output:
        raise SealError("Key and anchor must be outside export directory")
    return os.path.split(path)


def snapshot(dirfd):
    with regular(dirfd, "checkpoint.json") as fd:
        cp = parse(small(fd, 16384))
    if (
        not isinstance(cp, dict)
        or type(cp.get("version")) is not int
        or cp.get("version") != 1
        or cp.get("complete") is not True
        or cp.get("cursor") is not None
    ):
        raise SealError("Completed collector checkpoint required")
    spec = cp.get("spec")
    if not isinstance(spec, dict) or set(spec) != {"gateway", "since", "until"}:
        raise SealError("Invalid window")
    if specification(**spec) != spec:
        raise SealError("Noncanonical window")
    with regular(dirfd, "audit.jsonl") as fd:
        before = os.fstat(fd)
        if (
            type(cp.get("offset")) is not int
            or not 0 <= before.st_size <= MAX_OUTPUT
            or cp["offset"] != before.st_size
            or cp.get("device") != before.st_dev
            or cp.get("inode") != before.st_ino
        ):
            raise SealError("Checkpoint binding or length mismatch")
        hasher, count, total = hashlib.sha256(), 0, 0
        with os.fdopen(os.dup(fd), "rb") as source:
            while line := source.readline(MAX_LINE + 1):
                if len(line) > MAX_LINE or not line.endswith(b"\n"):
                    raise SealError("Invalid JSONL record boundary")
                row = parse(line)
                if not isinstance(row, dict) or set(row) != {
                    "id",
                    "actor_id",
                    "action",
                    "target_id",
                    "created_at",
                }:
                    raise SealError("Invalid record")
                records(
                    {
                        "kind": "audit",
                        "since": spec["since"],
                        "until": spec["until"],
                        "records": [row],
                        "next_cursor": None,
                    },
                    spec,
                    1,
                )
                total += len(line)
                if total > MAX_OUTPUT:
                    raise SealError("Output exceeds limit")
                hasher.update(line)
                count += 1
        after = os.fstat(fd)
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise SealError("Output changed while reading")
        if total != cp["offset"] or hasher.hexdigest() != cp.get("hash"):
            raise SealError("Checkpoint content mismatch")
    return {
        "version": 1,
        "format": "collector-audit-jsonl-v1",
        "file": "audit.jsonl",
        "spec": spec,
        "bytes": total,
        "records": count,
        "sha256": hasher.hexdigest(),
    }


def operate(output, anchor, key_path, *, verify=False):
    anchor_dir, anchor_name = external(output, anchor)
    key_dir, key_name = external(output, key_path)
    if os.path.abspath(anchor) == os.path.abspath(key_path):
        raise SealError("Distinct key and anchor required")
    with directory(key_dir) as key_parent, regular(key_parent, key_name) as fd:
        key = small(fd, 32)
        if len(key) != 32:
            raise SealError("Exactly 32 random key bytes required")
    with directory(output) as out, directory(anchor_dir) as target:
        with regular(out, "lock", os.O_RDWR) as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if verify:
                with regular(target, anchor_name) as fd:
                    manifest = parse(small(fd, 16384))
                if not isinstance(manifest, dict) or set(manifest) != {"snapshot", "hmac_sha256"}:
                    raise SealError("Invalid anchor")
                mac = manifest["hmac_sha256"]
                if not isinstance(mac, str) or not hmac.compare_digest(
                    mac, hmac.new(key, DOMAIN + canonical(manifest["snapshot"]), hashlib.sha256).hexdigest()
                ):
                    raise SealError("Anchor authentication failed")
                observed = snapshot(out)
                if canonical(observed) != canonical(manifest["snapshot"]):
                    raise SealError("Snapshot differs from anchor")
            else:
                observed = snapshot(out)
                manifest = {
                    "snapshot": observed,
                    "hmac_sha256": hmac.new(key, DOMAIN + canonical(observed), hashlib.sha256).hexdigest(),
                }
                temp = ".seal-" + uuid4().hex
                try:
                    with regular(target, temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL) as fd:
                        data = canonical(manifest) + b"\n"
                        while data:
                            n = os.write(fd, data)
                            if n <= 0:
                                raise SealError("Anchor write failed")
                            data = data[n:]
                        os.fsync(fd)
                    # Atomic publication without replacing any pre-existing anchor.
                    os.link(temp, anchor_name, src_dir_fd=target, dst_dir_fd=target, follow_symlinks=False)
                finally:
                    try:
                        os.unlink(temp, dir_fd=target)
                    except FileNotFoundError:
                        pass
                os.fsync(target)
    return {"ok": True, "verified": verify, "records": observed["records"], "bytes": observed["bytes"]}


def main():
    parser = argparse.ArgumentParser(
        description="Seal or verify completed local audit export; no network calls"
    )
    parser.add_argument("action", choices=["seal", "verify"])
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--anchor", required=True)
    args = parser.parse_args()
    try:
        result = operate(
            args.output_dir, args.anchor, os.environ["AUDIT_SEAL_KEY_FILE"], verify=args.action == "verify"
        )
    except Exception:
        print(
            json.dumps(
                {"ok": False, "reason": "Audit snapshot could not be authenticated; inspect protected files"}
            )
        )
        raise SystemExit(1) from None
    print(json.dumps(result))


if __name__ == "__main__":
    main()
