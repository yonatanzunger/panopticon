from datetime import UTC, datetime, timedelta

from panopticon.events import Event, Kind
from panopticon.features import Home
from panopticon.itinerary import Localizer, build, evidence_from_atoms

T0 = datetime(2026, 8, 1, tzinfo=UTC)
HOME = Home(37.38, -122.10, "America/Los_Angeles", 30, 30)
LIMA = (-12.05, -77.04)


def ev(i: int, hours: float, lat: float | None = None, lon: float | None = None) -> Event:
    return Event(i, T0 + timedelta(hours=hours), "Search", Kind.SEARCH, f"q{i}", None, None,
                 lat, lon, "device" if lat is not None else None)


def atom(ids, relation="present", strength="strong", place=LIMA, starts=None, ends=None):
    return {
        "event_ids": list(ids), "place": "Lima, Peru", "lat": place[0], "lon": place[1],
        "tz": "America/Lima", "relation": relation, "strength": strength,
        "starts": starts, "ends": ends,
    }


def trips(atoms, events):
    segments, _ = build(atoms, events, HOME)
    return [(s.place.label, s.start, s.end) for s in segments if not s.place.is_home]


def background(n_days: int = 20) -> list[Event]:
    # Activity at home every 3 hours, so the span is covered.
    return [ev(i, h) for i, h in enumerate(range(0, n_days * 24, 3))]


def test_no_evidence_means_home():
    assert trips([], background()) == []


def test_single_weak_signal_does_not_make_a_trip():
    events = background()
    assert trips([atom([5], strength="weak")], events) == []
    assert trips([atom([5], relation="remote_interest")], events) == []


def test_sustained_presence_makes_a_trip():
    events = background()
    # Present in Lima at events spread over days 5-8.
    lima = [e.id for e in events if 5 * 24 <= (e.ts - T0).total_seconds() / 3600 < 8 * 24]
    result = trips([atom([i]) for i in lima[::4]], events)
    assert len(result) == 1
    label, start, end = result[0]
    assert label == "Lima, Peru"
    assert T0 + timedelta(days=5) <= start <= T0 + timedelta(days=5, hours=1)
    assert T0 + timedelta(days=7, hours=12) <= end <= T0 + timedelta(days=8, hours=1)


def test_location_fixes_alone_make_a_trip():
    events = background() + [ev(1000 + k, 24 * 10 + k, *LIMA) for k in range(0, 48, 6)]
    events.sort(key=lambda e: e.ts)
    result = trips([], events)
    assert len(result) == 1
    assert result[0][0].startswith("near -12.05")


def test_dated_plan_covers_its_days():
    events = background()
    plan = atom([0], relation="planning", starts="2026-08-10", ends="2026-08-14")
    result = trips([plan, atom([80])], events)  # plus one strong sighting on day 10
    assert len(result) == 1
    _, start, end = result[0]
    assert start.date() == datetime(2026, 8, 10).date()
    assert end.date() in (datetime(2026, 8, 14).date(), datetime(2026, 8, 13).date())


def test_lookup_before_travel_date_is_not_presence():
    events = background()
    by_id = {e.id: e for e in events}
    # Event 8 is on Aug 2; the flight it looks up is on Aug 5.
    evidence = evidence_from_atoms(
        [atom([8], relation="in_transit", starts="2026-08-05")], by_id
    )
    assert [e.kind for e in evidence] == ["plan"]


def test_nearby_places_merge_into_one_trip_away_from_home():
    events = background()
    denver, boulder, airport = (39.74, -104.99), (40.01, -105.27), (39.86, -104.67)
    by_day = {d: next(e.id for e in events if (e.ts - T0).days == d) for d in range(20)}
    atoms = [
        atom([by_day[5]], place=denver, strength="moderate"),
        atom([by_day[6]], place=boulder, strength="moderate"),
        atom([by_day[7]], place=airport, strength="moderate"),
    ]
    # Each alone is too weak for a trip; as one metro area, bridged, they make one.
    assert len(trips(atoms[:1], events)) == 0
    assert len(trips(atoms, events)) == 1


def test_home_cluster_stays_tight():
    events = background()
    san_francisco = (37.78, -122.44)  # ~50km from HOME
    sf = [e.id for e in events if 24 * 3 <= (e.ts - T0).total_seconds() / 3600 < 24 * 3 + 9]
    segments, places = build([atom([i], place=san_francisco) for i in sf], events, HOME)
    assert len(places) == 2
    assert [s.place.is_home for s in segments] == [True, False, True]


def test_contrary_evidence_breaks_a_bridge():
    events = background()
    day = lambda d: next(e.id for e in events if (e.ts - T0).days == d)  # noqa: E731
    away = [atom([day(5)]), atom([day(5)]), atom([day(8)]), atom([day(8)])]
    home_fixes = [ev(2000 + k, 24 * 6 + 12 + k, HOME.lat, HOME.lon) for k in range(0, 24, 4)]
    with_fixes = sorted(events + home_fixes, key=lambda e: e.ts)
    assert len(trips(away, events)) == 1
    assert len(trips(away, with_fixes)) == 2


def test_localizer():
    events = background()
    lima = [e.id for e in events if 5 * 24 <= (e.ts - T0).total_seconds() / 3600 < 8 * 24]
    segments, _ = build([atom([i]) for i in lima[::4]], events, HOME)
    at = Localizer(segments, default="America/Los_Angeles")
    assert str(at(T0 + timedelta(days=6))) == "America/Lima"
    assert str(at(T0 + timedelta(days=2))) == "America/Los_Angeles"
    assert str(at(T0 - timedelta(days=30))) == "America/Los_Angeles"
