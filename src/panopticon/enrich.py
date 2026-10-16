"""Enrichment: acquire additional data sources about the subject before verification.

This is the generic framework for a phase that gathers external evidence bearing on the
synthesized profile, so that verification can weigh it alongside the history. An *enricher*
takes the profile's claims and returns findings; each finding is an external evidence item
with a stable id (``W<n>``), the claim it bears on, a source, and a quoted passage.

No enrichers are implemented yet: ``ENRICHERS`` is empty, so running this phase is a noop that
writes an empty finding set. That keeps the surrounding wiring (the `enrich` command, the
`run` orchestrator's dependency graph, and verification's future consumption of ``W`` items)
in place while the first enricher is designed.
"""

import json
from pathlib import Path
from typing import Literal, NamedTuple, Protocol

from panopticon.model import Claim, Trip

Stance = Literal["supports", "contradicts", "relates"]


class Finding(NamedTuple):
    # Stable within one enrichment run, assigned here: "W0", "W1", ...
    id: str
    # The index of the claim in profile.json this finding bears on.
    claim: int
    stance: Stance
    source: str
    # The verbatim passage the source gives, which verification checks the finding against.
    passage: str
    note: str

    def to_json(self) -> dict:
        return self._asdict()


class Enricher(Protocol):
    name: str

    def run(self, claims: list[Claim], trips: list[Trip]) -> list[dict]:
        """Return findings (as dicts without an ``id``: claim, stance, source, passage, note)."""
        ...


# No enrichers yet. The first one (web research) is still being designed; until then this phase
# is a deliberate noop, so the pipeline runs end to end without an enrichment implementation.
ENRICHERS: list[Enricher] = []


class EnrichmentResult(NamedTuple):
    findings: list[Finding]
    enrichers: list[str]


def run(claims: list[Claim], trips: list[Trip]) -> EnrichmentResult:
    findings: list[Finding] = []
    for enricher in ENRICHERS:
        for raw in enricher.run(claims, trips):
            findings.append(Finding(id=f"W{len(findings)}", **raw))
    return EnrichmentResult(findings, [e.name for e in ENRICHERS])


def write(result: EnrichmentResult, out: Path) -> None:
    (out / "enrichment.json").write_text(
        json.dumps(
            {"enrichers": result.enrichers, "findings": [f.to_json() for f in result.findings]},
            indent=2, ensure_ascii=False,
        ),
        encoding="utf8",
    )


def load(path: Path) -> list[dict]:
    """The findings in an enrichment.json, or [] if there is none."""
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf8")).get("findings", [])
