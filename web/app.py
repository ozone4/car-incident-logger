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
from modules.alpr_runner import ALPRRunner  # noqa: E402
from modules.config_manager import ConfigManager  # noqa: E402
from modules.dashcam import DashcamRecorder  # noqa: E402
from modules.health_monitor import HealthMonitor  # noqa: E402
from modules.loop_recorder import LoopRecorder  # noqa: E402
from modules.incident_trigger import WebTrigger  # noqa: E402
from modules.multi_frame_voter import MultiFrameVoter  # noqa: E402
from modules.plate_database import PlateDatabase  # noqa: E402
from modules.rolling_buffer import RollingBuffer  # noqa: E402
from modules.storage_manager import StorageManager  # noqa: E402

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
    metadata = {
        "timestamp": result.get("timestamp", ""),
        "clip_path": result.get("clip_path"),
        "trigger_source": result.get("trigger_source", "web"),
        "plate": plate,
        "pre_roll_frames": result.get("pre_roll_frames", 0),
        "post_roll_frames": result.get("post_roll_frames", 0),
        "total_frames": result.get("total_frames", 0),
        "recent_sightings": result.get("recent_sightings", []),
    }
    try:
        db.add_incident(plate, metadata)
    except Exception as exc:
        logger.warning("Could not save dashcam incident to DB: %s", exc)


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
        "models_dir": cfg.alpr_models_dir,
        "yolo_model_path": cfg.alpr_yolo_model_path,
    }


def _set_alpr_state(**updates) -> None:
    with _alpr_lock:
        _alpr_state.update(updates)
        _alpr_state["updated_at"] = time.time()


def _format_elapsed(seconds: float) -> str:
    if seconds < 1:
        return "now"
    if seconds < 60:
        return f"{int(seconds)}s ago"
    return f"{int(seconds // 60)}m ago"


def _new_sighting(plate: str, det: dict, now: float) -> dict:
    return {
        "id": f"{plate}-{int(now * 1000)}",
        "plate": plate,
        "raw_text": (det.get("raw_text") or "").strip(),
        "confidence": float(det.get("confidence", 0.0)),
        "best_confidence": float(det.get("confidence", 0.0)),
        "bbox": det.get("bbox"),
        "source": det.get("source"),
        "first_seen": now,
        "last_seen": now,
        "seen_count": 1,
        "active": True,
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
        "source": sighting.get("source"),
        "first_seen": first_seen,
        "last_seen": last_seen,
        "seen_count": int(sighting.get("seen_count", 0)),
        "active": active,
        "status": "visible" if active else "gone",
        "age_seconds": round(now - first_seen, 1),
        "last_seen_seconds_ago": round(now - last_seen, 1),
        "last_seen_label": _format_elapsed(now - last_seen),
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
            sighting["best_confidence"] = max(
                float(sighting.get("best_confidence", 0.0)),
                float(det.get("confidence", 0.0)),
            )
            sighting["raw_text"] = (det.get("raw_text") or sighting.get("raw_text") or "").strip()
            sighting["bbox"] = det.get("bbox") or sighting.get("bbox")
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
    scan_interval = max(0.2, float(cfg.alpr_scan_interval))
    voter = MultiFrameVoter(min_votes=2)

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
        detections = runner.run_on_frame(frame)
        voter.add_frame(detections)
        latest = detections[0] if detections else None
        best = voter.get_best()

        with _alpr_lock:
            now = time.time()
            _active, _recent, sightings = _update_plate_sightings(detections, now)
            _alpr_state["frames_scanned"] = int(_alpr_state.get("frames_scanned", 0)) + 1
            _alpr_state["detections_seen"] = int(_alpr_state.get("detections_seen", 0)) + len(detections)
            _alpr_state["latest"] = latest
            _alpr_state["best"] = best
            _alpr_state["sightings"] = sightings
            _alpr_state["error"] = None
            _alpr_state["updated_at"] = now

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
    status["last_result"] = _dashcam.last_result if _dashcam else None
    status["last_error"] = _dashcam.last_error if _dashcam else None
    return jsonify(status)


@app.route("/dashcam/trigger", methods=["POST"])
def dashcam_trigger_api():
    """Trigger a dashcam incident capture from the web UI."""
    # Gather current ALPR state for metadata
    meta: dict = {}
    with _alpr_lock:
        best = _alpr_state.get("best")
        if best and best.get("plate"):
            meta["alpr_plate"] = best["plate"]
        sightings = _alpr_state.get("sightings", [])
        if sightings:
            meta["recent_sightings"] = sightings[:10]

    result = _web_trigger.fire(meta)
    status_code = 200 if result.get("ok") else 409
    return jsonify(result), status_code


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


@app.route("/storage/cleanup", methods=["POST"])
def storage_cleanup_api():
    """Trigger a manual storage cleanup pass."""
    if _storage_manager is None:
        _start_storage_manager()
    dry_run = request.args.get("dry_run", "false").lower() in ("true", "1", "yes")
    result = _storage_manager.run_cleanup(dry_run=dry_run)
    return jsonify(result)


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

def _auto_start_dashcam_services() -> None:
    """Start dashcam services for normal in-car operation.

    The manual dashboard buttons remain useful for recovery/testing, but the
    default app behavior should be dashcam-like: boot → camera/buffer armed →
    ALPR scanning if available.
    """
    try:
        cfg = _load_config()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Auto-start skipped; config unavailable: %s", exc)
        return

    # Always start storage manager (cleanup runs regardless of camera state)
    _start_storage_manager()

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
