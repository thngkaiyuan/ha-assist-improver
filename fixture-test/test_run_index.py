"""The run log: on disk what's in memory, a record recorded again moved to the end, and what the pipeline reads from it
(the tokens recent analyses and quick checks used, a conversation's latest held verdict)."""

from datetime import timedelta
import json
from types import SimpleNamespace

import pytest
from homeassistant.util.dt import parse_datetime

from custom_components.assist_improver.analysis import kept_verdict
from custom_components.assist_improver.reflective import RoleRuns
from custom_components.assist_improver.settings import model_name
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


@pytest.mark.asyncio
async def test_the_run_preview_gives_the_tokens_each_step_takes_with_the_model_it_runs_on_now(improver):
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


def check(inp, out, **extra):
    return {"status": "completed", "usage": used(inp, out), **extra}


@pytest.mark.asyncio
async def test_the_run_preview_averages_what_recent_analyses_and_quick_checks_used_each_counted_once(improver):
    """The run confirmation's rough estimate, from the run log as the pipeline keeps it: input plus output tokens, on
    average, over the recent analyses and quick checks. A smooth verdict is a quick check, not an analysis; a best
    rejected answer is an analysis; a verdict kept from an earlier check, a failed run and one with no usage left count
    for nothing; a record recorded again counts once."""
    analysis = {"kind": "journey_analyzer", "status": "completed", "analysis": {"goals": []}}
    for run in (
        {**analysis, "id": "a1", "recording_id": "r1", "answer_status": "accepted", "usage": used(20_000, 1_000),
         "triage": check(3_000, 100)},
        {"id": "s1", "kind": "journey_analyzer", "recording_id": "r2", "status": "completed",
         "analysis": {"triaged": {}}, "answer_status": "triaged_smooth", "triage": check(2_000, 100)},
        {"id": "h1", "kind": "journey_analyzer", "recording_id": "r3", "status": "held", "triage": check(4_000, 200)},
        {**analysis, "id": "a3", "recording_id": "r3", "answer_status": "accepted", "usage": used(30_000, 2_000),
         "triage": check(4_000, 200, kept=True)},
        {"id": "f1", "kind": "journey_analyzer", "recording_id": "r4", "status": "failed", "usage": used(9_000, 0)},
        {**analysis, "id": "old", "recording_id": "r5", "answer_status": "accepted"},  # Trimmed: no usage left.
        {**analysis, "id": "b1", "recording_id": "r6", "answer_status": "best_rejected", "usage": used(10_000, 500)},
        {"id": "rr", "kind": "journey_analyzer", "recording_id": "r7", "status": "running"},
        {**analysis, "id": "rr", "recording_id": "r7", "answer_status": "accepted", "usage": used(40_000, 1_000)},
        {**analysis, "id": "rr", "recording_id": "r7", "answer_status": "accepted", "usage": used(40_000, 1_000)},
    ):
        await improver.runtime.role_runs.record(run)
    hass = improver.hass
    assert improver.runtime.pipeline.preview()["spend"] == {
        "analyze": {"tokens": (21_000 + 32_000 + 10_500 + 41_000) // 4, "model": model_name(hass, "ai_task.opus")},
        "triage": {"tokens": round((3_100 + 2_100 + 4_200) / 3), "model": model_name(hass, "ai_task.haiku")}}


@pytest.mark.asyncio
async def test_the_run_preview_goes_by_the_last_fifty_analyses_and_quick_checks(improver):
    for n in range(1, 56):
        await improver.runtime.role_runs.record(
            {"id": f"a{n}", "kind": "journey_analyzer", "recording_id": f"r{n}", "status": "completed",
             "analysis": {"goals": []}, "answer_status": "accepted", "usage": used(1_000 * n, 0),
             "triage": check(100 * n, 0)})
    spend = improver.runtime.pipeline.preview()["spend"]
    assert (spend["analyze"]["tokens"], spend["triage"]["tokens"]) == (30_500, 3_050)  # The 6th to the 55th.


@pytest.mark.asyncio
@pytest.mark.parametrize("full, expected", [("analyze", (30_500, 200)), ("triage", (2_000, 3_050))])
async def test_each_step_goes_by_its_own_last_fifty_when_the_other_has_only_a_few(improver, full, expected):
    """One step has 55 records, the other only the first three's: the one goes by its last fifty, the other by its
    three."""
    for n in range(1, 56):
        run = {"id": f"a{n}", "kind": "journey_analyzer", "recording_id": f"r{n}"}
        if n <= 3 or full == "analyze":
            run.update(status="completed", analysis={"goals": []}, answer_status="accepted", usage=used(1_000 * n, 0))
        else:
            run.update(status="held", answer_status="awaiting_analysis")
        if n <= 3 or full == "triage":
            run["triage"] = check(100 * n, 0)
        await improver.runtime.role_runs.record(run)
    spend = improver.runtime.pipeline.preview()["spend"]
    assert (spend["analyze"]["tokens"], spend["triage"]["tokens"]) == expected


@pytest.mark.asyncio
async def test_a_record_recorded_again_goes_to_the_end_and_the_log_on_disk_is_what_is_in_memory(hass):
    role_runs = RoleRuns(hass)
    await role_runs.start()
    now = parse_datetime("2026-06-01T00:00:00+00:00")
    old, recent = (now - timedelta(days=60)).isoformat(), (now - timedelta(days=2)).isoformat()
    for key, rid, started in (("a", "r1", recent), ("b", "r2", old), ("c", "r3", recent), ("d", "r4", recent)):
        await role_runs.record({"id": key, "kind": "quick_check", "recording_id": rid, "status": "completed",
                                "started": started})
    await role_runs.record({"id": "a", "kind": "quick_check", "recording_id": "r1", "status": "failed",
                            "started": recent})
    assert [(r["id"], r["status"]) for r in role_runs.runs] == [
        ("b", "completed"), ("c", "completed"), ("d", "completed"), ("a", "failed")]
    assert on_disk(hass) == role_runs.runs
    assert await role_runs.drop({"r3"}) == 1
    assert [r["id"] for r in role_runs.runs] == ["b", "d", "a"] and on_disk(hass) == role_runs.runs
    assert await role_runs.prune({"r1", "r2", "r4"}, now=now) == {"deleted": 1}
    assert [r["id"] for r in role_runs.runs] == ["d", "a"] and on_disk(hass) == role_runs.runs


@pytest.mark.asyncio
async def test_an_old_log_with_two_records_sharing_an_id_keeps_only_the_new_one_once_it_is_recorded_again(hass):
    await RoleRuns(hass).store.async_save({"runs": [
        {"id": "x", "kind": "quick_check", "status": "completed", "n": 1},
        {"id": "y", "kind": "quick_check", "status": "completed"},
        {"id": "x", "kind": "quick_check", "status": "completed", "n": 2}]})
    role_runs = RoleRuns(hass)
    await role_runs.start()
    assert [r["id"] for r in role_runs.runs] == ["x", "y", "x"]
    await role_runs.record({"id": "x", "kind": "quick_check", "status": "completed", "n": 3})
    assert [(r["id"], r.get("n")) for r in role_runs.runs] == [("y", None), ("x", 3)]
    assert on_disk(hass) == role_runs.runs


@pytest.mark.asyncio
async def test_the_analysis_reuses_the_latest_held_verdict_on_the_same_content_until_its_conversation_goes(hass):
    """analysis.kept_verdict over the run log as the pipeline keeps it: a conversation's latest held verdict, while its
    content is the same. A later analysis doesn't hide it; an earlier verdict on the same content doesn't stand in."""
    role_runs = RoleRuns(hass)
    await role_runs.start()
    runtime = SimpleNamespace(role_runs=role_runs)

    def held(key, digest, why):
        return {"id": key, "kind": "journey_analyzer", "recording_id": "r1", "status": "held",
                "answer_status": "awaiting_analysis", "digest_sha256": digest,
                "triage": {"status": "completed", "output": {"smooth": False, "sure": True, "why": why}}}
    await role_runs.record(held("h1", "d", "first"))
    await role_runs.record(held("h2", "d", "second"))
    await role_runs.record({"id": "a1", "kind": "journey_analyzer", "recording_id": "r1", "status": "completed",
                            "digest_sha256": "d", "analysis": {"goals": []}, "answer_status": "accepted"})
    assert kept_verdict(runtime, "r1", "d")["output"]["why"] == "second"
    assert kept_verdict(runtime, "r1", "e") is None and kept_verdict(runtime, "r2", "d") is None
    await role_runs.record(held("h3", "e", "third"))
    assert kept_verdict(runtime, "r1", "d") is None  # Changed since: the earlier verdicts on "d" don't stand in.
    assert kept_verdict(runtime, "r1", "e")["output"]["why"] == "third"
    await role_runs.drop({"r1"})
    assert kept_verdict(runtime, "r1", "e") is None
