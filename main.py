"""Camera Driver

Streams a camera feed to a Cyberwave digital twin. Launched by cyberwave-edge-core
with the following environment variables set:

  CYBERWAVE_TOKEN          – API token
  CYBERWAVE_TWIN_UUID      – UUID of the camera twin to stream to
  CYBERWAVE_TWIN_JSON_FILE – Path to the JSON file describing the twin (expanded
                             into CYBERWAVE_METADATA_* vars by entrypoint.sh)

Camera-specific metadata params (set on the twin / asset metadata):

  metadata.is_depth_camera  – "true" if the camera is an RGBD/depth camera
                               (e.g. Intel RealSense). Defaults to false.
  metadata.video_device     – The /dev/video* device index or path to use.
                               Defaults to "0".
"""

import asyncio
import logging
import os
import signal
import subprocess
import sys

from cyberwave import Cyberwave

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("camera-driver")


def _list_cameras() -> tuple[list[str], list[str]]:
    """List all available RealSense cameras, like the following:
    ```bash
    # List available CV2 cameras
    python -c "import cv2; [print(f'Camera {i}: {cv2.VideoCapture(i).isOpened()}') for i in range(5)]"

    # List RealSense devices
    python -c "import pyrealsense2 as rs; ctx = rs.context(); print([d.get_info(rs.camera_info.name) for d in ctx.devices])"
    ```
    """
    # # run sudo chmod 666 /dev/video* to allow non-root users to access the cameras
    # # use -n so it won't block waiting for a password prompt
    # subprocess.run("sudo -n chmod 666 /dev/video* >/dev/null 2>&1", shell=True, check=False)

    cv2_cameras: list[str] = []
    realsense_cameras: list[str] = []

    try:
        import cv2

        for index in range(10):
            cap = cv2.VideoCapture(index)
            try:
                if cap.isOpened():
                    cv2_cameras.append(str(index))
            finally:
                cap.release()
    except Exception:
        logger.exception("Failed to enumerate CV2 cameras")

    try:
        import pyrealsense2 as rs

        ctx = rs.context()
        for device in ctx.devices:
            serial = device.get_info(rs.camera_info.serial_number)
            if serial:
                realsense_cameras.append(serial)
            else:
                realsense_cameras.append(device.get_info(rs.camera_info.name))
    except Exception:
        logger.exception("Failed to enumerate RealSense devices")

    return cv2_cameras, realsense_cameras


def _parse_camera_id(video_device: str) -> int | str:
    """Parse camera metadata into SDK-compatible camera_id."""
    # Numeric values from metadata should be treated as local camera indices.
    # Non-numeric values can be /dev/video* paths, RTSP URLs, etc.
    try:
        return int(video_device)
    except ValueError:
        return video_device


async def main() -> None:
    token = os.getenv("CYBERWAVE_TOKEN")
    twin_uuid = os.getenv("CYBERWAVE_TWIN_UUID")

    if not token:
        logger.error("CYBERWAVE_TOKEN environment variable is required")
        sys.exit(1)
    if not twin_uuid:
        logger.error("CYBERWAVE_TWIN_UUID environment variable is required")
        sys.exit(1)

    is_depth_camera = os.getenv("CYBERWAVE_METADATA_IS_DEPTH_CAMERA", "false").lower() == "true"
    video_device = os.getenv("CYBERWAVE_METADATA_VIDEO_DEVICE", "0")
    camera_id = _parse_camera_id(video_device)

    asset_key = "intel/realsensed455" if is_depth_camera else "cyberwave/standard-cam"

    logger.info(
        "Initializing camera driver for twin %s (asset=%s, device=%s)",
        twin_uuid,
        asset_key,
        camera_id,
    )

    client = Cyberwave(token=token, source_type="edge")
    camera = client.twin(asset_key=asset_key, twin_id=twin_uuid)

    stop_event = asyncio.Event()

    def _handle_signal() -> None:
        logger.info("Shutdown signal received, stopping...")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_signal)

    try:
        logger.info("Starting camera stream for twin %s...", twin_uuid)
        try:
            await camera.start_streaming(camera_id=camera_id)
        except Exception:
            logger.exception(
                "Camera stream failed with configured device '%s', trying auto-detect fallback",
                camera_id,
            )
            cv2_cameras, realsense_cameras = _list_cameras()
            fallback_candidates = realsense_cameras if is_depth_camera else cv2_cameras
            if not fallback_candidates:
                raise

            fallback_camera_id: int | str
            if is_depth_camera:
                fallback_camera_id = fallback_candidates[0]
            else:
                fallback_camera_id = _parse_camera_id(fallback_candidates[0])

            logger.info(
                "Retrying camera stream using auto-detected fallback device '%s'",
                fallback_camera_id,
            )
            await camera.start_streaming(camera_id=fallback_camera_id)
        logger.info("Camera stream started. Waiting for shutdown signal...")
        await stop_event.wait()
    except Exception:
        logger.exception("Camera streaming failed")
    finally:
        logger.info("Stopping camera stream...")
        await camera.stop_streaming()
        client.disconnect()
        logger.info("Camera driver stopped.")


if __name__ == "__main__":
    asyncio.run(main())
