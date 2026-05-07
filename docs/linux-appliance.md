# Linux Appliance Mode

This mode turns the dashcam laptop into a boot-and-record appliance for a Linux ThinkPad-style install.

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

Dashboard:

```text
http://<laptop-ip>:5000/
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

Restart both:

```bash
sudo systemctl restart car-incident-logger car-incident-power-watch
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
systemctl status car-incident-logger car-incident-power-watch
curl http://127.0.0.1:5000/health
curl http://127.0.0.1:5000/system/power
curl http://127.0.0.1:5000/appliance/status
```

The dashboard also shows a **Linux appliance** card with AC/battery state, suspend grace countdown, camera/recording state, and last resume time.

If the laptop does **not** wake automatically when AC returns, the app will still resume cleanly once the laptop is woken by lid/power button. Later options include RTC wake polling or BIOS-specific AC wake tuning.

---

## How it works

### `car-incident-logger.service`

Runs:

```bash
.venv/bin/python web/app.py --host 0.0.0.0 --port 5000
```

The web app already auto-starts camera, dashcam buffer, loop recording, storage cleanup, and ALPR if configured.

### `car-incident-power-watch.service`

Runs:

```bash
.venv/bin/python scripts/linux/dashcam-power-watch.py --config config.yaml
```

It reads Linux power state from:

```text
/sys/class/power_supply
```

When suspend is required it calls:

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
car-incident-logger.service
car-incident-power-watch.service
```

That restart is intentional: USB cameras and OpenCV/V4L2 handles are often the flaky part after suspend, so a clean service restart is more reliable than trying to keep stale handles alive.
