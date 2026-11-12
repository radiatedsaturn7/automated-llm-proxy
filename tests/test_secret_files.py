import os

import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError

from gateway.config import Settings, get_settings
from gateway.secret_files import ALLOWED, SecretFileError, read_secret


@pytest.fixture
def mount(tmp_path, monkeypatch):
    for name in ALLOWED:
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name + "_FILE", raising=False)
    root = tmp_path / "secrets"
    root.mkdir(mode=0o750)
    monkeypatch.setenv("GATEWAY_SECRETS_DIR", str(root))
    yield root
    get_settings.cache_clear()


def write(root, name, value, mode=0o440):
    path = root / name
    if path.exists():
        path.chmod(0o600)
    path.write_bytes(value.encode() if isinstance(value, str) else value)
    path.chmod(mode)
    return path


def configure(root, monkeypatch):
    values = {
        "GATEWAY_MASTER_KEY": Fernet.generate_key().decode(),
        "GATEWAY_SIGNING_KEY": "signing-private-" + "x" * 40,
        "BOOTSTRAP_TOKEN": "bootstrap-private-" + "y" * 40,
    }
    for name, value in values.items():
        monkeypatch.setenv(name + "_FILE", str(write(root, name, value + "\n")))
    return values


def test_settings_load_mounted_allowlist_and_env_nonsecrets(mount, monkeypatch):
    values = configure(mount, monkeypatch)
    monkeypatch.setenv("DB_POOL_SIZE", "17")
    monkeypatch.setenv("UNRELATED_TOOL_FILE", "/nonexistent")
    settings = Settings()
    assert settings.gateway_master_key == values["GATEWAY_MASTER_KEY"]
    assert settings.gateway_signing_key == values["GATEWAY_SIGNING_KEY"]
    assert settings.db_pool_size == 17


@pytest.mark.parametrize("competing", ["init", "env", "dotenv", "secrets_dir"])
def test_competing_sources_rejected_without_value_or_path(mount, monkeypatch, tmp_path, competing):
    configure(mount, monkeypatch)
    private = "never-print-this-secret-" + "q" * 40
    kwargs = {}
    if competing == "init":
        kwargs["gateway_signing_key"] = private
    elif competing == "env":
        monkeypatch.setenv("GATEWAY_SIGNING_KEY", private)
    elif competing == "dotenv":
        path = tmp_path / "sensitive-path.env"
        path.write_text("GATEWAY_SIGNING_KEY=" + private)
        kwargs["_env_file"] = str(path)
    else:
        path = tmp_path / "other-secrets"
        path.mkdir()
        (path / "gateway_signing_key").write_text(private)
        kwargs["_secrets_dir"] = str(path)
    with pytest.raises((SecretFileError, ValueError)) as error:
        Settings(**kwargs)
    assert private not in str(error.value) and str(tmp_path) not in str(error.value)


@pytest.mark.parametrize(
    "bad",
    [
        "worldread",
        "groupwrite",
        "execute",
        "fifo",
        "directory",
        "escape",
        "hardlink",
        "oversize",
        "empty",
        "multiline",
        "nul",
        "utf8",
    ],
)
def test_unsafe_secret_files_fail_closed(mount, tmp_path, bad):
    path = write(mount, "key", "secret", mode=0o600)
    if bad == "worldread":
        path.chmod(0o644)
    elif bad == "groupwrite":
        path.chmod(0o660)
    elif bad == "execute":
        path.chmod(0o700)
    elif bad in {"fifo", "directory", "escape"}:
        path.unlink()
        if bad == "fifo":
            os.mkfifo(path, 0o600)
        elif bad == "directory":
            path.mkdir(mode=0o700)
        else:
            path.symlink_to(write(tmp_path, "outside", "outside-secret"))
    elif bad == "hardlink":
        os.link(path, mount / "second")
    elif bad == "oversize":
        path.write_bytes(b"x" * 16385)
    elif bad == "empty":
        path.write_bytes(b"\n")
    elif bad == "multiline":
        path.write_bytes(b"first\nsecond")
    elif bad == "nul":
        path.write_bytes(b"secret\x00")
    elif bad == "utf8":
        path.write_bytes(b"\xff")
    with pytest.raises(SecretFileError) as error:
        read_secret(str(mount), str(path))
    assert str(path) not in str(error.value) and "outside-secret" not in str(error.value)


def test_projected_symlinks_and_startup_cache(mount, monkeypatch):
    values = configure(mount, monkeypatch)
    path = mount / "GATEWAY_SIGNING_KEY"
    path.unlink()
    for version, value in [("..v1", values["GATEWAY_SIGNING_KEY"]), ("..v2", "replacement-" + "z" * 40)]:
        target = mount / version
        target.mkdir(mode=0o750)
        write(target, "signing", value)
    (mount / "..data").symlink_to("..v1", target_is_directory=True)
    path.symlink_to("..data/signing")
    get_settings.cache_clear()
    first = get_settings()
    (mount / "..data-next").symlink_to("..v2", target_is_directory=True)
    os.replace(mount / "..data-next", mount / "..data")
    assert get_settings().gateway_signing_key == first.gateway_signing_key
    assert Settings().gateway_signing_key == "replacement-" + "z" * 40


def test_invalid_loaded_value_redacted_from_validation_error(mount, monkeypatch):
    configure(mount, monkeypatch)
    value = "short-secret"
    write(mount, "GATEWAY_SIGNING_KEY", value)
    with pytest.raises(ValidationError) as error:
        Settings()
    assert value not in str(error.value) and str(mount) not in str(error.value)


def test_optional_secrets_and_database_source(mount, monkeypatch):
    configure(mount, monkeypatch)
    for name, value in [
        ("DATABASE_URL", "sqlite+aiosqlite:///test.db"),
        ("METRICS_TOKEN", "m" * 40),
        ("OIDC_CLIENT_SECRET", "oidc-secret"),
    ]:
        monkeypatch.setenv(name + "_FILE", str(write(mount, name, value + "\r\n")))
    result = Settings()
    assert result.database_url == "sqlite+aiosqlite:///test.db"
    assert result.oidc_client_secret == "oidc-secret"


def test_symlink_root_and_missing_root_rejected(mount, tmp_path, monkeypatch):
    configure(mount, monkeypatch)
    monkeypatch.delenv("GATEWAY_SECRETS_DIR")
    with pytest.raises(SecretFileError):
        Settings()
    link = tmp_path / "rootlink"
    link.symlink_to(mount, target_is_directory=True)
    with pytest.raises(SecretFileError):
        read_secret(str(link), str(link / "GATEWAY_MASTER_KEY"))


def test_untrusted_directories_and_owner_execute_rejected(mount):
    nested = mount / 'nested'
    nested.mkdir(mode=0o750)
    path = write(nested, 'secret', 'private')
    nested.chmod(0o770)
    with pytest.raises(SecretFileError):
        read_secret(str(mount), str(path))
    nested.chmod(0o750)
    mount.chmod(0o777)
    with pytest.raises(SecretFileError):
        read_secret(str(mount), str(path))
    mount.chmod(0o750)
    path.chmod(0o540)
    with pytest.raises(SecretFileError):
        read_secret(str(mount), str(path))
