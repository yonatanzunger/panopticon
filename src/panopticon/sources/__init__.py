"""The Takeout files panopticon can read, one module per kind of file.

Each module exports a ``SOURCE``: a name, a test for whether a file is one it reads, and a
parser from that file to ``Item``s. ``ingest`` walks the user's inputs, hands each file to the
source that claims it, and merges every source's items into one chronological list of Events.
To read a new kind of export, add a module here and list its ``SOURCE`` in ``SOURCES``.
"""

import csv
import json
import re
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import parse_qs, urlparse

from panopticon.events import Kind


class Item(NamedTuple):
    """One logical activity before id assignment. A source file row can yield several (an AI
    Mode conversation yields one per prompt), or none."""

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
    device: str | None = None


class Source(NamedTuple):
    # Short and stable, for logs and counts: "my_activity".
    name: str
    # Whether this source reads the file. Sees only the path, so it must be cheap.
    claims: Callable[[Path], bool]
    parse: Callable[[Path], Iterator[Item]]


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf8"))


def read_csv(path: Path) -> Iterator[dict[str, str]]:
    """Rows as dicts keyed by the header line. Takeout CSVs often start with a byte-order mark,
    which utf-8-sig strips so it doesn't end up in the first column's name."""
    with open(path, encoding="utf-8-sig", newline="") as f:
        yield from csv.DictReader(f)


_VIDEO_ID = re.compile(r"^[\w-]{11}$")
_YOUTUBE_HOSTS = {"www.youtube.com", "youtube.com", "m.youtube.com", "music.youtube.com"}


def youtube_video_url(url: str | None) -> str | None:
    """One canonical URL per YouTube video, whichever form a source recorded it in: watch
    pages on any YouTube host, youtu.be links, and Shorts. None for anything else."""
    if not url:
        return None
    parsed = urlparse(url)
    video = None
    if parsed.hostname in _YOUTUBE_HOSTS:
        if parsed.path == "/watch":
            video = parse_qs(parsed.query).get("v", [None])[0]
        elif parsed.path.startswith("/shorts/"):
            video = parsed.path.removeprefix("/shorts/").split("/")[0]
    elif parsed.hostname == "youtu.be":
        video = parsed.path.lstrip("/").split("/")[0]
    if video and _VIDEO_ID.match(video):
        return f"https://www.youtube.com/watch?v={video}"
    return None


# Imported last: the source modules use Item and Source from this module.
from panopticon.sources import chrome, my_activity, youtube

# In priority order: a file goes to the first source that claims it, and when two sources
# record the same activity, ingest keeps the earlier source's copy. Chrome comes last: the
# others record the same searches and videos with more context (locations, channels).
SOURCES: tuple[Source, ...] = (my_activity.SOURCE, youtube.SOURCE, chrome.SOURCE)


def claim(path: Path) -> Source | None:
    """The source that reads this file, if any."""
    return next((s for s in SOURCES if s.claims(path)), None)
