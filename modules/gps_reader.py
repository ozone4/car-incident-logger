"""
gps_reader.py — Thin wrapper around gpsd for GPS position.

Returns None gracefully when no GPS hardware is present (ThinkPad dev mode).
Enable via config: gps.enabled: true
Install gpsd client: pip install gps
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


class GPSReader:
    """Read current GPS position from gpsd.

    Usage:
        reader = GPSReader(config.get("gps", {}))
        loc = reader.get_location()  # {"lat": 49.2, "lon": -123.1, "speed_kmh": 0.0} or None
    """

    def __init__(self, config: dict | None = None) -> None:
        cfg = config or {}
        self._enabled = bool(cfg.get("enabled", False))
        self._host = str(cfg.get("host", "localhost"))
        self._port = int(cfg.get("port", 2947))
        self._session = None
        self._available = False

        if self._enabled:
            self._connect()

    def _connect(self) -> None:
        try:
            import gps as gpsd  # noqa: PLC0415
            self._session = gpsd.gps(
                host=self._host,
                port=self._port,
                mode=gpsd.WATCH_ENABLE | gpsd.WATCH_NEWSTYLE,
            )
            self._available = True
            logger.info("GPS connected to gpsd at %s:%d", self._host, self._port)
        except ImportError:
            logger.warning("GPS: 'gps' package not installed — run: pip install gps")
            self._available = False
        except Exception as exc:
            logger.warning("GPS: could not connect to gpsd: %s", exc)
            self._available = False

    def get_location(self) -> Optional[dict]:
        """Return {"lat", "lon", "speed_kmh"} or None if unavailable."""
        if not self._enabled or not self._available or self._session is None:
            return None
        try:
            # Non-blocking: read whatever report gpsd has buffered
            self._session.read()
            fix = self._session.fix
            if fix is None:
                return None
            import math  # noqa: PLC0415
            lat = getattr(fix, "latitude", None)
            lon = getattr(fix, "longitude", None)
            speed_ms = getattr(fix, "speed", None)
            if lat is None or lon is None or math.isnan(lat) or math.isnan(lon):
                return None
            speed_kmh = round(float(speed_ms) * 3.6, 1) if speed_ms and not math.isnan(speed_ms) else 0.0
            return {"lat": round(float(lat), 6), "lon": round(float(lon), 6), "speed_kmh": speed_kmh}
        except Exception as exc:
            logger.debug("GPS read error: %s", exc)
            return None

    @property
    def is_available(self) -> bool:
        return self._enabled and self._available
