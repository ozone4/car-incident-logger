"""
multi_frame_voter.py — Aggregate ALPR candidates across frames and vote.

Usage:
    voter = MultiFrameVoter(min_votes=2)
    for frame in frames:
        detections = runner.run_on_frame(frame)
        voter.add_frame(detections)
    best = voter.get_best()   # → {"plate": "ABC123", "confidence": 0.84, "votes": 7, ...}
    voter.reset()

Scoring: combined score = avg_confidence * 0.7 + vote_frequency * 0.3
  where vote_frequency = votes / frames_seen, capped at 1.0 with a 3× multiplier
  so a plate seen in 1/3 of frames already reaches the cap.
"""

from __future__ import annotations


class MultiFrameVoter:
    """
    Accumulates plate detection candidates across video frames and selects
    the best plate by weighted voting.

    Parameters
    ----------
    min_votes : int
        Minimum number of frames a plate must appear in to be considered.
    max_candidates : int
        Cap on distinct plate strings tracked at once (prevents memory growth
        from noisy OCR producing many spurious strings).
    """

    def __init__(self, min_votes: int = 2, max_candidates: int = 20) -> None:
        self._min_votes = min_votes
        self._max_candidates = max_candidates
        self._votes: dict[str, list[float]] = {}  # plate → [confidence, ...]
        self._frame_count = 0

    # ------------------------------------------------------------------
    # Accumulation
    # ------------------------------------------------------------------

    def add_frame(self, detections: list[dict]) -> None:
        """
        Record detections from one frame.

        Each detection dict must have at least:
          "plate"       str    (non-empty)
          "confidence"  float  (0.0–1.0)
        """
        self._frame_count += 1
        for det in detections:
            plate = det.get("plate", "")
            if not plate:
                continue
            conf = float(det.get("confidence", 0.0))
            if plate not in self._votes:
                if len(self._votes) >= self._max_candidates:
                    continue  # ignore new candidates when at capacity
                self._votes[plate] = []
            self._votes[plate].append(conf)

    # ------------------------------------------------------------------
    # Retrieval
    # ------------------------------------------------------------------

    def get_best(self) -> dict | None:
        """
        Return the highest-scoring candidate that meets min_votes.
        Returns None if no candidate qualifies.

        Keys in the returned dict:
          plate          str
          confidence     float  (combined score)
          votes          int    (number of frames it appeared in)
          frames_seen    int    (total frames added so far)
          avg_confidence float  (mean per-detection confidence)
        """
        scored = self._score_candidates()
        return scored[0] if scored else None

    def all_candidates(self) -> list[dict]:
        """All candidates meeting min_votes, sorted by score descending."""
        return self._score_candidates()

    # ------------------------------------------------------------------
    # Housekeeping
    # ------------------------------------------------------------------

    def reset(self) -> None:
        """Clear all accumulated state."""
        self._votes.clear()
        self._frame_count = 0

    @property
    def frame_count(self) -> int:
        """Total number of frames added since last reset."""
        return self._frame_count

    @property
    def candidate_count(self) -> int:
        """Number of distinct plate strings currently tracked."""
        return len(self._votes)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _score_candidates(self) -> list[dict]:
        results: list[dict] = []
        for plate, confs in self._votes.items():
            if len(confs) < self._min_votes:
                continue
            avg_conf = sum(confs) / len(confs)
            vote_freq = len(confs) / max(1, self._frame_count)
            # Cap frequency contribution at 1.0 (reached when seen in ≥1/3 of frames)
            freq_score = min(vote_freq * 3.0, 1.0)
            score = avg_conf * 0.7 + freq_score * 0.3
            results.append(
                {
                    "plate": plate,
                    "confidence": round(score, 3),
                    "votes": len(confs),
                    "frames_seen": self._frame_count,
                    "avg_confidence": round(avg_conf, 3),
                }
            )
        return sorted(results, key=lambda x: x["confidence"], reverse=True)
