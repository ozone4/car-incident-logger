# Linux Appliance Mode

This mode turns the dashcam laptop into a boot-and-record appliance for a Linux ThinkPad-style install. An iPad is the touch dashboard, connected directly via the Lenovo's own Wi-Fi access point.

Target behaviour:

1. AC power present → dashboard/app starts automatically and records normally.
2. AC power lost → keep recording for a configurable grace period.
3. Grace expires, or battery becomes critical → stop camera/recording cleanly, `sync`, then suspend.
4. Power returns / laptop resumes → restart the app service so camera handles and recording threads come back cleanly.

> Suspend is the default strategy. It is faster and simpler than hibernate, and ThinkPads usually support Linux suspend well. The only hardware-dependent piece is whether the laptop wakes automatically when AC returns.

---

## Install

On the Linux laptop:

```bash
git clone https://github.com/ozone4/car-incident-logger.git
cd car-incident-logger
bash scripts/install_linux_appliance.sh
```

The installer:

- Creates `.venv`
- Installs Python requirements
- Initializes the SQLite DB
- Installs and enables:
  - `car-incident-logger.service`
  - `car-incident-power-watch.service`
- Installs a `systemd-sleep` resume hook
- Starts both services

---

## Wi-Fi Access Point (iPad Connection)

The Lenovo creates its own Wi-Fi network so the iPad connects directly without internet.

### Setup

1. Edit `config.yaml` and set `wifi_ap.enabled: true`. Adjust `interface`, `ssid`, and `password` as needed:

```yaml
wifi_ap:
  enabled: true
  interface: wlan0           # check with: ip link show
  ssid: CarLogger
  country_code: CA
  static_ip: 192.168.77.1
  dhcp_range_start: 192.168.77.10
  dhcp_range_end: 192.168.77.50
  password: ""               # empty = open network
```

2. Run the setup script:

```bash
sudo bash scripts/linux/setup_wifi_ap.sh
```

This installs `hostapd` + `dnsmasq`, writes configs to `/etc/car-logger/`, and enables `car-logger-ap.service`.

3. On the iPad, join the `CarLogger` Wi-Fi network and open Safari to:

```
http://192.168.77.1:5000/dashboard
```

Add to home screen for full-screen kiosk mode (Settings → Add to Home Screen in Safari).

### AP Troubleshooting

```bash
# Check AP status
systemctl status car-logger-ap
journalctl -u car-logger-ap -f

# Check DHCP leases (iPad should appear here)
cat /var/lib/dnsmasq/car-logger-ap.leases

# Check interface
ip addr show wlan0
```

If the AP fails after suspend/resume, restart it:

```bash
sudo systemctl restart car-logger-ap
```

The resume hook (`deploy/linux/systemd-sleep/car-incident-logger-resume`) restarts both `car-logger-ap` and `car-incident-logger` on resume automatically.

---

## GPS

Connect a USB GPS dongle. Common models appear as `/dev/ttyUSB0`.

Edit `config.yaml`:

```yaml
gps:
  enabled: true
  backend: auto              # tries gpsd first, falls back to serial
  serial_port: /dev/ttyUSB0
  baud_rate: 9600
  poll_interval_seconds: 1.0
  stale_after_seconds: 10.0
```

If you have gpsd running:

```bash
sudo apt-get install gpsd gpsd-clients
sudo gpsd /dev/ttyUSB0 -F /var/run/gpsd.sock
# test
cgps -s
```

For direct serial (no gpsd), install dependencies:

```bash
.venv/bin/pip install pyserial pynmea2
```

GPS status is visible in the dashboard at `http://192.168.77.1:5000/gps/status` and as the live speed widget in the top bar.

### Trip Tracking

While the logger is running with a GPS fix, breadcrumbs are sampled every 5 seconds:

```yaml
trip_tracker:
  enabled: true
  sample_interval_seconds: 5.0
```

Trip data is stored in the `trips` and `trip_points` tables in `data/plates.db`. The `/trip/current` API returns live trip status.

---

## Dashboard

```text
http://<laptop-ip>:5000/
```

When connected via the Lenovo AP:

```text
http://192.168.77.1:5000/dashboard
```

Camera default:

- On the ThinkPad X1 Carbon, `/dev/video0` is the integrated webcam.
- The Arducam 1080P Ultra-lowlight usually appears as `/dev/video2`, so the default `camera.device_index` is `2`.
- To confirm on the laptop:

```bash
v4l2-ctl --list-devices
```

---

## Services

Main app:

```bash
sudo systemctl status car-incident-logger
journalctl -u car-incident-logger -f
```

Power watcher:

```bash
sudo systemctl status car-incident-power-watch
journalctl -u car-incident-power-watch -f
```

Wi-Fi AP:

```bash
sudo systemctl status car-logger-ap
journalctl -u car-logger-ap -f
```

Restart all:

```bash
sudo systemctl restart car-logger-ap car-incident-logger car-incident-power-watch
```

---

## Power behaviour config

Edit `config.yaml`:

```yaml
appliance:
  enabled: true
  app_url: http://127.0.0.1:5000
  check_interval_seconds: 5
  battery_grace_seconds: 600
  critical_battery_percent: 12
  stop_before_suspend: true
  restart_after_resume: true
  suspend_command: systemctl suspend
```

Recommended starting values for the ThinkPad X1 Carbon Gen 3:

- `battery_grace_seconds: 600` — keep recording for 10 minutes after AC loss.
- `critical_battery_percent: 12` — suspend before the battery gets dangerously low.
- `stop_before_suspend: true` — close video files and camera handles before sleep.
- `restart_after_resume: true` — asks the app to start camera/recording after resume.

For testing without actually suspending:

```bash
.venv/bin/python scripts/linux/dashcam-power-watch.py --dry-run
```

---

## BIOS / Linux settings to check

In ThinkPad BIOS/UEFI, look for power options like:

- Wake on AC attach / Power on with AC attach
- Always On USB / USB power while sleeping
- Disable deep sleep if it breaks USB camera resume

On Linux, confirm suspend works manually:

```bash
sudo systemctl suspend
```

After resume, check:

```bash
systemctl status car-incident-logger car-logger-ap car-incident-power-watch
curl http://127.0.0.1:5000/health
curl http://127.0.0.1:5000/gps/status
curl http://127.0.0.1:5000/trip/current
curl http://127.0.0.1:5000/appliance/status
```

The dashboard shows a **Linux appliance** card with AC/battery state, suspend grace countdown, camera/recording state, and last resume time.

If the laptop does **not** wake automatically when AC returns, the app will still resume cleanly once the laptop is woken by lid/power button. Later options include RTC wake polling or BIOS-specific AC wake tuning.

---

## How it works

### `car-logger-ap.service`

Runs `hostapd` (AP) and `dnsmasq` (DHCP) on the configured Wi-Fi interface. Brings up the static IP before hostapd starts. Installed by `scripts/linux/setup_wifi_ap.sh`.

### `car-incident-logger.service`

Runs:

```bash
.venv/bin/python web/app.py --host 0.0.0.0 --port 5000
```

The web app auto-starts camera, dashcam buffer, loop recording, storage cleanup, GPS reader, trip tracker, and ALPR if configured.

### `car-incident-power-watch.service`

Runs:

```bash
.venv/bin/python scripts/linux/dashcam-power-watch.py --config config.yaml
```

It reads Linux power state from `/sys/class/power_supply`. When suspend is required it calls:

1. `POST /camera/stop`
2. `sync`
3. `systemctl suspend`

### Resume hook

Installed at:

```text
/lib/systemd/system-sleep/car-incident-logger-resume
```

On resume, it restarts:

```bash
car-logger-ap.service
car-incident-logger.service
car-incident-power-watch.service
```

That restart is intentional: USB cameras, OpenCV/V4L2 handles, and Wi-Fi AP interfaces are often the flaky part after suspend, so a clean service restart is more reliable than trying to keep stale handles alive.
