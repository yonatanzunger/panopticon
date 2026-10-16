"""Typed domain objects passed between pipeline stages.

On disk these are JSON (`profile.json`, `verified.json`, `itinerary.json`); in memory they are
these objects, so code reads ``claim.final_confidence`` rather than
``claim.get("final_confidence", claim["confidence"])``. Load at the edges with ``from_dict`` /
``from_segments``; serialize with ``to_profile_dict`` / ``to_verified_dict``.

A ``Claim`` always has its ``final_*`` fields populated in memory — equal to the base value
until verification overrides them — so no consumer needs a "verified value, else base" fallback.
The on-disk shapes are unchanged: ``profile.json`` omits the verification overlay (write with
``to_profile_dict``); ``verified.json`` includes it (``to_verified_dict``).
"""

from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, model_validator

Confidence = Literal["high", "medium", "low"]
About = Literal["self", "household_member", "other_person", "unclear"]
# "unverified": not yet checked. "kept"/"revised"/"unconfirmed": verification's verdicts.
Status = Literal["unverified", "kept", "revised", "unconfirmed"]

_PROFILE_FIELDS = (
    "category", "claim", "detail", "confidence", "about", "sensitivity", "evidence",
    "alternatives", "unresolved_evidence", "event_ids", "first_seen", "last_seen",
)
_VERIFIED_EXTRA = (
    "verification", "status", "final_claim", "final_confidence", "final_about",
    "final_sensitivity",
)


class Claim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Synthesized.
    category: str
    claim: str
    detail: str
    confidence: Confidence
    about: About
    sensitivity: list[str] = []
    alternatives: list[str] = []
    # Added by synthesize.resolve: cited ids that exist, ids that didn't, source events, dates.
    evidence: list[str] = []
    unresolved_evidence: list[str] = []
    event_ids: list[int] = []
    first_seen: str | None = None
    last_seen: str | None = None
    # Added by verify.apply. final_* are the values to show; they default to the base value
    # and the model_validator fills them, so they are always populated in memory.
    status: Status = "unverified"
    verification: dict | None = None
    final_claim: str = ""
    final_confidence: Confidence | None = None
    final_about: About | None = None
    final_sensitivity: list[str] | None = None

    @model_validator(mode="after")
    def _fill_final(self) -> "Claim":
        if not self.final_claim:
            self.final_claim = self.claim
        if self.final_confidence is None:
            self.final_confidence = self.confidence
        if self.final_about is None:
            self.final_about = self.about
        if self.final_sensitivity is None:
            self.final_sensitivity = list(self.sensitivity)
        return self

    @classmethod
    def from_dict(cls, data: dict) -> "Claim":
        if data.get("status") == "dropped":  # the name "unconfirmed" had before the rename
            data = data | {"status": "unconfirmed"}
        return cls.model_validate(data)

    def to_profile_dict(self) -> dict:
        return {name: getattr(self, name) for name in _PROFILE_FIELDS}

    def to_verified_dict(self) -> dict:
        return self.to_profile_dict() | {name: getattr(self, name) for name in _VERIFIED_EXTRA}


class Trip(BaseModel):
    """A segment of the itinerary away from home. ``itinerary.json`` also holds home segments;
    ``from_segments`` keeps only the trips."""

    model_config = ConfigDict(extra="forbid")

    start: str
    end: str
    place: str
    lat: float = 0.0
    lon: float = 0.0
    tz: str
    is_home: bool = False
    weight: float = 0.0
    event_ids: list[int] = []

    @property
    def start_dt(self) -> datetime:
        return datetime.fromisoformat(self.start)

    @property
    def end_dt(self) -> datetime:
        return datetime.fromisoformat(self.end)

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.tz)

    @classmethod
    def from_segments(cls, segments: list[dict]) -> list["Trip"]:
        return [cls.model_validate(s) for s in segments if not s.get("is_home")]
