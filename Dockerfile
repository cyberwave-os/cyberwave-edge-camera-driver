# syntax=docker/dockerfile:1
# -----------------------------------------------------------------------------
# Stage 1: Debian bookworm + Python 3.11 + system OpenCV with the V4L2 backend.
#
# We deliberately install OpenCV via apt (python3-opencv) instead of the PyPI
# opencv-python wheel because the apt build is linked against libv4l and
# reports ``V4L/V4L2: YES``, which the manylinux PyPI wheel does not. Without
# V4L2 the SDK's ``cv2.VideoCapture`` for /dev/video* routes through FFmpeg's
# libavformat V4L2 demuxer, where ``cap.set(CAP_PROP_FOURCC)`` is a no-op and
# the camera silently falls back to its native default (often YUYV
# 1920x1080 @ 5 fps on USB 2.0). See CYB-1998 for the full root cause.
#
# This image is Linux-edge-only; macOS / Windows native developers should use
# ``pip install cyberwave[camera]`` directly (see the README).
#
# TODO(infra): pin by digest, e.g.
#   FROM debian:bookworm-slim@sha256:<digest> AS base
# so rebuilds are bit-for-bit reproducible. The current floating tag drifts
# whenever Debian publishes a new point release.
# -----------------------------------------------------------------------------
FROM debian:bookworm-slim AS base

WORKDIR /app

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 \
        python3-pip \
        python3-opencv \
        libv4l-0 \
        v4l-utils \
        libgl1 \
        libglib2.0-0 \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Debian 12 marks the system Python as externally-managed (PEP 668). We're
# building a single-purpose container so allowing pip to install alongside
# apt is safe — strip the marker rather than carrying --break-system-packages
# on every pip invocation.
RUN rm -f /usr/lib/python*/EXTERNALLY-MANAGED

# Fail the build immediately if the apt OpenCV does not actually carry the
# V4L2 backend. This is cheap insurance against a future debian/ubuntu
# packaging change quietly stripping V4L2, which is exactly the regression
# that this image was rebuilt to fix.
#
# Debian's python3-opencv 4.6.0 emits lowercase ``v4l/v4l2: YES``, while
# the manylinux PyPI wheels (and OpenCV's upstream Linux builds) emit
# uppercase ``V4L/V4L2: YES``. Match both with re.IGNORECASE.
RUN python3 -c "import cv2, re; info = cv2.getBuildInformation(); \
    assert re.search(r'V4L/V4L2:\s+YES', info, re.IGNORECASE), \
        'apt python3-opencv has no V4L2 backend:\n' + info; \
    print('OpenCV V4L2 backend confirmed at', cv2.__file__)"

# -----------------------------------------------------------------------------
# Stage 2: Optional RealSense — only invalidates when install_realsense_docker.sh
# or ENABLE_REALSENSE changes, not when application *.py changes.
# On amd64: pip wheel + libs; on arm64: builds librealsense from source.
# -----------------------------------------------------------------------------
FROM base AS camera-driver-base

ARG ENABLE_REALSENSE=false

COPY install_realsense_docker.sh .
RUN chmod +x install_realsense_docker.sh && \
    if [ "${ENABLE_REALSENSE}" = "true" ]; then ./install_realsense_docker.sh; \
    else echo "Skipping RealSense (build with --build-arg ENABLE_REALSENSE=true to enable)"; fi

# -----------------------------------------------------------------------------
# Stage 3: Application — copy project last so edits to code/pyproject rebuild
# only this stage.
# -----------------------------------------------------------------------------
FROM camera-driver-base AS runtime

COPY pyproject.toml .
COPY *.py ./
COPY README.md .
COPY LICENSE .

# Optional: local SDK source for pre-release CI builds (populated by CI before
# docker build). An empty sdk-local/ directory is used in production builds so
# this step is a no-op.
COPY sdk-local /tmp/sdk-local
RUN if [ -f "/tmp/sdk-local/pyproject.toml" ]; then \
        pip install "/tmp/sdk-local[camera,zenoh]"; \
    fi
RUN pip install .

# The [camera] extra pulls in opencv-python from PyPI. We want the apt-built
# python3-opencv (with V4L2) to be the cv2 that gets imported, so uninstall
# the PyPI wheel. The apt package lives at /usr/lib/python3/dist-packages
# and is owned by dpkg, so pip uninstall only removes the conflicting copy.
RUN pip uninstall -y opencv-python opencv-python-headless 2>/dev/null || true

# Final post-install assertion: cv2 still imports and still carries V4L2.
# Without this, a future SDK change that re-pulls opencv-python (e.g. via a
# transitive dep) would silently downgrade the runtime backend. Same
# lowercase/uppercase nuance as the base-stage assertion.
RUN python3 -c "import cv2, re; info = cv2.getBuildInformation(); \
    assert re.search(r'V4L/V4L2:\s+YES', info, re.IGNORECASE), \
        'Runtime cv2 lost V4L2 backend after install:\n' + info; \
    print('Runtime cv2 ready at', cv2.__file__)"

RUN mkdir -p /app/.cyberwave

COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

ENTRYPOINT ["./entrypoint.sh"]
