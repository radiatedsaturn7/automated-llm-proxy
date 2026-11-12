#!/usr/bin/env bash
set -euo pipefail
out=${1:?output directory required}
mkdir -p "$out"
helm=${HELM_BIN:-helm}
chart=deploy/helm/llm-gateway
# Render-only sentinel values. These are NOT a published image or real Secret.
common=(--namespace brrrtgate-security -f "$chart/values-security.yaml"
  --set mountedSecrets.existingSecret=ci-render-only
  --set image.digest=sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa)
"$helm" template security "$chart" "${common[@]}" > "$out/standard.yaml"
"$helm" template security-ha "$chart" -f "$chart/values-ha.yaml" "${common[@]}" \
  --set maintenance.enabled=true > "$out/ha.yaml"
