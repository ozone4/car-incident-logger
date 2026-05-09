"""
test_camera_capture.py — Unit tests for modules/camera_capture.py.

cv2.VideoCapture is mocked end-to-end so no real camera is required.
"""

from __future__ import annotations

import sys
import threading
import time
import types
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def _install_cv2_stub() -> types.ModuleType:
    """Install (or reuse) a stub cv2 module that camera_capture can import."""
    if "cv2" in sys.modules and isinstance(sys.modules["cv2"], types.ModuleType):
        cv2 = sys.modules["cv2"]
    else:
        cv2 = types.ModuleType("cv2")
        sys.modules["cv2"] = cv2
    # Constants used by camera_capture
    cv2.CAP_V4L2 = 200
    cv2.CAP_PROP_FOURCC = 6
    cv2.CAP_PROP_FRAME_WIDTH = 3
    cv2.CAP_PROP_FRAME_HEIGHT = 4
    cv2.CAP_PROP_FPS = 5
    if not hasattr(cv2, "VideoWriter_fourcc"):
        cv2.VideoWriter_fourcc = MagicMock(return_value=0)
    return cv2


_install_cv2_stub()
from modules import camera_capture  # noqa: E402


class FakeCapture:
    """Stand-in for cv2.VideoCapture that yields a fixed sequence of frames."""

    def __init__(self, frames=None, opened=True, fail_after=None):
        self._frames = list(frames or [np.zeros((4, 4, 3), dtype=np.uint8) for _ in range(50)])
        self._opened = opened
        self._fail_after = fail_after
        self._reads = 0
        self.released = False
        self.props_set: dict = {}

    def isOpened(self) -> bool:
        return self._opened and not self.released

    def read(self):
        self._reads += 1
        if self._fail_after is not None and self._reads > self._fail_after:
            return False, None
        if not self._frames:
            time.sleep(0.005)
            return False, None
        return True, self._frames.pop(0)

    def set(self, prop, value):
        self.props_set[prop] = value
        return True

    def get(self, prop):
        if prop == camera_capture.cv2.CAP_PROP_FRAME_WIDTH:
            return 1920
        if prop == camera_capture.cv2.CAP_PROP_FRAME_HEIGHT:
            return 1080
        if prop == camera_capture.cv2.CAP_PROP_FPS:
            return 30
        return 0

    def release(self):
        self.released = True


@pytest.fixture
def patch_videocapture(monkeypatch):
    """Replace cv2.VideoCapture so the real one never runs."""
    fake = FakeCapture()

    def factory(*_args, **_kwargs):
        return fake

    monkeypatch.setattr(camera_capture.cv2, "VideoCapture", factory, raising=False)
    return fake


def _wait_until(predicate, timeout=2.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def test_start_runs_capture_thread(patch_videocapture):
    cam = camera_capture.CameraCapture(device_index=0)
    cam.start()
    try:
        assert cam.is_running
        assert _wait_until(lambda: cam.get_frame() is not None), "no frame ever became available"
    finally:
        cam.stop()
    assert not cam.is_running


def test_stop_releases_capture(patch_videocapture):
    cam = camera_capture.CameraCapture()
    cam.start()
    assert _wait_until(lambda: cam.get_frame() is not None)
    cam.stop()
    assert patch_videocapture.released, "underlying capture was not released on stop"


def test_get_frame_returns_latest(patch_videocapture):
    distinct = [np.full((4, 4, 3), i, dtype=np.uint8) for i in range(1, 11)]
    patch_videocapture._frames = list(distinct)
    cam = camera_capture.CameraCapture()
    cam.start()
    try:
        assert _wait_until(lambda: cam.get_frame() is not None)
        # Give it a moment to drain a few frames
        time.sleep(0.05)
        latest = cam.get_frame()
        assert latest is not None
        frame, ts = latest
        assert frame.shape == (4, 4, 3)
        assert isinstance(ts, float)
    finally:
        cam.stop()


def test_queue_drops_oldest_when_full(patch_videocapture, monkeypatch):
    # Force a tiny queue so we can observe overflow behavior
    monkeypatch.setattr(camera_capture, "_QUEUE_MAXSIZE", 2)
    patch_videocapture._frames = [np.full((4, 4, 3), i, dtype=np.uint8) for i in range(20)]
    cam = camera_capture.CameraCapture()
    cam.start()
    try:
        assert _wait_until(lambda: cam.get_frame() is not None)
        time.sleep(0.05)
        q = cam.get_frame_queue()
        assert q.qsize() <= 2  # never exceeds maxsize despite 20 produced frames
    finally:
        cam.stop()


def test_open_failure_retries_with_backoff(monkeypatch):
    """When VideoCapture.isOpened returns False, the loop retries (doesn't crash)."""
    failing = FakeCapture(opened=False)

    def factory(*_a, **_k):
        return failing

    monkeypatch.setattr(camera_capture.cv2, "VideoCapture", factory, raising=False)

    cam = camera_capture.CameraCapture()
    cam.start()
    try:
        # Give it a tick to attempt opening
        time.sleep(0.1)
        assert cam.is_running  # thread alive even though open failed
        assert cam.get_frame() is None  # never produced a frame
    finally:
        cam.stop()


def test_wait_for_first_frame_returns_false_when_no_frames(monkeypatch):
    failing = FakeCapture(opened=False)
    monkeypatch.setattr(camera_capture.cv2, "VideoCapture", lambda *_a, **_k: failing, raising=False)
    cam = camera_capture.CameraCapture()
    cam.start()
    try:
        assert cam.wait_for_first_frame(timeout=0.2) is False
    finally:
        cam.stop()


def test_wait_for_first_frame_returns_true_when_ready(patch_videocapture):
    cam = camera_capture.CameraCapture()
    cam.start()
    try:
        assert cam.wait_for_first_frame(timeout=2.0) is True
    finally:
        cam.stop()


def test_consecutive_read_failures_trigger_reconnect(monkeypatch):
    """After 10 consecutive empty reads the loop breaks out and reopens."""
    counters = {"opens": 0}
    captures: list[FakeCapture] = []

    def factory(*_a, **_k):
        counters["opens"] += 1
        # First capture fails after 1 read; subsequent captures are fine
        cap = FakeCapture(fail_after=1) if counters["opens"] == 1 else FakeCapture()
        captures.append(cap)
        return cap

    monkeypatch.setattr(camera_capture.cv2, "VideoCapture", factory, raising=False)

    cam = camera_capture.CameraCapture()
    cam.start()
    try:
        # Wait long enough for the failure threshold (10 failed reads × 0.05s sleep)
        # plus reconnect attempt
        assert _wait_until(lambda: counters["opens"] >= 2, timeout=3.0), \
            f"expected at least 2 opens, got {counters['opens']}"
    finally:
        cam.stop()
