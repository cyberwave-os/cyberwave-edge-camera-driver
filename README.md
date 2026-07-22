<p align="center">
  <a href="https://cyberwave.com">
    <img src="https://cyberwave.com/cyberwave-logo-black.svg" alt="Cyberwave logo" width="240" />
  </a>
</p>

# Cyberwave Camera Driver

This module is part of **Cyberwave: Making the physical world programmable**.

[![License](https://img.shields.io/badge/License-Apache%202.0-orange.svg)](https://opensource.org/licenses/Apache-2.0)
[![Documentation](https://img.shields.io/badge/Documentation-docs.cyberwave.com-orange)](https://docs.cyberwave.com)
[![Discord](https://badgen.net/badge/icon/discord?icon=discord&label&color=orange)](https://discord.gg/dfGhNrawyF)
[![PyPI version](https://img.shields.io/pypi/v/cyberwave-edge-camera-driver.svg)](https://pypi.org/project/cyberwave-edge-camera-driver/)
[![PyPI Python versions](https://img.shields.io/pypi/pyversions/cyberwave-edge-camera-driver.svg)](https://pypi.org/project/cyberwave-edge-camera-driver/)
[![Docker Build](https://github.com/cyberwave-os/cyberwave-edge-camera-driver/actions/workflows/push-to-docker-hub.yml/badge.svg)](https://github.com/cyberwave-os/cyberwave-edge-camera-driver/actions/workflows/push-to-docker-hub.yml)

A Cyberwave edge driver that streams a USB, IP/RTSP, or depth camera feed to a digital twin.

Launched automatically by `cyberwave-edge-core` when a twin's metadata references this driver image.

## Platform support

This container image is **Linux-only**. It is built on Debian bookworm with `python3-opencv` from apt so that OpenCV is linked against `libv4l` and can negotiate `MJPG` on `/dev/video*` reliably (see the [CYB-1998 note](#why-debian-python3-opencv-instead-of-the-pip-wheel) below for the full story).

| Scenario | Recommended path |
| --- | --- |
| Linux edge device (Raspberry Pi, x86 server) | This container image (`cyberwaveos/camera-driver`) |
| macOS native development | `pip install cyberwave[camera]` — the macOS `opencv-python` wheel ships with AVFoundation |
| Windows native development | `pip install cyberwave[camera]` — the Windows wheel ships with MSMF + DSHOW |
| Docker Desktop on macOS | **Not supported.** Docker Desktop's LinuxKit VM does not expose host USB devices to containers, so `/dev/video0` does not exist inside the container regardless of how OpenCV is built. Run the SDK natively instead. |

## Driver metadata

Set the following fields in the twin or asset metadata to configure the driver:

```json
"drivers": {
    "default": {
        "docker_image": "cyberwaveos/camera-driver",
        "params": ["--device /dev/video0:/dev/video0"] // if you don't add this, it will pick up video0
    }
}
```

## Metadata params

| Field             | Type    | Default | Description                                                       |
| ----------------- | ------- | ------- | ----------------------------------------------------------------- |
| `is_depth_camera` | boolean | `false` | Set to `true` for RGBD/depth cameras (e.g. Intel RealSense D455). |
| `video_device`    | string  | `"0"`   | Capture source — see [Video source formats](#video-source-cyberwave_metadata_video_device) below. |

## Video source (`CYBERWAVE_METADATA_VIDEO_DEVICE`)

The driver opens whatever string is in `CYBERWAVE_METADATA_VIDEO_DEVICE` (from twin
`metadata.video_device`, expanded by `entrypoint.sh`, or injected by edge-core).
This is the **only** field that selects which camera to stream.

| Format | Example | Notes |
| --- | --- | --- |
| Device index | `"0"` | Local USB/V4L2 camera (OpenCV index). Default when unset. |
| Device path | `"/dev/video2"` | Linux V4L2 device node. |
| RTSP URL | `rtsp://user:pass@192.168.1.50:554/stream1` | IP cameras (Tapo, NVR channels, …). Uses OpenCV/FFmpeg with **TCP** transport. Credentials in the URL are optional; they are masked in edge-health telemetry. |
| HTTP(S) URL | `http://192.168.1.50/snapshot.jpg` | Snapshot or MJPEG-over-HTTP sources. |
| RealSense serial | `"123456789012"` | With `is_depth_camera: true` / `CYBERWAVE_METADATA_IS_DEPTH_CAMERA=true`. |

RTSP and HTTP sources do **not** use `/dev/video*`. If the container env shows
`/dev/video0` for an IP-camera twin, edge-core has applied the wrong source — see
below.

### IP / RTSP camera example

Pin the RTSP URL in twin metadata so edge-core does not substitute a local USB
device from `~/.cyberwave/cameras.json`:

```json
"drivers": {
    "default": {
        "docker_image": "cyberwaveos/camera-driver",
        "params": [
            "-e",
            "CYBERWAVE_METADATA_VIDEO_DEVICE=rtsp://user:pass@192.168.1.50:554/stream1"
        ]
    }
}
```

Verify after edge-core starts the driver:

```bash
docker inspect cyberwave-driver-<twin_uuid_prefix> \
  --format '{{range .Config.Env}}{{println .}}{{end}}' | grep VIDEO_DEVICE
```

The value must be your `rtsp://…` URL, not `/dev/video0`.

Optional: also store the URL under `metadata.edge_configs.camera_config.source`
for dashboard visibility — the driver reads `CYBERWAVE_METADATA_VIDEO_DEVICE`, not
`edge_configs`, at stream-open time today.

### How edge-core sets this variable

On **Linux**, when `CYBERWAVE_METADATA_VIDEO_DEVICE` is not already set via
`drivers.default.params`, edge-core may inject a device from
`~/.cyberwave/cameras.json` (`selected_device` or `twin_to_device`). That mapping
only covers local `/dev/video*` devices. **IP-camera twins on the same host as USB
webcams need an explicit RTSP URL** in driver `params` (or another explicit env)
to avoid picking up the wrong `/dev/video*`.

On **macOS**, edge-core may inject an MJPEG bridge URL from
`~/.cyberwave/camera_streams.json` instead of `/dev/video*`.

## Building with RealSense support

The default image only includes standard USB camera support. To include Intel RealSense (pyrealsense2), build with:

```bash
docker build --build-arg ENABLE_REALSENSE=true -t cyberwaveos/camera-driver:realsense .
```

On amd64 this installs pre-built pip wheels; on arm64 it builds librealsense from source (slower build).

Then reference the RealSense image in your asset metadata:

```json
"drivers": {
    "default": {
        "docker_image": "cyberwaveos/camera-driver:realsense"
    }
}
```

## Hardware-accelerated H264 encoding

The driver auto-detects a hardware H264 encoder at stream start and falls
back to software (libx264) when none is usable.

- **Raspberry Pi 4**: pass the V4L2 encoder device into the container:
  `--device /dev/video11` (alongside your camera device). Pi 5 has no H264
  encode hardware; software encoding is used automatically.
- **Pin or disable** with `CYBERWAVE_VIDEO_ENCODER`:
  `auto` (default), a specific encoder (e.g. `h264_v4l2m2m`), or `libx264`
  to force software encoding.

The active encoder is visible in the stream attributes of the WebRTC offer
(`video_encoder`).

## Environment variables

Injected by `cyberwave-edge-core` at runtime:

| Variable                          | Description                                                                                    |
| --------------------------------- | ---------------------------------------------------------------------------------------------- |
| `CYBERWAVE_API_KEY`               | API token                                                                                      |
| `CYBERWAVE_TWIN_UUID`             | UUID of the camera twin to stream to                                                           |
| `CYBERWAVE_TWIN_JSON_FILE`        | Path to the twin JSON file (auto-expanded into `CYBERWAVE_METADATA_*` vars by `entrypoint.sh`) |
| `CYBERWAVE_METADATA_VIDEO_DEVICE` | Capture source — device index, `/dev/video*`, RTSP/HTTP URL, or RealSense serial. See [Video source formats](#video-source-cyberwave_metadata_video_device). |
| `CYBERWAVE_FRAME_ENCODING`        | `raw` (default) for numpy arrays via SHM, or `jpeg` for JPEG-encoded frames (lower bandwidth)  |
| `CYBERWAVE_FRAME_JPEG_QUALITY`    | JPEG quality 1-100 when encoding is `jpeg` (default: `90`)                                     |
| `CYBERWAVE_DETECTION_OVERLAYS`    | `true` (default) to draw YOLO bounding boxes from the `detections/*` Zenoh channel on the WebRTC stream. Set to `false` to disable. Ignored on depth cameras. |
| `CYBERWAVE_METADATA_FRAME_FILTER_ENABLED` | `false` (default). Set to `true` to subscribe to the `frames/filtered` Zenoh channel and substitute worker-processed (e.g. anonymised/pixelated) frames into the WebRTC stream before encoding. When enabled, emits a black frame if no fresh processed frame is available — privacy-safe by default, no raw fallback. |
| `CYBERWAVE_METADATA_FRAME_FILTER_FRESHNESS_MS` | Max age (ms) of a processed frame before it is treated as stale and replaced with a blank frame. Default: `200` (tuned for ≥ 5 Hz GPU workers). Raise to `400`–`500` for CPU-only workers; higher values keep more visibly-stale frames on screen and weaken the privacy contract. `0` is a valid "force blank" fail-close test mode. Only honoured when `CYBERWAVE_METADATA_FRAME_FILTER_ENABLED=true`. |
| `CYBERWAVE_METADATA_DEPTH_FPS`    | Depth capture rate (default: `30`). Only read on twins with a `depth`-typed sensor; controls the publisher-thread budget log for the `depth/<sensor>` Zenoh channel. |
| `CYBERWAVE_CAMERA_STRICT_GEOMETRY` | `false` (default). Set to `true` on edge images where you want a resolution mismatch (e.g. requested VGA but driver got 1080p because the camera fell back to its native format) to raise `RuntimeError` at startup rather than logging a `WARNING` and shipping a stream that is 50x over the bandwidth budget. |
| `CYBERWAVE_CAMERA_SKIP_V4L2_CHECK` | `false` (default). Escape hatch for the Linux V4L2 build-info self-test in the SDK. Set to `true` to bypass the check; only useful if you are intentionally running on a Linux host with an OpenCV that lacks V4L2 and you understand the consequences (frames default to YUYV at the camera's native resolution). |
| `CYBERWAVE_VIDEO_ENCODER`         | `auto` (default) probes for a usable hardware H264 encoder (e.g. `h264_v4l2m2m` on Raspberry Pi 4 with `/dev/video11` passed through) and falls back to `libx264` if none is usable. Set to a specific encoder name to pin it, or `libx264` to force software encoding. |

## Zenoh data bus

When `CYBERWAVE_DATA_BACKEND=zenoh` (or `filesystem`) is set, this driver publishes sensor data to the local Zenoh data bus in addition to the WebRTC cloud path. Channel names are taken from the twin's asset schema so the sensor segment is meaningful (e.g. `color_camera`, `depth_camera`):

| Channel                        | Payload                                                                     |
| ------------------------------ | --------------------------------------------------------------------------- |
| `frames/<sensor>` (from asset) | Raw BGR uint8 frames via SDK binary header (JPEG optional via env override) |
| `frames/default` (legacy)      | Only when the twin's asset declares no camera sensor — the driver logs a warning pointing at the drift |
| `depth/<sensor>` (from asset)  | Raw uint16 depth frames aligned to the color grid (RealSense / RGBD only)   |

Worker containers can subscribe with `@cw.on_frame(twin_uuid)` (wildcard — matches any camera on the twin) or pin to a specific sensor via `@cw.on_frame(twin_uuid, sensor="color_camera")`. Use `cyberwave worker doctor` to verify that the expected subscription keys match what the driver actually publishes.

Set `CYBERWAVE_PUBLISH_MODE` to control which paths are active (`dual`, `zenoh_only`, `mqtt_only`). Default is `dual`.

### Depth streaming (RealSense / RGBD)

On twins with `is_depth_camera: true` (or a sensor of `type: depth` in the asset schema) the driver enables the SDK's RealSense pipeline and starts a second Zenoh publisher for depth frames on `depth/<sensor>`. Depth is **always published raw** (uint16) — JPEG encoding is intentionally not offered because lossy compression corrupts millimeter values. The SDK applies `rs.align(rs.stream.color)` before the callback fires so consumers receive depth registered to the color grid; downstream primitives such as `object_pose` rely on this contract.

Set `CYBERWAVE_METADATA_DEPTH_FPS` (default `30`) to match the camera's advertised depth mode. The value only affects the publisher-thread budget log line — the actual capture rate is negotiated inside the RealSense pipeline.

**Consumer-side color+depth synchronization.** The two channels publish independently on separate publisher threads, so downstream subscribers that fuse color and depth (e.g. `object_pose`, tracking, sim-to-real) should reconcile using the `ts` and `seq` fields emitted by the SDK's binary header on every sample:

- `ts` — wall-clock timestamp in seconds (float), stamped by the SDK header at **publish time** on each publisher thread. Both color and depth are dispatched from the same `recv()` call microseconds apart, but the actual `ts` values are set when each publisher thread wakes up and calls the backend; skew is bounded by scheduling latency and Zenoh publish overhead (typically sub-millisecond on the same host).
- `seq` — per-channel monotonic frame counter (starts at 0).

Recommended pairing strategy: keep a small (~1s) window of the last-seen depth frames keyed by `ts`, and on each color frame pop the depth entry with the closest `ts` (nearest-neighbour). A `abs(ts_color - ts_depth) < 5 ms` gate is a safe default at 30 fps; tighten to 2 ms if you observe clean pairings. Do **not** pair by `seq` alone — the two counters restart independently after reconnects, so `seq` is only monotone *within* a channel.

## Detection overlays

When an ML worker publishes YOLO-style detection results on `cw/<twin_uuid>/data/detections/<runtime>` (for example `detections/ultralytics` or `detections/onnxruntime`), the driver draws bounding boxes and labels on the video stream before WebRTC encoding. The overlays appear in the frontend without any client-side changes.

Frames published upstream on `frames/<sensor>` are always **clean** — the driver copies the capture buffer before drawing, so the ML worker never sees annotated images.

Detection messages are expected as raw JSON:

```json
{
  "detections": [
    {"label": "person", "confidence": 0.92, "x1": 120, "y1": 80, "x2": 340, "y2": 620}
  ],
  "frame_width": 1920,
  "frame_height": 1080
}
```

Coordinates are in pixel space of the detection frame; the driver scales them to the capture resolution and drops detections older than two seconds (matching the OBSBOT C++ driver) so the zero-copy fast path resumes as soon as the ML worker stops publishing. Workers are expected to publish every inference — including empty `{"detections": []}` heartbeats — so the driver's freshness timer stays alive between non-empty frames and overlays don't flicker when the scene transiently has nothing to detect.

Overlays are enabled by default on RGB cameras. Set `CYBERWAVE_DETECTION_OVERLAYS=false` to disable. Twins that declare a depth sensor in their capabilities skip this path automatically.

The driver depends on `cyberwave[camera,zenoh]`, which pulls in `eclipse-zenoh`. If the data bus cannot be opened at startup (for example, the router is unreachable) the driver logs an error pointing at the missing dependency and — when `CYBERWAVE_PUBLISH_MODE=zenoh_only` — exits rather than silently falling back to WebRTC-only.

## Frame filter (anonymisation pipeline)

When `CYBERWAVE_METADATA_FRAME_FILTER_ENABLED=true`, the driver subscribes to `cw/<twin_uuid>/data/frames/filtered` and swaps worker-processed frames (e.g. pixelated or redacted people) into the WebRTC stream before encoding. Raw frames on `frames/<sensor>` are **unchanged** — workers always receive clean pixels.

Workers publish processed frames using the SDK's `FILTERED_FRAME_CHANNEL` constant:

```python
import cyberwave as cw
from cyberwave.data import FILTERED_FRAME_CHANNEL

@cw.on_frame(TWIN_UUID, sensor="color_camera")
def anonymise(ctx: cw.HookContext):
    persons = cw.models.load("yolov8n").predict(ctx.frame)
    anonymised = cw.vision.anonymize_frame(ctx.frame, persons, mode="pixelate")
    cw.data.publish(FILTERED_FRAME_CHANNEL, anonymised)
```

Privacy-safe defaults:

- The filter depends on the Zenoh data bus to receive anonymised frames. If `CYBERWAVE_METADATA_FRAME_FILTER_ENABLED=true` but `CYBERWAVE_DATA_BACKEND` is unset or the bus fails to come up (e.g. `eclipse-zenoh` is missing from the image), the driver **aborts startup with exit code 1** rather than silently streaming raw camera frames to WebRTC. Set `CYBERWAVE_DATA_BACKEND=zenoh` and install `cyberwave[camera,zenoh]`, or disable the filter.
- Processed frames are considered "fresh" for **200 ms** by default. Beyond that the driver emits a black frame — there is no raw fallback, so if your worker is slower than ~5 Hz the stream will appear blacked out. Either budget your inference accordingly (GPU, lighter model, smaller `imgsz`) or raise `CYBERWAVE_METADATA_FRAME_FILTER_FRESHNESS_MS` (e.g. to `400`–`500` ms) at the cost of keeping visibly-stale anonymised frames on screen longer.
- Shape or dtype mismatches between raw and processed frames also emit black and are accumulated into the blank-frame summary log (see below).
- Subscription failures after startup (transient Zenoh router hiccups) also keep the stream blacked out rather than falling back to raw, and log an `ERROR` so the operator can investigate.
- Detection overlays (if enabled) are drawn **on top of** the filtered frame, so bounding boxes still appear even when the underlying pixels are pixelated. If this defeats your anonymisation requirement, set `CYBERWAVE_DETECTION_OVERLAYS=false` alongside the filter flag.

#### Blank-frame summary log

Stale and shape/dtype-mismatch fall-throughs are accumulated into a 30-second rolling window and logged as a single summary per window:

```
[FRAME_FILTER] 1.2% of 870 frames in last 30 s emitted blank fallback on
'frames/filtered' (reasons: stale=11). Worker may be slow or freshness
window too tight; consider raising
CYBERWAVE_METADATA_FRAME_FILTER_FRESHNESS_MS if pixelation occasionally
drops out.
```

A high blank rate switches the advice line:

```
[FRAME_FILTER] 100.0% of 900 frames in last 30 s emitted blank fallback on
'frames/filtered' (reasons: stale=900). Worker likely down or not
publishing on this channel — check the worker container logs.
```

The previous design fired one warning the moment any frame went stale and then suppressed further warnings for the rest of the window — that fired loudly even when 99% of frames were fine, which happens routinely when the worker publishes at 5 fps and the driver polls at 30 fps with the default 200 ms freshness window. The summary form lets operators distinguish "occasional jitter" (~1%) from "worker is down" (~100%).

> **Note:** `frame_filter.py` in this package is a temporary port of the same module in `cyberwave-edge-runtime/runtime-services/drivers/native/cyberwave/generic-camera`. Once that driver image is published and the backend asset registry repoints to it, the copy here should be removed.

## Troubleshooting

**Wrong camera / IP twin shows USB feed**

If `docker inspect cyberwave-driver-<prefix> --format '…' | grep VIDEO_DEVICE` shows
`/dev/video0` but you configured an RTSP camera, edge-core applied
`~/.cyberwave/cameras.json` instead of your RTSP URL. Pin the URL in
`drivers.default.params` as shown in [Video source formats](#video-source-cyberwave_metadata_video_device).

**RTSP stream does not open**

Test from the edge host (or inside the driver container network namespace):

```bash
ffprobe -v error "rtsp://user:pass@192.168.1.50:554/stream1"
```

Confirm the camera account, path (`/stream1` vs `/stream2` on Tapo), and that the
edge host can reach the camera IP on the LAN.

## Failure signaling

When required camera hardware is unavailable (for example, missing/disconnected USB camera), the driver exits with a non-zero code so edge-core can detect startup failures and restart loops.

- If a configured `/dev/video*` path is missing, the driver first attempts auto-discovery fallback before exiting with a hardware error.
- Exit code `66`: hardware connection failure
- Exit code `1`: other unhandled runtime failures (including Zenoh init failure when `CYBERWAVE_PUBLISH_MODE=zenoh_only`)

## Why Debian `python3-opencv` instead of the pip wheel?

The image installs OpenCV via Debian's `python3-opencv` package (built with `V4L/V4L2: YES`) rather than the `opencv-python` PyPI wheel. The manylinux `opencv-python` wheel is built without the native V4L2 backend, which routes `cv2.VideoCapture("/dev/video0")` through FFmpeg's libavformat V4L2 demuxer. In that demuxer, `cap.set(CAP_PROP_FOURCC, MJPG)` is a no-op (FFmpeg wants `-input_format mjpeg` at open time) and `cap.get(CAP_PROP_FOURCC)` returns `0`. The SDK's negotiator then concludes that MJPG did not stick, reopens the device without a FOURCC override, and the camera lands on its kernel default — typically YUYV `1920x1080 @ 5 fps` on USB 2.0. The resulting `~58 MB/s` raw firehose blows the Zenoh publish budget and causes downstream worker drops.

The Dockerfile asserts `V4L/V4L2: YES` in `cv2.getBuildInformation()` at build time so that a future packaging accident fails the image build instead of the running camera. The SDK does the same check at startup so a developer running `pip install cyberwave[camera]` on a Linux host with a stripped OpenCV wheel gets a clear `RuntimeError` instead of a 5 fps stream.

If you genuinely need to bypass the check (e.g. running a test container with a custom OpenCV build), set `CYBERWAVE_CAMERA_SKIP_V4L2_CHECK=1`. macOS and Windows users are unaffected — the check only fires on `platform.system() == "Linux"`.

## Local image build and smoke test

From the monorepo root, bundle the in-tree SDK and build the same image CI publishes:

```bash
./cyberwave-edge-nodes/cyberwave-edge-camera-driver/build-dev-image.sh
# RealSense variant:
ENABLE_REALSENSE=true IMAGE=cyberwaveos/camera-driver:dev-realsense \
  ./cyberwave-edge-nodes/cyberwave-edge-camera-driver/build-dev-image.sh
```

Run the Docker E2E smoke test (build, V4L2 assertion, `pytest tests` inside the container):

```bash
./cyberwave-edge-nodes/cyberwave-edge-camera-driver/test_docker_e2e.sh
```

CI runs the same flow via `.github/workflows/edge-node-camera-driver-test-and-push.yml` (unit tests on the host, E2E in `test_build_dockerfile`, multi-arch push to `cyberwaveos/camera-driver` with standard and `-realsense` tags).

## Contributing

Contributions are welcome. Please open an issue for bugs or feature requests, and submit a pull request for improvements.

## Community and Documentation

- Documentation: https://docs.cyberwave.com
- Community (Discord): https://discord.gg/dfGhNrawyF
- Issues: https://github.com/cyberwave-os/cyberwave-edge-camera-driver/issues
