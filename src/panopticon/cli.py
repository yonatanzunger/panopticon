import argparse
import json
import sys
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import anthropic

from panopticon import (
    compress,
    enrich,
    extract,
    features,
    itinerary,
    model,
    pipeline,
    prepare,
    report,
    summarize,
    synthesize,
    verify,
)
from panopticon.model import Trip


def _parse_time(value: str) -> datetime:
    """An ISO date or datetime; naive values are taken as UTC."""
    ts = datetime.fromisoformat(value)
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


def _source_flags(p: argparse.ArgumentParser) -> None:
    """The flags that choose which export `prepare` reads, shared by `prepare` and `run`."""
    p.add_argument(
        "-i",
        "--input",
        type=Path,
        action="append",
        help="a Takeout folder, or a file in one; repeatable (default: those recorded last time)",
    )
    p.add_argument(
        "--since", type=_parse_time, help="keep events at or after this date/time"
    )
    p.add_argument(
        "--until", type=_parse_time, help="keep events before this date/time"
    )
    p.add_argument(
        "--days",
        type=int,
        help="keep only the last N days (before --until, or the export's end)",
    )


def _update_settings(args: argparse.Namespace) -> prepare.Settings | None:
    """Record any source flags given, and return the settings now in effect. Giving any flag
    replaces the recorded range; leaving out --input keeps the recorded inputs."""
    settings = prepare.load_settings(args.dir)
    if (
        args.input is None
        and args.since is None
        and args.until is None
        and args.days is None
    ):
        return settings
    inputs = args.input or (settings.inputs if settings else [])
    if not inputs:
        sys.exit(
            "no input recorded for this directory: pass --input with a Takeout folder"
        )
    settings = prepare.Settings(inputs, args.since, args.until, args.days)
    prepare.save_settings(args.dir, settings)
    return prepare.load_settings(args.dir)


def prepare_command(args: argparse.Namespace) -> None:
    settings = _update_settings(args)
    if settings is None:
        sys.exit(f"{args.dir} records no Takeout export to read: pass --input")
    try:
        stats = prepare.run(settings, args.dir)
    except (FileNotFoundError, ValueError) as e:
        sys.exit(str(e))

    print(
        "read "
        + ", ".join(f"{n} {name} file(s)" for name, n in stats.files.most_common())
    )
    events = stats.events
    if events:
        print(f"range: {events[0].ts:%Y-%m-%d} to {events[-1].ts:%Y-%m-%d}")
    print(
        f"{len(events)} events ("
        + ", ".join(f"{n} {product}" for product, n in stats.products.most_common())
        + f"), {stats.kept} after noise removal, {stats.sessions} sessions"
    )
    tokens = stats.transcript_chars / _CHARS_PER_TOKEN
    print(f"transcript: {stats.transcript_chars:,} chars (~{tokens:,.0f} tokens)")
    if home := stats.home:
        print(
            f"home: {home.lat},{home.lon} [{home.tz}] on {home.days}/{home.of_days} fix-days"
        )
    pipeline.record(args.dir, "prepare")


# $/MTok, for the dry-run estimate. Output is a rough guess: atoms are small.
# Measured on a real run: transcripts are dense in ids, URLs, and non-Latin text, so they run
# about 2 characters per token rather than the usual 4 for English prose.
_CHARS_PER_TOKEN = 2.0
_PRICES = {
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
}


def _estimate(
    model: str, prompt_chars: int, requests: int, out_per_request: int
) -> None:
    in_tokens = prompt_chars / _CHARS_PER_TOKEN
    price_in, price_out = _PRICES.get(model, (0, 0))
    print(f"{requests} request(s), ~{in_tokens:,.0f} input tokens to {model}")
    if price_in:
        cost = (in_tokens * price_in + requests * out_per_request * price_out) / 1e6
        print(
            f"estimated cost: ~${cost:.2f} (output estimated at "
            f"{out_per_request // 1000}k tokens/request)"
        )


def _call_api[T](fn: Callable[[], T]) -> T:
    """Run a stage's API calls, turning API failures into a clear message and exit code."""
    try:
        return fn()
    except anthropic.AuthenticationError:
        sys.exit(
            "Claude API authentication failed: run `ant auth login` or set ANTHROPIC_API_KEY."
        )
    except anthropic.APIStatusError as e:
        sys.exit(
            f"Claude API error {e.status_code}: {e.message} (request {e.request_id})"
        )
    except anthropic.APIConnectionError as e:
        sys.exit(f"Could not reach the Claude API: {e}")


def extract_command(args: argparse.Namespace) -> None:
    events, home = prepare.load_prepared(args.dir)
    tz = ZoneInfo(home.tz if home else "UTC")
    groups = compress.chunk_sessions(
        compress.sessionize(events), args.chunk_chars, tz_at=lambda _: tz
    )
    chunks = extract.make_chunks(groups, tz_at=lambda _: tz)
    chars = sum(len(c.text) for c in chunks) + len(chunks) * len(extract.SYSTEM_PROMPT)
    _estimate(args.model, chars, len(chunks), out_per_request=20_000)

    if args.dry_run:
        for chunk in chunks:
            path = args.dir / f"chunk_{chunk.index:03d}.txt"
            path.write_text(extract.user_prompt(chunk, home), encoding="utf8")
        print(
            f"dry run: wrote the exact user prompts to {args.dir}/chunk_*.txt; nothing sent"
        )
        return

    results = _call_api(
        lambda: extract.run(
            chunks, home, args.dir, args.model, args.effort, args.concurrency
        )
    )
    places = sum(len(r.places) for r in results)
    facts = sum(len(r.facts) for r in results)
    print(
        f"{places} place atoms and {facts} fact atoms written to "
        f"{args.dir / 'places.jsonl'} and {args.dir / 'facts.jsonl'}"
    )
    for r in results:
        if r.stop_reason != "end_turn":
            print(
                f"  chunk {r.chunk}: stop_reason={r.stop_reason}; no atoms from this chunk"
            )
    pipeline.record(args.dir, "extract")


def itinerary_command(args: argparse.Namespace) -> None:
    events, home = prepare.load_prepared(args.dir)
    places_path = args.dir / "places.jsonl"
    atoms = (
        [
            json.loads(line)
            for line in places_path.read_text(encoding="utf8").splitlines()
        ]
        if places_path.exists()
        else []
    )
    if not atoms:
        print(
            "no places.jsonl from `extract`; using location fixes only", file=sys.stderr
        )

    segments, places = itinerary.build(atoms, events, home)
    localizer = itinerary.Localizer.from_segments(
        segments, default=home.tz if home else "UTC"
    )

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
    (args.dir / "itinerary.json").write_text(
        json.dumps(out, indent=2, ensure_ascii=False)
    )
    transcript = compress.render(compress.sessionize(events), tz_at=localizer)
    (args.dir / "transcript_local.txt").write_text(transcript, encoding="utf8")

    trips = [s for s in segments if not s.place.is_home]
    print(
        f"{len(places)} places, {len(segments)} segments, {len(trips)} away from home:"
    )
    for s in trips:
        zone = ZoneInfo(s.place.tz)
        print(
            f"  {s.start.astimezone(zone):%Y-%m-%d %H:%M} → {s.end.astimezone(zone):%Y-%m-%d %H:%M}"
            f"  {s.place.label}  [{s.place.tz}]  weight {s.weight:.1f},"
            f" {len(s.event_ids)} events"
        )
    print(
        f"wrote {args.dir / 'itinerary.json'} and {args.dir / 'transcript_local.txt'}"
    )
    pipeline.record(args.dir, "itinerary")


def _read_jsonl(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf8").splitlines()
        if line
    ]


# The most prompt (in characters) to put in one synthesis request, leaving the 1M-token
# context room to think and answer. Above this, synthesis goes hierarchical. ~2 chars/token.
_MAX_SYNTHESIS_CHARS = 1_000_000


def synthesize_command(args: argparse.Namespace) -> None:
    events, home = prepare.load_prepared(args.dir)
    facts_path = args.dir / "facts.jsonl"
    if not facts_path.exists():
        sys.exit(f"no {facts_path}: run `extract` first")
    facts = _read_jsonl(facts_path)
    default_tz = home.tz if home else "UTC"

    itinerary_path = args.dir / "itinerary.json"
    if itinerary_path.exists():
        segments = json.loads(itinerary_path.read_text(encoding="utf8"))
        tz_at = itinerary.Localizer.from_json(segments, default=default_tz)
        trips = Trip.from_segments(segments)
    else:
        print(
            "no itinerary.json: run `itinerary` for trips and local times",
            file=sys.stderr,
        )
        tz_at = itinerary.Localizer([], default=default_tz)
        trips = []

    inputs = synthesize.build_inputs(facts, trips, events, home, tz_at)
    chars = len(inputs.text) + len(synthesize.SYSTEM_PROMPT)
    # Over the budget, synthesis is hierarchical: one request per time window plus a merge.
    windows = synthesize.partition_facts(
        facts, events, tz_at, max(_MAX_SYNTHESIS_CHARS - (chars - len(inputs.text)), 1)
    )
    hierarchical = chars > _MAX_SYNTHESIS_CHARS and len(windows) > 1
    requests = len(windows) + 1 if hierarchical else 1
    _estimate(args.model, chars, requests, out_per_request=30_000)
    if hierarchical:
        print(
            f"facts exceed one request; synthesizing in {len(windows)} periods, then merging"
        )

    if args.dry_run:
        (args.dir / "synthesis_prompt.txt").write_text(inputs.text, encoding="utf8")
        print(
            f"dry run: wrote the exact prompt (single-pass form) to "
            f"{args.dir / 'synthesis_prompt.txt'}; nothing sent"
        )
        return

    if hierarchical:
        result = _call_api(
            lambda: synthesize.run_hierarchical(
                facts,
                trips,
                events,
                home,
                tz_at,
                _MAX_SYNTHESIS_CHARS,
                args.model,
                args.effort,
            )
        )
    else:
        result = _call_api(
            lambda: synthesize.run(
                inputs, facts, trips, events, tz_at, args.model, args.effort
            )
        )
    if result.stop_reason != "end_turn":
        sys.exit(
            f"synthesis stopped with stop_reason={result.stop_reason}; no profile written"
        )
    synthesize.write(result, args.dir)
    unresolved = sum(1 for c in result.claims if not c.evidence)
    print(
        f"{len(result.claims)} claims written to {args.dir / 'profile.json'} and "
        f"{args.dir / 'profile.md'}"
    )
    if unresolved:
        print(f"  {unresolved} claim(s) cite no evidence that exists", file=sys.stderr)
    pipeline.record(args.dir, "synthesize")


def _localizer(
    out: Path, home: features.Home | None
) -> tuple[itinerary.Localizer, list[Trip]]:
    """The itinerary's localizer and its trips, or home-zone times and no trips."""
    default_tz = home.tz if home else "UTC"
    segments = report.load_json(out / "itinerary.json")
    if not isinstance(segments, list):
        return itinerary.Localizer([], default=default_tz), []
    return (
        itinerary.Localizer.from_json(segments, default=default_tz),
        Trip.from_segments(segments),
    )


def verify_command(args: argparse.Namespace) -> None:
    events, home = prepare.load_prepared(args.dir)
    profile = report.load_json(args.dir / "profile.json")
    if not isinstance(profile, dict):
        sys.exit(f"no {args.dir / 'profile.json'}: run `synthesize` first")
    tz_at, _ = _localizer(args.dir, home)
    claims = [model.Claim.from_dict(c) for c in profile["claims"]]
    by_id = {e.id: e for e in events}
    activity = synthesize.activity_block(events, tz_at)
    prompts = [verify.claim_prompt(c, by_id, tz_at, activity) for c in claims]
    chars = sum(len(p) for p in prompts) + len(prompts) * len(verify.SYSTEM_PROMPT)
    _estimate(args.model, chars, len(prompts), out_per_request=2_000)

    if args.dry_run:
        (args.dir / "verify_prompts.txt").write_text(
            "\n\n=====\n\n".join(prompts), encoding="utf8"
        )
        print(
            f"dry run: wrote the exact prompts to {args.dir / 'verify_prompts.txt'}; "
            "nothing sent"
        )
        return

    verified, results = _call_api(
        lambda: verify.run(
            claims, events, tz_at, args.model, args.effort, args.concurrency, activity
        )
    )
    verify.write(profile["summary"], verified, results, args.dir)
    statuses = Counter(c.status for c in verified)
    print(
        f"{len(verified)} claims: "
        + ", ".join(f"{n} {s}" for s, n in statuses.most_common())
        + f"; written to {args.dir / 'verified.json'}"
    )
    pipeline.record(args.dir, "verify")


def summarize_command(args: argparse.Namespace) -> None:
    verified = report.load_json(args.dir / "verified.json")
    if not isinstance(verified, dict):
        sys.exit(f"no {args.dir / 'verified.json'}: run `verify` first")
    _, home = prepare.load_prepared(args.dir)
    _, trips = _localizer(args.dir, home)
    prompt = summarize.build_prompt(
        [model.Claim.from_dict(c) for c in verified["claims"]], trips
    )
    _estimate(
        args.model, len(prompt) + len(summarize.SYSTEM_PROMPT), 1, out_per_request=3_000
    )
    if args.dry_run:
        (args.dir / "summarize_prompt.txt").write_text(prompt, encoding="utf8")
        print(
            f"dry run: wrote the exact prompt to {args.dir / 'summarize_prompt.txt'}; "
            "nothing sent"
        )
        return
    result = _call_api(lambda: summarize.run(prompt, args.model, args.effort))
    if not result.summary:
        sys.exit(
            f"summary stopped with stop_reason={result.stop_reason}; nothing written"
        )
    summarize.write(result, args.dir)
    print(f"wrote {args.dir / 'summary.json'}")
    pipeline.record(args.dir, "summarize")


def enrich_command(args: argparse.Namespace) -> None:
    profile = report.load_json(args.dir / "profile.json")
    if not isinstance(profile, dict):
        sys.exit(f"no {args.dir / 'profile.json'}: run `synthesize` first")
    _, trips = _localizer(args.dir, prepare.load_prepared(args.dir)[1])
    result = enrich.run([model.Claim.from_dict(c) for c in profile["claims"]], trips)
    enrich.write(result, args.dir)
    if result.enrichers:
        print(
            f"{len(result.findings)} findings from {', '.join(result.enrichers)}; "
            f"wrote {args.dir / 'enrichment.json'}"
        )
    else:
        print(f"no enrichers configured; wrote an empty {args.dir / 'enrichment.json'}")
    pipeline.record(args.dir, "enrich")


def _phase_runners() -> dict[str, Callable[[Path], None]]:
    """Map each pipeline phase to a callable that runs it with default options."""

    def ns(**kw: object) -> argparse.Namespace:
        return argparse.Namespace(**kw)

    return {
        "prepare": lambda d: prepare_command(
            ns(dir=d, input=None, since=None, until=None, days=None)
        ),
        "extract": lambda d: extract_command(
            ns(
                dir=d,
                model=extract.DEFAULT_MODEL,
                effort=extract.DEFAULT_EFFORT,
                chunk_chars=extract.DEFAULT_CHUNK_CHARS,
                concurrency=4,
                dry_run=False,
            )
        ),
        "itinerary": lambda d: itinerary_command(ns(dir=d)),
        "synthesize": lambda d: synthesize_command(
            ns(
                dir=d,
                model=synthesize.DEFAULT_MODEL,
                effort=synthesize.DEFAULT_EFFORT,
                dry_run=False,
            )
        ),
        "enrich": lambda d: enrich_command(ns(dir=d)),
        "verify": lambda d: verify_command(
            ns(
                dir=d,
                model=verify.DEFAULT_MODEL,
                effort=verify.DEFAULT_EFFORT,
                concurrency=8,
                dry_run=False,
            )
        ),
        "summarize": lambda d: summarize_command(
            ns(
                dir=d,
                model=summarize.DEFAULT_MODEL,
                effort=summarize.DEFAULT_EFFORT,
                dry_run=False,
            )
        ),
        "report": lambda d: report_command(ns(dir=d, output=None)),
    }


def run_command(args: argparse.Namespace) -> None:
    if _update_settings(args) is None and not (args.dir / "events.jsonl").exists():
        sys.exit(f"{args.dir} records no Takeout export to read: pass --input")
    if args.adopt:
        config = pipeline.load_config(args.dir)
        adopted = pipeline.adopt(args.dir, config)
        pipeline.save_config(args.dir, config)
        print(f"adopted existing outputs as up-to-date: {', '.join(adopted) or 'none'}")
        return
    pipeline.run_pipeline(
        args.dir, _phase_runners(), force=args.force, plan_only=args.plan
    )


def report_command(args: argparse.Namespace) -> None:
    events, home = prepare.load_prepared(args.dir)
    tz_at, trips = _localizer(args.dir, home)
    verified = report.load_json(args.dir / "verified.json")
    profile = report.load_json(args.dir / "profile.json")
    if isinstance(verified, dict):
        source = verified
    elif isinstance(profile, dict):
        print(
            "no verified.json: reporting unverified claims (run `verify` first)",
            file=sys.stderr,
        )
        source = profile
    else:
        sys.exit(f"no profile in {args.dir}: run `synthesize` (and `verify`) first")

    extract_log = (
        _read_jsonl(args.dir / "extract_log.jsonl")
        if (args.dir / "extract_log.jsonl").exists()
        else None
    )
    final = report.load_json(args.dir / "summary.json")
    summary_is_final = isinstance(final, dict) and isinstance(verified, dict)
    if isinstance(verified, dict) and not summary_is_final:
        print(
            "no summary.json: showing synthesis's pre-verification draft summary "
            "(run `summarize`)",
            file=sys.stderr,
        )
    usage = report.api_usage(
        {
            "extract": extract_log,
            "synthesize": profile,
            "verify": verified,
            "summarize": final,
        }
    )
    html = report.render(
        final["summary"] if summary_is_final else source["summary"],
        [model.Claim.from_dict(c) for c in source["claims"]],
        trips,
        events,
        tz_at,
        home.tz if home else "UTC",
        usage,
        summary_is_final,
    )
    path = args.output or args.dir / "report.html"
    path.write_text(html, encoding="utf8")
    print(
        f"wrote {path} ({len(html) / 1000:,.0f} kB); it loads nothing from the network"
    )
    if (
        args.output is None
    ):  # recorded only when writing the default report.html the graph tracks
        pipeline.record(args.dir, "report")


def main() -> None:
    parser = argparse.ArgumentParser("panopticon")
    subparsers = parser.add_subparsers(dest="command", required=True)

    p = subparsers.add_parser(
        "prepare",
        help="stages 0-2: read the Takeout export, compress, features (local)",
    )
    p.add_argument("dir", type=Path, help="the output directory (created if needed)")
    _source_flags(p)

    p = subparsers.add_parser(
        "extract",
        help="stage 3: extract place and fact evidence with Claude (sends the transcript)",
    )
    p.add_argument("dir", type=Path, help="an output directory written by `prepare`")
    p.add_argument("--model", default=extract.DEFAULT_MODEL)
    p.add_argument(
        "--effort",
        default=extract.DEFAULT_EFFORT,
        choices=["low", "medium", "high", "xhigh", "max"],
    )
    p.add_argument("--chunk-chars", type=int, default=extract.DEFAULT_CHUNK_CHARS)
    p.add_argument("--concurrency", type=int, default=4)
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="write the exact prompts that would be sent, and estimate cost; send nothing",
    )

    p = subparsers.add_parser(
        "itinerary",
        help="stage 4: reconstruct the itinerary and localize timestamps (local)",
    )
    p.add_argument("dir", type=Path, help="an output directory written by `prepare`")

    p = subparsers.add_parser(
        "synthesize",
        help="stage 5: join facts, itinerary, and activity into a profile with Claude",
    )
    p.add_argument("dir", type=Path, help="an output directory with `extract` facts")
    p.add_argument("--model", default=synthesize.DEFAULT_MODEL)
    p.add_argument(
        "--effort",
        default=synthesize.DEFAULT_EFFORT,
        choices=["low", "medium", "high", "xhigh", "max"],
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="write the exact prompt that would be sent, and estimate cost; send nothing",
    )

    p = subparsers.add_parser(
        "verify",
        help="stage 6: check each claim against only its cited lines, with Claude",
    )
    p.add_argument(
        "dir", type=Path, help="an output directory with a `synthesize` profile"
    )
    p.add_argument("--model", default=verify.DEFAULT_MODEL)
    p.add_argument(
        "--effort",
        default=verify.DEFAULT_EFFORT,
        choices=["low", "medium", "high", "xhigh", "max"],
    )
    p.add_argument("--concurrency", type=int, default=8)
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="write the exact prompts that would be sent, and estimate cost; send nothing",
    )

    p = subparsers.add_parser(
        "summarize",
        help="write the final summary from the verified claims, with Claude",
    )
    p.add_argument("dir", type=Path, help="an output directory with `verify` output")
    p.add_argument("--model", default=summarize.DEFAULT_MODEL)
    p.add_argument(
        "--effort",
        default=summarize.DEFAULT_EFFORT,
        choices=["low", "medium", "high", "xhigh", "max"],
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="write the exact prompt that would be sent, and estimate cost; send nothing",
    )

    p = subparsers.add_parser(
        "enrich",
        help="gather external evidence about the subject (noop until enrichers exist)",
    )
    p.add_argument(
        "dir", type=Path, help="an output directory with a `synthesize` profile"
    )

    p = subparsers.add_parser(
        "report", help="write a self-contained HTML report (local)"
    )
    p.add_argument("dir", type=Path, help="an output directory with a profile")
    p.add_argument(
        "-o", "--output", type=Path, help="where to write it (default: DIR/report.html)"
    )

    p = subparsers.add_parser(
        "run",
        help="run every pipeline phase whose inputs or code changed, up to report",
    )
    p.add_argument("dir", type=Path, help="the output directory (created if needed)")
    _source_flags(p)
    p.add_argument("--force", action="store_true", help="re-run every phase")
    p.add_argument(
        "--plan", action="store_true", help="show what would run; run nothing"
    )
    p.add_argument(
        "--adopt",
        action="store_true",
        help="record existing outputs as up-to-date without running anything",
    )

    args = parser.parse_args()
    if args.command == "prepare":
        prepare_command(args)
    elif args.command == "extract":
        extract_command(args)
    elif args.command == "itinerary":
        itinerary_command(args)
    elif args.command == "synthesize":
        synthesize_command(args)
    elif args.command == "verify":
        verify_command(args)
    elif args.command == "summarize":
        summarize_command(args)
    elif args.command == "enrich":
        enrich_command(args)
    elif args.command == "report":
        report_command(args)
    elif args.command == "run":
        run_command(args)
