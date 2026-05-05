"""
test_incident_workflow.py — Tests for incident trigger status and export routes.

Uses Flask test client; no real camera required.
"""

import json
import sys
import types
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Stub cv2 and numpy if not installed
if "cv2" not in sys.modules:
    _cv2 = types.ModuleType("cv2")
    _cv2.imencode = MagicMock(return_value=(True, MagicMock(tobytes=MagicMock(return_value=b""))))
    _cv2.resize = MagicMock(side_effect=lambda f, *a, **kw: f)
    _cv2.VideoWriter = MagicMock()
    _cv2.VideoWriter_fourcc = MagicMock(return_value=0)
    sys.modules["cv2"] = _cv2

if "numpy" not in sys.modules:
    _np = types.ModuleType("numpy")
    _np.ndarray = type("ndarray", (), {})
    _np.uint8 = "uint8"
    sys.modules["numpy"] = _np


@pytest.fixture
def app_client(tmp_path):
    """Create a Flask test client with mocked config."""
    config_data = {
        "camera": {"device_index": 0, "resolution": {"width": 640, "height": 480}, "fps": 30, "format": "MJPG"},
        "buffer": {"duration_seconds": 35},
        "recording": {"enabled": True, "segment_duration_seconds": 60, "output_path": str(tmp_path / "recordings")},
        "dashcam": {
            "output_path": str(tmp_path / "dashcam"),
            "pre_roll_seconds": 30,
            "post_roll_seconds": 5,
            "auto_start_camera": False,
            "auto_start_alpr": False,
        },
        "overlay": {"enabled": False, "position": "bottom-left", "font_scale": 0.7, "color": [255, 255, 255], "background": True},
        "storage": {"max_recording_age_days": 7, "min_free_space_gb": 2.0, "cleanup_interval_seconds": 300},
        "transcription": {"model_size": "base"},
        "button": {"mode": "keyboard", "key": "space"},
        "alpr": {"enabled": False, "confidence_threshold": 0.6, "yolo_confidence_threshold": 0.5, "models_dir": str(tmp_path / "models"), "yolo_model_path": ""},
        "notifier": {"console": True},
        "logging": {"level": "INFO"},
    }

    config_path = tmp_path / "config.yaml"
    import yaml
    config_path.write_text(yaml.dump(config_data))

    with patch("web.app._config_path", return_value=config_path):
        from web.app import app
        app.config["TESTING"] = True
        with app.test_client() as client:
            yield client, tmp_path


class TestDashcamStatusCapture:
    def test_status_includes_capture_state(self, app_client):
        """Dashcam status endpoint includes capture_state field."""
        client, _ = app_client
        resp = client.get("/dashcam/status")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "capture_state" in data

    def test_trigger_when_not_armed_returns_error(self, app_client):
        """Triggering without armed buffer returns error."""
        client, _ = app_client
        resp = client.post("/dashcam/trigger")
        data = resp.get_json()
        assert data.get("ok") is False


class TestRecoveryRoutes:
    def test_recovery_status_no_run(self, app_client):
        """Recovery status before any run returns placeholder."""
        client, _ = app_client
        resp = client.get("/storage/recovery")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "summary" in data

    def test_recovery_run_on_empty_dir(self, app_client):
        """Manual recovery run on empty recording dir succeeds."""
        client, tmp_path = app_client
        (tmp_path / "recordings").mkdir(parents=True, exist_ok=True)
        resp = client.post("/storage/recovery/run")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["recovered"] == []
        assert data["corrupt"] == []


class TestDashcamExport:
    def test_export_incident_zip(self, app_client):
        """Export route returns a zip file for a valid incident."""
        client, tmp_path = app_client
        # Create fake incident
        incident_dir = tmp_path / "dashcam" / "20260504T143000Z"
        incident_dir.mkdir(parents=True, exist_ok=True)
        (incident_dir / "clip.mp4").write_bytes(b"\x00" * 100)
        (incident_dir / "metadata.json").write_text('{"plate": "ABC123"}')

        resp = client.get("/dashcam/export/20260504T143000Z")
        assert resp.status_code == 200
        assert resp.content_type == "application/zip"
        assert b"PK" in resp.data[:4]  # ZIP magic bytes

    def test_export_nonexistent_returns_404(self, app_client):
        """Export of nonexistent incident returns 404."""
        client, tmp_path = app_client
        (tmp_path / "dashcam").mkdir(parents=True, exist_ok=True)
        resp = client.get("/dashcam/export/nonexistent")
        assert resp.status_code == 404

    def test_export_path_traversal_blocked(self, app_client):
        """Path traversal in export route is blocked."""
        client, _ = app_client
        resp = client.get("/dashcam/export/../../../etc/passwd")
        assert resp.status_code in (403, 404)
