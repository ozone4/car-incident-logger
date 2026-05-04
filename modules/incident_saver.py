"""
incident_saver.py — Saves a video clip, audio recording, transcript, and
metadata for one incident into a structured folder hierarchy.

Folder layout
-------------
  data/plates/{PLATE}/incidents/{ISO_TIMESTAMP}/
      clip.mp4
      audio.wav
      transcript.txt
      metadata.json

  data/unresolved/{ISO_TIMESTAMP}/          ← when plate could not be parsed

Video encoding preference: ffmpeg subprocess (H.264) → cv2.VideoWriter (XVID).
"""

import json
import logging
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

import cv2
import numpy as np

logger = logging.getLogger(__name__)


class IncidentSaver:
    def __init__(self, base_path: str = "./data", fps: int = 30):
        self.base_path = Path(base_path)
        self.fps = fps
        self._ffmpeg_available = shutil.which("ffmpeg") is not None
        if not self._ffmpeg_available:
            logger.warning("ffmpeg not found — falling back to cv2.VideoWriter (XVID)")

    # ── Public API ────────────────────────────────────────────────────────────

    def save_incident(
        self,
        frames: List[Tuple[np.ndarray, float]],
        audio_data: Optional[np.ndarray],
        transcript: str,
        parse_result: dict,
        audio_recorder=None,  # AudioRecorder instance for save_wav()
        extra_tags: Optional[List[str]] = None,
    ) -> Path:
        """
        Persist an incident to disk and return the incident folder path.

        Parameters
        ----------
        frames       : list of (frame_ndarray, monotonic_timestamp)
        audio_data   : int16 numpy array from AudioRecorder, or None
        transcript   : raw transcript string
        parse_result : dict from phonetic_plate_parser.parse_plate_from_transcript()
        audio_recorder : AudioRecorder instance (used to call save_wav)
        extra_tags   : optional list of string tags for metadata

        Returns
        -------
        Path to the incident folder that was created.
        """
        plate = parse_result.get("plate", "").strip().upper()
        timestamp_str = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

        if plate:
            incident_dir = self.base_path / "plates" / plate / "incidents" / timestamp_str
        else:
            incident_dir = self.base_path / "unresolved" / timestamp_str

        incident_dir.mkdir(parents=True, exist_ok=True)
        logger.info("Saving incident to %s", incident_dir)

        clip_path = self._save_clip(frames, incident_dir)
        audio_path = self._save_audio(audio_data, audio_recorder, incident_dir)
        self._save_transcript(transcript, incident_dir)
        self._save_metadata(
            plate=plate,
            timestamp_str=timestamp_str,
            transcript=transcript,
            parse_result=parse_result,
            clip_path=clip_path,
            audio_path=audio_path,
            tags=extra_tags or [],
            incident_dir=incident_dir,
        )

        logger.info("Incident saved: plate=%r  dir=%s", plate or "(unresolved)", incident_dir)
        return incident_dir

    # ── Clip saving ───────────────────────────────────────────────────────────

    def _save_clip(
        self, frames: List[Tuple[np.ndarray, float]], incident_dir: Path
    ) -> Optional[str]:
        if not frames:
            logger.warning("No frames to save for clip")
            return None

        h, w = frames[0][0].shape[:2]

        if self._ffmpeg_available:
            mp4_path = incident_dir / "clip.mp4"
            if self._save_clip_ffmpeg(frames, mp4_path, w, h):
                return str(mp4_path)
            logger.warning("ffmpeg failed — falling back to cv2 (XVID/AVI)")

        avi_path = incident_dir / "clip.avi"
        if self._save_clip_opencv(frames, avi_path, w, h):
            return str(avi_path)
        return None

    def _save_clip_ffmpeg(
        self,
        frames: List[Tuple[np.ndarray, float]],
        out_path: Path,
        w: int,
        h: int,
    ) -> bool:
        """Pipe raw BGR frames into ffmpeg for H.264/mp4 output."""
        try:
            cmd = [
                "ffmpeg",
                "-y",
                "-f", "rawvideo",
                "-vcodec", "rawvideo",
                "-s", f"{w}x{h}",
                "-pix_fmt", "bgr24",
                "-r", str(self.fps),
                "-i", "pipe:0",
                "-c:v", "libx264",
                "-preset", "ultrafast",
                "-crf", "23",
                "-pix_fmt", "yuv420p",
                str(out_path),
            ]
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            for frame, _ in frames:
                proc.stdin.write(frame.astype(np.uint8).tobytes())  # type: ignore[union-attr]
            proc.stdin.close()  # type: ignore[union-attr]
            proc.wait(timeout=60)

            if proc.returncode != 0:
                stderr = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
                logger.error("ffmpeg exited with %d: %s", proc.returncode, stderr[-500:])
                return False

            logger.debug("Clip saved via ffmpeg: %s (%d frames)", out_path, len(frames))
            return True

        except Exception as e:
            logger.error("ffmpeg clip save failed: %s", e)
            return False

    def _save_clip_opencv(
        self,
        frames: List[Tuple[np.ndarray, float]],
        out_path: Path,
        w: int,
        h: int,
    ) -> bool:
        # cv2.VideoWriter prefers AVI/XVID for reliable cross-platform support
        avi_path = out_path.with_suffix(".avi")
        fourcc = cv2.VideoWriter_fourcc(*"XVID")
        writer = cv2.VideoWriter(str(avi_path), fourcc, self.fps, (w, h))
        if not writer.isOpened():
            logger.error("cv2.VideoWriter failed to open %s", avi_path)
            return False

        for frame, _ in frames:
            writer.write(frame)
        writer.release()
        logger.debug("Clip saved via cv2 (XVID): %s (%d frames)", avi_path, len(frames))
        return True

    # ── Audio saving ──────────────────────────────────────────────────────────

    def _save_audio(
        self,
        audio_data: Optional[np.ndarray],
        audio_recorder,
        incident_dir: Path,
    ) -> Optional[str]:
        if audio_data is None or audio_recorder is None:
            return None
        try:
            audio_path = incident_dir / "audio.wav"
            audio_recorder.save_wav(audio_data, str(audio_path))
            return str(audio_path)
        except Exception as e:
            logger.error("Failed to save audio: %s", e)
            return None

    # ── Text / metadata ───────────────────────────────────────────────────────

    def _save_transcript(self, transcript: str, incident_dir: Path) -> None:
        txt_path = incident_dir / "transcript.txt"
        txt_path.write_text(transcript, encoding="utf-8")

    def _save_metadata(
        self,
        plate: str,
        timestamp_str: str,
        transcript: str,
        parse_result: dict,
        clip_path: Optional[str],
        audio_path: Optional[str],
        tags: List[str],
        incident_dir: Path,
    ) -> None:
        metadata = {
            "plate": plate,
            "timestamp": timestamp_str,
            "transcript": transcript,
            "parsed_note": parse_result.get("note", ""),
            "raw_plate_spoken": parse_result.get("raw_plate_spoken", ""),
            "confidence": parse_result.get("confidence", 0.0),
            "clip_path": clip_path,
            "audio_path": audio_path,
            "matched_existing_plate": False,  # updated by orchestrator after DB check
            "tags": tags,
        }
        meta_path = incident_dir / "metadata.json"
        meta_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        logger.debug("Metadata written to %s", meta_path)

    def update_matched_flag(self, incident_dir: Path, matched: bool) -> None:
        """Update the matched_existing_plate flag after DB lookup."""
        meta_path = incident_dir / "metadata.json"
        if not meta_path.exists():
            return
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
            data["matched_existing_plate"] = matched
            meta_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception as e:
            logger.warning("Could not update metadata flag: %s", e)
