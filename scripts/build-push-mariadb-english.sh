#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="ghcr.io/gstephan76/argo-sm-mariadb:english-literature-20-v1"
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
  -f "$ROOT/full-multi/mariadb-english/Containerfile" \
  -t "$IMAGE" \
  "$ROOT/full-multi/mariadb-english"
"$ENGINE" push "$IMAGE"
printf 'Published %s\n' "$IMAGE"
echo 'Verify the image can be pulled without credentials before committing the manifests.'
