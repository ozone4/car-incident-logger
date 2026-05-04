"""
rolling_buffer.py — Thread-safe circular frame buffer.

Stores the last N seconds of (frame, timestamp) tuples consumed from a camera
queue.  get_clip(seconds_back) returns all frames from N seconds ago to now.
"""

import time
import queue
import logging
import threading
from collections import deque
from typing import List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)


class RollingBuffer:
    def __init__(self, duration_seconds: int = 45, fps: int = 30):
        self.duration_seconds = duration_seconds
        self.fps = fps
        # Pre-size the deque to avoid unbounded growth
        max_frames = (duration_seconds + 5) * fps
        self._buffer: deque[Tuple[np.ndarray, float]] = deque(maxlen=max_frames)
        self._lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._source_queue: Optional["queue.Queue[Tuple[np.ndarray, float]]"] = None

    # ── Public API ────────────────────────────────────────────────────────────

    def start(self, source_queue: "queue.Queue[Tuple[np.ndarray, float]]") -> None:
        """Start consuming frames from *source_queue* in a background thread."""
        self._source_queue = source_queue
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._consume_loop, daemon=True, name="RollingBuffer"
        )
        self._thread.start()
        logger.info(
            "RollingBuffer started (duration=%ds, max_frames=%d)",
            self.duration_seconds,
            self._buffer.maxlen,
        )

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5.0)
        logger.info("RollingBuffer stopped")

    def get_clip(self, seconds_back: Optional[float] = None) -> List[Tuple[np.ndarray, float]]:
        """
        Return a snapshot of frames from *seconds_back* seconds ago to now.

        If seconds_back is None, returns the entire buffer (up to duration_seconds).
        Frames are returned in chronological order (oldest first).
        """
        with self._lock:
            if not self._buffer:
                return []

            if seconds_back is None:
                return list(self._buffer)

            cutoff = time.monotonic() - seconds_back
            return [(f, ts) for f, ts in self._buffer if ts >= cutoff]

    def get_latest_frame(self) -> Optional[Tuple[np.ndarray, float]]:
        with self._lock:
            return self._buffer[-1] if self._buffer else None

    def frame_count(self) -> int:
        with self._lock:
            return len(self._buffer)

    def actual_duration(self) -> float:
        """Return the real span (seconds) currently covered by the buffer."""
        with self._lock:
            if len(self._buffer) < 2:
                return 0.0
            return self._buffer[-1][1] - self._buffer[0][1]

    # ── Internal ──────────────────────────────────────────────────────────────

    def _consume_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                frame, ts = self._source_queue.get(timeout=0.1)  # type: ignore[union-attr]
            except queue.Empty:
                continue

            with self._lock:
                self._buffer.append((frame, ts))
