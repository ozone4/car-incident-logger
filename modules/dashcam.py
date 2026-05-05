"""
dashcam.py — Dashcam incident capture: snapshots the rolling buffer (pre-roll)
and optionally captures additional post-roll frames, saves a clip + metadata JSON.

Works with the existing RollingBuffer / CameraCapture pipeline.
"""

import json
import logging
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import cv2
import numpy as np

logger = logging.getLogger(__name__)


class DashcamRecorder:
    """Captures dashcam incident clips from the rolling buffer."""

    def __init__(
        self,
        output_path: Path,
        pre_roll_seconds: float = 30.0,
        post_roll_seconds: float = 5.0,
        fps: int = 30,
    ):
        self.output_path = Path(output_path)
        self.pre_roll_seconds = pre_roll_seconds
        self.post_roll_seconds = post_roll_seconds
        self.fps = fps

        self._rolling_buffer = None
        self._camera = None
        self._ffmpeg_available = shutil.which("ffmpeg") is not None
        self._busy = threading.Lock()

        # Last trigger status (read by web UI)
        self._last_result: Optional[Dict[str, Any]] = None
        self._last_error: Optional[str] = None
        self._capture_state: str = "idle"  # idle, capturing, saving, done, error

    def attach(self, rolling_buffer, camera=None) -> None:
        """Attach a RollingBuffer (required) and optionally a CameraCapture for post-roll."""
        self._rolling_buffer = rolling_buffer
        self._camera = camera

    @property
    def is_attached(self) -> bool:
        return self._rolling_buffer is not None

    @property
    def last_result(self) -> Optional[Dict[str, Any]]:
        return self._last_result

    @property
    def last_error(self) -> Optional[str]:
        return self._last_error

    @property
    def capture_state(self) -> str:
        return self._capture_state

    def buffer_status(self) -> Dict[str, Any]:
        """Return current buffer state for the web UI."""
        if self._rolling_buffer is None:
            return {"attached": False, "frame_count": 0, "duration": 0.0}
        return {
            "attached": True,
            "frame_count": self._rolling_buffer.frame_count(),
            "duration": round(self._rolling_buffer.actual_duration(), 1),
            "pre_roll_seconds": self.pre_roll_seconds,
            "post_roll_seconds": self.post_roll_seconds,
        }

    def trigger(
        self,
        source: str = "web",
        alpr_plate: Optional[str] = None,
        recent_sightings: Optional[List[Dict]] = None,
    ) -> Dict[str, Any]:
        """
        Trigger an incident capture.

        Returns a dict with incident metadata (or error info).
        Thread-safe: only one capture runs at a time.
        """
        if not self._busy.acquire(blocking=False):
            err = "Capture already in progress"
            self._last_error = err
            self._capture_state = "error"
            return {"ok": False, "error": err}

        try:
            self._capture_state = "capturing"
            return self._do_capture(source, alpr_plate, recent_sightings)
        except Exception as exc:
            logger.exception("Dashcam capture failed")
            self._last_error = str(exc)
            self._capture_state = "error"
            return {"ok": False, "error": str(exc)}
        finally:
            self._busy.release()

    def _do_capture(
        self,
        source: str,
        alpr_plate: Optional[str],
        recent_sightings: Optional[List[Dict]],
    ) -> Dict[str, Any]:
        if self._rolling_buffer is None:
            self._last_error = "No rolling buffer attached"
            return {"ok": False, "error": self._last_error}

        # 1. Snapshot pre-roll frames from the buffer
        pre_frames = self._rolling_buffer.get_clip(self.pre_roll_seconds)
        if not pre_frames:
            self._last_error = "Buffer is empty — camera may not be running"
            return {"ok": False, "error": self._last_error}

        # 2. Capture post-roll frames (if camera available)
        self._capture_state = "capturing"
        post_frames = self._capture_post_roll()

        all_frames = pre_frames + post_frames

        # 3. Build incident dir
        now_utc = datetime.now(timezone.utc)
        timestamp_str = now_utc.strftime("%Y%m%dT%H%M%SZ")
        safe_ts = timestamp_str  # already filename-safe
        incident_dir = self.output_path / safe_ts
        incident_dir.mkdir(parents=True, exist_ok=True)

        # 4. Save clip
        self._capture_state = "saving"
        clip_path = self._save_clip(all_frames, incident_dir)

        # 5. Build and save metadata
        plate = (alpr_plate or "").strip().upper() or None
        metadata = {
            "timestamp": timestamp_str,
            "trigger_source": source,
            "plate": plate,
            "pre_roll_frames": len(pre_frames),
            "post_roll_frames": len(post_frames),
            "total_frames": len(all_frames),
            "clip_path": str(clip_path) if clip_path else None,
            "recent_sightings": _sanitize_sightings(recent_sightings),
        }
        meta_path = incident_dir / "metadata.json"
        meta_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")

        self._last_result = metadata
        self._last_error = None
        self._capture_state = "done"
        logger.info(
            "Dashcam incident saved: %s (%d frames, plate=%s)",
            incident_dir, len(all_frames), plate or "(none)",
        )
        return {"ok": True, **metadata, "incident_dir": str(incident_dir)}

    def _capture_post_roll(self) -> List[Tuple[np.ndarray, float]]:
        """Capture additional frames after the trigger for post-roll."""
        if self.post_roll_seconds <= 0 or self._camera is None:
            return []
        if not getattr(self._camera, "is_running", False):
            return []

        frames: List[Tuple[np.ndarray, float]] = []
        deadline = time.monotonic() + self.post_roll_seconds
        interval = 1.0 / max(self.fps, 1)

        while time.monotonic() < deadline:
            result = self._camera.get_frame()
            if result is not None:
                frames.append(result)
            time.sleep(interval)

        return frames

    def _save_clip(
        self, frames: List[Tuple[np.ndarray, float]], incident_dir: Path
    ) -> Optional[Path]:
        if not frames:
            return None

        h, w = frames[0][0].shape[:2]

        if self._ffmpeg_available:
            mp4_path = incident_dir / "clip.mp4"
            if self._save_clip_ffmpeg(frames, mp4_path, w, h):
                return mp4_path

        avi_path = incident_dir / "clip.avi"
        if self._save_clip_opencv(frames, avi_path, w, h):
            return avi_path
        return None

    def _save_clip_ffmpeg(
        self, frames: List[Tuple[np.ndarray, float]], out_path: Path, w: int, h: int
    ) -> bool:
        try:
            cmd = [
                "ffmpeg", "-y",
                "-f", "rawvideo", "-vcodec", "rawvideo",
                "-s", f"{w}x{h}", "-pix_fmt", "bgr24",
                "-r", str(self.fps),
                "-i", "pipe:0",
                "-c:v", "libx264", "-preset", "ultrafast",
                "-crf", "23", "-pix_fmt", "yuv420p",
                str(out_path),
            ]
            proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            )
            for frame, _ in frames:
                proc.stdin.write(frame.astype(np.uint8).tobytes())
            proc.stdin.close()
            proc.wait(timeout=120)

            if proc.returncode != 0:
                stderr = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
                logger.error("ffmpeg exited %d: %s", proc.returncode, stderr[-500:])
                return False
            return True
        except Exception as exc:
            logger.error("ffmpeg clip save failed: %s", exc)
            return False

    def _save_clip_opencv(
        self, frames: List[Tuple[np.ndarray, float]], out_path: Path, w: int, h: int
    ) -> bool:
        fourcc = cv2.VideoWriter_fourcc(*"XVID")
        writer = cv2.VideoWriter(str(out_path), fourcc, self.fps, (w, h))
        if not writer.isOpened():
            logger.error("cv2.VideoWriter failed to open %s", out_path)
            return False
        for frame, _ in frames:
            writer.write(frame)
        writer.release()
        return True


def _sanitize_sightings(sightings: Optional[List[Dict]]) -> List[Dict]:
    """Keep only JSON-safe fields from sighting dicts."""
    if not sightings:
        return []
    safe = []
    for s in sightings[:10]:
        safe.append({
            "plate": s.get("plate"),
            "confidence": s.get("confidence") or s.get("best_confidence"),
            "source": s.get("source"),
            "seen_count": s.get("seen_count"),
        })
    return safe
