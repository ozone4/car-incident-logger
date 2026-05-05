"""
test_recordings_browser.py — Tests for the recordings browser web UI.

Covers:
  - Recording listing helper
  - Lock/unlock sidecar updates
  - Delete locked/unlocked behavior
  - Route smoke tests
  - Path traversal protection
  - Video serving

No real camera or recordings required.
"""

import json
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Stub cv2 if not installed
if "cv2" not in sys.modules:
    _cv2 = types.ModuleType("cv2")
    _cv2.imencode = MagicMock(return_value=(True, MagicMock(tobytes=MagicMock(return_value=b""))))
    _cv2.resize = MagicMock(side_effect=lambda f, *a, **kw: f)
    _cv2.IMWRITE_JPEG_QUALITY = 1
    sys.modules["cv2"] = _cv2

from web.app import app as flask_app, _list_all_recordings, _find_video_for_sidecar  # noqa: E402


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def client():
    flask_app.config["TESTING"] = True
    with flask_app.test_client() as c:
        yield c


@pytest.fixture()
def rec_dir(tmp_path):
    """Create a temporary recordings directory with sample segments."""
    date_dir = tmp_path / "2026-05-04"
    date_dir.mkdir()

    # Segment 1: unlocked
    video1 = date_dir / "14-30-00.mp4"
    video1.write_bytes(b"\x00" * 1024)
    sidecar1 = date_dir / "14-30-00.json"
    sidecar1.write_text(json.dumps({
        "start_time": "2026-05-04T14:30:00",
        "end_time": "2026-05-04T14:31:00",
        "duration_seconds": 60.0,
        "frame_count": 1800,
        "file_path": str(video1),
        "locked": False,
    }))

    # Segment 2: locked
    video2 = date_dir / "14-31-00.mp4"
    video2.write_bytes(b"\x00" * 2048)
    sidecar2 = date_dir / "14-31-00.json"
    sidecar2.write_text(json.dumps({
        "start_time": "2026-05-04T14:31:00",
        "end_time": "2026-05-04T14:32:00",
        "duration_seconds": 60.0,
        "frame_count": 1800,
        "file_path": str(video2),
        "locked": True,
    }))

    return tmp_path


def _patch_config(rec_dir):
    """Return a mock ConfigManager pointing to the temp recording dir."""
    mock_cfg = MagicMock()
    mock_cfg.recording_output_path = rec_dir
    mock_cfg.recording_enabled = True
    return mock_cfg


# ── Unit: _find_video_for_sidecar ────────────────────────────────────────────

def test_find_video_by_file_path(tmp_path):
    video = tmp_path / "test.mp4"
    video.write_bytes(b"\x00")
    json_path = tmp_path / "test.json"
    meta = {"file_path": str(video)}
    assert _find_video_for_sidecar(json_path, meta, tmp_path) == video


def test_find_video_by_stem(tmp_path):
    video = tmp_path / "test.mp4"
    video.write_bytes(b"\x00")
    json_path = tmp_path / "test.json"
    assert _find_video_for_sidecar(json_path, {}, tmp_path) == video


def test_find_video_missing(tmp_path):
    json_path = tmp_path / "test.json"
    assert _find_video_for_sidecar(json_path, {}, tmp_path) is None


# ── Unit: _list_all_recordings ───────────────────────────────────────────────

def test_list_recordings_finds_segments(rec_dir):
    with patch("web.app._load_config", return_value=_patch_config(rec_dir)):
        recordings = _list_all_recordings()
    assert len(recordings) == 2
    # Newest first
    assert recordings[0]["start_time"] >= recordings[1]["start_time"]


def test_list_recordings_includes_metadata(rec_dir):
    with patch("web.app._load_config", return_value=_patch_config(rec_dir)):
        recordings = _list_all_recordings()

    for rec in recordings:
        assert "id" in rec
        assert "start_time" in rec
        assert "duration_seconds" in rec
        assert "frame_count" in rec
        assert "locked" in rec
        assert "size_mb" in rec
        assert "filename" in rec


def test_list_recordings_locked_status(rec_dir):
    with patch("web.app._load_config", return_value=_patch_config(rec_dir)):
        recordings = _list_all_recordings()

    locked = [r for r in recordings if r["locked"]]
    unlocked = [r for r in recordings if not r["locked"]]
    assert len(locked) == 1
    assert len(unlocked) == 1


def test_list_recordings_empty_dir(tmp_path):
    with patch("web.app._load_config", return_value=_patch_config(tmp_path)):
        recordings = _list_all_recordings()
    assert recordings == []


# ── Route: GET /recordings ───────────────────────────────────────────────────

def test_recordings_page_returns_200(client, rec_dir):
    with patch("web.app._load_config", return_value=_patch_config(rec_dir)):
        resp = client.get("/recordings")
    assert resp.status_code == 200
    assert b"Recordings" in resp.data


def test_recordings_page_with_date_filter(client, rec_dir):
    with patch("web.app._load_config", return_value=_patch_config(rec_dir)):
        resp = client.get("/recordings?date=2026-05-04")
    assert resp.status_code == 200
    assert b"14-30-00" in resp.data


def test_recordings_page_empty_date_filter(client, rec_dir):
    with patch("web.app._load_config", return_value=_patch_config(rec_dir)):
        resp = client.get("/recordings?date=1999-01-01")
    assert resp.status_code == 200
    assert b"No recordings found" in resp.data


# ── Route: GET /recordings/list ──────────────────────────────────────────────

def test_recordings_list_api(client, rec_dir):
    with patch("web.app._load_config", return_value=_patch_config(rec_dir)):
        resp = client.get("/recordings/list")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["count"] == 2
    assert len(data["recordings"]) == 2


# ── Route: GET /recordings/video/<id> ────────────────────────────────────────

def test_serve_video_returns_file(client, rec_dir):
    with patch("web.app._load_config", return_value=_patch_config(rec_dir)):
        resp = client.get("/recordings/video/2026-05-04/14-30-00")
    assert resp.status_code == 200


def test_serve_video_not_found(client, rec_dir):
    with patch("web.app._load_config", return_value=_patch_config(rec_dir)):
        resp = client.get("/recordings/video/2026-05-04/99-99-99")
    assert resp.status_code == 404


def test_serve_video_path_traversal(client, rec_dir):
    with patch("web.app._load_config", return_value=_patch_config(rec_dir)):
        resp = client.get("/recordings/video/../../../etc/passwd")
    assert resp.status_code in (403, 404)


# ── Route: POST /recordings/lock ─────────────────────────────────────────────

def test_lock_recording(client, rec_dir):
    with patch("web.app._load_config", return_value=_patch_config(rec_dir)):
        resp = client.post("/recordings/lock",
                           json={"id": "2026-05-04/14-30-00"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["locked"] is True

    # Verify sidecar updated
    sidecar = rec_dir / "2026-05-04" / "14-30-00.json"
    meta = json.loads(sidecar.read_text())
    assert meta["locked"] is True


def test_lock_missing_id(client, rec_dir):
    with patch("web.app._load_config", return_value=_patch_config(rec_dir)):
        resp = client.post("/recordings/lock", json={})
    assert resp.status_code == 400


def test_lock_nonexistent_recording(client, rec_dir):
    with patch("web.app._load_config", return_value=_patch_config(rec_dir)):
        resp = client.post("/recordings/lock",
                           json={"id": "2026-05-04/99-99-99"})
    assert resp.status_code == 404


# ── Route: POST /recordings/unlock ───────────────────────────────────────────

def test_unlock_recording(client, rec_dir):
    with patch("web.app._load_config", return_value=_patch_config(rec_dir)):
        resp = client.post("/recordings/unlock",
                           json={"id": "2026-05-04/14-31-00"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["locked"] is False

    sidecar = rec_dir / "2026-05-04" / "14-31-00.json"
    meta = json.loads(sidecar.read_text())
    assert meta["locked"] is False


# ── Route: POST /recordings/delete ───────────────────────────────────────────

def test_delete_unlocked_recording(client, rec_dir):
    with patch("web.app._load_config", return_value=_patch_config(rec_dir)):
        resp = client.post("/recordings/delete",
                           json={"id": "2026-05-04/14-30-00"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["bytes_freed"] > 0

    # Files should be gone
    assert not (rec_dir / "2026-05-04" / "14-30-00.mp4").exists()
    assert not (rec_dir / "2026-05-04" / "14-30-00.json").exists()


def test_delete_locked_recording_refused(client, rec_dir):
    with patch("web.app._load_config", return_value=_patch_config(rec_dir)):
        resp = client.post("/recordings/delete",
                           json={"id": "2026-05-04/14-31-00"})
    assert resp.status_code == 409
    data = resp.get_json()
    assert data["ok"] is False
    assert "locked" in data["error"].lower()

    # Files should still exist
    assert (rec_dir / "2026-05-04" / "14-31-00.mp4").exists()
    assert (rec_dir / "2026-05-04" / "14-31-00.json").exists()


def test_delete_missing_id(client, rec_dir):
    with patch("web.app._load_config", return_value=_patch_config(rec_dir)):
        resp = client.post("/recordings/delete", json={})
    assert resp.status_code == 400


def test_delete_nonexistent_recording(client, rec_dir):
    with patch("web.app._load_config", return_value=_patch_config(rec_dir)):
        resp = client.post("/recordings/delete",
                           json={"id": "2026-05-04/99-99-99"})
    assert resp.status_code == 404


def test_delete_path_traversal(client, rec_dir):
    with patch("web.app._load_config", return_value=_patch_config(rec_dir)):
        resp = client.post("/recordings/delete",
                           json={"id": "../../etc/passwd"})
    assert resp.status_code in (403, 404)


# ── Integration: unlock then delete ──────────────────────────────────────────

def test_unlock_then_delete(client, rec_dir):
    with patch("web.app._load_config", return_value=_patch_config(rec_dir)):
        # First attempt to delete locked — should fail
        resp = client.post("/recordings/delete",
                           json={"id": "2026-05-04/14-31-00"})
        assert resp.status_code == 409

        # Unlock it
        resp = client.post("/recordings/unlock",
                           json={"id": "2026-05-04/14-31-00"})
        assert resp.status_code == 200

        # Now delete should succeed
        resp = client.post("/recordings/delete",
                           json={"id": "2026-05-04/14-31-00"})
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True


def test_delete_removes_empty_date_dir(client, rec_dir):
    """After deleting all recordings in a date dir, the dir should be removed."""
    with patch("web.app._load_config", return_value=_patch_config(rec_dir)):
        # Delete unlocked segment
        client.post("/recordings/delete",
                     json={"id": "2026-05-04/14-30-00"})

        # Unlock and delete the other
        client.post("/recordings/unlock",
                     json={"id": "2026-05-04/14-31-00"})
        client.post("/recordings/delete",
                     json={"id": "2026-05-04/14-31-00"})

    assert not (rec_dir / "2026-05-04").exists()
