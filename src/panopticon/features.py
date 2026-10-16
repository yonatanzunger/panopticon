"""Stage 2: deterministic features (location fixes, home, daily rhythm, app usage). No LLM."""

import statistics
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from functools import cache
from typing import NamedTuple
from zoneinfo import ZoneInfo

from timezonefinder import TimezoneFinder

from panopticon.events import Event, Kind


@cache
def _finder() -> TimezoneFinder:
    return TimezoneFinder()


def timezone_at(lat: float, lon: float) -> str | None:
    """The IANA zone name at a coordinate, from an offline boundary database."""
    return _finder().timezone_at(lat=lat, lng=lon)


class LocationFix(NamedTuple):
    event_id: int
    ts: datetime
    lat: float
    lon: float
    tz: str | None
    source: str | None


def location_fixes(events: list[Event]) -> list[LocationFix]:
    return [
        LocationFix(e.id, e.ts, e.lat, e.lon, timezone_at(e.lat, e.lon), e.loc_source)
        for e in events
        if e.lat is not None and e.lon is not None
    ]


class Home(NamedTuple):
    lat: float
    lon: float
    tz: str
    # Number of distinct days with a fix in this cell, and the total days with any fix.
    days: int
    of_days: int


def home(fixes: list[LocationFix], precision: int = 2) -> Home | None:
    """The ~1km cell with fixes on the most distinct days. Days, not fixes, so one busy
    afternoon somewhere doesn't outweigh weeks of ordinary life. If Google has labeled any
    fixes as the user's saved Home, only those are considered."""
    labeled = [fix for fix in fixes if fix.source == "home"]
    days_by_cell: dict[tuple[float, float], set[date]] = defaultdict(set)
    tz_by_cell: dict[tuple[float, float], str | None] = {}
    for fix in labeled or fixes:
        cell = (round(fix.lat, precision), round(fix.lon, precision))
        days_by_cell[cell].add(fix.ts.date())
        tz_by_cell[cell] = fix.tz
    if not days_by_cell:
        return None
    cell, days = max(days_by_cell.items(), key=lambda kv: len(kv[1]))
    all_days = {fix.ts.date() for fix in fixes}
    return Home(cell[0], cell[1], tz_by_cell[cell] or "UTC", len(days), len(all_days))


class QuietPeriod(NamedTuple):
    """The longest gap in activity ending on a given local day; a proxy for sleep."""

    day: date
    start: datetime
    end: datetime
    # Midpoint as an hour of the UTC day, and its signed deviation (hours) from the typical
    # midpoint. A sustained deviation of N suggests the user moved about N hours east (-N) or
    # west (+N) of their usual zone.
    midpoint_utc: float
    deviation: float


MIN_QUIET = timedelta(hours=3)


def _circular_median(hours: list[float]) -> float:
    # Rotate so the largest empty arc of the clock sits at the wrap point, then take the median.
    pts = sorted(h % 24 for h in hours)
    gaps = [((pts[(i + 1) % len(pts)] - pts[i]) % 24, i) for i in range(len(pts))]
    _, i = max(gaps)
    pivot = pts[(i + 1) % len(pts)]
    return (statistics.median((h - pivot) % 24 for h in pts) + pivot) % 24


def daily_rhythm(events: list[Event], tz: ZoneInfo) -> list[QuietPeriod]:
    """For each day (in tz), the longest inter-event gap of at least MIN_QUIET ending that day."""
    longest: dict[date, tuple[datetime, datetime]] = {}
    for prev, cur in zip(events, events[1:]):
        if cur.ts - prev.ts < MIN_QUIET:
            continue
        day = cur.ts.astimezone(tz).date()
        if day not in longest or cur.ts - prev.ts > longest[day][1] - longest[day][0]:
            longest[day] = (prev.ts, cur.ts)
    if not longest:
        return []

    def midpoint(start: datetime, end: datetime) -> float:
        mid = start + (end - start) / 2
        return mid.hour + mid.minute / 60

    mids = {day: midpoint(*span) for day, span in longest.items()}
    typical = _circular_median(list(mids.values()))
    return [
        QuietPeriod(day, start, end, mids[day], ((mids[day] - typical + 12) % 24) - 12)
        for day, (start, end) in sorted(longest.items())
    ]


def app_usage(events: list[Event]) -> Counter[str]:
    return Counter(e.text for e in events if e.kind == Kind.APP_USE)


def hour_histogram(events: list[Event], tz: ZoneInfo) -> list[int]:
    counts = Counter(e.ts.astimezone(tz).hour for e in events)
    return [counts[h] for h in range(24)]
