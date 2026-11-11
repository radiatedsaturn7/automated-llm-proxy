"""Opt-in POSIX durable root credential journal. Contains plaintext secrets."""
import fcntl
import json
import os
import secrets
import stat
import time
import uuid

import httpx


class RotationError(RuntimeError):
    pass


def valid_secret(value):
    return isinstance(value, str) and len(value) == 46 and value.startswith("lg_") and all(
        c in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-" for c in value[3:])


class CredentialJournal:
    def __init__(self, directory, central_url, initial_credential):
        self.dirfd = self.lockfd = None
        self.central_url = central_url.rstrip("/")
        try:
            # Walk with openat/O_NOFOLLOW instead of resolving symlinks. The final
            # directory must already exist, be owned by this uid, and be private.
            path = os.path.abspath(directory)
            fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
            try:
                for part in path.split("/"):
                    if not part:
                        continue
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    os.close(fd)
                    fd = child
                self.dirfd = fd
            except BaseException:
                os.close(fd)
                raise
            info = os.fstat(self.dirfd)
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise RotationError("Journal directory must be owned by current user and mode 0700")
            self.lockfd = self._open("lock", os.O_RDWR | os.O_CREAT)
            fcntl.flock(self.lockfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                statefd = self._open("state.json", os.O_RDONLY)
            except FileNotFoundError:
                if not valid_secret(initial_credential):
                    raise RotationError("Initial agent root credential required") from None
                self.state = {"version": 1, "central_url": self.central_url, "current": initial_credential,
                              "pending": None, "last_rotated": time.time(), "key_id": None}
                self._write(self.state)
            else:
                try:
                    data = os.read(statefd, 16385)
                    if len(data) > 16384:
                        raise RotationError("Journal oversized")
                    self.state = json.loads(data)
                finally:
                    os.close(statefd)
                self._validate()
        except BaseException:
            self.close()
            raise

    def _open(self, name, flags):
        fd = os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=self.dirfd)
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600):
            os.close(fd)
            raise RotationError("Journal files must be owned, regular, single-link and mode 0600")
        return fd

    def _validate(self):
        state = self.state
        if (not isinstance(state, dict) or state.get("version") != 1 or state.get("central_url") != self.central_url
                or not valid_secret(state.get("current")) or not isinstance(state.get("last_rotated"), (int, float))
                or not 0 <= state["last_rotated"] < 10**12):
            raise RotationError("Invalid journal or gateway mismatch")
        pending = state.get("pending")
        if pending is not None:
            if not isinstance(pending, dict) or not valid_secret(pending.get("credential")):
                raise RotationError("Invalid pending journal")
            if pending["credential"] == state["current"]:
                raise RotationError("Pending secret cannot equal current secret")
            uuid.UUID(pending["operation_id"])

    def _write(self, state):
        temporary = ".state-" + uuid.uuid4().hex
        fd = self._open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        try:
            data = json.dumps(state, separators=(",", ":")).encode()
            while data:
                count = os.write(fd, data)
                if count <= 0:
                    raise RotationError("Journal write failed")
                data = data[count:]
            os.fsync(fd)
            os.replace(temporary, "state.json", src_dir_fd=self.dirfd, dst_dir_fd=self.dirfd)
            os.fsync(self.dirfd)
        finally:
            os.close(fd)
            try:
                os.unlink(temporary, dir_fd=self.dirfd)
            except FileNotFoundError:
                pass
        self.state = state

    def close(self):
        for name in ("lockfd", "dirfd"):
            fd = getattr(self, name, None)
            if fd is not None:
                os.close(fd)
                setattr(self, name, None)

    def _completed(self, payload):
        pending = self.state["pending"]
        if (not isinstance(payload, dict) or payload.get("state") != "committed"
                or payload.get("operation_id") != pending["operation_id"]
                or not isinstance(payload.get("key_id"), str) or type(payload.get("generation")) is not int
                or payload["generation"] < 1
                or (self.state["key_id"] is not None and self.state["key_id"] != payload["key_id"])):
            raise RotationError("Invalid rotation acknowledgement")
        self._write({**self.state, "current": pending["credential"], "pending": None,
                     "key_id": payload["key_id"], "last_rotated": time.time()})

    async def credential(self, client, interval):
        try:
            if self.state["pending"] is None:
                if time.time() - self.state["last_rotated"] < interval:
                    return self.state["current"]
                # This fsync must succeed before prepare/commit can see the secret.
                self._write({**self.state, "pending": {"operation_id": str(uuid.uuid4()),
                                                     "credential": "lg_" + secrets.token_urlsafe(32)}})
            pending = self.state["pending"]
            operation = {"operation_id": pending["operation_id"]}
            url = self.central_url + "/auth/agent/rotation"
            response = await client.post(url + "/commit", json=operation,
                headers={"Authorization": "Bearer " + pending["credential"]}, follow_redirects=False)
            if response.status_code == 200:
                self._completed(response.json())
                return self.state["current"]
            if response.status_code == 410:
                # Definite server expiry: pending never replaced current. Persist
                # discard before starting a new operation on a subsequent call.
                self._write({**self.state, "pending": None})
                raise RotationError("Pending rotation expired; retry will prepare a new operation")
            if response.status_code != 401:
                raise RotationError("Rotation recovery rejected; pending journal retained")
            response = await client.post(url + "/prepare", json={**operation, "next_credential": pending["credential"]},
                headers={"Authorization": "Bearer " + self.state["current"]}, follow_redirects=False)
            if response.status_code == 410:
                self._write({**self.state, "pending": None})
                raise RotationError("Pending rotation expired; retry will prepare a new operation")
            if response.status_code != 200:
                raise RotationError("Rotation prepare rejected; pending journal retained")
            response = await client.post(url + "/commit", json=operation,
                headers={"Authorization": "Bearer " + pending["credential"]}, follow_redirects=False)
            if response.status_code != 200:
                raise RotationError("Rotation commit rejected; pending journal retained")
            self._completed(response.json())
            return self.state["current"]
        except (httpx.HTTPError, ValueError, KeyError, TypeError, OSError) as error:
            raise RotationError("Rotation unavailable; journal retained for recovery") from error
