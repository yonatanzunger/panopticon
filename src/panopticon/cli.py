import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import anthropic

from panopticon import compress, extract, features, ingest, itinerary
from panopticon.events import Event, Kind, read_events, write_events


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
    tokens = len(transcript) / _CHARS_PER_TOKEN
    print(f"transcript: {len(transcript):,} chars (~{tokens:,.0f} tokens)")
    if home:
        print(f"home: {home.lat},{home.lon} [{home.tz}] on {home.days}/{home.of_days} fix-days")


def load_prepared(out: Path) -> tuple[list[Event], features.Home | None]:
    """Reload stage 0-2 outputs written by `prepare`."""
    events = [e for e in read_events(out / "events.jsonl") if not compress.is_noise(e)]
    report = json.loads((out / "features.json").read_text(encoding="utf8"))
    home = features.Home(**report["home"]) if report["home"] else None
    return events, home


# $/MTok, for the dry-run estimate. Output is a rough guess: atoms are small.
# Measured on a real run: transcripts are dense in ids, URLs, and non-Latin text, so they run
# about 2 characters per token rather than the usual 4 for English prose.
_CHARS_PER_TOKEN = 2.0
_PRICES = {"claude-opus-5-5": (4.00, 20.00), "claude-sonnet-5-5": (2.00, 10.00),
           "claude-haiku-5-5": (0.10, 0.50)}


def extract_command(args: argparse.Namespace) -> None:
    events, home = load_prepared(args.dir)
    tz = ZoneInfo(home.tz if home else "UTC")
    groups = compress.chunk_sessions(
        compress.sessionize(events), args.chunk_chars, tz_at=lambda _: tz
    )
    chunks = extract.make_chunks(groups, tz_at=lambda _: tz)

    chars = sum(len(c.text) for c in chunks) + len(chunks) * len(extract.SYSTEM_PROMPT)
    in_tokens = chars / _CHARS_PER_TOKEN
    out_tokens = len(chunks) * 8_000
    price_in, price_out = _PRICES.get(args.model, (0, 0))
    cost = (in_tokens * price_in + out_tokens * price_out) / 1e6
    print(f"{len(chunks)} chunks, ~{in_tokens:,.0f} input tokens to {args.model}")
    if price_in:
        print(f"estimated cost: ~${cost:.2f} (output estimated at 8k tokens/chunk)")

    if args.dry_run:
        for chunk in chunks:
            path = args.dir / f"chunk_{chunk.index:03d}.txt"
            path.write_text(extract.user_prompt(chunk, home), encoding="utf8")
        print(f"dry run: wrote the exact user prompts to {args.dir}/chunk_*.txt; nothing sent")
        return

    try:
        results = extract.run(chunks, home, args.dir, args.model, args.effort, args.concurrency)
    except anthropic.AuthenticationError:
        sys.exit("Claude API authentication failed: run `ant auth login` or set ANTHROPIC_API_KEY.")
    except anthropic.APIStatusError as e:
        sys.exit(f"Claude API error {e.status_code}: {e.message} (request {e.request_id})")
    except anthropic.APIConnectionError as e:
        sys.exit(f"Could not reach the Claude API: {e}")
    atoms = sum(len(r.evidence) for r in results)
    problems = [r for r in results if r.stop_reason != "end_turn"]
    print(f"{atoms} place atoms written to {args.dir / 'places.jsonl'}")
    for r in problems:
        print(f"  chunk {r.chunk}: stop_reason={r.stop_reason}; no atoms from this chunk")


def itinerary_command(args: argparse.Namespace) -> None:
    events, home = load_prepared(args.dir)
    places_path = args.dir / "places.jsonl"
    atoms = (
        [json.loads(line) for line in places_path.read_text(encoding="utf8").splitlines()]
        if places_path.exists()
        else []
    )
    if not atoms:
        print("no places.jsonl from `extract`; using location fixes only", file=sys.stderr)

    segments, places = itinerary.build(atoms, events, home)
    localizer = itinerary.Localizer(segments, default=home.tz if home else "UTC")

    out = [
        {
            "start": s.start.isoformat(),
            "end": s.end.isoformat(),
            "place": s.place.label,
            "lat": s.place.lat,
            "lon": s.place.lon,
            "tz": s.place.tz,
            "is_home": s.place.is_home,
            "weight": round(s.weight, 2),
            "event_ids": s.event_ids,
        }
        for s in segments
    ]
    (args.dir / "itinerary.json").write_text(json.dumps(out, indent=2, ensure_ascii=False))
    transcript = compress.render(compress.sessionize(events), tz_at=localizer)
    (args.dir / "transcript_local.txt").write_text(transcript, encoding="utf8")

    trips = [s for s in segments if not s.place.is_home]
    print(f"{len(places)} places, {len(segments)} segments, {len(trips)} away from home:")
    for s in trips:
        zone = ZoneInfo(s.place.tz)
        print(
            f"  {s.start.astimezone(zone):%Y-%m-%d %H:%M} → {s.end.astimezone(zone):%Y-%m-%d %H:%M}"
            f"  {s.place.label}  [{s.place.tz}]  weight {s.weight:.1f},"
            f" {len(s.event_ids)} events"
        )
    print(f"wrote {args.dir / 'itinerary.json'} and {args.dir / 'transcript_local.txt'}")


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

    p = subparsers.add_parser(
        "extract", help="stage 3: extract place evidence with Claude (sends the transcript)"
    )
    p.add_argument("dir", type=Path, help="an output directory written by `prepare`")
    p.add_argument("--model", default=extract.DEFAULT_MODEL)
    p.add_argument(
        "--effort", default=extract.DEFAULT_EFFORT,
        choices=["low", "medium", "high", "xhigh", "max"],
    )
    p.add_argument("--chunk-chars", type=int, default=extract.DEFAULT_CHUNK_CHARS)
    p.add_argument("--concurrency", type=int, default=4)
    p.add_argument(
        "--dry-run", action="store_true",
        help="write the exact prompts that would be sent, and estimate cost; send nothing",
    )

    p = subparsers.add_parser(
        "itinerary", help="stage 4: reconstruct the itinerary and localize timestamps (local)"
    )
    p.add_argument("dir", type=Path, help="an output directory written by `prepare`")

    args = parser.parse_args()
    if args.command == "prepare":
        events = ingest.load(args.inputs, since=args.since, until=args.until)
        prepare(select_range(events, args.since, args.until, args.days), args.output)
    elif args.command == "extract":
        extract_command(args)
    elif args.command == "itinerary":
        itinerary_command(args)
