"""What goes wrong stays where it happened (failures.py): one item's failure with that item, in every stage that works
over several, kept in the run's problems while the others finish; and every AI step's record closed however its step
ended."""

import asyncio
from types import SimpleNamespace

import pytest

from recordings import TARGET, before, prepared_recording, recorded_today
from kit import candidate_turn, improver, variant  # noqa: F401 - improver is a fixture.
from custom_components.assist_improver import failures
from custom_components.assist_improver.cases import CaseError
from custom_components.assist_improver.failures import closing, collecting, contained


def analysis(record):
    return {"recording_id": record["id"], "extra_turns": [2, 3], "goals": [
        {"turn": 1, "summary": "Press the fixture relay", "kind": "sequence", "entity_id": TARGET, "states": ["off", "on"]},
        {"turn": 2, "summary": "Asked for the press script", "kind": "step", "serves": 1},
        {"turn": 3, "summary": "Turned it back on", "kind": "step", "serves": 1},
        {"turn": 4, "summary": "Asked what it can do", "kind": "fact", "fact": "abilities"}]}


class Runs:
    """The run log, in memory: a record replaces the one with its id."""

    def __init__(self, runs=()):
        self.runs = list(runs)

    async def record(self, run):
        self.runs = [r for r in self.runs if r.get("id") != run["id"]] + [dict(run)]
        return run


def models(hass, **options):
    """The entry, with the AI models its steps call present in Home Assistant."""
    hass.states.async_set("ai_task.main", "unknown")
    hass.states.async_set("ai_task.small", "unknown")
    return SimpleNamespace(options={"ai_task_entity_id": "ai_task.main", **options})


# The helper.

@pytest.mark.asyncio
async def test_one_items_failure_stays_with_it_and_the_others_finish():
    problems, done = [], []
    with collecting(problems):
        for item in ("one", "two", "three"):
            async with contained("write", item) as attempt:
                if item == "two":
                    raise RuntimeError("the editor fell over")
                done.append(item)
            assert attempt.failed == (item == "two")
        # An error the caller expects is the item's own answer: contained, not a problem.
        async with contained("propose", "abcd1234", expected=(CaseError,)) as quiet:
            raise CaseError("Still being recorded")
        with contained("propose", "abcd1234", step="scenario") as sync:  # Code that doesn't await, the same way.
            raise KeyError("turns")
    assert done == ["one", "three"]
    assert quiet.failed and quiet.expected and quiet.error == "Still being recorded"
    assert problems == [
        {"stage": "write", "item": "two", "error": "RuntimeError: the editor fell over"},
        {"stage": "propose", "item": "abcd1234", "error": "scenario: KeyError: 'turns'"}]
    assert sync.failed and not sync.expected
    # A cancellation is never held, and outside a run an item's failure is contained (logged) all the same.
    with pytest.raises(asyncio.CancelledError):
        async with contained("write", "one"):
            raise asyncio.CancelledError
    async with contained("write", "one") as alone:
        raise RuntimeError("no run")
    assert alone.failed and alone.error == "RuntimeError: no run"


def test_a_failure_is_logged_with_its_stage_and_item_under_the_integrations_own_logger(caplog):
    """The logger's name says whose it is: the message names only the stage and the item, with the traceback."""
    with contained("write", "abcd1234"):
        raise RuntimeError("the editor fell over")
    [record] = [r for r in caplog.records if r.name == failures.__name__]
    assert record.getMessage() == "write failed on abcd1234" and record.exc_info[0] is RuntimeError


@pytest.mark.asyncio
async def test_a_record_closes_as_failed_or_interrupted_and_the_error_goes_on():
    kept = []

    async def save(record):
        kept.append(dict(record))
        return dict(record)
    record = {"id": "r", "status": "running"}
    with pytest.raises(RuntimeError):
        async with closing(save, record) as keep:
            record["status"] = "completed"  # Not kept yet when the step fails.
            raise RuntimeError("tool crashed")
    assert kept[0]["status"] == "running" and kept[-1]["status"] == "failed"
    assert kept[-1]["error"]["message"] == "RuntimeError: tool crashed"
    later = {"id": "r2", "status": "running"}

    async def step():
        async with closing(save, later) as keep:
            await keep({**later, "turns": 1})  # A later state: the one closed.
            await asyncio.Event().wait()
    task = asyncio.create_task(step())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert kept[-1]["status"] == "interrupted" and kept[-1]["turns"] == 1
    assert kept[-1]["error"]["message"] == failures.CANCELLED
    async with closing(save, {"id": "r3", "status": "running"}) as keep:
        assert (await keep({"id": "r3", "status": "completed"}))["status"] == "completed"
    assert kept[-1]["status"] == "completed"  # Closed by its step, as it ended.


# Each stage, in the pipeline.


@pytest.mark.asyncio
async def test_a_write_up_failing_stays_with_its_improvement_which_keeps_no_copy(hass, monkeypatch):
    """write_copy holds nothing itself: it names the improvements whose write-up failed (each without a copy), keeps
    the error in the run's problems, and writes the others; the flow retries and holds (SPEC.md P0.8)."""
    from custom_components.assist_improver import editor
    from custom_components.assist_improver.improvements import Improvements
    store = Improvements.__new__(Improvements)
    store.hass = hass
    store.items = {i: {"id": i, "title": f"Idea {i}", "status": "awaiting_review", "changes": [], "decisions": []}
                   for i in ("good", "bad")}

    async def nothing():
        pass
    store._save, store._sync = nothing, lambda: None

    async def write(hass, item, entity_id):
        if item["id"] == "bad":
            raise KeyError("benefit")
        return {"status": "written", "headline": "Good", "benefit": "Better"}
    monkeypatch.setattr(editor, "write", write)
    problems = []
    with collecting(problems):
        assert await store.write_copy("ai_task.small", [i["id"] for i in store.unwritten()]) == ["bad"]
    assert "copy" not in store.items["bad"] and [i["id"] for i in store.unwritten()] == ["bad"]
    assert problems == [{"stage": "write", "item": "bad", "error": "KeyError: 'benefit'"}]
    assert store.items["good"]["copy"]["headline"] == "Good"


@pytest.mark.asyncio
async def test_a_goal_or_session_ha_cant_check_is_said_so_and_the_other_facts_come(hass, monkeypatch):
    from custom_components.assist_improver import evidence
    from custom_components.assist_improver.evidence import Evidence
    record = prepared_recording()
    view = SimpleNamespace(recording=record)
    analyses = {"syntheti": analysis(record), "otherone": {**analysis(record), "recording_id": "otherone-x"}}

    async def load(session):
        return view
    sessions = SimpleNamespace(analyses=analyses, load=load, briefs={"syntheti": {}, "otherone": {}},
                               household=view)
    found = Evidence(sessions, recorded_today(record))

    async def try_scripts(self, recording, turn, setup, scripts, targets, goal):
        if goal.startswith("otherone"):
            raise RuntimeError("the worker stopped")
        return {"goal": goal, "reaches_goal": True}
    monkeypatch.setattr(Evidence, "_try_scripts", try_scripts)
    setup = {"scripts": {"press": {}}, "exposure": {}, "aliases": {}}
    hypothesis = {"goals": [{"session": "syntheti", "goal_turn": 1}, {"session": "otherone", "goal_turn": 1}]}
    problems = []
    with collecting(problems):
        facts = await found.implementation(hypothesis, setup)
    assert facts[0] == {"goal": "syntheti:1", "reaches_goal": True}
    assert facts[1]["goal"] == "otherone:1" and facts[1]["reaches_goal"] is None
    assert facts[1]["note"] == "Home Assistant couldn't check this: script check: RuntimeError: the worker stopped"

    async def replay_calls(recording, definitions, setup=None):
        if setup is not None and recording is record:
            raise RuntimeError("the worker stopped")
        if recording is not record:  # One the sandbox says it can't replay: skipped with why, no problem.
            raise CaseError("Cannot backfill missing historical identity")
        return []
    monkeypatch.setattr(evidence, "replay_calls", replay_calls)
    other = SimpleNamespace(recording={**record})

    async def load_either(session):
        return other if session == "otherone" else view
    sessions.load = load_either
    with collecting(problems):
        signals = await found.resolution_signals(setup, ["syntheti", "otherone"])
    assert signals["skipped"] == ["otherone: Cannot backfill missing historical identity",
                                  "syntheti: replay: RuntimeError: the worker stopped"]
    assert signals["sessions_checked"] == 0
    assert {(p["item"], p["error"].split(":")[0]) for p in problems} == {
        ("otherone:1", "script check"), ("syntheti", "replay")}


# Every AI step's record closes.

@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["error", "cancelled"])
async def test_an_analysis_record_closes_however_its_step_ends(hass, monkeypatch, ending):
    from custom_components.assist_improver import analysis
    record = prepared_recording()
    runtime = SimpleNamespace(role_runs=Runs(), improvements=None, index={record["id"]: {"status": "session_expired"}})

    async def export(recording_id):
        return record
    runtime.export, runtime.recorder = export, runtime
    started = asyncio.Event()

    async def run_role(hass, **kwargs):
        started.set()
        if ending == "error":
            raise RuntimeError("the tools crashed")
        await asyncio.Event().wait()
    monkeypatch.setattr(analysis, "run_role", run_role)
    task = asyncio.create_task(analysis.analyze(hass, models(hass), runtime, record["id"]))
    await asyncio.wait_for(started.wait(), 10)
    if ending == "cancelled":
        task.cancel()
    with pytest.raises(RuntimeError if ending == "error" else asyncio.CancelledError):
        await task
    [kept] = runtime.role_runs.runs
    assert kept["status"] == ("failed" if ending == "error" else "interrupted")
    assert ("the tools crashed" if ending == "error" else "Cancelled") in kept["error"]["message"]


@pytest.mark.asyncio
async def test_a_proposal_record_closes_when_its_step_is_cancelled(improver, monkeypatch):
    from custom_components.assist_improver import hypotheses, proposal
    started = asyncio.Event()

    async def hangs(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(hypotheses, "run_generator", hangs)
    await improver.arrives(improver.friction)
    task = asyncio.create_task(proposal.propose(
        improver.hass, improver.entry, improver.runtime, [{"recording_id": "friction01", "goals": []}], tester=None))
    await asyncio.wait_for(started.wait(), 10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    [generator] = [r for r in improver.runtime.role_runs.runs if r["kind"] == "hypothesis_generator"]
    assert generator["status"] == "interrupted"


@pytest.mark.asyncio
async def test_a_role_call_that_is_cancelled_passes_the_cancellation_on_and_one_that_crashes_returns_failed(
        hass, monkeypatch):
    """What run_role always did: its record is its caller's to keep and close (the closing tests above, at each
    caller), so a cut-off call raises rather than coming back as a failed one."""
    from homeassistant.components import ai_task
    from custom_components.assist_improver.reflective import run_role
    started = asyncio.Event()

    async def hangs(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(ai_task, "async_generate_data", hangs)
    task = asyncio.create_task(run_role(hass, role="editor", entity_id="ai_task.small", instructions="i", payload={},
                                        structure=None, allow_paid_inference=True))
    await asyncio.wait_for(started.wait(), 10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):  # Never turned into a failed call: the step was cut off.
        await task

    async def crashes(*args, **kwargs):
        raise RuntimeError("provider down")
    monkeypatch.setattr(ai_task, "async_generate_data", crashes)
    record = await run_role(hass, role="editor", entity_id="ai_task.small", instructions="i", payload={},
                            structure=None, allow_paid_inference=True)
    assert record["status"] == "failed" and record["finished"]


@pytest.mark.asyncio
async def test_propose_goes_on_without_facts_one_conversation_cant_give(improver, monkeypatch):
    from custom_components.assist_improver import evidence, sessions
    from custom_components.assist_improver.worker import WorkerError

    async def probe(recording, entity_ids, definitions):
        raise WorkerError("ValueError: API assist_improver not found")
    monkeypatch.setattr(evidence, "capabilities", probe)
    brief = sessions.brief

    def unreadable(view, recording_id):
        if recording_id == "brokenone1":
            raise TypeError("a turn without its messages")
        return brief(view, recording_id)
    monkeypatch.setattr(sessions, "brief", unreadable)
    await improver.arrives(improver.friction)
    await improver.arrives(variant(improver.friction, "brokenone1", "Please toggle the fixture relay."))
    improver.client.blocks = before(improver.friction) + candidate_turn()  # Only the readable one is tested.
    run = await improver.run()
    assert run["status"] == "done", run["error"]
    flow = improver.runtime.flow  # Contained: the proposal finished over what it could read, and went on.
    propose = flow.round_of("friction01")["records"]["Propose"]
    assert propose["attempts"][-1]["status"] == "done" and "brokenon" not in propose["given"]["conversations"]
    assert flow.place("friction01")["stage"] == "Your review"
    assert all(r["status"] == "completed" for r in improver.runtime.role_runs.runs
               if r["kind"] == "hypothesis_generator")
    problems = run["progress"]["problems"]
    assert {"stage": "propose", "item": "brokenon", "error": "TypeError: a turn without its messages"} in problems
    assert any(p["stage"] == "propose" and p["error"].startswith("capability probe: WorkerError") for p in problems)
    assert not any("item" not in p for p in problems)  # No step failed as a whole.
    assert all(d["assist_can"] == "unknown" for d in improver.model.proposer_input["devices_in_friction"])
