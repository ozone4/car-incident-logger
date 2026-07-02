import importlib.util
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
WATCHER_PATH = PROJECT_ROOT / "scripts" / "linux" / "dashcam-power-watch.py"


def load_watcher():
    spec = importlib.util.spec_from_file_location("dashcam_power_watch", WATCHER_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_power_tick_on_ac_resets_battery_timer_and_writes_ac_state():
    watcher = load_watcher()
    runtime = watcher.PowerWatchRuntime(
        battery_since=90.0,
        battery_since_wall="2026-07-02T19:59:50Z",
        last_suspend_reason="critical_battery_10%",
        last_suspend_at="2026-07-02T19:00:00Z",
        last_resume_at="2026-07-02T19:05:00Z",
    )

    decision = watcher.evaluate_power_tick(
        status={"on_ac": True, "state": "ac", "battery_percent": 91},
        runtime=runtime,
        grace_seconds=600,
        critical_percent=12,
        now_monotonic=100.0,
        now_wall="2026-07-02T20:00:00Z",
    )

    assert decision.action == "none"
    assert runtime.battery_since is None
    assert runtime.battery_since_wall is None
    assert decision.state_payload["state"] == "ac"
    assert decision.state_payload["grace_remaining_seconds"] == 600
    assert decision.state_payload["last_suspend_reason"] == "critical_battery_10%"


def test_power_tick_first_battery_observation_starts_grace_without_suspend():
    watcher = load_watcher()
    runtime = watcher.PowerWatchRuntime()

    decision = watcher.evaluate_power_tick(
        status={"on_ac": False, "state": "battery", "battery_percent": 80},
        runtime=runtime,
        grace_seconds=600,
        critical_percent=12,
        now_monotonic=100.0,
        now_wall="2026-07-02T20:00:00Z",
    )

    assert decision.action == "none"
    assert runtime.battery_since == 100.0
    assert runtime.battery_since_wall == "2026-07-02T20:00:00Z"
    assert decision.reason == "within_grace"
    assert decision.state_payload["state"] == "battery"
    assert decision.state_payload["battery_elapsed_seconds"] == 0
    assert decision.state_payload["grace_remaining_seconds"] == 600


def test_power_tick_battery_grace_elapsed_requests_suspend():
    watcher = load_watcher()
    runtime = watcher.PowerWatchRuntime(
        battery_since=100.0,
        battery_since_wall="2026-07-02T20:00:00Z",
    )

    decision = watcher.evaluate_power_tick(
        status={"on_ac": False, "state": "battery", "battery_percent": 70},
        runtime=runtime,
        grace_seconds=600,
        critical_percent=12,
        now_monotonic=700.0,
        now_wall="2026-07-02T20:10:00Z",
    )

    assert decision.action == "suspend"
    assert decision.reason == "battery_grace_elapsed_600s"
    assert decision.state_payload["state"] == "suspending"
    assert decision.state_payload["grace_remaining_seconds"] == 0
    assert decision.state_payload["last_suspend_reason"] == "battery_grace_elapsed_600s"
    assert runtime.last_suspend_reason == "battery_grace_elapsed_600s"
    assert runtime.last_suspend_at == "2026-07-02T20:10:00Z"


def test_power_tick_critical_battery_requests_immediate_suspend():
    watcher = load_watcher()
    runtime = watcher.PowerWatchRuntime(
        battery_since=100.0,
        battery_since_wall="2026-07-02T20:00:00Z",
    )

    decision = watcher.evaluate_power_tick(
        status={"on_ac": False, "state": "battery", "battery_percent": 10},
        runtime=runtime,
        grace_seconds=600,
        critical_percent=12,
        now_monotonic=105.0,
        now_wall="2026-07-02T20:00:05Z",
    )

    assert decision.action == "suspend"
    assert decision.reason == "critical_battery_10%"
    assert decision.state_payload["state"] == "suspending"
    assert decision.state_payload["battery_since"] == "2026-07-02T20:00:00Z"


def test_power_tick_unknown_ac_does_not_start_battery_grace():
    watcher = load_watcher()
    runtime = watcher.PowerWatchRuntime()

    decision = watcher.evaluate_power_tick(
        status={"on_ac": None, "state": "unknown", "battery_percent": 66},
        runtime=runtime,
        grace_seconds=600,
        critical_percent=12,
        now_monotonic=100.0,
        now_wall="2026-07-02T20:00:00Z",
    )

    assert decision.action == "none"
    assert runtime.battery_since is None
    assert decision.reason == "not_on_battery"
    assert decision.state_payload["state"] == "unknown"
    assert decision.state_payload["grace_remaining_seconds"] == 600


def test_handle_suspend_decision_dry_run_skips_all_side_effects(monkeypatch):
    watcher = load_watcher()
    runtime = watcher.PowerWatchRuntime(
        battery_since=100.0,
        battery_since_wall="2026-07-02T20:00:00Z",
        last_suspend_reason="battery_grace_elapsed_600s",
        last_suspend_at="2026-07-02T20:10:00Z",
    )
    decision = watcher.PowerTickDecision("suspend", "battery_grace_elapsed_600s", {"state": "suspending"})
    calls = []

    monkeypatch.setattr(watcher, "prepare_for_suspend", lambda *args, **kwargs: calls.append("prepare"))
    monkeypatch.setattr(watcher, "run_shell", lambda *args, **kwargs: calls.append("suspend") or 0)
    monkeypatch.setattr(watcher, "request_resume_start", lambda *args, **kwargs: calls.append("resume"))

    watcher.handle_suspend_decision(
        decision=decision,
        runtime=runtime,
        app_url="http://127.0.0.1:5000",
        stop_before_suspend=True,
        restart_after_resume=True,
        suspend_command="systemctl suspend",
        dry_run=True,
    )

    assert calls == []
    assert runtime.battery_since is None
    assert runtime.battery_since_wall is None
    assert runtime.last_resume_at is None


def test_handle_suspend_decision_runs_production_suspend_sequence(monkeypatch):
    watcher = load_watcher()
    runtime = watcher.PowerWatchRuntime(
        battery_since=100.0,
        battery_since_wall="2026-07-02T20:00:00Z",
        last_suspend_reason="battery_grace_elapsed_600s",
        last_suspend_at="2026-07-02T20:10:00Z",
    )
    decision = watcher.PowerTickDecision("suspend", "battery_grace_elapsed_600s", {"state": "suspending"})
    calls = []

    monkeypatch.setattr(watcher, "prepare_for_suspend", lambda app_url, stop: calls.append(("prepare", app_url, stop)))
    monkeypatch.setattr(watcher, "run_shell", lambda command: calls.append(("suspend", command)) or 0)
    monkeypatch.setattr(watcher, "utc_now", lambda: "2026-07-02T20:10:30Z")
    monkeypatch.setattr(watcher.time, "sleep", lambda seconds: calls.append(("sleep", seconds)))
    monkeypatch.setattr(watcher, "request_resume_start", lambda app_url: calls.append(("resume", app_url)))

    watcher.handle_suspend_decision(
        decision=decision,
        runtime=runtime,
        app_url="http://127.0.0.1:5000",
        stop_before_suspend=True,
        restart_after_resume=True,
        suspend_command="systemctl suspend",
        dry_run=False,
    )

    assert calls == [
        ("prepare", "http://127.0.0.1:5000", True),
        ("suspend", "systemctl suspend"),
        ("sleep", 5),
        ("resume", "http://127.0.0.1:5000"),
    ]
    assert runtime.battery_since is None
    assert runtime.battery_since_wall is None
    assert runtime.last_resume_at == "2026-07-02T20:10:30Z"
