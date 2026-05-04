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
from modules.config_manager import ConfigManager  # noqa: E402
from modules.plate_database import PlateDatabase  # noqa: E402

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
    return {"status": "started"}


def _stop_camera() -> dict:
    global _camera
    with _camera_lock:
        if _camera is None or not _camera.is_running:
            return {"status": "not_running"}
        _camera.stop()
        _camera = None
    logger.info("Camera preview stopped")
    return {"status": "stopped"}


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


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Car Incident Logger — Web UI")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=5000, help="Port (default: 5000)")
    parser.add_argument("--debug", action="store_true", help="Enable Flask debug mode")
    args = parser.parse_args()

    logger.info("Web UI starting at http://%s:%d/", args.host, args.port)
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)
