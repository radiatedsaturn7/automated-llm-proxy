# Official multi-platform Python 3.12 / Debian 13 image index, verified 2026-11-12.
# Review this digest and the minimum security package version together on updates.
FROM python:3.15-slim-trixie@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928 AS patched-base
# DSA-6613-1: do not silently build against the older zlib in the pinned base.
# APT verifies signed Debian metadata and package hashes with the image keyring.
RUN test -r /usr/share/keyrings/debian-archive-keyring.pgp \
    && grep -q '^Suites: trixie-security$' /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::AllowInsecureRepositories=false -o APT::Get::AllowUnauthenticated=false update \
    && apt-get -o APT::Get::AllowUnauthenticated=false install --no-install-recommends -y zlib1g \
    && dpkg --compare-versions "$(dpkg-query -W -f='${Version}' zlib1g)" ge '1:1.3.dfsg+really1.3.1-1+deb13u1' \
    && rm -rf /var/lib/apt/lists/*

FROM patched-base AS builder
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
WORKDIR /build
# Explicit application inputs: unrelated local files must not enter build layers.
COPY requirements.lock requirements-build.lock pyproject.toml LICENSE ./
COPY gateway/ ./gateway/
RUN python -m pip install --require-hashes --only-binary=:all: -r requirements-build.lock \
    && python -m pip install --prefix=/install --ignore-installed --require-hashes -r requirements.lock \
    && python -m pip install --prefix=/install --no-deps --no-build-isolation .

FROM patched-base AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    PATH=/usr/local/bin:$PATH
RUN groupadd --gid 10001 gateway && useradd --uid 10001 --gid gateway --no-create-home gateway
COPY --from=builder /install /usr/local
WORKDIR /app
USER 10001:10001
EXPOSE 8080
CMD ["python", "-m", "gateway.cli", "--host", "0.0.0.0", "--port", "8080"]
