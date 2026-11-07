"""Found in the bug hunt: the propose stage. What Propose takes in (the conversation's analysis, the household's
improvements, what the household sent back) and what it gives out (ideas with both stamps, or a duplicate claim).

The real pipeline runs, with the AI roles and the household's assistant scripted (kit.py). Each test fails
on the bug it names (the bug hunt of 2026-10-26) and holds the fix.
"""

import asyncio

import json

import pytest

from test_conversations import ask
from recordings import before
from kit import approve, candidate_turn, idea, improver, propose_record, variant  # noqa: F401 - improver: a fixture.
from editors import Editors
from custom_components.assist_improver import panel

SECOND = "Please toggle the fixture relay."  # Reads as the friction conversation does (kit.GOALS).


def replayed(runtime, rid):
    """Whether Simulate replayed the conversation with a change, to test an idea on it (its baseline runs first, as
    Propose takes it up: DETAILS P6.6.7)."""
    return any(r["recording_id"] == rid and r["plan"]["kind"] == "after" for r in runtime.replays.items.values())


def generators(runtime):
    return [r for r in runtime.role_runs.runs if r.get("kind") == "hypothesis_generator"]


async def up_for_review(improver):
    """The friction conversation run through every stage: its improvement is written up and waits for review."""
    await improver.arrives(improver.friction)
    improver.client.blocks = before(improver.friction) + candidate_turn()
    done = await improver.run()
    assert done["status"] == "done", done["error"]
    [item] = improver.runtime.improvements.pending()
    return item


# 1. The proposer's best rejected answer moves on as if it had been accepted.

@pytest.mark.asyncio
async def test_an_already_addressed_claim_the_code_rejected_isnt_a_duplicate(improver):
    """docs/DESIGN.md (The pipeline): a goal an existing improvement already addresses "is noted with it and
    skipped ...; code checks the claim (an improvement applied before the conversation didn't stop its friction)";
    (Models judge; code derives): answers are rejected "until it is valid, so a malformed or impossible answer never
    moves on". When the generator's only answer is rejected for such a claim, the conversation doesn't end a duplicate
    of that improvement (SPEC.md P0.12): Propose failed, so it's held there (P0.8)."""
    runtime, model = improver.runtime, improver.model
    improvements, flow = runtime.improvements, runtime.flow
    item = await up_for_review(improver)
    saves = Editors(improver.hass).save  # The panel's half: it saves the scripts.
    assert (await improvements.decide(item["id"], "accept", user_id="alex", editors=saves))["status"] == "applied"
    # Applied before the next conversation took place, so it didn't stop that one's friction.
    improvements.items[item["id"]]["application"]["at"] = "2025-12-01T00:00:00+00:00"
    second = variant(improver.friction, "second001", SECOND)
    await improver.arrives(second)
    improver.client.blocks = before(second)  # Its baseline, as Propose takes it up (DETAILS P6.6.7).
    verdicts = []

    async def claims_and_gives_up(hass, payload, tools):
        model.calls.append(("proposer", None))
        verdicts.append(await model.use(hass, tools["submit_hypotheses"], {"hypotheses": [], "already_addressed": [
            {"session": "second00", "goal_turn": 1, "improvement": item["id"], "why": "Its script does it."}]}))
        return "I couldn't find another way."
    model.hypothesis_generator = claims_and_gives_up
    done = await improver.run()
    assert done["status"] == "done", done["error"]
    assert verdicts and not any(v["accepted"] for v in verdicts)  # The code rejected the claim...
    assert generators(runtime)[-1]["answer_status"] == "best_rejected"
    place = flow.place("second001")  # ...so it's no duplicate of that improvement: Propose is held.
    assert (place["stage"], place["outcome"], place["covered_by"], place["held"]["cause"]) == (
        "Propose", None, None, "failures")


@pytest.mark.asyncio
async def test_an_impossible_idea_is_never_tested_or_sent_on(improver):
    """docs/DESIGN.md (Models judge; code derives): the roles' tools "validate their answer against the household
    and reject it with reasons until it is valid, so a malformed or impossible answer never moves on". An idea naming a
    device the household doesn't have, rejected by submit_hypotheses, is never tested (nothing is replayed with it,
    none of the round's tests is spent) nor built into an improvement: Propose failed, so the conversation is held
    there (P0.8)."""
    runtime, model, client = improver.runtime, improver.model, improver.client
    await improver.arrives(improver.friction)
    verdicts = []

    async def impossible(hass, payload, tools):
        model.calls.append(("proposer", None))
        idea = {"title": "Call it the garage relay", "cause": "Assist doesn't know the name",
                "goals": [{"session": "friction", "goal_turn": 1}], "expected": "One turn",
                "support": [{"session": "friction", "turn": 1, "note": "The user wanted one press"}], "conflicts": [],
                "changes": [{"kind": "registry", "entity_id": "switch.no_such_relay", "aliases": ["garage relay"]}]}
        verdicts.append(await model.use(hass, tools["submit_hypotheses"], {"hypotheses": [idea]}))
        return "Submitted."
    model.hypothesis_generator = impossible
    client.blocks = before(improver.friction) + candidate_turn()
    done = await improver.run()
    assert done["status"] == "done", done["error"]
    assert verdicts and not any(v["accepted"] for v in verdicts)  # Rejected: no such device...
    assert not replayed(runtime, "friction01") and runtime.flow.tests("friction01") == []  # ...never tested...
    assert runtime.improvements.items == {}  # ...nor sent on.
    assert runtime.flow.place("friction01")["held"]["cause"] == "failures"


# 2. The proposer reads the improvement it revises.

@pytest.mark.asyncio
async def test_the_proposer_reads_in_full_the_improvement_it_revises(improver):
    """SPEC.md P0.7: a revision goes straight back in at Propose; P0.3: its note is a turn, with a goal, which the
    round's fresh analysis gives it under the goal the improvement was for (DETAILS P12.8.1). The proposer is given
    what the household sent back for the round: the note and the improvement in full (its changes, the goals its test
    fixed, and the test), though list_improvements leaves out its own conversation's (P0.12). The revised version it
    sends on replaces that one, saying so."""
    runtime, model, client = improver.runtime, improver.model, improver.client
    item = await up_for_review(improver)
    seen = []
    original = model.hypothesis_generator

    scripts = []

    async def reads_it_first(hass, payload, tools):
        seen.append((payload.get("sent_back"), await model.use(hass, tools["list_improvements"])))
        scripts.append(json.dumps(payload.get("friction_scenarios")))
        return await original(hass, payload, tools)
    model.hypothesis_generator = reads_it_first
    client.blocks = candidate_turn()
    note = "Press it twice, it sticks"
    decided = await ask(improver.hass, panel.ws_decide, improvement_id=item["id"], decision="revise", note=note)
    assert decided[0] == "ok"
    await runtime.pipeline._task  # Its own run, at once.
    assert runtime.pipeline.current["status"] == "done", runtime.pipeline.current["error"]
    [(sent_back, listed)] = seen
    assert (sent_back["why"], sent_back["note"]) == ("revision", note)
    [noted] = [g for g in runtime.flow._judged(runtime.flow.record(item["conversation"]))
               if str(g["label"]).startswith("revision:")]
    assert (noted["quote"], noted["serves"]) == (note, 1)  # Analysed afresh, the note a turn: its goal from there.
    [held] = [n for scenario in json.loads(scripts[0]) for n in scenario.get("revision_notes") or []]
    assert held["round"] == 2 and [c["quote"] for c in held["conduct"]] == [note]  # The proposer's script shows it.
    shown = sent_back["improvement"]
    press = {"action": "switch.turn_off", "target": {"entity_id": "switch.fixture_relay"}}
    assert shown["improvement"] == item["id"] and shown["goals"] == ["friction:1"]
    assert press in [s for c in shown["changes"] for s in (c.get("config") or {}).get("sequence") or []]
    assert shown["details"]["tested_on"]
    assert item["id"] not in [i["improvement"] for i in listed["improvements"]]
    [revised] = runtime.improvements.pending()
    assert revised["revises"] == {"id": item["id"], "title": item["title"], "note": note}
    assert runtime.improvements.get(item["id"])["status"] == "revised"


# 3. A proposal that picks up a round is given the round so far.

@pytest.mark.asyncio
async def test_a_resumed_proposal_is_given_the_rounds_tests_reviews_and_whats_left(improver):
    """P0.2, P0.9: the first run is cut off once the proposer's idea is approved (after one request for changes) and
    tested; the manual run that releases the round proposes again. That proposal's input carries the round's test
    (its result) and reviews (their verdicts), with the tests and reviews the round has left, and the Propose record's
    given names the test it was given."""
    from custom_components.assist_improver.hypotheses import REVIEWS_PER_ROUND
    model, runtime, rid = improver.model, improver.runtime, improver.friction["id"]
    as_usual, tries = model.hypothesis_generator, []

    async def cut_off_after_a_test(hass, payload, tools):
        tries.append(payload)
        if len(tries) > 1:
            return await as_usual(hass, payload, tools)
        model.calls.append(("proposer", None))
        await approve(model, hass, tools, idea())
        await model.use(hass, tools["test_idea"], idea())
        runtime.pipeline._task.cancel()  # Cancel, or a restart: the run is cut off here.
        await asyncio.sleep(0)
    model.hypothesis_generator = cut_off_after_a_test
    await improver.arrives(improver.friction)
    improver.client.blocks = before(improver.friction) + candidate_turn()
    runtime.pipeline.start(trigger="manual")
    with pytest.raises(asyncio.CancelledError):
        await runtime.pipeline._task
    assert runtime.flow.place(rid)["held"]["cause"] == "cut off"
    done = await improver.run()
    assert done["status"] == "done", done["error"]
    given = model.proposer_input
    assert [test.get("validated") for test in given.get("tests") or []] == [True]
    assert [review.get("verdict") for review in given.get("reviews") or []] == ["request_changes", "approve"]
    assert given.get("tests_left") == runtime.flow.tests_cap() - 1
    assert given.get("reviews_left") == REVIEWS_PER_ROUND - 2
    assert propose_record(improver)["given"].get("tests") == [0]


def test_a_revision_that_brought_no_new_version_gives_one_reason_the_note_s_goal_first():
    """The line says why in one reason: the proposer's for the note's goal (the last goal of the conversation it gave
    one for), else why its submitted idea didn't go on, ending as a sentence; none when it gave none."""
    from custom_components.assist_improver.flow import Flow
    rid = "bcast001XXXX"
    answer = {"not_fixed": [{"session": "bcast001", "goal_turn": 1, "why": "No idea for it was validated."},
                            {"session": "bcast001", "goal_turn": 4, "why": "  The speakers can't announce "},
                            {"session": "others00", "goal_turn": 9, "why": "Not this one."}],
              "hypotheses": [{"title": "A"}]}
    held = lambda h: "No test of these changes validated."  # noqa: E731
    assert Flow._unrevised_why(rid, answer, held) == "The speakers can't announce."
    assert Flow._unrevised_why(rid, {"hypotheses": [{"title": "A"}]}, held) == "No test of these changes validated."
    assert Flow._unrevised_why(rid, {}, held) == ""
