"""
health_monitor.py — Exposes system health status for the dashboard/API.

Composes health from multiple subsystems into a single status with
green/yellow/red overall level and human-readable issues list.
"""

import shutil
import time
from pathlib import Path
from typing import Any, Optional


# Thresholds
DISK_WARNING_GB = 3.0
DISK_CRITICAL_GB = 1.0
SEGMENT_STALE_SECONDS = 180  # 3 minutes without a completed segment = warning


class HealthMonitor:
    """Composes health status from app subsystem state."""

    def __init__(
        self,
        recording_path: Path,
        min_free_space_gb: float = 2.0,
    ):
        self._recording_path = Path(recording_path)
        self._min_free_gb = min_free_space_gb

    def check(
        self,
        camera_running: bool = False,
        dashcam_buffer_armed: bool = False,
        loop_recorder_status: Optional[dict] = None,
        alpr_state: Optional[dict] = None,
        storage_status: Optional[dict] = None,
    ) -> dict:
        """Run all health checks and return composite status.

        Returns:
            {
                "status": "green" | "yellow" | "red",
                "issues": [...],
                "components": {...},
                "disk": {...},
            }
        """
        issues: list[str] = []
        components: dict[str, dict[str, Any]] = {}

        # ── Camera ───────────────────────────────────────────────────────
        components["camera"] = {
            "running": camera_running,
            "status": "green" if camera_running else "red",
        }
        if not camera_running:
            issues.append("Camera is not running")

        # ── Dashcam buffer ───────────────────────────────────────────────
        components["dashcam_buffer"] = {
            "armed": dashcam_buffer_armed,
            "status": "green" if dashcam_buffer_armed else "yellow",
        }
        if not dashcam_buffer_armed and camera_running:
            issues.append("Dashcam buffer not armed")

        # ── Loop recorder ────────────────────────────────────────────────
        rec = loop_recorder_status or {}
        rec_recording = rec.get("recording", False)
        rec_status = "green" if rec_recording else ("yellow" if camera_running else "grey")

        # Check segment recency
        last_segment = rec.get("last_completed_segment")
        segment_stale = False
        if rec_recording and last_segment:
            # last_completed_segment is a path string; check JSON sidecar mtime
            pass  # We rely on segments_completed growing; staleness via time below

        if rec_recording and rec.get("segments_completed", 0) == 0:
            # Recording but no segment completed yet — might be in first segment
            pass

        components["loop_recorder"] = {
            "recording": rec_recording,
            "segments_completed": rec.get("segments_completed", 0),
            "last_completed_segment": last_segment,
            "status": rec_status,
        }
        if camera_running and not rec_recording:
            rec_enabled = rec.get("enabled", True)
            if rec_enabled:
                issues.append("Loop recorder not recording")

        if rec.get("last_error"):
            issues.append(f"Recorder error: {rec['last_error']}")

        # ── Disk ─────────────────────────────────────────────────────────
        disk = self._check_disk()
        components["disk"] = disk
        if disk["status"] == "red":
            issues.append(f"Disk critically low: {disk['free_gb']:.1f} GB free")
        elif disk["status"] == "yellow":
            issues.append(f"Disk space low: {disk['free_gb']:.1f} GB free")

        # ── ALPR ─────────────────────────────────────────────────────────
        if alpr_state is not None:
            alpr_running = alpr_state.get("running", False)
            alpr_error = alpr_state.get("error")
            alpr_status_val = "green" if (alpr_running and not alpr_error) else (
                "yellow" if alpr_running else "grey"
            )
            components["alpr"] = {
                "running": alpr_running,
                "ready": alpr_state.get("ready", False),
                "mode": alpr_state.get("mode", "unavailable"),
                "frames_scanned": alpr_state.get("frames_scanned", 0),
                "error": alpr_error,
                "status": alpr_status_val,
            }
            if alpr_running and alpr_error:
                issues.append(f"ALPR issue: {alpr_error}")

        # ── Storage manager ──────────────────────────────────────────────
        if storage_status is not None:
            components["storage"] = {
                "running": storage_status.get("running", False),
                "total_deleted": storage_status.get("total_deleted", 0),
                "recording_count": storage_status.get("recording_count", 0),
                "status": "green" if storage_status.get("running") else "yellow",
            }

        # ── Overall status ───────────────────────────────────────────────
        component_statuses = [c.get("status", "grey") for c in components.values()]
        if "red" in component_statuses:
            overall = "red"
        elif "yellow" in component_statuses:
            overall = "yellow"
        elif any(s == "green" for s in component_statuses):
            overall = "green"
        else:
            overall = "grey"

        return {
            "status": overall,
            "issues": issues,
            "components": components,
            "disk": disk,
            "timestamp": time.time(),
        }

    def _check_disk(self) -> dict:
        """Check disk space for the recording path."""
        try:
            path = self._recording_path if self._recording_path.exists() else Path(".")
            usage = shutil.disk_usage(path)
            free_gb = usage.free / (1024**3)
            total_gb = usage.total / (1024**3)
            used_gb = usage.used / (1024**3)
            pct_used = (usage.used / usage.total) * 100 if usage.total > 0 else 0

            if free_gb < DISK_CRITICAL_GB:
                status = "red"
            elif free_gb < DISK_WARNING_GB or free_gb < self._min_free_gb:
                status = "yellow"
            else:
                status = "green"

            return {
                "free_gb": round(free_gb, 2),
                "total_gb": round(total_gb, 2),
                "used_gb": round(used_gb, 2),
                "percent_used": round(pct_used, 1),
                "status": status,
            }
        except OSError:
            return {
                "free_gb": 0,
                "total_gb": 0,
                "used_gb": 0,
                "percent_used": 0,
                "status": "red",
                "error": "Could not read disk usage",
            }
