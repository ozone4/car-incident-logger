"""
loop_recorder.py — Continuous loop recording to disk in segments.

Runs as a daemon thread consuming frames from CameraCapture, writing
fixed-duration video segments with sidecar JSON metadata.  Applies an
optional timestamp overlay at encode time (so ALPR sees clean frames).

Currently records VIDEO ONLY. When audio capture is added (planned), it must
respect the `audio.enabled` config flag and the `/audio/toggle` runtime
endpoint exposed by web/app.py — those wires are already in place.
"""

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Union

import cv2
import numpy as np

from modules.overlay import apply_timestamp

logger = logging.getLogger(__name__)


class LoopRecorder:
    """Continuously records camera frames into rotating video segments."""

    def __init__(
        self,
        output_path: Union[str, Path] = "./data/recordings",
        segment_duration_seconds: int = 60,
        fps: int = 30,
        overlay_enabled: bool = True,
        overlay_position: str = "bottom-left",
        overlay_font_scale: float = 0.7,
        overlay_color: tuple = (255, 255, 255),
        overlay_background: bool = True,
    ):
        self.output_path = Path(output_path)
        self.segment_duration = segment_duration_seconds
        self.fps = fps

        # Overlay config
        self.overlay_enabled = overlay_enabled
        self.overlay_position = overlay_position
        self.overlay_font_scale = overlay_font_scale
        self.overlay_color = tuple(overlay_color) if overlay_color else (255, 255, 255)
        self.overlay_background = overlay_background

        # State
        self._camera = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

        # Stats (read by web UI)
        self._recording = False
        self._current_segment_path: Optional[str] = None
        self._current_segment_start: Optional[float] = None
        self._frames_written = 0
        self._segments_completed = 0
        self._last_completed_segment: Optional[str] = None
        self._last_error: Optional[str] = None
        self._total_frames_written = 0

    # ── Public API ───────────────────────────────────────────────────────────

    def start(self, camera) -> None:
        """Start continuous recording, consuming frames from *camera*."""
        if self._thread and self._thread.is_alive():
            logger.warning("LoopRecorder already running")
            return
        self._camera = camera
        self._stop_event.clear()
        self._recording = True
        self._thread = threading.Thread(
            target=self._record_loop, daemon=True, name="LoopRecorder"
        )
        self._thread.start()
        logger.info(
            "LoopRecorder started (segment=%ds, output=%s)",
            self.segment_duration,
            self.output_path,
        )

    def stop(self) -> None:
        """Stop recording and wait for the thread to finish."""
        self._stop_event.set()
        self._recording = False
        if self._thread:
            self._thread.join(timeout=10.0)
            self._thread = None
        logger.info("LoopRecorder stopped")

    @property
    def is_recording(self) -> bool:
        return self._recording and self._thread is not None and self._thread.is_alive()

    def status(self) -> Dict[str, Any]:
        """Return current recording status for the web UI."""
        with self._lock:
            return {
                "recording": self.is_recording,
                "current_segment": self._current_segment_path,
                "current_segment_start": self._current_segment_start,
                "frames_in_segment": self._frames_written,
                "segments_completed": self._segments_completed,
                "total_frames_written": self._total_frames_written,
                "last_completed_segment": self._last_completed_segment,
                "last_error": self._last_error,
                "segment_duration": self.segment_duration,
                "output_path": str(self.output_path),
                "overlay_enabled": self.overlay_enabled,
            }

    # ── Internal ─────────────────────────────────────────────────────────────

    def _record_loop(self) -> None:
        """Main recording loop: rotate segments until stopped."""
        while not self._stop_event.is_set():
            try:
                self._record_segment()
            except Exception as exc:
                logger.exception("LoopRecorder segment failed")
                with self._lock:
                    self._last_error = str(exc)
                # Brief pause before retrying
                self._stop_event.wait(1.0)

    def _record_segment(self) -> None:
        """Record one segment of up to segment_duration seconds."""
        cam = self._camera
        if cam is None or not getattr(cam, "is_running", False):
            self._stop_event.wait(0.5)
            return

        # Wait for first frame to determine dimensions
        result = cam.get_frame()
        if result is None:
            self._stop_event.wait(0.2)
            return

        frame, _ = result
        h, w = frame.shape[:2]

        # Build segment path: data/recordings/YYYY-MM-DD/HH-MM-SS
        now_utc = datetime.now(timezone.utc)
        date_dir = self.output_path / now_utc.strftime("%Y-%m-%d")
        date_dir.mkdir(parents=True, exist_ok=True)
        segment_name = now_utc.strftime("%H-%M-%S")
        # Write to temp file, rename on completion for crash safety
        final_path = date_dir / f"{segment_name}.mp4"
        temp_path = date_dir / f"{segment_name}_INPROGRESS.mp4"

        # OpenCV VideoWriter with mp4v codec (widely available, no external deps)
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        writer = cv2.VideoWriter(str(temp_path), fourcc, self.fps, (w, h))
        if not writer.isOpened():
            logger.error("Failed to open VideoWriter for %s", temp_path)
            with self._lock:
                self._last_error = f"VideoWriter failed: {temp_path}"
            self._stop_event.wait(2.0)
            return

        segment_start = time.monotonic()
        segment_start_utc = now_utc.isoformat()
        frame_count = 0

        with self._lock:
            self._current_segment_path = str(final_path)
            self._current_segment_start = time.time()
            self._frames_written = 0
            self._last_error = None

        try:
            while not self._stop_event.is_set():
                elapsed = time.monotonic() - segment_start
                if elapsed >= self.segment_duration:
                    break

                result = cam.get_frame()
                if result is None:
                    time.sleep(0.01)
                    continue

                frame, ts = result
                if self.overlay_enabled:
                    frame = apply_timestamp(
                        frame,
                        timestamp=time.time(),
                        position=self.overlay_position,
                        font_scale=self.overlay_font_scale,
                        color=self.overlay_color,
                        background=self.overlay_background,
                    )

                writer.write(frame)
                frame_count += 1

                with self._lock:
                    self._frames_written = frame_count
                    self._total_frames_written += 1

                # Pace to avoid busy-spinning faster than camera FPS
                time.sleep(1.0 / max(self.fps, 1))
        finally:
            writer.release()

        # Finalize: rename temp → final
        if frame_count > 0:
            if temp_path.exists():
                try:
                    # On Windows, remove target if it exists before rename
                    if final_path.exists():
                        final_path.unlink()
                    temp_path.rename(final_path)
                except OSError as exc:
                    logger.error("Failed to rename segment %s → %s: %s", temp_path, final_path, exc)
                    final_path = temp_path

            # Write sidecar metadata
            end_utc = datetime.now(timezone.utc)
            duration = time.monotonic() - segment_start
            meta = {
                "start_time": segment_start_utc,
                "end_time": end_utc.isoformat(),
                "duration_seconds": round(duration, 2),
                "frame_count": frame_count,
                "file_path": str(final_path),
                "locked": False,
                "complete": True,
            }
            meta_path = final_path.with_suffix(".json")
            try:
                tmp_meta = meta_path.with_suffix(".json.tmp")
                tmp_meta.write_text(json.dumps(meta, indent=2), encoding="utf-8")
                if meta_path.exists():
                    meta_path.unlink()
                tmp_meta.rename(meta_path)
            except OSError as exc:
                logger.warning("Failed to write segment metadata: %s", exc)

            with self._lock:
                self._segments_completed += 1
                self._last_completed_segment = str(final_path)

            logger.info(
                "Segment complete: %s (%d frames, %.1fs)",
                final_path.name, frame_count, duration,
            )
        else:
            # No frames written — clean up empty temp file
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
