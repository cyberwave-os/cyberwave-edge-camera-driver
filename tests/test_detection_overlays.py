"""Unit tests for the detection-overlay helpers in ``main.py``.

These helpers are pure-Python and do not depend on a running camera, Zenoh
router, or the Cyberwave backend, so we can exercise them directly.
"""

from __future__ import annotations

import json
import time
from unittest.mock import patch

import numpy as np
import pytest

from main import (
    DetectionBox,
    _detection_overlays_enabled_env,
    _DetectionCache,
    _draw_detections,
    _parse_detections_payload,
)

# ── _parse_detections_payload ──────────────────────────────────────


def test_parse_valid_payload() -> None:
    payload = json.dumps(
        {
            "detections": [
                {"label": "person", "confidence": 0.92, "x1": 10, "y1": 20, "x2": 100, "y2": 200},
                {"label": "car", "confidence": 0.77, "x1": 150, "y1": 30, "x2": 400, "y2": 280},
            ],
            "frame_width": 1920,
            "frame_height": 1080,
        }
    ).encode()

    result = _parse_detections_payload(payload)
    assert result is not None
    boxes, w, h = result

    assert (w, h) == (1920, 1080)
    assert [b.label for b in boxes] == ["person", "car"]
    assert boxes[0] == DetectionBox("person", 0.92, 10, 20, 100, 200)


def test_parse_empty_detections_list() -> None:
    payload = json.dumps({"detections": [], "frame_width": 640, "frame_height": 480}).encode()
    result = _parse_detections_payload(payload)
    assert result == ([], 640, 480)


def test_parse_float_coordinates_truncates() -> None:
    # ONNX exports sometimes emit float pixel coords; matches OBSBOT C++ behavior.
    payload = json.dumps(
        {
            "detections": [
                {"label": "person", "confidence": 0.9, "x1": 10.7, "y1": 20.3, "x2": 100.9, "y2": 200.1}
            ],
            "frame_width": 640,
            "frame_height": 480,
        }
    ).encode()

    result = _parse_detections_payload(payload)
    assert result is not None
    boxes, _, _ = result
    assert boxes[0] == DetectionBox("person", 0.9, 10, 20, 100, 200)


def test_parse_invalid_json_returns_none() -> None:
    assert _parse_detections_payload(b"not json") is None


def test_parse_missing_fields_uses_defaults() -> None:
    payload = json.dumps({"detections": [{"confidence": 0.5}]}).encode()
    result = _parse_detections_payload(payload)
    assert result is not None
    boxes, w, h = result
    assert (w, h) == (0, 0)
    assert boxes[0] == DetectionBox("?", 0.5, 0, 0, 0, 0)


def test_parse_skips_non_dict_detections() -> None:
    payload = json.dumps(
        {"detections": ["not a dict", 42, {"label": "person", "x1": 1, "y1": 2, "x2": 3, "y2": 4}]}
    ).encode()
    result = _parse_detections_payload(payload)
    assert result is not None
    boxes, _, _ = result
    assert len(boxes) == 1
    assert boxes[0].label == "person"


# ── _DetectionCache ────────────────────────────────────────────────


def test_cache_empty_returns_none() -> None:
    assert _DetectionCache().snapshot() is None


def test_cache_returns_fresh_batch() -> None:
    cache = _DetectionCache()
    box = DetectionBox("dog", 0.8, 1, 2, 3, 4)
    cache.update([box], 640, 480)

    snap = cache.snapshot()
    assert snap is not None
    boxes, w, h = snap
    assert boxes == [box]
    assert (w, h) == (640, 480)


def test_cache_returns_none_when_stale() -> None:
    cache = _DetectionCache()
    cache.update([DetectionBox("dog", 0.8, 1, 2, 3, 4)], 640, 480)

    # Cache staleness is hardcoded to 1000 ms; fake the clock to jump 2s.
    with patch("main.time.monotonic", return_value=time.monotonic() + 2.0):
        assert cache.snapshot() is None


def test_cache_update_replaces_prior_batch() -> None:
    cache = _DetectionCache()
    cache.update([DetectionBox("dog", 0.5, 0, 0, 10, 10)], 640, 480)
    new_box = DetectionBox("cat", 0.9, 5, 5, 15, 15)
    cache.update([new_box], 1920, 1080)

    snap = cache.snapshot()
    assert snap is not None
    boxes, w, h = snap
    assert boxes == [new_box]
    assert (w, h) == (1920, 1080)


# ── _draw_detections ───────────────────────────────────────────────


def test_draw_is_noop_on_empty_list() -> None:
    pytest.importorskip("cv2")
    frame = np.zeros((100, 100, 3), dtype=np.uint8)
    original = frame.copy()
    _draw_detections(frame, [], 100, 100)
    assert np.array_equal(frame, original)


def test_draw_marks_bbox_corners_green() -> None:
    pytest.importorskip("cv2")
    frame = np.zeros((200, 300, 3), dtype=np.uint8)
    box = DetectionBox("person", 0.9, 50, 60, 150, 160)
    _draw_detections(frame, [box], 300, 200)

    # All four bbox corners should be green (OBSBOT C++ draw color, BGR).
    green = np.array([0, 255, 0], dtype=np.uint8)
    for x, y in [(box.x1, box.y1), (box.x2, box.y1), (box.x1, box.y2), (box.x2, box.y2)]:
        assert np.array_equal(frame[y, x], green), f"pixel at ({x},{y}) not green: {frame[y, x]}"


def test_draw_keeps_label_on_frame_for_near_top_bboxes() -> None:
    # Regression: a bbox with y1=0 used to draw its label background at
    # y = -text_h - 6, silently clipped by OpenCV.  The label must now land
    # inside the frame.
    pytest.importorskip("cv2")
    frame = np.zeros((120, 200, 3), dtype=np.uint8)
    box = DetectionBox("person", 0.9, 10, 0, 100, 80)

    _draw_detections(frame, [box], 200, 120)

    # Label area sits immediately below y1=0 when there's no room above, so a
    # horizontal strip of green pixels should exist in the first ~20 rows.
    green_mask = np.all(frame[: 25, box.x1 + 2 : box.x1 + 20] == (0, 255, 0), axis=-1)
    assert green_mask.any(), "label background not rendered inside frame for top-edge bbox"


def test_cache_snapshot_returns_defensive_copy() -> None:
    # Regression: snapshot() used to return the cache's internal list by
    # reference, so a caller could corrupt the cache by mutating the result.
    cache = _DetectionCache()
    cache.update([DetectionBox("dog", 0.8, 1, 2, 3, 4)], 640, 480)

    snap = cache.snapshot()
    assert snap is not None
    boxes, _, _ = snap
    boxes.clear()

    snap_again = cache.snapshot()
    assert snap_again is not None
    assert len(snap_again[0]) == 1, "mutation through snapshot leaked into cache"


def test_draw_scales_from_detection_frame_to_capture_frame() -> None:
    pytest.importorskip("cv2")
    import cv2

    # Detection frame 300x200; capture frame 600x400 (2x scale).
    frame = np.zeros((400, 600, 3), dtype=np.uint8)
    box = DetectionBox("person", 0.9, 50, 60, 150, 160)

    with patch.object(cv2, "rectangle", wraps=cv2.rectangle) as mock_rect:
        _draw_detections(frame, [box], 300, 200)

    # First call is the bbox rectangle — should be scaled 2x.
    args, _kwargs = mock_rect.call_args_list[0]
    _frame_arg, pt1, pt2, _color, _thickness = args
    assert pt1 == (100, 120)
    assert pt2 == (300, 320)


# ── _detection_overlays_enabled_env ────────────────────────────────


def test_overlays_enabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CYBERWAVE_DETECTION_OVERLAYS", raising=False)
    assert _detection_overlays_enabled_env() is True


@pytest.mark.parametrize("value", ["0", "false", "False", "NO", "off", " false "])
def test_overlays_disabled_values(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("CYBERWAVE_DETECTION_OVERLAYS", value)
    assert _detection_overlays_enabled_env() is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", ""])
def test_overlays_truthy_or_unset_enabled(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("CYBERWAVE_DETECTION_OVERLAYS", value)
    assert _detection_overlays_enabled_env() is True
