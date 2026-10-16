"""Stages 0-2 (local): read a Takeout export into events, compress them into a transcript, and
compute deterministic features.

Which export to read, which of its sources, and what range of it, is recorded in
``inputs.json`` in the output directory. That file is this phase's input, so like every other phase it re-runs when its
settings, its code, or the export files themselves change.
"""

import json
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import NamedTuple
from zoneinfo import ZoneInfo

from panopticon import compress, features, ingest
from panopticon.events import Event, Kind, read_events, write_events

SETTINGS = "inputs.json"


class Settings(NamedTuple):
    # Takeout roots, folders inside them, or single files; absolute.
    inputs: list[Path]
    since: datetime | None = None
    until: datetime | None = None
    days: int | None = None
    # Names of the sources to read (see sources.SOURCES); None reads every source.
    sources: list[str] | None = None

    def to_json(self) -> dict:
        return {
            "inputs": [str(p) for p in self.inputs],
            "since": self.since.isoformat() if self.since else None,
            "until": self.until.isoformat() if self.until else None,
            "days": self.days,
            "sources": self.sources,
        }

    @classmethod
    def from_json(cls, data: dict) -> "Settings":
        def ts(value: str | None) -> datetime | None:
            return datetime.fromisoformat(value) if value else None

        return cls(
            [Path(p) for p in data.get("inputs", [])],
            ts(data.get("since")),
            ts(data.get("until")),
            data.get("days"),
            data.get("sources"),
        )


def load_settings(out: Path) -> Settings | None:
    path = out / SETTINGS
    return Settings.from_json(json.loads(path.read_text(encoding="utf8"))) if path.exists() else None


def save_settings(out: Path, settings: Settings) -> None:
    out.mkdir(parents=True, exist_ok=True)
    settings = settings._replace(inputs=[p.expanduser().resolve() for p in settings.inputs])
    (out / SETTINGS).write_text(json.dumps(settings.to_json(), indent=2), encoding="utf8")


def source_files(out: Path) -> list[Path]:
    """The export files this phase reads, for the pipeline to hash as inputs. An input that no
    longer exists is listed as-is (it hashes as absent) rather than raising, so planning a run
    still works; running the phase is what reports it."""
    settings = load_settings(out)
    files: list[Path] = []
    for path in settings.inputs if settings else []:
        if path.exists():
            files.extend(file for _, file in ingest.discover([path], settings.sources))
        else:
            files.append(path)
    return files


def select_range(
    events: list[Event],
    since: datetime | None,
    until: datetime | None,
    days: int | None,
) -> list[Event]:
    """Apply --days, which counts back from --until if given, else from the newest event.
    Counting from the export rather than from now keeps runs on a snapshot reproducible."""
    if days is None or not events:
        return events
    end = until or events[-1].ts
    start = end - timedelta(days=days)
    if since is not None:
        start = max(start, since)
    return [e for e in events if e.ts >= start]


class Stats(NamedTuple):
    files: Counter[str]  # files read, by source name
    duplicates: int  # items dropped as another source's copy
    products: Counter[str]  # events in the range, by product
    events: list[Event]
    kept: int
    sessions: int
    transcript_chars: int
    home: features.Home | None


def run(settings: Settings, out: Path) -> Stats:
    """Read the export and write events.jsonl, transcript.txt, and features.json to out/."""
    files = ingest.discover(settings.inputs, settings.sources)
    loaded = ingest.load(files, since=settings.since, until=settings.until)
    events = select_range(loaded.events, settings.since, settings.until, settings.days)
    out.mkdir(parents=True, exist_ok=True)
    write_events(events, out / "events.jsonl")

    kept = [e for e in events if not compress.is_noise(e)]
    fixes = features.location_fixes(kept)
    home = features.home(fixes)
    tz = ZoneInfo(home.tz if home else "UTC")

    # The itinerary phase re-renders this per-session in local time; this one uses home's zone.
    sessions = compress.sessionize(kept)
    transcript = compress.render(sessions, tz_at=lambda _: tz)
    (out / "transcript.txt").write_text(transcript, encoding="utf8")

    rhythm = features.daily_rhythm(kept, tz)
    report = {
        "range": [events[0].ts.isoformat(), events[-1].ts.isoformat()] if events else None,
        "events": len(events),
        "kept": len(kept),
        "sessions": len(sessions),
        "kinds": {k.value: sum(1 for e in kept if e.kind == k) for k in Kind},
        "home": home._asdict() if home else None,
        "location_fixes": [f._asdict() | {"ts": f.ts.isoformat()} for f in fixes],
        "hour_histogram": {"tz": str(tz), "counts": features.hour_histogram(kept, tz)},
        "quiet_periods": [
            q._asdict()
            | {"day": q.day.isoformat(), "start": q.start.isoformat(), "end": q.end.isoformat()}
            for q in rhythm
        ],
        "app_usage": features.app_usage(kept).most_common(),
    }
    (out / "features.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf8"
    )
    return Stats(
        Counter(source.name for source, _ in files),
        loaded.duplicates,
        Counter(e.product for e in events),
        events, len(kept), len(sessions), len(transcript), home,
    )


def load_prepared(out: Path) -> tuple[list[Event], features.Home | None]:
    """Reload the outputs written by `prepare`: the non-noise events, and home."""
    events = [e for e in read_events(out / "events.jsonl") if not compress.is_noise(e)]
    report = json.loads((out / "features.json").read_text(encoding="utf8"))
    home = features.Home(**report["home"]) if report["home"] else None
    return events, home
