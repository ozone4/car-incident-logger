"""
notifier.py — Alert system for known-plate matches and system events.

Supports:
  • Logger-routed alerts (always on; honors logging config in app.py / main.py)
  • Optional audio chime via playsound (requires: pip install playsound)

The legacy `console_alerts` flag is accepted for backward compatibility but is
now a no-op — every notification routes through `logger` so that systemd
journal, the rotating file at `data/logs/system.log`, and any other handler
configured at app startup all see the same events.
"""

import logging
import threading
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class Notifier:
    def __init__(
        self,
        chime_enabled: bool = False,
        chime_file: str = "",
        console_alerts: bool = True,  # kept for compat; routing is always via logger
    ):
        self.chime_enabled = chime_enabled
        self.chime_file = chime_file
        self.console_alerts = console_alerts
        self._chime_available = False

        if self.chime_enabled:
            self._check_chime()

    # ── Public API ────────────────────────────────────────────────────────────

    def alert_known_plate(self, plate: str, context: Optional[dict] = None) -> None:
        """Fire a HIGH-PRIORITY alert for a known plate match."""
        msg = f"[ALERT] Known plate detected: {plate}"
        if context:
            inc = context.get("incident_count", "?")
            last = context.get("last_seen", "?")
            msg += f"  (seen {inc}x, last: {last})"
        logger.warning(msg)
        self._play_chime()

    def notify_incident_saved(self, plate: str, incident_dir: str, note: str = "") -> None:
        """Confirm to the user that an incident was logged."""
        if plate:
            msg = f"[SAVED] Incident logged for plate {plate}"
        else:
            msg = "[SAVED] Incident logged (no plate parsed)"
        if note:
            msg += f"  Note: {note!r}"
        msg += f"  → {incident_dir}"
        logger.info(msg)

    def notify_recording_started(self) -> None:
        logger.info("[REC] Recording started — hold button...")

    def notify_recording_stopped(self) -> None:
        logger.info("[REC] Recording stopped — processing...")

    def notify_transcription(self, transcript: str) -> None:
        logger.info("Transcript: %r", transcript)

    def notify_error(self, message: str) -> None:
        logger.error(message)

    def notify_info(self, message: str) -> None:
        logger.info(message)

    # ── Chime ─────────────────────────────────────────────────────────────────

    def _check_chime(self) -> None:
        try:
            import playsound  # noqa: F401
            if self.chime_file and Path(self.chime_file).exists():
                self._chime_available = True
                logger.info("Chime enabled: %s", self.chime_file)
            else:
                logger.warning(
                    "Chime file not found: %r — chime disabled", self.chime_file
                )
        except ImportError:
            logger.warning(
                "playsound not installed — chime disabled. "
                "Install with: pip install playsound"
            )

    def _play_chime(self) -> None:
        if not self._chime_available:
            return
        try:
            import playsound

            # Run in a thread so we don't block the main loop
            t = threading.Thread(
                target=playsound.playsound,
                args=(self.chime_file,),
                daemon=True,
                name="Chime",
            )
            t.start()
        except Exception as e:
            logger.debug("Chime playback error: %s", e)
