from datetime import UTC, datetime, timedelta

from panopticon.events import Event, Kind
from panopticon.extract import Chunk, Extraction, validate
from panopticon.features import Home
from panopticon.itinerary import Localizer
from panopticon.model import Trip
from panopticon.synthesize import ClaimDraft, Profile, build_inputs, resolve, to_markdown

T0 = datetime(2026, 8, 1, 18, tzinfo=UTC)
HOME = Home(37.38, -122.10, "America/Los_Angeles", 30, 30)
LA = Localizer([], default="America/Los_Angeles")


def ev(i: int, days: float, kind: Kind = Kind.SEARCH, text: str = "q", url=None) -> Event:
    return Event(i, T0 + timedelta(days=days), "Search", kind, text, url, None, None, None)


EVENTS = [
    ev(0, 0, text="propranolol dosage"),
    ev(1, 9, text="propranolol dosage"),
    ev(2, 10, Kind.APP_USE, "Spotify"),
    ev(3, 10, Kind.VISIT, "SMBC", "https://www.smbc-comics.com/"),
]
FACT = {
    "id": "F0", "event_ids": [0, 1], "category": "health",
    "observation": "Searched propranolol dosage twice.",
    "inference": "May take propranolol.", "about": "self", "strength": "moderate",
    "sensitivity": ["health"],
}
TRIP = Trip(start="2026-08-14T07:00:00+00:00", end="2026-08-21T05:00:00+00:00",
            place="Lima, Peru", event_ids=[2], tz="America/Lima", is_home=False)


def test_validate_drops_ids_outside_the_chunk():
    chunk = Chunk(0, T0, T0, "", frozenset({0, 1}))
    extraction = Extraction.model_validate({
        "places": [],
        "facts": [
            {k: v for k, v in FACT.items() if k != "id"} | {"event_ids": [0, 1, 99]},
            {k: v for k, v in FACT.items() if k != "id"} | {"event_ids": [98]},
        ],
    })
    places, facts, dropped = validate(extraction, chunk)
    assert places == []
    assert [f["event_ids"] for f in facts] == [[0, 1]]
    assert facts[0]["chunk"] == 0
    assert dropped == 1


def test_build_inputs():
    inputs = build_inputs([FACT], [TRIP], EVENTS, HOME, LA)
    assert inputs.fact_ids == {"F0"}
    assert inputs.trip_ids == {"T0"}
    assert "T0 2026-08-14 to 2026-08-21: Lima, Peru" in inputs.text
    assert (
        "F0 2026-08-01..2026-08-10 [health; about self; moderate; sensitive: health] "
        "Searched propranolol dosage twice. => May take propranolol." in inputs.text
    )
    assert "most used apps: Spotify (1)" in inputs.text
    assert "most visited sites: smbc-comics.com (1)" in inputs.text


def claim(evidence: list[str]) -> ClaimDraft:
    return ClaimDraft(
        category="health", claim="Takes propranolol.", detail="Searched dosage twice.",
        confidence="medium", about="self", sensitivity=["health"], evidence=evidence,
        alternatives=["Researching it for someone else."],
    )


def test_resolve_attaches_events_and_flags_bad_citations():
    inputs = build_inputs([FACT], [TRIP], EVENTS, HOME, LA)
    profile = Profile(summary="s", claims=[claim(["F0", "T0", "F7"])])
    (resolved,) = resolve(profile, inputs, [FACT], [TRIP], EVENTS, LA)
    assert resolved.evidence == ["F0", "T0"]
    assert resolved.unresolved_evidence == ["F7"]
    assert resolved.event_ids == [0, 1, 2]
    assert (resolved.first_seen, resolved.last_seen) == ("2026-08-01", "2026-08-11")


def test_markdown():
    inputs = build_inputs([FACT], [], EVENTS, HOME, LA)
    claims = resolve(Profile(summary="A portrait.", claims=[claim(["F0"])]),
                     inputs, [FACT], [], EVENTS, LA)
    md = to_markdown("A portrait.", claims)
    assert "## Health" in md
    assert "- **Takes propranolol.** _(medium; sensitive: health)_" in md
    assert "Evidence: F0 (2026-08-01 – 2026-08-10)" in md
