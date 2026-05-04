"""
alpr_runner.py — STUB for Phase 2 automatic license plate recognition.

─────────────────────────────────────────────────────────────────────────────
PHASE 2 PLAN
─────────────────────────────────────────────────────────────────────────────
Goal: Run a lightweight ALPR pipeline on every Nth live frame from the
rolling buffer, yielding detected plate strings + bounding boxes in real time.

Recommended engine options (pick one):
  A. EasyOCR + custom vehicle/plate detector
       pip install easyocr
       Use a YOLO-nano model (e.g. YOLOv8n) to detect plate regions first,
       then feed crops to EasyOCR for character recognition.
       Pro: fully offline, permissive license, no C deps.
       Con: slower than compiled solutions; may need tuning for US plates.

  B. OpenALPR (open-source C++ library with Python bindings)
       pip install openalpr   OR  apt install openalpr
       from openalpr import Alpr
       alpr = Alpr("us", "/etc/openalpr/openalpr.conf", "/usr/share/openalpr/runtime_data")
       result = alpr.recognize_ndarray(frame)
       Pro: mature, well-tested on North American plates.
       Con: large install, GPL-licensed, compile from source on ARM.

  C. Plate Recognizer Local SDK  (platerecognizer.com)
       Paid licence required; REST API runs locally in Docker.
       Pro: highest accuracy, supports many regions.
       Con: requires subscription + Docker.

Implementation sketch for Option A:
    from ultralytics import YOLO
    import easyocr

    detector = YOLO("yolov8n.pt")           # or a plate-specific fine-tune
    reader   = easyocr.Reader(["en"], gpu=False)

    def run_on_frame(frame):
        results = detector(frame, classes=[2])   # class 2 = car (COCO)
        plates  = []
        for box in results[0].boxes:
            crop = frame[y1:y2, x1:x2]
            texts = reader.readtext(crop)
            for bbox, text, conf in texts:
                if conf > THRESHOLD and looks_like_plate(text):
                    plates.append({"plate": text, "confidence": conf, "bbox": box})
        return plates
─────────────────────────────────────────────────────────────────────────────
"""

import logging
from typing import Any, Dict, List

import numpy as np

logger = logging.getLogger(__name__)


class ALPRRunner:
    """
    Phase 2 STUB.  run_on_frame() returns an empty list until implemented.
    Set alpr.enabled = true in config.yaml to activate (once implemented).
    """

    def __init__(
        self,
        engine: str = "easyocr",
        confidence_threshold: float = 0.7,
        models_dir: str = "./data/models",
    ):
        self.engine = engine
        self.confidence_threshold = confidence_threshold
        self.models_dir = models_dir
        self._initialized = False

    def initialize(self) -> None:
        """
        TODO (Phase 2): Load the chosen ALPR engine here.
        Called once at startup when alpr.enabled = true.
        """
        logger.warning(
            "ALPRRunner.initialize() called but Phase 2 is not yet implemented. "
            "Set alpr.enabled = false in config.yaml to suppress this warning."
        )
        self._initialized = True

    def run_on_frame(self, frame: np.ndarray) -> List[Dict[str, Any]]:
        """
        TODO (Phase 2): Run ALPR on a single BGR frame.

        Expected return format:
            [
                {
                    "plate":      "WJ1843",       # normalized uppercase
                    "confidence": 0.91,            # 0.0–1.0
                    "bbox":       [x1, y1, x2, y2] # pixel coords in frame
                },
                ...
            ]
        Returns an empty list until Phase 2 is implemented.
        """
        # STUB — replace with real engine call
        return []

    @property
    def is_ready(self) -> bool:
        return self._initialized
