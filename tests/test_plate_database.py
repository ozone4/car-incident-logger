"""
test_plate_database.py — pytest tests for PlateDatabase.

Uses an in-memory SQLite database (":memory:") to avoid touching the filesystem.
Run with: pytest tests/test_plate_database.py -v
"""

import sys
import json
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from modules.plate_database import PlateDatabase


@pytest.fixture
def db(tmp_path):
    """Fresh PlateDatabase backed by a temp file for each test."""
    db_path = tmp_path / "test_plates.db"
    database = PlateDatabase(db_path=str(db_path))
    yield database
    database.close()


# ── add_incident / get_incidents_for_plate ────────────────────────────────────

class TestAddAndRetrieveIncident:

    def test_add_incident_returns_id(self, db):
        meta = {"timestamp": "20240101T120000Z", "clip_path": "/tmp/clip.mp4"}
        incident_id = db.add_incident("WJ1843", meta)
        assert isinstance(incident_id, int)
        assert incident_id > 0

    def test_add_incident_creates_plate_row(self, db):
        db.add_incident("ABC123", {"timestamp": "20240101T120000Z"})
        plate = db.get_plate("ABC123")
        assert plate is not None
        assert plate["plate"] == "ABC123"
        assert plate["incident_count"] == 1

    def test_plate_incident_count_increments(self, db):
        meta = {"timestamp": "20240101T120000Z"}
        db.add_incident("WJ1843", meta)
        db.add_incident("WJ1843", {"timestamp": "20240101T130000Z"})
        plate = db.get_plate("WJ1843")
        assert plate["incident_count"] == 2

    def test_get_incidents_for_plate_empty_when_none(self, db):
        incidents = db.get_incidents_for_plate("XXXXXX")
        assert incidents == []

    def test_get_incidents_for_plate_returns_correct_rows(self, db):
        db.add_incident("WJ1843", {"timestamp": "20240101T120000Z", "note": "first"})
        db.add_incident("WJ1843", {"timestamp": "20240101T130000Z", "note": "second"})
        db.add_incident("ABC123", {"timestamp": "20240101T140000Z"})

        incidents = db.get_incidents_for_plate("WJ1843")
        assert len(incidents) == 2

    def test_get_incidents_metadata_json_stored(self, db):
        meta = {"timestamp": "20240101T120000Z", "clip_path": "/clips/x.mp4", "note": "ran light"}
        db.add_incident("WJ1843", meta)
        incidents = db.get_incidents_for_plate("WJ1843")
        assert len(incidents) == 1
        stored = json.loads(incidents[0]["metadata_json"])
        assert stored["note"] == "ran light"

    def test_plate_normalized_to_uppercase(self, db):
        db.add_incident("wj1843", {"timestamp": "20240101T120000Z"})
        plate = db.get_plate("WJ1843")
        assert plate is not None

    def test_clip_path_stored(self, db):
        db.add_incident("WJ1843", {"timestamp": "20240101T120000Z", "clip_path": "/data/clip.mp4"})
        incidents = db.get_incidents_for_plate("WJ1843")
        assert incidents[0]["clip_path"] == "/data/clip.mp4"


# ── is_known_plate ────────────────────────────────────────────────────────────

class TestIsKnownPlate:

    def test_known_plate_returns_true(self, db):
        db.add_incident("WJ1843", {"timestamp": "20240101T120000Z"})
        assert db.is_known_plate("WJ1843") is True

    def test_unknown_plate_returns_false(self, db):
        assert db.is_known_plate("ZZZ999") is False

    def test_case_insensitive(self, db):
        db.add_incident("ABC123", {"timestamp": "20240101T120000Z"})
        assert db.is_known_plate("abc123") is True
        assert db.is_known_plate("Abc123") is True


# ── search_plates ─────────────────────────────────────────────────────────────

class TestSearchPlates:

    def test_search_by_partial_plate(self, db):
        db.add_incident("WJ1843", {"timestamp": "20240101T120000Z"})
        db.add_incident("WJ9999", {"timestamp": "20240101T130000Z"})
        db.add_incident("ABC123", {"timestamp": "20240101T140000Z"})

        results = db.search_plates("WJ")
        plates = [r["plate"] for r in results]
        assert "WJ1843" in plates
        assert "WJ9999" in plates
        assert "ABC123" not in plates

    def test_search_returns_empty_for_no_match(self, db):
        db.add_incident("WJ1843", {"timestamp": "20240101T120000Z"})
        results = db.search_plates("XXXXXX")
        assert results == []


# ── add_sighting ──────────────────────────────────────────────────────────────

class TestSightings:

    def test_add_sighting_unknown_plate(self, db):
        sid = db.add_sighting("ZZZ999", confidence=0.85)
        assert isinstance(sid, int)
        rows = db.get_sightings_for_plate("ZZZ999")
        assert len(rows) == 1
        assert rows[0]["matched"] == 0

    def test_add_sighting_known_plate_sets_matched(self, db):
        db.add_incident("WJ1843", {"timestamp": "20240101T120000Z"})
        db.add_sighting("WJ1843", confidence=0.92)
        rows = db.get_sightings_for_plate("WJ1843")
        assert rows[0]["matched"] == 1

    def test_sighting_confidence_stored(self, db):
        db.add_sighting("ABC123", confidence=0.77)
        rows = db.get_sightings_for_plate("ABC123")
        assert abs(rows[0]["confidence"] - 0.77) < 0.001


# ── delete_plate ──────────────────────────────────────────────────────────────

class TestDeletePlate:

    def test_delete_removes_plate_and_incidents(self, db):
        db.add_incident("WJ1843", {"timestamp": "20240101T120000Z"})
        assert db.is_known_plate("WJ1843") is True

        result = db.delete_plate("WJ1843")
        assert result is True
        assert db.is_known_plate("WJ1843") is False
        assert db.get_incidents_for_plate("WJ1843") == []

    def test_delete_nonexistent_returns_false(self, db):
        result = db.delete_plate("NOTHERE")
        assert result is False
