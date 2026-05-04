#!/usr/bin/env bash
# install_deps.sh — Install Python dependencies for Car Incident Logger
# Usage: bash scripts/install_deps.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

echo "============================================================"
echo " Car Incident Logger — Dependency Installer"
echo "============================================================"

# ── Python version check ──────────────────────────────────────────────────────
python3 -c "import sys; assert sys.version_info >= (3,9), 'Python 3.9+ required'" \
  || { echo "ERROR: Python 3.9 or newer is required."; exit 1; }

# ── Core pip install ──────────────────────────────────────────────────────────
echo ""
echo "[1/4] Installing core Python dependencies..."
pip install --upgrade pip
pip install -r "$REPO_ROOT/requirements.txt"

# ── Whisper model download ────────────────────────────────────────────────────
echo ""
echo "[2/4] Pre-downloading faster-whisper model (base.en)..."
echo "      This downloads ~150 MB the first time. Subsequent runs use the cache."
python3 - <<'PYEOF'
from faster_whisper import WhisperModel
import os, pathlib

models_dir = pathlib.Path("data/models")
models_dir.mkdir(parents=True, exist_ok=True)

print("  Loading base.en model (this may take a minute)...")
model = WhisperModel("base.en", device="cpu", compute_type="int8", download_root=str(models_dir))
print("  Model ready.")
PYEOF

# ── Database init ─────────────────────────────────────────────────────────────
echo ""
echo "[3/4] Initialising SQLite database..."
python3 "$REPO_ROOT/scripts/setup_db.py"

# ── Raspberry Pi GPIO (optional) ──────────────────────────────────────────────
echo ""
echo "[4/4] Checking platform for GPIO support..."
if python3 -c "import RPi.GPIO" 2>/dev/null; then
  echo "  RPi.GPIO already installed."
elif [[ "$(uname -m)" == "aarch64" || "$(uname -m)" == "armv7l" ]]; then
  echo "  Raspberry Pi detected — installing RPi.GPIO..."
  pip install RPi.GPIO>=0.7.1
else
  echo "  Not a Raspberry Pi — skipping RPi.GPIO (keyboard mode will be used)."
fi

echo ""
echo "============================================================"
echo " Setup complete."
echo " Edit config.yaml to configure your camera, button, etc."
echo " Run with: python3 main.py"
echo "============================================================"
