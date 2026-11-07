"""Found in the bug hunt: resilience. How the pipeline and each stage handle what goes wrong: a run cut off (Cancel, a restart),
a transient failure of the machinery (the provider, the replay worker), and how the run log reports it.

Kai, 2026-10-26: "The pipeline overall needs to handle failures gracefully, and so does individual stages", and "let's
try to generalize the resiliency improvements beyond just that single stage". DETAILS P13.4.1: an operation is retried
only when another attempt plausibly helps, usually a fault of the machinery, up to three attempts. DESIGN.md: "A step
never fails silently."

Each test failed on the bug it names (the bug hunt of 2026-10-26) and holds the fix.
"""

import asyncio

import pytest
from homeassistant.core import CoreState

from recordings import before
from kit import NOT_FIXED, approve, candidate_turn, idea, improver, propose_record  # noqa: F401 - a fixture.
from test_records import check_all
from custom_components.assist_improver.reflective import RoleRuns

async def run(improver, **kwargs):
    pipeline = improver.runtime.pipeline
    pipeline.start(trigger="manual", **kwargs)
    await pipeline._task
    return pipeline.current


def replay_of(improver, kind):
    """The friction conversation's replay without the change ("before") or with it ("after"), as kept."""
    [found] = [r for r in improver.runtime.replays.items.values()
               if r["recording_id"] == "friction01" and r["plan"]["kind"] == kind]
    return found


# A goal's test cut off, or hit by a transient fault, is tried again: never left untested for good.

@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["cancelled", "transient_fault"])
async def test_a_goals_test_cut_off_or_hit_by_a_transient_fault_in_its_baseline_is_tested_by_a_later_run(improver,
                                                                                                         how):
    """Run 1 never finishes the friction goal's baseline: the household cancels it while the assistant's model is
    thinking (as a restart cuts it off too), or the model's provider fails for a moment (OSError, the
    infrastructure_failure path). The next run must replay it: DETAILS P13.4.1 retries a transient infrastructure fault
    up to three attempts, and History tells the household "Nothing was lost: what it didn't finish is held for your next
    run." Before the fix, the baseline's record stood for its plan, so every later test of the goal ended without a
    replay, and the goal was never tested again."""
    client, pipeline = improver.client, improver.runtime.pipeline
    await improver.arrives(improver.friction)
    client.blocks = before(improver.friction)
    if how == "cancelled":
        waiting = asyncio.Event()

        async def second_reply_never_comes(request):  # Turn 1's tool call ran; the model's reply never comes.
            if len(client.requests) > 1:
                waiting.set()
                await asyncio.Event().wait()
            return client.blocks.pop(0)
        client.reply = second_reply_never_comes
        pipeline.start(trigger="manual")
        await asyncio.wait_for(waiting.wait(), 30)
        await pipeline.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pipeline._task
        assert pipeline.current["status"] == "cancelled"
        assert replay_of(improver, "before")["status"] == "interrupted"
    else:
        async def overloaded(request):
            raise OSError("The provider is overloaded for a moment")
        client.reply = overloaded
        first = await run(improver)
        assert first["status"] == "done", first["error"]
        assert replay_of(improver, "before")["status"] == "infrastructure_failure"
    # The provider is back, and the next run releases the conversation the first one held.
    client.reply = None
    client.blocks = before(improver.friction) + candidate_turn()
    second = await run(improver)
    assert second["status"] == "done", second["error"]
    assert replay_of(improver, "before")["status"] == "reproduced", replay_of(improver, "before")["status"]
    assert not client.blocks and len(improver.runtime.improvements.pending()) == 1
    assert improver.model.slips == []


@pytest.mark.asyncio
async def test_a_fix_whose_replay_hit_a_transient_fault_is_replayed_when_it_is_tested_again(improver):
    """The baseline reproduces; the fix's replay then fails because the provider is overloaded for a moment
    (infrastructure_failure, which doesn't count against the goal's tests). When the same fix is tested again (Run
    anyway proposes it again), its replay must run: DETAILS P13.4.1 retries a transient infrastructure fault, and
    DESIGN.md's "No resampling" is about repeating a test hoping for a better result, not one that never got one.
    The test reused any earlier replay with a result under the same plan, so the failed replay "stood" for good: no
    request was made, nothing went up for review, and the goal never got a fix or No fix found."""
    client = improver.client
    await improver.arrives(improver.friction)
    async def overloaded_after_the_baseline(request):
        if not client.blocks:  # The baseline's turns are done; the fix's first call fails.
            raise OSError("The provider is overloaded for a moment")
        return client.blocks.pop(0)
    client.reply = overloaded_after_the_baseline
    client.blocks = before(improver.friction)
    first = await run(improver)
    assert first["status"] == "done", first["error"]
    assert improver.runtime.improvements.items == {}
    assert replay_of(improver, "after")["status"] == "infrastructure_failure"
    client.reply = None
    client.blocks = candidate_turn()  # The replay without the change is reused; the fix's replays again.
    second = await run(improver)
    assert second["status"] == "done", second["error"]
    assert not client.blocks, "the fix's replay was never tried again"
    assert len(improver.runtime.improvements.pending()) == 1 and improver.model.slips == []


# Reviews belong to the round, as tests do.

@pytest.mark.asyncio
async def test_reviews_are_kept_in_the_rounds_record_as_they_return_and_count_once_however_an_attempt_ends(improver):
    """P0.13 with P0.2: the proposal's first try gets a review asking for changes, then an approval, and fails (the
    model is overloaded); the next try goes on from there. The round's record keeps the reviews the reviewer gave, and
    those the run log has; each request for changes is one trip back from Review, the second try can ask only for the
    reviews the first left, and the first try's approval lets it test that idea without asking again."""
    from custom_components.assist_improver.hypotheses import REVIEWS_PER_ROUND
    model, out, press = improver.model, {"tries": 0}, idea()

    async def proposer(hass, payload, tools):
        out["tries"] += 1
        model.calls.append(("proposer", None))
        if out["tries"] == 1:
            await approve(model, hass, tools, press)
            raise RuntimeError("The model is overloaded")
        ask = tools["ask_reviewer"]
        out["left"] = ask.cap - len(ask.reviews)
        out["test"] = await model.use(hass, tools["test_idea"], press)
        await model.use(hass, tools["submit_hypotheses"],
                        {"hypotheses": [press]} if out["test"].get("validated") else NOT_FIXED)
        return "Submitted."
    model.hypothesis_generator = proposer
    await improver.arrives(improver.friction)
    improver.client.blocks = before(improver.friction) + candidate_turn()
    done = await run(improver)
    assert done["status"] == "done", done["error"]
    given = [{"blocking": "request_changes", "non-blocking": "approve"}[review] for review in model.reviews]
    logged = {(r["hypothesis"]["title"], r["verdict"]) for record in improver.runtime.role_runs.runs
              if record.get("kind") == "hypothesis_generator" for r in record.get("reviews") or []}
    kept = [(r["hypothesis"]["title"], r["verdict"]) for r in propose_record(improver)["reviews"]]
    assert kept == [(press["title"], verdict) for verdict in given] == [(press["title"], "request_changes"),
                                                                       (press["title"], "approve")]
    assert set(kept) == logged
    trips = [t for t in improver.runtime.flow.record(improver.friction["id"])["trips"] if t["start"] == "Review"]
    assert len(trips) == 1 and out["left"] == REVIEWS_PER_ROUND - 2 and out["test"].get("validated")


# A run cut off by a restart says so.

@pytest.mark.asyncio
async def test_a_run_cut_off_by_a_restart_keeps_the_shape_of_a_run_record(hass):
    """A run cut off by a restart is marked interrupted when the run log loads (RoleRuns.start). Its record must keep
    the shape records.PipelineRun declares (error: text), as the History reads it: settings.js shows a run's error in
    its Details, and an object there reads "[object Object]". Before the fix, start() gave every running record, a
    pipeline run's too, error={"message": "Reload during the call; not repeated"}."""
    earlier = RoleRuns(hass)
    await earlier.store.async_save({"runs": [{
        "id": "run-1", "kind": "pipeline", "trigger": "manual", "status": "running",
        "started": "2026-10-25T10:00:00+00:00", "stage": "analyze", "progress": {"taken_in": 2}, "error": None,
        "only": None, "trace_id": "run-1", "admitted": ["rec-1", "rec-2"], "released": [],
        "moved": ["rec-1", "rec-2"], "results": {}}]})
    log = RoleRuns(hass)
    await log.start()
    [kept] = log.runs
    assert kept["status"] == "interrupted"
    assert check_all(runs=log.runs) == []


@pytest.mark.asyncio
async def test_a_run_home_assistant_stops_is_marked_interrupted_not_cancelled_by_the_household(improver):
    """Home Assistant restarting (an update, an install) cancels the run's background task. The pipeline's docstring and
    DETAILS P5.10: "a run Home Assistant stops is marked interrupted"; its AI step's record says interrupted too
    (failures.closing). Before the fix, the run caught the cancellation like the household's Cancel and kept
    "cancelled", so History read "Run cancelled" for a restart nobody cancelled from the panel."""
    hass, runtime = improver.hass, improver.runtime
    entered = asyncio.Event()

    async def thinking(hass, payload, tools):  # The quick check, still thinking when Home Assistant stops.
        entered.set()
        await asyncio.Event().wait()
    improver.model.triage = thinking
    await improver.arrives(improver.friction)
    hass.set_state(CoreState.running)
    runtime.pipeline.start(trigger="manual")
    await asyncio.wait_for(entered.wait(), 10)
    await hass.async_stop()  # Home Assistant restarting.
    [record] = [r for r in runtime.role_runs.runs if r.get("kind") == "pipeline"]
    assert record["status"] == "interrupted", record["status"]
