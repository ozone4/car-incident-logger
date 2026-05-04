"""
audio_recorder.py — Records microphone audio while the button is held.

Uses sounddevice for low-latency capture.  Accumulates chunks into a list
and flushes to a WAV file on stop_recording().
"""

import io
import logging
import threading
import wave
from pathlib import Path
from typing import List, Optional

import numpy as np
import sounddevice as sd

logger = logging.getLogger(__name__)


class AudioRecorder:
    def __init__(
        self,
        device_index: Optional[int] = None,
        sample_rate: int = 16000,
        channels: int = 1,
    ):
        self.device_index = device_index
        self.sample_rate = sample_rate
        self.channels = channels

        self._chunks: List[np.ndarray] = []
        self._stream: Optional[sd.InputStream] = None
        self._recording = False
        self._lock = threading.Lock()

    # ── Public API ────────────────────────────────────────────────────────────

    def start_recording(self) -> None:
        """Begin capturing audio.  Safe to call from any thread."""
        with self._lock:
            if self._recording:
                logger.warning("start_recording() called while already recording — ignored")
                return
            self._chunks = []
            self._recording = True

        try:
            self._stream = sd.InputStream(
                samplerate=self.sample_rate,
                channels=self.channels,
                dtype="int16",
                device=self.device_index,
                callback=self._audio_callback,
                blocksize=1024,
            )
            self._stream.start()
            logger.info(
                "Audio recording started (device=%s, rate=%d, ch=%d)",
                self.device_index,
                self.sample_rate,
                self.channels,
            )
        except sd.PortAudioError as e:
            with self._lock:
                self._recording = False
            logger.error("Failed to open audio device: %s", e)
            raise

    def stop_recording(self) -> Optional[np.ndarray]:
        """
        Stop capturing and return the concatenated int16 numpy array.
        Returns None if no audio was captured or recording was never started.
        """
        with self._lock:
            if not self._recording:
                return None
            self._recording = False

        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception as e:
                logger.warning("Error closing audio stream: %s", e)
            finally:
                self._stream = None

        with self._lock:
            chunks = list(self._chunks)

        if not chunks:
            logger.warning("stop_recording(): no audio chunks captured")
            return None

        audio = np.concatenate(chunks, axis=0)
        duration = audio.shape[0] / self.sample_rate
        logger.info("Audio recording stopped: %.2f seconds captured", duration)
        return audio

    def save_wav(self, audio: np.ndarray, path: str) -> None:
        """Write a numpy int16 array to a WAV file at *path*."""
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)

        # Ensure int16
        if audio.dtype != np.int16:
            audio = (audio * 32767).astype(np.int16)

        # Flatten to 1-D if channels == 1
        flat = audio.flatten() if self.channels == 1 else audio

        with wave.open(str(out), "wb") as wf:
            wf.setnchannels(self.channels)
            wf.setsampwidth(2)  # 16-bit = 2 bytes
            wf.setframerate(self.sample_rate)
            wf.writeframes(flat.tobytes())

        logger.info("Audio saved to %s", out)

    def audio_to_bytes(self, audio: np.ndarray) -> bytes:
        """Encode a numpy int16 array as in-memory WAV bytes."""
        buf = io.BytesIO()
        if audio.dtype != np.int16:
            audio = (audio * 32767).astype(np.int16)
        flat = audio.flatten() if self.channels == 1 else audio

        with wave.open(buf, "wb") as wf:
            wf.setnchannels(self.channels)
            wf.setsampwidth(2)
            wf.setframerate(self.sample_rate)
            wf.writeframes(flat.tobytes())

        return buf.getvalue()

    @property
    def is_recording(self) -> bool:
        with self._lock:
            return self._recording

    # ── Internal ──────────────────────────────────────────────────────────────

    def _audio_callback(
        self,
        indata: np.ndarray,
        frames: int,
        time_info,
        status: sd.CallbackFlags,
    ) -> None:
        if status:
            logger.debug("Audio callback status: %s", status)
        with self._lock:
            if self._recording:
                self._chunks.append(indata.copy())
