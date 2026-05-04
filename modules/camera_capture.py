"""
camera_capture.py — OpenCV USB camera capture.

Runs in a background thread and puts (frame, timestamp) tuples into a queue.
Handles camera disconnect with exponential backoff retry.
"""

import cv2
import platform
import time
import queue
import logging
import threading
from typing import Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# Maximum frames held in the output queue before old ones are dropped.
_QUEUE_MAXSIZE = 10


class CameraCapture:
    def __init__(
        self,
        device_index: int = 0,
        width: int = 1920,
        height: int = 1080,
        fps: int = 30,
        fourcc_str: str = "MJPG",
    ):
        self.device_index = device_index
        self.width = width
        self.height = height
        self.fps = fps
        self.fourcc_str = fourcc_str

        self._cap: Optional[cv2.VideoCapture] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._frame_queue: queue.Queue[Tuple[np.ndarray, float]] = queue.Queue(
            maxsize=_QUEUE_MAXSIZE
        )
        self._lock = threading.Lock()
        self._latest_frame: Optional[Tuple[np.ndarray, float]] = None
        self._running = False

    # ── Public API ────────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._running:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._capture_loop, daemon=True, name="CameraCapture")
        self._thread.start()
        self._running = True
        logger.info("CameraCapture started (device=%d, %dx%d @ %dfps %s)",
                    self.device_index, self.width, self.height, self.fps, self.fourcc_str)

    def stop(self) -> None:
        self._stop_event.set()
        self._running = False
        if self._thread:
            self._thread.join(timeout=5.0)
        if self._cap and self._cap.isOpened():
            self._cap.release()
            self._cap = None
        logger.info("CameraCapture stopped")

    def get_frame(self) -> Optional[Tuple[np.ndarray, float]]:
        """Return the most recent (frame, timestamp) or None if unavailable."""
        with self._lock:
            return self._latest_frame

    def get_frame_queue(self) -> "queue.Queue[Tuple[np.ndarray, float]]":
        """Return the underlying frame queue for consumers like RollingBuffer."""
        return self._frame_queue

    def wait_for_first_frame(self, timeout: float = 5.0) -> bool:
        """Block until a frame is available or *timeout* expires. Returns True if ready."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.get_frame() is not None:
                return True
            time.sleep(0.1)
        return False

    @property
    def is_running(self) -> bool:
        return self._running and not self._stop_event.is_set()

    # ── Internal ──────────────────────────────────────────────────────────────

    def _open_camera(self) -> bool:
        if self._cap and self._cap.isOpened():
            self._cap.release()

        logger.debug("Opening camera device %d", self.device_index)
        if platform.system() == "Linux":
            cap = cv2.VideoCapture(self.device_index, cv2.CAP_V4L2)
            if not cap.isOpened():
                cap = cv2.VideoCapture(self.device_index)
        else:
            cap = cv2.VideoCapture(self.device_index)

        if not cap.isOpened():
            logger.warning("Could not open camera device %d", self.device_index)
            return False

        # Apply settings
        fourcc = cv2.VideoWriter_fourcc(*self.fourcc_str)
        cap.set(cv2.CAP_PROP_FOURCC, fourcc)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        cap.set(cv2.CAP_PROP_FPS, self.fps)

        actual_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        actual_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        actual_fps = cap.get(cv2.CAP_PROP_FPS)
        logger.info("Camera opened: actual resolution=%dx%d fps=%.1f", actual_w, actual_h, actual_fps)

        self._cap = cap
        return True

    def _capture_loop(self) -> None:
        backoff = 1.0
        max_backoff = 30.0

        while not self._stop_event.is_set():
            if not self._open_camera():
                logger.warning("Retrying camera open in %.0fs", backoff)
                self._stop_event.wait(backoff)
                backoff = min(backoff * 2, max_backoff)
                continue

            backoff = 1.0  # reset on successful open
            consecutive_failures = 0

            while not self._stop_event.is_set():
                ret, frame = self._cap.read()  # type: ignore[union-attr]
                if not ret or frame is None:
                    consecutive_failures += 1
                    if consecutive_failures >= 10:
                        logger.error("Camera read failed %d times consecutively — reconnecting", consecutive_failures)
                        break
                    logger.debug("Camera read returned empty frame (attempt %d)", consecutive_failures)
                    time.sleep(0.05)
                    continue

                consecutive_failures = 0
                ts = time.monotonic()

                with self._lock:
                    self._latest_frame = (frame, ts)

                # Non-blocking put: drop oldest frame if queue is full
                if self._frame_queue.full():
                    try:
                        self._frame_queue.get_nowait()
                    except queue.Empty:
                        pass
                self._frame_queue.put_nowait((frame, ts))

        if self._cap:
            self._cap.release()
            self._cap = None
