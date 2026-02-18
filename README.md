# Cyberwave Camera Driver

> New to Cyberwave? [Check out the docs](https://docs.cyberwave.com)

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

## Environment variables

Injected by `cyberwave-edge-core` at runtime:

| Variable                   | Description                                                                                    |
| -------------------------- | ---------------------------------------------------------------------------------------------- |
| `CYBERWAVE_TOKEN`          | API token                                                                                      |
| `CYBERWAVE_TWIN_UUID`      | UUID of the camera twin to stream to                                                           |
| `CYBERWAVE_TWIN_JSON_FILE` | Path to the twin JSON file (auto-expanded into `CYBERWAVE_METADATA_*` vars by `entrypoint.sh`) |
