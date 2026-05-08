# CLAUDE.md — Car Incident Logger

## Current product direction

This project is evolving into a fully local **Lenovo Linux + iPad in-car appliance**:

- **Lenovo laptop** stays plugged in/tucked away in the vehicle and acts as the recorder/server.
- Lenovo runs the camera, dashcam buffer, ALPR, SQLite database, GPS reader, Linux power watcher, and local web app.
- Lenovo should create its own Wi‑Fi access point so a mounted **10-inch iPad** can connect directly without internet.
- **iPad** is the touch dashboard/display, opened at the Lenovo's local AP address, e.g. `http://192.168.77.1:5000`.
- A USB GPS dongle on the Lenovo provides speed, location, heading, and trip context.
- GPS snapshots should attach to dashcam incidents, tagged/known plate events, and trip breadcrumbs.

Default implementation assumptions unless Owen says otherwise:

- GPS backend: `gpsd` first, direct NMEA serial fallback second.
- Speed units: default to km/h; dashboard can later expose km/h ↔ mph toggle.
- Wi‑Fi AP: built-in Lenovo Wi‑Fi by default, but interface name must be configurable (`wlan0`, `wlp*`, or USB `wlan1`).
- AP subnet: prefer `192.168.77.0/24`, Lenovo at `192.168.77.1`, iPad DHCP range roughly `.10–.50`.

## What this project does

A local, in-car incident logging and dashcam system. It records video from a USB/Arducam camera, maintains rolling pre-roll, captures incident clips, performs live ALPR, stores sightings/incidents in SQLite, and exposes a Flask/React dashboard for control and review.

The system should work offline. Internet is not required for in-car operation once dependencies/models are installed.

## Architecture

```text
main.py / web/app.py              Orchestrator + Flask dashboard/API
  ├── CameraCapture               Background thread: reads USB camera frames into a queue
  ├── RollingBuffer               Consumes frame queue into pre-roll buffer
  ├── DashcamRecorder             Saves incident clips + metadata from rolling buffer
  ├── ALPRRunner                  YOLO/EasyOCR/PaddleOCR plate recognition with fallback modes
  ├── LiveMatcher                 Background ALPR scanning / known-plate matching
  ├── PlateDatabase               SQLite with incidents/sightings/known vehicles
  ├── GPSReader                   GPS state from gpsd; should gain serial NMEA fallback
  ├── TripTracker                 Planned: samples GPS into trips/breadcrumbs
  ├── Linux appliance watcher     AC loss grace period, clean stop/sync/suspend/resume
  └── Web dashboard               iPad-friendly touch UI served locally over Lenovo AP
```

## Key flows

**Boot / appliance startup:**
1. Linux starts Wi‑Fi AP service.
2. Car logger service starts Flask app bound to LAN/AP interface.
3. Camera + recording buffer start automatically.
4. Live ALPR starts if configured and engines are available.
5. GPS reader/trip tracker start and expose current GPS state.
6. iPad connects to Lenovo AP and opens dashboard.

**Incident capture:**
1. Dashboard/button calls `/dashcam/trigger`.
2. Backend accepts request immediately and captures asynchronously.
3. Recorder saves pre-roll + post-roll clip.
4. Metadata should include timestamp, camera info, ALPR context, and nearest GPS snapshot.
5. Dashboard polls status until `capturing → saving → saved` or error.

**Tagged/known plate event:**
1. Live ALPR scans frames.
2. Match is written as a sighting/incident-like event.
3. GPS snapshot should be attached: lat/lon, speed_kmh, heading, altitude/fix quality where available.
4. Dashboard surfaces active/recent sightings with location/speed context.

**Power behavior:**
1. AC power present: run normally.
2. AC lost: continue recording for configured grace period.
3. Grace expires or battery critical: stop camera/recording cleanly, `sync`, then suspend.
4. Resume/power return: restart app/camera/AP as needed.

## Implementation plan — Lenovo AP + iPad + GPS

### Phase A — GPS enhancement

Relevant files:

- `modules/gps_reader.py`
- `modules/config_manager.py`
- `config.yaml`
- `web/app.py`
- tests under `tests/`

Plan:

- Extend GPS config with `enabled`, `backend`, `serial_port`, `baud_rate`, `poll_interval_seconds`, `stale_after_seconds`.
- Keep `gpsd` as first backend.
- Add direct NMEA serial fallback for common USB GPS dongles.
- Normalize GPS state into one dict shape:
  - `lat`, `lon`
  - `speed_kmh`
  - `heading`
  - `altitude`
  - `fix_quality` / fix mode
  - `satellites` if available
  - `timestamp`
  - `backend_used`
  - `stale` / `error`
- Add `/gps/status` API route.
- Ensure no GPS/fix failure blocks camera or ALPR.

### Phase B — Trip tracking

Relevant files:

- New `modules/trip_tracker.py`
- `modules/plate_database.py`
- `web/app.py`
- `config.yaml`

Plan:

- Add trip/session concept for each drive/run.
- Sample GPS every ~5 seconds while logger is active and fix is valid.
- Store breadcrumbs separately from plate sightings to avoid bloating incident rows.
- Add `/trip/current` route with current trip duration, latest speed, distance estimate later.

### Phase C — Database/storage changes

Prefer additive migrations that preserve existing data.

Likely changes:

- `sightings`: add `speed_kmh`, `heading`, `altitude`, `gps_timestamp`, `gps_backend`.
- `incidents`: add `lat`, `lon`, `speed_kmh`, `heading`, `altitude`, `gps_timestamp` if not already present.
- New `trips` table: `id`, `started_at`, `ended_at`, summary fields.
- New `trip_points` table: `trip_id`, `timestamp`, `lat`, `lon`, `speed_kmh`, `heading`, `altitude`, `fix_quality`.
- Metadata JSON for clips should include a nested `gps` object.

### Phase D — Wi‑Fi AP deployment

Relevant files/directories:

- `deploy/linux/`
- `docs/linux-appliance.md`
- `README.md`
- `config.yaml`

Plan:

- Add `wifi_ap:` config section:
  - `enabled`
  - `interface`
  - `ssid`
  - `country_code`
  - `static_ip`
  - `dhcp_range_start/end`
  - optional `password`
- Add install/setup script for hostapd + dnsmasq.
- Add deploy templates:
  - `hostapd.conf`
  - `dnsmasq` config
  - `car-logger-ap.service`
- Systemd ordering: AP before car logger where practical; app still works without AP for debugging.
- Add resume hook or service restart guidance because Wi‑Fi APs sometimes fail after suspend/resume.
- Document iPad URL: `http://192.168.77.1:5000`.

### Phase E — iPad dashboard UX

Relevant files:

- `web/static/dashboard/app.jsx`
- `web/static/dashboard/styles.css`
- Flask templates/static manifest files

Plan:

- Add PWA/home-screen support:
  - viewport meta for touch dashboard
  - `manifest.json`
  - Apple mobile web app tags
  - app icon placeholder if needed
- Add large GPS/speed widget.
- Add AP/local connection indicator.
- Add active trip/status bar.
- Ensure touch targets are at least 44px.
- Prefer landscape layout for 10-inch iPad.
- Avoid hover-only controls.
- Consider kiosk-ish dashboard route for iPad: `/dashboard?kiosk=1` or `/car`.

### Phase F — validation checklist

- Unit tests for GPS parsing and fallback behavior.
- Database migration tests for additive columns/tables.
- Flask route tests for `/gps/status` and `/trip/current`.
- Dashboard smoke test on iPad/Safari dimensions.
- Linux service tests:
  - boots without internet
  - AP comes up
  - iPad gets DHCP lease
  - dashboard reachable at static AP IP
  - GPS fix appears
  - capture saves clip with GPS metadata
  - suspend/resume restarts camera/AP cleanly

## Common commands

```bash
# Run web dashboard locally
python3 web/app.py --host 0.0.0.0 --port 5000

# Run tests
python3 -m pytest -q

# Live app logs on Linux appliance
journalctl -u car-incident-logger -f

# Power watcher logs
journalctl -u car-incident-power-watch -f

# Restart logger on Linux appliance
sudo systemctl restart car-incident-logger
```

## Config

All settings live in `config.yaml`. Key sections include camera, buffer/recording, dashcam, alpr, known_vehicles, gps, appliance, notifier, logging, and planned `wifi_ap`.

## Data layout

```text
data/
  dashcam/                              incident clips + metadata
  recordings/                           loop recording segments
  plates.db                             SQLite database
  models/                               ALPR/OCR model cache
  logs/                                 rotating system log
  appliance-power-state.json            Linux power watcher state
```

## Testing notes

- Tests should not require real camera, microphone, GPS dongle, Wi‑Fi hardware, or GPU.
- Hardware integrations need graceful degradation and mockable abstractions.
- Keep migrations additive and safe for existing appliance installs.
- Prefer small, verifiable phases over broad rewrites.
