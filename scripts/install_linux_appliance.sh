#!/usr/bin/env bash
# Run from the repo root on the target Linux laptop:
#   bash scripts/install_linux_appliance.sh
#
# Read-only preflight/diagnostic mode:
#   bash scripts/install_linux_appliance.sh --check-only
#

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_USER="${SUDO_USER:-$USER}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
CHECK_ONLY=0

usage() {
  cat <<'EOF'
Usage: bash scripts/install_linux_appliance.sh [--check-only]

Options:
  --check-only   Run read-only diagnostics/preflight checks and exit before
                 dependency installation, database setup, systemd writes, or
                 service restarts.
  -h, --help     Show this help.

After install, see docs/linux-appliance-power.md and run:
  python3 scripts/linux/check_power_appliance.py
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --check-only)
      CHECK_ONLY=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "This installer is for Linux targets only." >&2
  exit 1
fi

if ! command -v systemctl >/dev/null 2>&1; then
  echo "systemctl not found; this installer expects a systemd Linux distro." >&2
  exit 1
fi

if [[ ! -f "$PROJECT_DIR/config.yaml" ]]; then
  echo "config.yaml not found in $PROJECT_DIR" >&2
  exit 1
fi

if [[ "$CHECK_ONLY" == "1" ]]; then
  echo "==> Checking Car Incident Logger appliance (read-only)"
else
  echo "==> Installing Car Incident Logger appliance"
fi
echo "Project: $PROJECT_DIR"
echo "Service user: $SERVICE_USER"

cd "$PROJECT_DIR"

if [[ "$CHECK_ONLY" == "1" ]]; then
  echo "==> Running read-only appliance diagnostics (--check-only)"
  "$PYTHON_BIN" scripts/linux/check_power_appliance.py --project-dir "$PROJECT_DIR"
  echo
  echo "No changes made. To install or update services, run without --check-only."
  echo "Docs: docs/linux-appliance-power.md"
  exit 0
fi

if [[ ! -d .venv ]]; then
  echo "==> Creating virtualenv"
  "$PYTHON_BIN" -m venv .venv
fi

echo "==> Installing Python dependencies"
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt

if [[ -f requirements-alpr.txt ]]; then
  echo "==> Installing optional ALPR dependencies"
  .venv/bin/python -m pip install -r requirements-alpr.txt || echo "WARN: ALPR dependencies failed; app can still run without ALPR."
fi

echo "==> Initializing database"
.venv/bin/python scripts/setup_db.py

echo "==> Installing systemd units"
TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"' EXIT

sed \
  -e "s#__PROJECT_DIR__#$PROJECT_DIR#g" \
  -e "s#__USER__#$SERVICE_USER#g" \
  deploy/linux/systemd/car-incident-logger.service > "$TMPDIR/car-incident-logger.service"

sed \
  -e "s#__PROJECT_DIR__#$PROJECT_DIR#g" \
  -e "s#__USER__#$SERVICE_USER#g" \
  deploy/linux/systemd/car-incident-power-watch.service > "$TMPDIR/car-incident-power-watch.service"

sudo install -m 0644 "$TMPDIR/car-incident-logger.service" /etc/systemd/system/car-incident-logger.service
sudo install -m 0644 "$TMPDIR/car-incident-power-watch.service" /etc/systemd/system/car-incident-power-watch.service
sudo install -m 0755 deploy/linux/systemd-sleep/car-incident-logger-resume /lib/systemd/system-sleep/car-incident-logger-resume

sudo systemctl daemon-reload
sudo systemctl enable car-incident-logger.service car-incident-power-watch.service
sudo systemctl restart car-incident-logger.service car-incident-power-watch.service

echo "==> Done"
echo "Dashboard: http://$(hostname -I | awk '{print $1}'):5000/"
echo "Logs:      journalctl -u car-incident-logger -f"
echo "Power:     journalctl -u car-incident-power-watch -f"
echo "Check:     python3 scripts/linux/check_power_appliance.py"
echo "Docs:      docs/linux-appliance-power.md"
