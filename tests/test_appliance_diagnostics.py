import subprocess
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = PROJECT_ROOT / "scripts" / "linux" / "check_power_appliance.py"


def test_power_appliance_diagnostics_script_exists_and_is_read_only():
    assert SCRIPT.exists()
    text = SCRIPT.read_text(encoding="utf-8")
    forbidden = [
        "systemctl restart",
        "systemctl stop",
        "systemctl start",
        "systemctl enable",
        "systemctl disable",
        "systemctl suspend",
        "shutdown",
        "reboot",
        "pm-suspend",
    ]
    for phrase in forbidden:
        assert phrase not in text


def test_power_appliance_diagnostics_accepts_project_dir_and_json_output():
    result = subprocess.run(
        ["python3", str(SCRIPT), "--project-dir", str(PROJECT_ROOT), "--json"],
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert '"checks"' in result.stdout
    assert '"config"' in result.stdout
    assert '"state_file"' in result.stdout
    assert '"services"' in result.stdout
    assert '"live_power"' in result.stdout


def test_power_appliance_diagnostics_text_output_names_key_checks():
    result = subprocess.run(
        ["python3", str(SCRIPT), "--project-dir", str(PROJECT_ROOT)],
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    output = result.stdout
    assert "Car Incident Logger appliance diagnostics" in output
    assert "appliance.enabled" in output
    assert "watcher state file" in output
    assert "car-incident-power-watch.service" in output
    assert "live power" in output
