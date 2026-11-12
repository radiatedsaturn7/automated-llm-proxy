"""Static regression contract; CI builds/scans the actual Docker image separately."""
import pathlib
import re


def test_builder_and_runtime_share_patched_pinned_base():
    dockerfile = pathlib.Path('Dockerfile').read_text()
    assert re.search(r'^FROM python:3\.12-slim-trixie@sha256:[a-f0-9]{64} AS patched-base$', dockerfile, re.M)
    assert 'FROM patched-base AS builder' in dockerfile
    assert 'FROM patched-base AS runtime' in dockerfile
    assert dockerfile.count('FROM python:') == 1
    assert 'Acquire::AllowInsecureRepositories=false' in dockerfile
    assert 'APT::Get::AllowUnauthenticated=false' in dockerfile
    assert 'trixie-security' in dockerfile
    assert '/usr/share/keyrings/debian-archive-keyring.pgp' in dockerfile
    assert 'dpkg --compare-versions' in dockerfile
    assert "ge '1:1.3.dfsg+really1.3.1-1+deb13u1'" in dockerfile
    assert '--allow-unauthenticated' not in dockerfile
    assert 'trusted=yes' not in dockerfile


def test_builder_only_copies_declared_application_inputs():
    # A blacklist cannot anticipate local credentials with arbitrary names.
    # Keep COPY explicit even when .dockerignore also excludes common secrets.
    dockerfile = pathlib.Path("Dockerfile").read_text()
    builder = dockerfile.split("FROM patched-base AS builder", 1)[1].split(
        "FROM patched-base AS runtime", 1
    )[0]
    copies = [line.strip() for line in builder.splitlines() if line.strip().startswith("COPY ")]
    assert copies == [
        "COPY requirements.lock requirements-build.lock pyproject.toml LICENSE ./",
        "COPY gateway/ ./gateway/",
    ]
    assert not any(line.strip().startswith("ADD ") for line in dockerfile.splitlines())
    assert 'COPY --from=builder /install /usr/local' in dockerfile


def test_build_backend_is_hash_locked_and_not_copied_to_runtime():
    dockerfile = pathlib.Path("Dockerfile").read_text()
    assert "pip install --require-hashes --only-binary=:all: -r requirements-build.lock" in dockerfile
    assert "pip install --prefix=/install --no-deps --no-build-isolation ." in dockerfile
    runtime = dockerfile.split("FROM patched-base AS runtime", 1)[1]
    assert "COPY --from=builder /install /usr/local" in runtime
    assert "COPY --from=builder /usr/local" not in runtime


def test_build_input_matches_project_backend_requirements():
    import tomllib
    metadata = tomllib.loads(pathlib.Path("pyproject.toml").read_text())
    requirements = [line for line in pathlib.Path("requirements-build.in").read_text().splitlines()
                    if line and not line.startswith("#")]
    assert requirements == metadata["build-system"]["requires"]
