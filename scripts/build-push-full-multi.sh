#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="${FULL_MULTI_IMAGE:-ghcr.io/gstephan76/argo-sm-bookshelf:full-multi-v1}"
if command -v podman >/dev/null; then ENGINE=podman
elif command -v docker >/dev/null; then ENGINE=docker
else echo 'ERROR: podman or docker required' >&2; exit 1; fi
if [[ -n "${GHCR_TOKEN:-}" ]]; then printf '%s' "$GHCR_TOKEN" | "$ENGINE" login ghcr.io -u "${GHCR_USER:-gstephan76}" --password-stdin; fi
"$ENGINE" build -t "$IMAGE" -f "$ROOT/full-multi/bookshelf/Containerfile" "$ROOT/full-multi/bookshelf"
"$ENGINE" push "$IMAGE"
printf 'Published %s\n' "$IMAGE"
echo 'Ensure GHCR package is public before GitOps deploy.'
