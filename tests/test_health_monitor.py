"""
test_health_monitor.py — Tests for health monitor status composition.

No real camera, GPU, or heavy deps required.
"""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from modules.health_monitor import HealthMonitor


class TestHealthMonitor:
    def test_all_green_when_everything_running(self, tmp_path):
        """Full healthy state should return green."""
        monitor = HealthMonitor(recording_path=tmp_path)

        result = monitor.check(
            camera_running=True,
            dashcam_buffer_armed=True,
            loop_recorder_status={"recording": True, "segments_completed": 5},
            alpr_state={"running": True, "ready": True, "mode": "yolo+paddle", "error": None},
            storage_status={"running": True, "total_deleted": 0, "recording_count": 10},
        )

        assert result["status"] == "green"
        assert result["issues"] == []
        assert result["components"]["camera"]["status"] == "green"
        assert result["components"]["loop_recorder"]["status"] == "green"

    def test_camera_not_running_is_red(self, tmp_path):
        """Camera down should produce red status."""
        monitor = HealthMonitor(recording_path=tmp_path)
        result = monitor.check(camera_running=False)

        assert result["status"] == "red"
        assert any("Camera" in i for i in result["issues"])

    def test_low_disk_warning(self, tmp_path):
        """Low disk should produce yellow/red."""
        monitor = HealthMonitor(recording_path=tmp_path, min_free_space_gb=999)

        # Mock disk to report low space
        with patch.object(monitor, "_check_disk", return_value={
            "free_gb": 1.5,
            "total_gb": 100,
            "used_gb": 98.5,
            "percent_used": 98.5,
            "status": "yellow",
        }):
            result = monitor.check(camera_running=True, dashcam_buffer_armed=True)

        assert result["status"] == "yellow"
        assert any("Disk" in i for i in result["issues"])

    def test_critical_disk_is_red(self, tmp_path):
        monitor = HealthMonitor(recording_path=tmp_path)

        with patch.object(monitor, "_check_disk", return_value={
            "free_gb": 0.5,
            "total_gb": 100,
            "used_gb": 99.5,
            "percent_used": 99.5,
            "status": "red",
        }):
            result = monitor.check(camera_running=True, dashcam_buffer_armed=True)

        assert result["status"] == "red"
        assert any("critically" in i for i in result["issues"])

    def test_recorder_error_reported(self, tmp_path):
        monitor = HealthMonitor(recording_path=tmp_path)
        result = monitor.check(
            camera_running=True,
            loop_recorder_status={"recording": True, "last_error": "codec failed"},
        )
        assert any("codec failed" in i for i in result["issues"])

    def test_alpr_optional(self, tmp_path):
        """Health check should work with no ALPR state."""
        monitor = HealthMonitor(recording_path=tmp_path)
        result = monitor.check(camera_running=True)
        assert "alpr" not in result["components"]

    def test_disk_info_in_result(self, tmp_path):
        monitor = HealthMonitor(recording_path=tmp_path)
        result = monitor.check(camera_running=True)
        assert "disk" in result
        assert "free_gb" in result["disk"]
        assert "total_gb" in result["disk"]

    def test_dashcam_buffer_not_armed_yellow(self, tmp_path):
        """Dashcam buffer not armed while camera running = yellow."""
        monitor = HealthMonitor(recording_path=tmp_path)
        result = monitor.check(camera_running=True, dashcam_buffer_armed=False)
        assert result["components"]["dashcam_buffer"]["status"] == "yellow"
        assert any("buffer" in i.lower() for i in result["issues"])

    def test_timestamp_in_result(self, tmp_path):
        monitor = HealthMonitor(recording_path=tmp_path)
        result = monitor.check(camera_running=True)
        assert "timestamp" in result
        assert isinstance(result["timestamp"], float)
