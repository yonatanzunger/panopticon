import argparse
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from panopticon import compress, features, ingest
from panopticon.events import Event, Kind, write_events


def _parse_time(value: str) -> datetime:
    """An ISO date or datetime; naive values are taken as UTC."""
    ts = datetime.fromisoformat(value)
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


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


def prepare(events: list[Event], out: Path) -> None:
    """Run the local, deterministic stages (1-2) and write their outputs to out/."""
    out.mkdir(parents=True, exist_ok=True)
    write_events(events, out / "events.jsonl")

    kept = [e for e in events if not compress.is_noise(e)]
    fixes = features.location_fixes(kept)
    home = features.home(fixes)
    tz = ZoneInfo(home.tz if home else "UTC")

    # Until the itinerary solver exists, localize everything to the home zone.
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

    if events:
        print(f"range: {events[0].ts:%Y-%m-%d} to {events[-1].ts:%Y-%m-%d}")
    print(f"{len(events)} events, {len(kept)} after noise removal, {len(sessions)} sessions")
    print(f"transcript: {len(transcript):,} chars (~{len(transcript) // 4:,} tokens)")
    if home:
        print(f"home: {home.lat},{home.lon} [{home.tz}] on {home.days}/{home.of_days} fix-days")


def main() -> None:
    parser = argparse.ArgumentParser("panopticon")
    subparsers = parser.add_subparsers(dest="command", required=True)

    p = subparsers.add_parser("prepare", help="run the local stages: ingest, compress, features")
    p.add_argument(
        "inputs", nargs="+", type=Path, help="a Takeout folder, or MyActivity.json files"
    )
    p.add_argument("-o", "--output", type=Path, default=Path("out"))
    p.add_argument("--since", type=_parse_time, help="keep events at or after this date/time")
    p.add_argument("--until", type=_parse_time, help="keep events before this date/time")
    p.add_argument(
        "--days", type=int, help="keep only the last N days (before --until, or the export's end)"
    )

    args = parser.parse_args()
    if args.command == "prepare":
        events = ingest.load(args.inputs, since=args.since, until=args.until)
        prepare(select_range(events, args.since, args.until, args.days), args.output)
