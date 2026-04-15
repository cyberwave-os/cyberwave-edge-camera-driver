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

## Zenoh data bus

When `CYBERWAVE_DATA_BACKEND=zenoh` (or `filesystem`) is set, this driver publishes sensor data to the local Zenoh data bus in addition to the WebRTC cloud path:

| Channel          | Payload                                    |
| ---------------- | ------------------------------------------ |
| `frames/default` | Raw BGR uint8 frames via SDK binary header |

Worker containers can subscribe with `cw.data.subscribe("frames/default", callback)` — no adapter code required.

Set `CYBERWAVE_PUBLISH_MODE` to control which paths are active (`dual`, `zenoh_only`, `mqtt_only`). Default is `dual`.

## Failure signaling

When required camera hardware is unavailable (for example, missing/disconnected USB camera), the driver exits with a non-zero code so edge-core can detect startup failures and restart loops.

- If a configured `/dev/video*` path is missing, the driver first attempts auto-discovery fallback before exiting with a hardware error.
- Exit code `66`: hardware connection failure
- Exit code `1`: other unhandled runtime failures

## Contributing

Contributions are welcome. Please open an issue for bugs or feature requests, and submit a pull request for improvements.

## Community and Documentation

- Documentation: https://docs.cyberwave.com
- Community (Discord): https://discord.gg/dfGhNrawyF
- Issues: https://github.com/cyberwave-os/cyberwave-edge-camera-driver/issues
