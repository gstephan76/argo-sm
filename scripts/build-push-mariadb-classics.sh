#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="${MARIADB_CLASSICS_IMAGE:-ghcr.io/gstephan76/argo-sm-mariadb:classicos-brasileiros-20-v1}"
if command -v podman >/dev/null 2>&1; then
    ENGINE=podman
elif command -v docker >/dev/null 2>&1; then
    ENGINE=docker
else
    echo 'ERROR: podman or docker required' >&2
    exit 1
fi
if [[ -n "${GHCR_TOKEN:-}" ]]; then
    printf '%s' "$GHCR_TOKEN" | "$ENGINE" login ghcr.io \
      -u "${GHCR_USER:-gstephan76}" --password-stdin
fi
"$ENGINE" build \
  -f "$ROOT/full-multi/mariadb-classics/Containerfile" \
  -t "$IMAGE" \
  "$ROOT/full-multi/mariadb-classics"
"$ENGINE" push "$IMAGE"
printf 'Published %s\n' "$IMAGE"
echo 'Make the GHCR package public (or configure imagePullSecrets) before committing Rollout image changes.'
