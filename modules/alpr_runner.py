"""
alpr_runner.py — Phase 2 ALPR pipeline

Architecture:
    frames → plate detector → crop/preprocess → OCR → normalize/validate → detections

Engines (all optional — missing deps or model files degrade gracefully):
  Detector:  YOLO via ultralytics  (best for moving vehicles, recommended)
  OCR:       PaddleOCR             (recommended) or whole-frame fallback

Extension: swap detector/OCR by subclassing _BaseDetector / _BaseRecognizer
and passing instances to ALPRRunner.__init__().

Return format for run_on_frame():
    [
        {
            "plate":      "WJ1843",          # normalized uppercase
            "confidence": 0.81,              # combined det+OCR confidence
            "bbox":       [x1, y1, x2, y2], # pixel coords, or None (full-frame)
            "source":     "yolo+paddle",     # which engines produced this
            "raw_text":   "WJ1O43",         # OCR output before correction
            "corrected":  True,              # whether OCR corrections were applied
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


def _extract_ocr_texts(result: Any) -> list[tuple[str, float]]:
    """
    Extract (text, confidence) pairs from PaddleOCR v2/v3 result shapes.

    Known shapes include:
      v2: [[[[x,y],...], ["ABC123", 0.91]], ...]
      v3: [{"rec_texts": ["ABC123"], "rec_scores": [0.91], ...}]
    """
    texts: list[tuple[str, float]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            rec_texts = node.get("rec_texts") or node.get("texts")
            rec_scores = node.get("rec_scores") or node.get("scores")
            if isinstance(rec_texts, list) and isinstance(rec_scores, list):
                for text, score in zip(rec_texts, rec_scores):
                    try:
                        texts.append((str(text), float(score)))
                    except (TypeError, ValueError):
                        continue
            text = node.get("text") or node.get("transcription")
            score = node.get("confidence") or node.get("score")
            if text is not None and score is not None:
                try:
                    texts.append((str(text), float(score)))
                except (TypeError, ValueError):
                    pass
            for value in node.values():
                walk(value)
            return

        if isinstance(node, (list, tuple)):
            if len(node) >= 2:
                if isinstance(node[0], str):
                    try:
                        texts.append((node[0], float(node[1])))
                    except (TypeError, ValueError):
                        pass
                second = node[1]
                if isinstance(second, (list, tuple)) and len(second) >= 2 and isinstance(second[0], str):
                    try:
                        texts.append((second[0], float(second[1])))
                    except (TypeError, ValueError):
                        pass
            for item in node:
                walk(item)

    walk(result)

    # De-dupe exact repeats while preserving order.
    seen: set[tuple[str, float]] = set()
    unique: list[tuple[str, float]] = []
    for item in texts:
        if item not in seen:
            seen.add(item)
            unique.append(item)
    return unique


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
        """Return list of (text, confidence) from image."""
        raise NotImplementedError


class _YOLODetector(_BaseDetector):
    """YOLO plate detector via ultralytics (optional)."""

    def __init__(self, model_path: str, conf_threshold: float = 0.5, imgsz: int = 1280) -> None:
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
            # Ask YOLO for low-confidence raw boxes, then apply our own threshold.
            # This makes diagnostics much easier when a model is almost-but-not-quite
            # detecting a plate.
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

    def __init__(self, model_path: str = "yolov8n.pt", conf_threshold: float = 0.3, imgsz: int = 1280) -> None:
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


class _PaddleOCRRecognizer(_BaseRecognizer):
    """OCR recognizer for plate crops: EasyOCR preferred, PaddleOCR fallback.

    PaddleOCR is accurate when it works, but recent Windows CPU/oneDNN builds can
    fail at runtime. EasyOCR has been more reliable on Owen's Windows test box,
    so use it first when installed.
    """

    def __init__(self) -> None:
        self._ocr: Any = None
        self._easyocr: Any = None
        self._paddle_runtime_failed = False
        self.status = "uninitialized"
        self.error: str | None = None
        self.fallback_status = "uninitialized"
        self.fallback_error: str | None = None

    def initialize(self) -> bool:
        easy_ok = self._initialize_easyocr_fallback()

        try:
            from paddleocr import PaddleOCR  # noqa: PLC0415
        except ImportError as exc:
            self.status = "unavailable"
            self.error = f"paddleocr import failed: {exc}"
            return easy_ok

        # PaddleOCR changed constructor args across major versions. Try newest-safe
        # forms first, then older v2-style args. We intentionally do not pass
        # det=False here; PaddleOCR 3.x rejects it, and plate crops are small enough
        # that text detection inside the crop is acceptable.
        init_attempts = [
            {"lang": "en", "use_textline_orientation": True},  # PaddleOCR 3.x
            {"lang": "en"},
            {"use_angle_cls": True, "lang": "en", "show_log": False},  # PaddleOCR 2.x
        ]
        errors: list[str] = []
        for kwargs in init_attempts:
            try:
                self._ocr = PaddleOCR(**kwargs)
                self.status = "ready"
                self.error = None
                return True
            except Exception as exc:  # noqa: BLE001
                errors.append(str(exc))

        self.status = "error"
        self.error = errors[-1] if errors else "PaddleOCR initialization failed"
        return easy_ok

    def _initialize_easyocr_fallback(self) -> bool:
        try:
            import easyocr  # noqa: PLC0415
        except ImportError as exc:
            self.fallback_status = "unavailable"
            self.fallback_error = f"easyocr import failed: {exc} — run: pip install easyocr in the active venv"
            return self.status == "ready"

        try:
            self._easyocr = easyocr.Reader(["en"], gpu=False, verbose=False)
            self.fallback_status = "ready"
            self.fallback_error = None
            return True
        except Exception as exc:  # noqa: BLE001
            self.fallback_status = "error"
            self.fallback_error = str(exc)
            return self.status == "ready"

    def recognize(self, image: np.ndarray) -> list[tuple[str, float]]:
        # Prefer EasyOCR for cropped plates on Windows. It avoids PaddleOCR's
        # intermittent oneDNN runtime failures and was validated on the live test.
        if self._easyocr is not None:
            try:
                easy_results = self._easyocr.readtext(image, detail=1, paragraph=False)
                texts: list[tuple[str, float]] = []
                for item in easy_results:
                    if isinstance(item, (list, tuple)) and len(item) >= 3:
                        texts.append((str(item[1]), float(item[2])))
                if texts:
                    return texts
            except Exception as exc:  # noqa: BLE001
                logger.warning("EasyOCR failed: %s", exc)

        if self._ocr is not None and not self._paddle_runtime_failed:
            call_attempts = [
                lambda: self._ocr.ocr(image, cls=True),
                lambda: self._ocr.ocr(image),
                lambda: self._ocr.predict(image),
            ]
            last_error: Exception | None = None
            for call in call_attempts:
                try:
                    texts = _extract_ocr_texts(call())
                    if texts:
                        return texts
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
                    continue

            if last_error:
                # PaddleOCR can fail at runtime on some Windows/oneDNN builds.
                # Disable it for the rest of this process and use EasyOCR fallback
                # when available instead of logging the same noisy warning every frame.
                self._paddle_runtime_failed = True
                self.status = "runtime_error"
                self.error = str(last_error)
                if self._easyocr is None:
                    logger.warning("PaddleOCR recognition failed: %s", last_error)
                else:
                    logger.info("PaddleOCR failed once; using EasyOCR fallback for this session")

        return []


# ---------------------------------------------------------------------------
# Pre-processing helper
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
            interp = getattr(cv2, "INTER_CUBIC", 2)  # 2 is the cv2.INTER_CUBIC int value
            crop = cv2.resize(
                crop,
                (int(cw * scale), int(ch * scale)),
                interpolation=interp,
            )
        except (ImportError, Exception):  # noqa: BLE001
            pass  # cv2 missing or mocked — return crop as-is

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


def _ocr_crop_variants(crop: np.ndarray) -> list[np.ndarray]:
    """Return original + enhanced crop variants for OCR robustness."""
    variants = [crop]
    try:
        import cv2  # noqa: PLC0415
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if len(crop.shape) == 3 else crop
        variants.append(gray)
        variants.append(cv2.convertScaleAbs(gray, alpha=1.6, beta=8))
        variants.append(cv2.adaptiveThreshold(
            gray,
            255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY,
            31,
            5,
        ))
        import numpy as np  # noqa: PLC0415
        kernel = np.array([[0, -1, 0], [-1, 5, -1], [0, -1, 0]])
        variants.append(cv2.filter2D(gray, -1, kernel))
    except Exception:  # noqa: BLE001
        pass
    return variants


def _fullframe_ocr_variants(frame: np.ndarray) -> list[tuple[str, np.ndarray]]:
    """Return OCR inputs for whole-frame fallback, biased toward small test plates.

    Whole-frame OCR can miss a small plate/sign when it only sees the original
    1080p image. Try a few cheap, deterministic variants before giving up:
    original, upscaled original, and upper-frame crops where plates/signs often
    appear during bench/garage testing.
    """
    h, _w = frame.shape[:2]
    upper = frame[: max(1, int(h * 0.65)), :]
    variants: list[tuple[str, np.ndarray]] = [("full", frame), ("upper_65pct", upper)]
    try:
        import cv2  # noqa: PLC0415

        h, w = frame.shape[:2]
        variants.append(("full_2x", cv2.resize(frame, (w * 2, h * 2), interpolation=cv2.INTER_CUBIC)))
        uh, uw = upper.shape[:2]
        variants.append(("upper_65pct_2x", cv2.resize(upper, (uw * 2, uh * 2), interpolation=cv2.INTER_CUBIC)))
    except Exception:  # noqa: BLE001
        pass
    return variants


def _normalize_and_correct(raw: str) -> tuple[str, bool]:
    """Normalize OCR text without character substitution.

    RAW OCR is proving more reliable than plate-format guessing in live tests.
    Keep safe cleanup only: uppercase, remove spaces/hyphens/dots. Do not convert
    ambiguous characters like 6↔G, 0↔O, 5↔S, etc.
    """
    return normalize_plate(raw), False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class ALPRRunner:
    """
    Automatic License Plate Recognition runner.

    Pipeline: frame → detector → crop → OCR → normalize/validate → return

    Degrades gracefully:
      YOLO + PaddleOCR  → full pipeline (best accuracy for moving vehicles)
      PaddleOCR only    → whole-frame OCR (lower accuracy, more false positives)
      neither           → always returns [] with a logged warning

    Custom engines:
      Pass detector= / recognizer= keyword args with objects that subclass
      _BaseDetector / _BaseRecognizer to swap engines without changing this class.

    Constructor accepts a config dict with keys:
      confidence_threshold          float  (default 0.5)  final plate confidence
      yolo_confidence_threshold     float  (default 0.1)  plate detector box threshold
      yolo_imgsz                    int    (default 1280) YOLO input size (multiple of 32)
      models_dir                    str    (default ./data/models)
      yolo_model_path               str    (default <models_dir>/plate_detector.pt)
      vehicle_detection_enabled     bool   (default True) run vehicle detector first
      vehicle_model_path            str    (default yolov8n.pt, auto-downloaded)
      vehicle_confidence_threshold  float  (default 0.3)  vehicle detector threshold
      vehicle_fallback_to_fullframe bool   (default True) direct plate scan when no vehicles found
      ocr_fallback_when_no_detections bool (default True) OCR whole frame if YOLO finds no plate boxes
      fullframe_ocr_confidence_threshold float (default 0.60) OCR fallback threshold
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
        self._yolo_imgsz = int(cfg.get("yolo_imgsz", 1280))
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
        self._recognizer: _BaseRecognizer = recognizer or _PaddleOCRRecognizer()

        if vehicle_detector is not None:
            self._vehicle_detector: _VehicleDetector | None = vehicle_detector
        elif cfg.get("vehicle_detection_enabled", True):
            self._vehicle_detector = _VehicleDetector(
                model_path=cfg.get("vehicle_model_path", "yolov8n.pt"),
                conf_threshold=float(cfg.get("vehicle_confidence_threshold", 0.3)),
                imgsz=self._yolo_imgsz,
            )
        else:
            self._vehicle_detector = None

        self._ready = False
        self._init_status: dict = {}

    def initialize(self) -> bool:
        """
        Initialize available engines. Returns True if at least OCR is ready.
        Safe to call even if all deps are missing.
        """
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
            "ocr_fallback": getattr(self._recognizer, "fallback_status", None),
            "ocr_fallback_error": getattr(self._recognizer, "fallback_error", None),
            "vehicle_detector": self._vehicle_detector.status if self._vehicle_detector else "disabled",
            "vehicle_detector_error": self._vehicle_detector.error if self._vehicle_detector else None,
        }

        if not det_ok:
            logger.warning(
                "ALPR detector unavailable (%s): %s",
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
            logger.info("ALPR ready: vehicle detector + plate YOLO + PaddleOCR")
        elif det_ok and ocr_ok:
            logger.info("ALPR ready: plate YOLO + PaddleOCR (no vehicle detector)")
        elif ocr_ok:
            logger.info("ALPR ready: whole-frame PaddleOCR only (no plate detector)")
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

        # ── Two-stage vehicle → plate cascade ─────────────────────────────────
        # Detect vehicles first, then focus the plate detector on each vehicle crop.
        # A plate inside a 300px-wide vehicle crop appears ~5–10× larger to the plate
        # detector than it would in the full frame, enabling detection at much greater range.
        if (
            self._vehicle_detector is not None
            and self._vehicle_detector.status == "ready"
            and self._detector.status == "ready"
        ):
            vehicles = self._vehicle_detector.detect(frame)
            results: list[dict] = []
            for vehicle in vehicles:
                vx1, vy1, vx2, vy2 = vehicle["bbox"]
                veh_crop, veh_ox, veh_oy = _extract_region(frame, vx1, vy1, vx2, vy2, pad_ratio=0.05)
                for box in self._detector.detect(veh_crop):
                    # Remap plate coords from vehicle-crop space → full-frame space.
                    px1, py1, px2, py2 = box["bbox"]
                    full_bbox = [veh_ox + px1, veh_oy + py1, veh_ox + px2, veh_oy + py2]
                    extra = {
                        "source": "vehicle+yolo+paddle",
                        "vehicle_type": vehicle["vehicle_type"],
                        "vehicle_bbox": vehicle["bbox"],
                    }
                    results.extend(self._ocr_plate_box(frame, box, full_bbox=full_bbox, extra=extra))

            if not vehicles and self._vehicle_fallback_to_fullframe:
                results.extend(self._direct_plate_scan(frame))

            if not results and self._ocr_fallback_when_no_detections:
                results.extend(self._fullframe_ocr(frame, reason="vehicle_or_plate_detector_empty"))

            return results

        # ── Direct plate scan (no vehicle detector configured or ready) ────────
        if self._detector.status == "ready":
            results = self._direct_plate_scan(frame)
            if not results and self._ocr_fallback_when_no_detections:
                results.extend(self._fullframe_ocr(frame, reason="plate_detector_empty"))
            return results

        # ── Whole-frame OCR fallback ───────────────────────────────────────────
        return self._fullframe_ocr(frame)

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
        texts: list[tuple[str, float]] = []
        for idx, variant in enumerate(_ocr_crop_variants(crop)):
            texts = self._recognizer.recognize(variant)
            if texts:
                self.last_debug_candidates.append({
                    "stage": "ocr_variant_used",
                    "bbox": bbox,
                    "variant": idx,
                    "texts": [{"text": t, "confidence": round(float(c), 3)} for t, c in texts],
                })
                break
        if not texts:
            ch, cw = crop.shape[:2]
            self.last_debug_candidates.append({
                "stage": "ocr_empty",
                "bbox": bbox,
                "detector_confidence": round(det_conf, 3),
                "crop_size": [int(cw), int(ch)],
                "variants_tried": len(_ocr_crop_variants(crop)),
                "reason": "OCR returned no text for this crop",
            })
        results = []
        for raw_text, ocr_conf in texts:
            plate, corrected = _normalize_and_correct(raw_text)
            valid = validate_plate_candidate(plate)
            # Detector confidence from small/close/printed plates can be low even
            # when the crop is correct. Once YOLO has found a plausible plate box,
            # weight OCR more heavily and normalize the detector contribution
            # against the configured YOLO threshold instead of raw 0–1 confidence.
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
                "source": "yolo+paddle",
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

    def _fullframe_ocr(self, frame: np.ndarray, reason: str = "no_plate_detector") -> list[dict]:
        """OCR-only fallback when plate detection returns nothing.

        This is intentionally looser than the detector+OCR path. Real installs can
        show small/distant plates, printed test plates, or partial garage-test views
        where a vehicle detector has nothing to latch onto. We still validate that
        the OCR text looks plate-like before surfacing it.
        """
        if self._recognizer.status != "ready":
            return []
        results = []
        threshold = max(min(self._fullframe_ocr_threshold, 0.95), 0.0)
        variants = _fullframe_ocr_variants(frame)
        texts: list[tuple[str, float]] = []
        variant_name = "full"
        for candidate_name, candidate in variants:
            texts = self._recognizer.recognize(candidate)
            if texts:
                variant_name = candidate_name
                break
        if not texts:
            self.last_debug_candidates.append({
                "stage": "fullframe_ocr_empty",
                "reason": reason,
                "threshold": threshold,
                "variants_tried": [name for name, _ in variants],
            })
        for raw_text, ocr_conf in texts:
            plate, corrected = _normalize_and_correct(raw_text)
            valid = validate_plate_candidate(plate)
            debug = {
                "stage": "fullframe_ocr_candidate",
                "reason": reason,
                "raw_text": raw_text,
                "variant": variant_name,
                "plate": plate,
                "corrected": corrected,
                "ocr_confidence": round(float(ocr_conf), 3),
                "threshold": threshold,
                "valid_plate": valid,
            }
            if not valid:
                debug["reject_reason"] = "plate failed validation"
                self.last_debug_candidates.append(debug)
                continue
            if ocr_conf < threshold:
                debug["reject_reason"] = "OCR confidence below fullframe threshold"
                self.last_debug_candidates.append(debug)
                continue
            debug["accepted"] = True
            self.last_debug_candidates.append(debug)
            results.append({
                "plate": plate,
                "confidence": round(ocr_conf * 0.7, 3),  # penalty for no detector
                "bbox": None,
                "frame_w": frame.shape[1],
                "frame_h": frame.shape[0],
                "source": "ocr_fullframe",
                "raw_text": raw_text,
                "corrected": corrected,
            })
        return results
