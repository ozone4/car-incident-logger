"""
test_loop_recorder.py — Tests for continuous loop recording and overlay.

No real camera, GPU, or heavy deps required.
"""

import json
import sys
import time
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

# Ensure project root is on sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Stub cv2 if not available (must happen before importing modules)
_cv2_stub = sys.modules.get("cv2")
if _cv2_stub is None:
    _cv2_stub = types.ModuleType("cv2")
    sys.modules["cv2"] = _cv2_stub

# Ensure all constants/methods used by overlay.py and loop_recorder.py exist
for _attr, _val in [
    ("imencode", MagicMock(return_value=(True, MagicMock(tobytes=MagicMock(return_value=b""))))),
    ("resize", MagicMock(side_effect=lambda f, *a, **kw: f)),
    ("VideoWriter_fourcc", MagicMock(return_value=0)),
    ("IMWRITE_JPEG_QUALITY", 1),
    ("FONT_HERSHEY_SIMPLEX", 0),
    ("LINE_AA", 16),
    ("FILLED", -1),
    ("putText", MagicMock()),
    ("getTextSize", MagicMock(return_value=((100, 20), 5))),
    ("rectangle", MagicMock()),
]:
    if not hasattr(_cv2_stub, _attr):
        setattr(_cv2_stub, _attr, _val)

# VideoWriter must return a mock with isOpened=True
if not hasattr(_cv2_stub, "VideoWriter") or not callable(getattr(_cv2_stub, "VideoWriter", None)):
    _writer_mock = MagicMock()
    _writer_mock.isOpened.return_value = True
    _cv2_stub.VideoWriter = MagicMock(return_value=_writer_mock)

from modules.loop_recorder import LoopRecorder
from modules.overlay import apply_timestamp
from modules.config_manager import ConfigManager


# ── Helpers ─────────────────────────────────────────────────────────────────


def _make_frame(w=640, h=480):
    return np.zeros((h, w, 3), dtype=np.uint8)


class FakeCamera:
    """Minimal stand-in for CameraCapture."""

    def __init__(self, frames=None):
        self._frames = frames or [(_make_frame(), time.monotonic())]
        self._idx = 0
        self.is_running = True

    def get_frame(self):
        if not self._frames:
            return None
        frame = self._frames[self._idx % len(self._frames)]
        self._idx += 1
        return frame


# ── Overlay tests ───────────────────────────────────────────────────────────


class TestOverlay:
    def test_returns_copy(self):
        """Overlay should not modify the original frame."""
        frame = _make_frame()
        result = apply_timestamp(frame, timestamp=1700000000.0)
        assert result is not frame

    def test_output_shape_matches_input(self):
        frame = _make_frame(320, 240)
        result = apply_timestamp(frame, timestamp=1700000000.0)
        assert result.shape == frame.shape

    def test_custom_position(self):
        """Should not raise for any valid position."""
        frame = _make_frame()
        for pos in ("bottom-left", "bottom-right", "top-left", "top-right"):
            result = apply_timestamp(frame, timestamp=1700000000.0, position=pos)
            assert result.shape == frame.shape

    def test_no_background(self):
        frame = _make_frame()
        result = apply_timestamp(frame, timestamp=1700000000.0, background=False)
        assert result.shape == frame.shape

    def test_default_timestamp_uses_now(self):
        """When timestamp is None, should use current time (no crash)."""
        frame = _make_frame()
        result = apply_timestamp(frame)
        assert result.shape == frame.shape


# ── LoopRecorder tests ──────────────────────────────────────────────────────


class TestLoopRecorder:
    def test_status_before_start(self, tmp_path):
        rec = LoopRecorder(output_path=tmp_path, segment_duration_seconds=5)
        status = rec.status()
        assert status["recording"] is False
        assert status["segments_completed"] == 0

    def test_start_and_stop(self, tmp_path):
        cam = FakeCamera()
        rec = LoopRecorder(
            output_path=tmp_path,
            segment_duration_seconds=1,
            fps=10,
            overlay_enabled=False,
        )
        rec.start(cam)
        assert rec.is_recording
        time.sleep(0.3)
        rec.stop()
        assert not rec.is_recording

    def test_segment_completes(self, tmp_path):
        """After a short segment, status should report segments completed and sidecar JSON written."""
        cam = FakeCamera()
        rec = LoopRecorder(
            output_path=tmp_path,
            segment_duration_seconds=1,
            fps=30,
            overlay_enabled=False,
        )
        rec.start(cam)
        # Wait long enough for at least one segment to complete
        time.sleep(2.5)
        rec.stop()

        status = rec.status()
        assert status["segments_completed"] >= 1
        assert status["total_frames_written"] > 0

        # Check sidecar JSON files on disk
        jsons = list(tmp_path.rglob("*.json"))
        assert len(jsons) >= 1, f"Expected JSON sidecars, found: {list(tmp_path.rglob('*'))}"

        # Verify sidecar metadata structure
        meta = json.loads(jsons[0].read_text())
        assert "start_time" in meta
        assert "end_time" in meta
        assert "frame_count" in meta
        assert meta["locked"] is False

    def test_date_directory_structure(self, tmp_path):
        """Segments should be organized under a YYYY-MM-DD directory."""
        cam = FakeCamera()
        rec = LoopRecorder(
            output_path=tmp_path,
            segment_duration_seconds=1,
            fps=30,
            overlay_enabled=False,
        )
        rec.start(cam)
        time.sleep(2.0)
        rec.stop()

        subdirs = [p for p in tmp_path.iterdir() if p.is_dir()]
        assert len(subdirs) >= 1
        # Directory name should look like YYYY-MM-DD
        dirname = subdirs[0].name
        assert len(dirname) == 10 and dirname[4] == "-" and dirname[7] == "-"

    def test_last_completed_segment_set(self, tmp_path):
        """After at least one segment, last_completed_segment should be set."""
        cam = FakeCamera()
        rec = LoopRecorder(
            output_path=tmp_path,
            segment_duration_seconds=1,
            fps=30,
            overlay_enabled=False,
        )
        rec.start(cam)
        time.sleep(2.0)
        rec.stop()

        status = rec.status()
        if status["segments_completed"] > 0:
            assert status["last_completed_segment"] is not None

    def test_status_updates_during_recording(self, tmp_path):
        cam = FakeCamera()
        rec = LoopRecorder(
            output_path=tmp_path,
            segment_duration_seconds=2,
            fps=30,
            overlay_enabled=False,
        )
        rec.start(cam)
        time.sleep(0.5)
        status = rec.status()
        assert status["recording"] is True
        assert status["total_frames_written"] > 0
        rec.stop()

    def test_camera_not_running(self, tmp_path):
        """Recorder should handle camera not running gracefully."""
        cam = FakeCamera()
        cam.is_running = False
        rec = LoopRecorder(
            output_path=tmp_path,
            segment_duration_seconds=1,
            fps=10,
            overlay_enabled=False,
        )
        rec.start(cam)
        time.sleep(0.5)
        rec.stop()
        # Should not crash, no segments produced
        assert rec.status()["segments_completed"] == 0


# ── Config property tests ───────────────────────────────────────────────────


class TestRecordingConfig:
    def test_recording_defaults(self, tmp_path):
        """Config should return sensible defaults when recording section is absent."""
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("camera:\n  device_index: 0\n")
        cfg = ConfigManager(str(cfg_file))
        assert cfg.recording_enabled is True
        assert cfg.recording_segment_duration == 60
        assert cfg.recording_output_path == Path("./data/recordings")

    def test_overlay_defaults(self, tmp_path):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("camera:\n  device_index: 0\n")
        cfg = ConfigManager(str(cfg_file))
        assert cfg.overlay_enabled is True
        assert cfg.overlay_position == "bottom-left"
        assert cfg.overlay_font_scale == 0.7
        assert cfg.overlay_color == [255, 255, 255]
        assert cfg.overlay_background is True

    def test_recording_custom_values(self, tmp_path):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(
            "recording:\n"
            "  enabled: false\n"
            "  segment_duration_seconds: 120\n"
            "  output_path: /tmp/rec\n"
            "overlay:\n"
            "  enabled: false\n"
            "  position: top-right\n"
        )
        cfg = ConfigManager(str(cfg_file))
        assert cfg.recording_enabled is False
        assert cfg.recording_segment_duration == 120
        assert cfg.recording_output_path == Path("/tmp/rec")
        assert cfg.overlay_enabled is False
        assert cfg.overlay_position == "top-right"


# ── Web route smoke test ────────────────────────────────────────────────────


class TestRecordingRoute:
    @pytest.fixture()
    def client(self):
        from web.app import app as flask_app
        flask_app.config["TESTING"] = True
        with flask_app.test_client() as c:
            yield c

    def test_recording_status_endpoint(self, client):
        resp = client.get("/recording/status")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "recording" in data
