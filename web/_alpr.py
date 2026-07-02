"""
_alpr.py — Live ALPR scanning, sighting state, and snapshot persistence.

This module owns:
  - The background scan thread (live loop)
  - Active + recent sighting state (lock-protected)
  - The peak-confidence "best" detection
  - Plate-crop JPEG persistence under data/sightings/<PLATE>/

The web layer (web/app.py) injects camera + db + gps + config via configure().
Routes interact with this module only via the public functions defined here —
they never touch internal state directly.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

import cv2
import yaml

from modules.alpr_runner import ALPRRunner, _preprocess_crop
from modules.config_manager import ConfigManager
from modules.gps_reader import GPSReader
from modules.multi_frame_voter import MultiFrameVoter
from modules.plate_database import PlateDatabase

logger = logging.getLogger(__name__)


# ── State ────────────────────────────────────────────────────────────────────
_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()
_lock = threading.Lock()
_state: dict = {
    "running": False,
    "ready": False,
    "mode": "unavailable",
    "frames_scanned": 0,
    "detections_seen": 0,
    "latest": None,
    "best": None,
    "active_sightings": {},
    "recent_sightings": [],
    "sightings": [],
    "error": None,
}

# Tunables — overridden from config.yaml at every loop start (_refresh_tuning)
SIGHTING_ACTIVE_TIMEOUT_SECONDS = 5.0
SIGHTING_HISTORY_LIMIT = 30
MAX_CONSECUTIVE_FAILURES = 5
MIN_SIGHTINGS_TO_PERSIST = 2


# ── Injected dependencies (set via configure()) ──────────────────────────────
class _Ctx:
    """Holds callables wired by configure() — defaults are safe no-ops."""

    with_camera: Callable = staticmethod(lambda: contextlib.nullcontext(None))
    get_camera_status: Callable[[], dict] = staticmethod(lambda: {"running": False})
    start_camera: Callable[[], dict] = staticmethod(lambda: {})
    get_db: Callable[[], Optional[PlateDatabase]] = staticmethod(lambda: None)
    get_gps_reader: Callable[[], Optional[GPSReader]] = staticmethod(lambda: None)
    load_config: Callable[[], ConfigManager] = staticmethod(lambda: None)  # type: ignore
    config_path: Callable[[], Path] = staticmethod(lambda: Path("config.yaml"))


def configure(
    *,
    with_camera: Callable,
    get_camera_status: Callable[[], dict],
    start_camera: Callable[[], dict],
    get_db: Callable[[], Optional[PlateDatabase]],
    get_gps_reader: Callable[[], Optional[GPSReader]],
    load_config: Callable[[], ConfigManager],
    config_path: Callable[[], Path],
) -> None:
    """Wire ALPR module to the rest of the app.  Called once at startup.

    `with_camera` must be a context manager that yields the current camera
    instance (or None) while holding the camera lock.
    """
    _Ctx.with_camera = staticmethod(with_camera)
    _Ctx.get_camera_status = staticmethod(get_camera_status)
    _Ctx.start_camera = staticmethod(start_camera)
    _Ctx.get_db = staticmethod(get_db)
    _Ctx.get_gps_reader = staticmethod(get_gps_reader)
    _Ctx.load_config = staticmethod(load_config)
    _Ctx.config_path = staticmethod(config_path)


# ── Public API ───────────────────────────────────────────────────────────────
def signal_stop() -> None:
    """Signal the loop to exit. Non-blocking."""
    _stop_event.set()


def set_state(**updates: Any) -> None:
    with _lock:
        _state.update(updates)
        _state["updated_at"] = time.time()


def get_state_snapshot() -> dict:
    """Cheap, lock-protected shallow copy of the ALPR state dict."""
    with _lock:
        return dict(_state)


def get_status() -> dict:
    """Same as get_state_snapshot but also refreshes the serialized sighting list."""
    with _lock:
        now = time.time()
        _active, _recent, sightings = _update_plate_sightings([], now)
        _state["sightings"] = sightings
        return dict(_state)


def alpr_runner_config() -> dict:
    """Build the runtime config dict for ALPRRunner from ConfigManager."""
    cfg = _Ctx.load_config()
    return {
        "confidence_threshold": cfg.alpr_confidence_threshold,
        "yolo_confidence_threshold": cfg.alpr_yolo_confidence_threshold,
        "yolo_imgsz": cfg.alpr_yolo_imgsz,
        "models_dir": cfg.alpr_models_dir,
        "yolo_model_path": cfg.alpr_yolo_model_path,
        "vehicle_detection_enabled": cfg.alpr_vehicle_detection_enabled,
        "vehicle_model_path": cfg.alpr_vehicle_model_path,
        "vehicle_confidence_threshold": cfg.alpr_vehicle_confidence_threshold,
        "vehicle_fallback_to_fullframe": cfg.alpr_vehicle_fallback_to_fullframe,
        "vehicle_imgsz": cfg.alpr_vehicle_imgsz,
        "ocr_fallback_when_no_detections": cfg.alpr_ocr_fallback_when_no_detections,
        "fullframe_ocr_confidence_threshold": cfg.alpr_fullframe_ocr_confidence_threshold,
        "min_plate_length": cfg.alpr_min_plate_length,
    }


def start() -> dict:
    """Start the live ALPR thread (auto-starts the camera if needed)."""
    global _thread
    with _lock:
        if _state.get("running"):
            return {"status": "already_running", **_state}

    if not _Ctx.get_camera_status().get("running"):
        _Ctx.start_camera()

    _stop_event.clear()
    set_state(
        running=True,
        ready=False,
        mode="initializing",
        frames_scanned=0,
        detections_seen=0,
        latest=None,
        best=None,
        active_sightings={},
        recent_sightings=[],
        sightings=[],
        error="Initializing ALPR engines",
    )
    _thread = threading.Thread(target=_live_loop, daemon=True, name="LiveALPR")
    _thread.start()
    return {"status": "started", **get_status()}


def stop() -> dict:
    _stop_event.set()
    t = _thread
    if t and t.is_alive():
        t.join(timeout=2.0)
    set_state(running=False)
    return {"status": "stopped", **get_status()}


# ── Internal: tuning, helpers, snapshots ────────────────────────────────────
def _refresh_tuning() -> None:
    global SIGHTING_ACTIVE_TIMEOUT_SECONDS, SIGHTING_HISTORY_LIMIT, MAX_CONSECUTIVE_FAILURES, MIN_SIGHTINGS_TO_PERSIST
    try:
        cfg = _Ctx.load_config()
        SIGHTING_ACTIVE_TIMEOUT_SECONDS = float(cfg.alpr_sighting_active_timeout)
        SIGHTING_HISTORY_LIMIT = int(cfg.alpr_sighting_history_limit)
        MAX_CONSECUTIVE_FAILURES = int(cfg.alpr_max_consecutive_failures)
        MIN_SIGHTINGS_TO_PERSIST = int(cfg.alpr_min_sightings_to_persist)
    except Exception as exc:
        logger.warning("Could not read ALPR tuning from config (using defaults): %s", exc)


def _format_elapsed(seconds: float) -> str:
    if seconds < 1:
        return "now"
    if seconds < 60:
        return f"{int(seconds)}s ago"
    return f"{int(seconds // 60)}m ago"


def _save_plate_crop(frame, bbox: list, plate: str) -> Optional[str]:
    """Save a 640px-wide JPEG of the plate crop. Returns the saved path or None."""
    try:
        if bbox is None or frame is None:
            return None
        x1, y1, x2, y2 = bbox
        crop = _preprocess_crop(frame, x1, y1, x2, y2, pad_ratio=0.18, for_ocr=False)
        cfg = _Ctx.load_config()
        out_dir = Path(cfg.storage_base_path) / "sightings" / plate.upper()
        out_dir.mkdir(parents=True, exist_ok=True)
        ts_ms = int(time.time() * 1000)
        path = out_dir / f"{ts_ms}.jpg"
        cv2.imwrite(str(path), crop, [cv2.IMWRITE_JPEG_QUALITY, 90])
        logger.debug("Saved plate crop: %s", path)
        return str(path)
    except Exception as exc:
        logger.warning("Could not save plate crop for %s: %s", plate, exc)
        return None


def _new_sighting(plate: str, det: dict, now: float) -> dict:
    history = None
    db = _Ctx.get_db()
    if db is not None:
        try:
            history = db.get_plate_history(plate)
        except Exception as exc:
            logger.debug("Could not fetch plate history for %s: %s", plate, exc)

    return {
        "id": f"{plate}-{int(now * 1000)}",
        "plate": plate,
        "raw_text": (det.get("raw_text") or "").strip(),
        "confidence": float(det.get("confidence", 0.0)),
        "best_confidence": float(det.get("confidence", 0.0)),
        "bbox": det.get("bbox"),
        "frame_w": det.get("frame_w"),
        "frame_h": det.get("frame_h"),
        "source": det.get("source"),
        "first_seen": now,
        "last_seen": now,
        "seen_count": 1,
        "active": True,
        "history": history,
        "snapshot_path": None,
        "_needs_snapshot": MIN_SIGHTINGS_TO_PERSIST <= 1,
    }


def _serialize_sighting(sighting: dict, now: float) -> dict:
    first_seen = float(sighting.get("first_seen", now))
    last_seen = float(sighting.get("last_seen", now))
    active = bool(sighting.get("active", False))
    return {
        "id": sighting.get("id"),
        "plate": sighting.get("plate"),
        "raw_text": sighting.get("raw_text"),
        "confidence": round(float(sighting.get("confidence", 0.0)), 3),
        "best_confidence": round(float(sighting.get("best_confidence", 0.0)), 3),
        "bbox": sighting.get("bbox"),
        "frame_w": sighting.get("frame_w"),
        "frame_h": sighting.get("frame_h"),
        "source": sighting.get("source"),
        "first_seen": first_seen,
        "last_seen": last_seen,
        "seen_count": int(sighting.get("seen_count", 0)),
        "active": active,
        "status": "visible" if active else "gone",
        "age_seconds": round(now - first_seen, 1),
        "last_seen_seconds_ago": round(now - last_seen, 1),
        "last_seen_label": _format_elapsed(now - last_seen),
        "history": sighting.get("history"),
        "snapshot_path": sighting.get("snapshot_path"),
    }


def _update_plate_sightings(detections: list[dict], now: float):
    """Update active/recent plate sightings and return serialized display rows.

    Caller MUST hold _lock.
    """
    active: dict = _state.setdefault("active_sightings", {})
    recent: list = _state.setdefault("recent_sightings", [])
    by_plate: dict[str, dict] = {}

    for det in detections:
        plate = det.get("plate")
        if not plate:
            continue
        previous = by_plate.get(plate)
        if previous is None or float(det.get("confidence", 0.0)) > float(previous.get("confidence", 0.0)):
            by_plate[plate] = det

    for plate, det in by_plate.items():
        sighting = active.get(plate)
        if sighting is None:
            sighting = _new_sighting(plate, det, now)
            active[plate] = sighting
        else:
            sighting["last_seen"] = now
            sighting["seen_count"] = int(sighting.get("seen_count", 0)) + 1
            sighting["confidence"] = float(det.get("confidence", sighting.get("confidence", 0.0)))
            new_best = max(
                float(sighting.get("best_confidence", 0.0)),
                float(det.get("confidence", 0.0)),
            )
            if new_best > float(sighting.get("best_confidence", 0.0)):
                sighting["best_confidence"] = new_best
                if int(sighting.get("seen_count", 0)) >= MIN_SIGHTINGS_TO_PERSIST:
                    sighting["_needs_snapshot"] = True
            elif int(sighting.get("seen_count", 0)) == MIN_SIGHTINGS_TO_PERSIST:
                sighting["_needs_snapshot"] = True
            sighting["raw_text"] = (det.get("raw_text") or sighting.get("raw_text") or "").strip()
            sighting["bbox"] = det.get("bbox") or sighting.get("bbox")
            if det.get("frame_w"):
                sighting["frame_w"] = det["frame_w"]
                sighting["frame_h"] = det["frame_h"]
            sighting["source"] = det.get("source") or sighting.get("source")
            sighting["active"] = True

    expired = []
    for plate, sighting in list(active.items()):
        if now - float(sighting.get("last_seen", now)) > SIGHTING_ACTIVE_TIMEOUT_SECONDS:
            sighting["active"] = False
            expired.append(active.pop(plate))

    if expired:
        recent[:0] = expired
        del recent[SIGHTING_HISTORY_LIMIT:]

    active_rows = sorted(active.values(), key=lambda x: x.get("last_seen", 0), reverse=True)
    history_rows = active_rows + recent[:SIGHTING_HISTORY_LIMIT]
    serialized = [_serialize_sighting(row, now) for row in history_rows]
    return active, recent, serialized


# ── Background loop ──────────────────────────────────────────────────────────
def _live_loop() -> None:
    """Background scanner: sample latest preview frame, run ALPR, vote over time."""
    _refresh_tuning()
    runner = ALPRRunner(alpr_runner_config())
    ready = runner.initialize()
    status = runner.status_info()
    set_state(
        running=True,
        ready=ready,
        mode=status.get("mode", "unavailable"),
        engine=status,
        frames_scanned=0,
        detections_seen=0,
        latest=None,
        best=None,
        active_sightings={},
        recent_sightings=[],
        sightings=[],
        error=None if ready else "ALPR engines are not ready",
    )

    if not ready:
        set_state(running=False)
        return

    cfg = _Ctx.load_config()
    scan_interval = max(0.0, float(cfg.alpr_scan_interval))
    voter = MultiFrameVoter(min_votes=1)

    # Use the module-level GPS reader (started at startup); fall back to a
    # local reader if it's not initialised yet.
    gps = _Ctx.get_gps_reader()
    if gps is None:
        try:
            raw_cfg = yaml.safe_load(_Ctx.config_path().read_text()) or {}
            gps_cfg = raw_cfg.get("gps", {})
        except Exception:
            gps_cfg = {}
        gps = GPSReader(gps_cfg)

    db = _Ctx.get_db()
    consecutive_failures = 0

    while not _stop_event.is_set():
        with _Ctx.with_camera() as cam:
            if cam is None or not cam.is_running:
                set_state(error="Camera is not running")
                _stop_event.wait(0.5)
                continue
            result = cam.get_frame()

        if result is None:
            set_state(error="Waiting for first camera frame")
            _stop_event.wait(0.2)
            continue

        frame, _ts = result
        try:
            detections = runner.run_on_frame(frame)
            consecutive_failures = 0
        except Exception as exc:
            consecutive_failures += 1
            logger.warning(
                "ALPR run_on_frame failed (%d/%d): %s",
                consecutive_failures, MAX_CONSECUTIVE_FAILURES, exc,
            )
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                logger.error(
                    "ALPR loop disabled after %d consecutive failures: %s",
                    consecutive_failures, exc,
                )
                set_state(
                    error=f"ALPR halted after {consecutive_failures} consecutive failures: {exc}",
                    running=False,
                )
                return
            _stop_event.wait(scan_interval)
            continue
        voter.add_frame(detections)
        latest = detections[0] if detections else None
        best = voter.get_best()

        needs_snap: dict = {}
        with _lock:
            now = time.time()
            _active, _recent, sightings = _update_plate_sightings(detections, now)

            # Collect plates whose best_confidence just improved (snapshot needed)
            for plate_key, s in _active.items():
                if s.get("_needs_snapshot"):
                    s["_needs_snapshot"] = False
                    needs_snap[plate_key] = (s.get("bbox"), s.get("best_confidence", 0.0))

            _state["frames_scanned"] = int(_state.get("frames_scanned", 0)) + 1
            _state["detections_seen"] = int(_state.get("detections_seen", 0)) + len(detections)
            _state["latest"] = latest
            if best and best.get("plate") and _active:
                sighting = _active.get(best["plate"])
                if sighting:
                    best = {
                        **best,
                        "bbox":            sighting.get("bbox"),
                        "frame_w":         sighting.get("frame_w"),
                        "frame_h":         sighting.get("frame_h"),
                        "best_confidence": sighting.get("best_confidence", best.get("confidence", 0.0)),
                    }
            _state["best"] = best
            _state["sightings"] = sightings
            _state["error"] = None
            _state["updated_at"] = now

        # I/O outside the lock: save crops + persist sightings to DB
        if needs_snap:
            gps_state = gps.get_state() if gps else None
            for plate_key, (bbox, best_conf) in needs_snap.items():
                snap_path = _save_plate_crop(frame, bbox, plate_key)
                if snap_path:
                    with _lock:
                        active_now = _state.get("active_sightings", {})
                        if plate_key in active_now:
                            active_now[plate_key]["snapshot_path"] = snap_path
                if db is not None:
                    try:
                        db.add_sighting(
                            plate=plate_key,
                            confidence=best_conf,
                            snapshot_path=snap_path,
                            latitude=gps_state.get("lat") if gps_state else None,
                            longitude=gps_state.get("lon") if gps_state else None,
                            speed_kmh=gps_state.get("speed_kmh") if gps_state else None,
                            heading=gps_state.get("heading") if gps_state else None,
                            altitude=gps_state.get("altitude") if gps_state else None,
                            gps_timestamp=gps_state.get("timestamp") if gps_state else None,
                            gps_backend=gps_state.get("backend_used") if gps_state else None,
                        )
                    except Exception as exc:
                        logger.warning("Could not persist sighting for %s: %s", plate_key, exc)

        _stop_event.wait(scan_interval)

    set_state(running=False)
