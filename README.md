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

A Cyberwave edge driver that streams a USB or depth camera feed to a digital twin.

Launched automatically by `cyberwave-edge-core` when a twin's metadata references this driver image.

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
| `video_device`    | string  | `"0"`   | `/dev/video*` index or path (e.g. `"0"`, `"/dev/video2"`).        |

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

## Environment variables

Injected by `cyberwave-edge-core` at runtime:

| Variable                          | Description                                                                                    |
| --------------------------------- | ---------------------------------------------------------------------------------------------- |
| `CYBERWAVE_API_KEY`               | API token                                                                                      |
| `CYBERWAVE_TWIN_UUID`             | UUID of the camera twin to stream to                                                           |
| `CYBERWAVE_TWIN_JSON_FILE`        | Path to the twin JSON file (auto-expanded into `CYBERWAVE_METADATA_*` vars by `entrypoint.sh`) |
| `CYBERWAVE_FRAME_ENCODING`        | `raw` (default) for numpy arrays via SHM, or `jpeg` for JPEG-encoded frames (lower bandwidth)  |
| `CYBERWAVE_FRAME_JPEG_QUALITY`    | JPEG quality 1-100 when encoding is `jpeg` (default: `90`)                                     |
| `CYBERWAVE_DETECTION_OVERLAYS`    | `true` (default) to draw YOLO bounding boxes from the `detections/*` Zenoh channel on the WebRTC stream. Set to `false` to disable. Ignored on depth cameras. |
| `CYBERWAVE_METADATA_FRAME_FILTER_ENABLED` | `false` (default). Set to `true` to subscribe to the `frames/filtered` Zenoh channel and substitute worker-processed (e.g. anonymised/pixelated) frames into the WebRTC stream before encoding. When enabled, emits a black frame if no fresh processed frame is available — privacy-safe by default, no raw fallback. |
| `CYBERWAVE_METADATA_FRAME_FILTER_FRESHNESS_MS` | Max age (ms) of a processed frame before it is treated as stale and replaced with a blank frame. Default: `200` (tuned for ≥ 5 Hz GPU workers). Raise to `400`–`500` for CPU-only workers; higher values keep more visibly-stale frames on screen and weaken the privacy contract. `0` is a valid "force blank" fail-close test mode. Only honoured when `CYBERWAVE_METADATA_FRAME_FILTER_ENABLED=true`. |

## Zenoh data bus

When `CYBERWAVE_DATA_BACKEND=zenoh` (or `filesystem`) is set, this driver publishes sensor data to the local Zenoh data bus in addition to the WebRTC cloud path. The channel name is taken from the twin's asset schema so the sensor segment is meaningful (e.g. `color_camera`, `depth_camera`):

| Channel                       | Payload                                    |
| ----------------------------- | ------------------------------------------ |
| `frames/<sensor>` (from asset) | Raw BGR uint8 frames via SDK binary header |
| `frames/default` (legacy)     | Only when the twin's asset declares no camera sensor — the driver logs a warning pointing at the drift |

Worker containers can subscribe with `@cw.on_frame(twin_uuid)` (wildcard — matches any camera on the twin) or pin to a specific sensor via `@cw.on_frame(twin_uuid, sensor="color_camera")`. Use `cyberwave worker doctor` to verify that the expected subscription keys match what the driver actually publishes.

Set `CYBERWAVE_PUBLISH_MODE` to control which paths are active (`dual`, `zenoh_only`, `mqtt_only`). Default is `dual`.

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
- Shape or dtype mismatches between raw and processed frames also emit black and log a warning once every 30 s.
- Subscription failures after startup (transient Zenoh router hiccups) also keep the stream blacked out rather than falling back to raw, and log an `ERROR` so the operator can investigate.
- Detection overlays (if enabled) are drawn **on top of** the filtered frame, so bounding boxes still appear even when the underlying pixels are pixelated. If this defeats your anonymisation requirement, set `CYBERWAVE_DETECTION_OVERLAYS=false` alongside the filter flag.

> **Note:** `frame_filter.py` in this package is a temporary port of the same module in `cyberwave-edge-runtime/runtime-services/drivers/native/cyberwave/generic-camera`. Once that driver image is published and the backend asset registry repoints to it, the copy here should be removed.

## Failure signaling

When required camera hardware is unavailable (for example, missing/disconnected USB camera), the driver exits with a non-zero code so edge-core can detect startup failures and restart loops.

- If a configured `/dev/video*` path is missing, the driver first attempts auto-discovery fallback before exiting with a hardware error.
- Exit code `66`: hardware connection failure
- Exit code `1`: other unhandled runtime failures (including Zenoh init failure when `CYBERWAVE_PUBLISH_MODE=zenoh_only`)

## Contributing

Contributions are welcome. Please open an issue for bugs or feature requests, and submit a pull request for improvements.

## Community and Documentation

- Documentation: https://docs.cyberwave.com
- Community (Discord): https://discord.gg/dfGhNrawyF
- Issues: https://github.com/cyberwave-os/cyberwave-edge-camera-driver/issues
