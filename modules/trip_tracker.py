"""
trip_tracker.py — Track drive sessions and record GPS breadcrumbs.

Each time the logger starts it opens a new Trip row in SQLite.
A background thread samples GPS every ~5 seconds and writes TripPoint rows.
GPS failures are silently skipped — no fix = no breadcrumb, but the trip
session remains open.

Usage:
    tracker = TripTracker(db=plate_db, gps_reader=gps, config=cfg)
    tracker.start()
    ...
    tracker.stop()
    info = tracker.get_current_trip()  # dict or None
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from modules.plate_database import PlateDatabase
    from modules.gps_reader import GPSReader

logger = logging.getLogger(__name__)


class TripTracker:
    """Sample GPS every N seconds and persist breadcrumbs for the current trip."""

    def __init__(
        self,
        db: "PlateDatabase",
        gps_reader: "GPSReader",
        config: dict | None = None,
    ) -> None:
        cfg = config or {}
        self._db = db
        self._gps = gps_reader
        self._interval = float(cfg.get("sample_interval_seconds", 5.0))
        self._enabled = bool(cfg.get("enabled", True))

        self._current_trip_id: Optional[int] = None
        self._started_at: Optional[datetime] = None
        self._point_count: int = 0

        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ── Public API ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        if not self._enabled:
            return
        try:
            trip_id = self._db.start_trip()
            started = datetime.now(timezone.utc)
            with self._lock:
                self._current_trip_id = trip_id
                self._started_at = started
                self._point_count = 0
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._loop, daemon=True, name="TripTracker"
            )
            self._thread.start()
            logger.info("Trip started (id=%d)", trip_id)
        except Exception as exc:
            logger.warning("TripTracker.start failed: %s", exc)

    def stop(self) -> None:
        self._stop_event.set()
        with self._lock:
            tid = self._current_trip_id
        if tid is not None:
            try:
                self._db.end_trip(tid)
                logger.info("Trip ended (id=%d, points=%d)", tid, self._point_count)
            except Exception as exc:
                logger.warning("TripTracker.stop — could not end trip: %s", exc)
            with self._lock:
                self._current_trip_id = None

    def get_current_trip(self) -> Optional[dict]:
        """Return a summary of the in-progress trip, or None if not tracking."""
        with self._lock:
            tid = self._current_trip_id
            started = self._started_at
            pts = self._point_count
        if tid is None:
            return None
        elapsed = (
            (datetime.now(timezone.utc) - started).total_seconds()
            if started else 0.0
        )
        gps = self._gps.get_state()
        return {
            "trip_id":          tid,
            "started_at":       started.isoformat() if started else None,
            "elapsed_seconds":  round(elapsed),
            "point_count":      pts,
            "current_gps":      gps,
        }

    # ── Background loop ────────────────────────────────────────────────────────

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._sample_once()
            except Exception as exc:
                logger.debug("TripTracker sample error: %s", exc)
            self._stop_event.wait(self._interval)

    def _sample_once(self) -> None:
        with self._lock:
            tid = self._current_trip_id
        if tid is None:
            return

        state = self._gps.get_state()
        if state is None or state.get("stale") or state.get("error"):
            return
        lat = state.get("lat")
        lon = state.get("lon")
        if lat is None or lon is None:
            return

        self._db.add_trip_point(
            trip_id=tid,
            lat=lat,
            lon=lon,
            speed_kmh=state.get("speed_kmh"),
            heading=state.get("heading"),
            altitude=state.get("altitude"),
            fix_quality=state.get("fix_quality"),
        )
        with self._lock:
            self._point_count += 1
