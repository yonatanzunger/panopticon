"""Final summary: a portrait of the user written from the verified claims.

Synthesis drafts a summary too, but before verification, so it can repeat claims that were
later narrowed or left unconfirmed. This stage writes the summary the report shows, from the
claims' final forms and confidences only.
"""

import json
from pathlib import Path
from typing import NamedTuple

import anthropic
from pydantic import BaseModel, ConfigDict, Field

from panopticon import llm
from panopticon.model import Claim, Trip
from panopticon.progress import Progress

DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_EFFORT = "medium"


class Summary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paragraphs: list[str] = Field(
        description="Three to five paragraphs: who the person appears to be, their work, home "
        "and life, and the most sensitive things the history suggests."
    )


SYSTEM_PROMPT = """\
You are writing the summary at the top of a privacy report that shows people what their own \
Google activity history reveals about them. Below are claims that have already been checked \
against the history, each with a status and confidence, and trips reconstructed from the \
history. Write the portrait an observer with this data could assemble.

Rules:
- Use only the claims and trips given. Add nothing, and don't strengthen anything.
- Let wording follow confidence: state high-confidence claims plainly; use "appears to" or \
"likely" for medium; "may" or "possibly" for low.
- Claims marked unconfirmed weren't supported by their cited lines alone, though an observer \
might still guess them. Mention them only as such, briefly, if at all.
- Attribute claims about someone else to that person ("someone close to the user"), never to \
the user.
- Refer to the person as "the user" or "they", unless a high-confidence claim establishes \
something else.
- Include the sensitive inferences: they are what the user most needs to see. Keep each at \
its stated confidence.
- Write plainly, without hype. No headings or lists: paragraphs only.\
"""


def build_prompt(claims: list[Claim], trips: list[Trip]) -> str:
    lines = ["<claims>"]
    for i, c in enumerate(claims):
        sensitive = ", ".join(c.final_sensitivity or []) or "none"
        lines.append(
            f"C{i} [{c.status}; {c.final_confidence} confidence; "
            f"about {c.final_about}; {c.category}; sensitive: {sensitive}] {c.final_claim}"
        )
    lines.append("</claims>\n\n<trips>")
    for t in trips:
        lines.append(f"{t.start[:10]} to {t.end[:10]}: {t.place}")
    lines.append("</trips>")
    return "\n".join(lines)


class SummaryResult(NamedTuple):
    summary: str
    stop_reason: str | None
    model: str | None
    usage: dict


def run(prompt: str, model: str = DEFAULT_MODEL, effort: str = DEFAULT_EFFORT) -> SummaryResult:
    client = anthropic.Anthropic()
    with Progress("summarize", total=1) as progress:
        progress.start(0, "summary")
        result = llm.call(
            client, model=model, effort=effort, system=SYSTEM_PROMPT, prompt=prompt,
            output_format=Summary, max_tokens=16_000,
            on_output=lambda chars: progress.update(0, chars),
        )
        progress.finish(0, f"stop={result.stop_reason}, in={result.usage['input_tokens']:,} "
                           f"out={result.usage['output_tokens']:,} tokens")
    text = "\n".join(result.parsed.paragraphs) if result.parsed else ""
    return SummaryResult(text, result.stop_reason, result.model, result.usage)


def write(result: SummaryResult, out: Path) -> None:
    (out / "summary.json").write_text(
        json.dumps(result._asdict() | {"written_from": "verified.json"}, indent=2,
                   ensure_ascii=False),
        encoding="utf8",
    )
