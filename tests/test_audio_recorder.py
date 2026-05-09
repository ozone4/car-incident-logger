"""
test_audio_recorder.py — Unit tests for modules/audio_recorder.py.

sounddevice.InputStream is fully mocked so no real microphone is touched.
"""

from __future__ import annotations

import sys
import types
import wave
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ── sounddevice stub ─────────────────────────────────────────────────────────
# audio_recorder does `import sounddevice as sd` — we install a stub before that.
class _PortAudioError(Exception):
    pass


class _CallbackFlags:  # placeholder to satisfy the type hint at import time
    pass


_sd_stub = types.ModuleType("sounddevice")
_sd_stub.PortAudioError = _PortAudioError
_sd_stub.CallbackFlags = _CallbackFlags
_sd_stub.InputStream = MagicMock()  # replaced per-test
sys.modules.setdefault("sounddevice", _sd_stub)

from modules import audio_recorder  # noqa: E402


class FakeStream:
    """Minimal stand-in for sounddevice.InputStream that fires the callback on demand."""

    def __init__(self, callback=None, **_kwargs):
        self.callback = callback
        self.started = False
        self.stopped = False
        self.closed = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def close(self):
        self.closed = True

    def emit(self, samples: np.ndarray) -> None:
        """Push fake audio chunks into the registered callback."""
        if self.callback:
            self.callback(samples, samples.shape[0], None, None)


@pytest.fixture
def fake_stream(monkeypatch):
    """Patch sounddevice.InputStream so it returns a FakeStream we can drive."""
    holder: dict = {"stream": None}

    def factory(**kwargs):
        s = FakeStream(**kwargs)
        holder["stream"] = s
        return s

    monkeypatch.setattr(audio_recorder.sd, "InputStream", factory, raising=False)
    return holder


def test_start_recording_opens_stream(fake_stream):
    rec = audio_recorder.AudioRecorder(sample_rate=16000, channels=1)
    rec.start_recording()
    assert rec.is_recording is True
    assert fake_stream["stream"].started is True


def test_double_start_recording_is_a_noop(fake_stream):
    rec = audio_recorder.AudioRecorder()
    rec.start_recording()
    first_stream = fake_stream["stream"]
    rec.start_recording()  # should warn + ignore
    # Same stream object — no second start
    assert fake_stream["stream"] is first_stream


def test_callback_accumulates_chunks_and_concatenates(fake_stream):
    rec = audio_recorder.AudioRecorder(sample_rate=16000, channels=1)
    rec.start_recording()
    stream = fake_stream["stream"]

    chunk1 = np.full((1024, 1), 1, dtype=np.int16)
    chunk2 = np.full((512, 1), 2, dtype=np.int16)
    stream.emit(chunk1)
    stream.emit(chunk2)

    audio = rec.stop_recording()
    assert audio is not None
    assert audio.shape == (1024 + 512, 1)
    assert audio.dtype == np.int16
    # First slice came from chunk1, second from chunk2
    assert audio[0, 0] == 1
    assert audio[1024, 0] == 2


def test_stop_without_chunks_returns_none(fake_stream):
    rec = audio_recorder.AudioRecorder()
    rec.start_recording()
    audio = rec.stop_recording()
    assert audio is None


def test_stop_without_start_returns_none(fake_stream):
    rec = audio_recorder.AudioRecorder()
    assert rec.stop_recording() is None


def test_stream_is_stopped_and_closed_on_stop(fake_stream):
    rec = audio_recorder.AudioRecorder()
    rec.start_recording()
    stream = fake_stream["stream"]
    fake_stream["stream"].emit(np.full((100, 1), 3, dtype=np.int16))
    rec.stop_recording()
    assert stream.stopped is True
    assert stream.closed is True


def test_callback_after_stop_does_not_collect(fake_stream):
    rec = audio_recorder.AudioRecorder()
    rec.start_recording()
    stream = fake_stream["stream"]
    stream.emit(np.full((100, 1), 1, dtype=np.int16))
    audio_first = rec.stop_recording()
    assert audio_first is not None and audio_first.shape == (100, 1)

    # After stop, late callback fires (PortAudio can deliver one or two extra)
    stream.emit(np.full((100, 1), 7, dtype=np.int16))
    # Re-start to inspect _chunks state cleanly
    rec.start_recording()
    audio_after_restart = rec.stop_recording()
    # The "late" chunk emitted while not recording must NOT bleed into the new session
    assert audio_after_restart is None


def test_save_wav_round_trip(fake_stream, tmp_path):
    rec = audio_recorder.AudioRecorder(sample_rate=16000, channels=1)
    audio = np.array([100, -200, 300, -400], dtype=np.int16).reshape(-1, 1)
    out_path = tmp_path / "out.wav"
    rec.save_wav(audio, str(out_path))
    assert out_path.exists()

    with wave.open(str(out_path), "rb") as wf:
        assert wf.getnchannels() == 1
        assert wf.getsampwidth() == 2
        assert wf.getframerate() == 16000
        assert wf.getnframes() == 4
        frames = wf.readframes(4)
        decoded = np.frombuffer(frames, dtype=np.int16)
        assert decoded.tolist() == [100, -200, 300, -400]


def test_audio_to_bytes_returns_valid_wav(fake_stream):
    rec = audio_recorder.AudioRecorder(sample_rate=16000, channels=1)
    audio = np.array([1, 2, 3], dtype=np.int16).reshape(-1, 1)
    blob = rec.audio_to_bytes(audio)
    assert blob.startswith(b"RIFF")
    assert b"WAVE" in blob[:12]


def test_portaudio_error_resets_recording_flag(monkeypatch):
    def factory(**_kwargs):
        raise audio_recorder.sd.PortAudioError("no device")

    monkeypatch.setattr(audio_recorder.sd, "InputStream", factory, raising=False)
    rec = audio_recorder.AudioRecorder()
    with pytest.raises(audio_recorder.sd.PortAudioError):
        rec.start_recording()
    assert rec.is_recording is False
