"""
button_listener.py — Physical or keyboard button press/release detection.

Supports two modes:
  • keyboard — uses pynput to watch for a configurable key (default: Space).
    Works cross-platform (Linux, macOS, Windows).
  • gpio     — uses RPi.GPIO for a physical momentary button on a Raspberry Pi.
    Import is wrapped in try/except so it won't crash on non-Pi hardware.

Usage:
    listener = ButtonListener(mode="keyboard", key="space")
    listener.on_press = lambda: print("pressed")
    listener.on_release = lambda: print("released")
    listener.start()
    ...
    listener.stop()
"""

import logging
import threading
from typing import Callable, Optional

logger = logging.getLogger(__name__)


class ButtonListener:
    def __init__(
        self,
        mode: str = "keyboard",
        key: str = "space",
        gpio_pin: int = 17,
        gpio_pull: str = "up",
    ):
        self.mode = mode.lower()
        self.key = key.lower()
        self.gpio_pin = gpio_pin
        self.gpio_pull = gpio_pull.lower()

        self.on_press: Optional[Callable[[], None]] = None
        self.on_release: Optional[Callable[[], None]] = None

        self._listener = None  # pynput listener or None
        self._running = False
        self._pressed = False
        self._lock = threading.Lock()

    # ── Public API ────────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._running:
            return
        if self.mode == "keyboard":
            self._start_keyboard()
        elif self.mode == "gpio":
            self._start_gpio()
        else:
            raise ValueError(f"Unknown button mode: {self.mode!r}. Use 'keyboard' or 'gpio'.")
        self._running = True
        logger.info("ButtonListener started (mode=%s)", self.mode)

    def stop(self) -> None:
        self._running = False
        if self.mode == "keyboard" and self._listener is not None:
            try:
                self._listener.stop()
            except Exception:
                pass
        elif self.mode == "gpio":
            self._stop_gpio()
        logger.info("ButtonListener stopped")

    # ── Keyboard mode ─────────────────────────────────────────────────────────

    def _start_keyboard(self) -> None:
        try:
            from pynput import keyboard as pynput_keyboard
        except ImportError:
            raise ImportError(
                "pynput is required for keyboard mode. Install with: pip install pynput"
            )

        target_key = self._resolve_pynput_key(pynput_keyboard)

        def on_press(key):
            try:
                resolved = self._normalize_key(key)
            except Exception:
                return
            if resolved == self.key:
                with self._lock:
                    if not self._pressed:
                        self._pressed = True
                        self._fire(self.on_press, "on_press")

        def on_release(key):
            try:
                resolved = self._normalize_key(key)
            except Exception:
                return
            if resolved == self.key:
                with self._lock:
                    if self._pressed:
                        self._pressed = False
                        self._fire(self.on_release, "on_release")

        self._listener = pynput_keyboard.Listener(
            on_press=on_press, on_release=on_release
        )
        self._listener.daemon = True
        self._listener.start()

    def _normalize_key(self, key) -> str:
        """Convert a pynput key to a lowercase string."""
        from pynput import keyboard as pynput_keyboard

        if key == pynput_keyboard.Key.space:
            return "space"
        if key == pynput_keyboard.Key.enter:
            return "enter"
        if hasattr(key, "char") and key.char is not None:
            return key.char.lower()
        # Key.f1 → "f1", etc.
        name = getattr(key, "name", None)
        if name:
            return name.lower()
        return str(key).lower()

    def _resolve_pynput_key(self, pynput_keyboard):
        """Return the pynput Key for special keys, or just the string for chars."""
        special = {
            "space": pynput_keyboard.Key.space,
            "enter": pynput_keyboard.Key.enter,
            "f1": pynput_keyboard.Key.f1,
            "f2": pynput_keyboard.Key.f2,
            "f3": pynput_keyboard.Key.f3,
            "f4": pynput_keyboard.Key.f4,
        }
        return special.get(self.key, self.key)

    # ── GPIO mode ─────────────────────────────────────────────────────────────

    def _start_gpio(self) -> None:
        try:
            import RPi.GPIO as GPIO  # type: ignore
        except ImportError:
            raise ImportError(
                "RPi.GPIO is required for GPIO mode. "
                "Install with: pip install RPi.GPIO  (Raspberry Pi only)"
            )

        GPIO.setmode(GPIO.BCM)
        pull = GPIO.PUD_UP if self.gpio_pull == "up" else GPIO.PUD_DOWN
        GPIO.setup(self.gpio_pin, GPIO.IN, pull_up_down=pull)

        # Edge detection: BOTH so we catch press and release
        def _gpio_callback(channel):
            state = GPIO.input(self.gpio_pin)
            # Active-low (pull-up): LOW = pressed, HIGH = released
            if self.gpio_pull == "up":
                pressed_now = state == GPIO.LOW
            else:
                pressed_now = state == GPIO.HIGH

            with self._lock:
                if pressed_now and not self._pressed:
                    self._pressed = True
                    self._fire(self.on_press, "on_press")
                elif not pressed_now and self._pressed:
                    self._pressed = False
                    self._fire(self.on_release, "on_release")

        GPIO.add_event_detect(
            self.gpio_pin,
            GPIO.BOTH,
            callback=_gpio_callback,
            bouncetime=50,  # ms debounce
        )
        self._gpio = GPIO  # keep reference for cleanup
        logger.info("GPIO button on BCM pin %d (pull=%s)", self.gpio_pin, self.gpio_pull)

    def _stop_gpio(self) -> None:
        try:
            GPIO = self._gpio  # type: ignore
            GPIO.remove_event_detect(self.gpio_pin)
            GPIO.cleanup(self.gpio_pin)
        except Exception as e:
            logger.debug("GPIO cleanup error: %s", e)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _fire(self, callback: Optional[Callable], name: str) -> None:
        if callback is None:
            return
        try:
            t = threading.Thread(target=callback, daemon=True, name=f"ButtonCallback-{name}")
            t.start()
        except Exception as e:
            logger.error("Error in button callback %s: %s", name, e)
