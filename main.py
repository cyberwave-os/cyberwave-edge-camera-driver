"""Camera Driver

Streams a camera feed to a Cyberwave digital twin. Launched by cyberwave-edge-core
with the following environment variables set:

  CYBERWAVE_API_KEY          – API token
  CYBERWAVE_TWIN_UUID        – UUID of the camera twin to stream to
  CYBERWAVE_TWIN_JSON_FILE   – Path to the JSON file describing the twin (expanded
                               into CYBERWAVE_METADATA_* vars by entrypoint.sh)

Optional env vars:

  CYBERWAVE_DETECTION_OVERLAYS    – "true" (default) to subscribe to
                                    ``cw/<twin_uuid>/data/detections/**`` and
                                    draw YOLO-style bounding boxes on the
                                    WebRTC stream. "false" disables overlays.
                                    Automatically disabled for twins that
                                    declare a depth sensor.

  CYBERWAVE_METADATA_FRAME_FILTER_ENABLED
                                  – "false" (default). When "true" the driver
                                    subscribes to
                                    ``cw/<twin_uuid>/data/frames/filtered``
                                    and substitutes worker-processed
                                    (e.g. anonymised) frames into the WebRTC
                                    stream before encoding. Emits a black
                                    frame when no fresh processed frame is
                                    available — no raw fallback. Requires
                                    ``CYBERWAVE_DATA_BACKEND`` to be set (the
                                    filter depends on the Zenoh data bus to
                                    receive frames); the driver aborts
                                    startup otherwise, to avoid a silent
                                    raw-frame passthrough that would defeat
                                    the privacy opt-in. See ``frame_filter.py``
                                    and the README for the full contract.

  CYBERWAVE_METADATA_FRAME_FILTER_FRESHNESS_MS
                                  – Max age (ms) of a processed frame before
                                    it is treated as stale and replaced with
                                    a blank frame. Defaults to 200 ms (tuned
                                    for >= 5 Hz GPU workers). Raise for CPU
                                    workers (400-500 ms). Setting 0 forces
                                    every frame to blank (useful as a fail-
                                    close test mode). Malformed values fall
                                    back to the default with a warning. Only
                                    honoured when
                                    ``CYBERWAVE_METADATA_FRAME_FILTER_ENABLED``
                                    is true.

Camera-specific metadata params (set on the twin / asset metadata):

  metadata.is_depth_camera  – "true" if the camera is an RGBD/depth camera
                               (e.g. Intel RealSense). Defaults to false.
  metadata.video_device     – The /dev/video* device index or path to use.
                               Defaults to "0".
  metadata.resolution       – Optional ``"WxH"`` (e.g. ``"256x192"``,
                               ``"1920x1080"``) override for sensors whose
                               native modes don't include the SDK's
                               ``Resolution.VGA`` (640x480) default. When
                               unset the SDK negotiates 640x480 via V4L2,
                               which falls back to the camera's largest
                               available mode if the request fails — fine
                               for most webcams, but on entry-level thermal
                               cameras (Topdon TC001 / InfiRay P2 Pro-class)
                               this lands on a dual-frame 256x384 mode whose
                               bottom half is raw thermal bytes (the
                               green-bottom artifact). Pin this to a mode
                               your sensor actually serves natively to skip
                               the silent fallback.
"""

import asyncio
import json
import logging
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass

import numpy as np
from cyberwave import Cyberwave

# Ensure sibling modules (``frame_filter``) are importable when ``main.py`` is
# loaded via ``importlib.util.spec_from_file_location`` (as the edge-core E2E
# harness does). Inside the Docker image CWD is ``/app`` so this is a no-op;
# it only matters for out-of-tree test loaders.
_DRIVER_DIR = os.path.dirname(os.path.abspath(__file__))
if _DRIVER_DIR not in sys.path:
    sys.path.insert(0, _DRIVER_DIR)

from frame_filter import FRESHNESS_MS, FrameFilter  # noqa: E402

try:
    # Single source of truth for the worker→driver wire contract; workers
    # import the same constants when publishing. Fallback strings keep the
    # driver importable against SDK <0.5; remove once the pinned SDK bumps.
    from cyberwave.data import FILTERED_FRAME_CHANNEL, FRAME_OVERLAY_CHANNEL
except ImportError:
    FILTERED_FRAME_CHANNEL = "frames/filtered"
    FRAME_OVERLAY_CHANNEL = "frames/overlay"

# Emit timestamps in UTC so driver logs line up with edge-core's logs when
# forwarded into the same `cyberwave edge logs` stream (containers have no host
# timezone mounted, so local time would otherwise drift from the host).
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s.%(msecs)03d [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S UTC",
)
logging.Formatter.converter = time.gmtime
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


def _parse_resolution(value: str | None) -> tuple[int, int] | None:
    """Parse a ``metadata.resolution`` string into a ``(width, height)`` tuple.

    Accepts ``"WxH"`` (case-insensitive on the separator), e.g. ``"256x192"``,
    ``"640X480"``, ``"1920x1080"``. Returns ``None`` for empty / unset / malformed
    input so the caller can fall through to the SDK default.

    This is the escape hatch for sensors whose advertised native modes don't
    include the SDK's ``Resolution.VGA`` (640x480) default. Without it,
    ``cv2.VideoCapture`` silently falls back to whatever the V4L2 backend
    picks — typically the largest available mode — which on some thermal
    cameras (Topdon TC001 / InfiRay P2 Pro-class) lands on a dual-frame
    256x384 mode where the bottom 256x192 is raw thermal data exposed as
    YUYV bytes (the green-bottom artifact). Pinning ``metadata.resolution``
    to a mode the sensor actually serves natively avoids the fallback.
    """
    if not value:
        return None
    raw = value.strip().lower().replace(" ", "")
    if "x" not in raw:
        logger.warning(
            "Ignoring CYBERWAVE_METADATA_RESOLUTION=%r: expected 'WxH' (e.g. '256x192')",
            value,
        )
        return None
    parts = raw.split("x", 1)
    try:
        width = int(parts[0])
        height = int(parts[1])
    except ValueError:
        logger.warning(
            "Ignoring CYBERWAVE_METADATA_RESOLUTION=%r: width/height must be integers",
            value,
        )
        return None
    if width <= 0 or height <= 0:
        logger.warning(
            "Ignoring CYBERWAVE_METADATA_RESOLUTION=%r: width and height must be positive",
            value,
        )
        return None
    return (width, height)


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


# ── Detection overlay ──────────────────────────────────────────────
#
# Mirrors the C++ OBSBOT driver: an ML worker publishes JSON detection
# payloads on ``cw/<twin_uuid>/data/detections/<runtime>``; the driver caches
# the latest batch and draws bounding boxes on frames before WebRTC encoding.
# The capture thread copies the frame before drawing, so Zenoh subscribers on
# ``frames/*`` (including the ML worker itself) only ever see clean pixels.
#
# Overlays are RGB-only — depth cameras (RealSense) skip this path.

_DETECTIONS_STALE_MS = 2000  # matches the OBSBOT C++ driver's `> 2000` check


@dataclass(slots=True, frozen=True)
class DetectionBox:
    """A single bounding box emitted by an ML worker."""

    label: str
    confidence: float
    x1: int
    y1: int
    x2: int
    y2: int


class _DetectionCache:
    """Thread-safe holder for the most recent detection batch.

    Updated by the Zenoh subscriber thread, read by the capture thread.
    Returns ``None`` from :meth:`snapshot` when the cache is empty or the
    batch is older than :data:`_DETECTIONS_STALE_MS`.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._boxes: list[DetectionBox] = []
        self._frame_w = 0
        self._frame_h = 0
        self._updated_at = 0.0

    def update(self, boxes: list[DetectionBox], frame_w: int, frame_h: int) -> None:
        with self._lock:
            self._boxes = boxes
            self._frame_w = frame_w
            self._frame_h = frame_h
            self._updated_at = time.monotonic()

    def snapshot(self) -> tuple[list[DetectionBox], int, int] | None:
        with self._lock:
            if not self._boxes:
                return None
            if (time.monotonic() - self._updated_at) * 1000.0 > _DETECTIONS_STALE_MS:
                return None
            # Return a shallow copy so callers can't mutate the cache's list
            # out from under the subscriber thread.  ``DetectionBox`` is frozen,
            # so the shallow copy is sufficient.
            return list(self._boxes), self._frame_w, self._frame_h


def _parse_detections_payload(payload: bytes) -> tuple[list[DetectionBox], int, int] | None:
    """Decode a JSON detection payload. Returns ``None`` on parse error."""
    try:
        data = json.loads(payload)
        dets_raw = data.get("detections") or []
        frame_w = int(data.get("frame_width", 0) or 0)
        frame_h = int(data.get("frame_height", 0) or 0)
        boxes: list[DetectionBox] = []
        for det in dets_raw:
            if not isinstance(det, dict):
                continue
            boxes.append(
                DetectionBox(
                    label=str(det.get("label", "?")),
                    confidence=float(det.get("confidence", 0.0) or 0.0),
                    x1=int(det.get("x1", 0) or 0),
                    y1=int(det.get("y1", 0) or 0),
                    x2=int(det.get("x2", 0) or 0),
                    y2=int(det.get("y2", 0) or 0),
                )
            )
    except (ValueError, TypeError, AttributeError):
        logger.debug("Failed to parse detection payload", exc_info=True)
        return None
    return boxes, frame_w, frame_h


def _draw_detections(
    frame: np.ndarray,
    boxes: list[DetectionBox],
    det_w: int,
    det_h: int,
) -> None:
    """Draw ``boxes`` in-place on ``frame``.

    Matches the OBSBOT C++ draw routine (same colors, font, and label
    background style) so the frontend renders both driver families
    identically.
    """
    import cv2

    h, w = frame.shape[:2]
    sx = (w / det_w) if det_w > 0 else 1.0
    sy = (h / det_h) if det_h > 0 else 1.0

    for box in boxes:
        x1 = int(box.x1 * sx)
        y1 = int(box.y1 * sy)
        x2 = int(box.x2 * sx)
        y2 = int(box.y2 * sy)

        cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 0), 2)

        label = f"{box.label} {box.confidence * 100.0:.0f}%"
        (text_w, text_h), _baseline = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.6, 1)
        # Place the label above the bbox when there's room, otherwise drop it
        # inside the top of the bbox so near-edge detections don't render with
        # the label clipped off the frame.
        if y1 - text_h - 6 >= 0:
            bg_top, bg_bottom, text_y = y1 - text_h - 6, y1, y1 - 4
        else:
            bg_top, bg_bottom, text_y = y1, y1 + text_h + 6, y1 + text_h + 2
        cv2.rectangle(
            frame,
            (x1, bg_top),
            (x1 + text_w + 4, bg_bottom),
            (0, 255, 0),
            cv2.FILLED,
        )
        cv2.putText(
            frame,
            label,
            (x1 + 2, text_y),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.6,
            (0, 0, 0),
            1,
            cv2.LINE_AA,
        )


def _detection_overlays_enabled_env() -> bool:
    """Resolve ``CYBERWAVE_DETECTION_OVERLAYS`` (defaults to enabled)."""
    raw = os.getenv("CYBERWAVE_DETECTION_OVERLAYS")
    if raw is None:
        return True
    return raw.strip().lower() not in ("0", "false", "no", "off")


# ── Workflow overlay (annotate-node-driven) ─────────────────────────
#
# The ``annotate`` workflow node publishes a JSON spec to
# ``FRAME_OVERLAY_CHANNEL`` (``frames/overlay``) that carries the
# user's styling parameters (``line_width``, ``font_scale``, label
# filter) on top of the raw detection list. The driver caches the
# latest payload per twin and composites it onto every frame
# pre-encode, *unconditionally* — overlay is additive, not pixel
# substitution, so it doesn't ride the privacy-fail-closed
# ``frame_filter_enabled`` gate that ``anonymize`` uses.
#
# When a fresh overlay payload is present it preempts the legacy
# raw-``detections/<runtime>`` overlay so the user's styling wins.
# When stale or absent, the driver falls back to ``_draw_detections``.


class _OverlayCache:
    """Thread-safe holder for the most recent overlay payload.

    ``payload`` is the dict returned by
    :func:`cyberwave.vision.build_overlay_payload`. Returns ``None``
    from :meth:`snapshot` when the cache is empty or the last update
    is older than :data:`_DETECTIONS_STALE_MS` (same TTL as the raw
    detection cache, keeping the two paths feel-equivalent to the
    operator).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._payload: dict | None = None
        self._updated_at = 0.0

    def update(self, payload: dict) -> None:
        with self._lock:
            self._payload = payload
            self._updated_at = time.monotonic()

    def snapshot(self) -> dict | None:
        with self._lock:
            if self._payload is None:
                return None
            if (time.monotonic() - self._updated_at) * 1000.0 > _DETECTIONS_STALE_MS:
                return None
            # Defensive shallow copy — mirrors ``_DetectionCache.snapshot``
            # so a misbehaving caller can't mutate the cached dict (e.g.
            # by sorting the boxes list in place) and corrupt subsequent
            # reads on the encoder thread.
            return dict(self._payload)


def _validate_overlay_payload(data: object) -> dict | None:
    """Schema-check an already-decoded overlay payload.

    The Zenoh subscriber gets the decoded dict directly from the data
    bus (``cw.data.publish`` writes a CONTENT_TYPE_JSON envelope and
    ``cw.data.subscribe`` strips the header + ``json.loads`` it), so
    the validation step doesn't need to know about wire bytes at all.
    Kept separate from :func:`_parse_overlay_payload` so the bytes-in
    test entry point stays usable.
    """
    if not isinstance(data, dict):
        return None
    # ``v == 1`` is the only schema this driver knows; anything else
    # is silently dropped so future bumps remain forward-compatible
    # (the worker keeps publishing, just nothing renders here).
    if data.get("v") != 1:
        return None
    if not isinstance(data.get("boxes"), list):
        return None
    return data


def _parse_overlay_payload(payload: bytes) -> dict | None:
    """Decode + validate an overlay JSON payload from raw bytes.

    Used by tests that want to exercise the full bytes-in / dict-out
    round-trip without spinning up a data bus. The driver subscriber
    callback uses :func:`_validate_overlay_payload` directly because
    ``cw.data.subscribe`` already hands back a decoded dict.
    """
    try:
        data = json.loads(payload)
    except (ValueError, TypeError):
        logger.debug("Failed to parse overlay payload", exc_info=True)
        return None
    return _validate_overlay_payload(data)


# Defensive cap on incoming polygon vertices per detection. The SDK
# helper caps at ~64 via approxPolyDP, so anything over this is either
# a misbehaving worker or a future schema change we don't know about.
# Worst-case 64 KB of int32 points per detection — bounded enough to
# composite at frame rate without DoSing the encoder.
_POLY_MAX_POINTS: int = 1024


def _overlay_color_from_box(box: dict) -> tuple[int, int, int]:
    """Return the pre-resolved BGR color from a box entry, or fall back to auto.

    ``build_overlay_payload`` in the SDK resolves the palette and includes
    ``color: [r, g, b]`` on each box so the driver never needs its own palette
    copy. For payloads from older SDK versions that lack the field we delegate
    to ``cyberwave.vision.annotate.label_color``; if the installed SDK predates
    that helper we degrade to a constant green so the frame callback never
    raises ``ImportError`` on every frame.
    """
    raw = box.get("color")
    if isinstance(raw, (list, tuple)) and len(raw) == 3:
        try:
            return (int(raw[0]), int(raw[1]), int(raw[2]))
        except (TypeError, ValueError):
            pass
    try:
        from cyberwave.vision.annotate import label_color
    except ImportError:
        return (0, 200, 0)
    return label_color(str(box.get("label", "")))


def _draw_overlay_masks(
    frame: np.ndarray,
    boxes: list,
    *,
    mask_alpha: float,
    mask_outline: bool,
    line_width: int,
) -> None:
    """Composite per-detection polygons onto ``frame`` in place.

    Polygons live in original-frame coordinates. Pixels outside any
    polygon are untouched. Boxes without a ``polygon`` field skip the
    mask path so detection-only models cost nothing here.
    """
    import cv2

    h, w = frame.shape[:2]
    fill_overlay: np.ndarray | None = None
    mask_acc_u8: np.ndarray | None = None
    outlines: list[tuple[np.ndarray, tuple[int, int, int]]] = []
    for box in boxes:
        poly = box.get("polygon")
        if not isinstance(poly, list) or not (3 <= len(poly) <= _POLY_MAX_POINTS):
            continue
        try:
            pts = np.array(
                [[int(p[0]), int(p[1])] for p in poly if len(p) >= 2],
                dtype=np.int32,
            )
        except (TypeError, ValueError):
            continue
        if pts.shape[0] < 3:
            continue
        np.clip(pts[:, 0], 0, w - 1, out=pts[:, 0])
        np.clip(pts[:, 1], 0, h - 1, out=pts[:, 1])
        color = _overlay_color_from_box(box)
        if mask_alpha > 0:
            if fill_overlay is None:
                fill_overlay = frame.copy()
                mask_acc_u8 = np.zeros((h, w), dtype=np.uint8)
            cv2.fillPoly(fill_overlay, [pts], color)
            cv2.fillPoly(mask_acc_u8, [pts], 1)
        if mask_outline:
            outlines.append((pts, color))
    if fill_overlay is not None and mask_acc_u8 is not None and mask_alpha > 0:
        mask_acc = mask_acc_u8.astype(bool)
        if mask_acc.any():
            blended = (
                mask_alpha * fill_overlay.astype(np.float32)
                + (1.0 - mask_alpha) * frame.astype(np.float32)
            ).astype(np.uint8)
            frame[mask_acc] = blended[mask_acc]
    if mask_outline and line_width > 0:
        for pts, color in outlines:
            cv2.polylines(frame, [pts], True, color, max(1, line_width))


def _draw_overlay(frame: np.ndarray, payload: dict) -> None:
    """Draw the overlay payload's boxes + captions on ``frame`` in place.

    Mirrors :func:`_draw_detections` visually but reads styling from the
    payload's ``style`` block. Per-box colour comes from the ``color`` field
    that ``build_overlay_payload`` in the SDK pre-resolves from the chosen
    palette — the driver has no palette knowledge and stays in sync with the
    SDK automatically.
    """
    import cv2

    boxes = payload.get("boxes") or []
    if not boxes:
        return
    style = payload.get("style") or {}
    line_width = max(0, int(style.get("line_width", 2) or 0))
    font_scale = float(style.get("font_scale", 0.5) or 0.0)
    show_confidence = bool(style.get("show_confidence", True))
    mask_alpha = max(0.0, min(1.0, float(style.get("mask_alpha", 0.0) or 0.0)))
    mask_outline = bool(style.get("mask_outline", True))

    h, w = frame.shape[:2]

    if mask_alpha > 0 or mask_outline:
        _draw_overlay_masks(
            frame,
            boxes,
            mask_alpha=mask_alpha,
            mask_outline=mask_outline,
            line_width=line_width,
        )

    for box in boxes:
        coords = box.get("box_2d")
        if not isinstance(coords, list | tuple) or len(coords) != 4:
            continue
        try:
            x1 = max(0, min(int(coords[0]), w - 1))
            y1 = max(0, min(int(coords[1]), h - 1))
            x2 = max(0, min(int(coords[2]), w - 1))
            y2 = max(0, min(int(coords[3]), h - 1))
        except (TypeError, ValueError):
            continue
        if x2 <= x1 or y2 <= y1:
            continue

        color = _overlay_color_from_box(box)

        if line_width > 0:
            cv2.rectangle(frame, (x1, y1), (x2, y2), color, line_width)

        if font_scale <= 0:
            continue

        label_text = str(box.get("label", "?"))
        if show_confidence:
            try:
                conf = float(box.get("conf", 0.0) or 0.0)
            except (TypeError, ValueError):
                conf = 0.0
            label_text = f"{label_text} {conf * 100.0:.0f}%"
        text_thickness = max(1, line_width // 2 if line_width > 0 else 1)
        (text_w, text_h), _baseline = cv2.getTextSize(
            label_text, cv2.FONT_HERSHEY_SIMPLEX, font_scale, text_thickness
        )
        if y1 - text_h - 6 >= 0:
            bg_top, bg_bottom, text_y = y1 - text_h - 6, y1, y1 - 4
        else:
            bg_top, bg_bottom, text_y = y1, y1 + text_h + 6, y1 + text_h + 2
        cv2.rectangle(
            frame,
            (x1, bg_top),
            (x1 + text_w + 4, bg_bottom),
            color,
            cv2.FILLED,
        )
        cv2.putText(
            frame,
            label_text,
            (x1 + 2, text_y),
            cv2.FONT_HERSHEY_SIMPLEX,
            font_scale,
            (0, 0, 0),
            text_thickness,
            cv2.LINE_AA,
        )


def _frame_filter_enabled_env() -> bool:
    """Resolve ``CYBERWAVE_METADATA_FRAME_FILTER_ENABLED`` (defaults to disabled).

    Opt-in, not opt-out — a misconfiguration should not silently start
    blanking WebRTC frames.
    """
    raw = os.getenv("CYBERWAVE_METADATA_FRAME_FILTER_ENABLED")
    if raw is None:
        return False
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _enforce_frame_filter_requires_data_bus(
    *,
    frame_filter_enabled: bool,
    data_bus_available: bool,
) -> None:
    """Abort startup if the frame filter is requested but the data bus is down.

    Extracted so the invariant can be unit-tested without spinning up the
    full ``main()`` coroutine.

    The frame filter receives anonymised frames over the Zenoh data bus.
    Without a bus, the driver's ``frame_callback`` is never installed and
    the SDK encodes the raw camera frame — which is the exact opposite
    of the privacy opt-in the operator requested. Fail closed at startup.

    Raises ``SystemExit(1)`` when the filter is enabled but the data bus
    is unavailable. Returns ``None`` in every other case (filter disabled,
    or filter enabled with the bus up).
    """
    if not frame_filter_enabled:
        return
    if data_bus_available:
        return
    logger.error(
        "CYBERWAVE_METADATA_FRAME_FILTER_ENABLED=true but the Zenoh data "
        "bus is unavailable. The filter depends on the data bus to receive "
        "anonymised frames; without it the driver would stream raw camera "
        "frames to WebRTC, defeating the privacy opt-in. Either set "
        "CYBERWAVE_DATA_BACKEND=zenoh and install eclipse-zenoh "
        "(`pip install 'cyberwave[camera,zenoh]'`), or set "
        "CYBERWAVE_METADATA_FRAME_FILTER_ENABLED=false and restart the driver."
    )
    sys.exit(1)


def _build_depth_stream_extras(
    depth_fps: int,
    depth_callback: object,
) -> dict[str, object]:
    """Extra ``stream_kwargs`` for RGBD (RealSense) twins.

    Must NOT include ``camera_type`` or ``enable_depth``:
    ``DepthCameraTwin.stream_video_background`` sets both, so a
    collision on the inner ``client.video_stream(...)`` call would
    raise ``TypeError`` at startup. Pinned by the test suite.
    """
    return {
        "depth_fps": depth_fps,
        "depth_callback": depth_callback,
    }


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

    def _first_sensor_id_by_type(wanted_type: str) -> str | None:
        for entry in sensors:
            if isinstance(entry, dict) and entry.get("type") == wanted_type:
                sid = entry.get("id")
                if sid is not None:
                    return str(sid)
        return None

    # WebRTC / MQTT / color-Zenoh sensor identifier — "first sensor wins"
    # preserves the pre-existing channel-name contract for RGB twins.
    camera_name: str | None = None
    if sensors and isinstance(sensors[0], dict):
        sid = sensors[0].get("id")
        if sid is not None:
            camera_name = str(sid)

    # New (RealSense-only) channel — derived by type so downstream
    # perception nodes can look up the right depth intrinsics from
    # cw-driver.yml. No regression risk: the ``depth/<sensor>`` channel
    # didn't exist before this change.
    depth_sensor_name = _first_sensor_id_by_type("depth") or "default"

    try:
        depth_fps = int(os.getenv("CYBERWAVE_METADATA_DEPTH_FPS", "30"))
    except ValueError:
        logger.warning(
            "Invalid CYBERWAVE_METADATA_DEPTH_FPS=%r; falling back to 30",
            os.getenv("CYBERWAVE_METADATA_DEPTH_FPS"),
        )
        depth_fps = 30

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

    # Optional ``metadata.resolution`` override. When unset, the SDK uses its
    # ``Resolution.VGA`` (640x480) default, which is fine for the vast majority
    # of UVC webcams. Sensors that don't advertise 640x480 (e.g. entry-level
    # thermal cameras whose only modes are 256x192 / 256x384) need this pinned
    # to a native mode — see ``_parse_resolution`` for the rationale.
    resolution_override = _parse_resolution(os.getenv("CYBERWAVE_METADATA_RESOLUTION"))

    logger.info(
        "Initializing camera driver for twin %s (asset=%s, device=%s, camera_name=%s, resolution=%s)",
        twin_uuid,
        asset_key,
        camera_id,
        camera_name or "(from twin API default)",
        f"{resolution_override[0]}x{resolution_override[1]}"
        if resolution_override
        else "(SDK default)",
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

    # ── Depth Zenoh publisher (RealSense twins only) ──
    # Second single-slot buffer + publisher thread that streams the
    # aligned depth frame on ``depth/<depth_sensor_name>``. Depth is
    # always raw uint16 — JPEG would corrupt millimeter values.
    depth_slot: _FrameSlot | None = None
    stop_depth_publisher = threading.Event()
    depth_publisher_thread: threading.Thread | None = None
    if is_depth_camera and data_bus is not None:
        depth_channel = f"depth/{depth_sensor_name}"
        depth_slot = _FrameSlot()
        depth_publisher_thread = threading.Thread(
            target=_zenoh_publisher_thread,
            args=(data_bus, depth_slot, stop_depth_publisher, depth_channel, depth_fps),
            kwargs={"encoding": "raw"},
            daemon=True,
            name="zenoh-depth-publisher",
        )
        depth_publisher_thread.start()
        logger.info(
            "Zenoh depth publishing enabled on channel '%s' (fps=%d, encoding=raw)",
            depth_channel,
            depth_fps,
        )

    # ── Detection overlay subscription ──
    # Subscribe to ``cw/<twin_uuid>/data/detections/**`` so ML workers
    # (ultralytics, onnxruntime, ...) can push YOLO-style results that the
    # driver draws on the WebRTC stream.  Same payload schema as the C++
    # OBSBOT driver.
    #
    # Two overlay sources coexist:
    #   - This raw ``detections/**`` channel: fallback path that draws
    #     whatever the model detected, in the driver's default style.
    #   - ``FRAME_OVERLAY_CHANNEL`` (below): the ``annotate`` workflow
    #     node's styled spec. When fresh, it preempts the raw fallback
    #     so the author's ``line_width`` / ``font_scale`` choices win.
    detection_cache: _DetectionCache | None = None
    detection_subscription = None
    overlay_cache: _OverlayCache | None = None
    overlay_subscription = None
    if data_bus is not None and not is_depth_camera and _detection_overlays_enabled_env():
        try:
            from cyberwave.data.backend import Sample
            from cyberwave.data.keys import build_wildcard

            local_cache = _DetectionCache()
            detections_key = build_wildcard(
                twin_uuid=twin_uuid,
                channel="detections",
                prefix=data_bus.key_prefix,
            )

            def _on_detection_sample(sample: Sample) -> None:
                parsed = _parse_detections_payload(sample.payload)
                if parsed is None:
                    return
                boxes, frame_w, frame_h = parsed
                local_cache.update(boxes, frame_w, frame_h)

            detection_subscription = data_bus.backend.subscribe(
                detections_key,
                _on_detection_sample,
                policy="latest",
            )
            detection_cache = local_cache
            logger.info("Detection overlay subscriber active on key '%s'", detections_key)
        except Exception as exc:
            logger.warning(
                "Detection overlay subscription failed (%s: %s); bounding boxes disabled",
                type(exc).__name__,
                exc,
                exc_info=True,
            )
            detection_cache = None
            detection_subscription = None
    elif data_bus is not None and is_depth_camera:
        logger.info("Detection overlays disabled: depth cameras are not supported")

    # Workflow overlay subscription is independent of:
    #   - ``frame_filter_enabled`` (overlay is additive, not pixel
    #     substitution, so the privacy fail-closed gate doesn't apply).
    #   - ``CYBERWAVE_DETECTION_OVERLAYS`` (that flag toggles the raw
    #     ``detections/<runtime>`` fallback; the annotate channel is a
    #     separate, intentional surface — operators set the env var to
    #     turn off the *fallback* and rely on annotate, not the other
    #     way around).
    # Depth cameras are still excluded — colour overlays don't make
    # sense over a depth heatmap and the current draw helper is BGR.
    if data_bus is not None and not is_depth_camera:
        try:
            local_overlay = _OverlayCache()

            def _on_overlay_decoded(decoded: object) -> None:
                # ``data_bus.subscribe(channel, cb)`` (default
                # ``raw=False``) decodes the wire envelope and hands
                # ``cb`` the deserialised payload — a ``dict`` for our
                # CONTENT_TYPE_JSON publish. Validate the schema and
                # cache; ignore unknown shapes / future schema bumps so
                # a worker upgrade doesn't crash the driver.
                parsed = _validate_overlay_payload(decoded)
                if parsed is None:
                    return
                local_overlay.update(parsed)

            overlay_subscription = data_bus.subscribe(
                FRAME_OVERLAY_CHANNEL,
                _on_overlay_decoded,
            )
            overlay_cache = local_overlay
            logger.info(
                "Workflow overlay subscriber active on channel '%s'",
                FRAME_OVERLAY_CHANNEL,
            )
        except Exception as exc:
            logger.warning(
                "Workflow overlay subscription failed (%s: %s); annotate-node "
                "overlays disabled (raw detections fallback unaffected)",
                type(exc).__name__,
                exc,
                exc_info=True,
            )
            overlay_cache = None
            overlay_subscription = None

    # ── Frame-filter subscription ──
    # Subscribe to ``FILTERED_FRAME_CHANNEL`` (``frames/filtered``) so a
    # worker-processed ("anonymised") frame can be substituted into the
    # WebRTC stream before encoding. Driver-side opt-in via the
    # ``CYBERWAVE_METADATA_FRAME_FILTER_ENABLED`` twin metadata flag.
    #
    # When enabled but no fresh, shape-matched processed frame is available,
    # the driver emits a black frame — fail-closed, no "raw" fallback. See
    # ``frame_filter.py`` for the full contract.
    #
    # NOTE: This is a temporary port from the generic-camera driver in
    # ``cyberwave-edge-runtime`` so CYB-1716's e2e ships through the
    # currently-published ``cyberwaveos/camera-driver`` image. Delete this
    # block (and ``frame_filter.py``) once the generic-camera consolidation
    # lands and the backend asset registry repoints to that image.
    frame_filter_enabled = _frame_filter_enabled_env()

    # Privacy-safe fail-closed: if the operator asked for the frame
    # filter but the data bus is unavailable (CYBERWAVE_DATA_BACKEND
    # unset, eclipse-zenoh missing, init failure outside zenoh_only,
    # ...), the driver has no way to receive anonymised frames. The
    # frame_callback below is only installed when data_bus is not None,
    # so a silent bring-up would send RAW camera frames to WebRTC —
    # the exact opposite of what the opt-in requested. The helper
    # aborts startup with a clear message instead.
    _enforce_frame_filter_requires_data_bus(
        frame_filter_enabled=frame_filter_enabled,
        data_bus_available=data_bus is not None,
    )

    # Freshness is a soft knob: sensible default exists, so a typo falls
    # back to the default with a warning (unlike FPS above, which fails
    # the driver — FPS is a hardware property with no safe guess).
    # ``FrameFilter.__init__`` already clamps negatives to 0, and a 0
    # value is a legitimate "force blank frames" test mode — leave both
    # alone and let the filter do what the operator asked.
    _raw_freshness = os.getenv("CYBERWAVE_METADATA_FRAME_FILTER_FRESHNESS_MS")
    try:
        freshness_ms = float(_raw_freshness) if _raw_freshness else FRESHNESS_MS
    except ValueError:
        logger.warning(
            "Invalid CYBERWAVE_METADATA_FRAME_FILTER_FRESHNESS_MS=%r; "
            "using default %.0f ms",
            _raw_freshness,
            FRESHNESS_MS,
        )
        freshness_ms = FRESHNESS_MS

    frame_filter = FrameFilter(
        channel=FILTERED_FRAME_CHANNEL if frame_filter_enabled else None,
        freshness_ms=freshness_ms,
    )
    frame_filter_subscription = None
    if data_bus is not None and frame_filter.enabled:
        try:
            frame_filter_subscription = data_bus.subscribe(
                FILTERED_FRAME_CHANNEL,
                frame_filter.store_processed,
            )
            logger.info(
                "Frame-filter subscriber active on channel '%s' (freshness=%.0f ms)",
                FILTERED_FRAME_CHANNEL,
                frame_filter.freshness_s * 1000.0,
            )
        except Exception:
            # Subscription failed even though the bus is up (transient
            # Zenoh router hiccup, key-expression collision, …). Keep
            # fail-closed: the frame_filter is still enabled so
            # ``apply()`` returns a blank frame on every call, and the
            # dispatched frame_callback below substitutes those blanks
            # into WebRTC. Log loudly so the operator can investigate
            # without silently broadcasting raw video.
            logger.error(
                "Frame-filter subscription failed; driver will emit blank "
                "frames until the subscription recovers (fail-closed). Raw "
                "frames are NOT substituted back in.",
                exc_info=True,
            )

    def _on_frame(frame: np.ndarray, _frame_count: int) -> None:
        # Pipeline order (all optional, flagged independently):
        #   1. Stage the raw captured frame into ``frame_slot`` so the Zenoh
        #      publisher thread always sees clean pixels.
        #   2. If the frame-filter is enabled, substitute the worker's
        #      processed frame (or a black frame when stale) into ``frame``
        #      in place — WebRTC encodes whatever ``frame`` holds at return.
        #   3. If a workflow overlay is fresh, composite it on top
        #      (the ``annotate`` node's styled spec). Otherwise fall
        #      back to the raw ``detections/<runtime>`` cache.
        # Zero-copy fast path when no in-place mutation will happen.
        if frame_slot is None:
            return
        overlay_payload = (
            overlay_cache.snapshot() if overlay_cache is not None else None
        )
        snap = (
            detection_cache.snapshot()
            if detection_cache is not None and overlay_payload is None
            else None
        )
        will_mutate = (
            frame_filter.enabled or overlay_payload is not None or snap is not None
        )
        if will_mutate:
            # Copy first so Zenoh subscribers (and ML workers) always see
            # the raw pixels, regardless of what we do to ``frame`` below.
            frame_slot.put(frame.copy())
        else:
            frame_slot.put(frame)
            return

        if frame_filter.enabled:
            replacement = frame_filter.apply(frame)
            if replacement is not None:
                np.copyto(frame, replacement)

        if overlay_payload is not None:
            _draw_overlay(frame, overlay_payload)
        elif snap is not None:
            boxes, det_w, det_h = snap
            _draw_detections(frame, boxes, det_w, det_h)

    frame_callback = _on_frame if data_bus is not None else None

    def _on_depth_frame(depth: np.ndarray, _frame_count: int) -> None:
        # SDK already gave us a Python-owned copy; nobody mutates depth,
        # so no defensive copy needed before staging.
        if depth_slot is not None:
            depth_slot.put(depth)

    depth_callback = _on_depth_frame if depth_slot is not None else None

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

    # Build the kwargs dict once; only forward ``resolution`` when explicitly
    # set so the SDK still applies its ``Resolution.VGA`` default otherwise.
    # Forwarding ``resolution=None`` would short-circuit the default and trip
    # the SDK's resolution parsing.
    stream_kwargs: dict[str, object] = {
        "camera_id": camera_id,
        "camera_name": camera_name,
        "fps": 30,
        "frame_callback": frame_callback,
    }
    if resolution_override is not None:
        stream_kwargs["resolution"] = resolution_override

    # DepthCameraTwin.stream_video_background already hardcodes
    # ``camera_type="realsense"`` and ``enable_depth=True``; we only add
    # what the SDK doesn't set. See ``_build_depth_stream_extras``.
    if is_depth_camera:
        stream_kwargs.update(_build_depth_stream_extras(depth_fps, depth_callback))

    stream_started = False
    try:
        logger.info("Starting camera stream for twin %s...", twin_uuid)
        try:
            await camera.stream_video_background(**stream_kwargs)
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
            # Reuse the same kwargs as the primary attempt so the optional
            # ``resolution`` override applies to the fallback too — same
            # sensor class, same mode constraints.
            fallback_kwargs = dict(stream_kwargs, camera_id=fallback_camera_id)
            try:
                await camera.stream_video_background(**fallback_kwargs)
                stream_started = True
            except Exception as fallback_error:
                raise HardwareConnectionError(
                    f"Camera hardware unavailable: could not start stream with fallback '{fallback_camera_id}'"
                ) from fallback_error
        logger.info("Camera stream started. Running auto-reconnect loop until shutdown...")
        # Hand off to the SDK's reconnection loop: it monitors WebRTC
        # connectionState and republishes a new offer when the consumer
        # tab/network drops the peer connection. Without this the camera
        # would stream for one session and then go dark until the driver
        # is restarted.
        #
        # ``stream_video_background`` (called above) calls ``streamer.start()``
        # but does not arm the reconnect monitor — only ``stream_video`` and
        # the ``CameraStreamManager`` flip ``_should_reconnect`` after starting.
        # ``run_with_auto_reconnect`` itself only arms the monitor on the
        # branch where it has to call ``start()`` itself (i.e. ``pc is None``
        # at entry), so when we pre-seed the connection here the monitor
        # observes ``_should_reconnect == False`` and never re-offers after a
        # disconnect. Set the flag explicitly so the monitor task does its job.
        streamer = camera.streamer()
        streamer._should_reconnect = streamer.auto_reconnect
        await streamer.run_with_auto_reconnect(stop_event=stop_event)
    finally:
        logger.info("Stopping camera stream...")
        if detection_subscription is not None:
            try:
                detection_subscription.close()
            except Exception:
                logger.debug("Detection subscription close failed", exc_info=True)
        if overlay_subscription is not None:
            try:
                overlay_subscription.close()
            except Exception:
                logger.debug("Overlay subscription close failed", exc_info=True)
        if frame_filter_subscription is not None:
            try:
                frame_filter_subscription.close()
            except Exception:
                logger.debug("Frame-filter subscription close failed", exc_info=True)
        stop_publisher.set()
        if publisher_thread is not None:
            publisher_thread.join(timeout=5.0)
        stop_depth_publisher.set()
        if depth_publisher_thread is not None:
            depth_publisher_thread.join(timeout=5.0)
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
