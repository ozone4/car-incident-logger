"""
test_gps_reader.py — Tests for GPSReader.

No real GPS hardware required. Tests cover:
- Disabled GPS returns None
- State dict shape and stale flag
- Graceful failure when gpsd / serial unavailable
"""

import sys
import time
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from modules.gps_reader import GPSReader


# ── Disabled GPS ──────────────────────────────────────────────────────────────

class TestGPSDisabled:

    def test_disabled_reader_get_state_returns_none(self):
        reader = GPSReader({"enabled": False})
        assert reader.get_state() is None

    def test_disabled_reader_get_location_returns_none(self):
        reader = GPSReader({"enabled": False})
        assert reader.get_location() is None

    def test_disabled_is_available_false(self):
        reader = GPSReader({"enabled": False})
        assert reader.is_available is False

    def test_disabled_start_does_not_launch_thread(self):
        reader = GPSReader({"enabled": False})
        reader.start()
        assert reader._thread is None


# ── State dict shape ───────────────────────────────────────────────────────────

class TestGPSStateShape:
    """Set internal state directly and verify public API shape."""

    def _make_reader(self):
        reader = GPSReader({"enabled": True, "stale_after_seconds": 60.0})
        return reader

    def test_set_state_returns_correct_keys(self):
        reader = self._make_reader()
        state = {
            "lat": 49.2827, "lon": -123.1207,
            "speed_kmh": 42.5, "heading": 180.0,
            "altitude": 15.0, "fix_quality": 3,
            "satellites": 8, "timestamp": "2026-01-01T00:00:00+00:00",
            "backend_used": "gpsd", "error": None,
        }
        reader._set_state(state)
        got = reader.get_state()

        assert got is not None
        assert got["lat"] == pytest.approx(49.2827)
        assert got["lon"] == pytest.approx(-123.1207)
        assert got["speed_kmh"] == pytest.approx(42.5)
        assert got["heading"] == pytest.approx(180.0)
        assert got["altitude"] == pytest.approx(15.0)
        assert got["fix_quality"] == 3
        assert got["satellites"] == 8
        assert got["backend_used"] == "gpsd"
        assert got["error"] is None

    def test_stale_flag_fresh_state(self):
        reader = self._make_reader()
        reader._set_state({"lat": 1.0, "lon": 2.0, "error": None})
        got = reader.get_state()
        assert got["stale"] is False

    def test_stale_flag_after_expiry(self):
        reader = GPSReader({"enabled": True, "stale_after_seconds": 0.01})
        reader._set_state({"lat": 1.0, "lon": 2.0, "error": None})
        time.sleep(0.05)
        got = reader.get_state()
        assert got["stale"] is True

    def test_is_available_true_when_fresh_no_error(self):
        reader = GPSReader({"enabled": True, "stale_after_seconds": 60.0})
        reader._set_state({"lat": 1.0, "lon": 2.0, "error": None})
        assert reader.is_available is True

    def test_is_available_false_when_stale(self):
        reader = GPSReader({"enabled": True, "stale_after_seconds": 0.01})
        reader._set_state({"lat": 1.0, "lon": 2.0, "error": None})
        time.sleep(0.05)
        assert reader.is_available is False

    def test_is_available_false_when_error(self):
        reader = GPSReader({"enabled": True, "stale_after_seconds": 60.0})
        reader._set_state({"lat": None, "lon": None, "error": "no fix"})
        assert reader.is_available is False

    def test_get_state_returns_copy(self):
        reader = GPSReader({"enabled": True, "stale_after_seconds": 60.0})
        reader._set_state({"lat": 1.0, "lon": 2.0, "error": None})
        s1 = reader.get_state()
        s2 = reader.get_state()
        assert s1 is not s2  # must be different objects


# ── Error state ────────────────────────────────────────────────────────────────

class TestGPSErrorState:

    def test_set_error_when_no_existing_state(self):
        reader = GPSReader({"enabled": True, "stale_after_seconds": 60.0})
        reader._set_error("gpsd unavailable")
        state = reader.get_state()
        assert state is not None
        assert state["error"] == "gpsd unavailable"
        assert state["lat"] is None
        assert state["lon"] is None

    def test_set_error_preserves_previous_coords(self):
        reader = GPSReader({"enabled": True, "stale_after_seconds": 60.0})
        reader._set_state({"lat": 49.0, "lon": -123.0, "error": None})
        reader._set_error("connection lost")
        state = reader.get_state()
        # Coords preserved, error set
        assert state["lat"] == pytest.approx(49.0)
        assert state["error"] == "connection lost"


# ── gpsd import missing ────────────────────────────────────────────────────────

class TestGPSdNotInstalled:

    def test_gpsd_poll_returns_false_if_import_error(self):
        reader = GPSReader({"enabled": True, "backend": "gpsd"})
        # Patch 'gps' import to raise ImportError
        with patch.dict("sys.modules", {"gps": None}):
            result = reader._poll_gpsd()
        assert result is False


# ── Serial import missing ──────────────────────────────────────────────────────

class TestSerialNotInstalled:

    def test_serial_poll_returns_false_if_import_error(self):
        reader = GPSReader({"enabled": True, "backend": "serial"})
        with patch.dict("sys.modules", {"serial": None, "pynmea2": None}):
            result = reader._poll_serial()
        assert result is False


# ── Config defaults ────────────────────────────────────────────────────────────

class TestGPSConfig:

    def test_default_backend_is_gpsd(self):
        reader = GPSReader({"enabled": True})
        assert reader._backend == "gpsd"

    def test_default_poll_interval(self):
        reader = GPSReader({"enabled": True})
        assert reader._poll_interval == pytest.approx(1.0)

    def test_custom_serial_port(self):
        reader = GPSReader({"enabled": True, "serial_port": "/dev/ttyACM0", "baud_rate": 4800})
        assert reader._serial_port == "/dev/ttyACM0"
        assert reader._baud_rate == 4800

    def test_stale_after_configurable(self):
        reader = GPSReader({"enabled": True, "stale_after_seconds": 30.0})
        assert reader._stale_after == pytest.approx(30.0)


# ── gpsd backend with mocked gps library ──────────────────────────────────────

class TestGPSdBackend:
    """Drive _poll_gpsd with a mocked `gps` python module."""

    def _install_gpsd_stub(self, fix_obj):
        """Install a fake `gps` module whose .gps() returns a session with the given fix."""
        import types
        gps_mod = types.ModuleType("gps")
        gps_mod.WATCH_ENABLE = 1
        gps_mod.WATCH_NEWSTYLE = 2

        session = MagicMock()
        session.read = MagicMock()
        session.fix = fix_obj
        session.close = MagicMock()
        gps_mod.gps = MagicMock(return_value=session)
        return gps_mod, session

    def test_poll_gpsd_publishes_state_on_3d_fix(self):
        fix = MagicMock(latitude=49.2827, longitude=-123.1207,
                        speed=10.0, track=180.5, altitude=70.0, mode=3)
        gps_mod, session = self._install_gpsd_stub(fix)

        with patch.dict("sys.modules", {"gps": gps_mod}):
            reader = GPSReader({"enabled": True, "backend": "gpsd"})
            assert reader._poll_gpsd() is True

        state = reader.get_state()
        assert state is not None
        assert state["lat"] == 49.2827
        assert state["lon"] == -123.1207
        assert state["speed_kmh"] == pytest.approx(36.0, rel=0.01)  # 10 m/s ≈ 36 km/h
        assert state["heading"] == 180.5
        assert state["altitude"] == 70.0
        assert state["fix_quality"] == 3
        assert state["backend_used"] == "gpsd"
        assert state["error"] is None
        # session should have been read
        session.read.assert_called()

    def test_poll_gpsd_returns_false_when_no_fix(self):
        fix = MagicMock(latitude=None, longitude=None, mode=1)
        gps_mod, _ = self._install_gpsd_stub(fix)
        with patch.dict("sys.modules", {"gps": gps_mod}):
            reader = GPSReader({"enabled": True, "backend": "gpsd"})
            assert reader._poll_gpsd() is False

    def test_poll_gpsd_handles_nan_coords(self):
        fix = MagicMock(latitude=float("nan"), longitude=float("nan"), mode=2)
        gps_mod, _ = self._install_gpsd_stub(fix)
        with patch.dict("sys.modules", {"gps": gps_mod}):
            reader = GPSReader({"enabled": True, "backend": "gpsd"})
            assert reader._poll_gpsd() is False

    def test_poll_gpsd_reuses_session(self):
        fix = MagicMock(latitude=49.0, longitude=-123.0, speed=0.0,
                        track=None, altitude=None, mode=2)
        gps_mod, session = self._install_gpsd_stub(fix)
        with patch.dict("sys.modules", {"gps": gps_mod}):
            reader = GPSReader({"enabled": True, "backend": "gpsd"})
            reader._poll_gpsd()
            reader._poll_gpsd()
            # gpsd_lib.gps() should be constructed only once
            assert gps_mod.gps.call_count == 1


# ── Serial / NMEA backend with mocked pyserial + pynmea2 ──────────────────────

class TestSerialBackend:

    def _install_serial_stub(self, nmea_lines):
        """Install fake `serial` and `pynmea2` modules backed by a queue of NMEA strings."""
        import types

        # serial stub: Serial.readline() yields one line per call
        serial_mod = types.ModuleType("serial")

        class _Serial:
            def __init__(self, *_a, **_k):
                self.is_open = True
                self._lines = list(nmea_lines)

            def readline(self):
                if self._lines:
                    return (self._lines.pop(0) + "\r\n").encode("ascii")
                return b""

            def close(self):
                self.is_open = False

        serial_mod.Serial = _Serial

        # pynmea2 stub — only what the reader uses
        pynmea_mod = types.ModuleType("pynmea2")
        pynmea_types = types.ModuleType("pynmea2.types")
        pynmea_talker = types.ModuleType("pynmea2.types.talker")

        class _GGA:
            def __init__(self, latitude=None, longitude=None, altitude=None,
                         num_sats=None, gps_qual=None):
                self.latitude = latitude
                self.longitude = longitude
                self.altitude = altitude
                self.num_sats = num_sats
                self.gps_qual = gps_qual

        class _RMC:
            def __init__(self, latitude=None, longitude=None,
                         spd_over_grnd=None, true_course=None, status="A"):
                self.latitude = latitude
                self.longitude = longitude
                self.spd_over_grnd = spd_over_grnd
                self.true_course = true_course
                self.status = status

        pynmea_talker.GGA = _GGA
        pynmea_talker.RMC = _RMC
        pynmea_types.talker = pynmea_talker
        pynmea_mod.types = pynmea_types

        class _ParseError(Exception):
            pass

        pynmea_mod.ParseError = _ParseError

        # Tagged-string parser: lines like "GGA:49.0,-123.0,70,8,1"
        # or "RMC:49.0,-123.0,10,180,A". Anything else raises ParseError.
        def _parse(line: str):
            tag, _, rest = line[1:].partition(":")
            parts = rest.split(",")
            if tag == "GGA":
                return _GGA(
                    latitude=float(parts[0]),
                    longitude=float(parts[1]),
                    altitude=float(parts[2]) if parts[2] else None,
                    num_sats=int(parts[3]) if parts[3] else None,
                    gps_qual=int(parts[4]) if parts[4] else 0,
                )
            if tag == "RMC":
                return _RMC(
                    latitude=float(parts[0]),
                    longitude=float(parts[1]),
                    spd_over_grnd=float(parts[2]) if parts[2] else 0.0,
                    true_course=float(parts[3]) if parts[3] else None,
                    status=parts[4] if len(parts) > 4 else "A",
                )
            raise _ParseError(f"unknown tag {tag}")

        pynmea_mod.parse = _parse
        return serial_mod, pynmea_mod

    def test_poll_serial_publishes_state_from_rmc(self):
        serial_mod, pynmea_mod = self._install_serial_stub([
            "$GGA:49.2827,-123.1207,72.5,8,1",
            "$RMC:49.2827,-123.1207,5.4,90.0,A",  # 5.4 knots ≈ 10 km/h
        ])
        patches = {
            "serial": serial_mod,
            "pynmea2": pynmea_mod,
            "pynmea2.types": pynmea_mod.types,
            "pynmea2.types.talker": pynmea_mod.types.talker,
        }
        with patch.dict("sys.modules", patches):
            reader = GPSReader({"enabled": True, "backend": "serial",
                                "serial_port": "/dev/ttyUSB0"})
            assert reader._poll_serial() is True

        state = reader.get_state()
        assert state["lat"] == 49.2827
        assert state["lon"] == -123.1207
        assert state["speed_kmh"] == pytest.approx(10.0, abs=0.05)
        assert state["heading"] == 90.0
        assert state["altitude"] == 72.5  # carried from preceding GGA
        assert state["fix_quality"] == 1   # carried from preceding GGA
        assert state["satellites"] == 8
        assert state["backend_used"] == "serial"
        assert state["error"] is None

    def test_poll_serial_skips_rmc_with_void_status(self):
        serial_mod, pynmea_mod = self._install_serial_stub([
            "$RMC:49.0,-123.0,5.0,180.0,V",   # V = invalid, should be skipped
        ])
        patches = {
            "serial": serial_mod, "pynmea2": pynmea_mod,
            "pynmea2.types": pynmea_mod.types,
            "pynmea2.types.talker": pynmea_mod.types.talker,
        }
        with patch.dict("sys.modules", patches):
            reader = GPSReader({"enabled": True, "backend": "serial"})
            assert reader._poll_serial() is False

    def test_poll_serial_falls_back_to_gga_only(self):
        """GGA without a valid RMC still produces a state (with speed=0)."""
        serial_mod, pynmea_mod = self._install_serial_stub([
            "$GGA:49.5,-123.5,100.0,6,1",
        ] + [""] * 30)  # padding so the 30-line read loop terminates
        patches = {
            "serial": serial_mod, "pynmea2": pynmea_mod,
            "pynmea2.types": pynmea_mod.types,
            "pynmea2.types.talker": pynmea_mod.types.talker,
        }
        with patch.dict("sys.modules", patches):
            reader = GPSReader({"enabled": True, "backend": "serial"})
            assert reader._poll_serial() is True

        state = reader.get_state()
        assert state["lat"] == 49.5
        assert state["lon"] == -123.5
        assert state["speed_kmh"] == 0.0
        assert state["altitude"] == 100.0
        assert state["satellites"] == 6
        assert state["backend_used"] == "serial"
