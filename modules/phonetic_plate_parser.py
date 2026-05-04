"""
phonetic_plate_parser.py — Parses NATO phonetic alphabet + digit words into
a license plate string, separating the plate portion from any freeform note.

Examples
--------
"Whiskey Juliet One Eight Four Three"         → plate="WJ1843"
"Alpha Bravo Charlie One Two Three"           → plate="ABC123"
"November Charlie Seven tailgated me"         → plate="NC7", note="tailgated me"
"Tango Echo Sierra Lima Alpha november 5"     → plate="TESLA5" (mixed-case OK)
"""

import re
import logging
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ── Phonetic alphabet mapping ─────────────────────────────────────────────────

NATO: Dict[str, str] = {
    "alpha": "A",
    "bravo": "B",
    "charlie": "C",
    "delta": "D",
    "echo": "E",
    "foxtrot": "F",
    "golf": "G",
    "hotel": "H",
    "india": "I",
    "juliet": "J",
    "kilo": "K",
    "lima": "L",
    "mike": "M",
    "november": "N",
    "oscar": "O",
    "papa": "P",
    "quebec": "Q",
    "romeo": "R",
    "sierra": "S",
    "tango": "T",
    "uniform": "U",
    "victor": "V",
    "whiskey": "W",
    "x-ray": "X",
    "xray": "X",
    "yankee": "Y",
    "zulu": "Z",
}

DIGITS: Dict[str, str] = {
    "zero": "0",
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
    "niner": "9",   # aviation/military variant
    "oh": "0",      # common spoken variant for zero
}

# Single-letter words that are common direct letter callouts
_DIRECT_LETTERS = set("abcdefghijklmnopqrstuvwxyz")


def _token_to_char(token: str) -> Optional[str]:
    """
    Convert a single spoken token to a single plate character (letter or digit).
    Returns None if the token is not plate-relevant.
    """
    t = token.lower().strip()

    # NATO phonetic letter
    if t in NATO:
        return NATO[t]

    # Digit word
    if t in DIGITS:
        return DIGITS[t]

    # Bare digit character ("1", "2", ...)
    if re.fullmatch(r"\d", t):
        return t

    # Single letter spoken directly ("A", "b", ...)
    if re.fullmatch(r"[a-zA-Z]", t):
        return t.upper()

    return None


def _tokenize(text: str) -> List[str]:
    """Split transcript into lowercase tokens, preserving x-ray as one token."""
    text = text.lower()
    # Handle "x-ray" / "x ray" → normalize to "x-ray"
    text = re.sub(r"\bx[\s\-]ray\b", "x-ray", text)
    # Strip punctuation except hyphens inside tokens
    text = re.sub(r"[^\w\s\-]", " ", text)
    return text.split()


def parse_plate_from_transcript(transcript: str) -> dict:
    """
    Parse a transcript string and return a dict with:
        plate            : str   — normalized uppercase plate (e.g. "WJ1843"), or ""
        raw_plate_spoken : str   — the original spoken words that formed the plate
        note             : str   — remaining freeform text after the plate section
        confidence       : float — 0.0–1.0 heuristic confidence score
    """
    if not transcript or not transcript.strip():
        return {"plate": "", "raw_plate_spoken": "", "note": "", "confidence": 0.0}

    tokens = _tokenize(transcript)

    # Map each token to its plate char (or None)
    mapped: List[Optional[str]] = [_token_to_char(t) for t in tokens]

    # Find the longest contiguous run of plate chars
    # A "plate section" must be at least 2 chars to avoid false positives
    best_start, best_end = _find_best_plate_span(mapped)

    if best_end - best_start < 2:
        # No plate found
        return {
            "plate": "",
            "raw_plate_spoken": "",
            "note": " ".join(tokens).strip(),
            "confidence": 0.0,
        }

    plate_chars = [mapped[i] for i in range(best_start, best_end)]
    plate_str = "".join(plate_chars)  # type: ignore[arg-type]

    raw_spoken_tokens = tokens[best_start:best_end]
    raw_plate_spoken = " ".join(raw_spoken_tokens)

    # Everything after the plate section is the note
    note_tokens = tokens[best_end:]
    note = " ".join(note_tokens).strip()

    confidence = _score_confidence(plate_str, best_end - best_start, len(tokens))

    logger.debug(
        "Parsed plate=%r  note=%r  confidence=%.2f  raw=%r",
        plate_str,
        note,
        confidence,
        raw_plate_spoken,
    )

    return {
        "plate": plate_str,
        "raw_plate_spoken": raw_plate_spoken,
        "note": note,
        "confidence": confidence,
    }


def _find_best_plate_span(mapped: List[Optional[str]]) -> Tuple[int, int]:
    """
    Find the start and end indices of the longest contiguous run of non-None
    values in *mapped*.  Returns (0, 0) if none found.

    Ties are broken by earliest occurrence (prefer the plate mentioned first).
    """
    best_start = 0
    best_end = 0
    best_len = 0

    i = 0
    while i < len(mapped):
        if mapped[i] is not None:
            j = i
            while j < len(mapped) and mapped[j] is not None:
                j += 1
            run_len = j - i
            if run_len > best_len:
                best_len = run_len
                best_start = i
                best_end = j
            i = j
        else:
            i += 1

    return best_start, best_end


def _score_confidence(plate: str, plate_token_count: int, total_tokens: int) -> float:
    """
    Heuristic confidence score.

    Rules (all additive, clamped to [0.0, 1.0]):
    - Longer plates are more confident (max +0.5 at 7+ chars)
    - Mix of letters + digits → +0.2
    - Short plates (< 3 chars) with few total tokens → lower score
    """
    length = len(plate)

    score = min(0.5, length * 0.07)  # up to 0.5 for long plates

    has_letter = any(c.isalpha() for c in plate)
    has_digit = any(c.isdigit() for c in plate)
    if has_letter and has_digit:
        score += 0.25

    # Penalise very short plates
    if length < 3:
        score *= 0.5

    # Bonus if plate tokens are the majority of the transcript
    if total_tokens > 0:
        ratio = plate_token_count / total_tokens
        score += min(0.25, ratio * 0.25)

    return round(min(1.0, max(0.0, score)), 2)
