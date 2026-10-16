from pathlib import Path

from panopticon import pipeline
from panopticon.pipeline import PHASES, Phase, adopt, is_stale, plan, signature


def all_files() -> set[str]:
    files: set[str] = set()
    for ph in PHASES:
        files.update(ph.inputs)
        files.update(ph.outputs)
    return files


def populate(directory: Path) -> None:
    for name in all_files():
        (directory / name).write_text("seed", encoding="utf8")


def test_plan_runs_everything_when_nothing_recorded(tmp_path):
    populate(tmp_path)
    assert [p.name for p in plan(tmp_path, {})] == [p.name for p in PHASES]


def test_adopt_then_up_to_date(tmp_path):
    populate(tmp_path)
    config: dict = {}
    assert adopt(tmp_path, config) == [p.name for p in PHASES]
    assert plan(tmp_path, config) == []


def test_changing_an_input_cascades_downstream(tmp_path):
    populate(tmp_path)
    config: dict = {}
    adopt(tmp_path, config)
    # events.jsonl feeds extract; the change must flow all the way to report.
    (tmp_path / "events.jsonl").write_text("changed", encoding="utf8")
    names = [p.name for p in plan(tmp_path, config)]
    assert names == ["extract", "itinerary", "synthesize", "enrich", "verify", "summarize",
                     "report"]


def test_changing_a_midstream_output_reruns_only_dependents(tmp_path):
    populate(tmp_path)
    config: dict = {}
    adopt(tmp_path, config)
    # profile.json is synthesize's output and enrich/verify/report's input. Editing it directly
    # leaves synthesize current but makes its consumers stale.
    (tmp_path / "profile.json").write_text("changed", encoding="utf8")
    names = [p.name for p in plan(tmp_path, config)]
    assert "synthesize" not in names
    assert names == ["enrich", "verify", "summarize", "report"]


def test_missing_output_is_stale(tmp_path):
    populate(tmp_path)
    config: dict = {}
    adopt(tmp_path, config)
    (tmp_path / "report.html").unlink()
    assert [p.name for p in plan(tmp_path, config)] == ["report"]


def test_code_change_makes_phase_stale(tmp_path, monkeypatch):
    module = tmp_path / "fake_mod.py"
    module.write_text("x = 1", encoding="utf8")
    (tmp_path / "in.txt").write_text("a", encoding="utf8")
    (tmp_path / "out.txt").write_text("b", encoding="utf8")
    monkeypatch.setattr(pipeline, "_SRC", tmp_path)
    phase = Phase("fake", ("fake_mod.py",), ("in.txt",), ("out.txt",))
    config = {"phases": {"fake": signature(phase, tmp_path)}}
    assert not is_stale(phase, tmp_path, config)
    module.write_text("x = 2", encoding="utf8")
    assert is_stale(phase, tmp_path, config)


def test_force_runs_all_even_when_current(tmp_path):
    populate(tmp_path)
    config: dict = {}
    adopt(tmp_path, config)
    assert [p.name for p in plan(tmp_path, config, force=True)] == [p.name for p in PHASES]
