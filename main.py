"""
main.py — Car Incident Logger orchestrator.

Event flow
----------
1. Load config → set up logging
2. Init all modules
3. Start camera capture thread
4. Start rolling buffer thread
5. Start button listener
6. If alpr.enabled → init ALPRRunner + start LiveMatcher
7. Main loop: idle until KeyboardInterrupt
   • Button press  → start audio recording
   • Button release → stop audio + transcribe + parse + save + DB update
                       + notify (alert if known plate)
8. Ctrl+C → graceful shutdown of all threads
"""

import logging
import signal
import sys
import threading
import time
from pathlib import Path

# ── Local modules ─────────────────────────────────────────────────────────────
from modules.config_manager import ConfigManager, setup_logging
from modules.camera_capture import CameraCapture
from modules.rolling_buffer import RollingBuffer
from modules.button_listener import ButtonListener
from modules.audio_recorder import AudioRecorder
from modules.transcription_engine import TranscriptionEngine
from modules.phonetic_plate_parser import parse_plate_from_transcript
from modules.incident_saver import IncidentSaver
from modules.plate_database import PlateDatabase
from modules.alpr_runner import ALPRRunner
from modules.live_matcher import LiveMatcher
from modules.notifier import Notifier

logger = logging.getLogger(__name__)


# ── Global shutdown event ─────────────────────────────────────────────────────
_shutdown = threading.Event()


def _handle_sigint(sig, frame):
    logger.info("SIGINT received — shutting down")
    _shutdown.set()


signal.signal(signal.SIGINT, _handle_sigint)
signal.signal(signal.SIGTERM, _handle_sigint)


# ── Incident processing (runs in a worker thread) ─────────────────────────────

class IncidentProcessor:
    """
    Handles the async work triggered on button release so the button listener
    thread returns immediately.
    """

    def __init__(self, config, rolling_buffer, audio_recorder,
                 transcription_engine, incident_saver, plate_database, notifier):
        self._config = config
        self._rolling_buffer = rolling_buffer
        self._audio_recorder = audio_recorder
        self._transcription_engine = transcription_engine
        self._incident_saver = incident_saver
        self._plate_database = plate_database
        self._notifier = notifier
        self._lock = threading.Lock()
        self._busy = False

    def process(self, audio_data) -> None:
        """Called on button release with the captured audio numpy array."""
        with self._lock:
            if self._busy:
                logger.warning("Incident processing already in progress — skipping")
                return
            self._busy = True

        t = threading.Thread(target=self._run, args=(audio_data,), daemon=True, name="IncidentProcessor")
        t.start()

    def _run(self, audio_data) -> None:
        try:
            self._do_process(audio_data)
        except Exception as e:
            logger.exception("Unhandled error in incident processing: %s", e)
            self._notifier.notify_error(f"Incident processing failed: {e}")
        finally:
            with self._lock:
                self._busy = False

    def _do_process(self, audio_data) -> None:
        # ── 1. Snapshot rolling buffer ────────────────────────────────────────
        buffer_seconds = self._config.buffer_duration
        frames = self._rolling_buffer.get_clip(seconds_back=buffer_seconds)
        logger.info("Captured %d frames from rolling buffer", len(frames))

        # ── 2. Save audio to temp WAV ─────────────────────────────────────────
        import tempfile, os
        tmp_audio_path = None
        if audio_data is not None and len(audio_data) > 0:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                tmp_audio_path = tmp.name
            self._audio_recorder.save_wav(audio_data, tmp_audio_path)
        else:
            logger.warning("No audio data captured")

        # ── 3. Transcribe ─────────────────────────────────────────────────────
        transcript = ""
        if tmp_audio_path:
            try:
                transcript = self._transcription_engine.transcribe(tmp_audio_path)
                self._notifier.notify_transcription(transcript)
            except Exception as e:
                logger.error("Transcription failed: %s", e)
                self._notifier.notify_error(f"Transcription error: {e}")
        else:
            logger.info("No audio — skipping transcription")

        # ── 4. Parse plate ────────────────────────────────────────────────────
        parse_result = parse_plate_from_transcript(transcript)
        plate = parse_result.get("plate", "")
        logger.info("Parsed plate=%r  note=%r  confidence=%.2f",
                    plate, parse_result.get("note"), parse_result.get("confidence"))

        # ── 5. Save incident ──────────────────────────────────────────────────
        incident_dir = self._incident_saver.save_incident(
            frames=frames,
            audio_data=audio_data,
            transcript=transcript,
            parse_result=parse_result,
            audio_recorder=self._audio_recorder,
        )

        # ── 6. Update database ────────────────────────────────────────────────
        if plate:
            known = self._plate_database.is_known_plate(plate)
            meta = {
                "plate": plate,
                "timestamp": incident_dir.name,
                "transcript": transcript,
                "parsed_note": parse_result.get("note", ""),
                "confidence": parse_result.get("confidence", 0.0),
                "clip_path": str(incident_dir / "clip.mp4"),
                "audio_path": str(incident_dir / "audio.wav"),
            }
            self._plate_database.add_incident(plate, meta)
            self._incident_saver.update_matched_flag(incident_dir, known)

            if known:
                plate_info = self._plate_database.get_plate(plate)
                self._notifier.alert_known_plate(plate, plate_info)
            else:
                self._notifier.notify_incident_saved(
                    plate, str(incident_dir), parse_result.get("note", "")
                )
        else:
            self._notifier.notify_incident_saved("", str(incident_dir))

        # ── 7. Clean up temp file ─────────────────────────────────────────────
        if tmp_audio_path:
            try:
                os.unlink(tmp_audio_path)
            except OSError:
                pass


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    # ── Config + logging ──────────────────────────────────────────────────────
    config_path = "config.yaml"
    if not Path(config_path).exists():
        print(f"ERROR: config file not found at {config_path}", file=sys.stderr)
        return 1

    config = ConfigManager(config_path)
    setup_logging(config)
    logger.info("=" * 60)
    logger.info("Car Incident Logger starting up")
    logger.info("=" * 60)

    # Ensure data directories exist
    base = config.storage_base_path
    for sub in ["plates", "unresolved", "models", "config", "logs"]:
        (base / sub).mkdir(parents=True, exist_ok=True)

    # ── Module init ───────────────────────────────────────────────────────────
    notifier = Notifier(
        chime_enabled=config.notifier_chime_enabled,
        chime_file=config.notifier_chime_file,
        console_alerts=config.notifier_console_alerts,
    )

    plate_db = PlateDatabase(db_path=str(base / "plates.db"))

    camera = CameraCapture(
        device_index=config.camera_device_index,
        width=config.camera_width,
        height=config.camera_height,
        fps=config.camera_fps,
        fourcc_str=config.camera_format,
    )

    rolling_buf = RollingBuffer(
        duration_seconds=config.buffer_duration,
        fps=config.camera_fps,
    )

    audio_rec = AudioRecorder(
        device_index=config.audio_device_index,
        sample_rate=config.audio_sample_rate,
        channels=config.audio_channels,
    )

    transcription = TranscriptionEngine(
        model_name=config.transcription_model,
        device=config.transcription_device,
        compute_type=config.transcription_compute_type,
        models_dir=str(base / "models"),
    )

    saver = IncidentSaver(
        base_path=str(base),
        fps=config.camera_fps,
    )

    processor = IncidentProcessor(
        config=config,
        rolling_buffer=rolling_buf,
        audio_recorder=audio_rec,
        transcription_engine=transcription,
        incident_saver=saver,
        plate_database=plate_db,
        notifier=notifier,
    )

    # ── Button callbacks ──────────────────────────────────────────────────────
    def on_button_press():
        notifier.notify_recording_started()
        try:
            audio_rec.start_recording()
        except Exception as e:
            logger.error("Failed to start audio recording: %s", e)
            notifier.notify_error(f"Audio start error: {e}")

    def on_button_release():
        notifier.notify_recording_stopped()
        audio_data = audio_rec.stop_recording()
        processor.process(audio_data)

    button = ButtonListener(
        mode=config.button_mode,
        key=config.button_key,
        gpio_pin=config.button_gpio_pin,
        gpio_pull=config.button_gpio_pull,
    )
    button.on_press = on_button_press
    button.on_release = on_button_release

    # ── Optional ALPR init ────────────────────────────────────────────────────
    alpr = ALPRRunner(
        engine=config.alpr_engine,
        confidence_threshold=config.alpr_confidence_threshold,
        models_dir=str(base / "models"),
    )
    live_matcher = LiveMatcher(
        alpr_runner=alpr,
        plate_database=plate_db,
        notifier=notifier,
        scan_interval=config.alpr_scan_interval,
    )

    # ── Start subsystems ──────────────────────────────────────────────────────
    notifier.notify_info("Starting camera capture...")
    camera.start()

    # Startup health check: wait briefly for the camera to produce a frame
    if camera.wait_for_first_frame(timeout=5.0):
        notifier.notify_info("Camera health check passed — frames arriving")
    else:
        logger.warning("Camera not yet producing frames — will keep retrying in background")
        notifier.notify_info(
            "WARNING: Camera not available yet. The system will keep retrying automatically."
        )

    notifier.notify_info("Starting rolling buffer...")
    rolling_buf.start(camera.get_frame_queue())

    notifier.notify_info("Starting button listener...")
    button.start()

    if config.alpr_enabled:
        notifier.notify_info("Initializing ALPR (Phase 2)...")
        alpr.initialize()
        live_matcher.start(rolling_buf)
    else:
        logger.info("ALPR disabled (alpr.enabled = false in config)")

    mode_str = config.button_mode
    key_str = f"key={config.button_key!r}" if mode_str == "keyboard" else f"GPIO pin {config.button_gpio_pin}"
    notifier.notify_info(
        f"Ready. Button mode: {mode_str} ({key_str})  |  "
        f"Buffer: {config.buffer_duration}s  |  "
        f"Press button to record an incident. Ctrl+C to quit."
    )

    # ── Pre-warm transcription model in background ────────────────────────────
    def _prewarm():
        try:
            transcription.warm_up()
            notifier.notify_info("Transcription model loaded and ready")
        except Exception as e:
            logger.warning("Model warm-up failed: %s", e)

    threading.Thread(target=_prewarm, daemon=True, name="ModelPrewarm").start()

    # ── Main loop: just wait for shutdown ─────────────────────────────────────
    try:
        while not _shutdown.is_set():
            _shutdown.wait(timeout=1.0)
    except KeyboardInterrupt:
        pass

    # ── Graceful shutdown ─────────────────────────────────────────────────────
    logger.info("Shutting down...")
    notifier.notify_info("Shutting down — please wait...")

    button.stop()
    live_matcher.stop()
    rolling_buf.stop()
    camera.stop()
    plate_db.close()

    logger.info("Car Incident Logger stopped cleanly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
