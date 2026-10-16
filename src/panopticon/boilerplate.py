"""Strip each website's boilerplate from page titles: the site name, the account, the section.

Page titles are mostly "<what's on the page> - <site chrome>": "Many thoughts - me@gmail.com -
Gmail", "Feed | LinkedIn", "Google Calendar - Week of July 12, 2026". The chrome repeats on
every visit and says nothing the "@site" tag doesn't, so this finds it per site, without an
LLM, and strips it.

Titles are split into segments at separators like " - " and " | ". Working inward from the end
(and then from the start), a segment position is boilerplate if one value fills it in most of
the site's distinct titles; it is stripped, and the next position in is checked among those
titles. Only titles with something left after a position vote on it, so a title is never
stripped to nothing, and a title Chrome truncated ("... - The ...") counts as the value it is
a prefix of. The walk stops where the values are varied. It also stops where a few values each
fill a real share of titles, like the account in "<subject> - <account> - Gmail" for someone
with two Gmail accounts: which one a page was in is signal, so those segments are kept.
"""

import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from urllib.parse import urlparse

from panopticon.events import Kind
from panopticon.sources import Item

# Spaced so hyphenated words and "a-b" ranges aren't split. Not ": ", which is too common
# inside real titles ("Money Stuff: ETFs Are for Bets").
_SEPARATOR = re.compile(r"( [-|–—·•] )")

# A site needs this many distinct titles before its boilerplate can be told from its content.
MIN_TITLES = 5
# A value filling at least this share of a position's titles is boilerplate, if it also fills
# SITE_SHARE of all the site's titles (so a few titles about one subject can't qualify).
DOMINANT = 0.5
SITE_SHARE = 0.2
# Two or more values each filling at least this share (and MIN_VARIANT titles) are variants.
VARIANT = 0.1
MIN_VARIANT = 3


def site(url: str | None) -> str | None:
    return (urlparse(url).hostname or "").removeprefix("www.") or None if url else None


def split(title: str) -> list[str]:
    """Alternating segments and separators: ["Feed", " | ", "LinkedIn"]."""
    return _SEPARATOR.split(title)


def _segments(parts: Sequence[str]) -> list[str]:
    return [p.strip() for p in parts[::2]]


_TRUNCATED = ("...", "…")


def _matches(segment: str, value: str) -> bool:
    """The segment is value, or value cut off by Chrome's title truncation."""
    if segment == value:
        return True
    stem = segment.removesuffix("...").removesuffix("…").rstrip()
    return segment.endswith(_TRUNCATED) and bool(stem) and value.startswith(stem)


def _walk(titles: list[list[str]]) -> list[str]:
    """The boilerplate segments at the front of these (already oriented) segment lists, in
    order from the edge inward."""
    found: list[str] = []
    population = titles
    floor = max(MIN_TITLES, SITE_SHARE * len(titles))
    for depth in range(max((len(t) for t in titles), default=0)):
        voters = [t for t in population if len(t) > depth + 1]
        values = Counter(t[depth] for t in voters if not t[depth].endswith(_TRUNCATED))
        if not values:
            break
        top, _ = values.most_common(1)[0]
        n = sum(1 for t in voters if _matches(t[depth], top))
        total = len(voters)
        common = [v for v, c in values.items() if c >= max(MIN_VARIANT, VARIANT * total)]
        if len(common) > 1 or n < DOMINANT * total or n < floor:
            break
        found.append(top)
        population = [t for t in voters if _matches(t[depth], top)]
    return found


class Boilerplate:
    """Per-site prefix and suffix segments to strip."""

    def __init__(self, titles_by_site: dict[str, Iterable[str]]):
        self.suffixes: dict[str, list[str]] = {}
        self.prefixes: dict[str, list[str]] = {}
        for host, titles in titles_by_site.items():
            distinct = sorted(set(titles))
            if len(distinct) < MIN_TITLES:
                continue
            segmented = [_segments(split(t)) for t in distinct]
            if suffix := _walk([s[::-1] for s in segmented]):
                self.suffixes[host] = suffix
                segmented = [_strip_suffix(s, suffix) for s in segmented]
            if prefix := _walk(segmented):
                self.prefixes[host] = prefix

    def strip(self, host: str | None, title: str) -> str:
        if host is None:
            return title
        parts = split(title)
        segments = _segments(parts)
        kept = _strip_suffix(segments, self.suffixes.get(host, []))
        end = len(kept)
        start = _prefix_length(kept, self.prefixes.get(host, []))
        if (start, end) == (0, len(segments)):
            return title
        # Rejoin the surviving segments with their original separators.
        return "".join(parts[2 * start : 2 * end - 1]).strip()


def _strip_suffix(segments: list[str], suffix: list[str]) -> list[str]:
    n = 0
    while n < len(suffix) and n < len(segments) - 1 and _matches(segments[-1 - n], suffix[n]):
        n += 1
    return segments[: len(segments) - n]


def _prefix_length(segments: list[str], prefix: list[str]) -> int:
    n = 0
    while n < len(prefix) and n < len(segments) - 1 and _matches(segments[n], prefix[n]):
        n += 1
    return n


def _titled(item: Item) -> bool:
    return item.kind in (Kind.VISIT, Kind.AD_CLICK) and bool(item.url) and not (
        item.text.startswith(("http://", "https://"))
    )


def strip_items(items: list[Item]) -> list[Item]:
    """Learn each site's boilerplate from every titled visit, and strip it from them all."""
    titles: dict[str, list[str]] = defaultdict(list)
    for item in items:
        if _titled(item) and (host := site(item.url)):
            titles[host].append(item.text)
    boilerplate = Boilerplate(titles)
    return [
        item._replace(text=boilerplate.strip(site(item.url), item.text)) if _titled(item) else item
        for item in items
    ]
