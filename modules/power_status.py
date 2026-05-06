"""
power_status.py — lightweight Linux AC/battery status helpers.

Reads /sys/class/power_supply directly so the app can report power state without
requiring upower, dbus, or desktop services. On non-Linux systems, the helpers
return an explicit unavailable state.
"""

from __future__ import annotations

import platform
from pathlib import Path
from typing import Any

POWER_SUPPLY_PATH = Path("/sys/class/power_supply")


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _read_int(path: Path) -> int | None:
    value = _read_text(path)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def read_power_status(base_path: Path = POWER_SUPPLY_PATH) -> dict[str, Any]:
    """Return AC/battery state using Linux power_supply sysfs.

    Shape is intentionally stable for both the Flask API and the standalone
    power watcher script.
    """
    if platform.system().lower() != "linux":
        return {
            "available": False,
            "reason": "power_supply sysfs is only available on Linux",
            "on_ac": None,
            "batteries": [],
            "ac_supplies": [],
        }

    if not base_path.exists():
        return {
            "available": False,
            "reason": f"{base_path} does not exist",
            "on_ac": None,
            "batteries": [],
            "ac_supplies": [],
        }

    ac_supplies: list[dict[str, Any]] = []
    batteries: list[dict[str, Any]] = []

    for supply in sorted(base_path.iterdir()):
        supply_type = (_read_text(supply / "type") or "").lower()
        name = supply.name

        if supply_type in {"mains", "usb", "usb_c", "usb_pd"} or name.upper().startswith(("AC", "ADP")):
            online = _read_int(supply / "online")
            ac_supplies.append({"name": name, "type": supply_type or "unknown", "online": bool(online) if online is not None else None})
            continue

        if supply_type == "battery" or name.upper().startswith("BAT"):
            capacity = _read_int(supply / "capacity")
            status = _read_text(supply / "status")
            batteries.append({
                "name": name,
                "type": supply_type or "battery",
                "capacity_percent": capacity,
                "status": status,
                "present": (_read_int(supply / "present") != 0) if (supply / "present").exists() else True,
            })

    online_values = [s["online"] for s in ac_supplies if s.get("online") is not None]
    on_ac = any(online_values) if online_values else None

    present_batteries = [b for b in batteries if b.get("present", True)]
    capacities = [b["capacity_percent"] for b in present_batteries if b.get("capacity_percent") is not None]
    battery_percent = min(capacities) if capacities else None

    if on_ac is True:
        state = "ac"
    elif on_ac is False:
        state = "battery"
    else:
        state = "unknown"

    return {
        "available": True,
        "state": state,
        "on_ac": on_ac,
        "battery_percent": battery_percent,
        "batteries": present_batteries,
        "ac_supplies": ac_supplies,
    }
