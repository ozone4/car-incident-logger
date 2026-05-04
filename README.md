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

### 5. Test the Arducam on Windows

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
│   ├── alpr_runner.py           # STUB: Phase 2 live ALPR
│   └── live_matcher.py          # STUB: Phase 2 known-plate matching
├── data/
│   ├── plates/                  # Per-plate incident folders
│   ├── unresolved/              # Incidents with no parsed plate
│   ├── models/                  # Whisper model cache
│   ├── logs/                    # Rotating system log
│   └── plates.db                # SQLite database (after first run)
├── scripts/
│   ├── install_deps.sh          # One-shot setup script
│   └── setup_db.py              # DB schema init
└── tests/
    ├── test_phonetic_parser.py
    └── test_plate_database.py
```

---

## Phase 2 Roadmap

Phase 2 adds **automatic** license plate recognition on live video — no button required for detection (button logging still works in parallel).

Planned additions:

1. **`modules/alpr_runner.py`** — implement `run_on_frame()` using one of:
   - EasyOCR + YOLOv8-nano plate detector (fully offline, permissive licence)
   - OpenALPR C++ library with Python bindings (mature, GPL)
   - Plate Recognizer Local SDK (paid, highest accuracy)

2. **`modules/live_matcher.py`** — background thread samples frames every N seconds,
   calls ALPR, compares results to `plate_database`, fires `notifier.alert_known_plate()`
   on a match.

3. **Incident auto-tagging** — when ALPR matches a plate during a button-triggered
   incident, automatically tag the metadata with `"alpr_confirmed": true`.

4. **Retention policy** — scheduled cleanup of incidents older than
   `storage.max_incident_age_days`.

5. **Web UI** — lightweight Flask/FastAPI dashboard to browse incidents and plates.

Enable Phase 2 ALPR once implemented:
```yaml
alpr:
  enabled: true
  engine: easyocr
  confidence_threshold: 0.7
  scan_interval_seconds: 2
```

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

## Licence

MIT
