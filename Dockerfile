# -----------------------------------------------------------------------------
# Stage 1: Python + system libs for OpenCV (stable; rarely changes)
# -----------------------------------------------------------------------------
FROM python:3.12-slim AS base

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

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
# Stage 3: Application — copy project last so edits to code/pyproject rebuild only here
# -----------------------------------------------------------------------------
FROM camera-driver-base AS runtime

COPY pyproject.toml .
COPY *.py ./
COPY README.md .
COPY LICENSE .
RUN pip install --no-cache-dir .

RUN mkdir -p /app/.cyberwave

COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

ENTRYPOINT ["./entrypoint.sh"]
