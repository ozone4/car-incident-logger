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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

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


@dataclass
class PowerWatchRuntime:
    battery_since: float | None = None
    battery_since_wall: str | None = None
    last_state: str | None = None
    last_suspend_reason: str | None = None
    last_suspend_at: str | None = None
    last_resume_at: str | None = None


@dataclass(frozen=True)
class PowerTickDecision:
    action: Literal["none", "suspend"]
    reason: str
    state_payload: dict[str, Any]


def _ac_state_payload(
    status: dict[str, Any],
    runtime: PowerWatchRuntime,
    grace_seconds: float,
    now_wall: str,
) -> dict[str, Any]:
    return {
        "updated_at": now_wall,
        "state": "ac" if status.get("on_ac") is True else "unknown",
        "battery_since": None,
        "battery_elapsed_seconds": 0,
        "grace_seconds": grace_seconds,
        "grace_remaining_seconds": grace_seconds,
        "last_suspend_reason": runtime.last_suspend_reason,
        "last_suspend_at": runtime.last_suspend_at,
        "last_resume_at": runtime.last_resume_at,
        "power": status,
    }


def _battery_state_payload(
    status: dict[str, Any],
    runtime: PowerWatchRuntime,
    grace_seconds: float,
    now_monotonic: float,
    now_wall: str,
    *,
    suspending: bool,
    reason: str | None = None,
) -> dict[str, Any]:
    elapsed = now_monotonic - runtime.battery_since if runtime.battery_since is not None else 0
    return {
        "updated_at": now_wall,
        "state": "suspending" if suspending else "battery",
        "battery_since": runtime.battery_since_wall,
        "battery_elapsed_seconds": round(elapsed, 1),
        "grace_seconds": grace_seconds,
        "grace_remaining_seconds": 0 if suspending else max(0, round(grace_seconds - elapsed, 1)),
        "last_suspend_reason": reason if suspending else runtime.last_suspend_reason,
        "last_suspend_at": runtime.last_suspend_at,
        "last_resume_at": runtime.last_resume_at,
        "power": status,
    }


def should_suspend(
    status: dict[str, Any],
    battery_since: float | None,
    grace_seconds: float,
    critical_percent: int,
    *,
    now_monotonic: float | None = None,
) -> tuple[bool, str]:
    if status.get("on_ac") is not False:
        return False, "not_on_battery"

    pct = status.get("battery_percent")
    if isinstance(pct, int) and pct <= critical_percent:
        return True, f"critical_battery_{pct}%"

    monotonic_now = time.monotonic() if now_monotonic is None else now_monotonic
    if battery_since is not None and (monotonic_now - battery_since) >= grace_seconds:
        return True, f"battery_grace_elapsed_{int(grace_seconds)}s"

    return False, "within_grace"


def evaluate_power_tick(
    status: dict[str, Any],
    runtime: PowerWatchRuntime,
    grace_seconds: float,
    critical_percent: int,
    now_monotonic: float,
    now_wall: str,
) -> PowerTickDecision:
    """Evaluate one watcher loop without side effects.

    Mutates runtime timers/last suspend metadata, but does not write files,
    call HTTP endpoints, run sync, or run suspend. This keeps power-loss
    behavior testable without risking the host running the tests.
    """
    if status.get("on_ac") is False:
        if runtime.battery_since is None:
            runtime.battery_since = now_monotonic
            runtime.battery_since_wall = now_wall

        suspend, reason = should_suspend(
            status,
            runtime.battery_since,
            grace_seconds,
            critical_percent,
            now_monotonic=now_monotonic,
        )
        if suspend:
            runtime.last_suspend_reason = reason
            runtime.last_suspend_at = now_wall
            payload = _battery_state_payload(
                status,
                runtime,
                grace_seconds,
                now_monotonic,
                now_wall,
                suspending=True,
                reason=reason,
            )
            return PowerTickDecision("suspend", reason, payload)

        payload = _battery_state_payload(
            status,
            runtime,
            grace_seconds,
            now_monotonic,
            now_wall,
            suspending=False,
        )
        return PowerTickDecision("none", reason, payload)

    runtime.battery_since = None
    runtime.battery_since_wall = None
    payload = _ac_state_payload(status, runtime, grace_seconds, now_wall)
    return PowerTickDecision("none", "not_on_battery", payload)


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


def handle_suspend_decision(
    decision: PowerTickDecision,
    runtime: PowerWatchRuntime,
    app_url: str,
    stop_before_suspend: bool,
    restart_after_resume: bool,
    suspend_command: str,
    dry_run: bool,
) -> None:
    if decision.action != "suspend":
        return

    LOG.warning("Preparing to suspend: %s", decision.reason)
    if dry_run:
        LOG.warning("Dry-run enabled; skipping camera stop, sync, and suspend")
        runtime.battery_since = None
        runtime.battery_since_wall = None
        return

    prepare_for_suspend(app_url, stop_before_suspend)
    rc = run_shell(suspend_command)
    runtime.last_resume_at = utc_now()
    LOG.info("Suspend command returned rc=%s; system has resumed or command failed", rc)
    runtime.battery_since = None
    runtime.battery_since_wall = None
    time.sleep(5)
    if restart_after_resume:
        request_resume_start(app_url)


def request_resume_start(app_url: str) -> None:
    ok, body = post_json(f"{app_url.rstrip('/')}/camera/start", timeout=8)
    if ok:
        LOG.info("Camera/recording start requested after resume")
    else:
        LOG.warning("Could not request camera start after resume: %s", body)


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
    runtime = PowerWatchRuntime()

    LOG.info("Power watcher started: grace=%ss critical=%s%% app=%s", int(grace), critical, app_url)

    while True:
        status = read_power_status()
        state = str(status.get("state", "unknown"))

        if state != runtime.last_state:
            LOG.info("Power state: %s battery=%s%%", state, status.get("battery_percent"))
            runtime.last_state = state

        previous_battery_since = runtime.battery_since
        decision = evaluate_power_tick(
            status,
            runtime,
            grace,
            critical,
            now_monotonic=time.monotonic(),
            now_wall=utc_now(),
        )
        if status.get("on_ac") is False and previous_battery_since is None:
            LOG.warning("AC power lost; continuing for %s seconds before suspend", int(grace))

        write_state(state_file, decision.state_payload)

        handle_suspend_decision(
            decision=decision,
            runtime=runtime,
            app_url=app_url,
            stop_before_suspend=bool(cfg["stop_before_suspend"]),
            restart_after_resume=bool(cfg["restart_after_resume"]),
            suspend_command=suspend_command,
            dry_run=args.dry_run,
        )

        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
