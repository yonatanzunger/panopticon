# Panopticon

This is a tool to help understand what and how much your Google activity history reveals about
you.

Input is a Google Takeout export of **My Activity** (the `Search`, `Ads`, and `AI Mode`
folders). The local phases run entirely on your machine; the LLM phases send the compressed
transcript and derived facts to the Claude API (every one has a `--dry-run` that shows exactly
what would be sent).

**WARNING:** The entire point of this software is to process very sensitive data and make sensitive
inferences. Only use this on your own data, and very carefully control who has access to this. Do
not run this, or ever store Google Takeout data, on a machine you do not personally control. The
data you process will be sent to Claude, so you will have to make a trust decision before using it;
if someone wants to update this code to allow using other LLMs, including local ones, that would
probably be a good idea.

## Running it

### What you need to begin

This software doesn't come with a nice user interface -- it was hacked up in an afternoon. If
someone wants to make this nicer to use, that would also be a good idea.

* You need a Google account with data you want to analyze. If you don't already have this, there's
  no point in running this.
* You need a Claude API account with [API access configured](https://platform.claude.com/docs/en/get-api-key) and
  [authenticated](https://platform.claude.com/docs/en/cli-sdks-libraries/cli/authentication). (You can
  use either the `ant auth login` or `ANTHROPIC_API_KEY` method; either works)
* You need to install this git repo on your computer, either using
  `git clone https://github.com/yonatanzunger/panopticon.git` (if you have git and know how to use
  it) or by downloading https://github.com/yonatanzunger/panopticon/archive/refs/heads/main.zip .
* If you haven't used Python on your computer before, you'll need to install
  [uv](https://docs.astral.sh/uv/getting-started/installation/).
* You'll need to know how to use the appropriate terminal app on your computer to actually run all
  those commands.

### Get a Google Takeout export

Go to takeout.google.com and create a new export. Start with a "deselect all" and just choose the
entries you want to explore.

* If you just want to look at your search history and similar activity logs, select "My Activity."
  Under that, click on "Multiple formats" and request the activity records in JSON (rather than
  HTML) format.
* If you want to include your web browsing information, also select "Chrome."
* If you want to include your location history, also select "Timeline."
* If you want to include your YouTube video history, also select "YouTube and YouTube Music," then
  select "All YouTube Data Included." Uncheck "music (library and uploads)" and "videos." Next,
  check "Multiple Formats" and scroll down to "history;" change that to "JSON."

Then select "next step" and "create export." You'll receive an email when your export is ready,
usually within minutes but it may take up to a few days. Download the export and unzip it.

### Running the analysis

From the `panopticon` directory, run

```bash
uv run panopticon run [outdir] -i [takeoutdir] --days 120
```

where you replace `[outdir]` with the directory where you want to put your results, and
`[takeoutdir]` with the name of the unzipped directory of your Takeout files. `--days 120` means to
analyze the last 120 days of data; you can pick any number you want, or omit this to analyze your
entire history.

### Looking at your results

In your output directory, you'll find a file called `report.html` -- double-click on it to see a
summary of what was learned. Other files in that directory will include deeper details.

## Appendix: for developers

### Tests

```bash
uv run python -m pytest
```

(`uv run pytest` can fail to start in some virtualenvs; `python -m pytest` always works.)

### The pipeline

| Stage | Module | Command | Status |
|---|---|---|---|
| 0. Ingest: Takeout files → `Event`s with stable ids | `ingest.py`, `sources/` | `prepare` | done |
| 1. Compress: noise removal, sessions, compact transcript | `compress.py` | `prepare` | done |
| 2. Deterministic features: location fixes → timezone, home, daily rhythm, app usage | `features.py` | `prepare` | done |
| 3. LLM evidence extraction: place and fact atoms | `extract.py` | `extract` | done |
| 4. Itinerary solver and re-localization | `itinerary.py` | `itinerary` | done |
| 5. LLM synthesis of claims (hierarchical when large), with cited evidence | `synthesize.py` | `synthesize` | done |
| 5b. Enrichment framework (gather external evidence) | `enrich.py` | `enrich` | noop |
| 6. LLM verification of each claim against its cited lines | `verify.py` | `verify` | done |
| 7. Final summary from the verified claims | `summarize.py` | `summarize` | done |
| 8. Report | `report.py` | `report` | done |

Typed objects (`model.py`: `Claim`, `Trip`) are passed between phases in memory and serialized
to JSON on disk. Everything lives in one output directory (`out/` is gitignored).

### `run` and phase freshness

`run` executes every phase whose code or inputs have changed since it last ran, in order, up
to `report`, and skips the rest. So after editing a prompt or pointing at a new export, `run`
again redoes only what's affected. An LLM phase that re-runs costs money, so check first with
`--plan`. A phase whose upstream re-ran but wrote identical files is skipped, so `--plan` can
list more than actually runs. The orchestrator is `pipeline.py`; it tracks state in
`out/pipeline.json`.

```bash
uv run panopticon run out/ --plan     # list the phases that are stale; run nothing
uv run panopticon run out/ --force    # re-run every phase
uv run panopticon run out/ --adopt    # mark the current outputs as up-to-date without running
```

Use `--adopt` on a directory whose phases you ran by hand before `run` existed, so `run` won't
redo finished work. A directory prepared before `prepare` became a phase has no record of its
export: pass `-i` (and the same range as before) once, and if the events come out the same,
nothing downstream re-runs.

### Running individual phases

You can run any phase on its own: useful for iterating on one stage, passing non-default
options (`--model`, `--effort`, `--dry-run`), or inspecting its output. **Running a phase
directly also updates the run state**, so a later `run` won't redo it. Each phase reads the
output directory and writes back into it.

#### prepare: ingest, compress, features (local)

```bash
uv run panopticon prepare out/ -i ~/Downloads/Takeout                 # all history
uv run panopticon prepare out/ -i ~/Downloads/Takeout --days 120      # the export's last 120 days
uv run panopticon prepare out/ --since 2026-08-01 --until 2026-09-01  # same export, new range
uv run panopticon prepare out/                                        # re-run with the recorded settings
```

- `-i` / `--input` can be a Takeout root, any folder inside one, or a single file, and can be
  repeated. Every file under it that a reader in `sources/` recognizes is read; the rest are
  ignored. Today that's `MyActivity.json` (from any product's `My Activity` folder).
- The inputs and range are recorded in `out/inputs.json`. `run` takes the same flags. Passing
  any of them replaces the recorded range; leaving out `-i` keeps the recorded inputs. The
  export files are hashed as inputs, so `run` notices if they change.
- `--since` / `--until` take an ISO date or datetime (UTC if no zone is given); `--until` is
  exclusive. `--days` counts back from `--until`, or from the newest event in the export, so
  repeated runs on one export select the same events.
- Event ids are assigned over the full history before filtering, so an event has the same id
  in every subset. (Adding a new kind of input changes the history, and so the ids.)
- To read a new kind of export file, add a module to `sources/` that exports a `SOURCE` (a
  name, a test for which files it reads, and a parser from a file to `Item`s), and list it in
  `sources.SOURCES`. `sources` has JSON and CSV readers.

Writes:

- `inputs.json`: the export and range to read
- `events.jsonl`: every event in the range (stage 0)
- `transcript.txt`: the compact, session-grouped transcript, localized to the home zone; lines
  with a Google location carry a `[loc lat,lon source]` tag (stage 1)
- `features.json`: location fixes with zones, home, hour histogram, daily quiet periods, app
  usage (stage 2)

#### extract: evidence extraction (Claude API)

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

#### itinerary (local)

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

#### synthesize (Claude API)

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
- **Hierarchical for long histories.** When the facts exceed one request, synthesis runs once
  per time window and then merges the per-window claims into one profile, unioning their
  evidence ids. Evidence ids are global, so citations survive the merge.

Writes:

- `profile.json`: the summary and claims
- `profile.md`: the same, readable, grouped by category

#### enrich (local; noop for now)

```bash
uv run panopticon enrich out/
```

- The framework for a phase that gathers external evidence about the subject (e.g. public web
  pages) for verification to weigh. No enrichers are implemented yet, so this writes an empty
  `enrichment.json`. The design is in the TODO list below.

Writes:

- `enrichment.json`: `{"enrichers": [...], "findings": [...]}` (currently empty)

#### verify (Claude API)

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

#### summarize (Claude API)

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

#### report (local)

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
- Writing to `-o` somewhere else doesn't update the run state; only the default
  `report.html` is tracked.

### Notes on the data

- **Ads** is not ads you clicked. It's activity Google observed through its ad network on
  third-party sites and apps; its `header` gives the site or app. In **Search**, a visit marked
  "From Google Ads" *is* an ad click (`Kind.AD_CLICK`).
- Locations are map-tile centers, labeled by source (`device`, `home`, `work`, ...).
  They are sparse: in one sample export, only 2015–2018 and 2026 had any.
- AI Mode items embed whole conversations; each of the user's follow-up prompts becomes its own
  event, and Google's responses are dropped.
- Timestamps are UTC throughout; localization happens at render time.

### TODO

- **More Takeout sources.** Chrome history, Timeline, and YouTube, as modules in `sources/`.
  The same activity can appear both in My Activity and in a product's own export, so these
  will need de-duplication; Timeline's visits span time rather than being instants.
- **Home that changes over time.** `features.home` picks one home for the whole range, so on a
  long history every earlier residence is treated as travel (or missed). Infer home per period,
  e.g. the dominant overnight location per month, and let the itinerary solver use it.
- **Web research (the first `enrich` enricher, between `synthesize` and `verify`).** Find public
  information that supports or contradicts the profile, as a human researcher would, especially
  to test an identity hypothesis. Agreed design:
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
