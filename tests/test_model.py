import pytest
from pydantic import ValidationError

from panopticon.model import Claim, Trip

SYNTH = {
    "category": "health", "claim": "Takes propranolol.", "detail": "Searched dosage.",
    "confidence": "high", "about": "self", "sensitivity": ["health"],
    "alternatives": ["For a relative."], "evidence": ["F0"], "unresolved_evidence": [],
    "event_ids": [0, 1], "first_seen": "2026-08-01", "last_seen": "2026-08-10",
}


def test_final_fields_default_to_base_when_unverified():
    c = Claim.from_dict(SYNTH)
    assert c.status == "unverified"
    assert c.final_claim == c.claim
    assert c.final_confidence == c.confidence
    assert c.final_about == c.about
    assert c.final_sensitivity == c.sensitivity
    assert c.verification is None


def test_profile_dict_omits_verification_overlay():
    assert set(Claim.from_dict(SYNTH).to_profile_dict()) == set(SYNTH)


def test_profile_dict_round_trips_byte_identical():
    assert Claim.from_dict(SYNTH).to_profile_dict() == SYNTH


def test_verified_dict_adds_overlay():
    verified = SYNTH | {
        "status": "revised", "verification": {"verdict": "partially_supported"},
        "final_claim": "Looked up propranolol.", "final_confidence": "low",
        "final_about": "unclear", "final_sensitivity": ["health"],
    }
    c = Claim.from_dict(verified)
    assert c.final_confidence == "low"
    assert c.to_verified_dict() == verified


def test_legacy_dropped_status_becomes_unconfirmed():
    c = Claim.from_dict(SYNTH | {"status": "dropped"})
    assert c.status == "unconfirmed"


def test_unknown_field_is_rejected():
    with pytest.raises(ValidationError):
        Claim.from_dict(SYNTH | {"surprise": 1})


def test_trip_parses_times_and_filters_home():
    segments = [
        {"start": "2026-08-14T07:00:00+00:00", "end": "2026-08-21T05:00:00+00:00",
         "place": "Lima, Peru", "lat": -12.0, "lon": -77.0, "tz": "America/Lima",
         "is_home": False, "weight": 3.0, "event_ids": [2]},
        {"start": "2026-09-01T00:00:00+00:00", "end": "2026-09-02T00:00:00+00:00",
         "place": "home", "lat": 37.4, "lon": -122.1, "tz": "America/Los_Angeles",
         "is_home": True, "weight": 9.0, "event_ids": [5]},
    ]
    trips = Trip.from_segments(segments)
    assert [t.place for t in trips] == ["Lima, Peru"]
    assert trips[0].start_dt.year == 2026
    assert str(trips[0].zone) == "America/Lima"
