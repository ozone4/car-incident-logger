"""
recording_recovery.py — Startup recovery for incomplete recording segments.

Scans the recording output path for:
  - _INPROGRESS.mp4 files (segments that were being written when the app crashed)
  - .json.tmp files (sidecar metadata that wasn't finalized)
  - Sidecar JSON with "complete": false (segments not fully finalized)

Recovery actions:
  - If video has frames (non-empty): finalize with metadata marking it recovered.
  - If video is empty/invalid: move to a corrupt folder.
  - Temp metadata files: remove (the video rename is the source of truth).
"""

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Union

logger = logging.getLogger(__name__)


class RecoveryResult:
    """Summary of a single recovered or cleaned artifact."""

    def __init__(self, path: str, action: str, detail: str = ""):
        self.path = path
        self.action = action  # "finalized", "moved_corrupt", "cleaned"
        self.detail = detail

    def to_dict(self) -> dict:
        return {"path": self.path, "action": self.action, "detail": self.detail}


def recover_recordings(
    recording_path: Union[str, Path],
    min_valid_size_bytes: int = 1024,
) -> dict:
    """Scan recording_path for incomplete artifacts and recover/clean them.

    Args:
        recording_path: Base path where recordings are stored.
        min_valid_size_bytes: Minimum file size to consider a video recoverable
            (smaller files are treated as empty/corrupt).

    Returns:
        Dict with keys: recovered, corrupt, cleaned, errors, summary.
    """
    recording_path = Path(recording_path)
    if not recording_path.exists():
        return _empty_result("Recording path does not exist")

    results: List[RecoveryResult] = []
    errors: List[str] = []

    # Phase 1: Handle _INPROGRESS video files
    for inprogress in recording_path.rglob("*_INPROGRESS.mp4"):
        try:
            result = _recover_inprogress_video(inprogress, min_valid_size_bytes)
            results.append(result)
        except Exception as exc:
            errors.append(f"{inprogress}: {exc}")
            logger.warning("Recovery error for %s: %s", inprogress, exc)

    # Phase 2: Handle orphaned .json.tmp files
    for tmp_meta in recording_path.rglob("*.json.tmp"):
        try:
            tmp_meta.unlink()
            results.append(RecoveryResult(str(tmp_meta), "cleaned", "orphaned temp metadata"))
        except OSError as exc:
            errors.append(f"{tmp_meta}: {exc}")

    # Phase 3: Handle sidecars marked incomplete (complete: false)
    for json_path in recording_path.rglob("*.json"):
        if json_path.suffix == ".tmp":
            continue
        try:
            meta = json.loads(json_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if meta.get("complete") is False:
            # Find associated video
            video_path = _find_video_for_json(json_path)
            if video_path and video_path.exists() and video_path.stat().st_size >= min_valid_size_bytes:
                meta["complete"] = True
                meta["recovered"] = True
                meta["recovery_time"] = datetime.now(timezone.utc).isoformat()
                json_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
                results.append(RecoveryResult(str(json_path), "finalized", "marked complete after recovery"))
            elif video_path and video_path.exists():
                _move_to_corrupt(video_path, recording_path)
                json_path.unlink(missing_ok=True)
                results.append(RecoveryResult(str(video_path), "moved_corrupt", "too small to recover"))
            else:
                # Orphaned sidecar with no video
                json_path.unlink(missing_ok=True)
                results.append(RecoveryResult(str(json_path), "cleaned", "orphaned sidecar, no video"))

    recovered = [r for r in results if r.action == "finalized"]
    corrupt = [r for r in results if r.action == "moved_corrupt"]
    cleaned = [r for r in results if r.action == "cleaned"]

    summary = (
        f"Recovery complete: {len(recovered)} finalized, "
        f"{len(corrupt)} corrupt, {len(cleaned)} cleaned"
    )
    if recovered or corrupt or cleaned:
        logger.info(summary)
    else:
        logger.debug("Recording recovery: no incomplete artifacts found")

    return {
        "recovered": [r.to_dict() for r in recovered],
        "corrupt": [r.to_dict() for r in corrupt],
        "cleaned": [r.to_dict() for r in cleaned],
        "errors": errors,
        "summary": summary,
        "timestamp": time.time(),
    }


def _recover_inprogress_video(
    inprogress_path: Path, min_valid_size_bytes: int
) -> RecoveryResult:
    """Recover or clean up an _INPROGRESS video file."""
    size = inprogress_path.stat().st_size

    if size < min_valid_size_bytes:
        # Too small — likely empty or just a header
        _move_to_corrupt(inprogress_path, _find_recording_root(inprogress_path))
        return RecoveryResult(str(inprogress_path), "moved_corrupt", f"too small ({size} bytes)")

    # Rename to final path (strip _INPROGRESS)
    stem = inprogress_path.stem.replace("_INPROGRESS", "")
    final_path = inprogress_path.with_name(f"{stem}.mp4")

    # Avoid overwriting an existing finalized segment
    if final_path.exists():
        # Both exist — keep the larger one
        if final_path.stat().st_size >= size:
            inprogress_path.unlink()
            return RecoveryResult(str(inprogress_path), "cleaned", "final already exists and is larger")
        else:
            final_path.unlink()

    inprogress_path.rename(final_path)

    # Write recovery sidecar metadata
    meta = {
        "start_time": _infer_start_time(final_path),
        "end_time": datetime.now(timezone.utc).isoformat(),
        "duration_seconds": 0,  # unknown — interrupted
        "frame_count": 0,  # unknown
        "file_path": str(final_path),
        "locked": False,
        "complete": True,
        "recovered": True,
        "recovery_time": datetime.now(timezone.utc).isoformat(),
    }
    meta_path = final_path.with_suffix(".json")
    if not meta_path.exists():
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    return RecoveryResult(str(final_path), "finalized", f"recovered {size} bytes")


def _move_to_corrupt(file_path: Path, recording_root: Path) -> Path:
    """Move a file to the corrupt subfolder for manual inspection."""
    corrupt_dir = recording_root / "_corrupt"
    corrupt_dir.mkdir(parents=True, exist_ok=True)
    dest = corrupt_dir / file_path.name
    # Avoid name collision
    if dest.exists():
        dest = corrupt_dir / f"{file_path.stem}_{int(time.time())}{file_path.suffix}"
    file_path.rename(dest)
    return dest


def _find_recording_root(path: Path) -> Path:
    """Walk up from a file to find the recording root (parent of date dirs)."""
    # Recording structure: recording_path/YYYY-MM-DD/HH-MM-SS.mp4
    # So parent.parent of a video file is the recording root
    parent = path.parent
    # If parent looks like a date dir (YYYY-MM-DD pattern), go one more up
    if len(parent.name) == 10 and parent.name[4] == "-":
        return parent.parent
    return parent


def _find_video_for_json(json_path: Path) -> "Optional[Path]":
    """Find video file matching a sidecar JSON."""
    for ext in (".mp4", ".avi", ".mkv"):
        candidate = json_path.with_suffix(ext)
        if candidate.exists():
            return candidate
    # Also check _INPROGRESS variant
    stem = json_path.stem
    inprogress = json_path.with_name(f"{stem}_INPROGRESS.mp4")
    if inprogress.exists():
        return inprogress
    return None


def _infer_start_time(video_path: Path) -> str:
    """Infer start time from the filename (HH-MM-SS) and parent dir (YYYY-MM-DD)."""
    try:
        date_str = video_path.parent.name  # e.g. "2026-05-04"
        time_str = video_path.stem  # e.g. "14-30-00"
        dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H-%M-%S")
        return dt.replace(tzinfo=timezone.utc).isoformat()
    except (ValueError, AttributeError):
        return datetime.now(timezone.utc).isoformat()


def _empty_result(reason: str) -> dict:
    return {
        "recovered": [],
        "corrupt": [],
        "cleaned": [],
        "errors": [],
        "summary": reason,
        "timestamp": time.time(),
    }
