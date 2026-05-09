"""
test_button_listener.py — Unit tests for modules/button_listener.py.

pynput and RPi.GPIO are stubbed so tests run on any host.
"""

from __future__ import annotations

import sys
import threading
import time
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ── pynput stub ──────────────────────────────────────────────────────────────
class _Key:
    """Stand-in for pynput.keyboard.Key.<name>."""

    def __init__(self, name):
        self.name = name

    def __eq__(self, other):
        return isinstance(other, _Key) and self.name == other.name

    def __hash__(self):
        return hash(("KEY", self.name))


class _CharKey:
    """Stand-in for pynput.keyboard.KeyCode (regular character keys)."""

    def __init__(self, char):
        self.char = char


class _Listener:
    """Stand-in for pynput.keyboard.Listener — captures callbacks for the test to drive."""

    last: "_Listener | None" = None  # most recently constructed instance

    def __init__(self, on_press=None, on_release=None):
        self.on_press = on_press
        self.on_release = on_release
        self.daemon = True
        self.started = False
        self.stopped = False
        _Listener.last = self

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


_pynput_pkg = types.ModuleType("pynput")
_pynput_kb = types.ModuleType("pynput.keyboard")


class _KeyNamespace:
    space = _Key("space")
    enter = _Key("enter")
    f1 = _Key("f1")
    f2 = _Key("f2")
    f3 = _Key("f3")
    f4 = _Key("f4")


_pynput_kb.Key = _KeyNamespace
_pynput_kb.Listener = _Listener
_pynput_kb.KeyCode = _CharKey
_pynput_pkg.keyboard = _pynput_kb
sys.modules.setdefault("pynput", _pynput_pkg)
sys.modules.setdefault("pynput.keyboard", _pynput_kb)


from modules import button_listener  # noqa: E402


# ── Tests ────────────────────────────────────────────────────────────────────
def _wait_until(predicate, timeout=1.0, interval=0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def test_keyboard_mode_fires_press_and_release_callbacks():
    pressed = threading.Event()
    released = threading.Event()
    listener = button_listener.ButtonListener(mode="keyboard", key="space")
    listener.on_press = pressed.set
    listener.on_release = released.set
    listener.start()
    try:
        # Drive the captured pynput callbacks directly
        _Listener.last.on_press(_KeyNamespace.space)
        assert _wait_until(pressed.is_set)
        _Listener.last.on_release(_KeyNamespace.space)
        assert _wait_until(released.is_set)
    finally:
        listener.stop()


def test_keyboard_press_is_idempotent_until_release():
    """Two consecutive presses (no release in between) should fire on_press only once."""
    fire_count = {"press": 0, "release": 0}
    listener = button_listener.ButtonListener(mode="keyboard", key="space")
    listener.on_press = lambda: fire_count.__setitem__("press", fire_count["press"] + 1)
    listener.on_release = lambda: fire_count.__setitem__("release", fire_count["release"] + 1)
    listener.start()
    try:
        _Listener.last.on_press(_KeyNamespace.space)
        _Listener.last.on_press(_KeyNamespace.space)
        _Listener.last.on_press(_KeyNamespace.space)
        assert _wait_until(lambda: fire_count["press"] == 1, timeout=0.5)
        # Pressing wrong key shouldn't fire either callback
        _Listener.last.on_press(_KeyNamespace.enter)
        time.sleep(0.05)
        assert fire_count["press"] == 1
    finally:
        listener.stop()


def test_keyboard_ignores_non_target_key():
    fired = {"any": 0}
    listener = button_listener.ButtonListener(mode="keyboard", key="space")
    listener.on_press = lambda: fired.__setitem__("any", fired["any"] + 1)
    listener.start()
    try:
        _Listener.last.on_press(_KeyNamespace.enter)
        _Listener.last.on_press(_CharKey("a"))
        time.sleep(0.05)
        assert fired["any"] == 0
    finally:
        listener.stop()


def test_keyboard_mode_with_char_key():
    fired = threading.Event()
    listener = button_listener.ButtonListener(mode="keyboard", key="a")
    listener.on_press = fired.set
    listener.start()
    try:
        _Listener.last.on_press(_CharKey("a"))
        assert _wait_until(fired.is_set)
    finally:
        listener.stop()


def test_unknown_mode_raises_value_error():
    listener = button_listener.ButtonListener(mode="bluetooth", key="space")
    with pytest.raises(ValueError, match="Unknown button mode"):
        listener.start()


def test_stop_cleans_up_pynput_listener():
    listener = button_listener.ButtonListener(mode="keyboard", key="space")
    listener.start()
    captured = _Listener.last
    listener.stop()
    assert captured.stopped is True


def test_double_start_is_a_noop():
    listener = button_listener.ButtonListener(mode="keyboard", key="space")
    listener.start()
    first = _Listener.last
    listener.start()
    # No new pynput.Listener was constructed
    assert _Listener.last is first
    listener.stop()


def test_gpio_mode_raises_clear_error_when_unavailable(monkeypatch):
    """On non-Pi hosts, RPi.GPIO is missing — gpio mode should fail loudly."""
    # Ensure RPi.GPIO is not importable
    monkeypatch.setitem(sys.modules, "RPi", None)
    listener = button_listener.ButtonListener(mode="gpio", gpio_pin=17)
    with pytest.raises(ImportError, match="RPi.GPIO"):
        listener.start()


def test_callbacks_run_in_separate_threads():
    """Callbacks should run in daemon threads so a slow callback doesn't block the listener."""
    main_thread_id = threading.get_ident()
    captured: dict = {}

    def press_handler():
        captured["thread_id"] = threading.get_ident()
        captured["done"] = True

    listener = button_listener.ButtonListener(mode="keyboard", key="space")
    listener.on_press = press_handler
    listener.start()
    try:
        _Listener.last.on_press(_KeyNamespace.space)
        assert _wait_until(lambda: captured.get("done"), timeout=0.5)
        assert captured["thread_id"] != main_thread_id
    finally:
        listener.stop()
