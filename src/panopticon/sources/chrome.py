"""Google Takeout "Chrome": the synced browsing history in Chrome/History.json.

History.json holds a year or so of page visits across every device signed in to Chrome sync,
each with its URL, page title, time in microseconds, and the syncing client's id. That id is
the `cache_guid` of a device in Device Information.json, next to it, which gives the device's
form factor and name; history from a device no longer synced has no entry there.

Google searches made in Chrome also appear in My Activity, which has them with locations, so
they come out as searches here and `ingest` merges the copies. Likewise YouTube videos come
out as watches and YouTube searches as searches, to merge with the YouTube export's.
Takeout's other Chrome files (bookmarks, reading list, extensions, settings, autofill) aren't
activity and aren't read.
"""

import re
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from panopticon.events import Kind
from panopticon.sources import Item, Source, read_json, youtube_video_url

_DEVICES = "Device Information.json"
_FORM_FACTORS = {
    "DEVICE_FORM_FACTOR_DESKTOP": "desktop",
    "DEVICE_FORM_FACTOR_PHONE": "phone",
    "DEVICE_FORM_FACTOR_TABLET": "tablet",
}
_YOUTUBE_SEARCH_HOSTS = {"www.youtube.com", "m.youtube.com", "music.youtube.com"}
# Unread counts change on every visit, so they would defeat deduplication within a session:
# "(3) Facebook", "Inbox (1,234) - ... - Gmail".
_LEADING_COUNT = re.compile(r"^\(\d[\d,]*\+?\)\s*")
_INBOX_COUNT = re.compile(r"^(Inbox|All Mail) \(\d[\d,]*\)")


def claims(path: Path) -> bool:
    return path.name == "History.json" and path.parent.name == "Chrome"


def devices(path: Path) -> dict[str, str]:
    """Client id → "<form factor>: <name>", from the Device Information.json beside path."""
    info = path.with_name(_DEVICES)
    if not info.exists():
        return {}
    out: dict[str, str] = {}
    for device in read_json(info).get("Device Info", []):
        form = _FORM_FACTORS.get(device.get("device_form_factor", ""), "device")
        name = device.get("client_name") or device.get("model") or "unnamed"
        out[device["cache_guid"]] = f"{form}: {name}"
    return out


def clean_title(title: str) -> str:
    return _INBOX_COUNT.sub(r"\1", _LEADING_COUNT.sub("", title.strip()))


def classify_page(url: str, title: str) -> tuple[Kind, str, str | None] | None:
    """What a page load in the browser was: (kind, text, url), or None if it isn't activity.
    Also used for My Activity's Chrome rows, which are the same page loads."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return None  # chrome-extension:, chrome:, file:, ...
    kind, text, item_url = Kind.VISIT, clean_title(title) or url, url
    if parsed.hostname == "www.google.com":
        query = parse_qs(parsed.query)
        if parsed.path == "/url":
            return None  # the redirector hop; the destination is its own visit
        if parsed.path == "/search" and (q := query.get("q")):
            # Like My Activity: the results URL just repeats the query.
            kind, text, item_url = Kind.SEARCH, " ".join(q[0].split()), None
    elif video := youtube_video_url(url):
        kind, item_url = Kind.WATCH, video
    elif parsed.hostname in _YOUTUBE_SEARCH_HOSTS and parsed.path == "/results":
        if q := parse_qs(parsed.query).get("search_query"):
            # The URL stays, so ingest can tell a YouTube search from a Google one.
            kind, text = Kind.SEARCH, " ".join(q[0].split())
    return kind, text, item_url


def parse_visit(row: dict, device: str | None) -> Item | None:
    if (page := classify_page(row["url"], row.get("title", ""))) is None:
        return None
    ts = datetime.fromtimestamp(row["time_usec"] / 1e6, UTC)
    return Item(ts, "Chrome", *page, None, None, None, None, None, device)


def parse(path: Path) -> Iterator[Item]:
    names = devices(path)
    for row in read_json(path).get("Browser History", []):
        if item := parse_visit(row, names.get(row.get("client_id", ""), "unknown device")):
            yield item


SOURCE = Source("chrome", claims, parse)
