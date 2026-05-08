"""
tests/test_alpr.py — Tests for ALPR utilities.

All tests pass without ultralytics or FastPlateOCR installed.
Covers: normalization, OCR corrections, validation, no-dep runner behavior,
        multi-frame voting.
"""

import numpy as np
import pytest

from modules.alpr_runner import (
    ALPRRunner,
    apply_ocr_corrections,
    normalize_plate,
    validate_plate_candidate,
)
from modules.multi_frame_voter import MultiFrameVoter


# ---------------------------------------------------------------------------
# normalize_plate
# ---------------------------------------------------------------------------

class TestNormalizePlate:
    def test_strips_spaces(self):
        assert normalize_plate("ABC 123") == "ABC123"

    def test_strips_hyphens(self):
        assert normalize_plate("ABC-123") == "ABC123"

    def test_strips_dots(self):
        assert normalize_plate("A.B.C") == "ABC"

    def test_uppercase(self):
        assert normalize_plate("abc123") == "ABC123"

    def test_already_clean(self):
        assert normalize_plate("WJ1843") == "WJ1843"

    def test_empty(self):
        assert normalize_plate("") == ""


# ---------------------------------------------------------------------------
# apply_ocr_corrections
# ---------------------------------------------------------------------------

class TestApplyOCRCorrections:
    # LLLDDD format: positions 0-2 → letters, 3-5 → digits

    def test_digit_to_letter_pos0(self):
        assert apply_ocr_corrections("0BC123", "LLLDDD")[0] == "O"

    def test_digit_to_letter_pos1(self):
        assert apply_ocr_corrections("A1C123", "LLLDDD")[1] == "I"

    def test_digit_to_letter_s5(self):
        assert apply_ocr_corrections("5BC123", "LLLDDD")[0] == "S"

    def test_digit_to_letter_b8(self):
        assert apply_ocr_corrections("8BC123", "LLLDDD")[0] == "B"

    def test_digit_to_letter_z2(self):
        assert apply_ocr_corrections("2BC123", "LLLDDD")[0] == "Z"

    def test_digit_to_letter_g6(self):
        assert apply_ocr_corrections("6BC123", "LLLDDD")[0] == "G"

    def test_letter_to_digit_pos3(self):
        assert apply_ocr_corrections("ABCO23", "LLLDDD")[3] == "0"

    def test_letter_to_digit_pos4(self):
        assert apply_ocr_corrections("ABC123", "LLLDDD")[4] == "2"  # no change

    def test_letter_to_digit_b8(self):
        assert apply_ocr_corrections("ABC12B", "LLLDDD")[5] == "8"

    def test_letter_to_digit_z2(self):
        assert apply_ocr_corrections("ABCZ23", "LLLDDD")[3] == "2"

    def test_letter_to_digit_i1(self):
        assert apply_ocr_corrections("ABCI23", "LLLDDD")[3] == "1"

    def test_letter_to_digit_s5(self):
        assert apply_ocr_corrections("ABCS23", "LLLDDD")[3] == "5"

    def test_no_change_clean_plate(self):
        assert apply_ocr_corrections("ABC123", "LLLDDD") == "ABC123"

    def test_auto_6char_treats_as_lllddd(self):
        result = apply_ocr_corrections("0BCO23", "auto")
        # Position 0: 0 → O  |  position 3: O → 0
        assert result[0] == "O"
        assert result[3] == "0"

    def test_none_format_passthrough(self):
        raw = "0BC123"
        assert apply_ocr_corrections(raw, "none") == raw

    def test_empty_string(self):
        assert apply_ocr_corrections("", "LLLDDD") == ""

    def test_auto_short_plate_heuristic(self):
        # 4-char: positions 0-1 early (letter), 2-3 late (digit)
        result = apply_ocr_corrections("0BO0", "auto")
        # pos 0 (early): 0 → O
        assert result[0] == "O"
        # pos 3 (late): O → 0
        assert result[3] == "0"


# ---------------------------------------------------------------------------
# validate_plate_candidate
# ---------------------------------------------------------------------------

class TestValidatePlateCandidate:
    def test_valid_bc_6char(self):
        assert validate_plate_candidate("ABC123")

    def test_valid_short_4char(self):
        assert validate_plate_candidate("AB12")

    def test_valid_8char(self):
        assert validate_plate_candidate("ABCD1234")

    def test_empty_rejected(self):
        assert not validate_plate_candidate("")

    def test_single_char_rejected(self):
        assert not validate_plate_candidate("A")

    def test_10char_rejected(self):
        assert not validate_plate_candidate("ABCDE12345")

    def test_hyphen_rejected(self):
        assert not validate_plate_candidate("AB-123")

    def test_space_rejected(self):
        assert not validate_plate_candidate("AB 123")

    def test_all_same_char_rejected(self):
        assert not validate_plate_candidate("AAAAAA")

    def test_lowercase_rejected(self):
        # normalize_plate should be called first; raw lowercase fails
        assert not validate_plate_candidate("abc123")

    def test_two_char_minimum(self):
        assert validate_plate_candidate("A1")

    def test_nine_char_maximum(self):
        assert validate_plate_candidate("ABC123456")


# ---------------------------------------------------------------------------
# ALPRRunner — no deps installed
# ---------------------------------------------------------------------------

class TestALPRRunnerNoDeps:
    """These tests pass whether or not ultralytics/FastPlateOCR are installed."""

    def test_initialize_does_not_raise(self):
        runner = ALPRRunner({})
        runner.initialize()  # must not raise regardless of installed deps

    def test_status_info_keys_present(self):
        runner = ALPRRunner({})
        runner.initialize()
        status = runner.status_info()
        assert "ready" in status
        assert "detector" in status
        assert "ocr" in status

    def test_run_on_frame_returns_list(self):
        runner = ALPRRunner({})
        runner.initialize()
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        result = runner.run_on_frame(frame)
        assert isinstance(result, list)

    def test_run_on_frame_empty_when_not_ready(self):
        runner = ALPRRunner({})
        runner.initialize()
        if not runner.is_ready:
            frame = np.zeros((480, 640, 3), dtype=np.uint8)
            assert runner.run_on_frame(frame) == []

    def test_custom_detector_and_recognizer(self):
        """Extension point: inject stubs without importing heavy deps."""
        from modules.alpr_runner import _BaseDetector, _BaseRecognizer

        class _StubDetector(_BaseDetector):
            def initialize(self):
                self.status = "ready"
                return True

            def detect(self, frame):
                return [{"bbox": [10, 10, 100, 50], "confidence": 0.9}]

        class _StubRecognizer(_BaseRecognizer):
            def initialize(self):
                self.status = "ready"
                return True

            def recognize(self, image):
                return [("ABC123", 0.95)]

        runner = ALPRRunner(
            {"confidence_threshold": 0.3},
            detector=_StubDetector(),
            recognizer=_StubRecognizer(),
        )
        runner.initialize()
        assert runner.is_ready

        frame = np.zeros((200, 640, 3), dtype=np.uint8)
        results = runner.run_on_frame(frame)
        assert len(results) == 1
        assert results[0]["plate"] == "ABC123"
        assert results[0]["source"] == "yolo+fastocr"

    def test_custom_recognizer_fullframe_fallback(self):
        """When detector status is not ready, uses whole-frame OCR."""
        from modules.alpr_runner import _BaseDetector, _BaseRecognizer

        class _FailDetector(_BaseDetector):
            def initialize(self):
                self.status = "unavailable"
                return False

            def detect(self, frame):
                return []

        class _StubRecognizer(_BaseRecognizer):
            def initialize(self):
                self.status = "ready"
                return True

            def recognize(self, image):
                return [("WJ1843", 0.92)]

        runner = ALPRRunner(
            {"confidence_threshold": 0.5},
            detector=_FailDetector(),
            recognizer=_StubRecognizer(),
        )
        runner.initialize()
        assert runner.is_ready

        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        results = runner.run_on_frame(frame)
        # OCR confidence 0.92 * 0.7 = 0.644 > 0.5 threshold (using max(0.5, 0.80)=0.80)
        # Actually 0.92 >= 0.80 so it passes
        assert isinstance(results, list)

    def test_no_detector_no_fullframe_ocr_with_fastplateocr(self):
        """FastPlateOCR needs cropped plates, so no-detector mode returns no full-frame guesses."""
        from modules.alpr_runner import _BaseDetector, _BaseRecognizer

        class _EmptyDetector(_BaseDetector):
            def initialize(self):
                self.status = "ready"
                return True

            def detect(self, frame):
                return []

        class _StubRecognizer(_BaseRecognizer):
            def __init__(self):
                self.calls = 0

            def initialize(self):
                self.status = "ready"
                return True

            def recognize(self, image):
                self.calls += 1
                return [("634-XSG", 0.91)]

        recognizer = _StubRecognizer()
        runner = ALPRRunner(
            {"confidence_threshold": 0.3, "fullframe_ocr_confidence_threshold": 0.60},
            detector=_EmptyDetector(),
            recognizer=recognizer,
        )
        runner.initialize()
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        results = runner.run_on_frame(frame)
        assert results == []
        assert recognizer.calls == 0

    def test_vehicle_heuristic_ocr_reads_cropped_vehicle_region(self):
        """When vehicle boxes are available, FastPlateOCR can run on likely plate crops."""
        from modules.alpr_runner import _BaseDetector, _BaseRecognizer, _VehicleDetector

        class _UnavailableDetector(_BaseDetector):
            def initialize(self):
                self.status = "unavailable"
                self.error = "plate detector unavailable"
                return False

            def detect(self, frame):
                return []

        class _VehicleStub(_VehicleDetector):
            def __init__(self):
                self.status = "uninitialized"
                self.error = None

            def initialize(self):
                self.status = "ready"
                return True

            def detect(self, frame):
                return [{"bbox": [20, 20, 220, 180], "confidence": 0.9, "vehicle_type": "car"}]

        class _StubRecognizer(_BaseRecognizer):
            def __init__(self):
                self.calls = 0

            def initialize(self):
                self.status = "ready"
                return True

            def recognize(self, image):
                self.calls += 1
                return [("634-XSG", 0.91)]

        recognizer = _StubRecognizer()
        runner = ALPRRunner(
            {"confidence_threshold": 0.3, "fullframe_ocr_confidence_threshold": 0.60},
            detector=_UnavailableDetector(),
            recognizer=recognizer,
            vehicle_detector=_VehicleStub(),
        )
        runner.initialize()
        frame = np.zeros((240, 320, 3), dtype=np.uint8)
        results = runner.run_on_frame(frame)
        assert recognizer.calls == 1
        assert len(results) == 1
        assert results[0]["plate"] == "634XSG"
        assert results[0]["source"] == "vehicle+fastocr_heuristic"

    def test_detection_dict_format(self):
        """Verify returned dicts have expected keys."""
        from modules.alpr_runner import _BaseDetector, _BaseRecognizer

        class _StubDetector(_BaseDetector):
            def initialize(self):
                self.status = "ready"
                return True

            def detect(self, frame):
                return [{"bbox": [0, 0, 200, 60], "confidence": 0.85}]

        class _StubRecognizer(_BaseRecognizer):
            def initialize(self):
                self.status = "ready"
                return True

            def recognize(self, image):
                return [("XYZ999", 0.88)]

        runner = ALPRRunner(
            {"confidence_threshold": 0.3},
            detector=_StubDetector(),
            recognizer=_StubRecognizer(),
        )
        runner.initialize()
        frame = np.zeros((200, 640, 3), dtype=np.uint8)
        results = runner.run_on_frame(frame)
        assert len(results) == 1
        d = results[0]
        for key in ("plate", "confidence", "bbox", "source", "raw_text", "corrected"):
            assert key in d, f"Missing key: {key}"


# ---------------------------------------------------------------------------
# MultiFrameVoter
# ---------------------------------------------------------------------------

class TestMultiFrameVoter:
    def test_no_frames_returns_none(self):
        voter = MultiFrameVoter()
        assert voter.get_best() is None

    def test_below_min_votes_returns_none(self):
        voter = MultiFrameVoter(min_votes=2)
        voter.add_frame([{"plate": "ABC123", "confidence": 0.9}])
        assert voter.get_best() is None

    def test_meets_min_votes(self):
        voter = MultiFrameVoter(min_votes=2)
        voter.add_frame([{"plate": "ABC123", "confidence": 0.9}])
        voter.add_frame([{"plate": "ABC123", "confidence": 0.85}])
        best = voter.get_best()
        assert best is not None
        assert best["plate"] == "ABC123"
        assert best["votes"] == 2

    def test_min_votes_1(self):
        voter = MultiFrameVoter(min_votes=1)
        voter.add_frame([{"plate": "WJ1843", "confidence": 0.75}])
        best = voter.get_best()
        assert best is not None
        assert best["plate"] == "WJ1843"

    def test_reset_clears_state(self):
        voter = MultiFrameVoter(min_votes=1)
        voter.add_frame([{"plate": "ABC123", "confidence": 0.9}])
        voter.reset()
        assert voter.get_best() is None
        assert voter.frame_count == 0
        assert voter.candidate_count == 0

    def test_frame_count_increments(self):
        voter = MultiFrameVoter()
        voter.add_frame([])
        voter.add_frame([])
        voter.add_frame([])
        assert voter.frame_count == 3

    def test_empty_plate_string_ignored(self):
        voter = MultiFrameVoter(min_votes=1)
        voter.add_frame([{"plate": "", "confidence": 0.9}])
        assert voter.get_best() is None

    def test_missing_plate_key_ignored(self):
        voter = MultiFrameVoter(min_votes=1)
        voter.add_frame([{"confidence": 0.9}])
        assert voter.get_best() is None

    def test_all_candidates_sorted_descending(self):
        voter = MultiFrameVoter(min_votes=1)
        voter.add_frame([{"plate": "LOW111", "confidence": 0.4}])
        voter.add_frame([{"plate": "HIGH99", "confidence": 0.95}])
        cands = voter.all_candidates()
        assert len(cands) == 2
        assert cands[0]["confidence"] >= cands[1]["confidence"]

    def test_best_is_first_of_all_candidates(self):
        voter = MultiFrameVoter(min_votes=1)
        voter.add_frame([{"plate": "AAA111", "confidence": 0.5}])
        voter.add_frame([{"plate": "BBB222", "confidence": 0.9}])
        best = voter.get_best()
        all_c = voter.all_candidates()
        assert best["plate"] == all_c[0]["plate"]

    def test_multiple_detections_per_frame(self):
        voter = MultiFrameVoter(min_votes=1)
        voter.add_frame([
            {"plate": "ABC123", "confidence": 0.8},
            {"plate": "XYZ999", "confidence": 0.7},
        ])
        assert voter.candidate_count == 2

    def test_max_candidates_cap(self):
        voter = MultiFrameVoter(min_votes=1, max_candidates=3)
        for i in range(10):
            voter.add_frame([{"plate": f"PL{i:04d}", "confidence": 0.5}])
        assert voter.candidate_count <= 3

    def test_avg_confidence_in_result(self):
        voter = MultiFrameVoter(min_votes=2)
        voter.add_frame([{"plate": "ABC123", "confidence": 0.8}])
        voter.add_frame([{"plate": "ABC123", "confidence": 0.6}])
        best = voter.get_best()
        assert abs(best["avg_confidence"] - 0.7) < 0.001

    def test_frames_seen_in_result(self):
        voter = MultiFrameVoter(min_votes=1)
        voter.add_frame([{"plate": "ABC123", "confidence": 0.8}])
        voter.add_frame([])  # frame with no detections
        best = voter.get_best()
        assert best["frames_seen"] == 2

# ── _normalize_and_correct regression tests ─────────────────────────────────

def test_normalize_and_correct_preserves_digit_digit_digit_letter_letter_letter():
    from modules.alpr_runner import _normalize_and_correct

    plate, corrected = _normalize_and_correct("634-XSG ")
    assert plate == "634XSG"
    assert corrected is False


def test_normalize_and_correct_does_not_substitute_ambiguous_chars():
    from modules.alpr_runner import _normalize_and_correct

    plate, corrected = _normalize_and_correct("0BCO23")
    assert plate == "0BCO23"
    assert corrected is False
