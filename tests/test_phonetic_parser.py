"""
test_phonetic_parser.py — pytest tests for phonetic_plate_parser.

Run with: pytest tests/test_phonetic_parser.py -v
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from modules.phonetic_plate_parser import parse_plate_from_transcript, _token_to_char


# ── _token_to_char unit tests ─────────────────────────────────────────────────

class TestTokenToChar:
    def test_nato_alpha(self):
        assert _token_to_char("alpha") == "A"

    def test_nato_whiskey(self):
        assert _token_to_char("whiskey") == "W"

    def test_nato_zulu(self):
        assert _token_to_char("zulu") == "Z"

    def test_nato_case_insensitive(self):
        assert _token_to_char("BRAVO") == "B"
        assert _token_to_char("Charlie") == "C"

    def test_digit_word_zero(self):
        assert _token_to_char("zero") == "0"

    def test_digit_word_nine(self):
        assert _token_to_char("nine") == "9"

    def test_digit_word_niner(self):
        assert _token_to_char("niner") == "9"

    def test_digit_word_oh(self):
        assert _token_to_char("oh") == "0"

    def test_bare_digit(self):
        assert _token_to_char("5") == "5"

    def test_bare_single_letter(self):
        assert _token_to_char("A") == "A"
        assert _token_to_char("z") == "Z"

    def test_non_plate_word_returns_none(self):
        assert _token_to_char("tailgated") is None
        assert _token_to_char("me") is None
        assert _token_to_char("the") is None

    def test_xray_variants(self):
        # tested via parse_plate_from_transcript since _token_to_char gets clean tokens
        result = parse_plate_from_transcript("x-ray one two three")
        assert result["plate"] == "X123"


# ── parse_plate_from_transcript integration tests ─────────────────────────────

class TestParsePlateFromTranscript:

    def test_wj1843(self):
        result = parse_plate_from_transcript("Whiskey Juliet One Eight Four Three")
        assert result["plate"] == "WJ1843"
        assert result["note"] == ""
        assert result["confidence"] > 0.5

    def test_abc123(self):
        result = parse_plate_from_transcript("Alpha Bravo Charlie One Two Three")
        assert result["plate"] == "ABC123"

    def test_note_separated(self):
        result = parse_plate_from_transcript(
            "November Charlie Seven tailgated me aggressively on the highway"
        )
        assert result["plate"] == "NC7"
        assert "tailgated" in result["note"]

    def test_longer_plate_with_note(self):
        result = parse_plate_from_transcript(
            "Whiskey Juliet One Eight Four Three ran a red light at Main and Fifth"
        )
        assert result["plate"] == "WJ1843"
        assert "red light" in result["note"]

    def test_all_nato_letters(self):
        # Just confirm a full standard plate parses
        result = parse_plate_from_transcript("Tango Echo Sierra Lima Alpha")
        assert result["plate"] == "TESLA"

    def test_digits_only(self):
        result = parse_plate_from_transcript("One Two Three Four")
        assert result["plate"] == "1234"

    def test_mixed_case_input(self):
        result = parse_plate_from_transcript("ALPHA bravo CHARLIE")
        assert result["plate"] == "ABC"

    def test_niner_variant(self):
        result = parse_plate_from_transcript("Alpha Bravo Niner")
        assert result["plate"] == "AB9"

    def test_bare_digits_in_transcript(self):
        result = parse_plate_from_transcript("alpha bravo 1 2 3")
        assert result["plate"] == "AB123"

    def test_empty_transcript(self):
        result = parse_plate_from_transcript("")
        assert result["plate"] == ""
        assert result["confidence"] == 0.0

    def test_no_plate_in_transcript(self):
        result = parse_plate_from_transcript("it was a blue ford pickup truck")
        # All words are non-plate tokens; plate should be empty or very short
        # "a" is a single char — could parse as "A" — so we check confidence is low
        assert result["confidence"] < 0.5

    def test_raw_plate_spoken_populated(self):
        result = parse_plate_from_transcript("Whiskey Juliet One Eight Four Three")
        assert "whiskey" in result["raw_plate_spoken"].lower()
        assert "juliet" in result["raw_plate_spoken"].lower()

    def test_plate_normalized_uppercase(self):
        result = parse_plate_from_transcript("whiskey juliet one eight four three")
        assert result["plate"] == result["plate"].upper()

    def test_plate_no_spaces(self):
        result = parse_plate_from_transcript("alpha bravo charlie one two three")
        assert " " not in result["plate"]

    def test_abc_1234_format(self):
        result = parse_plate_from_transcript("Alpha Bravo Charlie One Two Three Four")
        assert result["plate"] == "ABC1234"

    def test_confidence_higher_for_longer_plates(self):
        short = parse_plate_from_transcript("Alpha Bravo")
        long_ = parse_plate_from_transcript("Alpha Bravo Charlie Delta Echo Foxtrot One")
        assert long_["confidence"] >= short["confidence"]
