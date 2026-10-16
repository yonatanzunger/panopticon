# Panopticon

Shows people how much their Google activity history reveals about them.

Input is a Google Takeout export of **My Activity** (the `Search`, `Ads`, and `AI Mode`
folders). The local stages run entirely on your machine; nothing is sent anywhere.

```
uv run panopticon prepare ~/Downloads/Takeout -o out/             # everything
uv run panopticon prepare ~/Downloads/Takeout --days 120          # the last 120 days of the export
uv run panopticon prepare ~/Downloads/Takeout --since 2026-08-01 --until 2026-09-01
```

`--days` counts back from `--until`, or from the newest event in the export.

## Pipeline

| Stage | Module | Status |
|---|---|---|
| 0. Ingest: Takeout JSON → `Event`s with stable ids | `ingest.py` | done |
| 1. Compress: noise removal, sessions, compact transcript | `compress.py` | done |
| 2. Deterministic features: location fixes → timezone, home, daily rhythm, app usage | `features.py` | done |
| 3. LLM evidence extraction (place/travel atoms first) | | |
| 4. Itinerary solver and re-localization | | |
| 5. LLM synthesis of claims, with cited event ids | | |
| 6. LLM verification of each claim against its evidence | | |
| 7. Report | | |

`prepare` writes `events.jsonl` (stage 0), `transcript.txt` (stage 1), and `features.json`
(stage 2) to the output directory.

## Notes on the data

- **Ads** is not ads you clicked. It's activity Google observed through its ad network on
  third-party sites and apps; its `header` gives the site or app. In **Search**, a visit marked
  "From Google Ads" *is* an ad click (`Kind.AD_CLICK`).
- Locations are map-tile centers, labeled by source (`device`, `home`, `work`, ...).
  They are sparse: in one sample export, only 2015–2018 and 2026 had any.
- AI Mode items embed whole conversations; each of the user's follow-up prompts becomes its own
  event, and Google's responses are dropped.
- Timestamps are UTC throughout; localization happens at render time.
