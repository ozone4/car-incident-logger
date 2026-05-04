"""
incident_trigger.py — Abstract interface for incident triggers.

Concrete implementations:
  - WebTrigger: fires from the Flask dashboard button
  - (Future) HardwareButtonTrigger: fires from a GPIO or USB button
  - (Future) ALPRAlertTrigger: fires when a known plate is detected
"""

import logging
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

# Type alias for the callback that triggers receive.
# Signature: callback(source: str, metadata: dict) -> dict
TriggerCallback = Callable[[str, Dict[str, Any]], Dict[str, Any]]


class IncidentTrigger(ABC):
    """Base class for anything that can trigger a dashcam incident capture."""

    @property
    @abstractmethod
    def source_name(self) -> str:
        """Short identifier for the trigger source (e.g. 'web', 'gpio', 'alpr')."""

    @abstractmethod
    def arm(self, callback: TriggerCallback) -> None:
        """Register the callback to invoke when the trigger fires."""

    @abstractmethod
    def disarm(self) -> None:
        """Stop listening for triggers."""

    @property
    @abstractmethod
    def is_armed(self) -> bool:
        """Whether the trigger is currently active."""


class WebTrigger(IncidentTrigger):
    """Trigger fired by a POST from the web dashboard."""

    def __init__(self):
        self._callback: Optional[TriggerCallback] = None

    @property
    def source_name(self) -> str:
        return "web"

    def arm(self, callback: TriggerCallback) -> None:
        self._callback = callback
        logger.info("WebTrigger armed")

    def disarm(self) -> None:
        self._callback = None

    @property
    def is_armed(self) -> bool:
        return self._callback is not None

    def fire(self, metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Called by the web route handler. Returns the capture result."""
        if self._callback is None:
            return {"ok": False, "error": "Trigger is not armed"}
        return self._callback(self.source_name, metadata or {})


class HardwareButtonTrigger(IncidentTrigger):
    """Placeholder for a future GPIO / USB hardware button trigger.

    When implemented, this would listen on a GPIO pin or USB HID device
    and fire the callback on button press.
    """

    def __init__(self, pin: int = 17):
        self._pin = pin
        self._callback: Optional[TriggerCallback] = None
        self._armed = False

    @property
    def source_name(self) -> str:
        return "hardware_button"

    def arm(self, callback: TriggerCallback) -> None:
        self._callback = callback
        self._armed = True
        logger.info("HardwareButtonTrigger armed (pin=%d) — stub, not yet wired", self._pin)

    def disarm(self) -> None:
        self._armed = False
        self._callback = None

    @property
    def is_armed(self) -> bool:
        return self._armed
