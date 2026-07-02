from pathlib import Path
from unittest.mock import patch

from modules.power_status import read_power_status


def _supply(root: Path, name: str, values: dict[str, object]) -> None:
    d = root / name
    d.mkdir(parents=True)
    for key, value in values.items():
        (d / key).write_text(str(value), encoding="utf-8")


def test_reads_ac_online_and_lowest_present_battery_capacity(tmp_path):
    _supply(tmp_path, "AC", {"type": "Mains", "online": 1})
    _supply(tmp_path, "BAT0", {"type": "Battery", "capacity": 91, "status": "Not charging", "present": 1})
    _supply(tmp_path, "BAT1", {"type": "Battery", "capacity": 74, "status": "Discharging", "present": 1})

    with patch("modules.power_status.platform.system", return_value="Linux"):
        status = read_power_status(tmp_path)

    assert status["available"] is True
    assert status["on_ac"] is True
    assert status["state"] == "ac"
    assert status["battery_percent"] == 74
    assert [s["name"] for s in status["ac_supplies"]] == ["AC"]
    assert [b["name"] for b in status["batteries"]] == ["BAT0", "BAT1"]


def test_reads_usb_c_supply_as_ac_when_online(tmp_path):
    _supply(tmp_path, "ucsi-source-psy-USBC000:001", {"type": "USB_C", "online": 1})
    _supply(tmp_path, "BAT0", {"type": "Battery", "capacity": 55, "status": "Charging"})

    with patch("modules.power_status.platform.system", return_value="Linux"):
        status = read_power_status(tmp_path)

    assert status["on_ac"] is True
    assert status["state"] == "ac"
    assert status["ac_supplies"][0]["type"] == "usb_c"


def test_reads_battery_state_when_ac_offline(tmp_path):
    _supply(tmp_path, "ADP1", {"type": "Mains", "online": 0})
    _supply(tmp_path, "BAT0", {"type": "Battery", "capacity": 44, "status": "Discharging"})

    with patch("modules.power_status.platform.system", return_value="Linux"):
        status = read_power_status(tmp_path)

    assert status["on_ac"] is False
    assert status["state"] == "battery"
    assert status["battery_percent"] == 44


def test_missing_ac_online_reports_unknown_power_source(tmp_path):
    _supply(tmp_path, "AC", {"type": "Mains"})
    _supply(tmp_path, "BAT0", {"type": "Battery", "capacity": 66, "status": "Unknown"})

    with patch("modules.power_status.platform.system", return_value="Linux"):
        status = read_power_status(tmp_path)

    assert status["on_ac"] is None
    assert status["state"] == "unknown"
    assert status["battery_percent"] == 66


def test_absent_linux_power_supply_path_is_unavailable(tmp_path):
    missing = tmp_path / "missing"

    with patch("modules.power_status.platform.system", return_value="Linux"):
        status = read_power_status(missing)

    assert status["available"] is False
    assert status["on_ac"] is None
    assert status["batteries"] == []
    assert status["ac_supplies"] == []
