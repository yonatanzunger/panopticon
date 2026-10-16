from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from panopticon.compress import chunk_sessions, clean_url, is_noise, render, sessionize
from panopticon.events import Event, Kind

T0 = datetime(2026, 8, 14, 18, 0, tzinfo=UTC)


def ev(i: int, minutes: float, kind: Kind, text: str, url: str | None = None, **kw) -> Event:
    return Event(
        id=i,
        ts=T0 + timedelta(minutes=minutes),
        product=kw.get("product", "Search"),
        kind=kind,
        text=text,
        url=url,
        source=None,
        lat=kw.get("lat"),
        lon=kw.get("lon"),
        loc_source=kw.get("loc_source"),
    )


def test_noise():
    assert is_noise(ev(0, 0, Kind.VISIT, "x", "https://googleads.g.doubleclick.net/pagead"))
    assert is_noise(ev(0, 0, Kind.VISIT, "intent://maps", "intent://maps"))
    assert is_noise(ev(0, 0, Kind.APP_USE, "Ads"))
    assert not is_noise(ev(0, 0, Kind.VISIT, "x", "https://doubleclick.example.com/"))
    assert not is_noise(ev(0, 0, Kind.SEARCH, "criteo"))


def test_sessionize_splits_on_gaps_and_dedupes():
    events = [
        ev(0, 0, Kind.SEARCH, "aa 917"),
        ev(1, 1, Kind.APP_USE, "Sandy Jam"),
        ev(2, 2, Kind.SEARCH, "aa 917"),
        ev(3, 3, Kind.APP_USE, "Sandy Jam"),
        ev(4, 90, Kind.SEARCH, "aa 917"),
    ]
    first, second = sessionize(events)
    assert [(line.event.id, line.count) for line in first.lines] == [(0, 2), (1, 2)]
    assert [(line.event.id, line.count) for line in second.lines] == [(4, 1)]


def test_chunk_sessions_breaks_only_between_days():
    # Three days, two sessions each.
    events = [
        ev(i, day * 24 * 60 + offset, Kind.SEARCH, f"q{i}")
        for i, (day, offset) in enumerate((d, o) for d in range(3) for o in (0, 120))
    ]
    sessions = sessionize(events)
    one_day = len(render(sessions[:2]))
    chunks = chunk_sessions(sessions, max_chars=one_day * 2)
    assert [[line.event.id for s in c for line in s.lines] for c in chunks] == [
        [0, 1, 2, 3],
        [4, 5],
    ]
    # A budget smaller than one day still keeps each day whole.
    assert [len(c) for c in chunk_sessions(sessions, max_chars=1)] == [2, 2, 2]


def test_clean_url_keeps_meaningful_params():
    url = (
        "https://www.expedia.com/Hotel-Search?destination=Lima&startDate=2026-08-14"
        "&utm_source=x&gad_source=1&gclid=abc&affcid=US&theme=#top"
    )
    assert clean_url(url) == (
        "https://www.expedia.com/Hotel-Search?destination=Lima&startDate=2026-08-14"
    )


def test_render_localizes_and_labels():
    sessions = sessionize(
        [
            ev(7, 0, Kind.SEARCH, "weather lima"),
            ev(8, 1, Kind.SEARCH, "weather lima", product="AI Mode"),
            ev(9, 2, Kind.VISIT, "Caltrain: Home", "https://www.caltrain.com/"),
            ev(10, 3, Kind.VISIT, "https://x.example/?utm_source=y", "https://x.example/"),
            ev(11, 4, Kind.SEARCH, "uv index", lat=-12.1211, lon=-77.0297, loc_source="device"),
        ]
    )
    text = render(sessions, tz_at=lambda _: ZoneInfo("America/Lima"))
    assert text.splitlines() == [
        "## 2026-08-14 Fri [America/Lima]",
        "--",
        "#7 13:00 S weather lima",
        "#8 13:01 S* weather lima",
        "#9 13:02 V Caltrain: Home @caltrain.com",
        "#10 13:03 V https://x.example/",
        "#11 13:04 S uv index [loc -12.12,-77.03 device]",
    ]
