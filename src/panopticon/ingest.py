"""Stage 0: find the readable files in a Takeout export and merge them into Events."""

from datetime import datetime
from pathlib import Path

from panopticon import sources
from panopticon.events import Event
from panopticon.sources import Item, Source


def discover(paths: list[Path]) -> list[tuple[Source, Path]]:
    """Every file under the given paths that some source reads, each paired with that source.
    A path can be a Takeout root, any folder inside one, or a single file; a file named
    directly must be one a source reads."""
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
    return [(source, file) for file, source in found.items()]


def load(
    files: list[tuple[Source, Path]],
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[Event]:
    """Parse and merge the files into chronologically ordered Events, keeping those in
    [since, until). Ids are assigned over the full history before filtering, so an event has
    the same id no matter what range is selected."""
    items: list[Item] = []
    for source, file in files:
        items.extend(source.parse(file))
    # Sorting is stable, so AI Mode follow-ups sharing a timestamp keep their order.
    items.sort(key=lambda item: item.ts)
    return [
        Event(i, *item)
        for i, item in enumerate(items)
        if (since is None or item.ts >= since) and (until is None or item.ts < until)
    ]
