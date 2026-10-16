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
| 6. LLM verification of each claim against its evidence | | | |
| 7. Report | | | |

## TODO

- **Home that changes over time.** `features.home` picks one home for the whole range, so on a
  long history every earlier residence is treated as travel (or missed). Infer home per period,
  e.g. the dominant overnight location per month, and let the itinerary solver use it.
- **Hierarchical synthesis.** Long histories produce more facts than one synthesis request
  holds; synthesize per period (say, per year), then synthesize the period profiles.
- **Stage 6, verification.** Check each claim against only its cited source lines, and
  downgrade or drop the ones they don't support.
- **Stage 7, report.** The itinerary timeline and map, claims with click-through to source rows,
  and a sensitivity summary.
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
