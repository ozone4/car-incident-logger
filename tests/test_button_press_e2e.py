"""
test_button_press_e2e.py — End-to-end integration test of the button-press flow.

Exercises: rolling buffer → audio → transcription → phonetic parser → IncidentSaver
→ PlateDatabase. Hardware (camera, mic, ffmpeg, Whisper) is faked so the test
runs anywhere, but the wiring between the *real* IncidentProcessor, IncidentSaver,
phonetic parser, and PlateDatabase is exercised end-to-end.
"""

from __future__ import annotations

import sys
import time
import types
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ── cv2 stub: enough surface for IncidentSaver's OpenCV fallback ──────────────
def _install_cv2_stub() -> None:
    if "cv2" in sys.modules and isinstance(sys.modules["cv2"], types.ModuleType):
        cv2 = sys.modules["cv2"]
    else:
        cv2 = types.ModuleType("cv2")
        sys.modules["cv2"] = cv2

    class _FakeVideoWriter:
        def __init__(self, path, *_a, **_kw):
            self.path = path
            self._opened = True
            # Touch the file so the saver sees it exists
            Path(path).touch()

        def isOpened(self):
            return self._opened

        def write(self, _frame):
            pass

        def release(self):
            self._opened = False

    cv2.VideoWriter = _FakeVideoWriter
    cv2.VideoWriter_fourcc = MagicMock(return_value=0)
    cv2.imencode = MagicMock(return_value=(True, MagicMock(tobytes=lambda: b"")))
    cv2.resize = MagicMock(side_effect=lambda f, *a, **kw: f)
    cv2.IMWRITE_JPEG_QUALITY = 1


_install_cv2_stub()

# ── sounddevice stub: AudioRecorder imports it at module load ────────────────
if "sounddevice" not in sys.modules:
    _sd = types.ModuleType("sounddevice")
    _sd.PortAudioError = type("PortAudioError", (Exception,), {})
    _sd.CallbackFlags = type("CallbackFlags", (), {})
    _sd.InputStream = MagicMock()
    sys.modules["sounddevice"] = _sd


# Now the project imports are safe
from main import IncidentProcessor  # noqa: E402
from modules.audio_recorder import AudioRecorder  # noqa: E402
from modules.incident_saver import IncidentSaver  # noqa: E402
from modules.notifier import Notifier  # noqa: E402
from modules.plate_database import PlateDatabase  # noqa: E402


# ── Fakes ─────────────────────────────────────────────────────────────────────
class _FakeConfig:
    """Minimal config surface that IncidentProcessor reads."""

    def __init__(self, buffer_duration: int = 3):
        self.buffer_duration = buffer_duration


class _FakeRollingBuffer:
    """Returns a fixed list of (frame, ts) tuples for any get_clip call."""

    def __init__(self, n_frames: int = 10):
        self._frames = [
            (np.zeros((4, 4, 3), dtype=np.uint8), float(i)) for i in range(n_frames)
        ]

    def get_clip(self, seconds_back: float):
        return list(self._frames)


class _FakeTranscription:
    """Stand-in for TranscriptionEngine — returns a pre-set transcript."""

    def __init__(self, transcript: str):
        self._transcript = transcript

    def transcribe(self, _audio_path: str) -> str:
        return self._transcript


def _wait_until(predicate, timeout: float = 3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


# ── Tests ────────────────────────────────────────────────────────────────────
def test_button_press_to_db_full_flow(tmp_path):
    """Press → fake audio → fake transcription → real parser → real saver → DB row."""
    cfg = _FakeConfig(buffer_duration=3)
    rolling_buf = _FakeRollingBuffer(n_frames=10)
    audio_rec = AudioRecorder(sample_rate=16000, channels=1)
    transcription = _FakeTranscription("whiskey juliet one eight four three")
    saver = IncidentSaver(base_path=str(tmp_path), fps=30)
    db = PlateDatabase(db_path=str(tmp_path / "plates.db"))
    notifier = Notifier(console_alerts=False)

    processor = IncidentProcessor(
        config=cfg,
        rolling_buffer=rolling_buf,
        audio_recorder=audio_rec,
        transcription_engine=transcription,
        incident_saver=saver,
        plate_database=db,
        notifier=notifier,
    )

    # 100ms of int16 audio at 16kHz
    audio_data = np.zeros((1600, 1), dtype=np.int16)
    processor.process(audio_data)

    # Wait for the daemon thread to finish writing
    expected_dir = tmp_path / "plates" / "WJ1843" / "incidents"
    assert _wait_until(lambda: expected_dir.exists() and any(expected_dir.iterdir())), \
        "incident folder was never written"

    # The incident folder should contain a clip + audio + transcript + metadata
    incident = next(expected_dir.iterdir())
    assert (incident / "transcript.txt").exists()
    assert (incident / "audio.wav").exists()
    assert (incident / "metadata.json").exists()
    # Clip file is created by FakeVideoWriter (extension may be .mp4 or .avi
    # depending on whether ffmpeg is on PATH in this environment)
    assert any(p.suffix in {".mp4", ".avi"} for p in incident.iterdir()), \
        f"no clip file in {list(incident.iterdir())}"

    # DB row landed with the parsed plate
    incidents = db.get_incidents_for_plate("WJ1843")
    assert len(incidents) == 1
    assert incidents[0]["plate_id"] is not None


def test_unresolved_when_transcript_has_no_plate(tmp_path):
    """A non-phonetic transcript still produces an incident, but in unresolved/."""
    cfg = _FakeConfig(buffer_duration=3)
    rolling_buf = _FakeRollingBuffer(n_frames=5)
    audio_rec = AudioRecorder(sample_rate=16000, channels=1)
    transcription = _FakeTranscription("just some random talking with no plate")
    saver = IncidentSaver(base_path=str(tmp_path), fps=30)
    db = PlateDatabase(db_path=str(tmp_path / "plates.db"))
    notifier = Notifier(console_alerts=False)

    processor = IncidentProcessor(
        config=cfg, rolling_buffer=rolling_buf, audio_recorder=audio_rec,
        transcription_engine=transcription, incident_saver=saver,
        plate_database=db, notifier=notifier,
    )

    processor.process(np.zeros((800, 1), dtype=np.int16))

    unresolved = tmp_path / "unresolved"
    assert _wait_until(lambda: unresolved.exists() and any(unresolved.iterdir())), \
        "unresolved folder was never written"

    # No plate → no DB row in incidents
    assert db.all_plates() == []


def test_known_plate_triggers_alert_path(tmp_path, caplog):
    """When the parsed plate already exists in DB, the known-plate alert path fires."""
    cfg = _FakeConfig(buffer_duration=3)
    rolling_buf = _FakeRollingBuffer(n_frames=5)
    audio_rec = AudioRecorder(sample_rate=16000, channels=1)
    transcription = _FakeTranscription("alpha bravo charlie one two three")
    saver = IncidentSaver(base_path=str(tmp_path), fps=30)
    db = PlateDatabase(db_path=str(tmp_path / "plates.db"))
    notifier = Notifier(console_alerts=False)

    # Pre-seed the plate so the alert path runs
    db.add_incident("ABC123", {"timestamp": "20200101T000000Z", "transcript": "seed"})

    processor = IncidentProcessor(
        config=cfg, rolling_buffer=rolling_buf, audio_recorder=audio_rec,
        transcription_engine=transcription, incident_saver=saver,
        plate_database=db, notifier=notifier,
    )

    import logging
    with caplog.at_level(logging.WARNING, logger="modules.notifier"):
        processor.process(np.zeros((800, 1), dtype=np.int16))
        assert _wait_until(lambda: any(
            "Known plate detected" in r.message for r in caplog.records
        )), "known-plate alert was never logged"

    # incident_count incremented from 1 (seed) → 2 (the new run)
    plate_row = db.get_plate("ABC123")
    assert plate_row["incident_count"] == 2
