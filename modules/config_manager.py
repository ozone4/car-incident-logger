"""
config_manager.py — Loads config.yaml and provides typed access to all settings.
"""

import os
import logging
import yaml
from pathlib import Path
from typing import Any, Optional


class ConfigManager:
    def __init__(self, config_path: str = "config.yaml"):
        self._path = Path(config_path)
        self._data: dict = {}
        self.load()

    def load(self) -> None:
        if not self._path.exists():
            raise FileNotFoundError(f"Config file not found: {self._path}")
        with open(self._path, "r") as f:
            self._data = yaml.safe_load(f)

    def get(self, *keys: str, default: Any = None) -> Any:
        """Dot-path access: get('camera', 'fps') → self._data['camera']['fps']"""
        node = self._data
        for k in keys:
            if not isinstance(node, dict) or k not in node:
                return default
            node = node[k]
        return node

    # ── Typed convenience properties ─────────────────────────────────────────

    @property
    def camera_device_index(self) -> int:
        return int(self.get("camera", "device_index", default=0))

    @property
    def camera_width(self) -> int:
        return int(self.get("camera", "resolution", "width", default=1920))

    @property
    def camera_height(self) -> int:
        return int(self.get("camera", "resolution", "height", default=1080))

    @property
    def camera_fps(self) -> int:
        return int(self.get("camera", "fps", default=30))

    @property
    def camera_format(self) -> str:
        return str(self.get("camera", "format", default="MJPG"))

    @property
    def buffer_duration(self) -> int:
        return int(self.get("buffer", "duration_seconds", default=45))

    @property
    def audio_enabled(self) -> bool:
        return bool(self.get("audio", "enabled", default=True))

    @property
    def audio_device_index(self) -> Optional[int]:
        val = self.get("audio", "device_index", default=None)
        return int(val) if val is not None else None

    @property
    def audio_sample_rate(self) -> int:
        return int(self.get("audio", "sample_rate", default=16000))

    @property
    def audio_channels(self) -> int:
        return int(self.get("audio", "channels", default=1))

    @property
    def transcription_model(self) -> str:
        return str(self.get("transcription", "model", default="base.en"))

    @property
    def transcription_device(self) -> str:
        return str(self.get("transcription", "device", default="cpu"))

    @property
    def transcription_compute_type(self) -> str:
        return str(self.get("transcription", "compute_type", default="int8"))

    @property
    def button_mode(self) -> str:
        return str(self.get("button", "mode", default="keyboard"))

    @property
    def button_key(self) -> str:
        return str(self.get("button", "key", default="space"))

    @property
    def button_gpio_pin(self) -> int:
        return int(self.get("button", "gpio_pin", default=17))

    @property
    def button_gpio_pull(self) -> str:
        return str(self.get("button", "gpio_pull", default="up"))

    @property
    def storage_base_path(self) -> Path:
        return Path(self.get("storage", "base_path", default="./data"))

    @property
    def storage_max_age_days(self) -> int:
        return int(self.get("storage", "max_incident_age_days", default=90))

    @property
    def storage_max_recording_age_days(self) -> float:
        return float(self.get("storage", "max_recording_age_days", default=7))

    @property
    def storage_min_free_space_gb(self) -> float:
        return float(self.get("storage", "min_free_space_gb", default=2))

    @property
    def storage_cleanup_interval_seconds(self) -> float:
        return float(self.get("storage", "cleanup_interval_seconds", default=300))

    @property
    def alpr_enabled(self) -> bool:
        return bool(self.get("alpr", "enabled", default=False))

    @property
    def alpr_confidence_threshold(self) -> float:
        return float(self.get("alpr", "confidence_threshold", default=0.5))

    @property
    def alpr_yolo_confidence_threshold(self) -> float:
        return float(self.get("alpr", "yolo_confidence_threshold", default=0.1))

    @property
    def alpr_yolo_imgsz(self) -> int:
        return int(self.get("alpr", "yolo_imgsz", default=1280))

    @property
    def alpr_vehicle_detection_enabled(self) -> bool:
        return bool(self.get("alpr", "vehicle_detection_enabled", default=True))

    @property
    def alpr_vehicle_model_path(self) -> str:
        return str(self.get("alpr", "vehicle_model_path", default="yolov8n.pt"))

    @property
    def alpr_vehicle_confidence_threshold(self) -> float:
        return float(self.get("alpr", "vehicle_confidence_threshold", default=0.3))

    @property
    def alpr_vehicle_fallback_to_fullframe(self) -> bool:
        return bool(self.get("alpr", "vehicle_fallback_to_fullframe", default=True))

    @property
    def alpr_vehicle_imgsz(self) -> int:
        return int(self.get("alpr", "vehicle_imgsz", default=640))

    @property
    def alpr_ocr_fallback_when_no_detections(self) -> bool:
        return bool(self.get("alpr", "ocr_fallback_when_no_detections", default=True))

    @property
    def alpr_fullframe_ocr_confidence_threshold(self) -> float:
        return float(self.get("alpr", "fullframe_ocr_confidence_threshold", default=0.60))

    @property
    def alpr_scan_interval(self) -> float:
        return float(self.get("alpr", "scan_interval_seconds", default=2.0))

    @property
    def alpr_yolo_model_path(self) -> str:
        default = str(self.storage_base_path / "models" / "plate_detector.pt")
        return str(self.get("alpr", "yolo_model_path", default=default))

    @property
    def alpr_models_dir(self) -> str:
        return str(self.get("alpr", "models_dir", default=str(self.storage_base_path / "models")))

    @property
    def known_vehicle_profiles(self) -> list[dict]:
        profiles = self.get("known_vehicles", "profiles", default=[])
        return profiles if isinstance(profiles, list) else []

    # ── Dashcam ──────────────────────────────────────────────────────────────

    @property
    def dashcam_pre_roll_seconds(self) -> float:
        return float(self.get("dashcam", "pre_roll_seconds", default=30.0))

    @property
    def dashcam_post_roll_seconds(self) -> float:
        return float(self.get("dashcam", "post_roll_seconds", default=5.0))

    @property
    def dashcam_output_path(self) -> Path:
        return Path(self.get("dashcam", "output_path", default="./data/dashcam"))

    @property
    def dashcam_auto_start_camera(self) -> bool:
        return bool(self.get("dashcam", "auto_start_camera", default=True))

    @property
    def dashcam_auto_start_alpr(self) -> bool:
        return bool(self.get("dashcam", "auto_start_alpr", default=True))

    # ── Recording ────────────────────────────────────────────────────────────

    @property
    def recording_enabled(self) -> bool:
        return bool(self.get("recording", "enabled", default=True))

    @property
    def recording_segment_duration(self) -> int:
        return int(self.get("recording", "segment_duration_seconds", default=60))

    @property
    def recording_output_path(self) -> Path:
        return Path(self.get("recording", "output_path", default="./data/recordings"))

    # ── Overlay ─────────────────────────────────────────────────────────────

    @property
    def overlay_enabled(self) -> bool:
        return bool(self.get("overlay", "enabled", default=True))

    @property
    def overlay_position(self) -> str:
        return str(self.get("overlay", "position", default="bottom-left"))

    @property
    def overlay_font_scale(self) -> float:
        return float(self.get("overlay", "font_scale", default=0.7))

    @property
    def overlay_color(self) -> list:
        c = self.get("overlay", "color", default=[255, 255, 255])
        return list(c) if isinstance(c, (list, tuple)) else [255, 255, 255]

    @property
    def overlay_background(self) -> bool:
        return bool(self.get("overlay", "background", default=True))

    @property
    def notifier_chime_enabled(self) -> bool:
        return bool(self.get("notifier", "chime_enabled", default=False))

    @property
    def notifier_chime_file(self) -> str:
        return str(self.get("notifier", "chime_file", default=""))

    @property
    def notifier_console_alerts(self) -> bool:
        return bool(self.get("notifier", "console_alerts", default=True))

    @property
    def log_level(self) -> str:
        return str(self.get("logging", "level", default="INFO"))

    @property
    def log_file(self) -> str:
        return str(self.get("logging", "file", default="./data/logs/system.log"))

    @property
    def log_max_bytes(self) -> int:
        return int(self.get("logging", "max_bytes", default=10485760))

    @property
    def log_backup_count(self) -> int:
        return int(self.get("logging", "backup_count", default=3))

    @property
    def admin_token(self) -> str:
        """Shared secret for destructive routes. Empty string disables auth."""
        return str(self.get("security", "admin_token", default="") or "")


def setup_logging(config: ConfigManager) -> None:
    """Configure root logger from config settings."""
    log_file = Path(config.log_file)
    log_file.parent.mkdir(parents=True, exist_ok=True)

    level = getattr(logging, config.log_level.upper(), logging.INFO)

    from logging.handlers import RotatingFileHandler

    handlers = [
        logging.StreamHandler(),
        RotatingFileHandler(
            log_file,
            maxBytes=config.log_max_bytes,
            backupCount=config.log_backup_count,
        ),
    ]

    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        handlers=handlers,
    )
