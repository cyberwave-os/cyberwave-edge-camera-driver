"""Unit tests for the depth-Zenoh wiring helpers in ``main.py``.

Deterministic guardrails around the standalone driver script. The full
hardware path (RealSense -> SDK -> callback) is covered by the e2e
harness.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import numpy as np
import pytest

import main  # noqa: E402 — conftest inserts driver root on sys.path


class _FakeDataBus:
    """In-memory ``DataBus`` stand-in — captures publishes without touching Zenoh."""

    def __init__(self) -> None:
        self.published: list[tuple[str, Any, dict[str, Any] | None]] = []
        # ``_backend`` is read by the driver's start-up log line; a dummy
        # object is enough to keep that log from crashing.
        self._backend = type("_FakeBackend", (), {})()

    def publish(
        self,
        channel: str,
        sample: Any,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.published.append((channel, sample, metadata))

    def publish_raw(self, channel: str, payload: bytes) -> None:
        self.published.append((channel, payload, None))


def test_frame_slot_pointer_swap_semantics() -> None:
    """Producer faster than consumer: older frames are silently dropped."""
    slot = main._FrameSlot()
    a = np.zeros((4, 4), dtype=np.uint16)
    b = np.ones((4, 4), dtype=np.uint16)

    slot.put(a)
    slot.put(b)

    assert slot.take(timeout=0.1) is b
    assert slot.take(timeout=0.05) is None


def test_zenoh_publisher_thread_publishes_depth_on_expected_channel() -> None:
    """Publisher thread forwards depth frames verbatim on the depth channel."""
    bus = _FakeDataBus()
    slot = main._FrameSlot()
    stop = threading.Event()

    thread = threading.Thread(
        target=main._zenoh_publisher_thread,
        args=(bus, slot, stop, "depth/depth_camera", 30),
        kwargs={"encoding": "raw"},
        daemon=True,
    )
    thread.start()
    try:
        depth = (np.arange(16, dtype=np.uint16) * 100).reshape(4, 4)
        slot.put(depth)

        deadline = time.monotonic() + 1.0
        while not bus.published and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        stop.set()
        thread.join(timeout=2.0)

    assert bus.published
    channel, payload, metadata = bus.published[0]
    assert channel == "depth/depth_camera"
    assert isinstance(payload, np.ndarray)
    assert payload.dtype == np.uint16
    assert payload.shape == (4, 4)
    assert metadata == {"fps": 30}


def test_zenoh_publisher_thread_never_jpeg_encodes_when_raw() -> None:
    """Raw encoding must go through ``publish`` (numpy path), not ``publish_raw``.

    ``publish_raw`` bypasses the SDK header envelope, so subscribers
    would lose shape/dtype metadata and the ``HeaderTemplate`` fast
    path.
    """
    bus = _FakeDataBus()
    slot = main._FrameSlot()
    stop = threading.Event()

    thread = threading.Thread(
        target=main._zenoh_publisher_thread,
        args=(bus, slot, stop, "depth/depth_camera", 30),
        kwargs={"encoding": "raw"},
        daemon=True,
    )
    thread.start()
    try:
        slot.put(np.zeros((2, 2), dtype=np.uint16))
        deadline = time.monotonic() + 1.0
        while not bus.published and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        stop.set()
        thread.join(timeout=2.0)

    _, payload, _ = bus.published[0]
    assert isinstance(payload, np.ndarray)


def test_build_depth_stream_extras_never_carries_camera_type() -> None:
    """Guard against ``TypeError: multiple values for 'camera_type'``.

    ``DepthCameraTwin.stream_video_background`` sets ``camera_type`` and
    ``enable_depth`` itself; the driver's extras must not collide with
    those. An earlier iteration of this feature made exactly that
    mistake — this test drives the real helper to prevent regressions.
    """
    extras = main._build_depth_stream_extras(
        depth_fps=30, depth_callback=lambda d, i: None
    )
    for forbidden in ("camera_type", "enable_depth"):
        assert forbidden not in extras, (
            f"_build_depth_stream_extras must not return {forbidden!r}"
        )
    assert "depth_fps" in extras
    assert "depth_callback" in extras


@pytest.mark.parametrize(
    "sensors, expected_color, expected_depth",
    [
        (
            [
                {"id": "color_camera", "type": "rgb"},
                {"id": "depth_camera", "type": "depth"},
            ],
            "color_camera",
            "depth_camera",
        ),
        (
            [{"id": "depth_camera", "type": "depth"}],
            None,
            "depth_camera",
        ),
        (
            [{"id": "color_camera", "type": "rgb"}],
            "color_camera",
            None,
        ),
    ],
)
def test_sensor_id_resolution_by_type(
    sensors: list[dict[str, Any]],
    expected_color: str | None,
    expected_depth: str | None,
) -> None:
    """Mirrors the closure-scoped ``_first_sensor_id_by_type`` inside ``main.main()``."""

    def _first_sensor_id_by_type(wanted_type: str) -> str | None:
        for entry in sensors:
            if isinstance(entry, dict) and entry.get("type") == wanted_type:
                sid = entry.get("id")
                if sid is not None:
                    return str(sid)
        return None

    assert _first_sensor_id_by_type("rgb") == expected_color
    assert _first_sensor_id_by_type("depth") == expected_depth
