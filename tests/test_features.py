from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from panopticon.cli import select_range
from panopticon.events import Event, Kind
from panopticon.features import (
    LocationFix,
    _circular_median,
    daily_rhythm,
    home,
    timezone_at,
)


def test_timezone_at():
    assert timezone_at(37.383, -122.095) == "America/Los_Angeles"
    assert timezone_at(-12.12, -77.03) == "America/Lima"
    assert timezone_at(40.015, -105.27) == "America/Denver"


def fix(day: int, lat: float, lon: float, source: str = "device") -> LocationFix:
    return LocationFix(0, datetime(2026, 9, day, tzinfo=UTC), lat, lon, "Zone/Test", source)


def test_home_counts_days_not_fixes():
    fixes = [fix(1, 34.01, -118.50)] * 20 + [fix(d, 37.38, -122.10) for d in range(2, 6)]
    assert home(fixes)[:2] == (37.38, -122.10)


def test_home_prefers_labeled_home():
    fixes = [fix(d, 37.39, -122.07) for d in range(1, 10)] + [fix(10, 37.38, -122.10, "home")]
    assert home(fixes)[:2] == (37.38, -122.10)


def test_circular_median_wraps_midnight():
    assert _circular_median([23.0, 23.5, 0.5, 1.0, 1.5]) == 0.5


def ev(i: int, ts: datetime) -> Event:
    return Event(i, ts, "Search", Kind.SEARCH, "q", None, None, None, None)


def test_daily_rhythm_detects_eastward_shift():
    tz = ZoneInfo("America/Los_Angeles")
    events: list[Event] = []
    for day in range(1, 11):
        # Active 08:00-23:00 local; on days 8-10 the whole day shifts 3 hours earlier in UTC,
        # as it would for someone who has flown 3 hours east.
        shift = timedelta(hours=-3 if day >= 8 else 0)
        base = datetime(2026, 7, day, 8, tzinfo=tz) + shift
        events += [ev(len(events), base + timedelta(hours=h)) for h in range(0, 16)]
    rhythm = {q.day.day: round(q.deviation) for q in daily_rhythm(events, tz)}
    assert rhythm[5] == 0
    assert rhythm[9] == -3


def test_select_range_days_counts_back_from_export_end():
    events = [ev(i, datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=i)) for i in range(10)]
    assert [e.id for e in select_range(events, None, None, 3)] == [6, 7, 8, 9]
    assert select_range(events, None, None, None) == events
