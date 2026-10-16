"""Stage 0: find the readable files in a Takeout export and merge them into Events."""

import bisect
from collections import defaultdict
from collections.abc import Collection
from datetime import datetime, timedelta
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlparse

from panopticon import apps, boilerplate, sources
from panopticon.events import Event, Kind
from panopticon.sources import Item, Source

# How far apart two sources' records of one activity can be. Measured on Chrome searches
# against My Activity: median 0.06s apart, 90th percentile 13s, max 8 minutes.
DUPLICATE_WINDOW = timedelta(minutes=10)


def discover(
    paths: list[Path], only: Collection[str] | None = None
) -> list[tuple[Source, Path]]:
    """Every file under the given paths that some source reads, each paired with that source,
    keeping only the sources named in `only` if given. A path can be a Takeout root, any
    folder inside one, or a single file; a file named directly must be one a source reads."""
    found: dict[Path, Source] = {}
    for path in paths:
        if not path.exists():
            raise FileNotFoundError(f"{path}: no such file or directory")
        if path.is_file():
            if (source := sources.claim(path)) is None:
                raise ValueError(f"{path}: not a kind of file panopticon can read")
            found[path.resolve()] = source
            continue
        for file in sorted(path.rglob("*")):
            if file.is_file() and (source := sources.claim(file)):
                found[file.resolve()] = source
    return [
        (source, file) for file, source in found.items() if only is None or source.name in only
    ]


# Which product's record of an activity to keep, whichever source it came from (then by
# source order): those with the most context first. Search has locations; Chrome has page titles; Ads and Google Analytics, which saw the
# page from a tracker, have little more than the URL. Unlisted products go in between.
_PRODUCT_PRIORITY = ("Search", "AI Mode", "Image Search", "Maps", "YouTube", "YouTube Music")
_PRODUCT_LAST = ("Chrome", "Ads", "Google Analytics")


def _product_rank(product: str) -> tuple[int, int]:
    if product in _PRODUCT_PRIORITY:
        return 0, _PRODUCT_PRIORITY.index(product)
    if product in _PRODUCT_LAST:
        return 2, _PRODUCT_LAST.index(product)
    return 1, 0


# Where a search with no URL was made, by product. Anything else is a Google search.
_SEARCH_ENGINES = {"YouTube": "youtube.com", "YouTube Music": "youtube.com"}


def _engine(item: Item) -> str:
    if item.url and (host := urlparse(item.url).hostname):
        return "youtube.com" if host.endswith("youtube.com") else host.removeprefix("www.")
    return _SEARCH_ENGINES.get(item.product, "google.com")


def _page(url: str) -> str:
    """A page, ignoring scheme, "www.", query, and fragment: Google Analytics records
    "http://site/path" for what Chrome recorded as "https://www.site/path?q=...", and Docs
    pages pick up "?tab=..." as they're used."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").removeprefix("www.")
    return host + (parsed.path.rstrip("/") or "")


def _identity(item: Item) -> tuple[Kind, str, str]:
    """What makes two records the same activity: a search's query on the same search engine;
    a page visit's page (an ad click is a visit to its landing page); else the URL (for a
    video, its canonical URL)."""
    if item.kind in (Kind.SEARCH, Kind.IMAGE_SEARCH) or not item.url:
        return item.kind, _engine(item), " ".join(item.text.lower().split())
    if item.kind in (Kind.VISIT, Kind.AD_CLICK):
        return Kind.VISIT, "", _page(item.url)
    return item.kind, "", item.url


# What a dropped copy can fill in on the record kept: facts about the same event. Not the
# source, which means something different in each product.
_FILLABLE = ("url", "device", "lat", "lon", "loc_source", "loc_precise")


def merge_duplicates(by_source: list[list[Item]]) -> tuple[list[Item], int]:
    """Merge item lists, one per record stream (a source's product), given in priority order.
    An item that matches one from a higher-priority stream within DUPLICATE_WINDOW is a second
    record of the same activity: it is dropped, and fills in any fields the kept item lacks (a
    Chrome visit's device, say). Matching is one-to-one, so reloading a page five times in
    Chrome while My Activity recorded one visit keeps four. Items within one stream are never
    merged: repeats there are real, and compress counts them. Returns the kept items and the
    number merged away."""
    seen: dict[tuple[Kind, str, str], list[tuple[float, int]]] = defaultdict(list)  # (ts, index)
    window = DUPLICATE_WINDOW.total_seconds()
    kept: list[Item] = []
    merged = 0
    for items in by_source:
        claimed: set[int] = set()  # kept items already matched by this source
        fresh: list[Item] = []
        for item in items:
            t = item.ts.timestamp()
            candidates = seen.get(_identity(item), [])
            nearest: tuple[float, int] | None = None  # (gap, kept index)
            for ts, k in candidates[bisect.bisect_left(candidates, (t - window, -1)):]:
                if ts > t + window:
                    break
                if k not in claimed and (nearest is None or abs(ts - t) < nearest[0]):
                    nearest = abs(ts - t), k
            if nearest is None:
                fresh.append(item)
                continue
            best = nearest[1]
            claimed.add(best)
            kept[best] = kept[best]._replace(
                **{f: getattr(item, f) for f in _FILLABLE if getattr(kept[best], f) is None}
            )
            merged += 1
        for item in fresh:
            bisect.insort(seen[_identity(item)], (item.ts.timestamp(), len(kept)))
            kept.append(item)
    return kept, merged


class Loaded(NamedTuple):
    events: list[Event]
    # Items merged into another source's record of the same activity.
    duplicates: int


def load(
    files: list[tuple[Source, Path]],
    since: datetime | None = None,
    until: datetime | None = None,
) -> Loaded:
    """Parse and merge the files into chronologically ordered Events, keeping those in
    [since, until). Ids are assigned over the full history before filtering, so an event has
    the same id no matter what range is selected."""
    streams: dict[tuple[str, str], list[Item]] = defaultdict(list)
    for source, file in files:
        for item in source.parse(file):
            streams[source.name, item.product].append(item)
    order = {s.name: i for i, s in enumerate(sources.SOURCES)}
    items, duplicates = merge_duplicates(
        [streams[k] for k in sorted(streams, key=lambda k: (_product_rank(k[1]), order[k[0]]))]
    )
    items = apps.rename(boilerplate.strip_items(items))
    # Sorting is stable, so AI Mode follow-ups sharing a timestamp keep their order.
    items.sort(key=lambda item: item.ts)
    events = [
        Event(i, *item)
        for i, item in enumerate(items)
        if (since is None or item.ts >= since) and (until is None or item.ts < until)
    ]
    return Loaded(events, duplicates)
