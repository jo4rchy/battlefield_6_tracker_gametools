#!/usr/bin/env bash
# ----------------------------------------------------------------------------
# build.sh — build, tag, and export the bf6-tracker docker image.
#
# Reads the version from `app/__init__.py.__version__` so bumping that one
# line is the only thing required to cut a release. Produces:
#
#   * docker image tagged   bf6-tracker:<version>
#   * docker image tagged   bf6-tracker:latest         (also)
#   * tarball               bf6-tracker-amd64-v<version>.tar
#
# Usage:
#   ./build.sh                 # build + save tarball (default)
#   ./build.sh --no-tar        # build + tag only, skip the tarball
#   ./build.sh --push <repo>   # build + push <repo>:<version> (no tarball)
# ----------------------------------------------------------------------------

set -euo pipefail

# Resolve the repo root (the directory containing this script) and cd there
# so paths work no matter where the user invoked the script from.
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

# --- derive version from the single source of truth -------------------------
# Use python because that's the only thing guaranteed to be installed if the
# rest of the project is checked out. Fail loud if the import fails.
VERSION="$(python3 -c 'import sys; sys.path.insert(0, "."); from app import __version__; print(__version__)')"
if [ -z "$VERSION" ]; then
  echo "[build.sh] ERROR: could not read app.__version__" >&2
  exit 1
fi

IMAGE="bf6-tracker"
PLATFORM="linux/amd64"   # NAS is x86; change if you ever target arm64
TARBALL="bf6-tracker-amd64-v${VERSION}.tar"

echo "[build.sh] version = ${VERSION}"
echo "[build.sh] image   = ${IMAGE}:${VERSION}  (+ :latest)"
echo "[build.sh] tarball = ${TARBALL}"

# --- buildx setup (idempotent — safe to re-run) -----------------------------
if ! docker buildx inspect bf6builder >/dev/null 2>&1; then
  echo "[build.sh] creating buildx builder 'bf6builder'..."
  docker buildx create --name bf6builder --use >/dev/null
fi
docker buildx use bf6builder >/dev/null
docker buildx inspect --bootstrap >/dev/null

# --- decide what to do (tar / push / image-only) ----------------------------
MODE="tar"
PUSH_REPO=""
case "${1:-}" in
  --no-tar)  MODE="image"; ;;
  --push)    MODE="push"; PUSH_REPO="${2:-}"; if [ -z "$PUSH_REPO" ]; then
               echo "[build.sh] --push requires a registry/repo argument"; exit 2
             fi ;;
  "" )       ;;
  * )        echo "[build.sh] unknown option: $1"; exit 2 ;;
esac

# Common build args. --no-cache so a version bump always re-runs pip and the
# COPY app/ layer; we don't want a stale layer to ship inside the new tag.
BUILD_ARGS=(
  --no-cache
  --platform "$PLATFORM"
  --build-arg "BF6_VERSION=${VERSION}"
  -t "${IMAGE}:${VERSION}"
  -t "${IMAGE}:latest"
)

case "$MODE" in
  push)
    echo "[build.sh] building + pushing to ${PUSH_REPO}:${VERSION}..."
    docker buildx build "${BUILD_ARGS[@]}" \
      -t "${PUSH_REPO}:${VERSION}" \
      -t "${PUSH_REPO}:latest" \
      --push .
    ;;
  image|tar)
    echo "[build.sh] building local image..."
    docker buildx build "${BUILD_ARGS[@]}" --load .
    if [ "$MODE" = "tar" ]; then
      echo "[build.sh] saving tarball -> ${TARBALL}"
      docker save "${IMAGE}:${VERSION}" -o "${TARBALL}"
      ls -lh "${TARBALL}"
    fi
    ;;
esac

echo "[build.sh] done."
