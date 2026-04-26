"""Frame-filter helpers for the legacy camera driver.

The driver can optionally consult a Zenoh channel for an "anonymised" or
otherwise transformed version of the captured frame BEFORE it is encoded
into the WebRTC stream. This decouples the privacy/anonymisation policy
(provided by a worker) from the stream lifecycle (owned by the driver).

This module contains everything needed to implement that contract that
does NOT depend on the cyberwave SDK — making it easy to unit-test in
isolation. The driver wires the subscription side via its existing
:class:`cyberwave.data.DataBus`.

.. note::

    This file is intentionally a verbatim port of the same module in the
    newer ``cyberwave-edge-runtime`` generic-camera driver
    (``runtime-services/drivers/native/cyberwave/generic-camera/src/frame_filter.py``).
    It lives here temporarily so the frame-filter feature ships in the
    currently-published ``cyberwaveos/camera-driver`` image for CYB-1716.
    When the generic-camera driver consolidation (CYB-xxxx) lands, delete
    this copy and depend on the single source of truth.

Wire contract:

  * Channel name comes from :data:`cyberwave.data.FILTERED_FRAME_CHANNEL`
    (``"frames/filtered"``). Per-twin isolation is automatic — the
    DataBus injects the twin UUID into the Zenoh key.
  * Payload is a numpy ndarray that **must match the raw camera frame's
    shape and dtype** (typically ``uint8`` BGR). Mismatched frames are
    dropped at :meth:`FrameFilter.apply` and the driver emits a blank
    frame, fail-closed.

Driver-side opt-in is the per-twin metadata flag
``CYBERWAVE_METADATA_FRAME_FILTER_ENABLED`` (see ``main.py``).

Behavior:
  - When enabled but no fresh, well-formed processed frame is available
    (worker not running, slow, crashed, or publishing the wrong shape/
    dtype) the driver emits a blank (black) frame of the same shape as
    the raw input — the privacy-safe default. There is intentionally
    no "raw" fallback: to debug what the camera sees, disable the
    filter entirely.
  - Stale and shape-mismatch conditions are logged at most once every
    :data:`STALE_LOG_S` seconds to avoid log floods when the worker
    is misbehaving.
"""

from __future__ import annotations

import logging
import threading
import time

import numpy as np

logger = logging.getLogger(__name__)

# Tuning knobs — defaults. ``FRESHNESS_MS`` is tunable per-driver via the
# ``freshness_ms`` constructor kwarg (the driver wires this up from its
# own env-var parsing; see the driver's module docs). ``STALE_LOG_S`` is
# a log-hygiene decision, not an operational knob, and is intentionally
# not exposed.
# 200 ms ≈ 6 frames at 30 fps; large enough to absorb a worker hiccup,
# small enough that prolonged staleness flips to blank within ~one cycle.
FRESHNESS_MS = 200.0
STALE_LOG_S = 30.0


class ProcessedFrameSlot:
    """Thread-safe single-slot store for the latest processed frame.

    Updated by the Zenoh subscriber thread; read by the driver's frame
    callback (which runs on whatever thread the SDK dispatches from).
    """

    def __init__(self) -> None:
        self._frame: np.ndarray | None = None
        self._timestamp: float = 0.0
        self._lock = threading.Lock()

    def put(self, frame: np.ndarray) -> None:
        with self._lock:
            self._frame = frame
            self._timestamp = time.monotonic()

    def get_if_fresh(self, max_age_s: float) -> np.ndarray | None:
        """Return the latest frame if it arrived within *max_age_s* seconds.

        Returns ``None`` if the slot is empty or the cached frame is stale.
        """
        with self._lock:
            if self._frame is None:
                return None
            if time.monotonic() - self._timestamp > max_age_s:
                return None
            return self._frame

    @property
    def timestamp(self) -> float:
        """Monotonic timestamp of the latest ``put()``. ``0.0`` if empty."""
        with self._lock:
            return self._timestamp

    @timestamp.setter
    def timestamp(self, value: float) -> None:
        """Override the cached timestamp (used by tests to simulate staleness)."""
        with self._lock:
            self._timestamp = value


class FrameFilter:
    """Per-driver state container for the frame-filter feature.

    Owns the latest-frame slot and the rate-limited stale logger. The
    driver is responsible for actually subscribing to Zenoh and feeding
    processed frames into :meth:`store_processed`.

    The ``freshness_ms`` and ``stale_log_s`` constructor parameters
    default to the module constants. Drivers override ``freshness_ms``
    from their own env-var parsing (e.g.
    ``CYBERWAVE_METADATA_FRAME_FILTER_FRESHNESS_MS``) — this module
    intentionally stays env-free so it's trivially unit-testable without
    the SDK. ``stale_log_s`` is a test seam; not meant to be tuned in
    production.
    """

    def __init__(
        self,
        *,
        channel: str | None,
        freshness_ms: float = FRESHNESS_MS,
        stale_log_s: float = STALE_LOG_S,
    ) -> None:
        self.channel = channel
        self.freshness_s = max(0.0, freshness_ms / 1000.0)
        self.stale_log_s = max(0.0, stale_log_s)
        self._slot = ProcessedFrameSlot()
        self._last_stale_log_ts: float = 0.0

    @property
    def enabled(self) -> bool:
        return self.channel is not None

    @property
    def slot(self) -> ProcessedFrameSlot:
        return self._slot

    def store_processed(self, frame: object) -> None:
        """Zenoh subscriber callback target. Silently drops non-ndarray samples.

        Shape / dtype validation happens in :meth:`apply` — at this point
        we don't yet have the raw frame to compare against.
        """
        if not isinstance(frame, np.ndarray):
            logger.debug(
                "[FRAME_FILTER] Ignoring non-ndarray sample (type=%s)",
                type(frame).__name__,
            )
            return
        self._slot.put(frame)

    def apply(self, raw_frame: np.ndarray) -> np.ndarray | None:
        """Decide what frame the SDK should encode for *raw_frame*.

        Returns:
            * ``None`` to keep the raw frame (filter disabled).
            * A new ``ndarray`` to replace the raw frame (the worker's
              processed frame when fresh and shape/dtype-matched,
              otherwise a black frame).
        """
        if not self.enabled:
            return None
        processed = self._slot.get_if_fresh(self.freshness_s)
        if processed is None:
            self._maybe_warn(
                "no fresh processed frame within %.0f ms", self.freshness_s * 1000.0
            )
            return np.zeros_like(raw_frame)
        if processed.shape != raw_frame.shape or processed.dtype != raw_frame.dtype:
            # A worker publishing the wrong shape/dtype would corrupt the
            # WebRTC encoder — fail closed and warn.
            self._maybe_warn(
                "processed frame shape/dtype mismatch (got %s/%s, expected %s/%s)",
                processed.shape,
                processed.dtype,
                raw_frame.shape,
                raw_frame.dtype,
            )
            return np.zeros_like(raw_frame)
        return processed

    def _maybe_warn(self, fmt: str, *args: object) -> None:
        """Rate-limited warning for stale or malformed processed frames."""
        if self.stale_log_s <= 0:
            return
        now = time.monotonic()
        if now - self._last_stale_log_ts < self.stale_log_s:
            return
        self._last_stale_log_ts = now
        logger.warning(
            "[FRAME_FILTER] " + fmt + " on '%s' — emitting blank frame. "
            "Is the worker running and publishing the right shape/dtype?",
            *args,
            self.channel,
        )
