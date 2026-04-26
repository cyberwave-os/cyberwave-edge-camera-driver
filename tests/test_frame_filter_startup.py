"""Unit tests for the frame-filter startup guard in ``main.py``.

The guard enforces the driver's privacy-safe opt-in contract: if the
operator sets ``CYBERWAVE_METADATA_FRAME_FILTER_ENABLED=true`` but the
Zenoh data bus is unavailable, the driver must NOT silently fall back
to streaming raw camera frames to WebRTC. It aborts startup instead.

These tests exercise the extracted helper directly — driving the full
``main()`` coroutine just to reach the guard would require stubbing the
SDK client, the camera enumerator, and the event loop, which adds a lot
of noise for a 10-line invariant.
"""

from __future__ import annotations

import logging

import pytest

from main import _enforce_frame_filter_requires_data_bus


class TestFilterDisabledNeverAborts:
    def test_no_data_bus_is_fine_when_filter_disabled(self) -> None:
        _enforce_frame_filter_requires_data_bus(
            frame_filter_enabled=False,
            data_bus_available=False,
        )

    def test_data_bus_up_is_fine_when_filter_disabled(self) -> None:
        _enforce_frame_filter_requires_data_bus(
            frame_filter_enabled=False,
            data_bus_available=True,
        )


class TestFilterEnabled:
    def test_allows_startup_when_data_bus_is_up(self) -> None:
        _enforce_frame_filter_requires_data_bus(
            frame_filter_enabled=True,
            data_bus_available=True,
        )

    def test_aborts_when_data_bus_is_missing(self, caplog) -> None:
        """Filter opted-in without the bus → SystemExit(1), loud ERROR."""
        with caplog.at_level(logging.ERROR, logger="camera-driver"):
            with pytest.raises(SystemExit) as excinfo:
                _enforce_frame_filter_requires_data_bus(
                    frame_filter_enabled=True,
                    data_bus_available=False,
                )
        assert excinfo.value.code == 1
        assert any("FRAME_FILTER_ENABLED" in r.message for r in caplog.records), [
            r.message for r in caplog.records
        ]
        # The message must reference BOTH env vars so the operator
        # knows how to unstick the driver.
        joined = "\n".join(r.message for r in caplog.records)
        assert "CYBERWAVE_DATA_BACKEND" in joined
        assert "CYBERWAVE_METADATA_FRAME_FILTER_ENABLED" in joined
