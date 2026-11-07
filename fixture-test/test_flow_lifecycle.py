"""A conversation's record over its life (flow.py), on real runs: deleted during a run, it leaves that run at once and
goes, record and all, when the run ends (DETAILS P5.1.3); accepted or declined while a run proposes for it, it leaves
that run too (P0.11); improved, an undo that didn't take leaves it improved (P0.12); a run is on record from its start,
so a restart that cuts it off shows it interrupted (P5.10); a conversation sent in and still waiting at a restart
gets its fast lane (P0.7); and the household's answer to a question takes it back to the stage that asked (P0.6).
The AI roles and the household's assistant are scripted (kit.py); the graph's harness never deletes or restarts."""

import asyncio
from copy import deepcopy

import pytest
from homeassistant.core import CoreState

from recordings import before, make_recording
from test_conversations import ask
from kit import candidate_turn, improver, variant  # noqa: F401 - improver is a fixture.
from test_on_demand import file
import kit
from custom_components.assist_improver import analysis, async_setup_entry, async_unload_entry, panel
from custom_components.assist_improver.const import CONF_AI_TASK, CONF_CHECK_AI_TASK, CONF_TRIAGE_AI_TASK
from custom_components.assist_improver.reflective import RoleRuns

SECOND = "Please toggle the fixture relay."  # Reads as the friction conversation does (kit.GOALS).


def paused(improver, *stages):
    improver.entry.options = {**improver.entry.options, "paused_stages": list(stages)}


def proposals(improver, monkeypatch):
    """The conversations each of the proposer's calls was asked to fix, call by call."""
    asked, propose = [], improver.model.hypothesis_generator

    async def recording(hass, payload, tools):
        asked.append(sorted(s["session"] for s in payload.get("friction_scenarios", [])))
        return await propose(hass, payload, tools)
    monkeypatch.setattr(improver.model, "hypothesis_generator", recording)
    return asked


def left_by_the_run(improver, monkeypatch, rid):
    """The conversation's flow record as the run left it, kept just before the deletions queued during it go."""
    left, forget_queued = {}, improver.recorder.forget_queued

    async def at_the_runs_end():
        left.update(deepcopy(improver.runtime.flow.conversations.get(rid) or {}))
        return await forget_queued()
    monkeypatch.setattr(improver.recorder, "forget_queued", at_the_runs_end)
    return left


async def gone(improver, rid):
    """Deleted for good: its recording, and its flow record in memory and as saved."""
    runtime = improver.runtime
    saved = (await runtime.flow.store.async_load() or {}).get("conversations", {})
    return (rid not in runtime.recorder.index and await runtime.recorder.store(rid).async_load() is None
            and rid not in runtime.flow.conversations and rid not in saved)


# Deleted during a run (DETAILS P5.1.3).

@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["delete", "archive"])
async def test_a_conversation_deleted_or_archived_while_a_run_works_is_not_analysed_by_it(improver, monkeypatch, how):
    """Alex deletes (archives) second001 while the run gives friction01 its quick check: no role reads second001
    (P5.1.3, P5.1.4). Deleted, it isn't held, and it goes when the run ends; archived, it ends archived."""
    hass, model, flow = improver.hass, improver.model, improver.runtime.flow
    await improver.arrives(improver.friction)
    await improver.arrives(variant(improver.friction, "second001", SECOND))
    paused(improver, "propose", "write")
    check, done_once = model.triage, []
    left = left_by_the_run(improver, monkeypatch, "second001")

    async def triage(hass_, payload, tools):
        if not done_once:
            done_once.append(True)
            handler = panel.ws_conversation_delete if how == "delete" else panel.ws_conversation_hide
            status, _ = await ask(hass, handler, recording_id="second001")
            assert status == "ok"
        return await check(hass_, payload, tools)
    monkeypatch.setattr(model, "triage", triage)
    done = await improver.run()
    await hass.async_block_till_done()
    assert done["status"] == "done" and done_once
    assert [said for _, said in model.calls if said == SECOND] == [], \
        f"second001 was sent to the models after it was {how}d"
    if how == "delete":
        assert left["stage"] == "Your Assist" and left["held"] is None
        assert await gone(improver, "second001")
    else:
        assert flow.place("second001")["outcome"] == "archived"
    assert flow.place("friction01")["stage"] == "Propose"  # The other went on as far as it could.


@pytest.mark.asyncio
async def test_a_conversation_deleted_during_a_run_leaves_propose_its_count_and_the_proposers_view(
        improver, monkeypatch):
    """Both wait at Propose. Alex deletes second001 from the panel as a run starts: it's queued, and out of Propose's
    count at once, as it is out of every stage's; the run never proposes for it, nor shows it to the proposer as part
    of the household; and it goes when the run ends."""
    hass, runtime = improver.hass, improver.runtime
    pipeline, flow = runtime.pipeline, runtime.flow
    await improver.arrives(improver.friction)
    await improver.arrives(variant(improver.friction, "second001", SECOND))
    paused(improver, "propose", "write")
    assert (await improver.run())["status"] == "done"
    assert flow.counts()["Propose"] == 2  # Both held at Propose, blocked.
    paused(improver, "write")
    asked = proposals(improver, monkeypatch)
    improver.client.blocks = before(improver.friction) + candidate_turn()
    pipeline.start(trigger="manual")
    assert await ask(hass, panel.ws_conversation_delete, recording_id="second001") == (
        "ok", {"deleted": "second001", "queued": True})
    assert flow.counts()["Propose"] == 1, "Propose still counted the deleted conversation"
    await pipeline._task
    await hass.async_block_till_done()
    done = pipeline.current
    assert done["status"] == "done", done["error"]
    assert asked == [["friction"]], "the proposer was asked to fix a deleted conversation's trouble"
    [generator] = [r for r in runtime.role_runs.runs
                   if r.get("run_id") == done["id"] and r.get("kind") == "hypothesis_generator"]
    assert not {"second00", "second001"} & set(generator["sessions"])
    assert await gone(improver, "second001")


@pytest.mark.asyncio
async def test_a_conversation_deleted_while_the_run_analyses_another_is_not_proposed_for_or_simulated(
        improver, monkeypatch):
    """friction01 waits at Propose; the run releases it, and Alex deletes it while the run analyses second001, which
    comes first. Propose never asks for friction01's trouble, Simulate spends nothing on it, and the run leaves it
    unheld: it goes when the run ends."""
    hass, model, runtime = improver.hass, improver.model, improver.runtime
    await improver.arrives(improver.friction)
    paused(improver, "propose", "write")
    await improver.run()  # friction01 analysed; held at Propose.
    paused(improver, "write")
    second = variant(improver.friction, "second001", SECOND)
    await improver.arrives(second)
    asked, analyse = proposals(improver, monkeypatch), model.journey_analyzer
    left = left_by_the_run(improver, monkeypatch, "friction01")

    async def household_deletes_friction01_meanwhile(hass_, payload, tools):
        await runtime.recorder.forget_later("friction01")  # As ws_conversation_delete does during a run.
        return await analyse(hass_, payload, tools)
    monkeypatch.setattr(model, "journey_analyzer", household_deletes_friction01_meanwhile)
    improver.client.blocks = before(second) + candidate_turn()
    done = await improver.run()
    await hass.async_block_till_done()
    assert done["status"] == "done", done["error"]
    assert asked == [["second00"]]
    assert left["stage"] == "Propose" and left["held"] is None
    proposing = left["rounds"]["1"]["records"]["Propose"]
    assert proposing["attempts"] == [] and not proposing.get("tests")  # Neither proposed for nor simulated.
    assert await gone(improver, "friction01")


@pytest.mark.asyncio
async def test_a_conversation_deleted_while_a_stage_works_on_it_moves_no_further(improver, monkeypatch):
    """Alex deletes friction01 while its own analysis is under way: what that stage found is kept till the run ends,
    but it moves no further (no proposal), isn't held, and goes when the run ends."""
    hass, model, runtime = improver.hass, improver.model, improver.runtime
    await improver.arrives(improver.friction)
    asked, analyse = proposals(improver, monkeypatch), model.journey_analyzer
    left = left_by_the_run(improver, monkeypatch, "friction01")

    async def household_deletes_it_meanwhile(hass_, payload, tools):
        await runtime.recorder.forget_later("friction01")
        return await analyse(hass_, payload, tools)
    monkeypatch.setattr(model, "journey_analyzer", household_deletes_it_meanwhile)
    done = await improver.run()
    await hass.async_block_till_done()
    assert done["status"] == "done", done["error"]
    assert asked == [] and left["held"] is None
    assert left["stage"] == "Propose" and left["rounds"]["1"]["records"]["Propose"]["attempts"] == []  # Not worked on.
    assert await gone(improver, "friction01")


# Declined during a run (SPEC.md P0.11).

@pytest.mark.asyncio
async def test_a_conversation_declined_while_a_run_proposes_for_its_revisions_round_leaves_that_run(improver):
    """Alex asks for a revision, and declines the earlier version, still up for review (P0.6), while the revision's run
    proposes. The decline archives the conversation (P0.11): it ends declined in Your household, and the run leaves it
    as it leaves one archived meanwhile (DETAILS P5.1.4): it finishes without error, holds nothing, and builds no
    revised version of it, though the proposer sends one on."""
    hass, runtime, model = improver.hass, improver.runtime, improver.model
    await improver.arrives(improver.friction)
    improver.client.blocks = before(improver.friction) + candidate_turn()
    assert (await improver.run())["status"] == "done"
    [earlier] = runtime.improvements.pending()
    propose, declined = model.hypothesis_generator, []

    async def declined_meanwhile(hass_, payload, tools):
        declined.append(await ask(hass, panel.ws_decide, improvement_id=earlier["id"], decision="decline", note=""))
        return await propose(hass_, payload, tools)
    model.hypothesis_generator = declined_meanwhile
    improver.client.blocks = candidate_turn()
    assert (await ask(hass, panel.ws_decide, improvement_id=earlier["id"], decision="revise",
                      note="Name it after the room"))[0] == "ok"
    await runtime.pipeline._task  # The revision's own run.
    done = runtime.pipeline.current
    assert done["status"] == "done", done["error"]
    assert [status for status, _ in declined] == ["ok"] and model.tests[-1]["validated"]  # It had one to send on.
    place = runtime.flow.place("friction01")
    assert (place["stage"], place["round"], place["outcome"], place["archived"], place["held"]) == (
        "Your household", 2, "declined", True, None)
    assert [item["status"] for item in runtime.improvements.items.values()] == ["declined"]


@pytest.mark.asyncio
@pytest.mark.parametrize("revised", [True, False], ids=["new-version", "no-new-version"])
async def test_a_version_being_revised_waits_on_the_improver_so_neither_the_count_nor_the_notification_has_it(
        improver, monkeypatch, revised):
    """DETAILS P12.8.6, P12.10: once Alex asks for a revision, the earlier version waits on the improver, not on him:
    the sidebar's count and the notification of what waits leave it out at once, and while the revision's run
    proposes. When the round ends, what's up for review is counted and announced: the revised version, or the earlier
    one again when the round brought none."""
    from homeassistant.components import persistent_notification

    from custom_components.assist_improver.improvements import NOTIFICATION
    hass, runtime, model = improver.hass, improver.runtime, improver.model
    counts = []  # Every count the sidebar was given.
    monkeypatch.setattr(panel, "set_count", lambda hass_, count, **kw: counts.append(count))
    notices = lambda: persistent_notification._async_get_or_create_notifications(hass)
    await improver.arrives(improver.friction)
    improver.client.blocks = before(improver.friction) + candidate_turn()
    assert (await improver.run())["status"] == "done"
    [earlier] = runtime.improvements.pending()
    assert counts[-1] == 1 and NOTIFICATION in notices()
    if not revised:  # The revision's proposer finds no fix for it.
        model.proposer_unfixable.add(kit.FIRST)
    propose, meanwhile = model.hypothesis_generator, []

    async def looked_meanwhile(hass_, payload, tools):
        meanwhile.append((counts[-1], NOTIFICATION in notices(), runtime.improvements.pending()))
        return await propose(hass_, payload, tools)
    model.hypothesis_generator = looked_meanwhile
    improver.client.blocks = candidate_turn()
    assert (await ask(hass, panel.ws_decide, improvement_id=earlier["id"], decision="revise",
                      note="Name it after the room"))[0] == "ok"
    assert counts[-1] == 0 and NOTIFICATION not in notices()  # At once.
    await runtime.pipeline._task  # The revision's own run.
    assert runtime.pipeline.current["status"] == "done", runtime.pipeline.current["error"]
    assert [(count, shown, [i["id"] for i in pending]) for count, shown, pending in meanwhile] == [
        (0, False, [earlier["id"]])]  # Still up for review (P0.6), open to accept or decline: not waiting on Alex.
    [up] = runtime.improvements.pending()
    assert (up["id"] != earlier["id"]) is revised and counts[-1] == 1
    assert notices()[NOTIFICATION]["title"] == "1 improvement to review"
    if not revised:
        assert runtime.flow.place("friction01")["stage"] == "Your review" and up.get("unrevised")


@pytest.mark.asyncio
async def test_a_version_being_revised_whose_conversation_is_deleted_waits_on_the_household_again(improver, monkeypatch):
    """Propose is blocked, so the revision's round is held there; the conversation is deleted. Its round goes with it
    (flow.forget): the earlier version, still up for review, is counted and announced again at once."""
    from homeassistant.components import persistent_notification

    from custom_components.assist_improver.improvements import NOTIFICATION
    hass, runtime = improver.hass, improver.runtime
    counts = []
    monkeypatch.setattr(panel, "set_count", lambda hass_, count, **kw: counts.append(count))
    await improver.arrives(improver.friction)
    improver.client.blocks = before(improver.friction) + candidate_turn()
    assert (await improver.run())["status"] == "done"
    [earlier] = runtime.improvements.pending()
    paused(improver, "propose")
    assert (await ask(hass, panel.ws_decide, improvement_id=earlier["id"], decision="revise",
                      note="Name it after the room"))[0] == "ok"
    await runtime.pipeline._task  # Its run stops at Propose, blocked.
    assert runtime.flow.revising(earlier["id"]) and counts[-1] == 0
    assert (await ask(hass, panel.ws_conversation_delete, recording_id="friction01"))[0] == "ok"
    await hass.async_block_till_done()
    assert counts[-1] == 1 and [i["id"] for i in runtime.improvements.waiting()] == [earlier["id"]]
    assert persistent_notification._async_get_or_create_notifications(hass)[NOTIFICATION]["title"] == (
        "1 improvement to review")


@pytest.mark.asyncio
async def test_a_conversation_whose_earlier_version_is_accepted_while_a_run_proposes_for_its_revision_leaves_that_run(
        improver):
    """Accept, decline's twin: the earlier version, still up for review (P0.6), is accepted while the revision's run
    proposes. It's applied and the conversation is improved (P0.12), and the run leaves it as it leaves one declined
    meanwhile: it finishes without error, holds nothing, and builds no revised version, though the proposer sends one
    on."""
    hass, runtime, model = improver.hass, improver.runtime, improver.model
    await improver.arrives(improver.friction)
    improver.client.blocks = before(improver.friction) + candidate_turn()
    assert (await improver.run())["status"] == "done"
    [earlier] = runtime.improvements.pending()
    propose, accepted = model.hypothesis_generator, []

    async def accepted_meanwhile(hass_, payload, tools):
        answer = await propose(hass_, payload, tools)  # It tests the revision and sends it on.
        accepted.append(await ask(hass, panel.ws_decide, improvement_id=earlier["id"], decision="accept", note=""))
        return answer
    model.hypothesis_generator = accepted_meanwhile
    improver.client.blocks = candidate_turn()
    assert (await ask(hass, panel.ws_decide, improvement_id=earlier["id"], decision="revise",
                      note="Name it after the room"))[0] == "ok"
    await runtime.pipeline._task  # The revision's own run.
    done = runtime.pipeline.current
    assert done["status"] == "done", done["error"]
    assert [status for status, _ in accepted] == ["ok"] and model.tests[-1]["validated"]  # It had one to send on.
    place = runtime.flow.place("friction01")
    assert (place["stage"], place["round"], place["outcome"], place["archived"], place["held"]) == (
        "Your household", 2, "improved", False, None)
    assert [item["status"] for item in runtime.improvements.items.values()] == ["applied"]


@pytest.mark.asyncio
@pytest.mark.parametrize("decision, outcome, archived", [("accept", "improved", False), ("decline", "declined", True)])
async def test_a_conversation_decided_while_a_run_proposes_for_its_revision_isnt_held_when_that_run_is_cut_off(
        improver, decision, outcome, archived):
    """The earlier version is accepted or declined while the revision's run proposes, and that run is cut off before
    the proposer answers (Cancel, or a restart). The conversation stays where the decision put it, out of the pipeline
    in Your household (P0.12), unheld: held is for one stopped at a stage (P0.8)."""
    hass, runtime, model = improver.hass, improver.runtime, improver.model
    await improver.arrives(improver.friction)
    improver.client.blocks = before(improver.friction) + candidate_turn()
    assert (await improver.run())["status"] == "done"
    [earlier] = runtime.improvements.pending()
    decided = []

    async def decided_then_cut_off(hass_, payload, tools):
        decided.append(await ask(hass, panel.ws_decide, improvement_id=earlier["id"], decision=decision, note=""))
        runtime.pipeline._task.cancel()
        await asyncio.sleep(0)
    model.hypothesis_generator = decided_then_cut_off
    improver.client.blocks = candidate_turn()
    assert (await ask(hass, panel.ws_decide, improvement_id=earlier["id"], decision="revise",
                      note="Name it after the room"))[0] == "ok"
    with pytest.raises(asyncio.CancelledError):
        await runtime.pipeline._task  # The revision's own run, cut off.
    assert [status for status, _ in decided] == ["ok"]
    place = runtime.flow.place("friction01")
    assert (place["stage"], place["round"], place["outcome"], place["archived"], place["held"]) == (
        "Your household", 2, outcome, archived, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("undo", ["busy", "rolled back"])
async def test_an_undo_that_didnt_take_leaves_the_conversation_improved(improver, undo):
    """An undo while another change is still being applied, or one whose write failed and was rolled back, undoes
    nothing: the improvement stays applied, so its conversation stays improved in Your household, not back at Your
    review with nothing to review (P0.12). Once it can, the undo goes through, and the conversation is back up for
    review."""
    from custom_components.assist_improver.changeset import Changesets
    hass, runtime = improver.hass, improver.runtime
    await improver.arrives(improver.friction)
    improver.client.blocks = before(improver.friction) + candidate_turn()
    assert (await improver.run())["status"] == "done"
    [item] = runtime.improvements.pending()
    assert (await ask(hass, panel.ws_decide, improvement_id=item["id"], decision="accept", note=""))[0] == "ok"

    def where():
        place = runtime.flow.place("friction01")
        return place["stage"], place["outcome"], runtime.improvements.get(item["id"])["status"]
    assert where() == ("Your household", "improved", "applied")
    if undo == "busy":
        runtime.improvements.changesets.journal = {"improvement_id": "another", "action": "apply", "steps": [],
                                                   "done": -1}
    else:
        async def failing(hass_, kind, target, value):
            raise OSError("disk full")
        runtime.improvements.changesets = Changesets(hass, writer=failing)
    status, result = await ask(hass, panel.ws_decide, improvement_id=item["id"], decision="undo", note="")
    assert (status, result["status"]) == ("ok", "applied") and not result["ok"]
    assert where() == ("Your household", "improved", "applied")
    runtime.improvements.changesets = Changesets(hass)  # It can now.
    assert (await ask(hass, panel.ws_decide, improvement_id=item["id"], decision="undo", note=""))[1]["ok"]
    assert where() == ("Your review", None, "awaiting_review")

# A restart (DETAILS P5.10, P5.5).

async def restarted(improver):
    """Home Assistant restarts, and has started as the integration sets up again; the runs that starts are done. The
    new runtime."""
    hass, entry = improver.hass, improver.entry
    await improver.recorder.save_index()
    await async_unload_entry(hass, entry)
    state = hass.state
    hass.set_state(CoreState.running)
    try:
        await async_setup_entry(hass, entry)
        await hass.async_block_till_done()
    finally:
        hass.set_state(state)
    pipeline = entry.runtime_data.pipeline
    while pipeline.running:
        await pipeline._task
        await hass.async_block_till_done()
    return entry.runtime_data


@pytest.mark.asyncio
async def test_a_run_is_on_record_from_its_start_so_a_restart_shows_it_interrupted(improver):
    """The run's record is kept as the run starts, so Home Assistant stopping with no chance to close it still leaves
    it in History: interrupted once the run log loads again, never restarted (P5.10)."""
    runtime, pipeline = improver.runtime, improver.runtime.pipeline
    thinking = asyncio.Event()

    async def triage(hass, payload, tools):  # The quick check, still thinking when Home Assistant stops.
        thinking.set()
        await asyncio.Event().wait()
    improver.model.triage = triage
    await improver.arrives(improver.friction)
    started = pipeline.start(trigger="manual")
    await asyncio.wait_for(thinking.wait(), 10)
    saved = (await runtime.role_runs.store.async_load())["runs"]
    assert [(r["id"], r["status"]) for r in saved if r.get("kind") == "pipeline"] == [(started["id"], "running")]
    log = RoleRuns(improver.hass)
    await log.start()  # As the run log loads after a restart.
    assert [(r["id"], r["status"]) for r in log.runs if r.get("kind") == "pipeline"] == [(started["id"], "interrupted")]
    await pipeline.stop()


@pytest.mark.asyncio
async def test_a_conversation_sent_in_and_waiting_at_a_restart_gets_its_fast_lane(improver):
    """friction01 was sent in from Assist; second001 wasn't. Home Assistant restarts before either is taken in. Once
    it has started, friction01 gets its fast lane, a run of its own, and nothing else moves (P0.7)."""
    paused(improver, "propose", "write")
    await improver.arrives(improver.friction)
    await improver.arrives(variant(improver.friction, "second001", SECOND))
    await file(improver, "friction01")
    runtime = await restarted(improver)
    flow, pipeline = runtime.flow, runtime.pipeline
    assert flow.waiting("second001")
    run = pipeline.current
    assert run["status"] == "done", run["error"]
    assert (run["trigger"], run["picked"], run["moved"]) == ("filed", ["friction01"], ["friction01"])
    assert flow.place("friction01")["stage"] == "Propose"  # Its note sent it past the quick check, to Propose.


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["available", "missing"])
async def test_conversations_sent_in_and_waiting_at_a_restart_each_get_a_fast_lane_of_their_own(improver, model):
    """Both were sent in from Assist before the restart: each gets a run of its own, in turn (SPEC.md P0.7). With a
    step's model missing, the first run is refused and the other waits too, so History shows one refusal, not one per
    conversation."""
    paused(improver, "propose", "write")
    await improver.arrives(improver.friction)
    await improver.arrives(variant(improver.friction, "second001", SECOND))
    await file(improver, "friction01")
    await file(improver, "second001")
    if model == "missing":
        improver.entry.options = {**improver.entry.options, CONF_AI_TASK: "ai_task.gone",
                                  CONF_TRIAGE_AI_TASK: "ai_task.gone", CONF_CHECK_AI_TASK: "ai_task.gone"}
    runtime = await restarted(improver)
    runs = [r for r in runtime.role_runs.runs if r.get("kind") == "pipeline"]
    if model == "missing":
        assert [(r["trigger"], r["status"]) for r in runs] == [("filed", "failed")]
        assert runtime.flow.waiting("friction01") and runtime.flow.waiting("second001")
        return
    assert sorted((r["trigger"], r["status"], r["moved"]) for r in runs) == [
        ("filed", "done", ["friction01"]), ("filed", "done", ["second001"])], [r.get("error") for r in runs]


def heard_by_the_analyzer(monkeypatch):
    """What the analyzer was given, call by call: the household's answers, how it was sent in, and its turns."""
    heard, run_role = [], analysis.run_role

    async def listening(hass_, **kwargs):
        if kwargs["role"] == "journey_analyzer":
            payload = kwargs["payload"]
            heard.append((payload.get("answered_by_user"), payload.get("filed_by_user"),
                          [line["user_says"] for line in payload["conversation"]]))
        return await run_role(hass_, **kwargs)
    monkeypatch.setattr(analysis, "run_role", listening)
    return heard


@pytest.mark.asyncio
async def test_the_households_answer_takes_the_conversation_back_to_the_analyzer_in_the_same_round(
        improver, monkeypatch):
    """The analyzer couldn't tell which relay Alex meant: its question waits at Your review. His answer takes the
    conversation straight back to Analyze on a run of its own, in the same round (P0.6, P0.7): the analyzer hears it
    with the question it answers, as no turn of the conversation and no note (P0.3), and nothing goes through the
    quick check again. The analysis counts only the turns said."""
    fine, said = "Switch the fixture relay off.", "The fixture relay, the one by the door."
    goals = kit.GOALS[fine]
    monkeypatch.setitem(kit.GOALS, fine, {**goals, "question_for_user": "Which relay did you mean?"})
    await improver.arrives(variant(make_recording(), "fine0001", fine, turns=2))
    await improver.run()
    runtime = improver.runtime
    assert runtime.flow.question("fine0001") == "Which relay did you mean?"
    monkeypatch.setitem(kit.GOALS, fine, goals)  # Told, it can tell.
    heard = heard_by_the_analyzer(monkeypatch)
    improver.model.calls.clear()
    assert await ask(improver.hass, panel.ws_answer, recording_id="fine0001", answer=said) == ("ok", {})
    await runtime.pipeline._task  # The answer's own run.
    assert runtime.pipeline.current["trigger"] == "answer" and runtime.pipeline.current["status"] == "done"
    said_to_assist = [fine, "Use the press script."]  # Its turns: the answer isn't one.
    assert heard == [([{"question": "Which relay did you mean?", "answer": said}], None, said_to_assist)]
    assert [c for c in improver.model.calls if c[0] == "triage"] == []
    assert runtime.flow.analysis("fine0001")["turns"] == 2 and "notes" not in runtime.flow.analysis("fine0001")
    assert runtime.flow.place("fine0001")["round"] == 1 and runtime.flow.question("fine0001") is None
    assert runtime.flow.round_of("fine0001")["path"][2:5] == ["Analyze", "Your review", "Analyze"]
    assert (await ask(improver.hass, panel.ws_answer, recording_id="fine0001", answer=said))[0] == "not_asking"


@pytest.mark.asyncio
async def test_the_analyzer_hears_the_households_answer_again_in_a_revisions_round(improver, monkeypatch):
    """The answer stays with the conversation until it's sent in again: a revision's round reads the conversation
    afresh (P12.8.1), the answer with it, so the analyzer can still tell which relay was meant."""
    hass, runtime, asked, said = improver.hass, improver.runtime, "Which relay did you mean?", "The one by the door."
    friction = kit.GOALS[kit.FIRST]
    monkeypatch.setitem(kit.GOALS, kit.FIRST, {**kit.GOALS["Switch the fixture relay off."], "question_for_user": asked})
    await improver.arrives(improver.friction)
    await improver.run()
    assert runtime.flow.question("friction01") == asked
    monkeypatch.setitem(kit.GOALS, kit.FIRST, friction)  # Told, it can tell.
    heard = heard_by_the_analyzer(monkeypatch)
    improver.client.blocks = before(improver.friction) + candidate_turn()
    assert await ask(hass, panel.ws_answer, recording_id="friction01", answer=said) == ("ok", {})
    await runtime.pipeline._task  # The answer's own run.
    [earlier] = runtime.improvements.pending()
    improver.client.blocks = candidate_turn()
    assert (await ask(hass, panel.ws_decide, improvement_id=earlier["id"], decision="revise",
                      note="Name it after the room"))[0] == "ok"
    await runtime.pipeline._task  # The revision's own run.
    assert runtime.pipeline.current["status"] == "done", runtime.pipeline.current["error"]
    assert runtime.flow.place("friction01")["round"] == 2
    assert [answers for answers, _, _ in heard] == [[{"question": asked, "answer": said}]] * 2
    assert [filed for _, filed, _ in heard] == [None, None]  # Nobody sent it in: it isn't framed as filed.
    assert [said in lines for _, _, lines in heard] == [False, False]  # Nor is the answer a turn.


def waiting_signals(improver, monkeypatch):
    """What tells the household something waits: every count the sidebar was given, and the notification now (None
    while there's none)."""
    from homeassistant.components import persistent_notification

    from custom_components.assist_improver.improvements import NOTIFICATION
    counts = []
    monkeypatch.setattr(panel, "set_count", lambda hass_, count, **kw: counts.append(count))
    return counts, lambda: persistent_notification._async_get_or_create_notifications(improver.hass).get(NOTIFICATION)


@pytest.mark.asyncio
@pytest.mark.parametrize("gone", ["answered", "deleted", "deleted during a run"])
async def test_a_question_alone_is_counted_and_announced_until_it_is_answered_or_its_conversation_deleted(
        improver, monkeypatch, gone):
    """A run that ends with only a question for Alex tells him so (P0.11): the sidebar counts it, and the notification
    names it in the home page's words, linking to the home page, where it's answered. Both follow at once when it's
    answered, or its conversation is deleted (during a run, queued: gone from view at once)."""
    fine, asked = "Switch the fixture relay off.", "Which relay did you mean?"
    goals = kit.GOALS[fine]
    monkeypatch.setitem(kit.GOALS, fine, {**goals, "question_for_user": asked})
    counts, notice = waiting_signals(improver, monkeypatch)
    await improver.arrives(variant(make_recording(), "fine0001", fine, turns=2))
    await improver.run()
    assert counts[-1] == 1 and improver.runtime.improvements.pending() == []
    assert (notice()["title"], notice()["message"]) == ("1 question for you", f"* [{asked}](/assist-improver)")
    monkeypatch.setitem(kit.GOALS, fine, goals)  # Told, it can tell.
    if gone == "answered":
        assert await ask(improver.hass, panel.ws_answer, recording_id="fine0001", answer="The one by the door.") == (
            "ok", {})
    else:
        if gone == "deleted during a run":
            monkeypatch.setattr(type(improver.runtime.pipeline), "running", property(lambda self: True))
        status, result = await ask(improver.hass, panel.ws_conversation_delete, recording_id="fine0001")
        assert status == "ok" and result.get("queued", False) is (gone == "deleted during a run")
    assert counts[-1] == 0 and notice() is None  # At once.
    if gone == "answered":
        await improver.runtime.pipeline._task  # The answer's own run: it can tell now, and asks nothing more.
        assert counts[-1] == 0 and notice() is None


@pytest.mark.asyncio
async def test_with_a_question_and_an_improvement_waiting_both_are_counted_and_announced_the_question_first(
        improver, monkeypatch):
    """The notification lists what waits as the home page does: the question first, then the improvement up for
    review, under both their counts in the home page's words."""
    fine, asked = "Switch the fixture relay off.", "Which relay did you mean?"
    monkeypatch.setitem(kit.GOALS, fine, {**kit.GOALS[fine], "question_for_user": asked})
    counts, notice = waiting_signals(improver, monkeypatch)
    await improver.arrives(improver.friction)
    await improver.arrives(variant(make_recording(), "fine0001", fine, turns=2))
    improver.client.blocks = before(improver.friction) + candidate_turn()
    assert (await improver.run())["status"] == "done"
    [up] = improver.runtime.improvements.pending()
    assert counts[-1] == 2 and notice()["title"] == "1 question for you, 1 improvement to review"
    assert notice()["message"] == (f"* [{asked}](/assist-improver)\n\n"
                                   f"- [{up['copy']['headline']}](/assist-improver?id={up['id']})\n\n"
                                   "Review this improvement in [Assist Improver](/assist-improver).")


def test_what_a_crash_left_between_stages_is_held_cut_off_when_the_record_is_read_again():
    """A hard stop mid-run never got to hold what the run hadn't finished (P0.8): reading the record again holds each
    conversation still in a round between stages, so a manual full run or its stage's run picks it up. One waiting
    for a run, ended, or already held stays as it was."""
    from types import SimpleNamespace

    from custom_components.assist_improver.flow import Flow

    def conv(stage, path, held=None):
        return {"id": stage, "stage": stage, "round": 1, "held": held, "outcome": None, "archived": False,
                "covered_by": None, "sent": None, "rounds": {"1": {"path": path, "records": {}, "handoffs": []}},
                "trips": [], "trace": []}
    flow = Flow.__new__(Flow)
    flow.conversations = {"mid": conv("Propose", ["Your Assist", "Analyze", "Propose"]),
                          "admitted": conv("Your Assist", ["Your Assist"]),
                          "review": conv("Your review", ["Your Assist", "Analyze", "Propose", "Write up", "Your review"]),
                          "held": conv("Analyze", ["Your Assist", "Analyze"], held={"cause": "failures", "why": "x"})}
    for rid, record in flow.conversations.items():
        record["id"] = rid
    flow.runtime = SimpleNamespace(recorder=SimpleNamespace(index={rid: {} for rid in flow.conversations}))
    flow.recover()
    assert flow.conversations["mid"]["held"]["cause"] == "cut off"
    assert flow.conversations["admitted"]["held"]["cause"] == "cut off"
    assert flow.conversations["review"]["held"] is None
    assert flow.conversations["held"]["held"]["cause"] == "failures"

