# Dashcam Implementation Roadmap

> Created: 2026-05-04
> Status: Planning
> Goal: Transform car-incident-logger from "camera + incident button" into a real, user-worthy dashcam system.

---

## 1. Gap Analysis: Current State vs Real Dashcam

### What exists today
| Capability | Status | Notes |
|---|---|---|
| Rolling in-memory buffer (45s) | Done | Frames in deque, not persisted to disk |
| Manual incident trigger (keyboard/GPIO) | Done | Button press records voice note + clip |
| Voice note transcription (faster-whisper) | Done | NATO phonetic plate parsing |
| ALPR pipeline (YOLO + PaddleOCR) | Done | Multi-frame voter, known vehicle suppression |
| Live ALPR sightings dashboard | Done | Real-time plate tracking in web UI |
| Web UI (Flask) | Done | Camera preview, incidents list, config editor |
| Dashcam clip capture | Done | Pre-roll + post-roll on web trigger |
| SQLite database | Done | Plates, incidents, sightings tables |

### What's missing for a real dashcam
| Feature | Priority | Impact |
|---|---|---|
| **Continuous loop recording to disk** | Critical | THE core dashcam feature — record always, segment into files |
| **Storage management / auto-cleanup** | Critical | Config has `max_incident_age_days` but zero implementation |
| **Clip locking / protection** | High | Prevent auto-delete of important clips |
| **Clip browser/gallery with playback** | High | Browse all recordings, not just incidents |
| **Timestamp/text overlay on frames** | High | Date/time burned into video for evidence value |
| **Graceful power-loss handling** | High | Cars lose power suddenly; clips must not corrupt |
| **Health monitoring & warnings** | High | Camera disconnect, disk full, temperature, errors |
| **G-sensor / accelerometer events** | Medium | Auto-trigger on impact/hard brake (hardware dependent) |
| **GPS integration** | Medium | Speed, coordinates in metadata or overlay |
| **Parking mode** | Medium | Motion-triggered recording while parked |
| **Audio recording toggle** | Medium | Privacy control, one-tap mute |
| **Incident lifecycle (tag/note/export)** | Medium | Edit incidents, add tags, export for insurance |
| **System service / auto-start** | Medium | Start recording on boot, restart on crash |
| **Multi-camera (front + rear)** | Low | Significant architecture change |
| **Night mode / image enhancement** | Low | Camera-level settings, HDR |
| **Windows-friendly operation** | Low | PaddleOCR issues, path handling, service model |

---

## 2. Prioritized Roadmap

### MVP — "It's a real dashcam" (3-4 sessions)

The minimum to call this a dashcam rather than an incident button.

#### M1: Continuous Loop Recording
**The single most important missing feature.**

Currently the rolling buffer keeps ~45s of frames in memory and only writes to disk on trigger. A real dashcam writes *continuously* to disk in segments.

**Design:**
- New module: `modules/loop_recorder.py`
- Consumes frames from `RollingBuffer` (or directly from `CameraCapture`)
- Writes video segments of configurable length (default: 3 minutes)
- Uses ffmpeg subprocess for H.264 MP4 encoding (matches existing pattern in `incident_saver.py`)
- Output: `data/recordings/YYYY-MM-DD/HH-MM-SS.mp4`
- Segment metadata sidecar: `.json` with start/end timestamps, duration, locked flag
- When a segment finishes, start the next one seamlessly
- The in-memory rolling buffer remains for quick incident pre-roll; loop recorder is the persistent layer

**Config additions (`config.yaml`):**
```yaml
recording:
  enabled: true
  segment_duration_seconds: 180   # 3-minute segments
  output_path: ./data/recordings
  codec: h264          # h264 or copy (if camera outputs H.264 natively)
  quality: 23          # CRF value (lower = better, 18-28 reasonable)
```

**Files affected:**
- New: `modules/loop_recorder.py`
- Modified: `web/app.py` (recording status, start/stop controls)
- Modified: `config.yaml` (new `recording` section)
- Modified: `main.py` (wire up loop recorder)
- New test: `tests/test_loop_recorder.py`

**Key decisions for Owen:**
- [ ] Should loop recording run independently from the web UI, or only when web app is running?
- [ ] Segment duration preference? (1/2/3/5 min — shorter = less data lost on corruption, more files)
- [ ] Should segments be written via ffmpeg subprocess or OpenCV VideoWriter? (ffmpeg = better compression, cv2 = simpler)

#### M2: Storage Management
**Without this, continuous recording fills the disk and crashes.**

**Design:**
- New module: `modules/storage_manager.py`
- Periodic scan (every 5 min) of recording and incident directories
- Two deletion strategies:
  1. **Age-based**: delete recordings older than `max_recording_age_days` (default: 7)
  2. **Space-based**: delete oldest unlocked recordings when free space < `min_free_space_gb` (default: 2)
- Never delete locked/protected clips
- Never delete incidents (separate retention policy, already in config as `max_incident_age_days`)
- Log all deletions

**Config additions:**
```yaml
storage:
  base_path: ./data
  max_incident_age_days: 90
  max_recording_age_days: 7        # NEW
  min_free_space_gb: 2             # NEW — delete oldest when below this
  cleanup_interval_seconds: 300    # NEW
```

**Files affected:**
- New: `modules/storage_manager.py`
- Modified: `config.yaml`
- Modified: `web/app.py` (storage status endpoint)
- Modified: `main.py` or `web/app.py` (start cleanup timer)
- New test: `tests/test_storage_manager.py`

#### M3: Timestamp Overlay
**Burned-in timestamp is expected on every dashcam for evidence.**

**Design:**
- Add `cv2.putText()` to frames before they enter the rolling buffer (or at write time)
- Format: `2026-05-04 14:30:22` in bottom-left corner
- Configurable: enable/disable, position, font size, color
- Option: overlay at capture time (every frame, CPU cost) vs. at encode time (only when writing segments)
- Recommendation: overlay at encode time in `loop_recorder.py` to avoid tainting the ALPR pipeline with text artifacts

**Config additions:**
```yaml
overlay:
  enabled: true
  timestamp: true
  position: bottom-left     # top-left, top-right, bottom-left, bottom-right
  font_scale: 0.7
  color: [255, 255, 255]    # white
  background: true          # dark background behind text for readability
  # gps_speed: false        # Phase 2
```

**Files affected:**
- New: `modules/overlay.py` (small utility, applied per-frame at write time)
- Modified: `modules/loop_recorder.py` (apply overlay before encoding)
- Modified: `modules/incident_saver.py` (apply overlay to incident clips too)

#### M4: Health Monitoring
**User needs to know when something is wrong.**

**Design:**
- New module: `modules/health_monitor.py`
- Checks (periodic, every 30s):
  - Camera: is capture thread alive? Getting frames? FPS within tolerance?
  - Storage: disk free space, percentage used
  - Recording: is loop recorder writing? Last segment age?
  - Temperature: CPU temp on Raspberry Pi (`/sys/class/thermal/thermal_zone0/temp`)
  - Errors: count of recent errors from log
- Exposes status dict consumed by web UI
- Web UI: health badge on dashboard (green/yellow/red), expandable details
- Notifications: log warnings, optional chime on critical (disk full, camera lost)

**Files affected:**
- New: `modules/health_monitor.py`
- Modified: `web/app.py` (health status endpoint + dashboard section)
- Modified: `web/templates/index.html` (health display)

---

### v1.0 — "Reliable daily driver" (3-4 sessions)

#### V1: Clip Browser & Playback
**Browse all recorded segments, not just incidents.**

- New web route: `/recordings` — paginated list of all segments
- Calendar/date picker navigation
- In-browser video playback (HTML5 `<video>` tag, MP4 serves directly)
- Lock/unlock toggle per clip
- Delete individual clips
- Filter: date range, locked only, has-incident
- Link from incident to the corresponding recording segment

**Files affected:**
- New template: `web/templates/recordings.html`
- Modified: `web/app.py` (new routes: `/recordings`, `/recordings/<id>/lock`, `/recordings/<id>/delete`)
- Modified: `web/templates/base.html` (nav link)
- Modified: `web/static/style.css`
- Modified: `modules/plate_database.py` or new `modules/recording_database.py` (recording index)

**Database additions:**
```sql
CREATE TABLE recordings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  filename TEXT NOT NULL,
  start_time TEXT NOT NULL,
  end_time TEXT NOT NULL,
  duration_seconds REAL NOT NULL,
  file_size_bytes INTEGER,
  file_path TEXT NOT NULL,
  locked INTEGER NOT NULL DEFAULT 0,
  has_incident INTEGER NOT NULL DEFAULT 0,
  deleted INTEGER NOT NULL DEFAULT 0
);
```

#### V2: Graceful Power-Loss Handling
**Cars lose power without warning when ignition turns off.**

- Write segments using a crash-safe pattern:
  1. Write to temp file (`segment_INPROGRESS.mp4`)
  2. On segment complete, rename to final name (atomic on most filesystems)
  3. On startup, scan for `_INPROGRESS` files — either finalize or discard
- Use moov atom at front of MP4 (ffmpeg `-movflags +faststart`) so partial files are still playable
- Periodic fsync of segment metadata
- Startup recovery: check last segment, attempt to recover partial data
- Consider: write in MKV/fragmented MP4 (fMP4) which is inherently crash-resilient, then remux to MP4 on completion

**Files affected:**
- Modified: `modules/loop_recorder.py` (crash-safe write pattern)
- Modified: startup in `main.py` / `web/app.py` (recovery scan)

#### V3: Incident Lifecycle Improvements
**Edit, tag, annotate, export incidents.**

- Edit incident notes/tags from web UI
- Tag taxonomy: `impact`, `near-miss`, `road-rage`, `parking`, `theft`, `other`
- Export incident package: ZIP containing clip + audio + metadata + transcript
- Delete incidents from web UI (with confirmation)
- Lock incidents (prevent auto-cleanup)

**Files affected:**
- Modified: `web/app.py` (edit/delete/export/lock endpoints)
- Modified: `web/templates/plate_detail.html` (edit UI)
- Modified: `web/templates/incidents.html` (bulk actions)
- Modified: `modules/plate_database.py` (update/lock/delete methods)

#### V4: Audio Toggle
**Privacy control — one button to mute/unmute dashcam audio.**

- Toggle in web UI header (persistent across sessions)
- When muted: loop recorder writes video-only segments
- When unmuted: record audio track alongside video
- Indicator on dashboard: mic icon with slash when muted
- Config default: `audio.dashcam_enabled: true`

**Files affected:**
- Modified: `modules/loop_recorder.py` (conditional audio mux)
- Modified: `web/app.py` (toggle endpoint)
- Modified: `web/templates/base.html` (toggle button)

---

### v1.5 — "Advanced features" (future)

#### GPS Integration
- USB GPS dongle (gpsd) or phone GPS via web API
- Store lat/lon/speed per frame or per second
- Optional overlay on video
- GPX track export
- Map view in web UI (Leaflet.js with offline tiles)

#### Parking Mode
- Detect "parked" state (no motion for N minutes, or manual toggle)
- Switch to motion-triggered recording (save clips only when motion detected)
- Lower power consumption (reduce FPS, stop ALPR)
- Configurable sensitivity
- Resume normal mode on sustained motion

#### G-Sensor / Accelerometer
- USB accelerometer or phone sensor via web API
- Detect: hard brake, impact, sudden swerve
- Auto-trigger incident + lock current and adjacent segments
- Configurable threshold (g-force)
- Hardware: ADXL345 via I2C on Raspberry Pi, or phone accelerometer via JavaScript

#### Multi-Camera Support
- Front + rear camera capture
- Synchronized segments
- Split-screen or picture-in-picture playback
- Architecture: multiple `CameraCapture` + `RollingBuffer` instances
- Significant refactor of recording pipeline

#### System Service
- systemd unit file for Linux / launchd plist for macOS
- Auto-start on boot
- Watchdog: restart on crash
- Journal logging integration

#### Evidence Export Package
- Generate PDF report: timeline, plate, screenshots, map, transcript
- ZIP bundle with all media + metadata
- QR code linking to local web UI for the incident
- Chain-of-custody metadata (hash of original files)

#### Night Mode / Image Enhancement
- Camera exposure/gain controls via OpenCV
- Auto-detect low light, adjust settings
- IR camera support (if hardware available)

---

## 3. Concrete Implementation Plan

### Phase 1: MVP (Sessions 1-4)

#### Session 1: Loop Recorder + Config
**Goal:** Continuous recording to disk in segments.

1. Create `modules/loop_recorder.py`:
   - `LoopRecorder` class, daemon thread
   - Attach to `RollingBuffer` or `CameraCapture` frame source
   - Write segments via ffmpeg subprocess (reuse pattern from `incident_saver.py`)
   - Segment rotation: finish current, start next, no gap
   - Filename: `data/recordings/YYYY-MM-DD/HH-MM-SS.mp4`
   - Sidecar JSON metadata per segment
2. Add `recording` section to `config.yaml`
3. Wire into `web/app.py`:
   - Start/stop recording endpoint
   - Auto-start if `recording.enabled: true`
   - Status endpoint returning: recording state, current segment, segment count today
4. Dashboard: recording indicator (red dot + "REC") on index.html
5. Write `tests/test_loop_recorder.py` (mock frame source, verify segment files created)

#### Session 2: Storage Manager + Health Monitor
**Goal:** Don't fill the disk; surface problems.

1. Create `modules/storage_manager.py`:
   - Periodic cleanup thread
   - Age-based + space-based deletion of unlocked recordings
   - Separate policy for incidents
   - Deletion logging
2. Create `modules/health_monitor.py`:
   - Camera health, disk space, recording status, CPU temp
   - Status dict with green/yellow/red per subsystem
3. Add storage + health endpoints to `web/app.py`
4. Dashboard: storage bar (used/free/total), health badges
5. Tests for both modules

#### Session 3: Timestamp Overlay + Clip Locking
**Goal:** Evidence-grade video; protect important clips.

1. Create `modules/overlay.py`:
   - `apply_overlay(frame, timestamp, config)` — pure function
   - Timestamp text with dark background for readability
   - Configurable position, scale, color
2. Integrate into `loop_recorder.py` (apply before encoding each frame)
3. Integrate into `incident_saver.py` (apply to incident clips too)
4. Add `locked` field to recording metadata / DB
5. Lock/unlock endpoint in web app
6. Locked clips excluded from auto-cleanup
7. Tests for overlay (verify text placement, config options)

#### Session 4: Recording Browser + Playback
**Goal:** Browse and watch recorded clips in the web UI.

1. Add `recordings` table to database (or extend plate_database.py)
2. New route `/recordings` with date navigation
3. New template `recordings.html`: segment list, date picker, playback
4. HTML5 video player for MP4 segments
5. Lock/unlock/delete controls per segment
6. Link incidents to their corresponding recording segment
7. Update nav in `base.html`

### Phase 2: v1.0 Reliability (Sessions 5-7)

#### Session 5: Power-Loss Resilience
- Crash-safe write pattern (temp file + atomic rename)
- fMP4/MKV intermediate format
- Startup recovery scan
- Test: simulate kill during write, verify recovery

#### Session 6: Incident Lifecycle
- Edit notes/tags from web UI
- Export ZIP package
- Delete with confirmation
- Lock incidents

#### Session 7: Audio Toggle + Polish
- Mic mute/unmute toggle
- Audio track in loop recordings (when enabled)
- UI polish pass: mobile-responsive, loading states, error messages
- End-to-end integration test

### Phase 3: v1.5 Advanced (Sessions 8+)
- GPS, parking mode, G-sensor, multi-camera, system service, evidence export
- Each is roughly one session

---

## 4. Data Model & Storage Changes

### New: recordings table
```sql
CREATE TABLE recordings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  filename TEXT NOT NULL UNIQUE,
  start_time TEXT NOT NULL,         -- ISO 8601
  end_time TEXT,                    -- NULL if in-progress
  duration_seconds REAL,
  file_size_bytes INTEGER,
  file_path TEXT NOT NULL,
  locked INTEGER NOT NULL DEFAULT 0,
  has_incident INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_recordings_start ON recordings(start_time);
CREATE INDEX idx_recordings_locked ON recordings(locked);
```

### New: on-disk layout
```
data/
  recordings/
    2026-05-04/
      14-30-00.mp4          # 3-minute segment
      14-30-00.json         # metadata sidecar
      14-33-00.mp4
      14-33-00.json
      ...
  plates/  ...              # unchanged
  unresolved/  ...          # unchanged
  dashcam/  ...             # may merge into recordings with incident flag
```

### Schema migration
- Add `recordings` table (new, no migration needed for existing data)
- Consider: add `recording_id` FK to `incidents` table to link incidents to their recording segment
- Consider: merge `dashcam/` captures into the recordings system (dashcam trigger = mark segment as incident)

---

## 5. Recommended First Implementation Batch

**Session 1 scope (one coding session):**

1. `modules/loop_recorder.py` — the core loop recording module
2. `recording` section in `config.yaml`
3. Wire into `web/app.py` with start/stop/status endpoints
4. "REC" indicator on dashboard
5. Basic test

This is the highest-impact single feature. Everything else (storage management, browser, locking) builds on top of it. Without continuous recording, the other features have nothing to operate on.

**Estimated new/modified files:**
| File | Action |
|---|---|
| `modules/loop_recorder.py` | Create (~200 lines) |
| `modules/overlay.py` | Create (~50 lines, simple timestamp utility) |
| `config.yaml` | Add `recording` + `overlay` sections |
| `web/app.py` | Add 3-4 endpoints (~60 lines) |
| `web/templates/index.html` | Add REC indicator + recording controls (~20 lines) |
| `tests/test_loop_recorder.py` | Create (~100 lines) |

---

## 6. Design Decisions for Owen

Before implementing, these choices will shape the architecture:

### Must decide before Session 1:
1. **Segment format**: MP4 (via ffmpeg) vs. MKV (crash-resilient but needs remux)? Recommend: write fMP4, remux to standard MP4 on segment completion.
2. **Segment duration**: 1 / 2 / 3 / 5 minutes? Shorter = less data lost on crash, more files to manage. Recommend: 3 minutes.
3. **Frame source for recording**: Pull from `RollingBuffer` (shares frames with incident system) or tap `CameraCapture` directly (independent pipeline)? Recommend: tap CameraCapture directly — loop recorder is a second consumer of the camera queue.
4. **Audio in loop recordings**: Record audio continuously in segments, or video-only? Recommend: video-only by default (privacy), with toggle.
5. **Overlay timing**: Burn timestamp into every frame at capture time, or at encode time? Recommend: encode time only, so ALPR sees clean frames.

### Should decide before Session 4:
6. **Dashcam capture merge**: Should the existing `dashcam/` trigger system merge into loop recording (trigger = lock current segment + mark as incident), or remain separate? Recommend: merge — triggering an incident locks the current and previous segment.
7. **Recording database**: Extend `plate_database.py` with a `recordings` table, or create `recording_database.py`? Recommend: extend existing (one DB file, simpler joins).

### Can decide later:
8. **GPS hardware**: USB GPS dongle (gpsd) vs. phone GPS via browser API vs. skip for now?
9. **Accelerometer**: USB sensor vs. phone sensor vs. skip?
10. **Multi-camera**: Worth planning the abstraction now, or wait until needed?

---

## 7. Risks & Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| **Disk I/O bottleneck** (1080p30 continuous write) | Dropped frames, gaps | Use hardware encoding if available; reduce quality/resolution; benchmark on target hardware |
| **SD card wear** (Raspberry Pi) | Card failure | Use high-endurance SD card; minimize write amplification; consider USB SSD |
| **ffmpeg not installed** | Recording fails | Detect at startup; fall back to cv2.VideoWriter; warn in health monitor |
| **Power loss during segment write** | Corrupted MP4 | Use fMP4/MKV intermediate; atomic rename on completion; startup recovery |
| **Storage fills up despite cleanup** | System crash | Aggressive cleanup trigger; emergency mode (stop recording, alert user) |
| **ALPR false positives on overlay text** | Wrong plate reads | Apply overlay at encode time, not to frames sent to ALPR |
| **Threading complexity** | Race conditions, deadlocks | Loop recorder is independent thread with its own frame source; minimal shared state |
| **Large codebase growth** | Maintenance burden | Keep modules focused; extend existing patterns; good test coverage |
