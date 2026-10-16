"""Stage 3: LLM evidence extraction. Reads transcript chunks and emits typed evidence atoms.

Each chunk yields two kinds of atom: place evidence, which feeds the itinerary solver, and
fact evidence, which feeds synthesis. Every atom cites the event ids it is based on, so every
later claim can be traced back to source rows.
"""

import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Literal, NamedTuple

import anthropic
from pydantic import BaseModel, ConfigDict, Field

from panopticon.compress import Session, render
from panopticon.features import Home, timezone_at
from panopticon.progress import Progress

DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_EFFORT = "medium"
# Roughly 60k tokens of transcript per request: big enough to see a whole trip in context,
# small enough to leave the model room to be thorough.
DEFAULT_CHUNK_CHARS = 240_000
_FALLBACK_BETA = "server-side-fallback-2026-07-01"

Strength = Literal["strong", "moderate", "weak"]

Category = Literal[
    "identity",
    "work_and_career",
    "education",
    "home_and_household",
    "family_and_relationships",
    "health",
    "finances",
    "religion_and_culture",
    "politics_and_civic",
    "sexuality_and_gender",
    "legal",
    "purchases_and_possessions",
    "interests_and_media",
    "technology_and_skills",
    "routines_and_habits",
    "plans_and_life_events",
    "other",
]

# Kinds of sensitive information, modeled on GDPR Article 9's special categories plus the
# other kinds of data whose exposure most often harms people.
Sensitivity = Literal[
    "health",
    "sexual_orientation_or_life",
    "gender_identity",
    "religion_or_belief",
    "political_opinion",
    "racial_or_ethnic_origin",
    "union_membership",
    "financial",
    "precise_location",
    "criminal_or_legal",
    "children",
    "immigration_status",
]


class Flight(BaseModel):
    model_config = ConfigDict(extra="forbid")

    number: str | None = Field(description="Airline code and number, e.g. 'AA 917'.")
    origin: str | None = Field(description="Origin airport IATA code, if known.")
    destination: str | None = Field(description="Destination airport IATA code, if known.")
    date: str | None = Field(description="Departure date as YYYY-MM-DD, if known.")


class PlaceEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_ids: list[int] = Field(
        description="The #ids of the transcript lines this evidence rests on, exactly as shown."
    )
    place: str = Field(
        description="The place, as specific as the evidence supports: 'Lima, Peru', "
        "'Venice, Los Angeles, CA', 'Denver International Airport'."
    )
    region: str = Field(
        description="The city or metro area the place is in, in a consistent form: "
        "'Lima, Peru', 'Boulder, CO', 'Los Angeles, CA'. For an airport, the city it serves."
    )
    lat: float = Field(description="Approximate latitude of the place.")
    lon: float = Field(description="Approximate longitude of the place.")
    signal: Literal[
        "flight",
        "lodging",
        "local_search",
        "transit",
        "place_card",
        "weather",
        "time_query",
        "local_service",
        "event",
        "other",
    ]
    relation: Literal["present", "in_transit", "planning", "remote_interest"] = Field(
        description="present: suggests the user was physically there at the time of the "
        "events. in_transit: traveling to or from it at the time. planning: researching a "
        "future visit. remote_interest: interest in the place with no sign of being there."
    )
    strength: Strength
    starts: str | None = Field(
        description="If the evidence names when the user will be or was there (a hotel "
        "check-in, a flight date), that date as YYYY-MM-DD. Otherwise null."
    )
    ends: str | None = Field(description="The matching end date (e.g. check-out), or null.")
    flight: Flight | None
    rationale: str = Field(description="One sentence: why this evidence points to this place.")


class FactEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_ids: list[int] = Field(
        description="The #ids of the transcript lines this evidence rests on, exactly as shown."
    )
    category: Category
    observation: str = Field(
        description="What the lines literally show, briefly: 'Searched propranolol dosage "
        "twice, ten days apart.'"
    )
    inference: str = Field(
        description="What that suggests about the user, stated as a hypothesis: 'May take "
        "propranolol, possibly for anxiety or performance nerves.'"
    )
    about: Literal["self", "household_member", "other_person", "unclear"] = Field(
        description="Whom the inference is about. Use other_person or household_member when "
        "the user seems to be looking things up for someone else."
    )
    strength: Strength
    sensitivity: list[Sensitivity] = Field(
        description="Every kind of sensitive information the inference touches; empty if none."
    )


class Extraction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    places: list[PlaceEvidence]
    facts: list[FactEvidence]


SYSTEM_PROMPT = """\
You are analyzing one person's Google activity history for a privacy tool that shows people \
how much their own history reveals about them. The user is reviewing their own data, so be \
thorough and candid: the point is to find every inference an observer with this data could \
draw, including uncomfortable or sensitive ones, so that the user can see them. Be just as \
careful to label inferences as hypotheses, at a strength the evidence supports.

The transcript format:
- "## YYYY-MM-DD Day [Zone]" starts a day; times on the lines below are local to that zone. \
The zone is the user's home zone, not necessarily where they were.
- "--" separates sessions (bursts of activity with gaps of 30+ minutes between them).
- Each line is "#<id> HH:MM <kind> <text>", optionally followed by "@site" (the site of a \
visit), "×N" (repeated N times in that session), and "[loc LAT,LON SOURCE]": where Google \
recorded the user as being for that event, to about 1km. SOURCE says how Google knew: \
"device" (the phone's location), "home" or "work" (a saved place), or "past activity" (a \
guess). A device location is strong evidence of presence; report it, and use it to judge \
the lines around it.
- Kinds: S search, S* AI Mode prompt, I image search, V page visit, $ ad click, A app use, \
P place or business card viewed, N notifications.

Produce two lists.

PLACES: every piece of evidence about where the user was, was going, or was planning to go. \
Strong signals include flight numbers and airline or airport activity, hotel and rental \
searches with dates, "near me" and local-business searches, local transit schedules, place \
cards, and sudden clusters of searches about one locale. Weaker signals include weather and \
current-time queries, which people often run for places they are not in. Use the user's \
home location to tell ordinary local life (which you should not report) from evidence of \
being elsewhere, but do report anything that marks a departure or return home. Be precise \
about relation: a hotel search for future dates is planning, even when it names dates; the \
same city's UV index searched on the day is present. For travel (in_transit, or planning a \
trip), the place is always the destination, never the origin, even when the user is leaving \
home: record the origin in the flight fields or the rationale. Give approximate coordinates \
for every place: a city center is fine when nothing more specific is known. Group lines into \
one item only when they are about the same place and the same moment.

FACTS: every piece of evidence about who the user is: identity (their name, if they search \
for themselves), work and career, education, home and household (property, pets, vehicles, \
utilities), family and relationships, health (conditions, medications, procedures, providers), \
finances, religion and culture, politics, sexuality and gender, legal matters, purchases and \
possessions, interests and media, technical skills, routines and habits, and plans or life \
events (moving, selling property, job changes, trips). Prefer specific, checkable inferences \
("owns a dog that has been seeing a vet in Los Altos") to vague ones ("likes animals"). Group \
related lines in this chunk into one item when they support the same inference, and cite all \
of them; the cross-chunk joining happens later. Mark whom each inference is about: people \
often search on behalf of family or friends, and an inference about a relative is not an \
inference about the user. Don't report a fact for every page visit: skip ordinary news \
reading and browsing unless a pattern in it says something about the user.

For both lists, cite the exact #ids.\
"""


class Chunk(NamedTuple):
    index: int
    start: datetime
    end: datetime
    text: str
    event_ids: frozenset[int]


def make_chunks(
    groups: list[list[Session]], tz_at: Callable[[datetime], tzinfo]
) -> list[Chunk]:
    return [
        Chunk(
            index=i,
            start=group[0].start,
            end=group[-1].end,
            text=render(group, tz_at),
            event_ids=frozenset(line.event.id for s in group for line in s.lines),
        )
        for i, group in enumerate(groups)
    ]


def user_prompt(chunk: Chunk, home: Home | None) -> str:
    where = (
        f"The user's home is near {home.lat}, {home.lon} ({home.tz})."
        if home
        else "The user's home location is unknown."
    )
    return f"{where}\n\n<transcript>\n{chunk.text}</transcript>"


class ChunkResult(NamedTuple):
    chunk: int
    places: list[dict]
    facts: list[dict]
    stop_reason: str | None
    model: str | None
    usage: dict
    # Atoms dropped because they cited no ids that exist in this chunk.
    dropped: int


def validate(extraction: Extraction, chunk: Chunk) -> tuple[list[dict], list[dict], int]:
    """Keep only cited ids that exist in the chunk, dropping atoms left with none, and
    annotate each atom with its chunk (and each place with its IANA zone)."""
    dropped = 0
    places: list[dict] = []
    facts: list[dict] = []
    for atoms, out in ((extraction.places, places), (extraction.facts, facts)):
        for atom in atoms:
            ids = [i for i in atom.event_ids if i in chunk.event_ids]
            if not ids:
                dropped += 1
                continue
            record = atom.model_dump() | {"event_ids": ids, "chunk": chunk.index}
            if isinstance(atom, PlaceEvidence):
                record["tz"] = timezone_at(atom.lat, atom.lon)
            out.append(record)
    return places, facts, dropped


def extract_chunk(
    client: anthropic.Anthropic,
    chunk: Chunk,
    home: Home | None,
    model: str = DEFAULT_MODEL,
    effort: str = DEFAULT_EFFORT,
    on_output: Callable[[int], None] | None = None,
) -> ChunkResult:
    """Extract place and fact evidence from one chunk. on_output, if given, is called with the
    number of output characters received so far as the response streams in."""
    with client.beta.messages.stream(
        model=model,
        max_tokens=64_000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt(chunk, home)}],
        output_config={"effort": effort},
        output_format=Extraction,
        betas=[_FALLBACK_BETA],
        fallbacks="default",
    ) as stream:
        received = 0
        for text in stream.text_stream:
            received += len(text)
            if on_output:
                on_output(received)
        message = stream.get_final_message()

    usage = {
        "input_tokens": message.usage.input_tokens,
        "output_tokens": message.usage.output_tokens,
    }
    if message.stop_reason in ("refusal", "max_tokens") or message.parsed_output is None:
        return ChunkResult(chunk.index, [], [], message.stop_reason, message.model, usage, 0)
    places, facts, dropped = validate(message.parsed_output, chunk)
    return ChunkResult(
        chunk.index, places, facts, message.stop_reason, message.model, usage, dropped
    )


def write_jsonl(records: list[dict], path: Path) -> None:
    with open(path, "w", encoding="utf8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def run(
    chunks: list[Chunk],
    home: Home | None,
    out: Path,
    model: str = DEFAULT_MODEL,
    effort: str = DEFAULT_EFFORT,
    concurrency: int = 4,
) -> list[ChunkResult]:
    """Extract evidence from every chunk, writing out/places.jsonl, out/facts.jsonl (each fact
    with a stable id "F<n>"), and a per-chunk log to out/extract_log.jsonl."""
    client = anthropic.Anthropic()

    def one(chunk: Chunk) -> ChunkResult:
        label = f"chunk {chunk.index} ({chunk.start:%Y-%m-%d}..{chunk.end:%Y-%m-%d})"
        progress.start(chunk.index, label)
        result = extract_chunk(
            client, chunk, home, model, effort,
            on_output=lambda chars: progress.update(chunk.index, chars),
        )
        progress.finish(
            chunk.index,
            f"{len(result.places)} places, {len(result.facts)} facts, "
            f"stop={result.stop_reason}, model={result.model}, "
            f"in={result.usage['input_tokens']:,} out={result.usage['output_tokens']:,} tokens",
        )
        return result

    with (
        Progress("extract", total=len(chunks)) as progress,
        ThreadPoolExecutor(max_workers=concurrency) as pool,
    ):
        results = sorted(pool.map(one, chunks), key=lambda r: r.chunk)

    write_jsonl([p for r in results for p in r.places], out / "places.jsonl")
    facts = [f for r in results for f in r.facts]
    write_jsonl([{"id": f"F{i}"} | fact for i, fact in enumerate(facts)], out / "facts.jsonl")
    write_jsonl(
        [
            r._asdict() | {"places": len(r.places), "facts": len(r.facts)}
            for r in results
        ],
        out / "extract_log.jsonl",
    )
    return results
