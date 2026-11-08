"""The household's moves that take a conversation out (flow.py): accepted or declined, archived, or marked as a test,
it ends its round in Your household from wherever it reached, unheld, with one Ended handoff and no trip; one not in
a round has a round started at Your Assist to end it (P0.6, P0.11, P0.12). And the round it was last sent in, as its
sent mark, its revision notes and its next analysis read it (DETAILS P4.5, P8.9.4, P12.8.1). Each conversation is
moved to where it stands by the flow's own moves, on a flow whose recorder and improvements are stand-ins."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from custom_components.assist_improver import flow as flow_module
from custom_components.assist_improver.flow import STAGES, Flow

RID = "talk0001"
UPDATED = "2026-11-01T10:00:00+00:00"  # When its recording last changed: what a round started at Your Assist ended at.
IN_ROUND = ["at Your Assist", "at Triage", "at Analyze", "at Propose", "at Write up", "at Your review",
            "held at Analyze", "in a revision's round at Propose", "in a revision's round held at Propose",
            "in a revision's round at Write up"]
OUT_OF_ROUND = ["new", "sent in", "unarchived mid-round", "sent in again"]  # At Your Assist, waiting for a run.
ENDED = ["ended", "archived", "archived mid-round"]  # In Your household.


def new_flow(hass):
    runtime = SimpleNamespace(
        recorder=SimpleNamespace(index={RID: {"updated": UPDATED, "status": "session_expired", "user_turns": 2}}),
        improvements=SimpleNamespace(items={}, refresh=lambda: None))
    return Flow(hass, SimpleNamespace(options={}), runtime)


def into_round(flow, conv, stage):
    flow._start_round(RID, conv)
    for step in STAGES[1:STAGES.index(stage) + 1]:
        flow._enter(conv, step, **({"improvements": ["imp1"]} if step == "Write up" else {}))


async def placed(flow, where):
    """The conversation, moved where it stands by the flow's own moves."""
    conv = flow.record(RID)
    if where == "sent in":
        await flow.sent_in(RID, note="It took two tries.")
    elif where.startswith("at "):
        into_round(flow, conv, where[3:])
    elif where == "held at Analyze":
        into_round(flow, conv, "Analyze")
        flow._hold(conv, "failures", "Analyze failed 3 times in a row.")
    elif where.startswith("in a revision's round"):
        into_round(flow, conv, "Your review")
        flow._back_to_propose(RID, conv, revision={"improvement": "imp1", "note": "Press it once."})
        if "held" in where:
            flow._hold(conv, "cut off", "The run was cut off while this stage worked on it.")
        if where.endswith("Write up"):
            flow._enter(conv, "Write up", improvements=["imp2"])
    elif where in ("ended", "archived", "sent in again"):
        into_round(flow, conv, "Analyze")
        flow._end(conv, "no trouble found")
        if where == "archived":
            await flow.archive(RID)
        elif where == "sent in again":
            await flow.sent_in(RID, note="Still two tries.")
    elif where in ("archived mid-round", "unarchived mid-round"):
        into_round(flow, conv, "Propose")
        await flow.archive(RID)
        if where == "unarchived mid-round":
            await flow.archive(RID, archived=False)
    else:
        assert where == "new"
    return conv


def plain(conv):
    """The record, its trace's times left out."""
    out = deepcopy(conv)
    for entry in out["trace"]:
        entry.pop("at")
    return out


def ended_from(before, outcome, new_round, **extra):
    """The record a take-out leaves: unheld, its round ended in Your household from where it stood, with one Ended
    handoff and no trip; not in a round, a round first starts at Your Assist (closed when its recording last changed,
    with the mark it was sent in with), to end it."""
    after = deepcopy(before)
    if new_round:
        after["round"] += 1
        after["rounds"][str(after["round"])] = {"path": ["Your Assist"], "handoffs": [], "records": {
            "Your Assist": {"attempts": [], "ended": UPDATED, "sent": before["sent"]}}}
        after["trace"].append({"round": after["round"], "stage": "Your Assist", "goals": []})
        after.update(stage="Your Assist", sent=None, covered_by=None)
    current = after["rounds"][str(after["round"])]
    current["handoffs"].append({"start": after["stage"], "end": "Your household", "contract": "Ended",
                                "record": {"conversation": RID, "round": after["round"], "outcome": outcome}})
    current["path"].append("Your household")
    current["records"]["Your household"] = {"attempts": [], "outcome": outcome}
    after["trace"].append({"round": after["round"], "stage": "Your household", "goals": []})
    after.update({"stage": "Your household", "outcome": outcome, "held": None, "archived": False, **extra})
    return after


@pytest.mark.asyncio
@pytest.mark.parametrize("where", IN_ROUND + OUT_OF_ROUND + ENDED)
async def test_marked_as_a_test_it_ends_its_round_from_wherever_it_stood_or_one_started_to_end_it(hass, where):
    flow = new_flow(hass)
    before = plain(await placed(flow, where))
    assert await flow.mark_test(RID) is True
    assert plain(flow.record(RID)) == ended_from(before, "marked as a test", where not in IN_ROUND)


@pytest.mark.asyncio
@pytest.mark.parametrize("where", IN_ROUND + OUT_OF_ROUND)
async def test_archived_it_ends_its_round_from_wherever_it_stood_or_one_started_to_end_it(hass, where):
    flow = new_flow(hass)
    before = plain(await placed(flow, where))
    assert await flow.archive(RID) == [RID]
    after = ended_from(before, "archived", where not in IN_ROUND, archived=True,
                       before_archive={k: before[k] for k in ("stage", "outcome", "covered_by")})
    after.pop("listed", None)
    assert plain(flow.record(RID)) == after


@pytest.mark.asyncio
async def test_archived_once_its_round_ended_it_stays_in_that_round_archived(hass):
    flow = new_flow(hass)
    before = plain(await placed(flow, "ended"))
    assert await flow.archive(RID) == [RID]
    assert plain(flow.record(RID)) == {**before, "archived": True, "outcome": "archived", "held": None,
                                       "before_archive": {"stage": "Your household", "outcome": "no trouble found",
                                                          "covered_by": None}}


@pytest.mark.asyncio
@pytest.mark.parametrize("decision, outcome, archived", [("accept", "improved", False), ("decline", "declined", True)])
@pytest.mark.parametrize("where", ["at Your review", "in a revision's round at Propose",
                                   "in a revision's round held at Propose", "in a revision's round at Write up"])
async def test_accepted_or_declined_its_round_ends_from_wherever_it_reached(hass, where, decision, outcome, archived):
    flow = new_flow(hass)
    before = plain(await placed(flow, where))
    item = {"id": "imp1", "conversation": RID, "status": "applied" if decision == "accept" else "pending"}
    assert await flow.decided(item, decision) == []
    assert plain(flow.record(RID)) == ended_from(before, outcome, False, **({"archived": True} if archived else {}))


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["accept", "decline"])
async def test_accepted_or_declined_once_its_round_ended_it_stays_as_it_was(hass, decision):
    flow = new_flow(hass)
    before = plain(await placed(flow, "ended"))
    item = {"id": "imp1", "conversation": RID, "status": "applied" if decision == "accept" else "pending"}
    assert await flow.decided(item, decision) == []
    assert plain(flow.record(RID)) == before


@pytest.mark.asyncio
async def test_the_round_it_was_last_sent_in_gives_its_sent_mark_its_revision_notes_and_its_analysis_input(
        hass, monkeypatch):
    """Rounds: 1 sent in from the panel, 2 a revision, 3 sent in again as filed (the note kept), 4 a revision. Its
    sent mark is round 3's, its revision notes round 4's alone, and its next analysis is given round 3's mark and the
    analyzer's answers from round 3 on."""
    asked = []

    async def analyze(hass_, entry, runtime, rid, **kwargs):
        asked.append(kwargs)
        return {"status": "completed"}
    monkeypatch.setattr(flow_module, "analyze", analyze)
    flow = new_flow(hass)
    conv = flow.record(RID)
    assert flow.sent(RID) is None and flow.revisions(conv) == []
    assert await flow._analysis(RID) == {"status": "completed"}
    assert asked.pop() == {"sent": None, "answered": []}

    await flow.sent_in(RID, note="It took two tries.")
    first = flow.sent(RID)
    into_round(flow, conv, "Your review")
    flow._record(conv, "Analyze", answers=[{"question": "Which relay?", "answer": "The fixture relay."}])
    flow._back_to_propose(RID, conv, revision={"improvement": "imp1", "note": "Press it once."})
    flow._enter(conv, "Write up", improvements=["imp2"])
    flow._enter(conv, "Your review")
    flow._end(conv, "no fix found")
    await flow.sent_in(RID, via="assist")
    again = flow.sent(RID)
    assert again["note"] == first["note"] and again["via"] == "assist" and again["again"] is True
    into_round(flow, conv, "Your review")
    flow._record(conv, "Analyze", answers=[{"question": "Which press?", "answer": "The short one."}])
    flow._back_to_propose(RID, conv, revision={"improvement": "imp2", "note": "Press it twice."})

    assert conv["round"] == 4 and conv["sent"] is None
    assert flow.sent(RID) == again != first
    assert flow.revisions(conv) == [{"round": 4, "note": "Press it twice.", "title": "", "changes": [], "roots": []}]
    assert await flow._analysis(RID) == {"status": "completed"}
    assert asked.pop() == {"sent": again, "answered": [{"question": "Which press?", "answer": "The short one."}]}
