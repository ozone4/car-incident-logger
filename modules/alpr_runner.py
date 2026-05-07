"""
alpr_runner.py — Phase 2 ALPR pipeline

Architecture:
    frames → vehicle detector → vehicle crop → plate detector → crop/preprocess
           → FastPlateOCR → normalize/validate → detections

Engines (all optional — missing deps or model files degrade gracefully):
  Vehicle detector: YOLO via ultralytics (yolov8n.pt, auto-downloaded)
  Plate detector:   YOLO via ultralytics (custom plate .pt model)
  OCR:              fast-plate-ocr (ONNX, optimized for NA plates)

Degradation paths:
  vehicle+plate+OCR  → full cascade (best — plate detected at realistic distance)
  vehicle+OCR only   → heuristic crop of vehicle lower region (no plate bbox)
  plate+OCR only     → direct plate scan on full frame
  OCR only           → not useful with FastPlateOCR (requires plate crops); returns []

Return format for run_on_frame():
    [
        {
            "plate":        "WJ1843",          # normalized uppercase
            "confidence":   0.81,              # combined det+OCR confidence
            "bbox":         [x1, y1, x2, y2], # pixel coords, or None (no plate detector)
            "vehicle_bbox": [x1, y1, x2, y2], # vehicle box when vehicle detector ran
            "vehicle_type": "car",             # COCO class label when vehicle detector ran
            "source":       "vehicle+yolo+fastocr",
            "raw_text":     "WJ1843",
            "corrected":    False,
        },
        ...
    ]
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Plate string utilities  (importable for tests without any heavy deps)
# ---------------------------------------------------------------------------

# OCR confusions in LETTER positions: digit that looks like a letter
_DIGIT_AS_LETTER: dict[str, str] = {
    "0": "O",
    "1": "I",
    "5": "S",
    "8": "B",
    "2": "Z",
    "6": "G",
}
# OCR confusions in DIGIT positions: letter that looks like a digit
_LETTER_AS_DIGIT: dict[str, str] = {
    "O": "0",
    "I": "1",
    "S": "5",
    "B": "8",
    "Z": "2",
    "G": "6",
}

# Valid plate: 2–9 chars, alphanumeric only
_PLATE_RE = re.compile(r"^[A-Z0-9]{2,9}$")


def normalize_plate(raw: str) -> str:
    """Uppercase and strip spaces, hyphens, and dots."""
    return re.sub(r"[\s\-\.]", "", raw.upper())


def apply_ocr_corrections(raw: str, plate_format: str = "auto") -> str:
    """
    Apply BC/North-American OCR correction rules.

    plate_format:
      "auto"    — apply LLLDDD rules if len == 6, else positional heuristic
      "LLLDDD"  — strict: positions 0-2 are letters, 3-5 are digits
      "none"    — no correction, return raw unchanged

    Corrections:
      Letter positions: 0→O  1→I  5→S  8→B  2→Z  6→G
      Digit  positions: O→0  I→1  S→5  B→8  Z→2  G→6
    """
    if not raw or plate_format == "none":
        return raw

    upper = raw.upper()

    if plate_format == "LLLDDD" or (plate_format == "auto" and len(upper) == 6):
        corrected = []
        for i, ch in enumerate(upper):
            if i < 3:
                corrected.append(_DIGIT_AS_LETTER.get(ch, ch))
            else:
                corrected.append(_LETTER_AS_DIGIT.get(ch, ch))
        return "".join(corrected)

    # Generic positional heuristic for other lengths
    n = len(upper)
    result = []
    for i, ch in enumerate(upper):
        is_early = i < n / 2
        if is_early and ch in _DIGIT_AS_LETTER:
            result.append(_DIGIT_AS_LETTER[ch])
        elif not is_early and ch in _LETTER_AS_DIGIT:
            result.append(_LETTER_AS_DIGIT[ch])
        else:
            result.append(ch)
    return "".join(result)


def validate_plate_candidate(plate: str) -> bool:
    """
    Return True if plate looks like a valid NA plate candidate:
      - 2–9 alphanumeric characters only
      - Not all the same character (e.g. "AAAAAA" rejected)
    """
    if not plate or not _PLATE_RE.match(plate):
        return False
    if len(set(plate)) == 1:
        return False
    return True


# ---------------------------------------------------------------------------
# Internal engine wrappers  (lazy-load heavy deps at initialize() time)
# ---------------------------------------------------------------------------

class _BaseDetector:
    """Extension point: subclass to swap the plate detector."""

    status: str = "uninitialized"
    error: str | None = None

    def initialize(self) -> bool:
        raise NotImplementedError

    def detect(self, frame: np.ndarray) -> list[dict]:
        """Return list of {bbox: [x1,y1,x2,y2], confidence: float}."""
        raise NotImplementedError


class _BaseRecognizer:
    """Extension point: subclass to swap the OCR engine."""

    status: str = "uninitialized"
    error: str | None = None

    def initialize(self) -> bool:
        raise NotImplementedError

    def recognize(self, image: np.ndarray) -> list[tuple[str, float]]:
        """Return list of (text, confidence) from a pre-cropped plate image."""
        raise NotImplementedError


class _YOLODetector(_BaseDetector):
    """YOLO plate detector via ultralytics (optional)."""

    def __init__(self, model_path: str, conf_threshold: float = 0.5, imgsz: int = 640) -> None:
        self.model_path = model_path
        self.conf_threshold = conf_threshold
        self._imgsz = imgsz
        self._model: Any = None
        self.status = "uninitialized"
        self.error: str | None = None
        self.last_raw_detections: list[dict] = []
        self.last_debug_candidates: list[dict] = []

    def initialize(self) -> bool:
        try:
            from ultralytics import YOLO  # noqa: PLC0415
        except ImportError:
            self.status = "unavailable"
            self.error = "ultralytics not installed — run: pip install ultralytics"
            return False

        path = Path(self.model_path)
        if not path.exists():
            self.status = "model_missing"
            self.error = (
                f"YOLO model not found: {path}. "
                "Download a plate-detection model and set alpr.yolo_model_path in config.yaml."
            )
            return False

        # Reject obviously corrupted downloads (real YOLO models are several MB)
        if path.stat().st_size < 10_000:
            self.status = "model_missing"
            self.error = (
                f"YOLO model file too small ({path.stat().st_size} bytes) — "
                "likely a failed download. Delete it and re-download."
            )
            return False

        try:
            self._model = YOLO(str(path))
            self.status = "ready"
            return True
        except Exception as exc:  # noqa: BLE001
            self.status = "error"
            self.error = str(exc)
            return False

    def detect(self, frame: np.ndarray) -> list[dict]:
        if self._model is None:
            return []
        try:
            results = self._model(frame, conf=0.01, verbose=False, imgsz=self._imgsz)
            detections: list[dict] = []
            raw: list[dict] = []
            for r in results:
                names = getattr(r, "names", {}) or {}
                for box in r.boxes:
                    x1, y1, x2, y2 = box.xyxy[0].tolist()
                    conf = float(box.conf[0])
                    cls_id = int(box.cls[0]) if getattr(box, "cls", None) is not None else None
                    label = names.get(cls_id, str(cls_id)) if cls_id is not None else None
                    item = {
                        "bbox": [int(x1), int(y1), int(x2), int(y2)],
                        "confidence": conf,
                        "class_id": cls_id,
                        "label": label,
                    }
                    raw.append(item)
                    if conf >= self.conf_threshold:
                        detections.append({"bbox": item["bbox"], "confidence": conf})
            self.last_raw_detections = raw
            return detections
        except Exception as exc:  # noqa: BLE001
            logger.warning("YOLO detection failed: %s", exc)
            return []


_VEHICLE_COCO_CLASSES: dict[int, str] = {
    2: "car",
    3: "motorcycle",
    5: "bus",
    7: "truck",
}


class _VehicleDetector:
    """General COCO-trained YOLO vehicle detector.

    Detects cars, trucks, motorcycles, and buses on the full frame so the plate
    detector can focus on high-resolution vehicle crops rather than the full image.
    ultralytics will auto-download yolov8n.pt (~6 MB) on first use when given a
    bare model name rather than a file path.
    """

    def __init__(self, model_path: str = "yolov8n.pt", conf_threshold: float = 0.3, imgsz: int = 640) -> None:
        self.model_path = model_path
        self.conf_threshold = conf_threshold
        self._imgsz = imgsz
        self._model: Any = None
        self.status = "uninitialized"
        self.error: str | None = None

    def initialize(self) -> bool:
        try:
            from ultralytics import YOLO  # noqa: PLC0415
        except ImportError:
            self.status = "unavailable"
            self.error = "ultralytics not installed"
            return False
        try:
            self._model = YOLO(self.model_path)
            self.status = "ready"
            return True
        except Exception as exc:  # noqa: BLE001
            self.status = "error"
            self.error = str(exc)
            return False

    def detect(self, frame: np.ndarray) -> list[dict]:
        """Return [{bbox, confidence, vehicle_type}] for all detected vehicles."""
        if self._model is None:
            return []
        try:
            results = self._model(frame, conf=self.conf_threshold, verbose=False, imgsz=self._imgsz)
            detections: list[dict] = []
            for r in results:
                for box in r.boxes:
                    cls_id = int(box.cls[0]) if getattr(box, "cls", None) is not None else None
                    if cls_id not in _VEHICLE_COCO_CLASSES:
                        continue
                    x1, y1, x2, y2 = box.xyxy[0].tolist()
                    detections.append({
                        "bbox": [int(x1), int(y1), int(x2), int(y2)],
                        "confidence": float(box.conf[0]),
                        "vehicle_type": _VEHICLE_COCO_CLASSES[cls_id],
                    })
            return detections
        except Exception as exc:  # noqa: BLE001
            logger.warning("Vehicle detection failed: %s", exc)
            return []


class _FastPlateOCRRecognizer(_BaseRecognizer):
    """OCR recognizer using fast-plate-ocr (ONNX, optimized for NA plates).

    Significantly faster than EasyOCR/PaddleOCR on CPU and better trained for
    North American plates (BC, Washington, California, etc.).
    The ONNX model (~10 MB) is downloaded automatically on first use.

    Install: pip install fast-plate-ocr
    """

    def __init__(self, model_name: str = "global-plates-mobile-vit-v2-model") -> None:
        self._model_name = model_name
        self._model: Any = None
        self.status = "uninitialized"
        self.error: str | None = None

    def initialize(self) -> bool:
        try:
            try:
                from fast_plate_ocr import LicensePlateRecognizer as _Recognizer  # noqa: PLC0415
            except ImportError:
                from fast_plate_ocr import ONNXPlateRecognizer as _Recognizer  # noqa: PLC0415
            self._model = _Recognizer(self._model_name)
            self.status = "ready"
            logger.info("FastPlateOCR ready (model: %s)", self._model_name)
            return True
        except ImportError:
            self.status = "unavailable"
            self.error = "fast-plate-ocr not installed — run: pip install fast-plate-ocr"
            return False
        except Exception as exc:  # noqa: BLE001
            self.status = "error"
            self.error = str(exc)
            logger.error("FastPlateOCR init failed: %s", exc)
            return False

    def recognize(self, image: np.ndarray) -> list[tuple[str, float]]:
        """Run OCR on a pre-cropped plate image. Returns [(text, confidence)] or []."""
        if self._model is None:
            return []
        try:
            import cv2  # noqa: PLC0415
            # fast-plate-ocr expects grayscale (1-channel); OpenCV frames are BGR
            gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if len(image.shape) == 3 else image
            results = self._model.run(gray)
            if not results:
                return []
            text = str(results[0]).strip() if results[0] else ""
            if not text:
                return []
            # fast-plate-ocr does not expose per-character confidence; use a fixed
            # score that reflects model-level accuracy on NA plates.
            # The combined confidence in _ocr_plate_box() still filters noise.
            return [(text, 0.78)]
        except Exception as exc:  # noqa: BLE001
            logger.warning("FastPlateOCR recognize error: %s", exc)
            return []


# ---------------------------------------------------------------------------
# Pre-processing helpers
# ---------------------------------------------------------------------------

def _preprocess_crop(
    frame: np.ndarray, x1: int, y1: int, x2: int, y2: int, pad_ratio: float = 0.08
) -> np.ndarray:
    """Crop a plate region with padding, upscale if too small for OCR."""
    h, w = frame.shape[:2]
    bw = max(1, x2 - x1)
    bh = max(1, y2 - y1)
    pad_x = max(5, int(bw * pad_ratio))
    pad_y = max(5, int(bh * pad_ratio))
    x1 = max(0, x1 - pad_x)
    y1 = max(0, y1 - pad_y)
    x2 = min(w, x2 + pad_x)
    y2 = min(h, y2 + pad_y)
    crop = frame[y1:y2, x1:x2]

    ch, cw = crop.shape[:2]
    if cw < 480 and cw > 0:
        try:
            import cv2  # noqa: PLC0415
            scale = 480 / cw
            interp = getattr(cv2, "INTER_CUBIC", 2)
            crop = cv2.resize(
                crop,
                (int(cw * scale), int(ch * scale)),
                interpolation=interp,
            )
        except (ImportError, Exception):  # noqa: BLE001
            pass
    return crop


def _extract_region(
    frame: np.ndarray, x1: int, y1: int, x2: int, y2: int, pad_ratio: float = 0.05
) -> tuple[np.ndarray, int, int]:
    """Crop a region with padding. Returns (crop, origin_x, origin_y).

    origin_x / origin_y are the top-left pixel of the crop in the original frame,
    used to remap bounding boxes detected inside the crop back to full-frame coords.
    Unlike _preprocess_crop this never upscales, so the coordinate mapping stays 1:1.
    """
    h, w = frame.shape[:2]
    bw = max(1, x2 - x1)
    bh = max(1, y2 - y1)
    pad_x = max(5, int(bw * pad_ratio))
    pad_y = max(5, int(bh * pad_ratio))
    cx1 = max(0, x1 - pad_x)
    cy1 = max(0, y1 - pad_y)
    cx2 = min(w, x2 + pad_x)
    cy2 = min(h, y2 + pad_y)
    return frame[cy1:cy2, cx1:cx2], cx1, cy1


def _normalize_and_correct(raw: str) -> tuple[str, bool]:
    """Normalize OCR text without character substitution.

    RAW OCR is proving more reliable than plate-format guessing in live tests.
    Keep safe cleanup only: uppercase, remove spaces/hyphens/dots.
    """
    return normalize_plate(raw), False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class ALPRRunner:
    """
    Automatic License Plate Recognition runner.

    Pipeline: frame → vehicle detector → plate detector → FastPlateOCR → return

    Degradation paths:
      vehicle+plate+OCR  → full cascade (best — realistic detection range)
      vehicle+OCR only   → heuristic crop of vehicle lower region (no plate bbox)
      plate+OCR only     → direct plate scan on full frame
      OCR only           → not useful (FastPlateOCR requires plate crops); returns []

    Constructor accepts a config dict with keys:
      confidence_threshold              float  (default 0.5)
      yolo_confidence_threshold         float  (default 0.1)
      yolo_imgsz                        int    (default 640)
      models_dir                        str    (default ./data/models)
      yolo_model_path                   str    (default <models_dir>/plate_detector.pt)
      vehicle_detection_enabled         bool   (default True)
      vehicle_model_path                str    (default yolov8n.pt, auto-downloaded)
      vehicle_confidence_threshold      float  (default 0.3)
      vehicle_imgsz                     int    (default 640)
      vehicle_fallback_to_fullframe     bool   (default True)
      ocr_fallback_when_no_detections   bool   (default True)
      fullframe_ocr_confidence_threshold float (default 0.60)
      fastocr_model_name                str    (default global-plates-mobile-vit-v2-model)
    """

    def __init__(
        self,
        config: dict | None = None,
        *,
        detector: _BaseDetector | None = None,
        recognizer: _BaseRecognizer | None = None,
        vehicle_detector: _VehicleDetector | None = None,
    ) -> None:
        cfg = config or {}
        self._conf_threshold = float(cfg.get("confidence_threshold", 0.5))
        self._yolo_conf_threshold = float(cfg.get("yolo_confidence_threshold", 0.1))
        self._yolo_imgsz = int(cfg.get("yolo_imgsz", 640))
        self._vehicle_fallback_to_fullframe = bool(cfg.get("vehicle_fallback_to_fullframe", True))
        self._ocr_fallback_when_no_detections = bool(cfg.get("ocr_fallback_when_no_detections", True))
        self._fullframe_ocr_threshold = float(cfg.get("fullframe_ocr_confidence_threshold", 0.60))
        models_dir = Path(cfg.get("models_dir", "./data/models"))
        yolo_path = cfg.get(
            "yolo_model_path", str(models_dir / "plate_detector.pt")
        )

        self._detector: _BaseDetector = detector or _YOLODetector(
            yolo_path, self._yolo_conf_threshold, imgsz=self._yolo_imgsz
        )
        fastocr_model = cfg.get("fastocr_model_name", "global-plates-mobile-vit-v2-model")
        self._recognizer: _BaseRecognizer = recognizer or _FastPlateOCRRecognizer(fastocr_model)

        if vehicle_detector is not None:
            self._vehicle_detector: _VehicleDetector | None = vehicle_detector
        elif cfg.get("vehicle_detection_enabled", True):
            vehicle_imgsz = int(cfg.get("vehicle_imgsz", 640))
            self._vehicle_detector = _VehicleDetector(
                model_path=cfg.get("vehicle_model_path", "yolov8n.pt"),
                conf_threshold=float(cfg.get("vehicle_confidence_threshold", 0.3)),
                imgsz=vehicle_imgsz,
            )
        else:
            self._vehicle_detector = None

        self._ready = False
        self._init_status: dict = {}

    def initialize(self) -> bool:
        """Initialize available engines. Returns True if at least OCR is ready."""
        det_ok = self._detector.initialize()
        ocr_ok = self._recognizer.initialize()

        veh_ok = False
        if self._vehicle_detector is not None:
            veh_ok = self._vehicle_detector.initialize()
            if not veh_ok:
                logger.warning(
                    "Vehicle detector unavailable (%s): %s",
                    self._vehicle_detector.status,
                    self._vehicle_detector.error or "",
                )

        self._init_status = {
            "detector": self._detector.status,
            "detector_error": self._detector.error,
            "ocr": self._recognizer.status,
            "ocr_error": self._recognizer.error,
            "vehicle_detector": self._vehicle_detector.status if self._vehicle_detector else "disabled",
            "vehicle_detector_error": self._vehicle_detector.error if self._vehicle_detector else None,
        }

        if not det_ok:
            logger.warning(
                "ALPR plate detector unavailable (%s): %s",
                self._detector.status,
                self._detector.error or "",
            )
        if not ocr_ok:
            logger.warning(
                "ALPR OCR unavailable (%s): %s",
                self._recognizer.status,
                self._recognizer.error or "",
            )

        self._ready = ocr_ok
        if veh_ok and det_ok and ocr_ok:
            logger.info("ALPR ready: vehicle detector + plate YOLO + FastPlateOCR")
        elif veh_ok and ocr_ok:
            logger.info("ALPR ready: vehicle detector + FastPlateOCR (no plate YOLO — heuristic crop mode)")
        elif det_ok and ocr_ok:
            logger.info("ALPR ready: plate YOLO + FastPlateOCR (no vehicle detector)")
        elif ocr_ok:
            logger.warning("ALPR: FastPlateOCR ready but no detectors — will return [] (needs plate crops)")
        else:
            logger.warning("ALPR: no engines available — run_on_frame() will return []")

        return self._ready

    @property
    def is_ready(self) -> bool:
        return self._ready

    def status_info(self) -> dict:
        """Return a dict describing engine states, suitable for UI/API display."""
        detector_ready = self._init_status.get("detector") == "ready"
        ocr_ready = self._init_status.get("ocr") == "ready"
        if detector_ready and ocr_ready:
            mode = "detector_ocr"
        elif ocr_ready:
            mode = "ocr_fallback"
        else:
            mode = "unavailable"
        return {"ready": self._ready, "mode": mode, **self._init_status}

    def run_on_frame(self, frame: np.ndarray) -> list[dict]:
        """
        Run ALPR on one BGR frame. Returns a list of detection dicts.
        Always returns a list (empty if not ready or nothing found).
        """
        if not self._ready:
            return []

        self.last_debug_candidates: list[dict] = []

        veh_ready = (
            self._vehicle_detector is not None
            and self._vehicle_detector.status == "ready"
        )
        det_ready = self._detector.status == "ready"

        # ── CASE 1: Full cascade — vehicle detector + plate YOLO + OCR ───────
        if veh_ready and det_ready:
            vehicles = self._vehicle_detector.detect(frame)
            results: list[dict] = []
            for vehicle in vehicles:
                vx1, vy1, vx2, vy2 = vehicle["bbox"]
                veh_crop, veh_ox, veh_oy = _extract_region(frame, vx1, vy1, vx2, vy2, pad_ratio=0.05)
                for box in self._detector.detect(veh_crop):
                    px1, py1, px2, py2 = box["bbox"]
                    full_bbox = [veh_ox + px1, veh_oy + py1, veh_ox + px2, veh_oy + py2]
                    extra = {
                        "source": "vehicle+yolo+fastocr",
                        "vehicle_type": vehicle["vehicle_type"],
                        "vehicle_bbox": vehicle["bbox"],
                    }
                    results.extend(self._ocr_plate_box(frame, box, full_bbox=full_bbox, extra=extra))

            if not vehicles and self._vehicle_fallback_to_fullframe:
                results.extend(self._direct_plate_scan(frame))

            if not results and self._ocr_fallback_when_no_detections:
                results.extend(self._fullframe_ocr(frame, reason="vehicle_or_plate_detector_empty"))

            return results

        # ── CASE 2: Vehicle detector + OCR only (no plate YOLO) ──────────────
        # Use a heuristic: plate is typically in the lower portion of the vehicle.
        if veh_ready and not det_ready:
            vehicles = self._vehicle_detector.detect(frame)
            if vehicles:
                results = self._fullframe_ocr(frame, reason="no_plate_detector", vehicles=vehicles)
                if results:
                    return results
            # No vehicles found or heuristic returned nothing — can't help
            return []

        # ── CASE 3: Plate YOLO + OCR (no vehicle detector) ───────────────────
        if det_ready:
            results = self._direct_plate_scan(frame)
            if not results and self._ocr_fallback_when_no_detections:
                results.extend(self._fullframe_ocr(frame, reason="plate_detector_empty"))
            return results

        # ── CASE 4: OCR only — FastPlateOCR needs plate crops, can't help ────
        return []

    def _ocr_plate_box(
        self,
        frame: np.ndarray,
        box: dict,
        full_bbox: list[int] | None = None,
        extra: dict | None = None,
    ) -> list[dict]:
        """Run OCR on a single plate detection box and return validated detection dicts."""
        bbox = full_bbox if full_bbox is not None else box["bbox"]
        x1, y1, x2, y2 = bbox
        det_conf = box["confidence"]
        crop = _preprocess_crop(frame, x1, y1, x2, y2, pad_ratio=0.18)
        texts = self._recognizer.recognize(crop)
        if not texts:
            self.last_debug_candidates.append({
                "stage": "ocr_empty",
                "bbox": bbox,
                "detector_confidence": round(det_conf, 3),
                "crop_size": [int(crop.shape[1]), int(crop.shape[0])],
                "reason": "OCR returned no text for this crop",
            })
        results = []
        for raw_text, ocr_conf in texts:
            plate, corrected = _normalize_and_correct(raw_text)
            valid = validate_plate_candidate(plate)
            # Normalize detector confidence against the configured threshold so that
            # low-confidence YOLO boxes on small/distant plates don't dominate.
            det_score = min(det_conf / max(self._yolo_conf_threshold, 0.01), 1.0)
            combined = ocr_conf * 0.8 + det_score * 0.2
            debug = {
                "stage": "candidate",
                "bbox": bbox,
                "detector_confidence": round(det_conf, 3),
                "raw_text": raw_text,
                "plate": plate,
                "corrected": corrected,
                "ocr_confidence": round(float(ocr_conf), 3),
                "combined_confidence": round(combined, 3),
                "valid_plate": valid,
            }
            if not valid:
                debug["reason"] = "plate failed validation"
                self.last_debug_candidates.append(debug)
                continue
            if combined < self._conf_threshold:
                debug["reason"] = "combined confidence below threshold"
                self.last_debug_candidates.append(debug)
                continue
            debug["accepted"] = True
            self.last_debug_candidates.append(debug)
            det: dict = {
                "plate": plate,
                "confidence": round(combined, 3),
                "bbox": bbox,
                "frame_w": frame.shape[1],
                "frame_h": frame.shape[0],
                "source": "yolo+fastocr",
                "raw_text": raw_text,
                "corrected": corrected,
            }
            if extra:
                det.update(extra)
            results.append(det)
        return results

    def _direct_plate_scan(self, frame: np.ndarray) -> list[dict]:
        """Run the plate detector directly on the full frame."""
        results = []
        for box in self._detector.detect(frame):
            results.extend(self._ocr_plate_box(frame, box))
        return results

    def _fullframe_ocr(
        self,
        frame: np.ndarray,
        reason: str = "no_plate_detector",
        vehicles: list[dict] | None = None,
    ) -> list[dict]:
        """Fallback OCR when plate detection is unavailable.

        FastPlateOCR requires tight plate crops — it cannot read full frames.
        When vehicle detections are available, crop the lower portion of each
        vehicle (where the plate typically lives) and run OCR on that region.
        Without vehicle crops there is nothing useful to do.
        """
        if self._recognizer.status != "ready":
            return []

        results = []
        threshold = max(min(self._fullframe_ocr_threshold, 0.95), 0.0)
        h, w = frame.shape[:2]

        if vehicles:
            for vehicle in vehicles:
                vx1, vy1, vx2, vy2 = vehicle["bbox"]
                vh = vy2 - vy1
                vw = vx2 - vx1
                # Plates appear in the lower ~40% of the vehicle bounding box.
                # Trim horizontal margins slightly to exclude wheel arches.
                py1 = vy1 + int(vh * 0.55)
                px_margin = max(0, int(vw * 0.10))
                plate_region = frame[
                    py1:vy2,
                    max(0, vx1 + px_margin):min(w, vx2 - px_margin),
                ]
                if plate_region.size == 0:
                    continue
                texts = self._recognizer.recognize(plate_region)
                for raw_text, ocr_conf in texts:
                    plate, corrected = _normalize_and_correct(raw_text)
                    if not validate_plate_candidate(plate):
                        continue
                    if ocr_conf < threshold:
                        continue
                    results.append({
                        "plate": plate,
                        "confidence": round(ocr_conf * 0.7, 3),
                        "bbox": None,
                        "vehicle_bbox": vehicle["bbox"],
                        "vehicle_type": vehicle.get("vehicle_type"),
                        "frame_w": w,
                        "frame_h": h,
                        "source": "vehicle+fastocr_heuristic",
                        "raw_text": raw_text,
                        "corrected": corrected,
                    })

        if not results:
            self.last_debug_candidates.append({
                "stage": "fullframe_ocr_skipped",
                "reason": reason,
                "vehicles_tried": len(vehicles) if vehicles else 0,
                "note": "FastPlateOCR requires plate crops; no tight crops available",
            })

        return results
