#!/usr/bin/env bash
# Build (and optionally push) the runtime image. Usage: scripts/build-runtime-image.sh [ECR_REPOSITORY_URI]
# Prints the image reference with digest, which `factory build --image` takes.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CTX="$ROOT/build/runtime-image"
rm -rf "$CTX" && mkdir -p "$CTX/wheels"
cd "$ROOT/packages"
uv export --package factory-runtime --no-dev --frozen --no-emit-workspace --no-header --python 3.12 \
  --format requirements-txt > "$CTX/requirements.txt"
for p in factory-core factory-spec factory-runtime; do uv build --package "$p" --wheel -q -o "$CTX/wheels"; done
cp "$ROOT/packages/factory_runtime/image/Dockerfile" "$CTX/Dockerfile"
TAG="${1:-factory-runtime}:$(git -C "$ROOT" rev-parse --short=12 HEAD)"
docker build --platform linux/amd64 --provenance=false -t "$TAG" "$CTX"
if [ -n "${1:-}" ]; then
  docker push "$TAG" >/dev/null
  docker inspect --format '{{index .RepoDigests 0}}' "$TAG"
else
  echo "$TAG (local only; pass an ECR repository URI to push and get a digest)"
fi
