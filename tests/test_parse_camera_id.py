"""Guardrails around camera-source resolution in ``main.py``.

RealSense serials are all-digit strings, so they are indistinguishable from
a local camera index by shape alone. ``_parse_camera_id`` exists to turn
``"0"`` into the int OpenCV wants; applying it to a serial destroys the
leading zero and silently selects the wrong device.
"""

from __future__ import annotations

import pytest

import main  # noqa: E402 — conftest inserts driver root on sys.path


class TestParseCameraId:
    """``_parse_camera_id`` itself — index coercion for UVC sources."""

    def test_index_strings_become_ints(self) -> None:
        assert main._parse_camera_id("0") == 0
        assert main._parse_camera_id("2") == 2

    def test_device_paths_pass_through(self) -> None:
        assert main._parse_camera_id("/dev/video2") == "/dev/video2"

    def test_rtsp_urls_pass_through(self) -> None:
        url = "rtsp://192.168.250.2/avc"
        assert main._parse_camera_id(url) == url

    def test_realsense_serial_is_mangled_by_int_coercion(self) -> None:
        """Documents *why* serials must never reach this helper.

        This is the behaviour that broke two-camera setups: the leading zero
        is lost and the 12-digit serial becomes an 11-digit int.
        """
        assert main._parse_camera_id("046322252081") == 46322252081


class TestIsStableDeviceIdentifier:
    """Which configured values may be silently replaced by another camera."""

    @pytest.mark.parametrize(
        "camera_id",
        [
            "046322252081",
            "rtsp://192.168.250.2/avc",
            "http://192.168.1.50/snapshot.jpg",
            "/dev/v4l/by-id/usb-046d_C920_ABC123-video-index0",
            "/dev/v4l/by-path/pci-0000:00:14.0-usb-0:1:1.0-video-index0",
        ],
    )
    def test_stable_identifiers(self, camera_id: str) -> None:
        assert main._is_stable_device_identifier(camera_id) is True

    @pytest.mark.parametrize("camera_id", [0, 2, "/dev/video0", "/dev/video2"])
    def test_positional_identifiers(self, camera_id: int | str) -> None:
        assert main._is_stable_device_identifier(camera_id) is False


class TestResolveCameraSourceDepth:
    """Depth twins address the device by serial, never by index."""

    def _resolve(self, **kw):
        kw.setdefault("serial_env", None)
        kw.setdefault("video_device_env", None)
        return main._resolve_camera_source(is_depth_camera=True, **kw)

    def test_serial_preserves_leading_zero(self) -> None:
        camera_id, serial, pinned = self._resolve(serial_env="046322252081")
        assert serial == "046322252081"
        assert pinned is True

    def test_two_serials_stay_distinct(self) -> None:
        """Both twins must resolve to different devices, or they race for one."""
        left = self._resolve(serial_env="043422251999")[1]
        right = self._resolve(serial_env="046322252081")[1]
        assert left != right

    @pytest.mark.parametrize(
        "video_device", ["0", "2", "/dev/video0", "046322252081"]
    )
    def test_video_device_is_never_read_as_a_serial(self, video_device: str) -> None:
        """``video_device`` names a source, never a physical unit.

        A serial and an index are indistinguishable by shape, so guessing
        means ``enable_device("0")`` failing every lookup on a twin that
        configured nothing. Before ``serial_number`` existed, a serial put
        here was dropped by the SDK for RealSense anyway, so nothing
        regresses — the serial must move to ``metadata.serial_number``.
        """
        _, serial, pinned = self._resolve(video_device_env=video_device)
        assert serial is None
        assert pinned is False

    def test_unconfigured_twin_pins_nothing(self) -> None:
        camera_id, serial, pinned = self._resolve()
        assert (camera_id, serial, pinned) == (0, None, False)

    def test_serial_number_wins_over_video_device(self) -> None:
        _, serial, _ = self._resolve(
            serial_env="046322252081", video_device_env="043422251999"
        )
        assert serial == "046322252081"


class TestResolveCameraSourceUvc:
    """UVC twins resolve a serial to a stable ``/dev/v4l/by-id`` path."""

    def _resolve(self, **kw):
        kw.setdefault("serial_env", None)
        kw.setdefault("video_device_env", None)
        return main._resolve_camera_source(is_depth_camera=False, **kw)

    def test_unconfigured_allows_fallback(self) -> None:
        assert self._resolve() == (0, None, False)

    def test_plain_index_allows_fallback(self) -> None:
        assert self._resolve(video_device_env="0") == (0, None, False)

    def test_rtsp_url_refuses_fallback(self) -> None:
        """An IP-camera twin must never quietly serve a local webcam."""
        camera_id, _, pinned = self._resolve(video_device_env="rtsp://10.0.0.2/avc")
        assert camera_id == "rtsp://10.0.0.2/avc"
        assert pinned is True

    def test_dev_video_path_allows_fallback(self) -> None:
        """``/dev/video2`` is "whatever enumerated second" — positional."""
        assert self._resolve(video_device_env="/dev/video2")[2] is False

    def test_serial_resolves_to_by_id_path(self, tmp_path, monkeypatch) -> None:
        by_id = tmp_path / "by-id"
        by_id.mkdir()
        (by_id / "usb-046d_C920_ABC123-video-index0").write_text("")
        monkeypatch.setattr(main, "_V4L_BY_ID_DIR", str(by_id))
        camera_id, serial, pinned = self._resolve(serial_env="ABC123")
        assert camera_id == str(by_id / "usb-046d_C920_ABC123-video-index0")
        assert pinned is True

    def test_unknown_serial_raises(self, tmp_path, monkeypatch) -> None:
        by_id = tmp_path / "by-id"
        by_id.mkdir()
        monkeypatch.setattr(main, "_V4L_BY_ID_DIR", str(by_id))
        with pytest.raises(main.HardwareConnectionError, match="NOPE"):
            self._resolve(serial_env="NOPE")


class TestResolveUvcSerial:
    """``serial_number`` -> stable ``/dev/v4l/by-id`` path for UVC cameras."""

    @staticmethod
    def _make_by_id(tmp_path, names: list[str]) -> str:
        d = tmp_path / "by-id"
        d.mkdir()
        for n in names:
            (d / n).write_text("")
        return str(d)

    def test_matches_serial_and_prefers_index0(self, tmp_path) -> None:
        by_id = self._make_by_id(
            tmp_path,
            [
                "usb-046d_C920_ABC123-video-index0",
                "usb-046d_C920_ABC123-video-index1",
            ],
        )
        got = main._resolve_uvc_serial("ABC123", by_id)
        assert got is not None and got.endswith("video-index0")

    def test_prefers_index0_even_when_listed_later(self, tmp_path) -> None:
        by_id = self._make_by_id(
            tmp_path,
            [
                "usb-046d_C920_ABC123-video-index1",
                "usb-046d_C920_ABC123-video-index0",
            ],
        )
        got = main._resolve_uvc_serial("ABC123", by_id)
        assert got is not None and got.endswith("video-index0")

    def test_falls_back_to_first_match_without_index0(self, tmp_path) -> None:
        by_id = self._make_by_id(tmp_path, ["usb-046d_C920_ABC123-video-index1"])
        got = main._resolve_uvc_serial("ABC123", by_id)
        assert got is not None and got.endswith("video-index1")

    def test_two_identical_models_stay_distinct(self, tmp_path) -> None:
        """The case name matching cannot handle: same model, different serial."""
        by_id = self._make_by_id(
            tmp_path,
            [
                "usb-046d_C920_AAA111-video-index0",
                "usb-046d_C920_BBB222-video-index0",
            ],
        )
        left = main._resolve_uvc_serial("AAA111", by_id)
        right = main._resolve_uvc_serial("BBB222", by_id)
        assert left != right
        assert "AAA111" in left and "BBB222" in right

    def test_does_not_match_serial_prefix_of_another_device(self, tmp_path) -> None:
        by_id = self._make_by_id(
            tmp_path,
            [
                "usb-046d_BRIO_ABC1234-video-index0",
                "usb-046d_C920_ABC123-video-index0",
            ],
        )
        got = main._resolve_uvc_serial("ABC123", by_id)
        assert got is not None and "C920_ABC123-video-index0" in got

    def test_does_not_match_serial_in_product_name(self, tmp_path) -> None:
        by_id = self._make_by_id(
            tmp_path,
            [
                "usb-046d_C920_ABC123-video-index0",
                "usb-1234_Other_C920-video-index0",
            ],
        )
        got = main._resolve_uvc_serial("C920", by_id)
        assert got is not None and "Other_C920-video-index0" in got

    def test_unknown_serial_returns_none(self, tmp_path) -> None:
        by_id = self._make_by_id(tmp_path, ["usb-046d_C920_AAA111-video-index0"])
        assert main._resolve_uvc_serial("NOPE", by_id) is None

    def test_missing_directory_returns_none(self) -> None:
        assert main._resolve_uvc_serial("ABC123", "/nonexistent/by-id") is None


class TestMissingCameraDeviceMessage:
    def test_pinned_device_does_not_promise_fallback(self) -> None:
        message = main._missing_camera_device_message(device_pinned=True)
        assert "fail rather than substitute" in message
        assert "auto-discovery fallback" not in message

    def test_positional_device_keeps_fallback_message(self) -> None:
        message = main._missing_camera_device_message(device_pinned=False)
        assert message == "will attempt auto-discovery fallback if stream start fails"
