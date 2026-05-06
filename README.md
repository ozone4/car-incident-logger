# Car Incident Logger

A fully local, in-car incident logging system. Press and hold a physical button (or keyboard key) to record a voice note describing a license plate using NATO phonetic alphabet. On release, the system transcribes your audio locally, parses the plate from spoken phonetics, and saves a timestamped video clip, audio recording, and metadata — all indexed in a local SQLite database.

No internet connection required. No cloud. Runs on a Raspberry Pi or any Windows/Linux/macOS system with a USB camera and microphone.

---

## Hardware Requirements

| Component | Details |
|-----------|---------|
| Camera | Arducam IMX662 (USB UVC, 1080p, MJPG/H.264/YUV) or any V4L2-compatible USB camera |
| Microphone | Any USB or built-in microphone recognized by ALSA/PortAudio |
| Button (optional) | Momentary push button wired to a Raspberry Pi GPIO pin |
| Computer | Raspberry Pi 4/5 (2 GB+ RAM) or any Windows/Linux/macOS machine |
| Storage | 16 GB+ SD card or USB drive for video clip storage |

---

## Software Setup

### 1. Clone and enter the repo

```bash
git clone https://github.com/ozone4/car-incident-logger.git
cd car-incident-logger
```

### 2. Create a virtual environment (recommended)

macOS/Linux:
```bash
python3 -m venv .venv
source .venv/bin/activate
```

Windows PowerShell:
```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
```

If PowerShell blocks activation, run this once in that shell:
```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

### 3. Run the installer

```bash
bash scripts/install_deps.sh
```

This will:
- Install all Python dependencies from `requirements.txt`
- Download the `base.en` Whisper model (~150 MB, one time)
- Initialize the SQLite database at `data/plates.db`
- Install `RPi.GPIO` if running on a Raspberry Pi

### 4. Manual install (alternative / Windows-friendly)

macOS/Linux:
```bash
pip install -r requirements.txt
python3 scripts/setup_db.py
```

Windows PowerShell:
```powershell
pip install -r requirements.txt
py scripts/setup_db.py
```

### 5. Linux appliance mode — ThinkPad / always-on dashcam

To install the app as a Linux boot-and-record appliance with systemd services, AC-loss grace period, clean suspend, and resume restart hooks, see:

```text
docs/linux-appliance.md
```

Quick start on the Linux laptop:

```bash
bash scripts/install_linux_appliance.sh
```

### 6. Test the Arducam on Windows

Plug in the Arducam first, then run:

```powershell
py scripts/test_camera.py
```

If it opens the wrong camera, edit `config.yaml` and try `camera.device_index: 1`, then `2`, etc.

---

## Configuration

Edit `config.yaml` before running. Key settings:

### Camera

```yaml
camera:
  device_index: 0        # Linux: /dev/video0, macOS: typically 0
  resolution:
    width: 1920
    height: 1080
  fps: 30
  format: MJPG           # MJPG gives best throughput on the Arducam IMX662
```

To find your camera device index on Linux:
```bash
ls /dev/video*
v4l2-ctl --list-devices
```

On Windows, USB cameras are usually numeric indexes. Start with `0`; if that is the laptop/internal webcam or fails, try `1`, `2`, etc. in `config.yaml`.

### Button Modes

**Keyboard mode** (default — works everywhere):
```yaml
button:
  mode: keyboard
  key: space             # space, enter, f1, f2, f3, f4, or any character
```

**GPIO mode** (Raspberry Pi physical button):
```yaml
button:
  mode: gpio
  gpio_pin: 17           # BCM numbering
  gpio_pull: up          # Connect button between GPIO pin and GND
```

Wire the button between the GPIO pin and GND. The internal pull-up resistor is enabled — no external resistor needed.

### Audio

```yaml
audio:
  device_index: null     # null = system default microphone
  sample_rate: 16000     # Whisper works best at 16 kHz
  channels: 1
```

To list available audio devices:
```python
import sounddevice as sd
print(sd.query_devices())
```

### Transcription Model

```yaml
transcription:
  model: base.en         # Options: tiny.en, base.en, small.en, medium.en
  device: cpu            # cpu or cuda (GPU)
  compute_type: int8     # int8 (fastest on CPU), float16 (GPU), float32
```

Larger models are more accurate but slower. `base.en` is a good balance for in-car use on a Pi 4.

### Storage

```yaml
storage:
  base_path: ./data
  max_incident_age_days: 90
```

Incidents older than `max_incident_age_days` are not automatically deleted (manual cleanup or a cron job is needed).

---

## Usage

### Start the logger

```bash
python3 main.py
```

You will see:
```
[INFO] Ready. Button mode: keyboard (key='space') | Buffer: 45s | Press button to record an incident. Ctrl+C to quit.
[INFO] Transcription model loaded and ready
```

### Recording an incident

1. **Press and hold** the button (or Space bar in keyboard mode) when you observe the incident.
2. **Speak the plate phonetically**, then describe what happened:
   ```
   Whiskey Juliet One Eight Four Three — tailgated me and ran the red light at Main
   ```
3. **Release** the button. The system will:
   - Stop audio recording
   - Transcribe the audio (typically 1–3 seconds on Pi 4)
   - Parse the plate (`WJ1843`) from the phonetics
   - Save the video clip (last 45 seconds), audio, transcript, and metadata
   - Add the incident to the database
   - Print a confirmation

### Spoken plate format

Use the NATO phonetic alphabet for letters and word-form numbers for digits:

| Letter | NATO word | Digit | Spoken word |
|--------|-----------|-------|-------------|
| A | Alpha | 0 | Zero / Oh |
| B | Bravo | 1 | One |
| C | Charlie | 2 | Two |
| D | Delta | 3 | Three |
| E | Echo | 4 | Four |
| F | Foxtrot | 5 | Five |
| G | Golf | 6 | Six |
| H | Hotel | 7 | Seven |
| I | India | 8 | Eight |
| J | Juliet | 9 | Nine / Niner |
| K | Kilo | | |
| L | Lima | | |
| M | Mike | | |
| N | November | | |
| O | Oscar | | |
| P | Papa | | |
| Q | Quebec | | |
| R | Romeo | | |
| S | Sierra | | |
| T | Tango | | |
| U | Uniform | | |
| V | Victor | | |
| W | Whiskey | | |
| X | X-Ray | | |
| Y | Yankee | | |
| Z | Zulu | | |

Example: plate `WJ1843` → *"Whiskey Juliet One Eight Four Three"*

Everything after the plate tokens is recorded as the freeform note.

### Incident storage layout

```
data/
  plates/
    WJ1843/
      incidents/
        20240315T143022Z/
          clip.mp4          <- last 45 seconds of video
          audio.wav         <- raw voice recording
          transcript.txt    <- Whisper output
          metadata.json     <- plate, note, paths, confidence, tags
  unresolved/
    20240315T150011Z/       <- incidents where no plate could be parsed
  plates.db                 <- SQLite index of all plates and incidents
```

### Querying the database

```python
from modules.plate_database import PlateDatabase
db = PlateDatabase("data/plates.db")

# Check if a plate is known
db.is_known_plate("WJ1843")   # True/False

# Get all incidents for a plate
db.get_incidents_for_plate("WJ1843")

# Search partial plate
db.search_plates("WJ")

# All known plates
db.all_plates()
```

---

## Running Tests

```bash
pip install pytest
pytest tests/ -v
```

Tests cover:
- Full NATO phonetic alphabet parsing
- Edge cases (niner, oh, bare digits, mixed case, x-ray)
- Note separation from plate tokens
- Database add/retrieve/search/delete operations
- ALPR normalization, OCR corrections, plate validation
- Multi-frame voting logic
- ALPRRunner behavior when deps are missing (no ultralytics/PaddleOCR needed)

---

## Project Structure

```
car-incident-logger/
├── main.py                      # Orchestrator — start here
├── config.yaml                  # All configuration
├── requirements.txt
├── modules/
│   ├── config_manager.py        # Typed config access
│   ├── camera_capture.py        # OpenCV USB camera, background thread
│   ├── rolling_buffer.py        # Thread-safe circular frame buffer
│   ├── button_listener.py       # Keyboard or GPIO button detection
│   ├── audio_recorder.py        # sounddevice mic capture
│   ├── transcription_engine.py  # faster-whisper local STT
│   ├── phonetic_plate_parser.py # NATO phonetic -> plate string
│   ├── incident_saver.py        # Saves clip + audio + metadata to disk
│   ├── plate_database.py        # SQLite plates/incidents/sightings
│   ├── notifier.py              # Console + chime alerts
│   ├── dashcam.py               # Dashcam incident capture from rolling buffer
│   ├── loop_recorder.py         # Continuous loop recording to disk in segments
│   ├── overlay.py               # Timestamp overlay for recorded frames
│   ├── incident_trigger.py      # Abstract trigger interface (web, hardware, ALPR)
│   ├── alpr_runner.py           # Phase 2: YOLO+PaddleOCR pipeline (deps optional)
│   ├── multi_frame_voter.py     # Phase 2: aggregate candidates across frames
│   └── live_matcher.py          # Phase 2 stub: background known-plate alerting
├── data/
│   ├── plates/                  # Per-plate incident folders
│   ├── recordings/              # Continuous loop recording segments (by date)
│   ├── unresolved/              # Incidents with no parsed plate
│   ├── models/                  # Whisper model cache
│   ├── logs/                    # Rotating system log
│   └── plates.db                # SQLite database (after first run)
├── scripts/
│   ├── install_deps.sh          # One-shot setup script
│   ├── setup_db.py              # DB schema init
│   └── test_alpr.py             # ALPR pipeline test (--image / --camera / --status)
├── requirements.txt             # Core deps (no heavy ALPR libs)
├── requirements-alpr.txt        # Optional ALPR deps (ultralytics, paddleocr)
└── tests/
    ├── test_phonetic_parser.py
    ├── test_plate_database.py
    ├── test_web_ui.py
    ├── test_loop_recorder.py    # Loop recorder, overlay, and recording config tests
    └── test_alpr.py             # ALPR utils + voter tests (no heavy deps needed)
```

---

## Web UI

A lightweight Flask dashboard lets you preview the camera and browse incidents without running the full logger loop.

### Start the web UI (Windows / macOS / Linux)

```powershell
# From the project root (Windows PowerShell or Command Prompt):
pip install -r requirements.txt
python web/app.py
```

```bash
# macOS / Linux:
pip install -r requirements.txt
python3 web/app.py
```

Then open **http://127.0.0.1:5000/** in a browser.

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--host` | `127.0.0.1` | Bind address. Use `0.0.0.0` to expose on your LAN |
| `--port` | `5000` | TCP port |
| `--debug` | off | Enable Flask debug/reloader |

```powershell
# Example: expose on LAN at port 8080
python web/app.py --host 0.0.0.0 --port 8080
```

### Pages

| URL | Description |
|-----|-------------|
| `/` | Dashboard — touch-friendly kiosk UI with REC status, incident trigger, health, sightings, recent incidents |
| `/dashboard` | Alias for `/` |
| `/camera` | Live MJPEG camera preview with start/stop controls |
| `/recordings` | Browse, play, lock/unlock, and delete continuous recording segments |
| `/incidents` | All incidents; search by partial plate (e.g. `WJ`) |
| `/incidents/<PLATE>` | Per-plate incident history |
| `/config` | View and edit camera device, resolution, FPS, buffer duration |

The camera preview on `/camera` is independent of the main logger — you can start/stop it to test your camera without running `main.py`.

### Recordings browser

The `/recordings` page lets you browse continuous recording segments written by the loop recorder.

- **View**: Segments are listed newest-first with date, duration, frame count, file size, and lock status. Use the date dropdown to filter by day.
- **Play**: Click **Play** to open an inline HTML5 video player for any segment.
- **Lock**: Click **Lock** to protect a segment from automatic storage cleanup. Locked segments show a yellow "Locked" badge.
- **Unlock**: Click **Unlock** to remove protection. The segment becomes eligible for cleanup again.
- **Delete**: Click **Delete** to permanently remove an unlocked segment and its sidecar. Locked segments must be unlocked first.

The dashboard's Continuous Recording card also links directly to the recordings browser.

### Live ALPR from the dashboard

Once optional ALPR dependencies and `data/models/plate_detector.pt` are installed, the dashboard can scan automatically:

1. Start the web UI: `python web/app.py`
2. Open `http://127.0.0.1:5000/`
3. Click **Start Live ALPR**
4. The dashboard reuses the preview camera, scans every `alpr.scan_interval_seconds`, and shows the latest/best voted plate.

### Windows notes

- Core camera/UI features are tested with Python 3.10+ on Windows 10/11.
- **ALPR note:** PaddlePaddle does not currently publish Windows wheels for Python 3.14. For ALPR, use Python **3.11 or 3.12**.
- No external services required — everything runs locally.
- Use `python` on your Windows PC; the `py` launcher may not be installed.
- Camera device index: plug in your USB camera, start the web UI, go to `/camera`, and click **Start Preview**. If the wrong camera opens, edit `camera.device_index` on the `/config` page.

### Kiosk / Touchscreen mode

The dashboard is designed for in-car use on a 7–10" touchscreen (e.g. Raspberry Pi + official display). To run in kiosk mode:

```bash
# Start the web server on the local network
python3 web/app.py --host 0.0.0.0 --port 5000

# Open Chromium in kiosk/fullscreen mode (Raspberry Pi example):
chromium-browser --kiosk --noerrdialogs --disable-infobars http://localhost:5000/
```

**Tips:**
- The dashboard hides the sidebar on screens under 900px wide, maximizing usable space.
- All core actions (incident trigger, ALPR start/stop) use large tap targets — no hover-only interactions.
- The REC indicator and health light provide at-a-glance status while driving.
- On macOS/Windows, press **F11** in your browser for fullscreen, or use `--kiosk` flag with Chrome/Edge.
- The `/dashboard` URL is an alias for `/` if you prefer a descriptive bookmark.

---

## Dashcam Incident Capture

The web dashboard includes a dashcam mode that continuously buffers live camera frames and lets you save incident clips with one click.

### How it works

1. Start the web UI and click **Start Preview** on the Camera page (or the camera starts automatically on the Dashboard).
2. A rolling buffer continuously stores the last N seconds of video frames in memory.
3. Click **Trigger Incident** on the Dashboard to save a clip with pre-roll (video before the trigger) and optional post-roll (a few seconds after).
4. The saved clip, metadata JSON, and any live ALPR plate detections are written to `data/dashcam/<timestamp>/`.
5. The incident appears in the Recent Incidents table and the Incidents page.

### Configuration

```yaml
dashcam:
  pre_roll_seconds: 30       # seconds of video before the trigger
  post_roll_seconds: 5       # seconds of video after the trigger
  output_path: ./data/dashcam
```

These values can be adjusted in `config.yaml`. The rolling buffer size automatically accommodates the configured pre-roll.

### Trigger sources

| Source | Status | Description |
|--------|--------|-------------|
| Web UI button | Working | Click "Trigger Incident" on the Dashboard |
| Hardware button (GPIO) | Placeholder | `HardwareButtonTrigger` in `modules/incident_trigger.py` — wire up to GPIO or USB HID |
| ALPR alert | Planned | Auto-trigger when a known plate is detected |

### Windows usage

By default, the web app now behaves like a dashcam: `python web\app.py` automatically starts the camera, arms the rolling buffer, and starts Live ALPR when available. The dashboard Start/Stop buttons remain as recovery/testing controls. To disable this, set:

```yaml
dashcam:
  auto_start_camera: false
  auto_start_alpr: false
```


```powershell
# Start the web UI
python web/app.py

# Or bind to LAN
python web/app.py --host 0.0.0.0 --port 8080
```

Open `http://127.0.0.1:5000/` in a browser. Start the camera preview, then use the **Trigger Incident** button on the Dashboard. Saved clips appear in `data\dashcam\`.

ffmpeg is recommended for H.264/MP4 output. Without it, clips are saved as AVI (XVID) via OpenCV.

```powershell
# Install ffmpeg on Windows (winget)
winget install ffmpeg

# Or download from https://ffmpeg.org/download.html and add to PATH
```

---

## Continuous Loop Recording

The system can continuously record video to disk in rotating segments, like a real dashcam. Recordings are organized by date and include sidecar JSON metadata.

### How it works

When the camera starts (either manually or via auto-start), the loop recorder automatically begins writing video segments to disk. Each segment is a fixed duration (default: 60 seconds). When a segment completes, a new one starts seamlessly. A timestamp overlay is burned into recorded frames at encode time — ALPR sees clean, unmodified frames.

Segments are written to a temp file first, then renamed on completion for crash safety.

### Generated files

```
data/recordings/
  2026-05-04/
    14-30-00.mp4          # 60-second video segment
    14-30-00.json         # sidecar metadata (start/end time, frame count, locked flag)
    14-31-00.mp4
    14-31-00.json
    ...
```

### Configuration

```yaml
recording:
  enabled: true                    # enable/disable continuous recording
  segment_duration_seconds: 60     # length of each segment
  output_path: ./data/recordings   # where segments are saved

overlay:
  enabled: true                    # burn timestamp into recorded frames
  position: bottom-left            # top-left, top-right, bottom-left, bottom-right
  font_scale: 0.7
  color: [255, 255, 255]           # white BGR
  background: true                 # dark rectangle behind text for readability
```

### Dashboard status

The web dashboard shows a **Continuous Recording** card with a red REC indicator when active. It displays the current segment name, frame count, and number of completed segments.

The recording status is also available via the `/recording/status` JSON endpoint.

### Windows usage

```powershell
# Start the web UI — recording starts automatically with the camera
python web/app.py
```

Recordings are saved to `data\recordings\` by default. To change the path, edit `recording.output_path` in `config.yaml`. Use forward slashes or `./` relative paths for cross-platform compatibility.

To disable continuous recording while keeping the camera active:

```yaml
recording:
  enabled: false
```

---

## Phase 2 — ALPR (Automatic License Plate Recognition)

Phase 2 adds **automatic** plate recognition on live video — no button required for detection. Button logging continues to work alongside it.

### Architecture

```
frame → YOLO plate detector → crop + preprocess → PaddleOCR → normalize/correct → validate
                                                                         ↓
                                              MultiFrameVoter aggregates across frames
                                                                         ↓
                                                    best plate + confidence returned
```

OCR correction rules handle common confusions for BC/North-American plates:
`O↔0  I↔1  S↔5  B↔8  Z↔2  G↔6` applied based on letter vs digit position in the plate.

### Install ALPR dependencies

```bash
# All ALPR deps:
pip install -r requirements-alpr.txt

# Or individually:
pip install ultralytics              # YOLO detector
pip install paddlepaddle paddleocr   # OCR (CPU)
pip install easyocr                  # optional cropped-plate OCR fallback
```

**Windows note:** If `pip install paddlepaddle` says "No matching distribution found", check your Python version first:
```powershell
python --version
```
If it says Python 3.14, create a Python 3.11/3.12 virtual environment and install ALPR there. PaddlePaddle wheels commonly lag behind the newest Python releases.

If PaddleOCR initializes but returns no text for good YOLO plate crops, install the optional cropped-plate fallback:
```powershell
pip install easyocr
```

### Get a plate detector model

Quality ALPR requires a YOLO model fine-tuned on license plates. A general YOLOv8n (vehicle/object detector) will not find plate bounding boxes reliably.

Options:
- **Roboflow Universe** — search "license plate detection", download YOLOv8 `.pt` weights (free for research use)
- **keremberke/license-plate-object-detection** — available on Hugging Face Hub
- Train your own on local footage with [Roboflow](https://roboflow.com/) or CVAT

Place the `.pt` file anywhere and set the path in `config.yaml`:
```yaml
alpr:
  yolo_model_path: ./data/models/plate_detector.pt
```

### Enable ALPR

```yaml
alpr:
  enabled: true
  confidence_threshold: 0.5        # final combined plate confidence
  yolo_confidence_threshold: 0.1   # lower = more OCR attempts, more noise
  scan_interval_seconds: 2
  yolo_model_path: ./data/models/plate_detector.pt
  models_dir: ./data/models
```

### Test the ALPR pipeline (without running the full logger)

```bash
# Check which engines are installed:
python scripts/test_alpr.py --status

# Run on a single image:
python scripts/test_alpr.py --image path/to/plate.jpg

# Run on live camera, aggregate 60 frames:
python scripts/test_alpr.py --camera --frames 60 --conf 0.4
```

Windows PowerShell:
```powershell
python scripts/test_alpr.py --status
python scripts/test_alpr.py --image plate.jpg
python scripts/test_alpr.py --camera --frames 30
```

### Limitations

- **Model quality is the bottleneck.** A fine-tuned plate detector is essential; without one ALPR will miss most plates in real traffic.
- PaddleOCR can struggle with motion blur and low-light plates — good camera placement helps more than model tweaking.
- Whole-frame OCR fallback (no YOLO) produces many false positives and is not suitable for moving vehicles.
- `live_matcher.py` (background alert on known plates) is still a stub pending real-world tuning.

### Planned additions

- Incident auto-tagging: when ALPR confirms a plate during a button-triggered incident, add `"alpr_confirmed": true` to metadata.
- Retention policy: auto-delete incidents older than `storage.max_incident_age_days`.
- Web UI ALPR page: browsable sightings separate from button-triggered incidents.

---

## Troubleshooting

**Camera not opening**
- Check `camera.device_index` in config.yaml (try 0, 1, 2...)
- Verify the camera is recognized: `ls /dev/video*`
- On macOS, grant camera permissions to Terminal/Python

**No audio captured**
- Run `python3 -c "import sounddevice as sd; print(sd.query_devices())"` to list devices
- Set `audio.device_index` to the correct index for your microphone

**Transcription is slow**
- Switch to `model: tiny.en` for faster (less accurate) transcription
- If you have an NVIDIA GPU, set `device: cuda` and `compute_type: float16`

**GPIO button not responding**
- Confirm `gpio_pin` matches the BCM pin number (not the physical pin number)
- Check the button is wired between the pin and GND (not 3.3V)
- Try `gpio_pull: down` with the button wired to 3.3V

**`RPi.GPIO` ImportError on non-Pi hardware**
- This is expected. Set `button.mode: keyboard` in config.yaml.

---

## Windows Auto-Start

To run the dashcam app automatically on boot (Windows 10/11), use **Task Scheduler**:

### Option 1: Task Scheduler (recommended)

1. Open **Task Scheduler** (search "Task Scheduler" in Start).
2. Click **Create Basic Task**.
3. Name it `Car Incident Logger`, click Next.
4. Trigger: **When the computer starts**, click Next.
5. Action: **Start a program**.
6. Program/script: `pythonw.exe` (or full path, e.g. `C:\Python311\pythonw.exe`)
7. Arguments: `web/app.py --host 0.0.0.0 --port 5000`
8. Start in: `C:\path\to\car-incident-logger` (your project directory)
9. Finish. Then edit the task properties:
   - Check **Run whether user is logged on or not**
   - Check **Run with highest privileges** (needed for camera/USB access)
   - Under Conditions, uncheck "Start only if on AC power" for in-car use

### Option 2: Startup batch file

Create `start_dashcam.bat` in the project root:

```batch
@echo off
cd /d "%~dp0"
python web/app.py --host 0.0.0.0 --port 5000
```

Place a shortcut to this file in `shell:startup` (press Win+R, type `shell:startup`).

### Notes

- Use `pythonw.exe` instead of `python.exe` to run without a console window.
- Ensure your Python environment has all dependencies installed (`pip install -r requirements.txt`).
- The app binds to `0.0.0.0` so you can access the dashboard from another device on the same network.
- For headless operation, the camera auto-starts on boot if `dashcam.auto_start_camera: true` in config.yaml.

---

## Licence

MIT
