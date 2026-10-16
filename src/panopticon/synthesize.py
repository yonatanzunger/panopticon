"""Stage 5: synthesis. Joins fact atoms, the itinerary, and activity statistics from the whole
range into a profile of claims about the user, each citing the atoms it rests on.

Extraction sees one chunk at a time, so it can only report local evidence; this is where
evidence from months apart gets combined ("searched Truckee sewer service shutdown" in
September + "California form 593" in August = selling the Truckee house).
"""

import json
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from typing import Literal, NamedTuple
from urllib.parse import urlparse

import anthropic
from pydantic import BaseModel, ConfigDict, Field

from panopticon.events import Event, Kind
from panopticon import llm
from panopticon.extract import Category, Sensitivity
from panopticon.features import Home
from panopticon.model import Claim, Trip
from panopticon.progress import Progress

DEFAULT_MODEL = "claude-opus-5-5"
# Joining evidence across months is the hardest reasoning step in the pipeline.
DEFAULT_EFFORT = "high"


class ClaimDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: Category
    claim: str = Field(description="The inference, in one plain sentence about the user.")
    detail: str = Field(
        description="One to three sentences on how the evidence fits together: what was "
        "joined with what, and when."
    )
    confidence: Literal["high", "medium", "low"]
    about: Literal["self", "household_member", "other_person", "unclear"]
    sensitivity: list[Sensitivity]
    evidence: list[str] = Field(
        description="The ids of the facts (F<n>) and trips (T<n>) this claim rests on."
    )
    alternatives: list[str] = Field(
        description="Other explanations for the same evidence that the claim's confidence "
        "should be weighed against; empty if there are no serious ones."
    )


class Profile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(
        description="A short portrait of the user, two or three paragraphs, as an observer "
        "with this data would write it. State only what the claims support."
    )
    claims: list[ClaimDraft]


SYSTEM_PROMPT = """\
You are the synthesis stage of a privacy tool that shows people how much their own Google \
activity history reveals about them. The user is reviewing their own data. Your job is to \
write the profile that an observer with this data could assemble, so that the user can see \
it: thorough, specific, and candid, including the sensitive inferences, but honest about how \
sure each one is.

You will get the user's approximate home, a reconstructed itinerary of trips away from home \
(T<n>), activity statistics, and fact atoms (F<n>) extracted from the history one chunk at a \
time. Each fact line reads:
  F<n> <local date or date range> [<category>; about <whom>; <strength>; sensitive: <kinds>] \
<observation> => <inference>

Extraction saw only one chunk at a time, so its inferences are local and often tentative. \
Your value is in joining them: facts weeks or months apart that point at the same \
conclusion, a pattern that only shows up across many facts, a sequence of events that tells \
a story (a sale, a move, a new job, an illness), and contradictions that rule an inference \
out. Prefer specific, checkable claims to vague ones, and cover every category the evidence \
supports.

Calibrate confidence to the evidence. High means a careful observer would be surprised to \
be wrong; low means a plausible hypothesis worth showing the user. Use "about" carefully: \
health searches can be for a relative, and a claim about someone else is not a claim about \
the user. List serious alternative explanations, especially for sensitive claims. Do not \
present guesses about sexuality, health, or similar matters as facts; do report them, \
at the confidence the evidence supports, because they are exactly the inferences the user \
needs to know an observer could draw.

Cite every fact and trip a claim rests on, by id.\
"""


class Inputs(NamedTuple):
    text: str
    fact_ids: frozenset[str]
    trip_ids: frozenset[str]


def _local_day(ts: datetime, tz_at: Callable[[datetime], tzinfo]) -> str:
    return ts.astimezone(tz_at(ts)).strftime("%Y-%m-%d")


def activity_block(events: list[Event], tz_at: Callable[[datetime], tzinfo]) -> str:
    """Statistics computed over every event in the range, not a sample: kinds, hours, top apps
    (with monthly counts for the top ten, so claims about when habits happened can be checked),
    and top sites."""
    lines = ["<activity>"]
    if events:
        lines.append(f"range: {_local_day(events[0].ts, tz_at)} to "
                     f"{_local_day(events[-1].ts, tz_at)}, {len(events)} events")
        kinds = Counter(e.kind.value for e in events)
        lines.append("events by kind: " + ", ".join(f"{k} {n}" for k, n in kinds.most_common()))
        hours = Counter(e.ts.astimezone(tz_at(e.ts)).hour for e in events)
        lines.append("events by local hour 0-23: " + " ".join(str(hours[h]) for h in range(24)))
        apps = Counter(e.text for e in events if e.kind == Kind.APP_USE)
        lines.append("most used apps: " + "; ".join(f"{a} ({n})" for a, n in apps.most_common(25)))
        months = sorted({e.ts.astimezone(tz_at(e.ts)).strftime("%Y-%m") for e in events})
        for app, _ in apps.most_common(10):
            by_month = Counter(
                e.ts.astimezone(tz_at(e.ts)).strftime("%Y-%m")
                for e in events
                if e.kind == Kind.APP_USE and e.text == app
            )
            lines.append(f"  {app} by month: "
                         + ", ".join(f"{m} {by_month[m]}" for m in months))
        sites = Counter(
            (urlparse(e.url).hostname or "").removeprefix("www.")
            for e in events
            if e.kind == Kind.VISIT and e.url
        )
        lines.append("most visited sites: " + "; ".join(
            f"{s} ({n})" for s, n in sites.most_common(40) if s
        ))
    lines.append("</activity>")
    return "\n".join(lines)


def _fact_line(fact: dict, by_id: dict[int, Event], tz_at: Callable[[datetime], tzinfo]) -> str | None:
    times = [by_id[i].ts for i in fact["event_ids"] if i in by_id]
    if not times:
        return None
    first, last = _local_day(min(times), tz_at), _local_day(max(times), tz_at)
    when = first if first == last else f"{first}..{last}"
    sensitive = ", ".join(fact["sensitivity"]) or "none"
    return (
        f"{fact['id']} {when} [{fact['category']}; about {fact['about']}; "
        f"{fact['strength']}; sensitive: {sensitive}] "
        f"{fact['observation']} => {fact['inference']}"
    )


def build_inputs(
    facts: list[dict],
    trips: list[Trip],
    events: list[Event],
    home: Home | None,
    tz_at: Callable[[datetime], tzinfo],
) -> Inputs:
    by_id = {e.id: e for e in events}
    lines: list[str] = []

    lines.append("<home>")
    lines.append(f"near {home.lat}, {home.lon} ({home.tz})" if home else "unknown")
    lines.append("</home>\n")

    lines.append("<itinerary>")
    for i, trip in enumerate(trips):
        lines.append(f"T{i} {trip.start[:10]} to {trip.end[:10]}: {trip.place}")
    lines.append("</itinerary>\n")

    lines.append(activity_block(events, tz_at) + "\n")

    lines.append("<facts>")
    for fact in facts:
        line = _fact_line(fact, by_id, tz_at)
        if line:
            lines.append(line)
    lines.append("</facts>")

    return Inputs(
        "\n".join(lines),
        frozenset(f["id"] for f in facts),
        frozenset(f"T{i}" for i in range(len(trips))),
    )


def resolve(
    profile: Profile,
    inputs: Inputs,
    facts: list[dict],
    trips: list[Trip],
    events: list[Event],
    tz_at: Callable[[datetime], tzinfo],
) -> list[Claim]:
    """Turn each drafted claim into a domain Claim: keep only citations that resolve, and
    attach the source event ids and the dates the claim's evidence spans."""
    fact_events = {f["id"]: f["event_ids"] for f in facts}
    trip_events = {f"T{i}": t.event_ids for i, t in enumerate(trips)}
    by_id = {e.id: e for e in events}
    out: list[Claim] = []
    for claim in profile.claims:
        cited = [c for c in claim.evidence if c in inputs.fact_ids or c in inputs.trip_ids]
        ids = sorted({i for c in cited for i in fact_events.get(c) or trip_events.get(c) or []})
        times = [by_id[i].ts for i in ids if i in by_id]
        out.append(
            Claim(
                category=claim.category, claim=claim.claim, detail=claim.detail,
                confidence=claim.confidence, about=claim.about, sensitivity=claim.sensitivity,
                alternatives=claim.alternatives,
                evidence=cited,
                unresolved_evidence=[c for c in claim.evidence if c not in cited],
                event_ids=ids,
                first_seen=_local_day(min(times), tz_at) if times else None,
                last_seen=_local_day(max(times), tz_at) if times else None,
            )
        )
    return out


class SynthesisResult(NamedTuple):
    summary: str
    claims: list[Claim]
    stop_reason: str | None
    model: str | None
    usage: dict


def run(
    inputs: Inputs,
    facts: list[dict],
    trips: list[Trip],
    events: list[Event],
    tz_at: Callable[[datetime], tzinfo],
    model: str = DEFAULT_MODEL,
    effort: str = DEFAULT_EFFORT,
) -> SynthesisResult:
    client = anthropic.Anthropic()
    with Progress("synthesize", total=1) as progress:
        progress.start(0, "profile")
        result = llm.call(
            client, model=model, effort=effort, system=SYSTEM_PROMPT, prompt=inputs.text,
            output_format=Profile, on_output=lambda chars: progress.update(0, chars),
        )
        usage = result.usage
        profile = result.parsed
        n_claims = len(profile.claims) if profile else 0
        progress.finish(0, f"{n_claims} claims, stop={result.stop_reason}, "
                           f"in={usage['input_tokens']:,} out={usage['output_tokens']:,} tokens")

    if profile is None:
        return SynthesisResult("", [], result.stop_reason, result.model, usage)
    claims = resolve(profile, inputs, facts, trips, events, tz_at)
    return SynthesisResult(profile.summary, claims, result.stop_reason, result.model, usage)


MERGE_SYSTEM_PROMPT = """\
You are combining several per-period profiles of one person into a single profile. Each line \
below (Pn) is a claim already synthesized from one slice of the history, carrying its evidence \
ids: F-ids are facts, T-ids are trips. You also get the full list of trips.

Merge claims that are about the same thing across periods into one claim, and keep genuinely \
distinct ones separate. When you merge claims, union their evidence ids onto the result, and \
set the confidence the combined evidence supports: the same thing recurring across periods can \
raise confidence; a later period contradicting an earlier one lowers it, or splits into a \
"changed over time" claim. A claim that appears in only one period is still worth keeping.

Preserve evidence ids: every claim you output must cite, in its evidence list, all the F-ids \
and T-ids of the per-period claims it came from. Add nothing beyond what the per-period claims \
and trips contain, and don't strengthen anything. Keep each claim at the confidence its \
evidence supports, attribute claims about others to those people, and keep the sensitive \
inferences, since the user needs to see them. Also write a short overall summary.\
"""


def partition_facts(
    facts: list[dict],
    events: list[Event],
    tz_at: Callable[[datetime], tzinfo],
    budget_chars: int,
) -> list[list[dict]]:
    """Split facts into time-ordered windows, each rendering to at most budget_chars of fact
    lines, so one window plus the fixed context fits in a single synthesis request. A single
    fact larger than the budget becomes its own window rather than being dropped."""
    by_id = {e.id: e for e in events}

    def when(fact: dict) -> datetime:
        times = [by_id[i].ts for i in fact["event_ids"] if i in by_id]
        return min(times) if times else datetime.max.replace(tzinfo=UTC)

    windows: list[list[dict]] = []
    current: list[dict] = []
    size = 0
    for fact in sorted(facts, key=when):
        length = len(_fact_line(fact, by_id, tz_at) or "") + 1
        if current and size + length > budget_chars:
            windows.append(current)
            current, size = [], 0
        current.append(fact)
        size += length
    if current:
        windows.append(current)
    return windows


def _partial_line(index: int, claim: Claim) -> str:
    sensitive = ", ".join(claim.sensitivity) or "none"
    evidence = ", ".join(claim.evidence) or "none"
    alternatives = " | ".join(claim.alternatives) or "none"
    return (
        f"P{index} [{claim.category}; {claim.confidence}; about {claim.about}; "
        f"sensitive: {sensitive}] {claim.claim} :: {claim.detail} :: "
        f"alternatives: {alternatives} :: evidence: {evidence}"
    )


def build_merge_prompt(partials: list[Claim], trips: list[Trip]) -> str:
    lines = ["<period_claims>"]
    lines += [_partial_line(i, c) for i, c in enumerate(partials)]
    lines.append("</period_claims>\n\n<trips>")
    for i, trip in enumerate(trips):
        lines.append(f"T{i} {trip.start[:10]} to {trip.end[:10]}: {trip.place}")
    lines.append("</trips>")
    return "\n".join(lines)


def run_hierarchical(
    facts: list[dict],
    trips: list[Trip],
    events: list[Event],
    home: Home | None,
    tz_at: Callable[[datetime], tzinfo],
    max_chars: int,
    model: str = DEFAULT_MODEL,
    effort: str = DEFAULT_EFFORT,
) -> SynthesisResult:
    """Synthesize histories too large for one request: synthesize each time window into partial
    claims, then merge the partials into one profile. Evidence ids are global, so citations
    survive the merge."""
    overhead = len(build_inputs([], trips, events, home, tz_at).text) + len(SYSTEM_PROMPT)
    windows = partition_facts(facts, events, tz_at, max(max_chars - overhead, 1))
    client = anthropic.Anthropic()
    usage = {"input_tokens": 0, "output_tokens": 0}

    def add(u: dict) -> None:
        usage["input_tokens"] += u["input_tokens"]
        usage["output_tokens"] += u["output_tokens"]

    partials: list[Claim] = []
    with Progress("synthesize", total=len(windows) + 1) as progress:
        for w, window in enumerate(windows):
            progress.start(w, f"period {w + 1}/{len(windows)} ({len(window)} facts)")
            inputs = build_inputs(window, trips, events, home, tz_at)
            result = llm.call(
                client, model=model, effort=effort, system=SYSTEM_PROMPT, prompt=inputs.text,
                output_format=Profile, on_output=lambda chars, w=w: progress.update(w, chars),
            )
            add(result.usage)
            if result.parsed:
                partials.extend(resolve(result.parsed, inputs, window, trips, events, tz_at))
            progress.finish(w, f"{len(result.parsed.claims) if result.parsed else 0} claims, "
                               f"stop={result.stop_reason}")

        progress.start("merge", f"merging {len(partials)} claims from {len(windows)} periods")
        merged = llm.call(
            client, model=model, effort=effort, system=MERGE_SYSTEM_PROMPT,
            prompt=build_merge_prompt(partials, trips), output_format=Profile,
            on_output=lambda chars: progress.update("merge", chars),
        )
        add(merged.usage)
        progress.finish("merge", f"{len(merged.parsed.claims) if merged.parsed else 0} claims, "
                                 f"stop={merged.stop_reason}")

    if merged.parsed is None:
        return SynthesisResult("", [], merged.stop_reason, merged.model, usage)
    all_inputs = Inputs(
        "", frozenset(f["id"] for f in facts), frozenset(f"T{i}" for i in range(len(trips)))
    )
    claims = resolve(merged.parsed, all_inputs, facts, trips, events, tz_at)
    return SynthesisResult(merged.parsed.summary, claims, merged.stop_reason, merged.model, usage)


_CONFIDENCE_ORDER = {"high": 0, "medium": 1, "low": 2}


def to_markdown(summary: str, claims: list[Claim]) -> str:
    """A readable rendering of the profile, grouped by category, most confident first."""
    out = ["# Profile", "", summary.strip(), ""]
    by_category: dict[str, list[Claim]] = {}
    for claim in claims:
        by_category.setdefault(claim.category, []).append(claim)
    for category, items in by_category.items():
        out += [f"## {category.replace('_', ' ').capitalize()}", ""]
        for c in sorted(items, key=lambda c: _CONFIDENCE_ORDER[c.confidence]):
            tags = [c.confidence]
            if c.about != "self":
                tags.append(f"about {c.about.replace('_', ' ')}")
            if c.sensitivity:
                tags.append("sensitive: " + ", ".join(c.sensitivity))
            when = c.first_seen or ""
            if c.last_seen and c.last_seen != c.first_seen:
                when += f" – {c.last_seen}"
            out.append(f"- **{c.claim}** _({'; '.join(tags)})_")
            out.append(f"  {c.detail}")
            if c.alternatives:
                out.append("  Alternatives: " + " ".join(c.alternatives))
            out.append(f"  Evidence: {', '.join(c.evidence)}" + (f" ({when})" if when else ""))
        out.append("")
    return "\n".join(out)


def write(result: SynthesisResult, out: Path) -> None:
    (out / "profile.json").write_text(
        json.dumps(
            {"summary": result.summary, "claims": [c.to_profile_dict() for c in result.claims],
             "model": result.model, "usage": result.usage, "stop_reason": result.stop_reason},
            indent=2, ensure_ascii=False,
        ),
        encoding="utf8",
    )
    (out / "profile.md").write_text(to_markdown(result.summary, result.claims), encoding="utf8")
