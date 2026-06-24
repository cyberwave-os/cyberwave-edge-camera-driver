"""Unit tests for ``main._parse_resolution``.

PR #2775 added ``metadata.resolution`` / ``CYBERWAVE_METADATA_RESOLUTION`` so
thermal and other non-VGA sensors can pin a native V4L2 mode instead of
silently falling back to the SDK's 640x480 default. Malformed values must
fail closed (return ``None``) so the driver keeps the legacy default path.
"""

from __future__ import annotations

import logging

import pytest

from main import _parse_resolution


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("256x192", (256, 192)),
        ("640X480", (640, 480)),
        ("1920x1080", (1920, 1080)),
        (" 1280 x 720 ", (1280, 720)),
    ],
)
def test_parse_resolution_accepts_valid_wxh(raw: str, expected: tuple[int, int]) -> None:
    assert _parse_resolution(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_parse_resolution_returns_none_for_empty(raw: str | None) -> None:
    assert _parse_resolution(raw) is None


@pytest.mark.parametrize(
    "raw",
    [
        "640",
        "vga",
        "640*480",
        "640x",
        "x480",
        "640x480x1",
    ],
)
def test_parse_resolution_rejects_malformed_strings(
    raw: str, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="camera-driver"):
        assert _parse_resolution(raw) is None
    assert any("CYBERWAVE_METADATA_RESOLUTION" in r.message for r in caplog.records)


@pytest.mark.parametrize("raw", ["640xabc", "axb", "12.5x480"])
def test_parse_resolution_rejects_non_integer_dimensions(
    raw: str, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="camera-driver"):
        assert _parse_resolution(raw) is None
    assert any("integers" in r.message for r in caplog.records)


@pytest.mark.parametrize("raw", ["0x480", "640x0", "-10x480", "640x-1"])
def test_parse_resolution_rejects_non_positive_dimensions(
    raw: str, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="camera-driver"):
        assert _parse_resolution(raw) is None
    assert any("positive" in r.message for r in caplog.records)
