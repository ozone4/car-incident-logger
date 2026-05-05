"""
test_storage_manager.py — Tests for storage manager deletion rules.

Uses temp directories and mocks; no real camera or heavy deps required.
"""

import json
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from modules.storage_manager import StorageManager


# ── Helpers ──────────────────────────────────────────────────────────────────


def _create_recording(
    base_path: Path,
    name: str = "12-00-00",
    date_dir: str = "2025-01-01",
    locked: bool = False,
    age_days: float = 0,
    size_bytes: int = 1024,
) -> Path:
    """Create a fake recording segment with sidecar JSON."""
    day_dir = base_path / date_dir
    day_dir.mkdir(parents=True, exist_ok=True)

    video_path = day_dir / f"{name}.mp4"
    json_path = day_dir / f"{name}.json"

    video_path.write_bytes(b"\x00" * size_bytes)

    from datetime import datetime, timezone, timedelta

    end_time = datetime.now(timezone.utc) - timedelta(days=age_days)

    meta = {
        "start_time": (end_time - timedelta(seconds=60)).isoformat(),
        "end_time": end_time.isoformat(),
        "duration_seconds": 60,
        "frame_count": 1800,
        "file_path": str(video_path),
        "locked": locked,
    }
    json_path.write_text(json.dumps(meta))
    return video_path


# ── Tests ────────────────────────────────────────────────────────────────────


class TestStorageManagerDeletion:
    def test_deletes_old_recordings(self, tmp_path):
        """Recordings older than max_age should be deleted."""
        _create_recording(tmp_path, name="old", age_days=10)
        _create_recording(tmp_path, name="recent", age_days=1)

        mgr = StorageManager(
            recording_path=tmp_path,
            max_recording_age_days=7,
            min_free_space_gb=0,  # disable space-based cleanup
        )
        result = mgr.run_cleanup()

        assert result["deleted_count"] == 1
        assert "old" in result["deleted"][0]["path"]
        assert result["deleted"][0]["reason"] == "max_age"

        # Recent recording should still exist
        assert (tmp_path / "2025-01-01" / "recent.mp4").exists()

    def test_preserves_locked_recordings(self, tmp_path):
        """Locked recordings should never be deleted regardless of age."""
        _create_recording(tmp_path, name="locked-old", age_days=30, locked=True)
        _create_recording(tmp_path, name="unlocked-old", age_days=30, locked=False)

        mgr = StorageManager(
            recording_path=tmp_path,
            max_recording_age_days=7,
            min_free_space_gb=0,
        )
        result = mgr.run_cleanup()

        assert result["deleted_count"] == 1
        assert "unlocked-old" in result["deleted"][0]["path"]
        # Locked file still exists
        assert (tmp_path / "2025-01-01" / "locked-old.mp4").exists()
        assert (tmp_path / "2025-01-01" / "locked-old.json").exists()

    def test_low_space_deletes_oldest_first(self, tmp_path):
        """When disk space is low, oldest unlocked recordings are deleted first."""
        _create_recording(tmp_path, name="oldest", date_dir="2025-01-01", age_days=5)
        _create_recording(tmp_path, name="middle", date_dir="2025-01-03", age_days=3)
        _create_recording(tmp_path, name="newest", date_dir="2025-01-05", age_days=1)

        mgr = StorageManager(
            recording_path=tmp_path,
            max_recording_age_days=999,  # don't trigger age-based
            min_free_space_gb=9999,  # always trigger space-based
        )

        # Mock disk_usage to report low space, then enough after one deletion
        call_count = [0]
        original_disk_usage = mgr._free_space_gb

        def mock_free_space():
            call_count[0] += 1
            # After first call (initial check), report enough space
            if call_count[0] <= 2:
                return 0.5  # below threshold
            return 9999.0  # above threshold

        with patch.object(mgr, "_free_space_gb", side_effect=mock_free_space):
            result = mgr.run_cleanup()

        assert result["deleted_count"] >= 1
        assert result["deleted"][0]["reason"] == "low_space"

    def test_dry_run_does_not_delete(self, tmp_path):
        """Dry run should report what would be deleted without actually deleting."""
        _create_recording(tmp_path, name="old", age_days=30)

        mgr = StorageManager(
            recording_path=tmp_path,
            max_recording_age_days=7,
            min_free_space_gb=0,
        )
        result = mgr.run_cleanup(dry_run=True)

        assert result["deleted_count"] == 1
        assert result["dry_run"] is True
        # File should still exist
        assert (tmp_path / "2025-01-01" / "old.mp4").exists()

    def test_empty_directory_no_crash(self, tmp_path):
        """Should handle empty recording directory gracefully."""
        mgr = StorageManager(recording_path=tmp_path, max_recording_age_days=7)
        result = mgr.run_cleanup()
        assert result["deleted_count"] == 0

    def test_nonexistent_directory(self, tmp_path):
        """Should handle nonexistent recording path gracefully."""
        mgr = StorageManager(
            recording_path=tmp_path / "nonexistent",
            max_recording_age_days=7,
        )
        result = mgr.run_cleanup()
        assert result["deleted_count"] == 0

    def test_status_reports_correctly(self, tmp_path):
        """Status should report recording counts and config."""
        _create_recording(tmp_path, name="a", age_days=1)
        _create_recording(tmp_path, name="b", age_days=2, locked=True)

        mgr = StorageManager(
            recording_path=tmp_path,
            max_recording_age_days=7,
            min_free_space_gb=2.0,
            cleanup_interval_seconds=300,
        )
        status = mgr.status()

        assert status["recording_count"] == 2
        assert status["locked_count"] == 1
        assert status["max_recording_age_days"] == 7
        assert status["min_free_space_gb"] == 2.0
        assert status["free_space_gb"] > 0

    def test_removes_empty_date_dirs(self, tmp_path):
        """After deleting all recordings in a date folder, the folder should be removed."""
        _create_recording(tmp_path, name="only-one", date_dir="2024-12-01", age_days=30)

        mgr = StorageManager(
            recording_path=tmp_path,
            max_recording_age_days=7,
            min_free_space_gb=0,
        )
        mgr.run_cleanup()

        assert not (tmp_path / "2024-12-01").exists()

    def test_start_stop(self, tmp_path):
        """Start and stop should not crash."""
        mgr = StorageManager(
            recording_path=tmp_path,
            max_recording_age_days=7,
            cleanup_interval_seconds=9999,  # won't fire again during test
        )
        mgr.start()
        assert mgr.is_running
        time.sleep(0.2)
        mgr.stop()
        assert not mgr.is_running
