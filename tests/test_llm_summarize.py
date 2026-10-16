from types import SimpleNamespace

from pydantic import BaseModel

from panopticon import llm
from panopticon.summarize import build_prompt


class Answer(BaseModel):
    text: str


class FakeStream:
    def __init__(self, chunks: list[str], stop_reason: str, parsed):
        self.text_stream = iter(chunks)
        self._message = SimpleNamespace(
            stop_reason=stop_reason, model="claude-opus-5-5", parsed_output=parsed,
            usage=SimpleNamespace(input_tokens=10, output_tokens=5),
        )

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self._message


def fake_client(stream: FakeStream, calls: list[dict]):
    def make(**kwargs):
        calls.append(kwargs)
        return stream

    return SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(stream=make)))


def test_call_streams_progress_and_sends_fallback():
    calls: list[dict] = []
    seen: list[int] = []
    client = fake_client(FakeStream(['{"te', 'xt": "hi"}'], "end_turn", Answer(text="hi")), calls)
    result = llm.call(client, model="m", effort="low", system="s", prompt="p",
                      output_format=Answer, on_output=seen.append)
    assert result.parsed == Answer(text="hi")
    assert result.usage == {"input_tokens": 10, "output_tokens": 5}
    assert seen == [4, 14]
    (kwargs,) = calls
    assert kwargs["fallbacks"] == "default"
    assert kwargs["betas"] == [llm.FALLBACK_BETA]
    assert kwargs["output_config"] == {"effort": "low"}
    assert kwargs["messages"] == [{"role": "user", "content": "p"}]


def test_call_discards_refused_or_truncated_output():
    for stop in ("refusal", "max_tokens"):
        client = fake_client(FakeStream([], stop, Answer(text="partial")), [])
        result = llm.call(client, model="m", effort="low", system="s", prompt="p",
                          output_format=Answer)
        assert result.parsed is None
        assert result.stop_reason == stop


def test_summary_prompt_uses_final_forms():
    from panopticon.model import Claim, Trip

    claim = Claim.from_dict({
        "category": "health", "claim": "Takes propranolol daily.", "detail": "d",
        "confidence": "high", "about": "self", "sensitivity": ["health"], "status": "revised",
        "final_claim": "Has looked up propranolol dosing.", "final_confidence": "low",
        "final_about": "unclear", "final_sensitivity": ["health"],
    })
    old = Claim.from_dict(claim.to_verified_dict() | {"status": "dropped"})
    trip = Trip(start="2026-08-14T07:00:00+00:00", end="2026-08-21T05:00:00+00:00",
                place="Lima, Peru", tz="America/Lima")
    prompt = build_prompt([claim, old], [trip])
    assert ("C0 [revised; low confidence; about unclear; health; sensitive: health] "
            "Has looked up propranolol dosing.") in prompt
    assert "C1 [unconfirmed;" in prompt
    assert "Takes propranolol daily." not in prompt
    assert "2026-08-14 to 2026-08-21: Lima, Peru" in prompt
