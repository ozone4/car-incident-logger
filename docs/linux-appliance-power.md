# Linux appliance power lifecycle

The car logger can behave like a dashcam appliance when installed on a Linux laptop:

- app/service start → camera, rolling buffer, loop recorder, and ALPR auto-start
- AC disconnected → power watcher keeps recording for a grace period
- grace elapsed or critical battery → camera/recording stops, filesystems sync, system suspends
- resume/boot → systemd restarts services and app auto-starts recording again

## Important boundary

The Python app cannot guarantee that a fully off or suspended laptop powers on when car power appears. That depends on Lenovo/ThinkPad firmware and Linux wake support. Enable/check BIOS/UEFI options such as **Power on with AC attach** / **Wake on AC**. App code only handles what happens after Linux boots or resumes.

## Read-only diagnostics

Run this on the Lenovo from the project directory:

```bash
python3 scripts/linux/check_power_appliance.py
```

For machine-readable output:

```bash
python3 scripts/linux/check_power_appliance.py --json
```

The diagnostic is intentionally read-only. It checks:

- `appliance.enabled` from `config.yaml`
- dashcam auto-start config
- live `/sys/class/power_supply` AC/battery state
- `data/appliance-power-state.json` freshness
- `car-incident-logger.service` enabled/active status
- `car-incident-power-watch.service` enabled/active status
- recent journal snippets for both services

Red flags:

- `appliance.enabled: false` — watcher exits without suspending on power loss.
- `watcher_stale: true` — state file is missing, old, future-dated, or malformed.
- live power says `on_ac: true` while watcher file says `battery` — stale watcher state or stopped watcher.
- power-watch service inactive/failed — watcher is not protecting power loss.

## Safe runtime checks

These are read-only:

```bash
systemctl is-enabled car-incident-logger.service car-incident-power-watch.service
systemctl is-active car-incident-logger.service car-incident-power-watch.service
systemctl status car-incident-power-watch.service --no-pager
journalctl -u car-incident-power-watch.service -n 120 --no-pager
curl -s http://127.0.0.1:5000/system/power | python3 -m json.tool
curl -s http://127.0.0.1:5000/appliance/status | python3 -m json.tool
```

Check firmware/OS wake evidence:

```bash
cat /proc/acpi/wakeup 2>/dev/null || true
grep -RInE '^(HandleLidSwitch|HandleLidSwitchExternalPower|HandlePowerKey|IdleAction|IdleActionSec)' \
  /etc/systemd/logind.conf /etc/systemd/logind.conf.d 2>/dev/null || true
```

## Manual tests

Only run real suspend/power-unplug tests with Owen present and after confirming recording files are safe.

Suggested sequence:

1. Run diagnostics while AC is connected.
2. Confirm `/system/power` reports `on_ac: true`.
3. Unplug car/AC power.
4. Confirm `/system/power` flips to `on_ac: false`.
5. Confirm `check_power_appliance.py` shows a fresh watcher state and decreasing grace remaining.
6. Reconnect AC before grace expires; confirm state returns to AC and no suspend occurs.
7. For real suspend testing, temporarily use a short grace period and watch journal logs locally.

Avoid running `systemctl suspend`, service restarts, or config-changing commands remotely unless Owen is present and prepared to recover the laptop.
