#!/usr/bin/env bash
# Build cyberwaveos/camera-driver:dev (standard variant) like CI publish.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
SDK_SOURCE_DIR="$REPO_ROOT/cyberwave-sdks/cyberwave-python"
SDK_CONTEXT_DIR="$SCRIPT_DIR/sdk-local"
IMAGE="${IMAGE:-cyberwaveos/camera-driver:dev}"
ENABLE_REALSENSE="${ENABLE_REALSENSE:-false}"
PLATFORM="${PLATFORM:-}"

cleanup() {
  rm -rf "$SDK_CONTEXT_DIR"
}
trap cleanup EXIT

mkdir -p "$SDK_CONTEXT_DIR"
if [ -f "$SDK_SOURCE_DIR/pyproject.toml" ]; then
  echo "[INFO] Bundling in-tree SDK into sdk-local/"
  cp -r "$SDK_SOURCE_DIR/." "$SDK_CONTEXT_DIR/"
else
  echo "[WARN] SDK not found — build uses PyPI cyberwave only"
fi

VERSION="$(grep -m1 '^version' "$SCRIPT_DIR/pyproject.toml" | sed 's/.*"\(.*\)".*/\1/')"
BUILD_DATE="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
VCS_REF="$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo local)"

echo "[INFO] Building $IMAGE (ENABLE_REALSENSE=$ENABLE_REALSENSE)"
build_args=(
  --build-arg "ENABLE_REALSENSE=$ENABLE_REALSENSE"
  --build-arg "VERSION=$VERSION"
  --build-arg "BUILD_DATE=$BUILD_DATE"
  --build-arg "VCS_REF=$VCS_REF"
  -t "$IMAGE"
)
if [ -n "$PLATFORM" ]; then
  docker build --platform "$PLATFORM" "${build_args[@]}" "$SCRIPT_DIR"
else
  docker build "${build_args[@]}" "$SCRIPT_DIR"
fi

echo "[PASS] Built $IMAGE"
echo "[INFO] RealSense variant: ENABLE_REALSENSE=true IMAGE=cyberwaveos/camera-driver:dev-realsense $0"
