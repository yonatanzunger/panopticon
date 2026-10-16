"""The Takeout files panopticon can read, one module per kind of file.

Each module exports a ``SOURCE``: a name, a test for whether a file is one it reads, and a
parser from that file to ``Item``s. ``ingest`` walks the user's inputs, hands each file to the
source that claims it, and merges every source's items into one chronological list of Events.
To read a new kind of export, add a module here and list its ``SOURCE`` in ``SOURCES``.
"""

import csv
import json
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any, NamedTuple

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


# Imported last: the source modules use Item and Source from this module.
from panopticon.sources import chrome, my_activity

# In priority order: a file goes to the first source that claims it, and when two sources
# record the same activity, ingest keeps the earlier source's copy.
SOURCES: tuple[Source, ...] = (my_activity.SOURCE, chrome.SOURCE)


def claim(path: Path) -> Source | None:
    """The source that reads this file, if any."""
    return next((s for s in SOURCES if s.claims(path)), None)
