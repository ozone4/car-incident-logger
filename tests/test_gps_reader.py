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
