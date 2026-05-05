"""
overlay.py — Timestamp overlay for dashcam frames.

Applies a readable timestamp (and optional custom text) to a frame copy.
Applied at write time so ALPR sees clean frames.
"""

from typing import Optional, Tuple

import cv2
import numpy as np
from datetime import datetime


# Position presets: (x_func, y_func) taking (frame_w, frame_h, text_w, text_h)
_POSITIONS = {
    "bottom-left": lambda w, h, tw, th: (10, h - 10),
    "bottom-right": lambda w, h, tw, th: (w - tw - 10, h - 10),
    "top-left": lambda w, h, tw, th: (10, th + 10),
    "top-right": lambda w, h, tw, th: (w - tw - 10, th + 10),
}


def apply_timestamp(
    frame: np.ndarray,
    timestamp: Optional[float] = None,
    position: str = "bottom-left",
    font_scale: float = 0.7,
    color: tuple = (255, 255, 255),
    background: bool = True,
) -> np.ndarray:
    """Return a copy of *frame* with a burned-in timestamp overlay.

    Parameters
    ----------
    frame : np.ndarray
        BGR image (not modified in place).
    timestamp : float | None
        Unix epoch seconds.  ``None`` → ``datetime.now()``.
    position : str
        One of ``bottom-left``, ``bottom-right``, ``top-left``, ``top-right``.
    font_scale : float
        OpenCV font scale.
    color : tuple
        BGR text colour.
    background : bool
        Draw a dark rectangle behind the text for readability.
    """
    out = frame.copy()
    if timestamp is not None:
        dt = datetime.fromtimestamp(timestamp)
    else:
        dt = datetime.now()
    text = dt.strftime("%Y-%m-%d %H:%M:%S")

    font = cv2.FONT_HERSHEY_SIMPLEX
    thickness = max(1, int(font_scale * 2))
    (tw, th), baseline = cv2.getTextSize(text, font, font_scale, thickness)

    h, w = out.shape[:2]
    pos_fn = _POSITIONS.get(position, _POSITIONS["bottom-left"])
    x, y = pos_fn(w, h, tw, th + baseline)

    if background:
        pad = 4
        cv2.rectangle(
            out,
            (x - pad, y - th - pad),
            (x + tw + pad, y + baseline + pad),
            (0, 0, 0),
            cv2.FILLED,
        )

    cv2.putText(out, text, (x, y), font, font_scale, color, thickness, cv2.LINE_AA)
    return out
