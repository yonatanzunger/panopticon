"""The phase graph and the `run` orchestrator.

Each phase declares the source modules that implement it, the files it reads, and the files it
writes. `pipeline.json` in the output directory records, per phase, when it last ran and the
hashes of its modules and input files at that time. A phase is stale when it has never run, an
output is missing, its module code changed, or an input changed. Because one phase's outputs
are the next phase's inputs, re-running an upstream phase makes every dependent phase stale, so
`run` refreshes exactly the phases that need it, in order, up to `report`.

The first phase, `prepare`, also reads files outside the directory: the Takeout export named
in `inputs.json`. Those are hashed as inputs too, so a new export re-runs everything.

This module is pure: it does not run phases itself. The caller passes a map from phase name to
a callable that runs it, which keeps the dependency graph independent of the CLI.
"""

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple

from panopticon import prepare

_SRC = Path(__file__).parent
CONFIG = "pipeline.json"


class Phase(NamedTuple):
    name: str
    # Source files, or packages (every .py under them), relative to this package.
    modules: tuple[str, ...]
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    # Input files outside the directory, as a function of the directory.
    external: Callable[[Path], list[Path]] | None = None


# In dependency order. `enrich` is the noop enrichment phase; `verify` lists enrichment.json as
# an input so it re-runs once an enricher produces findings.
PHASES: tuple[Phase, ...] = (
    Phase("prepare",
          ("prepare.py", "ingest.py", "sources", "events.py", "compress.py", "features.py"),
          (prepare.SETTINGS,),
          ("events.jsonl", "transcript.txt", "features.json"),
          external=prepare.source_files),
    Phase("extract", ("extract.py", "llm.py", "compress.py"),
          ("events.jsonl", "features.json"),
          ("places.jsonl", "facts.jsonl", "extract_log.jsonl")),
    Phase("itinerary", ("itinerary.py",),
          ("events.jsonl", "features.json", "places.jsonl"),
          ("itinerary.json", "transcript_local.txt")),
    Phase("synthesize", ("synthesize.py", "llm.py"),
          ("events.jsonl", "features.json", "facts.jsonl", "itinerary.json"),
          ("profile.json",)),
    Phase("enrich", ("enrich.py",),
          ("profile.json",),
          ("enrichment.json",)),
    Phase("verify", ("verify.py", "llm.py", "synthesize.py"),
          ("events.jsonl", "features.json", "profile.json", "itinerary.json", "enrichment.json"),
          ("verified.json",)),
    Phase("summarize", ("summarize.py", "llm.py"),
          ("events.jsonl", "features.json", "verified.json", "itinerary.json"),
          ("summary.json",)),
    Phase("report", ("report.py", "compress.py"),
          ("events.jsonl", "features.json", "verified.json", "profile.json", "summary.json",
           "itinerary.json", "extract_log.jsonl"),
          ("report.html",)),
)


def _hash_file(path: Path) -> str | None:
    if not path.exists():
        return None
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _code_hash(phase: Phase) -> str:
    h = hashlib.sha256()
    for module in phase.modules:
        path = _SRC / module
        files = sorted(path.rglob("*.py")) if path.is_dir() else [path]
        for file in files:
            h.update((_hash_file(file) or "").encode())
    return h.hexdigest()


def signature(phase: Phase, directory: Path) -> dict:
    """The phase's current code hash and the hash of each of its input files (None if absent).
    External inputs are keyed by absolute path."""
    inputs = {name: _hash_file(directory / name) for name in phase.inputs}
    if phase.external:
        inputs |= {str(path): _hash_file(path) for path in phase.external(directory)}
    return {"code": _code_hash(phase), "inputs": inputs}


def is_stale(phase: Phase, directory: Path, config: dict) -> bool:
    recorded = config.get("phases", {}).get(phase.name)
    if recorded is None:
        return True
    if any(not (directory / out).exists() for out in phase.outputs):
        return True
    current = signature(phase, directory)
    return recorded.get("code") != current["code"] or recorded.get("inputs") != current["inputs"]


def plan(directory: Path, config: dict, force: bool = False) -> list[Phase]:
    """The phases that need to run, in order. A phase runs if it is stale, or if one of its
    inputs is an output of a phase already scheduled to run this time (so a refresh cascades)."""
    scheduled: set[str] = set()
    scheduled_outputs: set[str] = set()
    todo: list[Phase] = []
    for phase in PHASES:
        if (
            force
            or is_stale(phase, directory, config)
            or any(name in scheduled_outputs for name in phase.inputs)
        ):
            todo.append(phase)
            scheduled.add(phase.name)
            scheduled_outputs.update(phase.outputs)
    return todo


def load_config(directory: Path) -> dict:
    path = directory / CONFIG
    return json.loads(path.read_text(encoding="utf8")) if path.exists() else {}


def save_config(directory: Path, config: dict) -> None:
    (directory / CONFIG).write_text(json.dumps(config, indent=2), encoding="utf8")


_BY_NAME = {phase.name: phase for phase in PHASES}


def record(directory: Path, name: str) -> None:
    """Mark one phase as just run, by storing its current signature. Called at the end of each
    phase command, so running a phase on its own keeps the run state accurate and `run` won't
    redo it."""
    phase = _BY_NAME[name]
    config = load_config(directory)
    config.setdefault("phases", {})[name] = signature(phase, directory) | {
        "ran_at": datetime.now(UTC).isoformat()
    }
    save_config(directory, config)


def adopt(directory: Path, config: dict) -> list[str]:
    """Record the current signature of every phase whose outputs already exist, as if it had
    just run. For a directory populated before tracking began, so `run` won't redo finished
    work. Returns the names of the phases adopted."""
    adopted: list[str] = []
    for phase in PHASES:
        if all((directory / out).exists() for out in phase.outputs):
            config.setdefault("phases", {})[phase.name] = signature(phase, directory) | {
                "ran_at": datetime.now(UTC).isoformat(), "adopted": True
            }
            adopted.append(phase.name)
    return adopted


def run_pipeline(
    directory: Path,
    runners: dict[str, Callable[[Path], None]],
    force: bool = False,
    plan_only: bool = False,
    log: Callable[[str], None] = print,
) -> list[Phase]:
    """Run every stale phase in order, recording each phase's signature after it writes its
    outputs. Returns the phases that ran (or, with plan_only, that would run).

    The plan assumes every scheduled phase changes its outputs. Running, each phase is checked
    again just before its turn, so one whose upstream rewrote identical files is skipped: a
    re-run `prepare` that reproduces the same events costs no LLM phases."""
    config = load_config(directory)
    todo = plan(directory, config, force)
    if not todo:
        log("everything up to date")
        return []
    log(("would run: " if plan_only else "will run: ") + ", ".join(p.name for p in todo))
    if plan_only:
        return todo
    ran: list[Phase] = []
    for phase in todo:
        if not force and not is_stale(phase, directory, config):
            log(f"=== {phase.name}: inputs unchanged, skipping ===")
            continue
        log(f"=== {phase.name} ===")
        runners[phase.name](directory)
        config.setdefault("phases", {})[phase.name] = signature(phase, directory) | {
            "ran_at": datetime.now(UTC).isoformat()
        }
        save_config(directory, config)
        ran.append(phase)
    return ran
