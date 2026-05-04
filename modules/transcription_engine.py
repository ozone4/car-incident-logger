"""
transcription_engine.py — Local Whisper speech-to-text.

Uses faster-whisper (CTranslate2 backend) for fast, low-memory transcription.
Model is loaded lazily on first use.

Fallback note: if faster-whisper is unavailable, install whisper.cpp and call
  subprocess.run(["whisper-cpp", audio_path, "--model", model_path])
"""

import logging
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class TranscriptionEngine:
    def __init__(
        self,
        model_name: str = "base.en",
        device: str = "cpu",
        compute_type: str = "int8",
        models_dir: Optional[str] = None,
    ):
        self.model_name = model_name
        self.device = device
        self.compute_type = compute_type
        self.models_dir = Path(models_dir) if models_dir else None
        self._model = None

    # ── Public API ────────────────────────────────────────────────────────────

    def transcribe(self, audio_path: str) -> str:
        """
        Transcribe the WAV file at *audio_path* and return the full text.

        The model is loaded on first call (lazy init).
        """
        path = Path(audio_path)
        if not path.exists():
            raise FileNotFoundError(f"Audio file not found: {path}")

        self._ensure_model()

        logger.info("Transcribing %s ...", path.name)
        t0 = time.monotonic()

        segments, info = self._model.transcribe(  # type: ignore[union-attr]
            str(path),
            beam_size=5,
            language="en",
            condition_on_previous_text=False,
        )

        text_parts = []
        for segment in segments:
            text_parts.append(segment.text.strip())

        transcript = " ".join(text_parts).strip()
        elapsed = time.monotonic() - t0

        logger.info(
            "Transcription complete in %.2fs (%.1fx realtime): %r",
            elapsed,
            info.duration / elapsed if elapsed > 0 else 0,
            transcript[:120],
        )
        return transcript

    def warm_up(self) -> None:
        """Pre-load the model so the first real transcription is fast."""
        self._ensure_model()
        logger.info("TranscriptionEngine model warmed up")

    # ── Internal ──────────────────────────────────────────────────────────────

    def _ensure_model(self) -> None:
        if self._model is not None:
            return

        try:
            from faster_whisper import WhisperModel
        except ImportError:
            raise ImportError(
                "faster-whisper is not installed. "
                "Run: pip install faster-whisper"
            )

        download_root = str(self.models_dir) if self.models_dir else None

        logger.info(
            "Loading Whisper model '%s' on %s (%s) ...",
            self.model_name,
            self.device,
            self.compute_type,
        )
        t0 = time.monotonic()
        self._model = WhisperModel(
            self.model_name,
            device=self.device,
            compute_type=self.compute_type,
            download_root=download_root,
        )
        logger.info("Model loaded in %.1fs", time.monotonic() - t0)
