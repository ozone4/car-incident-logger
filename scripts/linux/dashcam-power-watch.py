#!/usr/bin/env python3
"""Suspend-first power watcher for Linux dashcam appliance installs.

Behavior:
- AC present: do nothing; the web app/systemd service records normally.
- AC lost: start a grace timer while recording continues.
- Grace expires or battery is critical: ask the app to stop camera/recording,
  flush filesystems, then suspend.
- After resume: reset timers and optionally ask the app to start camera again.

This script is designed to run as a systemd service next to the Flask app.
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from modules.power_status import read_power_status  # noqa: E402

LOG = logging.getLogger("dashcam-power-watch")

DEFAULTS = {
    "enabled": True,
    "app_url": "http://127.0.0.1:5000",
    "check_interval_seconds": 5,
    "battery_grace_seconds": 600,
    "critical_battery_percent": 12,
    "stop_before_suspend": True,
    "restart_after_resume": True,
    "suspend_command": "systemctl suspend",
    "state_file": "./data/appliance-power-state.json",
}


def load_appliance_config(config_path: Path) -> dict[str, Any]:
    data: dict[str, Any] = {}
    if config_path.exists():
        with config_path.open("r", encoding="utf-8") as f:
            loaded = yaml.safe_load(f) or {}
            if isinstance(loaded, dict):
                section = loaded.get("appliance", {})
                if isinstance(section, dict):
                    data.update(section)
    return {**DEFAULTS, **data}


def post_json(url: str, timeout: float = 5.0) -> tuple[bool, str]:
    req = urllib.request.Request(url, data=b"{}", headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - local appliance URL from config
            body = resp.read(500).decode("utf-8", errors="replace")
            return 200 <= resp.status < 300, body
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}: {exc.read(500).decode('utf-8', errors='replace')}"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def run_shell(command: str, timeout: float | None = None) -> int:
    LOG.info("Running: %s", command)
    return subprocess.run(command, shell=True, timeout=timeout, check=False).returncode  # noqa: S602 - admin-configured local command


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def write_state(state_file: Path, payload: dict[str, Any]) -> None:
    try:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = state_file.with_suffix(state_file.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(state_file)
    except OSError as exc:
        LOG.warning("Could not write state file %s: %s", state_file, exc)


def prepare_for_suspend(app_url: str, stop_before_suspend: bool) -> None:
    if stop_before_suspend:
        ok, body = post_json(f"{app_url.rstrip('/')}/camera/stop", timeout=8)
        if ok:
            LOG.info("Camera/recording stopped before suspend")
        else:
            LOG.warning("Could not stop camera cleanly before suspend: %s", body)

    # Make sure recording segments and SQLite WAL data are pushed out before sleep.
    run_shell("sync", timeout=15)


def request_resume_start(app_url: str) -> None:
    ok, body = post_json(f"{app_url.rstrip('/')}/camera/start", timeout=8)
    if ok:
        LOG.info("Camera/recording start requested after resume")
    else:
        LOG.warning("Could not request camera start after resume: %s", body)


def should_suspend(status: dict[str, Any], battery_since: float | None, grace_seconds: float, critical_percent: int) -> tuple[bool, str]:
    if status.get("on_ac") is not False:
        return False, "not_on_battery"

    pct = status.get("battery_percent")
    if isinstance(pct, int) and pct <= critical_percent:
        return True, f"critical_battery_{pct}%"

    if battery_since is not None and (time.monotonic() - battery_since) >= grace_seconds:
        return True, f"battery_grace_elapsed_{int(grace_seconds)}s"

    return False, "within_grace"


def main() -> int:
    parser = argparse.ArgumentParser(description="Linux dashcam power watcher")
    parser.add_argument("--config", default=str(PROJECT_ROOT / "config.yaml"), help="Path to config.yaml")
    parser.add_argument("--dry-run", action="store_true", help="Log intended actions but do not suspend")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")

    cfg = load_appliance_config(Path(args.config))
    if not cfg.get("enabled", True):
        LOG.info("Appliance power watcher disabled in config")
        return 0

    interval = max(1.0, float(cfg["check_interval_seconds"]))
    grace = max(0.0, float(cfg["battery_grace_seconds"]))
    critical = int(cfg["critical_battery_percent"])
    app_url = str(cfg["app_url"])
    suspend_command = str(cfg["suspend_command"])
    state_file = Path(str(cfg["state_file"]))
    if not state_file.is_absolute():
        state_file = PROJECT_ROOT / state_file
    battery_since: float | None = None
    battery_since_wall: str | None = None
    last_state: str | None = None
    last_suspend_reason: str | None = None
    last_suspend_at: str | None = None
    last_resume_at: str | None = None

    LOG.info("Power watcher started: grace=%ss critical=%s%% app=%s", int(grace), critical, app_url)

    while True:
        status = read_power_status()
        state = str(status.get("state", "unknown"))

        if state != last_state:
            LOG.info("Power state: %s battery=%s%%", state, status.get("battery_percent"))
            last_state = state

        if status.get("on_ac") is False:
            if battery_since is None:
                battery_since = time.monotonic()
                battery_since_wall = utc_now()
                LOG.warning("AC power lost; continuing for %s seconds before suspend", int(grace))

            elapsed = time.monotonic() - battery_since if battery_since is not None else 0
            write_state(state_file, {
                "updated_at": utc_now(),
                "state": "battery",
                "battery_since": battery_since_wall,
                "battery_elapsed_seconds": round(elapsed, 1),
                "grace_seconds": grace,
                "grace_remaining_seconds": max(0, round(grace - elapsed, 1)),
                "last_suspend_reason": last_suspend_reason,
                "last_suspend_at": last_suspend_at,
                "last_resume_at": last_resume_at,
                "power": status,
            })

            suspend, reason = should_suspend(status, battery_since, grace, critical)
            if suspend:
                LOG.warning("Preparing to suspend: %s", reason)
                prepare_for_suspend(app_url, bool(cfg["stop_before_suspend"]))
                last_suspend_reason = reason
                last_suspend_at = utc_now()
                write_state(state_file, {
                    "updated_at": utc_now(),
                    "state": "suspending",
                    "battery_since": battery_since_wall,
                    "grace_seconds": grace,
                    "grace_remaining_seconds": 0,
                    "last_suspend_reason": last_suspend_reason,
                    "last_suspend_at": last_suspend_at,
                    "last_resume_at": last_resume_at,
                    "power": status,
                })
                if args.dry_run:
                    LOG.warning("Dry-run enabled; skipping suspend")
                    battery_since = None
                    battery_since_wall = None
                else:
                    rc = run_shell(suspend_command)
                    last_resume_at = utc_now()
                    LOG.info("Suspend command returned rc=%s; system has resumed or command failed", rc)
                    battery_since = None
                    battery_since_wall = None
                    time.sleep(5)
                    if bool(cfg["restart_after_resume"]):
                        request_resume_start(app_url)
        else:
            battery_since = None
            battery_since_wall = None
            write_state(state_file, {
                "updated_at": utc_now(),
                "state": "ac" if status.get("on_ac") is True else "unknown",
                "battery_since": None,
                "battery_elapsed_seconds": 0,
                "grace_seconds": grace,
                "grace_remaining_seconds": grace,
                "last_suspend_reason": last_suspend_reason,
                "last_suspend_at": last_suspend_at,
                "last_resume_at": last_resume_at,
                "power": status,
            })

        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
