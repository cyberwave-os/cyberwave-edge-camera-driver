"""Camera Driver

Streams a camera feed to a Cyberwave digital twin. Launched by cyberwave-edge-core
with the following environment variables set:

  CYBERWAVE_API_KEY          – API token
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
import sys

from cyberwave import Cyberwave

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("camera-driver")


def _parse_camera_id(video_device: str) -> int | str:
    """Parse camera metadata into SDK-compatible camera_id."""
    # Numeric values from metadata should be treated as local camera indices.
    # Non-numeric values can be /dev/video* paths, RTSP URLs, etc.
    try:
        return int(video_device)
    except ValueError:
        return video_device


async def main() -> None:
    token = os.getenv("CYBERWAVE_API_KEY")
    twin_uuid = os.getenv("CYBERWAVE_TWIN_UUID")

    if not token:
        logger.error("CYBERWAVE_API_KEY environment variable is required")
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
        await camera.start_streaming(camera_id=camera_id)
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
