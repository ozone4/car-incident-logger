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

    def __init__(self, model_path: str, conf_threshold: float = 0.5) -> None:
        self.model_path = model_path
        self.conf_threshold = conf_threshold
        self._model: Any = None
        self.status = "uninitialized"
        self.error: str | None = None
        self.last_raw_detections: list[dict] = []

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
            results = self._model(frame, conf=0.01, verbose=False)
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


class _PaddleOCRRecognizer(_BaseRecognizer):
    """PaddleOCR text recognizer (optional)."""

    def __init__(self) -> None:
        self._ocr: Any = None
        self.status = "uninitialized"
        self.error: str | None = None

    def initialize(self) -> bool:
        try:
            from paddleocr import PaddleOCR  # noqa: PLC0415
        except ImportError:
            self.status = "unavailable"
            self.error = (
                "paddleocr not installed — run: "
                "pip install paddlepaddle paddleocr  (see requirements-alpr.txt)"
            )
            return False

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
                return True
            except Exception as exc:  # noqa: BLE001
                errors.append(str(exc))

        self.status = "error"
        self.error = errors[-1] if errors else "PaddleOCR initialization failed"
        return False

    def recognize(self, image: np.ndarray) -> list[tuple[str, float]]:
        if self._ocr is None:
            return []

        call_attempts = [
            lambda: self._ocr.ocr(image, cls=True),
            lambda: self._ocr.ocr(image),
            lambda: self._ocr.predict(image),
        ]
        last_error: Exception | None = None
        for call in call_attempts:
            try:
                return _extract_ocr_texts(call())
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                continue

        if last_error:
            logger.warning("PaddleOCR recognition failed: %s", last_error)
        return []


# ---------------------------------------------------------------------------
# Pre-processing helper
# ---------------------------------------------------------------------------

def _preprocess_crop(
    frame: np.ndarray, x1: int, y1: int, x2: int, y2: int
) -> np.ndarray:
    """Crop a plate region with small padding, upscale if too small for OCR."""
    h, w = frame.shape[:2]
    x1 = max(0, x1 - 5)
    y1 = max(0, y1 - 5)
    x2 = min(w, x2 + 5)
    y2 = min(h, y2 + 5)
    crop = frame[y1:y2, x1:x2]

    ch, cw = crop.shape[:2]
    if cw < 200 and cw > 0:
        try:
            import cv2  # noqa: PLC0415
            scale = 200 / cw
            interp = getattr(cv2, "INTER_CUBIC", 2)  # 2 is the cv2.INTER_CUBIC int value
            crop = cv2.resize(
                crop,
                (int(cw * scale), int(ch * scale)),
                interpolation=interp,
            )
        except (ImportError, Exception):  # noqa: BLE001
            pass  # cv2 missing or mocked — return crop as-is

    return crop


def _normalize_and_correct(raw: str) -> tuple[str, bool]:
    """Normalize then apply OCR corrections. Returns (plate, was_corrected)."""
    norm = normalize_plate(raw)
    corrected = apply_ocr_corrections(norm)
    return corrected, corrected != norm


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
      confidence_threshold       float  (default 0.5) final plate confidence
      yolo_confidence_threshold  float  (default 0.1) detector box threshold
      models_dir                 str    (default ./data/models)
      yolo_model_path            str    (default <models_dir>/plate_detector.pt)
    """

    def __init__(
        self,
        config: dict | None = None,
        *,
        detector: _BaseDetector | None = None,
        recognizer: _BaseRecognizer | None = None,
    ) -> None:
        cfg = config or {}
        self._conf_threshold = float(cfg.get("confidence_threshold", 0.5))
        self._yolo_conf_threshold = float(cfg.get("yolo_confidence_threshold", 0.1))
        models_dir = Path(cfg.get("models_dir", "./data/models"))
        yolo_path = cfg.get(
            "yolo_model_path", str(models_dir / "plate_detector.pt")
        )

        self._detector: _BaseDetector = detector or _YOLODetector(
            yolo_path, self._yolo_conf_threshold
        )
        self._recognizer: _BaseRecognizer = recognizer or _PaddleOCRRecognizer()
        self._ready = False
        self._init_status: dict = {}

    def initialize(self) -> bool:
        """
        Initialize available engines. Returns True if at least OCR is ready.
        Safe to call even if all deps are missing.
        """
        det_ok = self._detector.initialize()
        ocr_ok = self._recognizer.initialize()

        self._init_status = {
            "detector": self._detector.status,
            "detector_error": self._detector.error,
            "ocr": self._recognizer.status,
            "ocr_error": self._recognizer.error,
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
        if det_ok and ocr_ok:
            logger.info("ALPR ready: YOLO detector + PaddleOCR")
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

        results: list[dict] = []

        # ── Detector-first pipeline (YOLO crop → OCR) ─────────────────────────
        if self._detector.status == "ready":
            boxes = self._detector.detect(frame)
            for box in boxes:
                x1, y1, x2, y2 = box["bbox"]
                det_conf = box["confidence"]
                crop = _preprocess_crop(frame, x1, y1, x2, y2)
                texts = self._recognizer.recognize(crop)
                for raw_text, ocr_conf in texts:
                    plate, corrected = _normalize_and_correct(raw_text)
                    if not validate_plate_candidate(plate):
                        continue
                    # Detector confidence from small/close/printed plates can be low even
                    # when the crop is correct. Once YOLO has found a plausible plate box,
                    # weight OCR more heavily and normalize the detector contribution
                    # against the configured YOLO threshold instead of raw 0–1 confidence.
                    det_score = min(det_conf / max(self._yolo_conf_threshold, 0.01), 1.0)
                    combined = ocr_conf * 0.8 + det_score * 0.2
                    if combined < self._conf_threshold:
                        continue
                    results.append(
                        {
                            "plate": plate,
                            "confidence": round(combined, 3),
                            "bbox": box["bbox"],
                            "source": "yolo+paddle",
                            "raw_text": raw_text,
                            "corrected": corrected,
                        }
                    )
            return results

        # ── Whole-frame fallback (PaddleOCR on full frame) ────────────────────
        if self._recognizer.status == "ready":
            texts = self._recognizer.recognize(frame)
            for raw_text, ocr_conf in texts:
                plate, corrected = _normalize_and_correct(raw_text)
                if not validate_plate_candidate(plate):
                    continue
                # Higher bar for full-frame: more false positives expected
                threshold = max(self._conf_threshold, 0.80)
                if ocr_conf < threshold:
                    continue
                results.append(
                    {
                        "plate": plate,
                        "confidence": round(ocr_conf * 0.7, 3),  # penalty for no detector
                        "bbox": None,
                        "source": "paddle_fullframe",
                        "raw_text": raw_text,
                        "corrected": corrected,
                    }
                )

        return results
