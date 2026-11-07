"""The run log on disk is what's in memory, and its index keeps what recent analyses and quick checks used."""

import json

import pytest

from custom_components.assist_improver.reflective import RoleRuns, RunIndex
from kit import improver  # noqa: F401 - a fixture.


def on_disk(hass):
    with open(hass.config.path(".storage", "assist_improver.role_runs"), encoding="utf-8") as file:
        return json.load(file)["data"]["runs"]


@pytest.mark.asyncio
async def test_a_record_that_wont_serialize_is_reported_as_before_and_the_rest_still_saved(hass, caplog):
    role_runs = RoleRuns(hass)
    await role_runs.start()
    await role_runs.record({"id": "a", "kind": "quick_check", "status": "completed"})
    await role_runs.record({"id": "odd", "kind": "quick_check", "status": "completed", "thing": object()})
    assert "Error writing config for assist_improver.role_runs" in caplog.text  # The store's own report.
    assert [r["id"] for r in role_runs.runs] == ["a", "odd"]  # Kept in memory, as before.
    await role_runs.record({"id": "odd", "kind": "quick_check", "status": "completed", "thing": "fine now"})
    assert on_disk(hass) == role_runs.runs


def used(inp, out):
    return {"events": [{"input_tokens": inp, "output_tokens": out}]}


def test_the_index_keeps_what_recent_analyses_and_quick_checks_used_without_counting_one_twice():
    """The run confirmation's rough estimate: input plus output tokens, on average, over the recent analyses and quick
    checks. A smooth verdict is a quick check, not an analysis; a verdict kept from an earlier check is counted once."""
    from custom_components.assist_improver import reflective
    check = lambda inp, out, **extra: {"status": "completed", "usage": used(inp, out), **extra}  # noqa: E731
    runs = [
        {"id": "a1", "kind": "journey_analyzer", "recording_id": "r1", "status": "completed", "analysis": {"goals": []},
         "answer_status": "accepted", "usage": used(20_000, 1_000), "triage": check(3_000, 100)},
        {"id": "s1", "kind": "journey_analyzer", "recording_id": "r2", "status": "completed", "analysis": {"triaged": {}},
         "answer_status": "triaged_smooth", "triage": check(2_000, 100)},
        {"id": "h1", "kind": "journey_analyzer", "recording_id": "r3", "status": "held", "triage": check(4_000, 200)},
        {"id": "a3", "kind": "journey_analyzer", "recording_id": "r3", "status": "completed", "analysis": {"goals": []},
         "answer_status": "accepted", "usage": used(30_000, 2_000), "triage": check(4_000, 200, kept=True)},
        {"id": "f1", "kind": "journey_analyzer", "recording_id": "r4", "status": "failed", "usage": used(9_000, 0)},
        {"id": "old", "kind": "journey_analyzer", "recording_id": "r5", "status": "completed",
         "analysis": {"goals": []}, "answer_status": "accepted"},  # Trimmed by the tidy-up: no usage left.
    ]
    index = RunIndex(runs)
    assert index.spent["analyze"].average() == (21_000 + 32_000) / 2
    assert index.spent["triage"].average() == (3_100 + 2_100 + 4_200) / 3
    recent = reflective.Recent(size=2)
    for value in (10, 20, 30):
        recent.add(value)
    assert recent.average() == 25  # Only the last ones count.
    assert RunIndex().spent["analyze"].average() is None


@pytest.mark.asyncio
async def test_the_run_preview_gives_the_tokens_each_step_takes_with_the_model_it_runs_on_now(improver):
    from custom_components.assist_improver.settings import model_name
    pipeline = improver.runtime.pipeline
    assert pipeline.preview()["spend"] == {}  # No past runs: no estimate.
    await improver.runtime.role_runs.record(
        {"id": "a1", "kind": "journey_analyzer", "recording_id": "r1", "status": "completed",
         "analysis": {"goals": []}, "answer_status": "accepted", "usage": used(20_400, 1_000),
         "triage": {"status": "completed", "usage": used(3_000, 50)}})
    hass = improver.hass
    assert pipeline.preview()["spend"] == {
        "analyze": {"tokens": 21_400, "model": model_name(hass, "ai_task.opus")},
        "triage": {"tokens": 3_050, "model": model_name(hass, "ai_task.haiku")}}
    improver.entry.options = {k: v for k, v in improver.entry.options.items() if k != "triage_ai_task_entity_id"}
    assert set(pipeline.preview()["spend"]) == {"analyze"}  # No quick check now: nothing to say about one.
