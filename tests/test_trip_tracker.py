"""
test_trip_tracker.py — Tests for TripTracker.

Uses an in-memory SQLite DB and a mock GPS reader. No real hardware required.
"""

import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from modules.plate_database import PlateDatabase
from modules.trip_tracker import TripTracker


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "test_plates.db"
    database = PlateDatabase(db_path=str(db_path))
    yield database
    database.close()


def make_gps(lat=49.28, lon=-123.12, speed=55.0, heading=90.0, stale=False, error=None):
    """Return a mock GPSReader with a fixed state."""
    gps = MagicMock()
    gps.get_state.return_value = {
        "lat": lat, "lon": lon,
        "speed_kmh": speed, "heading": heading,
        "altitude": 10.0, "fix_quality": 3,
        "satellites": 8, "stale": stale, "error": error,
        "timestamp": "2026-01-01T00:00:00+00:00",
        "backend_used": "gpsd",
    }
    return gps


# ── Trip start / stop ─────────────────────────────────────────────────────────

class TestTripLifecycle:

    def test_start_creates_trip_row(self, db):
        gps = make_gps()
        tracker = TripTracker(db=db, gps_reader=gps)
        tracker.start()
        info = tracker.get_current_trip()
        assert info is not None
        assert info["trip_id"] > 0
        tracker.stop()

    def test_stop_ends_trip_in_db(self, db):
        gps = make_gps()
        tracker = TripTracker(db=db, gps_reader=gps)
        tracker.start()
        tid = tracker.get_current_trip()["trip_id"]
        tracker.stop()

        # trip row should have ended_at set
        row = db.get_trip(tid)
        assert row is not None
        assert row["ended_at"] is not None

    def test_get_current_trip_returns_none_before_start(self, db):
        gps = make_gps()
        tracker = TripTracker(db=db, gps_reader=gps)
        assert tracker.get_current_trip() is None

    def test_get_current_trip_returns_none_after_stop(self, db):
        gps = make_gps()
        tracker = TripTracker(db=db, gps_reader=gps)
        tracker.start()
        tracker.stop()
        assert tracker.get_current_trip() is None

    def test_disabled_tracker_does_not_start(self, db):
        gps = make_gps()
        tracker = TripTracker(db=db, gps_reader=gps, config={"enabled": False})
        tracker.start()
        assert tracker.get_current_trip() is None
        assert tracker._thread is None


# ── Trip point sampling ───────────────────────────────────────────────────────

class TestTripPointSampling:

    def test_sample_once_writes_trip_point(self, db):
        gps = make_gps(lat=49.28, lon=-123.12, speed=60.0)
        tracker = TripTracker(db=db, gps_reader=gps, config={"sample_interval_seconds": 1.0})
        tracker.start()
        tracker._sample_once()  # manual call — no need to wait for background thread
        points = db.get_trip_points(tracker.get_current_trip()["trip_id"])
        tracker.stop()
        assert len(points) == 1
        assert points[0]["lat"] == pytest.approx(49.28)
        assert points[0]["lon"] == pytest.approx(-123.12)
        assert points[0]["speed_kmh"] == pytest.approx(60.0)

    def test_sample_skips_when_gps_stale(self, db):
        gps = make_gps(stale=True)
        tracker = TripTracker(db=db, gps_reader=gps)
        tracker.start()
        tracker._sample_once()
        points = db.get_trip_points(tracker.get_current_trip()["trip_id"])
        tracker.stop()
        assert len(points) == 0

    def test_sample_skips_when_gps_error(self, db):
        gps = make_gps(error="no fix")
        tracker = TripTracker(db=db, gps_reader=gps)
        tracker.start()
        tracker._sample_once()
        points = db.get_trip_points(tracker.get_current_trip()["trip_id"])
        tracker.stop()
        assert len(points) == 0

    def test_sample_skips_when_gps_none(self, db):
        gps = MagicMock()
        gps.get_state.return_value = None
        tracker = TripTracker(db=db, gps_reader=gps)
        tracker.start()
        tracker._sample_once()
        tid = tracker.get_current_trip()["trip_id"]
        tracker.stop()
        assert db.get_trip_points(tid) == []

    def test_point_count_increments(self, db):
        gps = make_gps()
        tracker = TripTracker(db=db, gps_reader=gps)
        tracker.start()
        tracker._sample_once()
        tracker._sample_once()
        info = tracker.get_current_trip()
        tracker.stop()
        assert info["point_count"] == 2


# ── get_current_trip dict ─────────────────────────────────────────────────────

class TestTripSummary:

    def test_trip_summary_has_required_keys(self, db):
        gps = make_gps()
        tracker = TripTracker(db=db, gps_reader=gps)
        tracker.start()
        info = tracker.get_current_trip()
        tracker.stop()
        for key in ("trip_id", "started_at", "elapsed_seconds", "point_count", "current_gps"):
            assert key in info

    def test_elapsed_seconds_increases(self, db):
        gps = make_gps()
        tracker = TripTracker(db=db, gps_reader=gps)
        tracker.start()
        time.sleep(0.05)
        info = tracker.get_current_trip()
        tracker.stop()
        assert info["elapsed_seconds"] >= 0
