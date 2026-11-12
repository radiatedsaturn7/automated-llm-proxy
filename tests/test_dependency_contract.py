"""Offline lock/export contracts; CI separately checks uv's complete export."""
import importlib.metadata
from pathlib import Path
import re
import tomllib

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parents[1]


def runtime_requirements():
    text = (ROOT / "requirements.lock").read_text()
    for line in text.replace("\\\n", " ").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        requirement, *hashes = line.split(" --hash=sha256:")
        yield Requirement(requirement.strip()), {value.strip() for value in hashes}


def test_runtime_export_versions_and_hashes_belong_to_uv_lock():
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    packages = {(canonicalize_name(p["name"]), p["version"]): p for p in lock["package"]}
    requirements = list(runtime_requirements())
    assert requirements
    for requirement, hashes in requirements:
        pins = list(requirement.specifier)
        assert len(pins) == 1 and pins[0].operator == "=="
        package = packages[(canonicalize_name(requirement.name), pins[0].version)]
        artifacts = package.get("wheels", []) + ([package["sdist"]] if "sdist" in package else [])
        assert hashes == {a["hash"].removeprefix("sha256:") for a in artifacts}
        assert all(re.fullmatch(r"[a-f0-9]{64}", digest) for digest in hashes)


def test_test_environment_uses_container_runtime_versions():
    for requirement, _ in runtime_requirements():
        if requirement.marker is None or requirement.marker.evaluate():
            assert importlib.metadata.version(requirement.name) in requirement.specifier, requirement.name
