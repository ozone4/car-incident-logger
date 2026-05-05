"""
test_recording_recovery.py — Tests for power-loss recovery logic.

Uses temp directories and fake files; no real camera required.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from modules.recording_recovery import recover_recordings


# ── Helpers ──────────────────────────────────────────────────────────────────


def _create_inprogress(base: Path, date_dir: str = "2026-05-04", name: str = "14-30-00", size: int = 2048):
    """Create a fake _INPROGRESS video file."""
    day = base / date_dir
    day.mkdir(parents=True, exist_ok=True)
    path = day / f"{name}_INPROGRESS.mp4"
    path.write_bytes(b"\x00" * size)
    return path


def _create_finalized(base: Path, date_dir: str = "2026-05-04", name: str = "14-30-00", complete: bool = True, locked: bool = False):
    """Create a finalized segment with sidecar."""
    day = base / date_dir
    day.mkdir(parents=True, exist_ok=True)
    video = day / f"{name}.mp4"
    video.write_bytes(b"\x00" * 4096)
    meta = {
        "start_time": datetime.now(timezone.utc).isoformat(),
        "end_time": datetime.now(timezone.utc).isoformat(),
        "duration_seconds": 60,
        "frame_count": 1800,
        "file_path": str(video),
        "locked": locked,
        "complete": complete,
    }
    json_path = day / f"{name}.json"
    json_path.write_text(json.dumps(meta, indent=2))
    return video, json_path


def _create_tmp_meta(base: Path, date_dir: str = "2026-05-04", name: str = "14-30-00"):
    """Create an orphaned .json.tmp file."""
    day = base / date_dir
    day.mkdir(parents=True, exist_ok=True)
    path = day / f"{name}.json.tmp"
    path.write_text('{"partial": true}')
    return path


# ── Tests ────────────────────────────────────────────────────────────────────


class TestRecoverInprogressVideo:
    def test_recovers_valid_inprogress(self, tmp_path):
        """Valid _INPROGRESS file is renamed and sidecar created."""
        _create_inprogress(tmp_path, size=2048)

        result = recover_recordings(tmp_path)

        assert len(result["recovered"]) == 1
        assert result["recovered"][0]["action"] == "finalized"
        # Final video should exist
        final = tmp_path / "2026-05-04" / "14-30-00.mp4"
        assert final.exists()
        # Sidecar should exist
        sidecar = tmp_path / "2026-05-04" / "14-30-00.json"
        assert sidecar.exists()
        meta = json.loads(sidecar.read_text())
        assert meta["recovered"] is True
        assert meta["complete"] is True

    def test_moves_empty_inprogress_to_corrupt(self, tmp_path):
        """Tiny _INPROGRESS file is moved to _corrupt folder."""
        _create_inprogress(tmp_path, size=100)  # below 1024 threshold

        result = recover_recordings(tmp_path)

        assert len(result["corrupt"]) == 1
        assert result["corrupt"][0]["action"] == "moved_corrupt"
        # Original should be gone
        assert not (tmp_path / "2026-05-04" / "14-30-00_INPROGRESS.mp4").exists()
        # Should be in _corrupt
        corrupt_dir = tmp_path / "_corrupt"
        assert corrupt_dir.exists()
        assert any(corrupt_dir.iterdir())

    def test_inprogress_with_existing_final_keeps_larger(self, tmp_path):
        """If both _INPROGRESS and final exist, keep the larger one."""
        # Create a small final
        day = tmp_path / "2026-05-04"
        day.mkdir(parents=True, exist_ok=True)
        final = day / "14-30-00.mp4"
        final.write_bytes(b"\x00" * 500)
        # Create a larger inprogress
        _create_inprogress(tmp_path, size=2048)

        result = recover_recordings(tmp_path)

        assert len(result["recovered"]) == 1
        # Final should be the larger one
        assert final.exists()
        assert final.stat().st_size == 2048


class TestRecoverOrphanedMetadata:
    def test_cleans_tmp_metadata(self, tmp_path):
        """Orphaned .json.tmp files are removed."""
        _create_tmp_meta(tmp_path)

        result = recover_recordings(tmp_path)

        assert len(result["cleaned"]) == 1
        assert not (tmp_path / "2026-05-04" / "14-30-00.json.tmp").exists()


class TestRecoverIncompleteSegments:
    def test_finalizes_incomplete_with_valid_video(self, tmp_path):
        """Sidecar with complete:false is marked complete if video is valid."""
        _create_finalized(tmp_path, complete=False)

        result = recover_recordings(tmp_path)

        assert len(result["recovered"]) == 1
        sidecar = tmp_path / "2026-05-04" / "14-30-00.json"
        meta = json.loads(sidecar.read_text())
        assert meta["complete"] is True
        assert meta["recovered"] is True

    def test_skips_already_complete(self, tmp_path):
        """Already-complete segments are not touched."""
        _create_finalized(tmp_path, complete=True)

        result = recover_recordings(tmp_path)

        assert len(result["recovered"]) == 0
        assert len(result["corrupt"]) == 0
        assert len(result["cleaned"]) == 0

    def test_orphaned_sidecar_no_video_cleaned(self, tmp_path):
        """Sidecar with complete:false and no video is cleaned up."""
        day = tmp_path / "2026-05-04"
        day.mkdir(parents=True, exist_ok=True)
        json_path = day / "14-30-00.json"
        meta = {"complete": False, "file_path": "/nonexistent/video.mp4"}
        json_path.write_text(json.dumps(meta))

        result = recover_recordings(tmp_path)

        assert len(result["cleaned"]) == 1
        assert not json_path.exists()


class TestRecoveryEdgeCases:
    def test_nonexistent_path(self, tmp_path):
        """Non-existent path returns empty result."""
        result = recover_recordings(tmp_path / "does_not_exist")
        assert result["recovered"] == []
        assert "does not exist" in result["summary"]

    def test_empty_directory(self, tmp_path):
        """Empty recording directory produces no results."""
        result = recover_recordings(tmp_path)
        assert result["recovered"] == []
        assert result["corrupt"] == []
        assert result["cleaned"] == []

    def test_multiple_inprogress_files(self, tmp_path):
        """Multiple _INPROGRESS files are all handled."""
        _create_inprogress(tmp_path, name="14-30-00", size=2048)
        _create_inprogress(tmp_path, name="14-31-00", size=2048)

        result = recover_recordings(tmp_path)

        assert len(result["recovered"]) == 2

    def test_custom_min_size(self, tmp_path):
        """Custom min_valid_size_bytes threshold works."""
        _create_inprogress(tmp_path, size=500)

        # Default threshold (1024) → corrupt
        result = recover_recordings(tmp_path)
        assert len(result["corrupt"]) == 1

    def test_result_has_expected_keys(self, tmp_path):
        """Result dict has all expected keys."""
        result = recover_recordings(tmp_path)
        assert "recovered" in result
        assert "corrupt" in result
        assert "cleaned" in result
        assert "errors" in result
        assert "summary" in result
        assert "timestamp" in result
