#!/usr/bin/env bash
# setup_wifi_ap.sh — Configure Lenovo as a Wi-Fi access point for the Car Logger iPad.
#
# Reads wifi_ap settings from config.yaml (python3 + PyYAML required).
# Must be run as root or with sudo.
#
# What this does:
#   1. Installs hostapd + dnsmasq if missing.
#   2. Writes /etc/car-logger/hostapd.conf and dnsmasq.conf from templates.
#   3. Installs and enables car-logger-ap.service.
#   4. Optionally prevents NetworkManager from managing the AP interface.
#
# Usage:
#   sudo bash scripts/linux/setup_wifi_ap.sh [--dry-run]
#
# After running, the iPad dashboard will be reachable at:
#   http://<static_ip>:5000/dashboard

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
DEPLOY_DIR="${PROJECT_DIR}/deploy/linux"
DRY_RUN=false

for arg in "$@"; do
  [[ "$arg" == "--dry-run" ]] && DRY_RUN=true
done

log() { echo "[setup_wifi_ap] $*"; }
die() { echo "[setup_wifi_ap] ERROR: $*" >&2; exit 1; }
run() {
  if $DRY_RUN; then
    echo "[DRY-RUN] $*"
  else
    "$@"
  fi
}

[[ $EUID -eq 0 ]] || die "Run with sudo: sudo bash $0"

# ── Read config from config.yaml ──────────────────────────────────────────────
log "Reading config.yaml..."
read_yaml() {
  python3 -c "
import yaml, sys
with open('${PROJECT_DIR}/config.yaml') as f:
    cfg = yaml.safe_load(f)
key = '$1'.split('.')
node = cfg
for k in key:
    node = node.get(k, '')
print(node or '')
"
}

ENABLED="$(read_yaml wifi_ap.enabled)"
if [[ "$ENABLED" != "True" && "$ENABLED" != "true" && "$ENABLED" != "1" ]]; then
  log "wifi_ap.enabled is not true in config.yaml — skipping setup."
  log "Set wifi_ap.enabled: true and re-run to configure the AP."
  exit 0
fi

INTERFACE="$(read_yaml wifi_ap.interface)"
SSID="$(read_yaml wifi_ap.ssid)"
COUNTRY="$(read_yaml wifi_ap.country_code)"
STATIC_IP="$(read_yaml wifi_ap.static_ip)"
DHCP_START="$(read_yaml wifi_ap.dhcp_range_start)"
DHCP_END="$(read_yaml wifi_ap.dhcp_range_end)"
PASSWORD="$(read_yaml wifi_ap.password)"

INTERFACE="${INTERFACE:-wlan0}"
SSID="${SSID:-CarLogger}"
COUNTRY="${COUNTRY:-CA}"
STATIC_IP="${STATIC_IP:-192.168.77.1}"
DHCP_START="${DHCP_START:-192.168.77.10}"
DHCP_END="${DHCP_END:-192.168.77.50}"

log "  interface : $INTERFACE"
log "  SSID      : $SSID"
log "  static_ip : $STATIC_IP"
log "  country   : $COUNTRY"
log "  DHCP      : $DHCP_START – $DHCP_END"
log "  password  : ${PASSWORD:-(none — open AP)}"

# ── Install packages ───────────────────────────────────────────────────────────
log "Installing hostapd and dnsmasq..."
run apt-get install -y hostapd dnsmasq

# ── Create config directory ────────────────────────────────────────────────────
run mkdir -p /etc/car-logger
run mkdir -p /var/lib/dnsmasq

# ── Write hostapd.conf ────────────────────────────────────────────────────────
HOSTAPD_CONF="$(cat "${DEPLOY_DIR}/wifi-ap/hostapd.conf")"

if [[ -n "$PASSWORD" ]]; then
  WPA_BLOCK="wpa=2
wpa_passphrase=${PASSWORD}
wpa_key_mgmt=WPA-PSK
wpa_pairwise=CCMP
rsn_pairwise=CCMP"
else
  WPA_BLOCK="# No WPA — open network"
fi

HOSTAPD_CONF="${HOSTAPD_CONF//__INTERFACE__/$INTERFACE}"
HOSTAPD_CONF="${HOSTAPD_CONF//__SSID__/$SSID}"
HOSTAPD_CONF="${HOSTAPD_CONF//__COUNTRY_CODE__/$COUNTRY}"
HOSTAPD_CONF="${HOSTAPD_CONF//__WPA_BLOCK__/$WPA_BLOCK}"

if $DRY_RUN; then
  echo "[DRY-RUN] Would write /etc/car-logger/hostapd.conf"
else
  echo "$HOSTAPD_CONF" > /etc/car-logger/hostapd.conf
  log "Wrote /etc/car-logger/hostapd.conf"
fi

# ── Write dnsmasq.conf ────────────────────────────────────────────────────────
DNSMASQ_CONF="$(cat "${DEPLOY_DIR}/wifi-ap/dnsmasq.conf")"
DNSMASQ_CONF="${DNSMASQ_CONF//__INTERFACE__/$INTERFACE}"
DNSMASQ_CONF="${DNSMASQ_CONF//__STATIC_IP__/$STATIC_IP}"
DNSMASQ_CONF="${DNSMASQ_CONF//__DHCP_START__/$DHCP_START}"
DNSMASQ_CONF="${DNSMASQ_CONF//__DHCP_END__/$DHCP_END}"

if $DRY_RUN; then
  echo "[DRY-RUN] Would write /etc/car-logger/dnsmasq.conf"
else
  echo "$DNSMASQ_CONF" > /etc/car-logger/dnsmasq.conf
  log "Wrote /etc/car-logger/dnsmasq.conf"
fi

# ── Install and fill systemd service ─────────────────────────────────────────
AP_SVC="$(cat "${DEPLOY_DIR}/systemd/car-logger-ap.service")"
AP_SVC="${AP_SVC//__INTERFACE__/$INTERFACE}"
AP_SVC="${AP_SVC//__STATIC_IP__/$STATIC_IP}"

if $DRY_RUN; then
  echo "[DRY-RUN] Would write /etc/systemd/system/car-logger-ap.service"
else
  echo "$AP_SVC" > /etc/systemd/system/car-logger-ap.service
  log "Wrote /etc/systemd/system/car-logger-ap.service"
fi

# ── Prevent NetworkManager from taking over the AP interface ──────────────────
NM_UNMANAGED="/etc/NetworkManager/conf.d/car-logger-ap-unmanaged.conf"
if command -v nmcli &>/dev/null; then
  if $DRY_RUN; then
    echo "[DRY-RUN] Would write $NM_UNMANAGED"
  else
    cat > "$NM_UNMANAGED" <<EOF
[keyfile]
unmanaged-devices=interface-name:${INTERFACE}
EOF
    log "Wrote $NM_UNMANAGED (prevents NM from reclaiming $INTERFACE)"
    systemctl reload NetworkManager 2>/dev/null || true
  fi
fi

# ── Reload + enable services ───────────────────────────────────────────────────
run systemctl daemon-reload
run systemctl enable car-logger-ap.service
run systemctl start  car-logger-ap.service

log ""
log "Done! iPad dashboard should be reachable at:"
log "  http://${STATIC_IP}:5000/dashboard"
log ""
log "Check AP status with:"
log "  systemctl status car-logger-ap"
log "  journalctl -u car-logger-ap -f"
