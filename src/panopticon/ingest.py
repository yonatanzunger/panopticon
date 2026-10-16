"""Stage 0: load Google Takeout "My Activity" JSON into Events."""

import html
import json
import re
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import NamedTuple
from urllib.parse import parse_qs, urlparse

from panopticon.events import Event, Kind


class Item(NamedTuple):
    """One logical activity before id assignment. An AI Mode conversation yields several."""

    ts: datetime
    product: str
    kind: Kind
    text: str
    url: str | None
    source: str | None
    lat: float | None
    lon: float | None
    loc_source: str | None
    loc_precise: bool | None


# Order matters: longer prefixes that share a verb must come first.
_PREFIXES: list[tuple[str, Kind]] = [
    ("Searched with an image", Kind.IMAGE_SEARCH),
    ("Searched for ", Kind.SEARCH),
    ("Visited ", Kind.VISIT),
    ("Opened ", Kind.VISIT),
    ("Used ", Kind.APP_USE),
    ("Selected ", Kind.APP_USE),
    ("Viewed ", Kind.PLACE_VIEW),
]
_NOTIFICATIONS = re.compile(r"^\d+ notifications?$")
_MAP_CENTER = re.compile(r"center=(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)")
_PLAY_STORE_SUFFIX = " - Apps on Google Play"
_AI_PROMPT = re.compile(r"<strong>Your prompt:</strong><br>\s*(.*?)</p>", re.DOTALL)
_TAG = re.compile(r"<[^>]+>")
_BR = re.compile(r"<br\s*/?>|</?p>", re.IGNORECASE)
_FROM_ADS = "From Google Ads"
_LOC_SOURCES = {
    "From your device": "device",
    "From your places (Home)": "home",
    "From your places (Work)": "work",
    "Based on your past activity": "past activity",
    "Based on your Location History": "location history",
}


def _flatten(text: str) -> str:
    # Multi-line queries are almost always pasted fragments.
    return re.sub(r"\s*\n\s*", " ", text).strip()


def classify(title: str) -> tuple[Kind, str]:
    """Split a raw activity title into its kind and payload."""
    title = _flatten(title)
    for prefix, kind in _PREFIXES:
        if title.startswith(prefix):
            text = title.removeprefix(prefix).strip()
            if kind == Kind.APP_USE:
                text = text.removesuffix(_PLAY_STORE_SUFFIX)
            return kind, text
    if _NOTIFICATIONS.match(title):
        return Kind.NOTIFICATIONS, ""
    return Kind.OTHER, title


def unwrap_url(url: str | None) -> str | None:
    """Strip Google's https://www.google.com/url?q=<target> redirector."""
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.hostname == "www.google.com" and parsed.path == "/url":
        if target := parse_qs(parsed.query).get("q"):
            return target[0]
    return url


def _location(row: dict) -> tuple[float | None, float | None, str | None, bool | None]:
    for info in row.get("locationInfos", []):
        if m := _MAP_CENTER.search(info.get("url", "")):
            return (
                float(m.group(1)),
                float(m.group(2)),
                _LOC_SOURCES.get(info.get("source", ""), info.get("source")),
                info.get("name") == "At this place",
            )
    return None, None, None, None


def _ai_prompts(row: dict) -> list[str]:
    """All of the user's prompts in an AI Mode conversation, dropping Google's responses."""
    prompts: list[str] = []
    for item in row.get("safeHtmlItem", []):
        for raw in _AI_PROMPT.findall(item.get("html", "")):
            text = _TAG.sub("", _BR.sub(" ", raw))
            prompts.append(re.sub(r"\s+", " ", html.unescape(text)).strip())
    return prompts


def parse_row(row: dict) -> Iterator[Item]:
    product: str = row["products"][0]
    kind, text = classify(row["title"])
    details = {d["name"] for d in row.get("details", [])}
    url = unwrap_url(row.get("titleUrl"))

    if kind == Kind.NOTIFICATIONS:
        # Subtitles are "Including topics:", then "<topic> - <dismissed|clicked>", ...
        text = "; ".join(s["name"] for s in row.get("subtitles", [])[1:])
    if kind == Kind.VISIT and product == "Search" and _FROM_ADS in details:
        kind = Kind.AD_CLICK
    if kind in (Kind.SEARCH, Kind.IMAGE_SEARCH, Kind.NOTIFICATIONS):
        # The search-results URL just repeats the query.
        url = None
    if kind == Kind.VISIT and url is None and text.startswith(("http://", "https://")):
        url = text
    # In the Search product the header is always "Search"; in Ads it's the site or app.
    source = row.get("header") if row.get("header") not in (None, product) else None
    if source:
        source = source.removesuffix(_PLAY_STORE_SUFFIX)

    item = Item(
        datetime.fromisoformat(row["time"]), product, kind, text, url, source, *_location(row)
    )
    if product == "AI Mode" and kind == Kind.SEARCH:
        # The title repeats the first prompt; each follow-up becomes its own event.
        prompts = _ai_prompts(row) or [text]
        for prompt in prompts:
            yield item._replace(text=prompt)
    else:
        yield item


def find_activity_files(path: Path) -> list[Path]:
    """Accept a Takeout root, its "My Activity" folder, or a single MyActivity.json."""
    if path.is_file():
        return [path]
    return sorted(path.rglob("MyActivity.json"))


def load(
    paths: list[Path], since: datetime | None = None, until: datetime | None = None
) -> list[Event]:
    """Load and normalize activity into chronologically ordered Events, keeping those in
    [since, until). Ids are assigned over the full history before filtering, so an event has
    the same id no matter what range is selected."""
    items: list[Item] = []
    for path in paths:
        for file in find_activity_files(path):
            for row in json.loads(file.read_text(encoding="utf8")):
                items.extend(parse_row(row))
    # Sorting is stable, so AI Mode follow-ups sharing a timestamp keep their order.
    items.sort(key=lambda item: item.ts)
    return [
        Event(i, *item)
        for i, item in enumerate(items)
        if (since is None or item.ts >= since) and (until is None or item.ts < until)
    ]
