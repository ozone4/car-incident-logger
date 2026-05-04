"""
test_dashcam.py — Tests for dashcam incident capture and trigger abstraction.

No real camera, GPU, or heavy deps required.
"""

import json
import sys
import time
import types
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

# Ensure project root is on sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Stub cv2 if not available
if "cv2" not in sys.modules:
    _cv2 = types.ModuleType("cv2")
    _cv2.imencode = MagicMock(return_value=(True, MagicMock(tobytes=MagicMock(return_value=b""))))
    _cv2.resize = MagicMock(side_effect=lambda f, *a, **kw: f)
    _cv2.VideoWriter_fourcc = MagicMock(return_value=0)
    _cv2.VideoWriter = MagicMock()
    _cv2.IMWRITE_JPEG_QUALITY = 1
    sys.modules["cv2"] = _cv2

from modules.dashcam import DashcamRecorder, _sanitize_sightings
from modules.incident_trigger import WebTrigger, HardwareButtonTrigger


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_frame(w=640, h=480):
    """Create a fake BGR frame."""
    return np.zeros((h, w, 3), dtype=np.uint8)


class FakeRollingBuffer:
    """Minimal stand-in for RollingBuffer with controllable output."""

    def __init__(self, frames=None):
        self._frames = frames or []

    def get_clip(self, seconds_back=None):
        return list(self._frames)

    def frame_count(self):
        return len(self._frames)

    def actual_duration(self):
        if len(self._frames) < 2:
            return 0.0
        return self._frames[-1][1] - self._frames[0][1]


class FakeCamera:
    is_running = True

    def __init__(self, frame=None):
        self._frame = frame or _make_frame()

    def get_frame(self):
        return (self._frame, time.monotonic())


# ── DashcamRecorder tests ────────────────────────────────────────────────────

class TestDashcamRecorder:
    def test_trigger_without_buffer_returns_error(self, tmp_path):
        rec = DashcamRecorder(output_path=tmp_path, post_roll_seconds=0)
        result = rec.trigger(source="test")
        assert result["ok"] is False
        assert "buffer" in result["error"].lower()

    def test_trigger_with_empty_buffer_returns_error(self, tmp_path):
        rec = DashcamRecorder(output_path=tmp_path, post_roll_seconds=0)
        rec.attach(FakeRollingBuffer([]))
        result = rec.trigger(source="test")
        assert result["ok"] is False
        assert "empty" in result["error"].lower()

    def test_trigger_saves_metadata_json(self, tmp_path):
        frames = [(_make_frame(), time.monotonic() + i * 0.033) for i in range(10)]
        buf = FakeRollingBuffer(frames)

        rec = DashcamRecorder(output_path=tmp_path, post_roll_seconds=0)
        rec.attach(buf)
        result = rec.trigger(source="web", alpr_plate="ABC123")

        assert result["ok"] is True
        assert result["plate"] == "ABC123"
        assert result["trigger_source"] == "web"
        assert result["pre_roll_frames"] == 10
        assert result["total_frames"] == 10

        # Check metadata file on disk
        incident_dir = Path(result["incident_dir"])
        assert incident_dir.exists()
        meta = json.loads((incident_dir / "metadata.json").read_text())
        assert meta["plate"] == "ABC123"
        assert meta["trigger_source"] == "web"

    def test_trigger_without_plate(self, tmp_path):
        frames = [(_make_frame(), time.monotonic() + i * 0.033) for i in range(5)]
        rec = DashcamRecorder(output_path=tmp_path, post_roll_seconds=0)
        rec.attach(FakeRollingBuffer(frames))
        result = rec.trigger(source="web")

        assert result["ok"] is True
        assert result["plate"] is None

    def test_trigger_includes_recent_sightings(self, tmp_path):
        frames = [(_make_frame(), time.monotonic()) for _ in range(3)]
        rec = DashcamRecorder(output_path=tmp_path, post_roll_seconds=0)
        rec.attach(FakeRollingBuffer(frames))

        sightings = [
            {"plate": "XYZ789", "confidence": 0.91, "source": "yolo+easyocr", "seen_count": 5},
        ]
        result = rec.trigger(source="web", recent_sightings=sightings)
        assert result["ok"] is True

        meta = json.loads((Path(result["incident_dir"]) / "metadata.json").read_text())
        assert len(meta["recent_sightings"]) == 1
        assert meta["recent_sightings"][0]["plate"] == "XYZ789"

    def test_concurrent_trigger_rejected(self, tmp_path):
        """Only one capture should run at a time."""
        frames = [(_make_frame(), time.monotonic()) for _ in range(3)]
        rec = DashcamRecorder(output_path=tmp_path, post_roll_seconds=0)
        rec.attach(FakeRollingBuffer(frames))

        # Acquire the lock manually to simulate busy
        rec._busy.acquire()
        result = rec.trigger(source="test")
        assert result["ok"] is False
        assert "in progress" in result["error"].lower()
        rec._busy.release()

    def test_buffer_status_not_attached(self, tmp_path):
        rec = DashcamRecorder(output_path=tmp_path)
        status = rec.buffer_status()
        assert status["attached"] is False

    def test_buffer_status_attached(self, tmp_path):
        frames = [(_make_frame(), 100.0 + i) for i in range(30)]
        rec = DashcamRecorder(output_path=tmp_path, pre_roll_seconds=30, post_roll_seconds=5)
        rec.attach(FakeRollingBuffer(frames))
        status = rec.buffer_status()
        assert status["attached"] is True
        assert status["frame_count"] == 30
        assert status["pre_roll_seconds"] == 30
        assert status["post_roll_seconds"] == 5

    def test_last_result_and_error(self, tmp_path):
        rec = DashcamRecorder(output_path=tmp_path, post_roll_seconds=0)
        assert rec.last_result is None
        assert rec.last_error is None

        # Trigger without buffer → error
        rec.trigger(source="test")
        assert rec.last_error is not None

        # Trigger with buffer → success
        rec.attach(FakeRollingBuffer([(_make_frame(), time.monotonic())]))
        rec.trigger(source="test")
        assert rec.last_result is not None
        assert rec.last_error is None


# ── WebTrigger tests ─────────────────────────────────────────────────────────

class TestWebTrigger:
    def test_fire_when_not_armed(self):
        t = WebTrigger()
        result = t.fire()
        assert result["ok"] is False
        assert "not armed" in result["error"].lower()

    def test_fire_when_armed(self):
        t = WebTrigger()
        callback = MagicMock(return_value={"ok": True, "plate": "TEST"})
        t.arm(callback)
        assert t.is_armed is True

        result = t.fire({"alpr_plate": "TEST"})
        assert result["ok"] is True
        callback.assert_called_once_with("web", {"alpr_plate": "TEST"})

    def test_disarm(self):
        t = WebTrigger()
        t.arm(MagicMock())
        t.disarm()
        assert t.is_armed is False
        result = t.fire()
        assert result["ok"] is False


# ── HardwareButtonTrigger tests ──────────────────────────────────────────────

class TestHardwareButtonTrigger:
    def test_stub_arms_and_disarms(self):
        t = HardwareButtonTrigger(pin=17)
        assert t.source_name == "hardware_button"
        assert t.is_armed is False

        t.arm(MagicMock())
        assert t.is_armed is True

        t.disarm()
        assert t.is_armed is False


# ── _sanitize_sightings tests ────────────────────────────────────────────────

class TestSanitizeSightings:
    def test_empty(self):
        assert _sanitize_sightings(None) == []
        assert _sanitize_sightings([]) == []

    def test_keeps_safe_fields(self):
        sightings = [
            {"plate": "AB12", "confidence": 0.8, "source": "yolo", "seen_count": 3, "extra": "junk"},
        ]
        result = _sanitize_sightings(sightings)
        assert len(result) == 1
        assert result[0]["plate"] == "AB12"
        assert "extra" not in result[0]

    def test_limits_to_10(self):
        sightings = [{"plate": f"P{i}", "confidence": 0.5} for i in range(20)]
        result = _sanitize_sightings(sightings)
        assert len(result) == 10


# ── Web route tests ──────────────────────────────────────────────────────────

from web.app import app as flask_app


@pytest.fixture()
def client():
    flask_app.config["TESTING"] = True
    with flask_app.test_client() as c:
        yield c


def test_dashcam_status_returns_json(client):
    resp = client.get("/dashcam/status")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "attached" in data or "trigger_armed" in data


def test_dashcam_trigger_without_camera(client):
    resp = client.post("/dashcam/trigger")
    assert resp.status_code in (200, 409)
    data = resp.get_json()
    # Should fail gracefully since no camera/buffer is active
    assert isinstance(data, dict)


def test_dashcam_clip_nonexistent(client):
    resp = client.get("/dashcam/clips/nonexistent/clip.mp4")
    assert resp.status_code in (403, 404)
