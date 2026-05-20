#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
IMAGE_NAME="${IMAGE_NAME:-cyberwave-edge-camera-driver:e2e-test}"
SDK_SOURCE_DIR="$REPO_ROOT/cyberwave-sdks/cyberwave-python"
SDK_CONTEXT_DIR="$SCRIPT_DIR/sdk-local"
ENABLE_REALSENSE="${ENABLE_REALSENSE:-false}"

cleanup_sdk_context() {
  rm -rf "$SDK_CONTEXT_DIR"
}
trap cleanup_sdk_context EXIT

mkdir -p "$SDK_CONTEXT_DIR"
if [ -f "$SDK_SOURCE_DIR/pyproject.toml" ]; then
  echo "[INFO] Bundling in-tree SDK into sdk-local/ for Docker build"
  cp -r "$SDK_SOURCE_DIR/." "$SDK_CONTEXT_DIR/"
else
  echo "[WARN] SDK not found at $SDK_SOURCE_DIR; using sdk-local/.gitkeep only"
fi

echo "[INFO] Building Docker image: $IMAGE_NAME (ENABLE_REALSENSE=$ENABLE_REALSENSE)"
docker build \
  --build-arg ENABLE_REALSENSE="$ENABLE_REALSENSE" \
  --build-arg BUILDKIT_INLINE_CACHE=1 \
  -t "$IMAGE_NAME" \
  "$SCRIPT_DIR"

echo "[INFO] Verifying image (V4L2 OpenCV + entrypoint)"
docker run --rm --entrypoint bash "$IMAGE_NAME" -lc '
  set -e
  test -x /app/entrypoint.sh
  python3 -c "
import cv2, re
info = cv2.getBuildInformation()
assert re.search(r\"V4L/V4L2:\s+YES\", info, re.IGNORECASE), \"missing V4L2 backend\"
print(\"OpenCV V4L2 OK at\", cv2.__file__)
"
  python3 -c "import main; print(\"main module OK\")"
'

echo "[INFO] Running camera driver unit tests inside container"
docker run --rm --entrypoint bash \
  -v "$SCRIPT_DIR/tests:/app/tests:ro" \
  "$IMAGE_NAME" \
  -lc "pip install --quiet pytest pytest-asyncio && cd /app && pytest tests -q"

echo "[PASS] Camera driver Docker E2E smoke test passed."
