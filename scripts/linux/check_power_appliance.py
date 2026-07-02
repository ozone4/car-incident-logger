#!/usr/bin/env python3
"""Read-only diagnostics for Linux car dashcam appliance power lifecycle.

This script intentionally does not change systemd state, power settings, or
running services. It is safe to run over SSH while diagnosing whether the
ThinkPad power watcher is enabled, fresh, and seeing AC/battery correctly.
"""

from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from modules.power_status import read_power_status  # noqa: E402

SERVICE_NAMES = ["car-incident-logger.service", "car-incident-power-watch.service"]


def run_read_only(command: list[str], timeout: float = 5.0) -> dict[str, Any]:
    try:
        result = subprocess.run(command, text=True, capture_output=True, timeout=timeout, check=False)
        return {
            "ok": result.returncode == 0,
            "returncode": result.returncode,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
        }
    except FileNotFoundError:
        return {"ok": False, "returncode": None, "stdout": "", "stderr": f"{command[0]} not found"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "returncode": None, "stdout": "", "stderr": "command timed out"}


def parse_timestamp(value: object) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def load_config(project_dir: Path) -> tuple[dict[str, Any], str | None]:
    path = project_dir / "config.yaml"
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return loaded if isinstance(loaded, dict) else {}, None
    except Exception as exc:  # noqa: BLE001 - diagnostics should report, not crash
        return {}, str(exc)


def read_json_file(path: Path) -> tuple[dict[str, Any], str | None]:
    try:
        if not path.exists():
            return {}, "missing"
        loaded = json.loads(path.read_text(encoding="utf-8"))
        return loaded if isinstance(loaded, dict) else {}, None
    except Exception as exc:  # noqa: BLE001
        return {}, str(exc)


def collect_service_status() -> dict[str, dict[str, Any]]:
    services: dict[str, dict[str, Any]] = {}
    for name in SERVICE_NAMES:
        services[name] = {
            "is_enabled": run_read_only(["systemctl", "is-enabled", name]),
            "is_active": run_read_only(["systemctl", "is-active", name]),
            "recent_logs": run_read_only(["journalctl", "-u", name, "-n", "40", "--no-pager"], timeout=8),
        }
    return services


def collect(project_dir: Path) -> dict[str, Any]:
    config, config_error = load_config(project_dir)
    appliance = config.get("appliance", {}) if isinstance(config.get("appliance", {}), dict) else {}
    state_file = Path(str(appliance.get("state_file", "./data/appliance-power-state.json")))
    if not state_file.is_absolute():
        state_file = project_dir / state_file

    state, state_error = read_json_file(state_file)
    updated_ts = parse_timestamp(state.get("updated_at"))
    state_age_seconds = round(time.time() - updated_ts, 1) if updated_ts is not None else None
    check_interval = float(appliance.get("check_interval_seconds", 5) or 5)
    stale_after = max(30.0, check_interval * 3.0)
    watcher_stale = updated_ts is None or state_age_seconds is None or state_age_seconds < 0 or state_age_seconds > stale_after

    live_power = read_power_status()

    return {
        "host": {
            "hostname": platform.node(),
            "platform": platform.platform(),
            "python": sys.version.split()[0],
            "project_dir": str(project_dir),
        },
        "config": {
            "error": config_error,
            "appliance": appliance,
            "appliance_enabled": appliance.get("enabled"),
            "dashcam": config.get("dashcam", {}) if isinstance(config.get("dashcam", {}), dict) else {},
        },
        "state_file": {
            "path": str(state_file),
            "error": state_error,
            "exists": state_file.exists(),
            "updated_at": state.get("updated_at"),
            "age_seconds": state_age_seconds,
            "stale_after_seconds": stale_after,
            "watcher_stale": watcher_stale,
            "state": state.get("state"),
            "power": state.get("power") if isinstance(state.get("power"), dict) else None,
            "grace_remaining_seconds": state.get("grace_remaining_seconds"),
        },
        "live_power": live_power,
        "services": collect_service_status(),
        "checks": {
            "appliance_enabled": appliance.get("enabled") is True,
            "watcher_state_fresh": not watcher_stale,
            "live_power_available": live_power.get("available") is True,
        },
    }


def print_text(report: dict[str, Any]) -> None:
    print("Car Incident Logger appliance diagnostics")
    print("========================================")
    print(f"host: {report['host']['hostname']}")
    print(f"project: {report['host']['project_dir']}")
    print()

    cfg = report["config"]
    appliance = cfg.get("appliance", {})
    print("config")
    print(f"  appliance.enabled: {appliance.get('enabled')}")
    print(f"  battery_grace_seconds: {appliance.get('battery_grace_seconds')}")
    print(f"  critical_battery_percent: {appliance.get('critical_battery_percent')}")
    print(f"  state_file: {appliance.get('state_file')}")
    if cfg.get("error"):
        print(f"  config error: {cfg['error']}")
    print()

    state = report["state_file"]
    print("watcher state file")
    print(f"  path: {state['path']}")
    print(f"  exists: {state['exists']}")
    print(f"  updated_at: {state['updated_at']}")
    print(f"  age_seconds: {state['age_seconds']}")
    print(f"  watcher_stale: {state['watcher_stale']}")
    print(f"  state: {state['state']}")
    if state.get("error"):
        print(f"  error: {state['error']}")
    print()

    power = report["live_power"]
    print("live power")
    print(f"  available: {power.get('available')}")
    print(f"  state: {power.get('state')}")
    print(f"  on_ac: {power.get('on_ac')}")
    print(f"  battery_percent: {power.get('battery_percent')}")
    print()

    print("services")
    for name, svc in report["services"].items():
        print(f"  {name}")
        print(f"    enabled: {svc['is_enabled']['stdout'] or svc['is_enabled']['stderr']}")
        print(f"    active: {svc['is_active']['stdout'] or svc['is_active']['stderr']}")
    print()

    print("checks")
    for key, ok in report["checks"].items():
        print(f"  {key}: {'OK' if ok else 'CHECK'}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only Linux appliance power diagnostics")
    parser.add_argument("--project-dir", default=str(PROJECT_ROOT), help="Car Incident Logger project directory")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of human text")
    args = parser.parse_args()

    report = collect(Path(args.project_dir).resolve())
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print_text(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
