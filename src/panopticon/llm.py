"""The one way pipeline stages call Claude: a streamed request with structured output and
server-side refusal fallback, reporting output progress as it arrives."""

from collections.abc import Callable
from typing import NamedTuple

import anthropic
from pydantic import BaseModel

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class Result[T: BaseModel](NamedTuple):
    # None if the request was refused, truncated, or otherwise produced nothing parseable.
    parsed: T | None
    stop_reason: str | None
    model: str | None
    usage: dict


def call[T: BaseModel](
    client: anthropic.Anthropic,
    *,
    model: str,
    effort: str,
    system: str,
    prompt: str,
    output_format: type[T],
    max_tokens: int = 64_000,
    on_output: Callable[[int], None] | None = None,
) -> Result[T]:
    """on_output, if given, is called with the number of output characters received so far."""
    with client.beta.messages.stream(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": prompt}],
        output_config={"effort": effort},
        output_format=output_format,
        betas=[FALLBACK_BETA],
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
    parsed = message.parsed_output
    if message.stop_reason in ("refusal", "max_tokens"):
        parsed = None
    return Result(parsed, message.stop_reason, message.model, usage)
