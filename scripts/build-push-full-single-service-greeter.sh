#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="$(tr -d '\n' < "$ROOT/full-single-service/greeter-image.txt")"
[[ "$IMAGE" == *:configmap-v1 ]] || { echo "ERROR: expected a buildable image tag ending in :configmap-v1" >&2; exit 1; }
if command -v podman >/dev/null; then ENGINE=podman
elif command -v docker >/dev/null; then ENGINE=docker
else echo "ERROR: Install podman or docker" >&2; exit 1
fi
if [[ -n "${GHCR_TOKEN:-}" ]]; then
  printf '%s' "$GHCR_TOKEN" | "$ENGINE" login ghcr.io -u "${GHCR_USER:-gstephan76}" --password-stdin
fi
"$ENGINE" build -t "$IMAGE" -f "$ROOT/full-single-service/greeter/Containerfile" "$ROOT/full-single-service/greeter"
"$ENGINE" push "$IMAGE"
echo "Published $IMAGE"
echo "IMPORTANT: Set the GHCR package to PUBLIC (or configure an imagePullSecret) before pushing the GitOps Rollouts."
