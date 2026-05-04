"""
known_vehicle_matcher.py — configurable known-vehicle ignore matching.

This module is deliberately pure-Python and detector-agnostic.  It accepts
vehicle/ALPR detection dictionaries from whatever Phase 2 vision pipeline emits
and compares them against user-defined known vehicle profiles from config.yaml.

The goal is to suppress alerts for household vehicles only when multiple traits
match (for example: black Honda CR-V in the driveway), not to broadly ignore all
black SUVs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple


ALIASES = {
    "crv": "cr-v",
    "cr v": "cr-v",
    "sport utility vehicle": "suv",
    "crossover": "suv",
}


TRAIT_WEIGHTS = {
    "make": 0.25,
    "model": 0.30,
    "color": 0.20,
    "vehicle_type": 0.15,
    "zone": 0.10,
    "plate_fragment": 0.20,
}


def _norm(value: Any) -> str:
    text = str(value or "").strip().lower().replace("_", "-")
    text = " ".join(text.split())
    return ALIASES.get(text, text)


def _first(detection: Dict[str, Any], *keys: str) -> Any:
    """Return the first present value from top-level or nested vehicle dicts."""
    vehicle = detection.get("vehicle") if isinstance(detection.get("vehicle"), dict) else {}
    for key in keys:
        if key in detection and detection[key] not in (None, ""):
            return detection[key]
        if key in vehicle and vehicle[key] not in (None, ""):
            return vehicle[key]
    return None


def _as_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [_norm(value)]
    if isinstance(value, Iterable):
        return [_norm(v) for v in value if v not in (None, "")]
    return [_norm(value)]


@dataclass(frozen=True)
class KnownVehicleProfile:
    name: str
    enabled: bool = True
    make: Optional[str] = None
    model: Optional[str] = None
    color: Optional[str] = None
    vehicle_type: Optional[str] = None
    expected_zones: List[str] = field(default_factory=list)
    plate_fragments: List[str] = field(default_factory=list)
    min_score: float = 0.75
    min_matched_traits: int = 3

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "KnownVehicleProfile":
        return cls(
            name=str(data.get("name") or data.get("id") or "known_vehicle"),
            enabled=bool(data.get("enabled", True)),
            make=data.get("make"),
            model=data.get("model"),
            color=data.get("color") or data.get("colour"),
            vehicle_type=data.get("vehicle_type") or data.get("type"),
            expected_zones=_as_list(data.get("expected_zones") or data.get("zones")),
            plate_fragments=[str(p).upper().strip() for p in (data.get("plate_fragments") or []) if str(p).strip()],
            min_score=float(data.get("min_score", data.get("confidence_threshold", 0.75))),
            min_matched_traits=int(data.get("min_matched_traits", 3)),
        )


class KnownVehicleMatcher:
    def __init__(self, profiles: Iterable[Dict[str, Any] | KnownVehicleProfile]):
        self.profiles = [
            p if isinstance(p, KnownVehicleProfile) else KnownVehicleProfile.from_dict(p)
            for p in profiles
        ]

    def match(self, detection: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Return the best matching known vehicle profile, or None."""
        best: Optional[Tuple[float, int, KnownVehicleProfile, List[str]]] = None

        for profile in self.profiles:
            if not profile.enabled:
                continue
            score, matched_traits = self._score(profile, detection)
            if score >= profile.min_score and len(matched_traits) >= profile.min_matched_traits:
                candidate = (score, len(matched_traits), profile, matched_traits)
                if best is None or candidate[:2] > best[:2]:
                    best = candidate

        if best is None:
            return None

        score, _, profile, matched_traits = best
        return {
            "name": profile.name,
            "score": round(score, 3),
            "matched_traits": matched_traits,
            "ignored": True,
        }

    def should_ignore(self, detection: Dict[str, Any]) -> bool:
        return self.match(detection) is not None

    def _score(self, profile: KnownVehicleProfile, detection: Dict[str, Any]) -> Tuple[float, List[str]]:
        matched: List[str] = []
        possible_weight = 0.0
        score = 0.0

        checks = [
            ("make", profile.make, _first(detection, "make")),
            ("model", profile.model, _first(detection, "model")),
            ("color", profile.color, _first(detection, "color", "colour")),
            ("vehicle_type", profile.vehicle_type, _first(detection, "vehicle_type", "type", "class")),
        ]

        for trait, expected, actual in checks:
            if expected in (None, ""):
                continue
            possible_weight += TRAIT_WEIGHTS[trait]
            if _norm(expected) == _norm(actual):
                score += TRAIT_WEIGHTS[trait]
                matched.append(trait)

        if profile.expected_zones:
            possible_weight += TRAIT_WEIGHTS["zone"]
            actual_zone = _norm(_first(detection, "zone", "parking_zone", "location_zone"))
            if actual_zone in profile.expected_zones:
                score += TRAIT_WEIGHTS["zone"]
                matched.append("zone")

        if profile.plate_fragments:
            possible_weight += TRAIT_WEIGHTS["plate_fragment"]
            plate = str(_first(detection, "plate", "partial_plate") or "").upper().strip()
            if plate and any(fragment in plate for fragment in profile.plate_fragments):
                score += TRAIT_WEIGHTS["plate_fragment"]
                matched.append("plate_fragment")

        if possible_weight <= 0:
            return 0.0, []

        return score / possible_weight, matched
