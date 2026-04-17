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
import json
import logging
import os
import signal
import sys
import threading
import time

import numpy as np
from cyberwave import Cyberwave

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("camera-driver")
HARDWARE_CONNECTION_EXIT_CODE = 66


class HardwareConnectionError(RuntimeError):
    """Raised when required camera hardware is unavailable."""


def should_retry_camera_start(shutdown_requested: bool) -> bool:
    """Only retry startup when no shutdown has been requested."""
    return not shutdown_requested


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


class _FrameSlot:
    """Single-slot thread-safe frame buffer.

    Producer (capture thread) calls put(); consumer (publisher thread)
    calls take().  When the producer is faster than the consumer, the
    older frame is silently replaced -- matching the ``latest`` Zenoh
    subscriber policy and keeping memory constant.
    """

    def __init__(self) -> None:
        self._frame: np.ndarray | None = None
        self._event = threading.Event()
        self._lock = threading.Lock()

    def put(self, frame: np.ndarray) -> None:
        with self._lock:
            self._frame = frame
        self._event.set()

    def take(self, timeout: float = 1.0) -> np.ndarray | None:
        self._event.wait(timeout)
        self._event.clear()
        with self._lock:
            frame = self._frame
            self._frame = None
        return frame


def _encode_jpeg(frame: np.ndarray, quality: int = 90) -> bytes:
    """JPEG-encode a BGR numpy frame, returning raw JPEG bytes."""
    import cv2

    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise RuntimeError("cv2.imencode failed")
    return buf.tobytes()


def _zenoh_publisher_thread(
    data_bus: object,
    slot: _FrameSlot,
    stop: threading.Event,
    channel: str,
    fps: int,
    *,
    encoding: str = "raw",
    jpeg_quality: int = 90,
) -> None:
    """Read frames from *slot* and publish to the data bus.

    Runs on a dedicated daemon thread.  Drops frames when the publisher
    is slower than the capture loop (single-slot semantics).

    Args:
        encoding: ``"raw"`` publishes numpy arrays through the SDK wire
            format (zero-copy capable via SHM on same-host).  ``"jpeg"``
            JPEG-encodes frames before publishing — useful for remote or
            bridged subscribers that need lower bandwidth.
    """
    if data_bus is None:
        return

    try:
        backend_name = type(data_bus._backend).__name__  # type: ignore[attr-defined]  # noqa: SLF001
        logger.info(
            "Zenoh frame publisher active (backend=%s, channel=%s, encoding=%s)",
            backend_name,
            channel,
            encoding,
        )
    except Exception:
        pass

    use_jpeg = encoding == "jpeg"
    budget_s = 1.0 / fps * 2
    _first = True

    while not stop.is_set():
        frame = slot.take(timeout=1.0)
        if frame is None:
            continue
        t0 = time.monotonic()
        try:
            if use_jpeg:
                # Publish raw JPEG bytes without SDK wire-format header so
                # subscribers detect the JPEG SOI marker and decode to numpy.
                data_bus.publish_raw(channel, _encode_jpeg(frame, jpeg_quality))  # type: ignore[attr-defined]
            elif _first:
                data_bus.publish(channel, frame, metadata={"fps": fps})  # type: ignore[attr-defined]
                _first = False
            else:
                data_bus.publish(channel, frame)  # type: ignore[attr-defined]
        except Exception:
            logger.warning("Zenoh frame publish failed", exc_info=True)
        elapsed = time.monotonic() - t0
        if elapsed > budget_s:
            logger.warning(
                "Zenoh publish for %s took %.1f ms (budget %.1f ms)",
                channel,
                elapsed * 1000,
                budget_s * 1000,
            )


async def main() -> None:
    token = os.getenv("CYBERWAVE_API_KEY")
    twin_uuid = os.getenv("CYBERWAVE_TWIN_UUID")

    if not token:
        logger.error("CYBERWAVE_API_KEY environment variable is required")
        sys.exit(1)
    if not twin_uuid:
        logger.error("CYBERWAVE_TWIN_UUID environment variable is required")
        sys.exit(1)

    twin_json_path = os.getenv("CYBERWAVE_TWIN_JSON_FILE")
    twin_data: dict = {}
    if twin_json_path:
        try:
            with open(twin_json_path) as f:
                twin_data = json.load(f)
        except (OSError, json.JSONDecodeError):
            logger.exception("Failed to read twin JSON file at %s", twin_json_path)

    asset = twin_data.get("asset") or {}
    asset_key = asset.get("registry_id") or ""
    if not asset_key:
        raise ValueError("No asset.registry_id found in twin JSON")

    if not twin_data.get("capabilities"):
        raise ValueError("No capabilities found in twin JSON")
    # it has to have at least one sensor, otherwise it's not a camera
    if not (twin_data.get("capabilities") or {}).get("sensors"):
        raise ValueError("No sensors found in twin JSON")

    sensors = (twin_data.get("capabilities") or {}).get("sensors") or []
    is_depth_camera = any(s.get("type") == "depth" for s in sensors)
    camera_name: str | None = None
    if sensors and isinstance(sensors[0], dict):
        sid = sensors[0].get("id")
        if sid is not None:
            camera_name = str(sid)
    video_device = os.getenv("CYBERWAVE_METADATA_VIDEO_DEVICE", "0")
    camera_id = _parse_camera_id(video_device)
    if isinstance(camera_id, str) and camera_id.startswith("/dev/") and not os.path.exists(camera_id):
        logger.warning(
            (
                "Configured camera device '%s' does not exist inside the container; "
                "will attempt auto-discovery fallback if stream start fails"
            ),
            camera_id,
        )

    logger.info(
        "Initializing camera driver for twin %s (asset=%s, device=%s, camera_name=%s)",
        twin_uuid,
        asset_key,
        camera_id,
        camera_name or "(from twin API default)",
    )

    client = Cyberwave(api_key=token, source_type="edge")
    camera = client.twin(asset_key=asset_key, twin_id=twin_uuid)

    # ── Zenoh data bus initialization ──
    data_bus = None
    frame_slot: _FrameSlot | None = None
    stop_publisher = threading.Event()
    publisher_thread: threading.Thread | None = None
    publish_zenoh = False

    # ``zenoh_only`` decides whether we crash on init failure or degrade.
    # Captured here so the exception handler below doesn't have to reach
    # back into ``backend_cfg`` (which only exists when the import worked).
    zenoh_only = False

    if os.getenv("CYBERWAVE_DATA_BACKEND"):
        try:
            from cyberwave.data.config import BackendConfig, is_zenoh_publish_enabled

            backend_cfg = BackendConfig()
            publish_zenoh = is_zenoh_publish_enabled(backend_cfg)
            zenoh_only = backend_cfg.publish_mode == "zenoh_only"
            logger.info(
                "Driver publish config: mode=%s | Zenoh=%s | backend=%s",
                backend_cfg.publish_mode,
                "active" if publish_zenoh else "disabled",
                backend_cfg.backend if publish_zenoh else "n/a",
            )
        except ImportError:
            logger.info(
                "cyberwave.data module not available; Zenoh publishing disabled"
            )

    frame_encoding = os.getenv("CYBERWAVE_FRAME_ENCODING", "raw").lower()
    if frame_encoding not in ("raw", "jpeg"):
        logger.warning(
            "Unknown CYBERWAVE_FRAME_ENCODING '%s'; defaulting to 'raw'",
            frame_encoding,
        )
        frame_encoding = "raw"
    jpeg_quality = int(os.getenv("CYBERWAVE_FRAME_JPEG_QUALITY", "90"))

    if publish_zenoh:
        try:
            data_bus = client.data
        except Exception as exc:
            # Give a targeted hint when eclipse-zenoh is missing — this was the
            # silent failure mode that masqueraded as WebRTC-only streaming.
            hint = ""
            try:
                from cyberwave.data.exceptions import BackendUnavailableError

                if isinstance(exc, BackendUnavailableError):
                    hint = (
                        "  Install eclipse-zenoh in the driver image, e.g. "
                        "`pip install 'cyberwave[camera,zenoh]'`."
                    )
            except ImportError:
                pass
            if zenoh_only:
                logger.error(
                    "Failed to initialize Zenoh data bus and publish_mode=zenoh_only; "
                    "aborting driver startup.%s",
                    hint,
                    exc_info=True,
                )
                raise
            logger.error(
                "Failed to initialize Zenoh data bus; on-edge workers will not "
                "receive frames from this driver. Cloud-side WebRTC streaming "
                "continues, but any @cw.on_frame hook on this twin will stay "
                "idle.%s",
                hint,
                exc_info=True,
            )
            data_bus = None
            frame_slot = None
        else:
            if camera_name:
                camera_channel = f"frames/{camera_name}"
            else:
                # The twin's asset didn't declare a camera sensor name.  We
                # still publish (under the legacy ``frames/default`` key) so
                # workers subscribing with the SDK's wildcard default keep
                # receiving frames, but loudly flag the drift — every real
                # twin should declare a sensor in its asset schema so the
                # driver, worker, and doctor probe all agree on one name.
                camera_channel = "frames/default"
                logger.warning(
                    "Twin %s did not expose a camera sensor name via "
                    "`asset_key=%s`; falling back to the legacy "
                    "'frames/default' key. Declare a sensor (e.g. "
                    "'color_camera') in the twin's asset schema so "
                    "@cw.on_frame hooks can pin to a specific sensor and "
                    "`cyberwave worker doctor` can validate the binding.",
                    twin_uuid,
                    asset_key,
                )
            frame_slot = _FrameSlot()
            publisher_thread = threading.Thread(
                target=_zenoh_publisher_thread,
                args=(data_bus, frame_slot, stop_publisher, camera_channel, 30),
                kwargs={"encoding": frame_encoding, "jpeg_quality": jpeg_quality},
                daemon=True,
                name="zenoh-frame-publisher",
            )
            publisher_thread.start()
            logger.info(
                "Zenoh frame publishing enabled on channel '%s' (encoding=%s)",
                camera_channel,
                frame_encoding,
            )

    def _on_frame(frame: np.ndarray, _frame_count: int) -> None:
        if frame_slot is not None:
            frame_slot.put(frame)

    frame_callback = _on_frame if data_bus is not None else None

    stop_event = asyncio.Event()
    shutdown_requested = False

    def _handle_signal() -> None:
        nonlocal shutdown_requested
        logger.info("Shutdown signal received, stopping...")
        shutdown_requested = True
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_signal)

    stream_started = False
    try:
        logger.info("Starting camera stream for twin %s...", twin_uuid)
        try:
            await camera.stream_video_background(
                camera_id=camera_id,
                camera_name=camera_name,
                fps=30,
                frame_callback=frame_callback,
            )
            stream_started = True
        except Exception as stream_error:
            if not should_retry_camera_start(shutdown_requested):
                logger.info(
                    "Shutdown requested during stream startup; skipping fallback retry"
                )
                return
            logger.exception(
                "Camera stream failed with configured device '%s', trying auto-detect fallback",
                camera_id,
            )
            cv2_cameras, realsense_cameras = _list_cameras()
            fallback_candidates = realsense_cameras if is_depth_camera else cv2_cameras
            if not fallback_candidates:
                raise HardwareConnectionError(
                    f"No camera hardware available for configured device '{camera_id}'"
                ) from stream_error

            fallback_camera_id: int | str
            if is_depth_camera:
                fallback_camera_id = fallback_candidates[0]
            else:
                fallback_camera_id = _parse_camera_id(fallback_candidates[0])

            logger.info(
                "Retrying camera stream using auto-detected fallback device '%s'",
                fallback_camera_id,
            )
            try:
                await camera.stream_video_background(
                    camera_id=fallback_camera_id,
                    camera_name=camera_name,
                    fps=30,
                    frame_callback=frame_callback,
                )
                stream_started = True
            except Exception as fallback_error:
                raise HardwareConnectionError(
                    f"Camera hardware unavailable: could not start stream with fallback '{fallback_camera_id}'"
                ) from fallback_error
        logger.info("Camera stream started. Waiting for shutdown signal...")
        await stop_event.wait()
    finally:
        logger.info("Stopping camera stream...")
        stop_publisher.set()
        if publisher_thread is not None:
            publisher_thread.join(timeout=5.0)
        if stream_started:
            try:
                await camera.stop_streaming()
            except Exception:
                logger.exception("Failed while stopping camera stream")
        client.disconnect()
        logger.info("Camera driver stopped.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except HardwareConnectionError as exc:
        logger.error("Exiting due to camera hardware connection error: %s", exc)
        sys.exit(HARDWARE_CONNECTION_EXIT_CODE)
    except Exception:
        logger.exception("Unhandled camera driver failure")
        sys.exit(1)
