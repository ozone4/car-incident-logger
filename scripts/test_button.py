#!/usr/bin/env python3
"""
test_button.py -- Print button press/release events for hardware testing.

Usage:
    python3 scripts/test_button.py                      # keyboard mode, space key
    python3 scripts/test_button.py --mode keyboard --key f1
    python3 scripts/test_button.py --mode gpio --pin 17  # Raspberry Pi only

Press Ctrl+C to exit.

NOTE (macOS): You must grant Accessibility permissions to Terminal / your
Python binary for pynput keyboard monitoring to work.
"""

import argparse
import sys
import time
from pathlib import Path

# Allow running from repo root or scripts/
repo_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(repo_root))


def test_keyboard(key: str) -> None:
    """Listen for keyboard press/release and print events."""
    try:
        from pynput import keyboard as pynput_keyboard
    except ImportError:
        print("ERROR: pynput is not installed. Run: pip install pynput")
        sys.exit(1)

    print(f"Listening for key={key!r}  (press Ctrl+C to quit)")
    print("-" * 50)

    pressed = False

    def on_press(k):
        nonlocal pressed
        name = _key_name(k)
        if name == key and not pressed:
            pressed = True
            print(f"  [PRESS]   key={name}  t={time.strftime('%H:%M:%S')}")

    def on_release(k):
        nonlocal pressed
        name = _key_name(k)
        if name == key and pressed:
            pressed = False
            print(f"  [RELEASE] key={name}  t={time.strftime('%H:%M:%S')}")

    def _key_name(k) -> str:
        if k == pynput_keyboard.Key.space:
            return "space"
        if k == pynput_keyboard.Key.enter:
            return "enter"
        if hasattr(k, "char") and k.char is not None:
            return k.char.lower()
        name = getattr(k, "name", None)
        if name:
            return name.lower()
        return str(k).lower()

    listener = pynput_keyboard.Listener(on_press=on_press, on_release=on_release)
    listener.daemon = True
    listener.start()

    try:
        while True:
            time.sleep(0.1)
    except KeyboardInterrupt:
        print("\nDone.")
    finally:
        listener.stop()


def test_gpio(pin: int, pull: str) -> None:
    """Listen for GPIO button press/release and print events."""
    try:
        import RPi.GPIO as GPIO
    except ImportError:
        print("ERROR: RPi.GPIO is not installed (only available on Raspberry Pi).")
        sys.exit(1)

    GPIO.setmode(GPIO.BCM)
    pull_ud = GPIO.PUD_UP if pull == "up" else GPIO.PUD_DOWN
    GPIO.setup(pin, GPIO.IN, pull_up_down=pull_ud)

    print(f"Listening on GPIO BCM pin {pin} (pull={pull})  (press Ctrl+C to quit)")
    print("-" * 50)

    pressed = False

    def callback(channel):
        nonlocal pressed
        state = GPIO.input(pin)
        if pull == "up":
            pressed_now = state == GPIO.LOW
        else:
            pressed_now = state == GPIO.HIGH

        if pressed_now and not pressed:
            pressed = True
            print(f"  [PRESS]   pin={pin}  t={time.strftime('%H:%M:%S')}")
        elif not pressed_now and pressed:
            pressed = False
            print(f"  [RELEASE] pin={pin}  t={time.strftime('%H:%M:%S')}")

    GPIO.add_event_detect(pin, GPIO.BOTH, callback=callback, bouncetime=50)

    try:
        while True:
            time.sleep(0.1)
    except KeyboardInterrupt:
        print("\nDone.")
    finally:
        GPIO.remove_event_detect(pin)
        GPIO.cleanup(pin)


def main():
    parser = argparse.ArgumentParser(description="Test button press/release events")
    parser.add_argument("--mode", choices=["keyboard", "gpio"], default="keyboard")
    parser.add_argument("--key", default="space", help="Key to listen for (keyboard mode)")
    parser.add_argument("--pin", type=int, default=17, help="BCM GPIO pin (gpio mode)")
    parser.add_argument("--pull", choices=["up", "down"], default="up", help="Pull resistor (gpio mode)")
    args = parser.parse_args()

    if args.mode == "keyboard":
        test_keyboard(args.key.lower())
    else:
        test_gpio(args.pin, args.pull)


if __name__ == "__main__":
    main()
