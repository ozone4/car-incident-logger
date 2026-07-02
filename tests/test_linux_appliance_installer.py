from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
INSTALLER = PROJECT_ROOT / "scripts" / "install_linux_appliance.sh"


def test_installer_has_check_only_mode_before_mutating_system():
    text = INSTALLER.read_text(encoding="utf-8")

    assert "--check-only" in text
    assert "CHECK_ONLY" in text
    assert "scripts/linux/check_power_appliance.py" in text

    first_mutation = min(
        idx for idx in [
            text.find("pip install"),
            text.find("systemctl enable"),
            text.find("systemctl restart"),
            text.find("install -m 0644"),
        ] if idx != -1
    )
    check_only_branch = text.find("if [[ \"$CHECK_ONLY\" == \"1\" ]]")
    assert check_only_branch != -1
    assert check_only_branch < first_mutation


def test_installer_announces_post_install_diagnostics():
    text = INSTALLER.read_text(encoding="utf-8")

    assert "python3 scripts/linux/check_power_appliance.py" in text
    assert "docs/linux-appliance-power.md" in text
    assert "--check-only" in text


def test_installer_help_and_shell_syntax_are_valid():
    import subprocess

    syntax = subprocess.run(["bash", "-n", str(INSTALLER)], text=True, capture_output=True, check=False)
    assert syntax.returncode == 0, syntax.stderr

    help_result = subprocess.run(["bash", str(INSTALLER), "--help"], text=True, capture_output=True, check=False)
    assert help_result.returncode == 0
    assert "--check-only" in help_result.stdout
    assert "python3 scripts/linux/check_power_appliance.py" in help_result.stdout


def test_installer_check_only_branch_does_not_restart_or_enable_services():
    text = INSTALLER.read_text(encoding="utf-8")
    marker = "if [[ \"$CHECK_ONLY\" == \"1\" ]]"
    branch_start = text.find(marker)
    assert branch_start != -1
    branch_end = text.find("fi", branch_start)
    assert branch_end != -1
    branch = text[branch_start:branch_end]

    forbidden = ["systemctl restart", "systemctl enable", "pip install", "scripts/setup_db.py", "install -m"]
    for phrase in forbidden:
        assert phrase not in branch
