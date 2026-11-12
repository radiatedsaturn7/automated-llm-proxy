"""Explicit, bounded installation-secret files; no network or live reload."""

import os
import stat
from pathlib import Path

ALLOWED = frozenset(
    {
        "DATABASE_URL",
        "GATEWAY_MASTER_KEY",
        "GATEWAY_MASTER_DECRYPT_KEYS",
        "GATEWAY_SIGNING_KEY",
        "BOOTSTRAP_TOKEN",
        "METRICS_TOKEN",
        "OIDC_CLIENT_SECRET",
    }
)
MAX_BYTES = 16384


class SecretFileError(ValueError):
    """Messages intentionally contain neither file paths nor secret values."""


def directory(fd):
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid not in {0, os.geteuid()} or info.st_mode & 0o022:
        raise SecretFileError("Installation secret directory is not trusted")


def read_secret(root, filename):
    rootfd = fd = None
    try:
        if not root or not filename or not os.path.isabs(root) or not os.path.isabs(filename):
            raise SecretFileError("Installation secret paths must use an explicit absolute mount root")
        # The trusted root itself cannot use symlink components. Files below it
        # may use Kubernetes atomic projection symlinks, but only to targets
        # beneath this root. Walk the resolved relative target with openat and
        # O_NOFOLLOW, so a changed projection link cannot redirect the open.
        root = os.path.normpath(root)
        rootfd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        for part in Path(root).parts[1:]:
            nextfd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=rootfd)
            os.close(rootfd)
            rootfd = nextfd
        directory(rootfd)
        Path(os.path.abspath(filename)).relative_to(Path(root))
        resolved = Path(filename).resolve(strict=True)
        relative = resolved.relative_to(Path(root))
        if not relative.parts:
            raise SecretFileError("Installation secret must be a regular file")
        fd = os.dup(rootfd)
        for part in relative.parts[:-1]:
            nextfd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            try:
                directory(nextfd)
            except BaseException:
                os.close(nextfd)
                raise
            os.close(fd)
            fd = nextfd
        secretfd = os.open(relative.parts[-1], os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=fd)
        try:
            info = os.fstat(secretfd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid not in {0, os.geteuid()}
                or info.st_mode & 0o137
                or info.st_nlink != 1
                or info.st_size > MAX_BYTES
            ):
                raise SecretFileError("Installation secret file permissions or type invalid")
            data = os.read(secretfd, MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                raise SecretFileError("Installation secret exceeds size limit")
        finally:
            os.close(secretfd)
        value = data.decode("utf-8")
        if value.endswith("\r\n"):
            value = value[:-2]
        elif value.endswith("\n"):
            value = value[:-1]
        if not value.strip() or any(char in value for char in ("\r", "\n", "\x00")):
            raise SecretFileError("Installation secret must contain a nonempty single UTF-8 line")
        return value
    except SecretFileError:
        raise
    except Exception:
        raise SecretFileError("Installation secret file cannot be securely loaded") from None
    finally:
        for opened in (fd, rootfd):
            if opened is not None:
                os.close(opened)


def source(init_settings, env_settings, dotenv_settings, file_secret_settings):
    """Return a Pydantic source that rejects ambiguous competing sources."""

    def load():
        # Do not read arbitrary environment *_FILE values: unrelated tooling may
        # legitimately use that suffix. Only the published allowlist participates.
        configured = {name: os.environ[name + "_FILE"] for name in ALLOWED if name + "_FILE" in os.environ}
        if not configured:
            return {}
        # Evaluate standard sources too: init/env/.env/_secrets_dir must not
        # silently override, or be overridden by, a configured mounted file.
        try:
            competing = [
                provider()
                for provider in (init_settings, env_settings, dotenv_settings, file_secret_settings)
            ]
            if any(name.lower() in values for name in configured for values in competing):
                raise SecretFileError("Installation secret configured through conflicting sources")
            root = os.environ.get("GATEWAY_SECRETS_DIR", "")
            return {name.lower(): read_secret(root, filename) for name, filename in configured.items()}
        except SecretFileError:
            raise
        except Exception:
            raise SecretFileError("Installation secret sources are invalid") from None

    return load
