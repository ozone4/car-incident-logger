"""
storage_manager.py — Manages retention of continuous recordings.

Deletes oldest unlocked recordings when:
  - older than configured max age, OR
  - free disk space is below configured minimum.

Never deletes locked recordings (locked: true in sidecar JSON) or incident clips.
"""

import json
import logging
import shutil
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class StorageManager:
    """Periodic cleanup of continuous recording segments."""

    def __init__(
        self,
        recording_path: Path,
        max_recording_age_days: float = 7,
        min_free_space_gb: float = 2.0,
        cleanup_interval_seconds: float = 300,
    ):
        self._recording_path = Path(recording_path)
        self._max_age_days = max_recording_age_days
        self._min_free_space_gb = min_free_space_gb
        self._interval = cleanup_interval_seconds

        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._last_cleanup: Optional[float] = None
        self._last_deleted_count: int = 0
        self._total_deleted: int = 0
        self._total_bytes_freed: int = 0

    # ── Public API ───────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the periodic cleanup loop in a background daemon thread."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._cleanup_loop, daemon=True, name="StorageManager"
        )
        self._thread.start()
        logger.info(
            "Storage manager started (max_age=%dd, min_free=%.1fGB, interval=%ds)",
            self._max_age_days,
            self._min_free_space_gb,
            self._interval,
        )

    def stop(self) -> None:
        """Stop the cleanup loop."""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)
        self._thread = None
        logger.info("Storage manager stopped")

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def run_cleanup(self, dry_run: bool = False) -> dict:
        """Run one cleanup pass. Returns summary of actions taken (or planned if dry_run)."""
        deleted = []
        bytes_freed = 0
        reasons: list[str] = []

        recordings = self._list_recordings()
        now = time.time()
        max_age_seconds = self._max_age_days * 86400

        # Phase 1: delete recordings older than max age
        for rec in recordings:
            if rec["locked"]:
                continue
            age = now - rec["end_time"]
            if age > max_age_seconds:
                if not dry_run:
                    freed = self._delete_recording(rec)
                    bytes_freed += freed
                deleted.append({
                    "path": str(rec["video_path"]),
                    "reason": "max_age",
                    "age_days": round(age / 86400, 1),
                })

        # Phase 2: if still low on space, delete oldest unlocked until threshold met
        if not dry_run:
            recordings = self._list_recordings()  # refresh after age deletions

        free_gb = self._free_space_gb()
        if free_gb < self._min_free_space_gb:
            reasons.append(f"low_disk_space ({free_gb:.2f}GB < {self._min_free_space_gb}GB)")
            unlocked = [r for r in recordings if not r["locked"]]
            unlocked.sort(key=lambda r: r["end_time"])  # oldest first

            for rec in unlocked:
                if self._free_space_gb() >= self._min_free_space_gb:
                    break
                if not dry_run:
                    freed = self._delete_recording(rec)
                    bytes_freed += freed
                deleted.append({
                    "path": str(rec["video_path"]),
                    "reason": "low_space",
                    "age_days": round((now - rec["end_time"]) / 86400, 1),
                })

        if not dry_run:
            self._last_cleanup = now
            self._last_deleted_count = len(deleted)
            self._total_deleted += len(deleted)
            self._total_bytes_freed += bytes_freed

        if deleted and not dry_run:
            logger.info(
                "Storage cleanup: deleted %d recordings, freed %.1f MB",
                len(deleted),
                bytes_freed / (1024 * 1024),
            )

        return {
            "deleted_count": len(deleted),
            "bytes_freed": bytes_freed,
            "deleted": deleted,
            "reasons": reasons,
            "dry_run": dry_run,
            "free_space_gb": round(self._free_space_gb(), 2),
        }

    def status(self) -> dict:
        """Return current storage manager status."""
        free_gb = self._free_space_gb()
        recordings = self._list_recordings()
        locked_count = sum(1 for r in recordings if r["locked"])
        total_size = sum(r["size_bytes"] for r in recordings)

        return {
            "running": self.is_running,
            "recording_path": str(self._recording_path),
            "max_recording_age_days": self._max_age_days,
            "min_free_space_gb": self._min_free_space_gb,
            "cleanup_interval_seconds": self._interval,
            "free_space_gb": round(free_gb, 2),
            "recording_count": len(recordings),
            "locked_count": locked_count,
            "total_recording_size_mb": round(total_size / (1024 * 1024), 1),
            "last_cleanup": self._last_cleanup,
            "last_deleted_count": self._last_deleted_count,
            "total_deleted": self._total_deleted,
            "total_bytes_freed": self._total_bytes_freed,
        }

    # ── Internal ─────────────────────────────────────────────────────────────

    def _cleanup_loop(self) -> None:
        """Periodically run cleanup until stopped."""
        # Run immediately on start, then every interval
        while not self._stop_event.is_set():
            try:
                self.run_cleanup(dry_run=False)
            except Exception as exc:
                logger.error("Storage cleanup error: %s", exc)
            self._stop_event.wait(self._interval)

    def _list_recordings(self) -> list[dict]:
        """List all recording segments with metadata."""
        results = []
        if not self._recording_path.exists():
            return results

        for json_path in self._recording_path.rglob("*.json"):
            try:
                meta = json.loads(json_path.read_text())
            except (json.JSONDecodeError, OSError):
                continue

            # Find the corresponding video file
            video_path = self._find_video_for_sidecar(json_path, meta)
            if video_path is None:
                continue

            end_time_str = meta.get("end_time", "")
            try:
                from datetime import datetime, timezone
                dt = datetime.fromisoformat(end_time_str.replace("Z", "+00:00"))
                end_time = dt.timestamp()
            except (ValueError, AttributeError):
                # Fallback to file mtime
                end_time = video_path.stat().st_mtime

            results.append({
                "video_path": video_path,
                "json_path": json_path,
                "locked": bool(meta.get("locked", False)),
                "end_time": end_time,
                "size_bytes": video_path.stat().st_size if video_path.exists() else 0,
            })

        return results

    def _find_video_for_sidecar(self, json_path: Path, meta: dict) -> Optional[Path]:
        """Find the video file corresponding to a sidecar JSON."""
        # Try file_path from metadata
        file_path = meta.get("file_path")
        if file_path:
            candidate = Path(file_path)
            if candidate.exists():
                return candidate
            # Try relative to recording path
            candidate = self._recording_path / candidate.name
            if candidate.exists():
                return candidate

        # Try same stem as JSON
        stem = json_path.stem
        for ext in (".mp4", ".avi", ".mkv"):
            candidate = json_path.with_suffix(ext)
            if candidate.exists():
                return candidate

        return None

    def _delete_recording(self, rec: dict) -> int:
        """Delete a recording and its sidecar. Returns bytes freed."""
        freed = 0
        video_path: Path = rec["video_path"]
        json_path: Path = rec["json_path"]

        if video_path.exists():
            freed += video_path.stat().st_size
            video_path.unlink()
            logger.debug("Deleted recording: %s", video_path)

        if json_path.exists():
            freed += json_path.stat().st_size
            json_path.unlink()

        # Remove empty parent directories (date folders)
        parent = video_path.parent
        try:
            if parent != self._recording_path and parent.exists() and not any(parent.iterdir()):
                parent.rmdir()
        except OSError:
            pass

        return freed

    def _free_space_gb(self) -> float:
        """Return free disk space in GB for the recording path."""
        try:
            path = self._recording_path if self._recording_path.exists() else Path(".")
            usage = shutil.disk_usage(path)
            return usage.free / (1024**3)
        except OSError:
            return 999.0  # fail-safe: don't trigger deletion on error
