# CLAUDE.md — Car Incident Logger

## What this project does

A fully local, in-car incident logging system. Press a physical button (or keyboard key) to record a voice note describing a license plate using NATO phonetic alphabet. The system transcribes audio locally via faster-whisper, parses the plate from spoken phonetics, and saves a timestamped video clip + audio + metadata to disk, indexed in SQLite.

No internet required. Runs on Raspberry Pi 4/5 or any Linux/macOS machine with a USB camera and mic.

## Architecture

```
main.py                         Orchestrator — wires everything together
  ├── CameraCapture             Background thread: reads USB camera frames into a queue
  ├── RollingBuffer             Background thread: consumes frame queue into a fixed-size deque
  ├── ButtonListener            pynput (keyboard) or RPi.GPIO (physical button)
  ├── AudioRecorder             sounddevice InputStream, accumulates chunks while button held
  ├── TranscriptionEngine       faster-whisper (CTranslate2), lazy-loaded
  ├── phonetic_plate_parser     Pure function: NATO phonetic words → plate string
  ├── IncidentSaver             Writes clip (ffmpeg preferred, cv2 fallback), audio, metadata
  ├── PlateDatabase             SQLite with WAL mode, thread-local connections
  ├── Notifier                  Console output + optional chime
  ├── ALPRRunner                STUB (Phase 2) — automatic plate recognition
  └── LiveMatcher               STUB (Phase 2) — background ALPR scanning
```

## Key flows

**Button press → release:**
1. `on_press`: starts audio recording
2. `on_release`: stops audio, hands off to `IncidentProcessor` (runs in its own thread)
3. Processor: snapshots rolling buffer → saves temp WAV → transcribes → parses plate → saves incident → updates DB → notifies

**Startup:** config load → logging → module init → camera start → health check → rolling buffer start → button listener start → optional ALPR init → model pre-warm (background) → main loop (wait for shutdown)

**Shutdown (SIGINT/SIGTERM):** button stop → live matcher stop → rolling buffer stop → camera stop → DB close

## Threading model

- `CameraCapture` thread: single producer writing to a bounded Queue (drops oldest on overflow)
- `RollingBuffer` thread: single consumer reading from that Queue into a deque with maxlen
- `ButtonListener`: pynput runs its own daemon thread; callbacks fire in short-lived daemon threads
- `IncidentProcessor`: spawns a daemon thread per incident (guarded by a busy flag so only one runs at a time)
- `AudioRecorder`: PortAudio callback thread appends chunks under a lock
- `TranscriptionEngine`: model loaded lazily, pre-warmed in a background thread

## Common tasks

```bash
# Run the logger
python3 main.py

# Run tests
pip install pytest
pytest tests/ -v

# Test camera hardware
python3 scripts/test_camera.py

# Test button hardware
python3 scripts/test_button.py

# Init DB manually
python3 scripts/setup_db.py
```

## Config

All settings in `config.yaml`. Key sections: camera, buffer, audio, transcription, button, storage, alpr, notifier, logging. See README.md for full documentation.

## Data layout

```
data/
  plates/{PLATE}/incidents/{TIMESTAMP}/   clip.mp4, audio.wav, transcript.txt, metadata.json
  unresolved/{TIMESTAMP}/                  incidents where no plate was parsed
  models/                                  whisper model cache
  logs/                                    rotating system log
  plates.db                                SQLite database
```

## Phase 2 (not yet implemented)

`alpr_runner.py` and `live_matcher.py` are stubs. When implemented, they will run ALPR (EasyOCR/OpenALPR/PlateRecognizer) on live frames and alert on known-plate matches. Set `alpr.enabled: true` in config once ready.

## Testing notes

- `test_phonetic_parser.py` — covers full NATO alphabet, digit words, edge cases (niner, oh, x-ray), note separation, confidence scoring
- `test_plate_database.py` — uses tmp_path fixture for isolated SQLite, covers CRUD + search + sightings + delete
- Tests do NOT require camera, microphone, or GPU
