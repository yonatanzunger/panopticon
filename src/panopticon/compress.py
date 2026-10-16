"""Stage 1: drop noise, group events into sessions, and render a compact transcript for the LLM."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta, tzinfo
from typing import NamedTuple
from urllib.parse import parse_qsl, urlencode, urlparse

from panopticon.events import Event, Kind

# Ad-serving and tracking hosts that show up as "Visited" rows but say nothing about the user.
AD_TECH_DOMAINS = frozenset(
    {
        "doubleclick.net",
        "googlesyndication.com",
        "googleadservices.com",
        "imasdk.googleapis.com",
        "criteo.com",
        "outbrain.com",
        "taboola.com",
        "taboola-display.com",
        "dianomi.com",
        "clinch.co",
        "indexww.com",
        "simpli.fi",
        "amazon-adsystem.com",
        "padsquad.com",
        "connatix.com",
    }
)

# "Used Ads" and the like name the Takeout product, not a real app.
_NON_APPS = frozenset({"Ads", "Search"})

SESSION_GAP = timedelta(minutes=30)

_KIND_CODES = {
    Kind.SEARCH: "S",
    Kind.IMAGE_SEARCH: "I",
    Kind.VISIT: "V",
    Kind.AD_CLICK: "$",
    Kind.APP_USE: "A",
    Kind.PLACE_VIEW: "P",
    Kind.NOTIFICATIONS: "N",
    Kind.OTHER: "?",
}


def _host_matches(url: str, domains: frozenset[str]) -> bool:
    host = urlparse(url).hostname or ""
    parts = host.split(".")
    return any(".".join(parts[i:]) in domains for i in range(len(parts)))


def is_noise(event: Event) -> bool:
    if event.kind == Kind.VISIT and event.url:
        if _host_matches(event.url, AD_TECH_DOMAINS):
            return True
        # Android deep links into apps; the app itself shows up separately as app use.
        if event.url.startswith("intent:") or event.text.startswith("intent:"):
            return True
    if event.kind == Kind.APP_USE and event.text in _NON_APPS:
        return True
    return False


class Line(NamedTuple):
    """One transcript line: the first occurrence of an item within a session, plus a count."""

    event: Event
    count: int


class Session(NamedTuple):
    start: datetime
    end: datetime
    lines: list[Line]


def sessionize(events: list[Event], gap: timedelta = SESSION_GAP) -> list[Session]:
    """Group chronological events into sessions, deduplicating repeated items within each."""
    sessions: list[Session] = []
    current: list[Event] = []

    def flush() -> None:
        if not current:
            return
        lines: dict[tuple[str, Kind, str], Line] = {}
        for e in current:
            key = (e.product, e.kind, e.text)
            prior = lines.get(key)
            lines[key] = Line(prior.event, prior.count + 1) if prior else Line(e, 1)
        sessions.append(Session(current[0].ts, current[-1].ts, list(lines.values())))

    for event in events:
        if current and event.ts - current[-1].ts > gap:
            flush()
            current = []
        current.append(event)
    flush()
    return sessions


# Query parameters that track campaigns and referrals. Everything else is kept, because real
# parameters can be very revealing (a hotel search URL carries destination and dates).
_TRACKING_PARAMS = frozenset(
    {"gclid", "gbraid", "wbraid", "fbclid", "srsltid", "clickref", "ref_id", "my_ad", "semdtl",
     "taid", "link_source", "cmp", "brandcid", "mkt_tok", "igshid", "si"}
)
_TRACKING_PREFIXES = ("utm_", "gad_", "aff", "tw_", "mc_", "_hs", "pk_")
MAX_URL = 200


def clean_url(url: str) -> str:
    """Drop tracking parameters and empty values, and cap the length."""
    parsed = urlparse(url)
    params = [
        (k, v)
        for k, v in parse_qsl(parsed.query)
        if v and k not in _TRACKING_PARAMS and not k.startswith(_TRACKING_PREFIXES)
    ]
    cleaned = parsed._replace(query=urlencode(params), fragment="").geturl()
    return cleaned if len(cleaned) <= MAX_URL else cleaned[: MAX_URL - 1] + "…"


def _display(event: Event) -> str:
    if event.text.startswith(("http://", "https://")):
        return clean_url(event.text)
    return event.text


def _site(event: Event) -> str:
    """The site a visit was on, unless the title already is the URL."""
    if event.kind not in (Kind.VISIT, Kind.AD_CLICK) or not event.url:
        return ""
    if event.text.startswith(("http://", "https://")):
        return ""
    host = (urlparse(event.url).hostname or "").removeprefix("www.")
    return f" @{host}" if host else ""


def chunk_sessions(
    sessions: list[Session],
    max_chars: int,
    tz_at: Callable[[datetime], tzinfo] = lambda _: UTC,
) -> list[list[Session]]:
    """Split sessions into chunks whose rendered size stays under max_chars, breaking only at
    local day boundaries so that a day's context is never split. A single day larger than
    max_chars becomes its own oversized chunk rather than being cut."""
    days: list[list[Session]] = []
    for session in sessions:
        day = session.start.astimezone(tz_at(session.start)).date()
        if days and days[-1][0].start.astimezone(tz_at(days[-1][0].start)).date() == day:
            days[-1].append(session)
        else:
            days.append([session])

    chunks: list[list[Session]] = []
    size = 0
    for day_sessions in days:
        day_size = len(render(day_sessions, tz_at))
        if chunks and size + day_size <= max_chars:
            chunks[-1].extend(day_sessions)
            size += day_size
        else:
            chunks.append(list(day_sessions))
            size = day_size
    return chunks


def _loc(event: Event) -> str:
    """Google's recorded location for the event, to about 1km, and how it was determined."""
    if event.lat is None or event.lon is None:
        return ""
    source = f" {event.loc_source}" if event.loc_source else ""
    return f" [loc {event.lat:.2f},{event.lon:.2f}{source}]"


def _code(event: Event) -> str:
    code = _KIND_CODES[event.kind]
    return code + "*" if event.product == "AI Mode" else code


def render(
    sessions: list[Session], tz_at: Callable[[datetime], tzinfo] = lambda _: UTC
) -> str:
    """Render sessions as a compact transcript, localizing each session with tz_at(start).

    Format: a header line per local day (and whenever the zone changes), then one line per item:
        #<event id> HH:MM <kind code> <text> [@site] [×count] [[loc lat,lon source]]
    Kind codes: S search (S* = AI Mode), I image search, V visit, $ ad click, A app use,
    P place card, N notifications. Visits show their site after the title, as "<title> @site".
    """
    out: list[str] = []
    header: tuple[str, str] | None = None
    for session in sessions:
        tz = tz_at(session.start)
        day = session.start.astimezone(tz).strftime("%Y-%m-%d %a")
        if (day, str(tz)) != header:
            header = (day, str(tz))
            out.append(f"\n## {day} [{tz}]")
        out.append("--")
        for line in session.lines:
            e = line.event
            local = e.ts.astimezone(tz).strftime("%H:%M")
            count = f" ×{line.count}" if line.count > 1 else ""
            out.append(f"#{e.id} {local} {_code(e)} {_display(e)}{_site(e)}{count}{_loc(e)}")
    return "\n".join(out).lstrip() + "\n"
