# Panopticon

Shows people how much their Google activity history reveals about them.

Input is a Google Takeout export of **My Activity** (the `Search`, `Ads`, and `AI Mode`
folders). Stages 0–2 run entirely on your machine. Stage 3 sends the compressed transcript to
the Claude API; use `--dry-run` to see exactly what would be sent first.

## Running the pipeline

Each stage reads and writes files in one output directory (`out/` by default, which is
gitignored). Run the stages in order.

### Stages 0–2: ingest, compress, features (local)

```bash
uv run panopticon prepare ~/Downloads/Takeout -o out/                 # all history
uv run panopticon prepare ~/Downloads/Takeout -o out/ --days 120      # the export's last 120 days
uv run panopticon prepare ~/Downloads/Takeout -o out/ --since 2026-08-01 --until 2026-09-01
```

- The input can be a Takeout root, its `My Activity` folder, or individual `MyActivity.json`
  files.
- `--since` / `--until` take an ISO date or datetime (UTC if no zone is given); `--until` is
  exclusive. `--days` counts back from `--until`, or from the newest event in the export, so
  repeated runs on one export select the same events.
- Event ids are assigned over the full history before filtering, so an event has the same id
  in every subset.

Writes:

- `events.jsonl`: every event in the range (stage 0)
- `transcript.txt`: the compact, session-grouped transcript, localized to the home zone; lines
  with a Google location carry a `[loc lat,lon source]` tag (stage 1)
- `features.json`: location fixes with zones, home, hour histogram, daily quiet periods, app
  usage (stage 2)

### Stage 3: evidence extraction (Claude API)

Needs Claude API credentials: `ant auth login`, or `ANTHROPIC_API_KEY` in the environment.

```bash
uv run panopticon extract out/ --dry-run      # write the exact prompts to out/chunk_*.txt, estimate cost, send nothing
uv run panopticon extract out/                # run it
uv run panopticon extract out/ --model claude-opus-5-5 --effort high --chunk-chars 240000 --concurrency 4
```

- The transcript is split into chunks of about 60k tokens (`--chunk-chars`), breaking only
  between days.
- Each chunk yields two kinds of evidence atom in one request: **places** (for the itinerary)
  and **facts** (observations and the inferences they suggest, by category, with whom they're
  about and which kinds of sensitive information they touch).
- Defaults are `claude-opus-5-5` at `medium` effort. Server-side refusal fallback is enabled,
  so a chunk declined by a safety classifier is retried on another model rather than lost.
- Progress goes to stderr: a line as each chunk starts and finishes, and every 15 seconds a
  heartbeat listing in-flight chunks with elapsed time and output received so far.
- For scale: the last 120 days of one export is 2 chunks and ~210k input tokens; places-only
  extraction cost ~$1.10 on Opus 5.5 and took a few minutes. Facts add output, so expect more.

Writes:

- `places.jsonl`: one place-evidence atom per line, citing event ids, with the place's
  coordinates and IANA zone
- `facts.jsonl`: one fact atom per line, with a stable id (`F<n>`), citing event ids
- `extract_log.jsonl`: per-chunk stop reason, serving model, token usage, and dropped atoms

### Stage 4: itinerary (local)

```bash
uv run panopticon itinerary out/
```

- Combines the `extract` atoms (if `places.jsonl` exists) with Google's location fixes, and
  works from fixes alone when there are no atoms.
- Clusters evidence into places (metro areas within 80km away from home; a tighter 25km
  around home, so a day in the city still shows), then picks the likeliest place for each
  hour. Changing places has a cost, and home gets a small baseline, so a trip needs sustained
  or strong evidence. Two sightings in the same place up to 4 days apart are bridged, unless
  something contradicts the stay. Remote-interest atoms ("weather in Paris") are ignored.
- Places are labeled by the atoms' `region` field (added to `extract` after its first runs;
  older `places.jsonl` files fall back to the specific place name).
- Tuning constants are at the top of `itinerary.py`.

Writes:

- `itinerary.json`: segments (start, end, place, coordinates, IANA zone, home or not, evidence
  weight, supporting event ids)
- `transcript_local.txt`: the transcript re-rendered with each session in the zone where the
  itinerary places the user

### Stage 5: synthesis (Claude API)

Run after `extract` and `itinerary` (without an itinerary it still works, but with no trips
and with home-zone times).

```bash
uv run panopticon synthesize out/ --dry-run   # write the exact prompt to out/synthesis_prompt.txt, estimate cost, send nothing
uv run panopticon synthesize out/             # run it
uv run panopticon synthesize out/ --model claude-opus-5-5 --effort xhigh
```

- One request that joins all fact atoms, the trips, and activity statistics (event counts,
  hour-of-day histogram, top apps, top sites) into claims. This is where evidence from months
  apart gets combined.
- Defaults to `claude-opus-5-5` at `high` effort, with refusal fallback.
- Each claim has a category, confidence, whom it's about, sensitivity kinds, the facts and
  trips it cites, and alternative explanations. Citations are checked locally: claims get
  their source event ids and the dates their evidence spans, and citations to ids that don't
  exist are moved to `unresolved_evidence`.
- Refuses ranges whose facts won't fit in one request (see TODO).

Writes:

- `profile.json`: the summary and claims
- `profile.md`: the same, readable, grouped by category

### Stage 6: verification (Claude API)

```bash
uv run panopticon verify out/ --dry-run   # write the exact prompts to out/verify_prompts.txt, estimate cost, send nothing
uv run panopticon verify out/             # run it
uv run panopticon verify out/ --effort high --concurrency 8
```

- One request per claim, showing the verifier only the claim and the raw source lines it
  cites (with local dates and times), plus activity statistics computed over the whole range:
  no fact atoms, no other claims. That way an overreach at extraction can't carry through
  synthesis into a confident claim.
- Cited lines are a sample, not every occurrence, so the verifier is told to judge frequency
  and timing ("opens DoorDash several times a day") from the statistics, which include
  monthly counts for the top apps, and never from how many lines were cited.
- The verifier rules supported, partially supported (with a narrower claim), or unsupported;
  sets the confidence the lines alone justify; can change whom the claim is about; and lists
  specific concerns.
- Each claim ends up **kept**, **revised** (narrowed, or a different confidence or subject),
  **unconfirmed**, or **unverified** (the verifier returned nothing usable, e.g. a refusal).
  Unconfirmed means the cited lines alone don't establish the claim, not that it's false:
  evidence spread across other claims can still make it a good guess, and the report shows it.
- Defaults to `claude-opus-5-5` at `medium` effort, 8 requests at a time, with refusal
  fallback. For scale: 41 claims was ~170k input tokens and ~32k output, about $1.30.

Writes:

- `verified.json`: the summary, and every claim with its verification and final form
  (`status`, `final_claim`, `final_confidence`, `final_about`, `final_sensitivity`). This is
  the main machine-readable output.

### Stage 7: final summary (Claude API)

```bash
uv run panopticon summarize out/ --dry-run    # write the exact prompt to out/summarize_prompt.txt, estimate cost, send nothing
uv run panopticon summarize out/              # run it
```

- Synthesis drafts a summary before verification, so it can repeat claims that verification
  later narrowed. This writes the report's summary from the claims' final forms only, with
  wording that follows each claim's confidence. Unconfirmed claims appear only as such.
- One small request (~7k input tokens, a few cents).

Writes:

- `summary.json`

### Stage 8: report (local)

```bash
uv run panopticon report out/                 # writes out/report.html
uv run panopticon report out/ -o ~/Desktop/my-report.html
```

- A single self-contained HTML file: no scripts, fonts, styles, or images load from anywhere,
  so opening it sends nothing off the machine. (Hence no map: map tiles would leak
  coordinates to a tile server.)
- Sections: headline numbers, the summary portrait, a chart of claims per sensitive category,
  the trip timeline and table, and every claim as a card with its status, confidence,
  subject, sensitivity, other explanations, the verifier's concerns, and its source lines
  (lines the verifier judged irrelevant are dimmed). Filters for sensitivity, confidence, and
  unconfirmed claims, which are shown by default, ranked last, and left out of the headline
  numbers.
- Uses `verified.json` if present, otherwise the unverified `profile.json`; and the final
  summary from `summary.json` if present, otherwise synthesis's draft, marked as a draft.
  Follows the system light/dark setting.

### Tests

```bash
uv run pytest
```

## Pipeline

| Stage | Module | Command | Status |
|---|---|---|---|
| 0. Ingest: Takeout JSON → `Event`s with stable ids | `ingest.py` | `prepare` | done |
| 1. Compress: noise removal, sessions, compact transcript | `compress.py` | `prepare` | done |
| 2. Deterministic features: location fixes → timezone, home, daily rhythm, app usage | `features.py` | `prepare` | done |
| 3. LLM evidence extraction: place and fact atoms | `extract.py` | `extract` | done |
| 4. Itinerary solver and re-localization | `itinerary.py` | `itinerary` | done |
| 5. LLM synthesis of claims, with cited evidence | `synthesize.py` | `synthesize` | done |
| 6. LLM verification of each claim against its cited lines | `verify.py` | `verify` | done |
| 7. Final summary from the verified claims | `summarize.py` | `summarize` | done |
| 8. Report | `report.py` | `report` | done |

## TODO

- **Home that changes over time.** `features.home` picks one home for the whole range, so on a
  long history every earlier residence is treated as travel (or missed). Infer home per period,
  e.g. the dominant overnight location per month, and let the itinerary solver use it.
- **Hierarchical synthesis.** Long histories produce more facts than one synthesis request
  holds; synthesize per period (say, per year), then synthesize the period profiles.
- **Web research (`research`, between `synthesize` and `verify`).** Find public information
  that supports or contradicts the profile, as a human researcher would, especially to test an
  identity hypothesis. Agreed design:
  - Runs before verification, so web findings become evidence the verifier weighs. The
    verifier then records two judgments per claim: what the history alone supports, and what
    the history plus the web supports. That keeps "what your history alone reveals" intact
    and measures what the web adds.
  - Web findings can confirm facts about a hypothesized identity ("X wrote this book"), not
    the link between the user and that identity. Many independent matches between the history
    and one person's public record do strengthen the identity claim, and the verifier is told
    to reason in exactly those terms.
  - Researches only claims synthesized at medium confidence or higher, so it searches less on
    hypotheses that won't hold up.
  - Plan, then guard, then search. Claude proposes queries about identity and public,
    professional information. A guard in code (not only in the prompt) rejects any query
    derived from a claim with a sensitive category, or sharing terms with one: pairing an
    identity with a sensitive inference ("<name> propranolol") leaks it to the search provider.
    `--dry-run` lists the exact queries.
  - Search with Claude's server-side web search, **one request per approved query, containing
    nothing but that query**. The model may rephrase while it searches, but it never sees the
    profile, so it has no sensitive inference to add. Afterwards, the queries it actually ran
    are compared against the approved ones and any differences logged.
  - Findings become evidence items (`W<n>`: URL plus the verbatim passage the search's citation
    quotes), each tied to the claim it bears on. Verification checks them in the same blind pass
    as history lines, so there's no separate findings check.
  - The report shows web evidence separately from history evidence, with links.
  - Later: an optional loop (research → re-synthesize with findings as context → verify), since
    knowing who someone is changes how their history reads.
- **Inferability scoring.** For each claim, how much history it takes to infer it.

## Notes on the data

- **Ads** is not ads you clicked. It's activity Google observed through its ad network on
  third-party sites and apps; its `header` gives the site or app. In **Search**, a visit marked
  "From Google Ads" *is* an ad click (`Kind.AD_CLICK`).
- Locations are map-tile centers, labeled by source (`device`, `home`, `work`, ...).
  They are sparse: in one sample export, only 2015–2018 and 2026 had any.
- AI Mode items embed whole conversations; each of the user's follow-up prompts becomes its own
  event, and Google's responses are dropped.
- Timestamps are UTC throughout; localization happens at render time.
