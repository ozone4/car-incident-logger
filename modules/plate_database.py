"""
plate_database.py — SQLite-backed store for plates, incidents, and sightings.

Schema
------
plates    (id, plate, first_seen, last_seen, incident_count)
incidents (id, plate_id, timestamp, clip_path, metadata_json)
sightings (id, plate, timestamp, confidence, snapshot_path, matched)
"""

import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class PlateDatabase:
    def __init__(self, db_path: str = "./data/plates.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()  # per-thread connections
        self._init_schema()

    # ── Connection management ─────────────────────────────────────────────────

    def _conn(self) -> sqlite3.Connection:
        """Return a thread-local SQLite connection (auto-created if needed)."""
        if not hasattr(self._local, "conn") or self._local.conn is None:
            conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return self._local.conn

    def close(self) -> None:
        if hasattr(self._local, "conn") and self._local.conn:
            self._local.conn.close()
            self._local.conn = None

    # ── Schema ────────────────────────────────────────────────────────────────

    def _init_schema(self) -> None:
        conn = self._conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS plates (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                plate          TEXT    NOT NULL UNIQUE,
                first_seen     TEXT    NOT NULL,
                last_seen      TEXT    NOT NULL,
                incident_count INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS incidents (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                plate_id      INTEGER NOT NULL REFERENCES plates(id),
                timestamp     TEXT    NOT NULL,
                clip_path     TEXT,
                metadata_json TEXT
            );

            CREATE TABLE IF NOT EXISTS sightings (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                plate         TEXT    NOT NULL,
                timestamp     TEXT    NOT NULL,
                confidence    REAL    NOT NULL DEFAULT 0.0,
                snapshot_path TEXT,
                matched       INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS trips (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT    NOT NULL,
                ended_at   TEXT
            );

            CREATE TABLE IF NOT EXISTS trip_points (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                trip_id     INTEGER NOT NULL REFERENCES trips(id),
                timestamp   TEXT    NOT NULL,
                lat         REAL    NOT NULL,
                lon         REAL    NOT NULL,
                speed_kmh   REAL,
                heading     REAL,
                altitude    REAL,
                fix_quality INTEGER
            );

            CREATE INDEX IF NOT EXISTS idx_plates_plate      ON plates(plate);
            CREATE INDEX IF NOT EXISTS idx_incidents_plate   ON incidents(plate_id);
            CREATE INDEX IF NOT EXISTS idx_sightings_plate   ON sightings(plate);
            CREATE INDEX IF NOT EXISTS idx_trip_points_trip  ON trip_points(trip_id);
        """)
        conn.commit()

        # Additive migrations — safe to run on existing databases
        _migrations = [
            # sightings: original GPS columns
            ("sightings", "latitude",     "REAL"),
            ("sightings", "longitude",    "REAL"),
            # sightings: extended GPS context
            ("sightings", "speed_kmh",    "REAL"),
            ("sightings", "heading",      "REAL"),
            ("sightings", "altitude",     "REAL"),
            ("sightings", "gps_timestamp", "TEXT"),
            ("sightings", "gps_backend",  "TEXT"),
        ]
        for table, col, typedef in _migrations:
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typedef}")
                conn.commit()
                logger.info("DB migration: added %s.%s", table, col)
            except Exception:
                pass  # column already exists — that's fine

        logger.debug("Database schema ready at %s", self.db_path)

    # ── Plates ────────────────────────────────────────────────────────────────

    def get_plate(self, plate: str) -> Optional[Dict[str, Any]]:
        """Return the plates row for *plate* or None if not present."""
        plate = plate.upper().strip()
        row = self._conn().execute(
            "SELECT * FROM plates WHERE plate = ?", (plate,)
        ).fetchone()
        return dict(row) if row else None

    def is_known_plate(self, plate: str) -> bool:
        return self.get_plate(plate) is not None

    def _upsert_plate(self, plate: str, conn: sqlite3.Connection) -> int:
        """Insert plate if new, update last_seen, return its row id."""
        now = _utcnow()
        plate = plate.upper().strip()
        existing = conn.execute(
            "SELECT id, incident_count FROM plates WHERE plate = ?", (plate,)
        ).fetchone()

        if existing:
            conn.execute(
                "UPDATE plates SET last_seen = ?, incident_count = incident_count + 1 WHERE id = ?",
                (now, existing["id"]),
            )
            return existing["id"]
        else:
            cur = conn.execute(
                "INSERT INTO plates (plate, first_seen, last_seen, incident_count) VALUES (?, ?, ?, 1)",
                (plate, now, now),
            )
            return cur.lastrowid  # type: ignore[return-value]

    # ── Incidents ─────────────────────────────────────────────────────────────

    def add_incident(self, plate: str, metadata: dict) -> int:
        """
        Record a manual (button-triggered) incident.

        Returns the new incident row id.
        """
        plate = plate.upper().strip()
        now = _utcnow()
        conn = self._conn()

        plate_id = self._upsert_plate(plate, conn)
        cur = conn.execute(
            "INSERT INTO incidents (plate_id, timestamp, clip_path, metadata_json) VALUES (?, ?, ?, ?)",
            (
                plate_id,
                metadata.get("timestamp", now),
                metadata.get("clip_path"),
                json.dumps(metadata),
            ),
        )
        conn.commit()
        logger.info("Incident added: plate=%r  id=%d", plate, cur.lastrowid)
        return cur.lastrowid  # type: ignore[return-value]

    def get_incidents_for_plate(self, plate: str) -> List[Dict[str, Any]]:
        plate = plate.upper().strip()
        plate_row = self.get_plate(plate)
        if not plate_row:
            return []
        rows = self._conn().execute(
            "SELECT * FROM incidents WHERE plate_id = ? ORDER BY timestamp DESC",
            (plate_row["id"],),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_all_incidents(self, limit: int = 100) -> List[Dict[str, Any]]:
        rows = self._conn().execute(
            "SELECT i.*, p.plate FROM incidents i JOIN plates p ON p.id = i.plate_id "
            "ORDER BY i.timestamp DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Sightings (Phase 2 ALPR) ──────────────────────────────────────────────

    def add_sighting(
        self,
        plate: str,
        confidence: float,
        snapshot_path: Optional[str] = None,
        latitude: Optional[float] = None,
        longitude: Optional[float] = None,
        speed_kmh: Optional[float] = None,
        heading: Optional[float] = None,
        altitude: Optional[float] = None,
        gps_timestamp: Optional[str] = None,
        gps_backend: Optional[str] = None,
    ) -> int:
        plate = plate.upper().strip()
        matched = 1 if self.is_known_plate(plate) else 0
        conn = self._conn()
        cur = conn.execute(
            "INSERT INTO sightings "
            "(plate, timestamp, confidence, snapshot_path, matched, "
            " latitude, longitude, speed_kmh, heading, altitude, gps_timestamp, gps_backend) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (plate, _utcnow(), confidence, snapshot_path, matched,
             latitude, longitude, speed_kmh, heading, altitude, gps_timestamp, gps_backend),
        )
        conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    def get_plate_history(self, plate: str) -> Dict[str, Any]:
        """Return a summary of all sightings and incidents for a plate.

        Always returns a dict (never None) so callers can safely read keys.
        """
        plate = plate.upper().strip()
        conn = self._conn()

        row = conn.execute(
            "SELECT COUNT(*) as total, MIN(timestamp) as first_seen, MAX(timestamp) as last_seen "
            "FROM sightings WHERE plate = ?",
            (plate,),
        ).fetchone()
        total_sightings = int(row["total"]) if row else 0
        first_seen = row["first_seen"] if row else None
        last_seen = row["last_seen"] if row else None

        # Incident count from the plates table (already maintained by _upsert_plate)
        plate_row = conn.execute(
            "SELECT incident_count FROM plates WHERE plate = ?", (plate,)
        ).fetchone()
        total_incidents = int(plate_row["incident_count"]) if plate_row else 0

        # Most recent sighting with GPS + snapshot
        latest = conn.execute(
            "SELECT latitude, longitude, snapshot_path FROM sightings "
            "WHERE plate = ? ORDER BY timestamp DESC LIMIT 1",
            (plate,),
        ).fetchone()

        last_location = None
        last_snapshot_path = None
        if latest:
            if latest["latitude"] is not None and latest["longitude"] is not None:
                last_location = {"lat": latest["latitude"], "lon": latest["longitude"]}
            last_snapshot_path = latest["snapshot_path"]

        return {
            "total_sightings": total_sightings,
            "total_incidents": total_incidents,
            "first_seen": first_seen,
            "last_seen": last_seen,
            "last_location": last_location,
            "last_snapshot_path": last_snapshot_path,
        }

    def get_sightings_for_plate(self, plate: str, limit: int = 50) -> List[Dict[str, Any]]:
        plate = plate.upper().strip()
        rows = self._conn().execute(
            "SELECT * FROM sightings WHERE plate = ? ORDER BY timestamp DESC LIMIT ?",
            (plate, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Search ────────────────────────────────────────────────────────────────

    def search_plates(self, query: str) -> List[Dict[str, Any]]:
        """
        Partial-match search on plate strings.
        e.g. search_plates("WJ") returns all plates containing "WJ".
        """
        pattern = f"%{query.upper().strip()}%"
        rows = self._conn().execute(
            "SELECT * FROM plates WHERE plate LIKE ? ORDER BY last_seen DESC",
            (pattern,),
        ).fetchall()
        return [dict(r) for r in rows]

    def all_plates(self) -> List[Dict[str, Any]]:
        rows = self._conn().execute(
            "SELECT * FROM plates ORDER BY last_seen DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Trips ─────────────────────────────────────────────────────────────────

    def start_trip(self) -> int:
        """Open a new trip row and return its id."""
        conn = self._conn()
        cur = conn.execute(
            "INSERT INTO trips (started_at) VALUES (?)", (_utcnow(),)
        )
        conn.commit()
        logger.info("Trip started: id=%d", cur.lastrowid)
        return cur.lastrowid  # type: ignore[return-value]

    def end_trip(self, trip_id: int) -> None:
        """Set ended_at on a trip row."""
        conn = self._conn()
        conn.execute(
            "UPDATE trips SET ended_at = ? WHERE id = ?", (_utcnow(), trip_id)
        )
        conn.commit()

    def add_trip_point(
        self,
        trip_id: int,
        lat: float,
        lon: float,
        speed_kmh: Optional[float] = None,
        heading: Optional[float] = None,
        altitude: Optional[float] = None,
        fix_quality: Optional[int] = None,
    ) -> int:
        conn = self._conn()
        cur = conn.execute(
            "INSERT INTO trip_points "
            "(trip_id, timestamp, lat, lon, speed_kmh, heading, altitude, fix_quality) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (trip_id, _utcnow(), lat, lon, speed_kmh, heading, altitude, fix_quality),
        )
        conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    def get_trip(self, trip_id: int) -> Optional[Dict[str, Any]]:
        row = self._conn().execute(
            "SELECT * FROM trips WHERE id = ?", (trip_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_trip_points(self, trip_id: int, limit: int = 500) -> List[Dict[str, Any]]:
        rows = self._conn().execute(
            "SELECT * FROM trip_points WHERE trip_id = ? ORDER BY timestamp DESC LIMIT ?",
            (trip_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_recent_trips(self, limit: int = 10) -> List[Dict[str, Any]]:
        rows = self._conn().execute(
            "SELECT t.*, "
            "(SELECT COUNT(*) FROM trip_points tp WHERE tp.trip_id = t.id) AS point_count "
            "FROM trips t ORDER BY t.started_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Maintenance ───────────────────────────────────────────────────────────

    def delete_plate(self, plate: str) -> bool:
        """Remove a plate and all its incidents from the database."""
        plate = plate.upper().strip()
        conn = self._conn()
        row = conn.execute("SELECT id FROM plates WHERE plate = ?", (plate,)).fetchone()
        if not row:
            return False
        conn.execute("DELETE FROM incidents WHERE plate_id = ?", (row["id"],))
        conn.execute("DELETE FROM plates WHERE id = ?", (row["id"],))
        conn.commit()
        logger.info("Deleted plate %r and its incidents from DB", plate)
        return True
