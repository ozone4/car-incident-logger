"""
app.py — Flask web UI for the car incident logger.

Run from the project root:
    python web/app.py

Options:
    --host  Bind address (default: 127.0.0.1)
    --port  Port        (default: 5000)
    --debug Enable Flask debug mode
"""

import argparse
import json
import logging
import sys
import threading
import time
from pathlib import Path
from typing import Optional

import cv2
import yaml
from flask import (
    Flask,
    Response,
    jsonify,
    render_template,
    request,
    send_file,
    url_for,
)

# ── Path setup ────────────────────────────────────────────────────────────────
# Works whether launched from project root or from web/.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from modules.camera_capture import CameraCapture  # noqa: E402
from modules.alpr_runner import ALPRRunner, _preprocess_crop  # noqa: E402
from modules.config_manager import ConfigManager  # noqa: E402
from modules.dashcam import DashcamRecorder  # noqa: E402
from modules.health_monitor import HealthMonitor  # noqa: E402
from modules.loop_recorder import LoopRecorder  # noqa: E402
from modules.incident_trigger import WebTrigger  # noqa: E402
from modules.multi_frame_voter import MultiFrameVoter  # noqa: E402
from modules.gps_reader import GPSReader  # noqa: E402
from modules.trip_tracker import TripTracker  # noqa: E402
from modules.plate_database import PlateDatabase  # noqa: E402
from modules.rolling_buffer import RollingBuffer  # noqa: E402
from modules.recording_recovery import recover_recordings  # noqa: E402
from modules.storage_manager import StorageManager  # noqa: E402
from modules.power_status import read_power_status  # noqa: E402

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

# ── Flask app ─────────────────────────────────────────────────────────────────
app = Flask(__name__)

# ── Camera state (module-level, protected by _camera_lock) ───────────────────
_camera: Optional[CameraCapture] = None
_camera_lock = threading.Lock()

# ── Live ALPR state ──────────────────────────────────────────────────────────
_alpr_thread: Optional[threading.Thread] = None
_alpr_stop_event = threading.Event()
_alpr_lock = threading.Lock()
_alpr_state: dict = {
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

SIGHTING_ACTIVE_TIMEOUT_SECONDS = 5.0
SIGHTING_HISTORY_LIMIT = 30

# ── Loop recorder state ─────────────────────────────────────────────────────
_loop_recorder: Optional[LoopRecorder] = None

# ── Dashcam state ───────────────────────────────────────────────────────────
_dashcam: Optional[DashcamRecorder] = None
_dashcam_buffer: Optional[RollingBuffer] = None
_web_trigger = WebTrigger()

# ── Storage + health state ──────────────────────────────────────────────────
_storage_manager: Optional[StorageManager] = None
_health_monitor: Optional[HealthMonitor] = None
_last_recovery_result: Optional[dict] = None

# ── GPS + trip state ─────────────────────────────────────────────────────────
_gps_reader: Optional[GPSReader] = None
_trip_tracker: Optional[TripTracker] = None


# ── Config helpers ────────────────────────────────────────────────────────────

def _config_path() -> Path:
    return PROJECT_ROOT / "config.yaml"


def _load_config() -> ConfigManager:
    return ConfigManager(str(_config_path()))


# ── DB helper ─────────────────────────────────────────────────────────────────

def _get_db() -> Optional[PlateDatabase]:
    """Return a PlateDatabase, or None if the config/DB is unavailable."""
    try:
        cfg = _load_config()
        db_path = cfg.storage_base_path / "plates.db"
        return PlateDatabase(str(db_path))
    except Exception as exc:
        logger.warning("Could not open database: %s", exc)
        return None


# ── Camera management ─────────────────────────────────────────────────────────

def _start_camera() -> dict:
    global _camera
    with _camera_lock:
        if _camera and _camera.is_running:
            return {"status": "already_running"}
        cfg = _load_config()
        _camera = CameraCapture(
            device_index=cfg.camera_device_index,
            width=cfg.camera_width,
            height=cfg.camera_height,
            fps=cfg.camera_fps,
            fourcc_str=cfg.camera_format,
        )
        _camera.start()
    logger.info("Camera preview started")
    _start_dashcam_buffer()
    _start_loop_recorder()
    return {"status": "started"}


def _stop_camera() -> dict:
    global _camera
    _alpr_stop_event.set()
    _stop_loop_recorder()
    _stop_dashcam_buffer()
    with _camera_lock:
        if _camera is None or not _camera.is_running:
            return {"status": "not_running"}
        _camera.stop()
        _camera = None
    _set_alpr_state(running=False)
    logger.info("Camera preview stopped")
    return {"status": "stopped"}


def _start_dashcam_buffer() -> None:
    """Start the rolling buffer for dashcam capture, attached to the active camera."""
    global _dashcam, _dashcam_buffer
    with _camera_lock:
        cam = _camera
    if cam is None or not cam.is_running:
        return

    cfg = _load_config()
    if _dashcam_buffer is None:
        _dashcam_buffer = RollingBuffer(
            duration_seconds=int(cfg.dashcam_pre_roll_seconds) + 5,
            fps=cfg.camera_fps,
        )
        _dashcam_buffer.start(cam.get_frame_queue())

    if _dashcam is None:
        _dashcam = DashcamRecorder(
            output_path=cfg.dashcam_output_path,
            pre_roll_seconds=cfg.dashcam_pre_roll_seconds,
            post_roll_seconds=cfg.dashcam_post_roll_seconds,
            fps=cfg.camera_fps,
        )
    _dashcam.attach(_dashcam_buffer, cam)

    def _trigger_callback(source: str, meta: dict) -> dict:
        alpr_plate = meta.get("alpr_plate")
        recent = meta.get("recent_sightings")
        result = _dashcam.trigger(source=source, alpr_plate=alpr_plate, recent_sightings=recent)
        if result.get("ok"):
            _save_dashcam_incident_to_db(result)
        return result

    _web_trigger.arm(_trigger_callback)
    logger.info("Dashcam buffer and trigger armed")


def _stop_dashcam_buffer() -> None:
    global _dashcam_buffer
    _web_trigger.disarm()
    if _dashcam_buffer is not None:
        _dashcam_buffer.stop()
        _dashcam_buffer = None
    logger.info("Dashcam buffer stopped")


def _save_dashcam_incident_to_db(result: dict) -> None:
    """Save a dashcam incident to the existing incidents table."""
    db = _get_db()
    if db is None:
        return
    plate = result.get("plate") or "DASHCAM"

    # Attach nearest GPS snapshot to incident metadata
    gps_snap: Optional[dict] = None
    if _gps_reader is not None:
        gps_snap = _gps_reader.get_state()

    metadata = {
        "timestamp": result.get("timestamp", ""),
        "clip_path": result.get("clip_path"),
        "trigger_source": result.get("trigger_source", "web"),
        "plate": plate,
        "pre_roll_frames": result.get("pre_roll_frames", 0),
        "post_roll_frames": result.get("post_roll_frames", 0),
        "total_frames": result.get("total_frames", 0),
        "recent_sightings": result.get("recent_sightings", []),
        "gps": gps_snap,
    }
    try:
        db.add_incident(plate, metadata)
    except Exception as exc:
        logger.warning("Could not save dashcam incident to DB: %s", exc)

    # Auto-protect recent recording segments near the incident
    _auto_protect_recent_segments()


def _auto_protect_recent_segments() -> None:
    """Lock the most recent recording segments to protect incident context."""
    try:
        cfg = _load_config()
        recording_path = Path(cfg.recording_output_path)
        if not recording_path.exists():
            return

        # Lock segments from the last 2 minutes (covers pre-roll + current)
        import time as _time
        cutoff = _time.time() - 120

        for json_path in recording_path.rglob("*.json"):
            if json_path.suffix == ".tmp":
                continue
            try:
                meta = json.loads(json_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue

            if meta.get("locked"):
                continue

            # Check if this segment is recent enough to protect
            end_time_str = meta.get("end_time", "")
            try:
                from datetime import datetime as _dt, timezone as _tz
                dt = _dt.fromisoformat(end_time_str.replace("Z", "+00:00"))
                if dt.timestamp() >= cutoff:
                    meta["locked"] = True
                    meta["locked_reason"] = "incident_auto_protect"
                    json_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
                    logger.debug("Auto-protected segment: %s", json_path.name)
            except (ValueError, AttributeError):
                continue
    except Exception as exc:
        logger.warning("Auto-protect segments failed: %s", exc)


def _camera_status() -> dict:
    with _camera_lock:
        cam = _camera
    if cam and cam.is_running:
        return {
            "running": True,
            "device_index": cam.device_index,
            "resolution": f"{cam.width}×{cam.height}",
            "fps": cam.fps,
        }
    return {"running": False}


# ── Live ALPR management ─────────────────────────────────────────────────────

def _alpr_config() -> dict:
    cfg = _load_config()
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
    }


def _set_alpr_state(**updates) -> None:
    with _alpr_lock:
        _alpr_state.update(updates)
        _alpr_state["updated_at"] = time.time()


def _save_plate_crop(frame, bbox: list, plate: str) -> Optional[str]:
    """Save a 640px-wide JPEG of the plate crop. Returns the saved path or None."""
    try:
        if bbox is None or frame is None:
            return None
        x1, y1, x2, y2 = bbox
        crop = _preprocess_crop(frame, x1, y1, x2, y2, pad_ratio=0.18, for_ocr=False)
        cfg = _load_config()
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


def _format_elapsed(seconds: float) -> str:
    if seconds < 1:
        return "now"
    if seconds < 60:
        return f"{int(seconds)}s ago"
    return f"{int(seconds // 60)}m ago"


def _new_sighting(plate: str, det: dict, now: float) -> dict:
    history = None
    db = _get_db()
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
        "_needs_snapshot": True,
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


def _update_plate_sightings(detections: list[dict], now: float) -> tuple[dict, list[dict], list[dict]]:
    """Update active/recent plate sightings and return serialized display rows."""
    active: dict = _alpr_state.setdefault("active_sightings", {})
    recent: list = _alpr_state.setdefault("recent_sightings", [])

    for det in detections:
        plate = det.get("plate")
        if not plate:
            continue
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


def _live_alpr_loop() -> None:
    """Background scanner: sample latest preview frame, run ALPR, vote over time."""
    runner = ALPRRunner(_alpr_config())
    ready = runner.initialize()
    status = runner.status_info()
    _set_alpr_state(
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
        _set_alpr_state(running=False)
        return

    cfg = _load_config()
    scan_interval = max(0.0, float(cfg.alpr_scan_interval))
    voter = MultiFrameVoter(min_votes=1)

    # Use the module-level GPS reader (started at startup); fall back to local if not initialised
    gps = _gps_reader
    if gps is None:
        try:
            raw_cfg = yaml.safe_load(_config_path().read_text()) or {}
            gps_cfg = raw_cfg.get("gps", {})
        except Exception:
            gps_cfg = {}
        gps = GPSReader(gps_cfg)

    db = _get_db()

    while not _alpr_stop_event.is_set():
        with _camera_lock:
            cam = _camera
        if cam is None or not cam.is_running:
            _set_alpr_state(error="Camera is not running")
            _alpr_stop_event.wait(0.5)
            continue

        result = cam.get_frame()
        if result is None:
            _set_alpr_state(error="Waiting for first camera frame")
            _alpr_stop_event.wait(0.2)
            continue

        frame, _ts = result
        try:
            detections = runner.run_on_frame(frame)
        except Exception as exc:
            logger.warning("ALPR run_on_frame failed: %s", exc)
            _alpr_stop_event.wait(scan_interval)
            continue
        voter.add_frame(detections)
        latest = detections[0] if detections else None
        best = voter.get_best()

        needs_snap: dict = {}
        with _alpr_lock:
            now = time.time()
            _active, _recent, sightings = _update_plate_sightings(detections, now)

            # Collect plates whose best_confidence just improved (snapshot needed)
            for plate_key, s in _active.items():
                if s.get("_needs_snapshot"):
                    s["_needs_snapshot"] = False
                    needs_snap[plate_key] = (s.get("bbox"), s.get("best_confidence", 0.0))

            _alpr_state["frames_scanned"] = int(_alpr_state.get("frames_scanned", 0)) + 1
            _alpr_state["detections_seen"] = int(_alpr_state.get("detections_seen", 0)) + len(detections)
            _alpr_state["latest"] = latest
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
            _alpr_state["best"] = best
            _alpr_state["sightings"] = sightings
            _alpr_state["error"] = None
            _alpr_state["updated_at"] = now

        # I/O outside the lock: save crops + persist sightings to DB
        if needs_snap:
            gps_state = gps.get_state() if gps else None
            for plate_key, (bbox, best_conf) in needs_snap.items():
                snap_path = _save_plate_crop(frame, bbox, plate_key)
                if snap_path:
                    with _alpr_lock:
                        active_now = _alpr_state.get("active_sightings", {})
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
                        logger.debug("Could not persist sighting for %s: %s", plate_key, exc)

        _alpr_stop_event.wait(scan_interval)

    _set_alpr_state(running=False)


def _start_live_alpr() -> dict:
    global _alpr_thread
    with _alpr_lock:
        if _alpr_state.get("running"):
            return {"status": "already_running", **_alpr_state}

    # Reuse the preview camera; start it automatically if needed.
    if not _camera_status().get("running"):
        _start_camera()

    _alpr_stop_event.clear()
    _set_alpr_state(
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
    _alpr_thread = threading.Thread(target=_live_alpr_loop, daemon=True, name="LiveALPR")
    _alpr_thread.start()
    return {"status": "started", **_get_live_alpr_status()}


def _stop_live_alpr() -> dict:
    _alpr_stop_event.set()
    thread = _alpr_thread
    if thread and thread.is_alive():
        thread.join(timeout=2.0)
    _set_alpr_state(running=False)
    return {"status": "stopped", **_get_live_alpr_status()}


def _get_live_alpr_status() -> dict:
    with _alpr_lock:
        now = time.time()
        _active, _recent, sightings = _update_plate_sightings([], now)
        _alpr_state["sightings"] = sightings
        return dict(_alpr_state)


# ── MJPEG stream ──────────────────────────────────────────────────────────────

def _generate_frames():
    """Yield multipart MJPEG chunks for the live camera stream."""
    while True:
        with _camera_lock:
            cam = _camera

        if cam is None or not cam.is_running:
            time.sleep(0.1)
            continue

        result = cam.get_frame()
        if result is None:
            time.sleep(0.05)
            continue

        frame, _ = result

        # Downscale for preview — full 1080p is needlessly heavy in a browser.
        h, w = frame.shape[:2]
        if w > 854:
            scale = 854 / w
            frame = cv2.resize(frame, (854, int(h * scale)))

        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
        if not ok:
            continue

        yield (
            b"--frame\r\n"
            b"Content-Type: image/jpeg\r\n\r\n"
            + buf.tobytes()
            + b"\r\n"
        )
        time.sleep(1 / 30)


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    db = _get_db()
    incidents = db.get_all_incidents(limit=10) if db else []
    _decode_metadata(incidents)
    return render_template("index.html", incidents=incidents, camera=_camera_status())


@app.route("/dashboard")
def dashboard():
    return render_template("dashboard.html")


@app.route("/camera")
def camera_page():
    return render_template("camera.html", camera=_camera_status())


@app.route("/camera/stream")
def camera_stream():
    return Response(
        _generate_frames(),
        mimetype="multipart/x-mixed-replace; boundary=frame",
    )


@app.route("/camera/start", methods=["POST"])
def camera_start():
    return jsonify(_start_camera())


@app.route("/camera/stop", methods=["POST"])
def camera_stop():
    return jsonify(_stop_camera())


@app.route("/camera/status")
def camera_status_api():
    return jsonify(_camera_status())


@app.route("/camera/snapshot")
def camera_snapshot():
    """Return one JPEG frame from the active preview camera for diagnostics."""
    with _camera_lock:
        cam = _camera

    if cam is None or not cam.is_running:
        return "Camera is not running", 409

    result = cam.get_frame()
    if result is None:
        return "No frame available yet", 503

    frame, _ = result
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 92])
    if not ok:
        return "Could not encode frame", 500

    return Response(buf.tobytes(), mimetype="image/jpeg")


@app.route("/alpr/test-frame")
def alpr_test_frame_api():
    """Run ALPR once against the current preview frame and return raw detections."""
    with _camera_lock:
        cam = _camera

    if cam is None or not cam.is_running:
        return jsonify({"ok": False, "error": "Camera is not running"}), 409

    result = cam.get_frame()
    if result is None:
        return jsonify({"ok": False, "error": "No frame available yet"}), 503

    runner = ALPRRunner(_alpr_config())
    ready = runner.initialize()
    status = runner.status_info()
    detections = runner.run_on_frame(result[0]) if ready else []
    raw_boxes = getattr(getattr(runner, "_detector", None), "last_raw_detections", [])
    ocr_candidates = getattr(runner, "last_debug_candidates", [])
    return jsonify({
        "ok": True,
        "ready": ready,
        "status": status,
        "detections": detections,
        "count": len(detections),
        "raw_detector_boxes": raw_boxes,
        "raw_detector_count": len(raw_boxes),
        "ocr_candidates": ocr_candidates,
        "ocr_candidate_count": len(ocr_candidates),
    })


@app.route("/incidents")
def incidents_page():
    query = request.args.get("q", "").strip()
    db = _get_db()

    if db is None:
        return render_template("incidents.html", incidents=[], query=query)

    if query:
        plates = db.search_plates(query)
        incidents = []
        for p in plates:
            rows = db.get_incidents_for_plate(p["plate"])
            for row in rows:
                row["plate"] = p["plate"]
            incidents.extend(rows)
        incidents.sort(key=lambda r: r.get("timestamp", ""), reverse=True)
    else:
        incidents = db.get_all_incidents(limit=100)

    _decode_metadata(incidents)
    return render_template("incidents.html", incidents=incidents, query=query)


@app.route("/incidents/<plate>")
def plate_detail(plate):
    db = _get_db()
    if db is None:
        return render_template(
            "plate_detail.html", plate=plate, incidents=[], plate_info=None
        )
    incidents = db.get_incidents_for_plate(plate)
    _decode_metadata(incidents)
    plate_info = db.get_plate(plate)
    return render_template(
        "plate_detail.html",
        plate=plate,
        incidents=incidents,
        plate_info=plate_info,
    )


@app.route("/incidents/json")
def incidents_json():
    """Return recent incidents as JSON for the live dashboard."""
    limit = request.args.get("limit", 10, type=int)
    db = _get_db()
    incidents = db.get_all_incidents(limit=limit) if db else []
    _decode_metadata(incidents)
    return jsonify(incidents=incidents)


@app.route("/media")
def serve_media():
    """Serve a clip, audio, or transcript file by path query param."""
    filepath = request.args.get("path", "")
    if not filepath:
        return "Missing path parameter", 400

    candidate = Path(filepath)
    if not candidate.is_absolute():
        candidate = PROJECT_ROOT / filepath

    # Restrict serving to files within the project tree.
    try:
        candidate.resolve().relative_to(PROJECT_ROOT.resolve())
    except ValueError:
        return "Forbidden", 403

    if not candidate.exists():
        return "Not found", 404

    return send_file(str(candidate))


@app.route("/alpr/status")
def alpr_status_api():
    """Return JSON describing ALPR engine availability without initializing engines."""
    info: dict = {}

    # Check ultralytics (YOLO detector)
    try:
        import ultralytics  # noqa: F401
        info["detector"] = "available"
    except ImportError:
        info["detector"] = "unavailable"
        info["detector_hint"] = "pip install ultralytics"

    # Check paddleocr
    try:
        import paddleocr  # noqa: F401
        info["ocr"] = "available"
    except ImportError:
        info["ocr"] = "unavailable"
        info["ocr_hint"] = "pip install paddlepaddle paddleocr"

    try:
        import easyocr  # noqa: F401
        info["ocr_fallback"] = "available"
    except ImportError as exc:
        info["ocr_fallback"] = "unavailable"
        info["ocr_fallback_hint"] = f"easyocr import failed: {exc}; run pip install easyocr in the active venv"

    # Check model file
    try:
        cfg = _load_config()
        model_path = cfg.alpr_yolo_model_path
        info["model_path"] = model_path
        info["model_exists"] = Path(model_path).exists()
        info["alpr_enabled"] = cfg.alpr_enabled
    except Exception:
        info["alpr_enabled"] = False

    info["ready"] = (
        info.get("detector") == "available"
        and info.get("ocr") == "available"
        and info.get("model_exists", False)
    )
    return jsonify(info)


@app.route("/sightings/image")
def sightings_image():
    """Serve a saved plate crop JPEG by its absolute path (read-only, within data dir)."""
    path_str = request.args.get("path", "")
    if not path_str:
        return "missing path", 400
    path = Path(path_str).resolve()
    # Safety: only serve files under the project data directory
    try:
        cfg = _load_config()
        allowed_root = Path(cfg.storage_base_path).resolve()
    except Exception:
        allowed_root = (PROJECT_ROOT / "data").resolve()
    if not str(path).startswith(str(allowed_root)):
        return "forbidden", 403
    if not path.exists() or not path.is_file():
        return "not found", 404
    return send_file(str(path), mimetype="image/jpeg")


@app.route("/alpr/live/status")
def alpr_live_status_api():
    return jsonify(_get_live_alpr_status())


@app.route("/alpr/live/start", methods=["POST"])
def alpr_live_start_api():
    return jsonify(_start_live_alpr())


@app.route("/alpr/live/stop", methods=["POST"])
def alpr_live_stop_api():
    return jsonify(_stop_live_alpr())


# ── Loop recorder management ────────────────────────────────────────────────

def _start_loop_recorder() -> None:
    """Start continuous loop recording if enabled in config."""
    global _loop_recorder
    cfg = _load_config()
    if not cfg.recording_enabled:
        return
    with _camera_lock:
        cam = _camera
    if cam is None or not cam.is_running:
        return
    if _loop_recorder is not None and _loop_recorder.is_recording:
        return
    _loop_recorder = LoopRecorder(
        output_path=cfg.recording_output_path,
        segment_duration_seconds=cfg.recording_segment_duration,
        fps=cfg.camera_fps,
        overlay_enabled=cfg.overlay_enabled,
        overlay_position=cfg.overlay_position,
        overlay_font_scale=cfg.overlay_font_scale,
        overlay_color=tuple(cfg.overlay_color),
        overlay_background=cfg.overlay_background,
    )
    _loop_recorder.start(cam)
    logger.info("Loop recorder started")


def _stop_loop_recorder() -> None:
    global _loop_recorder
    if _loop_recorder is not None:
        _loop_recorder.stop()
        _loop_recorder = None
    logger.info("Loop recorder stopped")


# ── Storage manager management ─────────────────────────────────────────────

def _start_storage_manager() -> None:
    """Start the storage manager for periodic cleanup of old recordings."""
    global _storage_manager, _health_monitor
    cfg = _load_config()
    recording_path = cfg.recording_output_path

    if _storage_manager is None or not _storage_manager.is_running:
        _storage_manager = StorageManager(
            recording_path=recording_path,
            max_recording_age_days=cfg.storage_max_recording_age_days,
            min_free_space_gb=cfg.storage_min_free_space_gb,
            cleanup_interval_seconds=cfg.storage_cleanup_interval_seconds,
        )
        _storage_manager.start()

    if _health_monitor is None:
        _health_monitor = HealthMonitor(
            recording_path=recording_path,
            min_free_space_gb=cfg.storage_min_free_space_gb,
        )


def _stop_storage_manager() -> None:
    global _storage_manager
    if _storage_manager is not None:
        _storage_manager.stop()
        _storage_manager = None


# ── Loop recorder routes ────────────────────────────────────────────────────

@app.route("/recording/status")
def recording_status_api():
    """Return loop recorder status as JSON."""
    if _loop_recorder is None:
        return jsonify({"recording": False, "enabled": _load_config().recording_enabled})
    return jsonify(_loop_recorder.status())


# ── Dashcam routes ───────────────────────────────────────────────────────────

@app.route("/dashcam/status")
def dashcam_status_api():
    """Return dashcam buffer status and last trigger result."""
    status = _dashcam.buffer_status() if _dashcam else {"attached": False}
    status["trigger_armed"] = _web_trigger.is_armed
    status["capture_state"] = _dashcam.capture_state if _dashcam else "idle"
    status["last_result"] = _dashcam.last_result if _dashcam else None
    status["last_error"] = _dashcam.last_error if _dashcam else None
    return jsonify(status)


@app.route("/dashcam/trigger", methods=["POST"])
def dashcam_trigger_api():
    """Trigger a dashcam incident capture from the web UI.

    Capture can take 35+ seconds because it includes post-roll and video encoding.
    Start it in the background so the dashboard request does not hang and exhaust
    browser/server request resources while other status polling continues.
    """
    if not _web_trigger.is_armed:
        return jsonify({"ok": False, "error": "Trigger is not armed"}), 409

    if _dashcam and _dashcam.capture_state in {"capturing", "saving"}:
        return jsonify({"ok": False, "error": "Capture already in progress"}), 409

    # Gather current ALPR state for metadata
    meta: dict = {}
    with _alpr_lock:
        best = _alpr_state.get("best")
        if best and best.get("plate"):
            meta["alpr_plate"] = best["plate"]
        sightings = _alpr_state.get("sightings", [])
        if sightings:
            meta["recent_sightings"] = sightings[:10]

    # Mark immediately so the dashboard poll cannot mistake a previous completed
    # capture for this newly accepted one before the background thread starts.
    if _dashcam:
        _dashcam._capture_state = "capturing"  # noqa: SLF001 - route owns singleton lifecycle

    def _run_capture() -> None:
        try:
            _web_trigger.fire(meta)
        except Exception:
            logger.exception("Background dashcam capture failed")

    threading.Thread(target=_run_capture, daemon=True, name="DashcamTrigger").start()
    return jsonify({"ok": True, "accepted": True, "capture_state": "capturing"}), 202


@app.route("/dashcam/clips/<path:subpath>")
def dashcam_clip_file(subpath):
    """Serve a dashcam clip file."""
    cfg = _load_config()
    clip_dir = cfg.dashcam_output_path.resolve()
    candidate = (clip_dir / subpath).resolve()
    try:
        candidate.relative_to(clip_dir)
    except ValueError:
        return "Forbidden", 403
    if not candidate.exists():
        return "Not found", 404
    return send_file(str(candidate))


@app.route("/dashcam/export/<path:incident_id>")
def dashcam_export(incident_id):
    """Export a dashcam incident as a zip (clip + metadata)."""
    import io
    import zipfile

    cfg = _load_config()
    clip_dir = cfg.dashcam_output_path.resolve()
    incident_dir = (clip_dir / incident_id).resolve()

    try:
        incident_dir.relative_to(clip_dir)
    except ValueError:
        return "Forbidden", 403
    if not incident_dir.exists() or not incident_dir.is_dir():
        return "Not found", 404

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for file in incident_dir.iterdir():
            if file.is_file():
                zf.write(file, f"{incident_id}/{file.name}")
    buf.seek(0)

    return Response(
        buf.getvalue(),
        mimetype="application/zip",
        headers={"Content-Disposition": f"attachment; filename=incident_{incident_id}.zip"},
    )


# ── Health + Storage routes ─────────────────────────────────────────────────

@app.route("/health")
def health_api():
    """Return composite system health status as JSON."""
    if _health_monitor is None:
        # Lazy init if not yet started
        _start_storage_manager()

    cam_running = _camera_status().get("running", False)
    dashcam_armed = _web_trigger.is_armed if _web_trigger else False
    rec_status = _loop_recorder.status() if _loop_recorder else {}

    with _alpr_lock:
        alpr = dict(_alpr_state)

    stor_status = _storage_manager.status() if _storage_manager else None

    result = _health_monitor.check(
        camera_running=cam_running,
        dashcam_buffer_armed=dashcam_armed,
        loop_recorder_status=rec_status,
        alpr_state=alpr,
        storage_status=stor_status,
    )
    return jsonify(result)


@app.route("/storage/status")
def storage_status_api():
    """Return storage manager status as JSON."""
    if _storage_manager is None:
        return jsonify({"running": False})
    return jsonify(_storage_manager.status())


def _appliance_config() -> dict:
    cfg = _load_config()
    section = cfg.get("appliance", default={})
    if not isinstance(section, dict):
        section = {}
    return {
        "enabled": bool(section.get("enabled", True)),
        "app_url": section.get("app_url", "http://127.0.0.1:5000"),
        "check_interval_seconds": int(section.get("check_interval_seconds", 5)),
        "battery_grace_seconds": int(section.get("battery_grace_seconds", 600)),
        "critical_battery_percent": int(section.get("critical_battery_percent", 12)),
        "stop_before_suspend": bool(section.get("stop_before_suspend", True)),
        "restart_after_resume": bool(section.get("restart_after_resume", True)),
        "suspend_command": section.get("suspend_command", "systemctl suspend"),
        "state_file": section.get("state_file", "./data/appliance-power-state.json"),
    }


def _read_appliance_state(state_file: str) -> dict:
    path = Path(state_file)
    if not path.is_absolute():
        path = (PROJECT_ROOT / path).resolve()
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not read appliance state file %s: %s", path, exc)
    return {}


@app.route("/system/power")
def system_power_api():
    """Return Linux AC/battery power status for appliance installs."""
    return jsonify(read_power_status())


@app.route("/appliance/status")
def appliance_status_api():
    """Return dashboard-friendly Linux appliance status."""
    cfg = _appliance_config()
    state = _read_appliance_state(str(cfg["state_file"]))
    power = state.get("power") if isinstance(state.get("power"), dict) else read_power_status()

    grace = int(cfg["battery_grace_seconds"])
    remaining = state.get("grace_remaining_seconds")
    if remaining is None:
        remaining = grace if power.get("on_ac") is not False else 0

    cam = _camera_status()
    rec = _loop_recorder.status() if _loop_recorder else {"recording": False, "enabled": _load_config().recording_enabled}

    return jsonify({
        "enabled": cfg["enabled"],
        "mode": "linux-appliance",
        "power": power,
        "state": state.get("state") or power.get("state") or "unknown",
        "grace_seconds": grace,
        "grace_remaining_seconds": remaining,
        "battery_since": state.get("battery_since"),
        "last_suspend_reason": state.get("last_suspend_reason"),
        "last_suspend_at": state.get("last_suspend_at"),
        "last_resume_at": state.get("last_resume_at"),
        "watcher_updated_at": state.get("updated_at"),
        "camera_running": cam.get("running", False),
        "recording": rec.get("recording", False),
        "segments_completed": rec.get("segments_completed", 0),
        "config": cfg,
    })


@app.route("/gps/status")
def gps_status_api():
    """Return current GPS state (normalized dict) or a disabled/no-fix response."""
    if _gps_reader is None:
        # GPS not started — read config to report whether it is even enabled
        try:
            raw_cfg = yaml.safe_load(_config_path().read_text()) or {}
            enabled = bool(raw_cfg.get("gps", {}).get("enabled", False))
        except Exception:
            enabled = False
        return jsonify({"enabled": enabled, "available": False, "state": None})

    state = _gps_reader.get_state()
    return jsonify({
        "enabled": True,
        "available": _gps_reader.is_available,
        "state": state,
    })


@app.route("/trip/current")
def trip_current_api():
    """Return current trip summary from TripTracker, or not-running response."""
    if _trip_tracker is None:
        return jsonify({"running": False, "trip": None})
    trip = _trip_tracker.get_current_trip()
    return jsonify({"running": trip is not None, "trip": trip})


@app.route("/storage/cleanup", methods=["POST"])
def storage_cleanup_api():
    """Trigger a manual storage cleanup pass."""
    if _storage_manager is None:
        _start_storage_manager()
    dry_run = request.args.get("dry_run", "false").lower() in ("true", "1", "yes")
    result = _storage_manager.run_cleanup(dry_run=dry_run)
    return jsonify(result)


@app.route("/storage/recovery")
def storage_recovery_api():
    """Return last startup recovery results."""
    if _last_recovery_result is None:
        return jsonify({"summary": "No recovery has been run", "recovered": [], "corrupt": [], "cleaned": []})
    return jsonify(_last_recovery_result)


@app.route("/storage/recovery/run", methods=["POST"])
def storage_recovery_run_api():
    """Manually trigger recording recovery scan."""
    global _last_recovery_result
    cfg = _load_config()
    _last_recovery_result = recover_recordings(cfg.recording_output_path)
    return jsonify(_last_recovery_result)


# ── Recordings browser routes ──────────────────────────────────────────────

def _list_all_recordings() -> list[dict]:
    """List all recording segments from sidecar JSON files, newest first."""
    cfg = _load_config()
    recording_path = Path(cfg.recording_output_path)
    results = []

    if not recording_path.exists():
        return results

    for json_path in recording_path.rglob("*.json"):
        try:
            meta = json.loads(json_path.read_text())
        except (json.JSONDecodeError, OSError):
            continue

        # Find video file
        video_path = _find_video_for_sidecar(json_path, meta, recording_path)
        if video_path is None:
            continue

        start_time = meta.get("start_time", "")
        end_time = meta.get("end_time", "")
        duration = meta.get("duration_seconds", 0)
        frame_count = meta.get("frame_count", 0)
        locked = bool(meta.get("locked", False))
        size_bytes = video_path.stat().st_size if video_path.exists() else 0

        # Build a stable ID from the sidecar path relative to recording_path
        try:
            rel = json_path.relative_to(recording_path)
            rec_id = str(rel.with_suffix("")).replace("\\", "/")
        except ValueError:
            rec_id = json_path.stem

        results.append({
            "id": rec_id,
            "start_time": start_time,
            "end_time": end_time,
            "duration_seconds": round(duration, 1),
            "frame_count": frame_count,
            "locked": locked,
            "size_bytes": size_bytes,
            "size_mb": round(size_bytes / (1024 * 1024), 1),
            "video_path": str(video_path),
            "json_path": str(json_path),
            "filename": video_path.name,
            "date_dir": json_path.parent.name,
        })

    # Sort newest first by start_time
    results.sort(key=lambda r: r["start_time"], reverse=True)
    return results


def _find_video_for_sidecar(json_path: Path, meta: dict, recording_path: Path):
    """Find the video file corresponding to a sidecar JSON."""
    file_path = meta.get("file_path")
    if file_path:
        candidate = Path(file_path)
        if candidate.exists():
            return candidate
        candidate = recording_path / candidate.name
        if candidate.exists():
            return candidate

    for ext in (".mp4", ".avi", ".mkv"):
        candidate = json_path.with_suffix(ext)
        if candidate.exists():
            return candidate
    return None


def _resolve_recording_path(rec_id: str) -> tuple:
    """Resolve a recording ID to (json_path, video_path, recording_path) safely.

    Returns (None, None, None) if invalid or path traversal detected.
    """
    cfg = _load_config()
    recording_path = Path(cfg.recording_output_path).resolve()
    json_path = (recording_path / (rec_id + ".json")).resolve()

    # Prevent path traversal
    try:
        json_path.relative_to(recording_path)
    except ValueError:
        return None, None, None

    if not json_path.exists():
        return None, None, None

    try:
        meta = json.loads(json_path.read_text())
    except (json.JSONDecodeError, OSError):
        return None, None, None

    video_path = _find_video_for_sidecar(json_path, meta, recording_path)
    return json_path, video_path, recording_path


@app.route("/recordings")
def recordings_page():
    """Browse continuous recording segments."""
    date_filter = request.args.get("date", "").strip()
    recordings = _list_all_recordings()

    if date_filter:
        recordings = [r for r in recordings if r["date_dir"] == date_filter]

    # Collect available dates for filter
    all_recordings = _list_all_recordings() if date_filter else recordings
    dates = sorted(set(r["date_dir"] for r in all_recordings), reverse=True)

    return render_template(
        "recordings.html",
        recordings=recordings,
        dates=dates,
        current_date=date_filter,
    )


@app.route("/recordings/list")
def recordings_list_api():
    """Return recording segments as JSON."""
    date_filter = request.args.get("date", "").strip()
    recordings = _list_all_recordings()
    if date_filter:
        recordings = [r for r in recordings if r["date_dir"] == date_filter]
    return jsonify({"recordings": recordings, "count": len(recordings)})


@app.route("/recordings/video/<path:rec_id>")
def recordings_serve_video(rec_id):
    """Serve a recording MP4 file safely."""
    cfg = _load_config()
    recording_path = Path(cfg.recording_output_path).resolve()

    # rec_id is like "2026-05-04/14-30-00" — map to the video file
    # Try common extensions
    for ext in (".mp4", ".avi", ".mkv"):
        candidate = (recording_path / (rec_id + ext)).resolve()
        try:
            candidate.relative_to(recording_path)
        except ValueError:
            return "Forbidden", 403
        if candidate.exists():
            return send_file(str(candidate))

    return "Not found", 404


@app.route("/recordings/lock", methods=["POST"])
def recordings_lock():
    """Set locked=true on a recording sidecar."""
    rec_id = request.form.get("id") or (request.get_json() or {}).get("id", "")
    if not rec_id:
        return jsonify({"ok": False, "error": "Missing recording id"}), 400

    json_path, video_path, recording_path = _resolve_recording_path(rec_id)
    if json_path is None:
        return jsonify({"ok": False, "error": "Recording not found"}), 404

    try:
        meta = json.loads(json_path.read_text())
        meta["locked"] = True
        json_path.write_text(json.dumps(meta, indent=2))
    except (json.JSONDecodeError, OSError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500

    return jsonify({"ok": True, "locked": True, "id": rec_id})


@app.route("/recordings/unlock", methods=["POST"])
def recordings_unlock():
    """Set locked=false on a recording sidecar."""
    rec_id = request.form.get("id") or (request.get_json() or {}).get("id", "")
    if not rec_id:
        return jsonify({"ok": False, "error": "Missing recording id"}), 400

    json_path, video_path, recording_path = _resolve_recording_path(rec_id)
    if json_path is None:
        return jsonify({"ok": False, "error": "Recording not found"}), 404

    try:
        meta = json.loads(json_path.read_text())
        meta["locked"] = False
        json_path.write_text(json.dumps(meta, indent=2))
    except (json.JSONDecodeError, OSError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500

    return jsonify({"ok": True, "locked": False, "id": rec_id})


@app.route("/recordings/delete", methods=["POST"])
def recordings_delete():
    """Delete an unlocked recording segment and its sidecar."""
    rec_id = request.form.get("id") or (request.get_json() or {}).get("id", "")
    if not rec_id:
        return jsonify({"ok": False, "error": "Missing recording id"}), 400

    json_path, video_path, recording_path = _resolve_recording_path(rec_id)
    if json_path is None:
        return jsonify({"ok": False, "error": "Recording not found"}), 404

    # Check locked status
    try:
        meta = json.loads(json_path.read_text())
    except (json.JSONDecodeError, OSError):
        return jsonify({"ok": False, "error": "Could not read sidecar"}), 500

    if meta.get("locked"):
        return jsonify({"ok": False, "error": "Recording is locked. Unlock it first."}), 409

    freed = 0
    if video_path and video_path.exists():
        freed += video_path.stat().st_size
        video_path.unlink()

    if json_path.exists():
        freed += json_path.stat().st_size
        json_path.unlink()

    # Remove empty parent date directory
    parent = json_path.parent
    try:
        if parent.resolve() != recording_path and parent.exists() and not any(parent.iterdir()):
            parent.rmdir()
    except OSError:
        pass

    return jsonify({"ok": True, "id": rec_id, "bytes_freed": freed})


@app.route("/config", methods=["GET", "POST"])
def config_page():
    error: Optional[str] = None
    success: Optional[str] = None

    if request.method == "POST":
        try:
            cfg_path = _config_path()
            with open(cfg_path) as f:
                data = yaml.safe_load(f)

            data["camera"]["device_index"] = int(request.form["device_index"])
            data["camera"]["resolution"]["width"] = int(request.form["width"])
            data["camera"]["resolution"]["height"] = int(request.form["height"])
            data["camera"]["fps"] = int(request.form["fps"])
            data["buffer"]["duration_seconds"] = int(request.form["buffer_duration"])

            with open(cfg_path, "w") as f:
                yaml.dump(data, f, default_flow_style=False, sort_keys=False)

            success = "Config saved. Restart the camera preview to apply changes."
        except (ValueError, KeyError) as exc:
            error = f"Invalid value: {exc}"
        except OSError as exc:
            error = f"Save failed: {exc}"

    try:
        cfg = _load_config()
        config_values = {
            "device_index": cfg.camera_device_index,
            "width": cfg.camera_width,
            "height": cfg.camera_height,
            "fps": cfg.camera_fps,
            "buffer_duration": cfg.buffer_duration,
            "format": cfg.camera_format,
            "transcription_model": cfg.transcription_model,
            "button_mode": cfg.button_mode,
            "button_key": cfg.button_key,
            "storage_base_path": str(cfg.storage_base_path),
        }
    except Exception as exc:
        config_values = {}
        error = error or f"Could not load config: {exc}"

    return render_template(
        "config.html", config=config_values, error=error, success=success
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _decode_metadata(incidents: list) -> None:
    """Parse metadata_json string into a 'meta' dict on each incident in-place."""
    for inc in incidents:
        raw = inc.get("metadata_json") or "{}"
        try:
            inc["meta"] = json.loads(raw) if isinstance(raw, str) else (raw or {})
        except json.JSONDecodeError:
            inc["meta"] = {}


def _format_timestamp(ts: str) -> str:
    """Convert a compact ISO timestamp like '20240315T143022Z' to a readable string."""
    try:
        from datetime import datetime, timezone
        dt = datetime.strptime(ts, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    except (ValueError, TypeError):
        return ts or "—"


app.jinja_env.globals["fmt_ts"] = _format_timestamp


# ── Startup helpers ──────────────────────────────────────────────────────────

def _start_gps_and_trip() -> None:
    """Start GPS reader and trip tracker from config. Failures are non-fatal."""
    global _gps_reader, _trip_tracker
    try:
        raw_cfg = yaml.safe_load(_config_path().read_text()) or {}
        gps_cfg = raw_cfg.get("gps", {})
        trip_cfg = raw_cfg.get("trip_tracker", {})
    except Exception as exc:
        logger.warning("GPS/trip config unavailable: %s", exc)
        return

    try:
        _gps_reader = GPSReader(gps_cfg)
        _gps_reader.start()
    except Exception as exc:
        logger.warning("GPS reader failed to start: %s", exc)
        _gps_reader = None

    if _gps_reader is not None and bool(trip_cfg.get("enabled", True)):
        db = _get_db()
        if db is not None:
            try:
                _trip_tracker = TripTracker(db=db, gps_reader=_gps_reader, config=trip_cfg)
                _trip_tracker.start()
            except Exception as exc:
                logger.warning("Trip tracker failed to start: %s", exc)
                _trip_tracker = None


def _auto_start_dashcam_services() -> None:
    """Start dashcam services for normal in-car operation.

    The manual dashboard buttons remain useful for recovery/testing, but the
    default app behavior should be dashcam-like: boot → camera/buffer armed →
    ALPR scanning if available.
    """
    global _last_recovery_result
    try:
        cfg = _load_config()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Auto-start skipped; config unavailable: %s", exc)
        return

    # Run recording recovery before starting services
    try:
        recording_path = cfg.recording_output_path
        _last_recovery_result = recover_recordings(recording_path)
        if _last_recovery_result.get("recovered") or _last_recovery_result.get("corrupt"):
            logger.info("Startup recovery: %s", _last_recovery_result["summary"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("Recording recovery failed: %s", exc)

    # Always start storage manager (cleanup runs regardless of camera state)
    _start_storage_manager()

    # Start GPS reader + trip tracker (non-fatal if hardware absent)
    _start_gps_and_trip()

    if cfg.dashcam_auto_start_camera:
        result = _start_camera()
        logger.info("Auto-start camera: %s", result.get("status"))

    if cfg.dashcam_auto_start_alpr:
        try:
            result = _start_live_alpr()
            logger.info("Auto-start ALPR: %s", result.get("status"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Auto-start ALPR failed: %s", exc)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Car Incident Logger — Web UI")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=5000, help="Port (default: 5000)")
    parser.add_argument("--debug", action="store_true", help="Enable Flask debug mode")
    args = parser.parse_args()

    logger.info("Web UI starting at http://%s:%d/", args.host, args.port)
    if not args.debug:
        _auto_start_dashcam_services()
    else:
        logger.info("Debug mode enabled; skipping auto-start to avoid Flask reloader duplicate camera opens")
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)
