"""
test_web_ui.py — Tests for the Flask web UI.

These tests use Flask's built-in test client and do not require a real
camera, microphone, GPU, or pre-populated database.
"""

import json
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Ensure project root is on sys.path so web.app can import from modules/.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Stub heavy native dependencies that may not be installed in CI / dev.
# web/app.py imports cv2 at module level for frame encoding; stub it so
# tests can load the module without OpenCV installed.
if "cv2" not in sys.modules:
    _cv2 = types.ModuleType("cv2")
    _cv2.imencode = MagicMock(return_value=(True, MagicMock(tobytes=MagicMock(return_value=b""))))
    _cv2.resize = MagicMock(side_effect=lambda f, *a, **kw: f)
    _cv2.IMWRITE_JPEG_QUALITY = 1
    sys.modules["cv2"] = _cv2

from web.app import app as flask_app, _decode_metadata  # noqa: E402


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def client():
    flask_app.config["TESTING"] = True
    with flask_app.test_client() as c:
        yield c


# ── Page smoke tests (no DB required) ────────────────────────────────────────

def test_index_returns_200(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"Dashboard" in resp.data


def test_dashboard_alias_returns_200(client):
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert b"Dashboard" in resp.data
    assert b"kiosk-hero" in resp.data


def test_dashboard_has_incident_button(client):
    resp = client.get("/")
    assert b"dashcam-trigger" in resp.data
    assert b"INCIDENT" in resp.data


def test_camera_page_returns_200(client):
    resp = client.get("/camera")
    assert resp.status_code == 200
    assert b"Camera Preview" in resp.data


def test_incidents_page_returns_200(client):
    resp = client.get("/incidents")
    assert resp.status_code == 200
    assert b"Incidents" in resp.data


def test_incidents_search_returns_200(client):
    resp = client.get("/incidents?q=ABC")
    assert resp.status_code == 200


def test_config_page_returns_200(client):
    resp = client.get("/config")
    assert resp.status_code == 200
    assert b"Configuration" in resp.data


def test_plate_detail_unknown_plate(client):
    resp = client.get("/incidents/ZZNOTEXIST")
    assert resp.status_code == 200
    # Should render gracefully even for an unknown plate
    assert b"ZZNOTEXIST" in resp.data


# ── Camera API tests ──────────────────────────────────────────────────────────

def test_camera_status_api_returns_json(client):
    resp = client.get("/camera/status")
    assert resp.status_code == 200
    data = resp.get_json()
    assert isinstance(data, dict)
    assert "running" in data


def test_camera_status_initially_stopped(client):
    resp = client.get("/camera/status")
    data = resp.get_json()
    # No camera hardware in CI — should report not running.
    assert data["running"] is False


def test_camera_stop_when_not_running(client):
    resp = client.post("/camera/stop")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "not_running"


# ── Media route ───────────────────────────────────────────────────────────────

def test_media_missing_path_param(client):
    resp = client.get("/media")
    assert resp.status_code == 400


def test_media_nonexistent_file(client, tmp_path):
    fake_path = str(tmp_path / "does_not_exist.mp4")
    resp = client.get(f"/media?path={fake_path}")
    # Either 403 (outside project root) or 404 (inside but missing).
    assert resp.status_code in (403, 404)


def test_media_forbidden_path(client):
    resp = client.get("/media?path=/etc/passwd")
    assert resp.status_code == 403


# ── Config POST validation ────────────────────────────────────────────────────

def test_config_post_invalid_value(client):
    resp = client.post("/config", data={
        "device_index": "not_a_number",
        "width": "1920",
        "height": "1080",
        "fps": "30",
        "buffer_duration": "45",
    })
    assert resp.status_code == 200
    assert b"Invalid value" in resp.data


# ── Unit: _decode_metadata ────────────────────────────────────────────────────

def test_decode_metadata_valid_json():
    incidents = [{"metadata_json": '{"plate": "WJ1843", "confidence": 0.92}'}]
    _decode_metadata(incidents)
    assert incidents[0]["meta"]["plate"] == "WJ1843"
    assert incidents[0]["meta"]["confidence"] == pytest.approx(0.92)


def test_decode_metadata_missing_field():
    incidents = [{}]
    _decode_metadata(incidents)
    assert incidents[0]["meta"] == {}


def test_decode_metadata_null_field():
    incidents = [{"metadata_json": None}]
    _decode_metadata(incidents)
    assert incidents[0]["meta"] == {}


def test_decode_metadata_broken_json():
    incidents = [{"metadata_json": "{ bad json }"}]
    _decode_metadata(incidents)
    assert incidents[0]["meta"] == {}


def test_decode_metadata_multiple():
    incidents = [
        {"metadata_json": '{"plate": "AA111"}'},
        {"metadata_json": '{"plate": "BB222"}'},
    ]
    _decode_metadata(incidents)
    assert incidents[0]["meta"]["plate"] == "AA111"
    assert incidents[1]["meta"]["plate"] == "BB222"

# ── Live ALPR API tests ──────────────────────────────────────────────────────

def test_alpr_live_status_returns_json(client):
    resp = client.get("/alpr/live/status")
    assert resp.status_code == 200
    data = resp.get_json()
    assert isinstance(data, dict)
    assert "running" in data
    assert "frames_scanned" in data


def test_alpr_live_stop_when_not_running(client):
    resp = client.post("/alpr/live/stop")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "stopped"
    assert data["running"] is False

# ── Plate sightings log tests ────────────────────────────────────────────────

def test_plate_sightings_track_multiple_active_plates():
    from web.app import _alpr_state, _update_plate_sightings

    _alpr_state["active_sightings"] = {}
    _alpr_state["recent_sightings"] = []
    _, _, rows = _update_plate_sightings([
        {"plate": "634XSG", "confidence": 0.53, "raw_text": "634-XSG"},
        {"plate": "ABC123", "confidence": 0.61, "raw_text": "ABC123"},
    ], 100.0)

    assert [r["plate"] for r in rows] == ["634XSG", "ABC123"]
    assert all(r["active"] for r in rows)


def test_plate_sightings_expire_to_recent_history():
    from web.app import _alpr_state, _update_plate_sightings

    _alpr_state["active_sightings"] = {}
    _alpr_state["recent_sightings"] = []
    _update_plate_sightings([{"plate": "634XSG", "confidence": 0.53}], 100.0)
    _, _, rows = _update_plate_sightings([], 106.0)

    assert rows[0]["plate"] == "634XSG"
    assert rows[0]["active"] is False
    assert rows[0]["status"] == "gone"


# ── Health + Storage route tests ─────────────────────────────────────────────

def test_health_endpoint_returns_json(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "status" in data
    assert data["status"] in ("green", "yellow", "red", "grey")
    assert "issues" in data
    assert "components" in data
    assert "disk" in data


def test_storage_status_endpoint(client):
    resp = client.get("/storage/status")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "running" in data


def test_storage_cleanup_dry_run(client):
    resp = client.post("/storage/cleanup?dry_run=true")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["dry_run"] is True
    assert "deleted_count" in data
