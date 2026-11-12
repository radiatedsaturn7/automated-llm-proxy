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
