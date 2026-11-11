#!/bin/sh
set -eu
# Disposable self-signed test CA/server. Never commit its private key.
cd "$(dirname "$0")"
mkdir -p tls
openssl req -x509 -newkey rsa:2048 -nodes -days 1 \
  -keyout tls/server.key -out tls/server.crt \
  -subj '/CN=mock-provider' \
  -addext 'subjectAltName=DNS:mock-provider' \
  -addext 'basicConstraints=critical,CA:TRUE' >/dev/null 2>&1
# Test-only key must be readable by the unprivileged mock container.
chmod 644 tls/server.key tls/server.crt
