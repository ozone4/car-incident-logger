"""
gps_reader.py — GPS position reader with gpsd and direct NMEA serial fallback.

Runs a background polling thread. Call get_state() from any thread.
All GPS failures are soft — camera and ALPR are never blocked.

Config keys (under gps:):
    enabled               bool   — default False
    backend               str    — "gpsd" | "serial" | "auto"  (auto tries gpsd then serial)
    host                  str    — gpsd host (default localhost)
    port                  int    — gpsd port (default 2947)
    serial_port           str    — serial device (default /dev/ttyUSB0)
    baud_rate             int    — serial baud rate (default 9600)
    poll_interval_seconds float  — how often to poll (default 1.0)
    stale_after_seconds   float  — mark state stale after this many seconds without update (default 10)

Normalized state dict returned by get_state():
    lat            float | None
    lon            float | None
    speed_kmh      float | None
    heading        float | None   — degrees True (0–360)
    altitude       float | None   — metres
    fix_quality    int | None     — 0=none, 1=no fix, 2=2D, 3=3D (gpsd mode); 0/1/2+ for GGA
    satellites     int | None
    timestamp      str            — ISO8601 UTC
    backend_used   str            — "gpsd" | "serial" | None
    stale          bool           — True if last update is older than stale_after_seconds
    error          str | None     — last error message if no valid fix
"""

from __future__ import annotations

import logging
import math
import threading
import time
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


class GPSReader:
    """Background GPS reader. Thread-safe. Fails gracefully when hardware absent."""

    def __init__(self, config: dict | None = None) -> None:
        cfg = config or {}
        self._enabled = bool(cfg.get("enabled", False))
        self._backend = str(cfg.get("backend", "gpsd"))   # gpsd | serial | auto
        self._host = str(cfg.get("host", "localhost"))
        self._port = int(cfg.get("port", 2947))
        self._serial_port = str(cfg.get("serial_port", "/dev/ttyUSB0"))
        self._baud_rate = int(cfg.get("baud_rate", 9600))
        self._poll_interval = float(cfg.get("poll_interval_seconds", 1.0))
        self._stale_after = float(cfg.get("stale_after_seconds", 10.0))

        self._state: dict | None = None
        self._state_mono: float = 0.0
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # Persistent connection handles — reused across polls
        self._gpsd_session = None
        self._serial_handle = None

    # ── Public API ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start background polling thread. No-op if GPS disabled."""
        if not self._enabled:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="GPSReader")
        self._thread.start()
        logger.info("GPSReader started (backend=%s)", self._backend)

    def stop(self) -> None:
        self._stop_event.set()
        self._close_serial()
        self._close_gpsd()

    @property
    def is_available(self) -> bool:
        with self._lock:
            if self._state is None:
                return False
            age = time.monotonic() - self._state_mono
            return not self._state.get("error") and age <= self._stale_after

    def get_state(self) -> Optional[dict]:
        """Return normalized GPS state dict (with 'stale' flag set) or None."""
        with self._lock:
            if self._state is None:
                return None
            age = time.monotonic() - self._state_mono
            state = dict(self._state)
        state["stale"] = age > self._stale_after
        return state

    def get_location(self) -> Optional[dict]:
        """Backwards-compatible alias — returns state dict or None."""
        return self.get_state()

    # ── Background loop ────────────────────────────────────────────────────────

    def _loop(self) -> None:
        auto_tried_gpsd = False
        auto_tried_serial = False

        while not self._stop_event.is_set():
            try:
                backend = self._backend

                if backend == "gpsd":
                    success = self._poll_gpsd()
                    if not success:
                        self._close_gpsd()
                        self._set_error("gpsd: no fix or connection lost")

                elif backend == "serial":
                    success = self._poll_serial()
                    if not success:
                        self._close_serial()
                        self._set_error("serial: no fix or read failed")

                elif backend == "auto":
                    # Try gpsd first; on persistent failure fall through to serial
                    gpsd_ok = self._poll_gpsd()
                    if gpsd_ok:
                        auto_tried_gpsd = True
                        auto_tried_serial = False
                    else:
                        if not auto_tried_serial:
                            self._close_gpsd()
                        serial_ok = self._poll_serial()
                        if serial_ok:
                            auto_tried_serial = True
                        elif auto_tried_gpsd or auto_tried_serial:
                            self._set_error("auto: gpsd and serial both failed")

            except Exception as exc:
                logger.debug("GPS loop error: %s", exc)
                self._set_error(str(exc))

            self._stop_event.wait(self._poll_interval)

        logger.debug("GPSReader loop exited")

    # ── gpsd backend ───────────────────────────────────────────────────────────

    def _poll_gpsd(self) -> bool:
        """Read one update from gpsd. Returns True on valid fix."""
        try:
            import gps as gpsd_lib  # noqa: PLC0415

            if self._gpsd_session is None:
                self._gpsd_session = gpsd_lib.gps(
                    host=self._host,
                    port=self._port,
                    mode=gpsd_lib.WATCH_ENABLE | gpsd_lib.WATCH_NEWSTYLE,
                )
                logger.info("GPS connected to gpsd at %s:%d", self._host, self._port)

            self._gpsd_session.read()
            fix = self._gpsd_session.fix
            if fix is None:
                return False

            lat = getattr(fix, "latitude", None)
            lon = getattr(fix, "longitude", None)
            if lat is None or lon is None:
                return False
            try:
                if math.isnan(float(lat)) or math.isnan(float(lon)):
                    return False
            except (TypeError, ValueError):
                return False

            speed_ms = getattr(fix, "speed", None)
            heading  = getattr(fix, "track", None)
            altitude = getattr(fix, "altitude", None)
            mode     = getattr(fix, "mode", None)

            def _safe(v):
                try:
                    f = float(v)
                    return None if math.isnan(f) else f
                except (TypeError, ValueError):
                    return None

            speed_kmh = round((_safe(speed_ms) or 0.0) * 3.6, 1)

            self._set_state({
                "lat":         round(float(lat), 6),
                "lon":         round(float(lon), 6),
                "speed_kmh":   speed_kmh,
                "heading":     round(_safe(heading), 1) if _safe(heading) is not None else None,
                "altitude":    round(_safe(altitude), 1) if _safe(altitude) is not None else None,
                "fix_quality": int(mode) if mode is not None else None,
                "satellites":  None,
                "timestamp":   datetime.now(timezone.utc).isoformat(),
                "backend_used": "gpsd",
                "error":       None,
            })
            return True

        except ImportError:
            logger.warning("GPS: 'gps' package not installed — run: pip install gps")
            return False
        except Exception as exc:
            logger.debug("gpsd read error: %s", exc)
            return False

    def _close_gpsd(self) -> None:
        if self._gpsd_session is not None:
            try:
                self._gpsd_session.close()
            except Exception:
                pass
            self._gpsd_session = None

    # ── Serial / NMEA backend ──────────────────────────────────────────────────

    def _poll_serial(self) -> bool:
        """Read NMEA sentences from a serial GPS dongle. Returns True on valid fix."""
        try:
            import serial      # noqa: PLC0415
            import pynmea2     # noqa: PLC0415
        except ImportError as exc:
            logger.warning("GPS serial backend requires pyserial + pynmea2: %s", exc)
            return False

        try:
            if self._serial_handle is None or not self._serial_handle.is_open:
                self._serial_handle = serial.Serial(
                    self._serial_port, self._baud_rate, timeout=2
                )
                logger.info("GPS serial opened: %s @ %d", self._serial_port, self._baud_rate)

            # Read up to 30 NMEA sentences looking for a valid RMC or GGA
            gga_state: dict | None = None
            for _ in range(30):
                raw = self._serial_handle.readline()
                try:
                    line = raw.decode("ascii", errors="ignore").strip()
                except Exception:
                    continue
                if not line.startswith("$"):
                    continue
                try:
                    msg = pynmea2.parse(line)
                except pynmea2.ParseError:
                    continue

                # GGA — position + altitude + satellites
                if isinstance(msg, pynmea2.types.talker.GGA):
                    try:
                        lat = float(msg.latitude) if msg.latitude else None
                        lon = float(msg.longitude) if msg.longitude else None
                        if lat and lon:
                            alt  = float(msg.altitude) if msg.altitude else None
                            sats = int(msg.num_sats) if msg.num_sats else None
                            fq   = int(msg.gps_qual) if msg.gps_qual else 0
                            gga_state = {
                                "lat": round(lat, 6), "lon": round(lon, 6),
                                "altitude": round(alt, 1) if alt else None,
                                "satellites": sats, "fix_quality": fq,
                            }
                    except (TypeError, ValueError):
                        pass

                # RMC — position + speed + heading + validity flag
                elif isinstance(msg, pynmea2.types.talker.RMC):
                    try:
                        if getattr(msg, "status", "") != "A":
                            continue
                        lat = float(msg.latitude) if msg.latitude else None
                        lon = float(msg.longitude) if msg.longitude else None
                        if lat is None or lon is None:
                            continue
                        spd_kn  = float(msg.spd_over_grnd or 0)
                        heading = float(msg.true_course) if msg.true_course else None
                        state = {
                            "lat":         round(lat, 6),
                            "lon":         round(lon, 6),
                            "speed_kmh":   round(spd_kn * 1.852, 1),
                            "heading":     round(heading, 1) if heading else None,
                            "altitude":    gga_state.get("altitude") if gga_state else None,
                            "fix_quality": gga_state.get("fix_quality", 2) if gga_state else 2,
                            "satellites":  gga_state.get("satellites") if gga_state else None,
                            "timestamp":   datetime.now(timezone.utc).isoformat(),
                            "backend_used": "serial",
                            "error":       None,
                        }
                        self._set_state(state)
                        return True
                    except (TypeError, ValueError, AttributeError):
                        pass

            # Got GGA but no RMC with valid status
            if gga_state:
                self._set_state({
                    **gga_state,
                    "speed_kmh":   0.0,
                    "heading":     None,
                    "timestamp":   datetime.now(timezone.utc).isoformat(),
                    "backend_used": "serial",
                    "error":       None,
                })
                return True

            return False

        except Exception as exc:
            logger.debug("Serial GPS error: %s", exc)
            self._close_serial()
            return False

    def _close_serial(self) -> None:
        if self._serial_handle is not None:
            try:
                self._serial_handle.close()
            except Exception:
                pass
            self._serial_handle = None

    # ── State helpers ──────────────────────────────────────────────────────────

    def _set_state(self, state: dict) -> None:
        with self._lock:
            self._state = state
            self._state_mono = time.monotonic()

    def _set_error(self, msg: str) -> None:
        with self._lock:
            if self._state is None:
                self._state = {
                    "lat": None, "lon": None, "speed_kmh": None,
                    "heading": None, "altitude": None, "fix_quality": None,
                    "satellites": None, "timestamp": None, "backend_used": None,
                    "stale": True, "error": msg,
                }
            else:
                self._state["error"] = msg
            # Don't update _state_mono so it continues ageing → stale=True
