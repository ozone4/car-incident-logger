"""
live_matcher.py — STUB for Phase 2 live plate matching.

Calls ALPRRunner on the current frame and compares detected plates against
the known-plate database.  Returns a match dict or None.

─────────────────────────────────────────────────────────────────────────────
PHASE 2 PLAN
─────────────────────────────────────────────────────────────────────────────
This module runs in a dedicated background thread, sampling one frame every
alpr.scan_interval_seconds seconds from the RollingBuffer.

On each sample:
  1. Call alpr_runner.run_on_frame(frame) → list of detections
  2. For each detection above the confidence threshold:
       if plate_database.is_known_plate(detection["plate"]):
           fire notifier.alert(plate, detection)
           plate_database.add_sighting(plate, detection["confidence"])

Threading model:
    live_matcher = LiveMatcher(alpr, db, notifier, config)
    live_matcher.start(rolling_buffer)      # background thread
    ...
    live_matcher.stop()
─────────────────────────────────────────────────────────────────────────────
"""

import logging
import threading
import time
from typing import Any, Dict, Optional

import numpy as np

logger = logging.getLogger(__name__)


class LiveMatcher:
    """
    Phase 2 STUB.  check_frame() always returns None until implemented.
    """

    def __init__(
        self,
        alpr_runner,
        plate_database,
        notifier,
        scan_interval: float = 2.0,
        known_vehicle_matcher=None,
    ):
        self.alpr_runner = alpr_runner
        self.plate_database = plate_database
        self.notifier = notifier
        self.scan_interval = scan_interval
        self.known_vehicle_matcher = known_vehicle_matcher

        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._rolling_buffer = None

    def start(self, rolling_buffer) -> None:
        """TODO (Phase 2): Start the background scanning thread."""
        self._rolling_buffer = rolling_buffer
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._scan_loop, daemon=True, name="LiveMatcher"
        )
        self._thread.start()
        logger.debug("LiveMatcher started (STUB — no-op until Phase 2)")

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5.0)

    def check_frame(self, frame: np.ndarray) -> Optional[Dict[str, Any]]:
        """
        Run ALPR/vehicle detection on *frame*.

        Returns the first known-plate match, a known-vehicle ignored match, or
        None when nothing actionable is detected.  The detector is expected to
        return dictionaries that may include plate/confidence/bbox plus optional
        vehicle traits such as make/model/color/vehicle_type/zone.
        """
        detections = self.alpr_runner.run_on_frame(frame)
        for detection in detections:
            confidence = float(detection.get("confidence", 0.0))
            if confidence < getattr(self.alpr_runner, "confidence_threshold", 0.0):
                continue

            if self.known_vehicle_matcher:
                known_vehicle = self.known_vehicle_matcher.match(detection)
                if known_vehicle:
                    result = dict(detection)
                    result.update({
                        "known_vehicle": known_vehicle,
                        "ignored": True,
                    })
                    logger.debug(
                        "Ignoring known vehicle %s (score=%.2f traits=%s)",
                        known_vehicle["name"],
                        known_vehicle["score"],
                        ",".join(known_vehicle["matched_traits"]),
                    )
                    return result

            plate = str(detection.get("plate", "")).upper().strip()
            if not plate:
                continue

            self.plate_database.add_sighting(
                plate,
                confidence=confidence,
                snapshot_path=detection.get("snapshot_path"),
            )
            if self.plate_database.is_known_plate(plate):
                result = dict(detection)
                result.update({"plate": plate, "known": True})
                return result

        return None

    def _scan_loop(self) -> None:
        """Periodic frame sampling and ALPR invocation."""
        while not self._stop_event.wait(self.scan_interval):
            if self._rolling_buffer is None:
                continue
            entry = self._rolling_buffer.get_latest_frame()
            if entry is None:
                continue
            frame, _ = entry
            match = self.check_frame(frame)
            if not match or match.get("ignored"):
                continue
            self.notifier.alert_known_plate(match["plate"], match)
