"""Testable specs for improvements: deciding, applying all or nothing, undo, conflicts and recovery.

Each test names a promise the product makes (SPEC.md, DETAILS.md, README.md, docs/DESIGN.md, the panel's
words and the modules' own docstrings) and fails if that promise breaks. They run on the offline Home Assistant of
test_changeset.py (its real entity registry, exposure settings, HA's script editor saving scripts.yaml as the panel
calls it, and config subentries), through the public Improvements API, the panel's websocket commands and the integration's own setup.
"""

import asyncio
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace

import pytest
from homeassistant.components import persistent_notification
from homeassistant.components.homeassistant.exposed_entities import async_should_expose
from homeassistant.helpers import entity_registry as er

from recordings import SCRIPT, TARGET, make_recording
from test_changeset import PROMPT, USER, _second_assistant, home, proposal, tested, users_edit  # noqa: F401  (home: a fixture)
from editors import Connection, Editors, command, open_panel, settle
from test_milestone1 import fake_entry
from conftest import serve
from kit import runtime_with_flow
from custom_components.assist_improver import changeset, household, panel
from custom_components.assist_improver.cases import CaseError, recording_turns
from custom_components.assist_improver.changes import setup_from_changes
from custom_components.assist_improver.changeset import Changesets, plan_steps
from custom_components.assist_improver.const import DOMAIN
from custom_components.assist_improver.improvements import (
    NOTIFICATION, OUTCOME_NOTIFICATION, Improvements, choices)

ALL = ["accept", "revise", "decline"]
LINE = "The garage means both garage doors."  # The household line in test_changeset.proposal (for USER).


# Improvements, as the review screens see them.

def improvement(home, id_="imp1", **extra):
    """Up for review (written up), with every change kind: an edited script, a new exposed one, an alias, a line; its
    test (test_changeset.tested) fixed goal s1:1: three turns before, one with it."""
    return {**proposal(home, id=id_), "cause": "Why", "fixes": ["s1:1"],
            "copy": {"status": "failed"}, "status": "awaiting_review", "created": "1", "decisions": [],
            "application": None, "history": [], **extra}


def alias_item(home, id_, entity_id="light.front", alias="front light", **extra):
    """Only a new name for a device."""
    return improvement(home, id_, title=f"Call {entity_id} {alias}",
                       changes=[{"kind": "registry", "entity_id": entity_id, "aliases": [alias]}],
                       setup={"scripts": {}, "exposure": {}, "aliases": {entity_id: [alias]}},
                       tested={"scripts": {}, "exposure": {}}, devices=[entity_id], **extra)


def line_item(home, id_, text="Quiet hours start at 22:00.", who="everyone", **extra):
    """Only a household line."""
    return improvement(home, id_, title="Quiet hours", changes=[{"kind": "preference", "for": who, "text": text}],
                       setup={"scripts": {}, "exposure": {}, "aliases": {}}, tested={"scripts": {}, "exposure": {}},
                       **extra)


async def reviewing(home, *items):
    """The improvements store with these items up for review; the panel's commands read the same store."""
    improvements = Improvements(home.hass, None)
    await improvements.start()
    for item in items:
        improvements.items[item["id"]] = item
    await improvements._save()
    improvements._sync()  # The notification brought up to date.
    serve(home.hass, runtime_with_flow(home.hass, improvements=improvements, role_runs=SimpleNamespace(runs=[]),
                                       recorder=SimpleNamespace(index={})))
    return improvements


async def home_now(home):
    """Everything an improvement can change in Home Assistant, read through HA's own APIs."""
    hass = home.hass
    return {"press_garage": await changeset.read(hass, "script", "press_garage"),
            "close_garage": await changeset.read(hass, "script", "close_garage"),
            "aliases": await changeset.read(hass, "alias", "light.front"),
            "exposure": {e: async_should_expose(hass, "conversation", e)
                         for e in ("script.press_garage", "script.close_garage")},
            "household": await changeset.read(hass, "household", "household"),
            "assistant": deepcopy(dict(home.provider.subentries["sub1"].data))}


def notifications(hass):
    return persistent_notification._async_get_or_create_notifications(hass)


def target(home):
    return f"{home.provider.entry_id}/sub1"


async def ws(handler, hass, editor=None, **msg):
    """One websocket command from the panel, signed in as Alex (an admin): ("ok", result) or ("error", code, text). The
    panel is open on its connection, saving scripts through HA's script editor (editor: an Editors, or one of
    failing_writes or crashing_after)."""
    connection = Connection()
    await open_panel(hass, connection, editor or Editors(hass))
    try:
        return await command(hass, connection, handler, **msg)
    finally:
        connection.close()


async def left_open(hass):
    """The change Home Assistant's store says is left open, if any (Changesets' journal)."""
    changesets = Changesets(hass)
    await changesets.load()
    return changesets.journal


def saves(home):
    """The panel's half of a change: it saves its scripts through HA's script editor."""
    return Editors(home.hass).save


def counted(act):
    """A writer, and its editor (.editor) for the scripts the panel saves through HA's script editor, counting their
    writes together from 1, in order: act(n, write, kind, target) does the nth (write() writes it)."""
    count = {"n": 0}

    async def around(write, kind, target_):
        count["n"] += 1
        await act(count["n"], write, kind, target_)

    async def writer(hass, kind, target_, value):
        await around(lambda: changeset.write(hass, kind, target_, value), kind, target_)

    class Editor(Editors):
        async def save(self, kind, item_id, config):
            await around(lambda: Editors.save(self, kind, item_id, config), kind, item_id)
    writer.editor = lambda hass: Editor(hass)
    return writer


def failing_writes(*numbers, error="disk full"):
    """A writer whose nth writes fail (counted from 1, the scripts the panel saves too), as a full disk or a refused
    value would."""
    async def act(n, write, *_):
        if n in numbers:
            raise OSError(error)
        await write()
    return counted(act)


class Crash(BaseException):
    """Home Assistant stopping mid-change: nothing in the apply catches it."""


def crashing_after(number):
    """A writer that writes its nth value (counted with the scripts the panel saves), then Home Assistant stops."""
    async def act(n, write, *_):
        await write()
        if n == number:
            raise Crash
    return counted(act)


# Deciding: accept, ask for a revision, or decline (as not an issue, or not).

@pytest.mark.asyncio
@pytest.mark.parametrize("decision, reason, status, still_open", [
    ("decline", None, "declined", []),
    ("decline", "not_an_issue", "declined", []),  # It went fine, or it was only a try: kept beside the decline.
    ("revise", None, "awaiting_review", ALL)])  # Up for review until a revised version replaces it (SPEC.md P0.6).
async def test_every_decision_but_accept_is_recorded_and_changes_nothing_in_home_assistant(home, decision, reason,
                                                                                          status, still_open):
    improvements = await reviewing(home, improvement(home))
    before = await home_now(home)
    result = await improvements.decide("imp1", decision, note="Name it after the room", user_id="alex", reason=reason)
    assert result["status"] == status and result["outcome"] == {"status": "recorded"}
    [recorded] = improvements.get("imp1")["decisions"]
    assert {k: recorded.get(k) for k in ("decision", "note", "reason", "by", "outcome")} == {
        "decision": decision, "note": "Name it after the room", "reason": reason, "by": "alex", "outcome": "recorded"}
    assert await home_now(home) == before
    assert (NOTIFICATION in notifications(home.hass)) == bool(still_open)  # Listed only while it waits on you.
    assert choices(improvements.get("imp1")) == still_open


@pytest.mark.asyncio
async def test_asking_for_a_revision_needs_a_note_saying_what_to_change(home):
    improvements = await reviewing(home, improvement(home), improvement(home, "imp2"))
    for note in ("", "   \n"):
        with pytest.raises(CaseError, match="Say what to change"):
            await improvements.decide("imp1", "revise", note=note, user_id="alex")
    assert improvements.get("imp1")["status"] == "awaiting_review" and improvements.get("imp1")["decisions"] == []
    # A revision's note is kept as written, without the surrounding blanks.
    assert (await improvements.decide("imp1", "revise", note="  Name it after the room \n"))["decisions"][-1][
        "note"] == "Name it after the room"


@pytest.mark.asyncio
async def test_a_decision_that_isnt_open_now_is_refused_and_changes_nothing(home):
    improvements = await reviewing(home, improvement(home))
    before = await home_now(home)
    for improvement_id, decision in (("imp1", "undo"), ("imp1", "maybe"), ("nope", "accept")):
        with pytest.raises(CaseError):
            await improvements.decide(improvement_id, decision, user_id="alex")
    await improvements.decide("imp1", "decline", user_id="alex")
    for decision in ("accept", "decline", "undo"):
        with pytest.raises(CaseError, match="isn't open"):
            await improvements.decide("imp1", decision, user_id="alex")
    assert [d["decision"] for d in improvements.get("imp1")["decisions"]] == ["decline"]
    assert await home_now(home) == before
    # The panel says so instead of acting.
    assert (await ws(panel.ws_decide, home.hass, improvement_id="imp1", decision="accept", note=""))[:2] == (
        "error", "decision")


@pytest.mark.asyncio
async def test_only_three_decisions_are_offered_and_a_reason_goes_only_with_decline(home):
    improvements = await reviewing(home, improvement(home))
    before = await home_now(home)
    for decision, reason in (("revise", "not_an_issue"), ("accept", "not_an_issue"), ("decline", "was_a_test")):
        with pytest.raises(CaseError, match="doesn't go with"):
            await improvements.decide("imp1", decision, note="Why", user_id="alex", reason=reason)
    assert improvements.get("imp1")["status"] == "awaiting_review" and improvements.get("imp1")["decisions"] == []
    assert await home_now(home) == before
    # Through the panel: decline as not an issue.
    assert (await ws(panel.ws_decide, home.hass, improvement_id="imp1", decision="decline", note="",
                     reason="not_an_issue"))[1]["result"] == "Thanks. Declined, and its conversation is archived."
    assert [(d["decision"], d["reason"]) for d in improvements.get("imp1")["decisions"]] == [
        ("decline", "not_an_issue")]


@pytest.mark.parametrize("status, open_", [
    ("awaiting_review", ALL), ("needs_attention", ALL), ("applied", ["undo"]), ("undone", ALL),
    ("declined", []), ("revised", []), ("conflict", [])])
def test_the_decisions_open_follow_the_improvements_status(status, open_):
    assert choices({"status": status}) == open_
    shown = panel.review({"id": "x", "title": "T", "cause": "", "status": status, "test": tested(), "changes": [],
                          "provider": {}})["choices"]
    labels = [c["label"] for c in shown]
    assert len(labels) == len(open_) and (not open_ or labels[0] == {"accept": "Accept changes",
                                                                     "undo": "Undo changes"}[open_[0]])
    # Decline can be given as not an issue; nothing else takes a reason.
    assert {c["value"]: c.get("reasons") for c in shown} == {d: ["not_an_issue"] if d == "decline" else None
                                                             for d in open_}


@pytest.mark.asyncio
async def test_an_improvement_replaced_by_its_revision_can_no_longer_be_decided(home):
    improvements = await reviewing(home, improvement(home, status="revised", revised_by="imp2"))
    before = await home_now(home)
    assert choices(improvements.get("imp1")) == []
    with pytest.raises(CaseError):
        await improvements.decide("imp1", "accept", user_id="alex")
    assert await home_now(home) == before


@pytest.mark.asyncio
async def test_decisions_and_the_undo_record_survive_a_restart(home):
    improvements = await reviewing(home, improvement(home), improvement(home, "imp2"))
    before = await home_now(home)
    await improvements.decide("imp1", "accept", user_id="alex", editors=saves(home))
    await improvements.decide("imp2", "decline", reason="not_an_issue", note="It was fine", user_id="alex")
    applied = await home_now(home)
    restarted = Improvements(home.hass, None)
    await restarted.start()
    assert restarted.get("imp1")["status"] == "applied" and restarted.get("imp2")["status"] == "declined"
    assert restarted.get("imp2")["decisions"][-1]["note"] == "It was fine"
    assert await home_now(home) == applied  # Starting again changes nothing in Home Assistant.
    # One click to undo, even after a restart: back to exactly what was there.
    assert (await restarted.decide("imp1", "undo", user_id="alex", editors=saves(home)))["status"] == "awaiting_review"
    assert await home_now(home) == before


@pytest.mark.asyncio
async def test_an_earlier_versions_improvements_dont_carry_over_and_todays_do(home):
    """Improvements an earlier version saved in a shape this one doesn't keep (0.39's: one user_id, its tests, no
    conversation or goals it fixes) start fresh: they're left behind at start, so nothing reads a field they don't have.
    Today's improvements carry over as they were."""
    improvements = await reviewing(home, improvement(home))
    await improvements.decide("imp1", "accept", user_id="alex", editors=saves(home))
    saved = await improvements.store.async_load()
    saved["improvements"]["old1"] = {"id": "old1", "title": "Earlier", "status": "applied", "user_id": "alex",
                                     "tests": [], "changes": [], "decisions": [], "history": []}
    await improvements.store.async_save(saved)
    restarted = Improvements(home.hass, None)
    await restarted.start()
    assert list(restarted.items) == ["imp1"] and restarted.get("imp1")["status"] == "applied"


@pytest.mark.asyncio
async def test_the_households_decisions_feed_the_next_run(home):
    """A plain history of what the household decided: each decided improvement's latest decision, with its note and
    reason, its status, and the changes it was about."""
    items = [improvement(home, f"i{n}", title=f"Idea {n}", fixes=[f"s{n}:1"]) for n in range(4)]
    improvements = await reviewing(home, *items)
    await improvements.decide("i1", "decline", note="Not this", user_id="alex")
    await improvements.decide("i2", "decline", reason="not_an_issue", user_id="alex")
    await improvements.decide("i3", "revise", note="Name it after the room", user_id="alex")
    feedback = {f["title"]: f for f in improvements.feedback()}
    assert "Idea 0" not in feedback  # Undecided: nothing to learn from yet.
    assert {t: (f["decision"], f["note"], f["reason"], f["status"]) for t, f in feedback.items()} == {
        "Idea 1": ("decline", "Not this", None, "declined"), "Idea 2": ("decline", "", "not_an_issue", "declined"),
        "Idea 3": ("revise", "Name it after the room", None, "awaiting_review")}
    assert feedback["Idea 3"]["changes"] == items[3]["changes"]  # The note travels with what it was about.


# One decision at a time per improvement.

@pytest.mark.asyncio
@pytest.mark.parametrize("first", ["accept", "decline"])
async def test_two_different_decisions_at_once_never_cross(home, first):
    improvements = await reviewing(home, improvement(home))
    before = await home_now(home)
    second = "decline" if first == "accept" else "accept"
    one, two = await asyncio.gather(improvements.decide("imp1", first, user_id="alex", editors=saves(home)),
                                    improvements.decide("imp1", second, user_id="jordan", editors=saves(home)),
                                    return_exceptions=True)
    # The second waits for the first, then sees its result: it isn't open any more.
    assert isinstance(two, CaseError) and one["status"] == ("applied" if first == "accept" else "declined")
    item = improvements.get("imp1")
    assert [(d["decision"], d["by"]) for d in item["decisions"]] == [(first, "alex")]
    if first == "accept":
        assert (await home_now(home))["household"]["people"] == {USER: [LINE]} and item["application"]["steps"]
    else:
        assert await home_now(home) == before


@pytest.mark.asyncio
async def test_two_improvements_accepted_at_once_never_interleave_their_changes(home):
    er.async_get(home.hass).async_get_or_create("light", "recorded", "back", suggested_object_id="back")
    improvements = await reviewing(home, alias_item(home, "a"), alias_item(home, "b", "light.back", "back light"))
    writing, release = asyncio.Event(), asyncio.Event()

    async def slow_write(hass, kind, target_, value):  # The first change is mid-write when the second is accepted.
        writing.set()
        await release.wait()
        await changeset.write(hass, kind, target_, value)
    improvements.changesets = Changesets(home.hass, writer=slow_write)
    first = asyncio.create_task(improvements.decide("a", "accept", user_id="alex"))
    await asyncio.wait_for(writing.wait(), 5)
    second = await asyncio.wait_for(improvements.decide("b", "accept", user_id="jordan"), 5)
    # It waited its turn: nothing of it applied, still up for review, and saying why.
    assert second["outcome"]["status"] == "busy" and second["status"] == "awaiting_review"
    assert "back light" not in (await changeset.read(home.hass, "alias", "light.back") or [])
    assert panel.outcome_text(second) == "Another change is still being applied."
    assert "light.back" not in str(improvements.changesets.journal)  # Nothing of it joined the change in flight.
    release.set()
    assert (await asyncio.wait_for(first, 5))["status"] == "applied"
    assert "front light" in await changeset.read(home.hass, "alias", "light.front")
    # Once the first is in, the second can be accepted.
    assert (await improvements.decide("b", "accept", user_id="jordan"))["status"] == "applied"
    assert "back light" in await changeset.read(home.hass, "alias", "light.back")
    assert await left_open(home.hass) is None  # No change left half-done.


# Applying: all of it or none of it.

@pytest.mark.asyncio
async def test_accepting_saves_settings_and_runs_nothing(home, effect_guards):
    improvements = await reviewing(home, improvement(home))
    result = await improvements.decide("imp1", "accept", user_id="alex", editors=saves(home))
    assert result["status"] == "applied" and result["application"]["scripts_executed"] == 0
    assert result["application"]["by"] == "alex" and len(result["application"]["steps"]) == 6
    await home.hass.async_block_till_done()
    assert home.hass.states.get("script.close_garage").state == "off"  # Loaded by HA, never run.
    assert effect_guards == []  # No device was asked to do anything.
    assert panel.outcome_text(result) == "Applied. Assist uses it from your next request; no script was run."
    sent = await ws(panel.ws_get, home.hass, improvement_id="imp1")
    assert sent[1]["status"] == "applied" and [c["value"] for c in sent[1]["choices"]] == ["undo"]


@pytest.mark.asyncio
async def test_a_failed_accept_leaves_nothing_applied_and_stays_up_for_review_saying_so(home):
    improvements = await reviewing(home, improvement(home))
    before = await home_now(home)
    writer = failing_writes(4)
    improvements.changesets = Changesets(home.hass, writer=writer)
    sent = await ws(panel.ws_decide, home.hass, writer.editor(home.hass), improvement_id="imp1", decision="accept",
                    note="")
    assert sent[0] == "ok" and sent[1]["status"] == "awaiting_review" and sent[1]["ok"] is False
    assert sent[1]["result"].startswith("Nothing was changed. ") and "failed: disk full." in sent[1]["result"]
    assert sent[1]["result"].endswith("The steps already done were undone, so you can try again.")
    assert await home_now(home) == before
    assert "[Garage](/assist-improver?id=imp1)" in notifications(home.hass)[NOTIFICATION]["message"]  # Still waiting.
    # Trying again works once the fault is gone.
    improvements.changesets = Changesets(home.hass)
    assert (await improvements.decide("imp1", "accept", user_id="alex", editors=saves(home)))["status"] == "applied"


@pytest.mark.asyncio
async def test_a_value_that_doesnt_take_its_new_value_undoes_the_whole_change(home):
    hass = home.hass
    steps, _, _ = await plan_steps(hass, proposal(home))
    before = await home_now(home)

    async def ignoring_aliases(hass, kind, target_, value):  # Home Assistant keeps the old name.
        if kind != "alias":
            await changeset.write(hass, kind, target_, value)
    result = await Changesets(hass, writer=ignoring_aliases).run("imp1", "apply", steps, saves(home))
    assert result["status"] == "rolled_back" and "didn't take the new value" in result["failure"]["error"]
    assert result["failure"]["what"] == "light.front" and not result["stuck"]
    assert await home_now(home) == before


@pytest.mark.asyncio
async def test_a_change_that_cant_be_fully_undone_after_a_failure_needs_attention_and_says_what(home):
    improvements = await reviewing(home, improvement(home))
    # The third write fails (the alias); undoing the new script fails too.
    async def act(n, write, kind, target_):
        if n == 3 or (n > 3 and target_ == "close_garage"):
            raise OSError("read-only")
        await write()
    write = counted(act)
    improvements.changesets = Changesets(home.hass, writer=write)
    result = await improvements.decide("imp1", "accept", user_id="alex", editors=write.editor(home.hass).save)
    assert result["status"] == "needs_attention" and result["outcome"]["stuck"] == [
        {"what": "Close garage", "why": "read-only"}]
    assert panel.outcome_text(result) == "Some changes couldn't be undone after a failure. Close garage: read-only."
    # Partial application is visible, never reported as complete: the script that stayed is really there.
    now = await home_now(home)
    assert now["close_garage"] is not None and now["press_garage"]["sequence"] == proposal(home)["tested"][
        "scripts"]["press_garage"]["sequence"] and "description" not in now["press_garage"]
    assert "[Garage]" in notifications(home.hass)[NOTIFICATION]["message"]  # It waits on the household.
    assert await left_open(home.hass) is None  # Reported now, not again at the next start.


# Undo.

@pytest.mark.asyncio
async def test_undo_puts_back_what_was_there_and_leaves_what_you_changed_since(home):
    improvements = await reviewing(home, improvement(home))
    before = await home_now(home)
    await improvements.decide("imp1", "accept", user_id="alex", editors=saves(home))
    await changeset.write(home.hass, "alias", "light.front", ["my light"])  # Alex renames it his own way.
    result = await improvements.decide("imp1", "undo", user_id="alex", editors=saves(home))
    assert result["status"] == "awaiting_review" and result["outcome"]["left"] == [
        {"what": "light.front", "why": "It was changed again after the improvement was applied."}]
    assert panel.outcome_text(result) == "Undone: what was there before is back. Left as you changed it: light.front"
    now = await home_now(home)
    assert now.pop("aliases") == ["my light"]
    assert now == {k: v for k, v in before.items() if k != "aliases"}


@pytest.mark.asyncio
async def test_an_undone_improvement_is_up_for_review_again_to_accept_revise_or_decline(home):
    improvements = await reviewing(home, improvement(home))
    await improvements.decide("imp1", "accept", user_id="alex", editors=saves(home))
    applied = await home_now(home)
    await improvements.decide("imp1", "undo", user_id="alex", editors=saves(home))
    assert improvements.get("imp1")["status"] == "awaiting_review"
    assert choices(improvements.get("imp1")) == ["accept", "revise", "decline"]
    assert (await improvements.decide("imp1", "accept", user_id="alex", editors=saves(home)))["status"] == "applied"
    assert await home_now(home) == applied
    assert [d["decision"] for d in improvements.get("imp1")["decisions"]] == ["accept", "undo", "accept"]


@pytest.mark.asyncio
async def test_an_undo_that_fails_midway_leaves_the_improvement_fully_applied(home):
    improvements = await reviewing(home, improvement(home))
    await improvements.decide("imp1", "accept", user_id="alex", editors=saves(home))
    applied = await home_now(home)
    writer = failing_writes(3)
    improvements.changesets = Changesets(home.hass, writer=writer)
    result = await improvements.decide("imp1", "undo", user_id="alex", editors=writer.editor(home.hass).save)
    assert result["outcome"]["status"] == "rolled_back" and result["status"] == "applied"
    assert await home_now(home) == applied and choices(result) == ["undo"]
    improvements.changesets = Changesets(home.hass)
    assert (await improvements.decide("imp1", "undo", user_id="alex", editors=saves(home)))["status"] == \
        "awaiting_review"


@pytest.mark.asyncio
async def test_an_undo_that_can_neither_finish_nor_be_reversed_needs_attention_and_says_what(home):
    improvements = await reviewing(home, improvement(home))
    await improvements.decide("imp1", "accept", user_id="alex", editors=saves(home))

    async def act(n, write, kind, target_):  # The third undo write fails; putting the lines back fails too.
        if n == 3 or (n > 3 and kind == "household"):
            raise OSError("read-only")
        await write()
    write = counted(act)
    improvements.changesets = Changesets(home.hass, writer=write)
    result = await improvements.decide("imp1", "undo", user_id="alex", editors=write.editor(home.hass).save)
    assert result["status"] == "needs_attention" and result["outcome"]["stuck"] == [
        {"what": "Household knowledge", "why": "read-only"}]
    assert panel.outcome_text(result) == ("Some changes couldn't be undone after a failure. Household knowledge: "
                                          "read-only.")
    assert "[Garage](/assist-improver?id=imp1)" in notifications(home.hass)[NOTIFICATION]["message"]


# Conflicts, and changes already in place.

@pytest.mark.asyncio
async def test_a_value_changed_since_testing_is_a_conflict_and_nothing_is_written(home):
    improvements = await reviewing(home, improvement(home))
    await users_edit(home.hass, "script", "press_garage", {"alias": "Mine", "sequence": []})  # Alex's own edit.
    before = await home_now(home)
    # The review shows it before anything is tried: checked against Home Assistant now.
    review = (await ws(panel.ws_get, home.hass, improvement_id="imp1"))[1]
    assert [(c["target"], c["why"]) for c in review["conflicts"]] == [
        ("press_garage", "It was changed in Home Assistant after the improvement was tested.")]
    editor = Editors(home.hass)
    result = await improvements.decide("imp1", "accept", user_id="alex", editors=editor.save)
    assert editor.calls == []  # Not a script saved.
    assert result["status"] == "conflict" and result["outcome"]["status"] == "conflict"
    assert panel.outcome_text(result) == ("Nothing was changed: Press garage: It was changed in Home Assistant after "
                                          "the improvement was tested.")
    assert await home_now(home) == before  # Alex's edit stays; nothing else of the improvement went in either.
    assert choices(improvements.get("imp1")) == []  # The household sends it back, or not (SPEC.md P0.7).


async def _script_outside_scripts_yaml(home):
    return {"setup": {**proposal(home)["setup"], "scripts": {"ghost": {"alias": "Ghost", "sequence": []}}},
            "tested": {"scripts": {"ghost": {"alias": "Ghost", "sequence": [{"stop": "x"}]}}, "exposure": {}}}, \
        "isn't kept by Home Assistant's script editor (scripts.yaml)"


async def _script_home_assistant_refuses(home):
    return {"setup": {**proposal(home)["setup"], "scripts": {"bad": {"alias": "Bad", "sequence": [
        {"action": "no dot here"}]}}}}, "Home Assistant doesn't accept this script"


async def _device_gone(home):
    return {"setup": {**proposal(home)["setup"], "aliases": {"light.gone": ["gone light"]}}}, \
        "no longer in Home Assistant"


async def _assistant_gone(home):
    return {"provider": {"config_entry_id": home.provider.entry_id, "subentry_id": "gone"}}, \
        "The assistant it was tested on is no longer set up."


async def _assistant_without_assist(home):
    other = _second_assistant(home.hass, [])
    return {"provider": {"config_entry_id": other.entry_id, "subentry_id": "sub2", "title": "Haiku"}}, \
        "doesn't use Assist"


async def _replaced_line_gone(home):
    return {"changes": [{"kind": "preference", "for": "everyone", "text": "Doors close at 11.",
                         "replaces": ["Doors close at 10."]}]}, "has changed since it was tested"


async def _instructions_home_assistant_cant_read(home):
    await changeset.write(home.hass, "prompt", target(home), PROMPT + "{% if is_state('sun.sun', 'below_horizon') %}"
                          "\nSay good evening.\n{% endif %}\n")
    return {"changes": [{"kind": "preference", "for": "everyone", "text": "Say good evening after dark.",
                         "replaces": ["{% endif %}"]}]}, "Home Assistant can't read the edited instructions"


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", [_script_outside_scripts_yaml, _script_home_assistant_refuses, _device_gone,
                                      _assistant_gone, _assistant_without_assist, _replaced_line_gone,
                                      _instructions_home_assistant_cant_read], ids=lambda s: s.__name__.strip("_"))
async def test_every_reason_an_improvement_cant_apply_now_is_a_conflict_with_a_reason_and_nothing_is_written(
        home, scenario):
    overrides, why = await scenario(home)
    improvements = await reviewing(home, improvement(home, **overrides))
    before = await home_now(home)
    result = await improvements.decide("imp1", "accept", user_id="alex", editors=saves(home))
    assert result["status"] == "conflict", result["outcome"]
    assert any(why in c["why"] for c in result["outcome"]["conflicts"]), result["outcome"]["conflicts"]
    assert await home_now(home) == before  # Not even the changes that would have gone in fine.


@pytest.mark.asyncio
async def test_changes_already_in_place_are_no_ops_shown_as_such_and_never_undone(home):
    hass = home.hass
    await changeset.write(hass, "alias", "light.front", [None, "front light"])
    await (await household.async_setup(hass)).save({"everyone": [], "people": {USER: [LINE]}})
    improvements = await reviewing(home, improvement(home))
    before = await home_now(home)
    review = (await ws(panel.ws_get, hass, improvement_id="imp1"))[1]
    already = [(c["name"], c["kind"]) for c in review["changes"] if c.get("already")]
    assert already == [("light.front", "aliases"), ("Household knowledge", "household")]
    assert all(c["rows"] == [] for c in review["changes"] if c.get("already"))
    assert [c["kind"] for c in review["changes"] if not c.get("already")] == ["script", "script", "exposure",
                                                                              "exposure"]
    settings = next(f for f in review["safety"] if f.startswith("Only settings change"))
    assert "name added" not in settings and "household line" not in settings and "switched" not in settings
    result = await improvements.decide("imp1", "accept", user_id="alex", editors=saves(home))
    assert result["status"] == "applied" and {a["kind"] for a in result["application"]["already"]} == {
        "alias", "household"}
    assert hass.data[household.KEY].knowledge == {"everyone": [], "people": {USER: [LINE]}}  # Not added twice.
    await improvements.decide("imp1", "undo", user_id="alex", editors=saves(home))
    assert await home_now(home) == before  # The name and line that were already there stay.


@pytest.mark.asyncio
async def test_an_improvement_switches_no_assistant_every_one_on_assist_reads_its_lines(home):
    hass = home.hass
    other = _second_assistant(hass, ["assist"])
    improvements = await reviewing(home, line_item(home, "imp1"))
    steps, already, conflicts = await plan_steps(hass, improvements.get("imp1"))
    assert [s["kind"] for s in steps] == ["household"] and not conflicts
    await improvements.decide("imp1", "accept", user_id="alex")
    assert other.subentries["sub2"].data == {"prompt": "Hi.", "llm_hass_api": ["assist"]}
    assert home.provider.subentries["sub1"].data == {**home.provider.subentries["sub1"].data, "prompt": PROMPT,
                                                     "llm_hass_api": ["assist"]}


# Recovery of a change Home Assistant stopped in the middle of.

@pytest.mark.asyncio
async def test_a_change_home_assistant_stopped_midway_is_undone_once_it_has_started_and_you_are_told(home):
    """Once HA has started, what the improver wrote itself after the scripts is undone; the scripts, which only the
    panel saves, and what came before them (the name), when the panel is next opened, before anything else; then
    you're told, once (DETAILS P12.11)."""
    from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
    from homeassistant.core import CoreState
    from custom_components.assist_improver import async_setup_entry, async_unload_entry
    hass = home.hass

    improvements = await reviewing(home, improvement(home))
    before = await home_now(home)
    hass.set_state(CoreState.starting)
    writer = crashing_after(4)  # The name, both scripts, then one exposure: Home Assistant stops.
    improvements.changesets = Changesets(hass, writer=writer)
    with pytest.raises(Crash):
        await improvements.decide("imp1", "accept", user_id="alex", editors=writer.editor(hass).save)
    crashed = await home_now(home)
    assert crashed != before
    entry = fake_entry({})
    await async_setup_entry(hass, entry)  # Home Assistant starting again.
    try:
        assert await home_now(home) == crashed  # Not before Home Assistant (and its scripts) has started.
        assert await ws(panel.ws_changeset_finish, hass, type=f"{DOMAIN}/changeset/finish") == ("ok", {})
        assert await home_now(home) == crashed
        hass.set_state(CoreState.running)
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
        await hass.async_block_till_done()
        waiting = await home_now(home)
        # What the improver wrote after the scripts is back; they wait for the panel, neither exposed nor run, and
        # the name written before them with them.
        assert {k: v for k, v in waiting.items() if "garage" not in k and k != "aliases"} == {
            k: v for k, v in before.items() if "garage" not in k and k != "aliases"}
        assert waiting["aliases"] == crashed["aliases"] != before["aliases"]
        assert waiting["close_garage"] is not None and OUTCOME_NOTIFICATION not in notifications(hass)
        improvements = entry.runtime_data.improvements
        result = await improvements.decide("imp1", "accept", user_id="alex", editors=saves(home))
        assert result["outcome"] == {"status": "busy", "error": "Another change is still being applied."}
        editor = Editors(hass)
        sent = await ws(panel.ws_changeset_finish, hass, editor, type=f"{DOMAIN}/changeset/finish")
        message = "Applying the improvement “Garage” was interrupted. Its changes were undone, so nothing of it is in place."
        assert sent == ("ok", {"result": "It was interrupted. Its changes were undone, so nothing of it is in place."})
        assert editor.calls == [("POST", "script", "press_garage"), ("DELETE", "script", "close_garage")]  # Newest first.
        assert await home_now(home) == before and await editor.get("script", "close_garage") is None
        assert notifications(hass)[OUTCOME_NOTIFICATION]["message"] == message
        item = improvements.get("imp1")
        assert item["status"] == "awaiting_review" and item["history"][-1]["event"] == "interrupted"
        assert [i["id"] for i in improvements.pending()] == ["imp1"]  # Still up for review.
        assert await left_open(hass) is None  # Finished once, not at every start or panel load.
        assert await ws(panel.ws_changeset_finish, hass, type=f"{DOMAIN}/changeset/finish") == ("ok", {})
    finally:
        await async_unload_entry(hass, entry)


@pytest.mark.asyncio
@pytest.mark.parametrize("gone", ["closed", "hung"])
async def test_a_change_whose_panel_went_away_midway_is_finished_by_the_next_panel_opened(home, monkeypatch, gone):
    """The panel saving a change's script went away (its tab closed, or it stopped answering): the change stops where
    it stands, nothing is recorded as decided, and it says it was interrupted. Nothing else applies meanwhile; the
    next panel opened finishes it first, its scripts too, says what that did, and the improvement can be accepted
    again (DETAILS P12.11). One that stopped answering may still save the script (a phone's tab put away, then back):
    nothing is finished until it answers, so what it saved late is undone too, never left in place unreported."""
    from homeassistant.core import CoreState
    hass = home.hass
    hass.set_state(CoreState.running)
    monkeypatch.setattr(panel.EditorChannel, "TIMEOUT", 0.2)
    improvements = await reviewing(home, improvement(home))
    before = await home_now(home)
    first, editor = Connection(), Editors(hass)
    asked = await open_panel(hass, first, editor, answering=False)
    deciding = hass.async_create_task(command(hass, first, panel.ws_decide, type=f"{DOMAIN}/decide",
                                              improvement_id="imp1", decision="accept", note=""))
    while not asked:
        await asyncio.sleep(0.01)
    finishing = Editors(hass)
    if gone == "closed":
        await editor.save(asked[0]["kind"], asked[0]["id"], asked[0]["config"])  # Saved, but its answer never comes.
        first.close()
    else:  # Another admin opens the panel meanwhile: its finish waits for the change under way.
        meanwhile = hass.async_create_task(ws(panel.ws_changeset_finish, hass, finishing,
                                              type=f"{DOMAIN}/changeset/finish"))
    assert await deciding == ("error", "interrupted", "It was interrupted before it finished.")
    assert asked[0]["id"] == "close_garage" and len(asked) == 1
    item = improvements.get("imp1")
    assert item["status"] == "awaiting_review" and item["decisions"] == []
    assert (await ws(panel.ws_decide, hass, improvement_id="imp1", decision="accept", note=""))[1]["result"] == (
        "Another change is still being applied.")
    if gone == "hung":  # Its save may still land: another panel finishes nothing until it answers.
        assert await meanwhile == ("ok", {})
        assert await ws(panel.ws_changeset_finish, hass, finishing, type=f"{DOMAIN}/changeset/finish") == ("ok", {})
        assert finishing.calls == [] and await left_open(hass)
        await editor.save(asked[0]["kind"], asked[0]["id"], asked[0]["config"])  # It lands late,
        await settle(hass)
        assert (await command(hass, first, panel.ws_editors_saved, type=f"{DOMAIN}/editors/saved",
                              key=asked[0]["key"]))[0] == "ok"  # and it answers.
    message = "Applying the improvement “Garage” was interrupted. Its changes were undone, so nothing of it is in place."
    assert await ws(panel.ws_changeset_finish, hass, finishing, type=f"{DOMAIN}/changeset/finish") == (
        "ok", {"result": "It was interrupted. Its changes were undone, so nothing of it is in place."})
    assert finishing.calls == [("DELETE", "script", "close_garage")] and await home_now(home) == before
    assert notifications(hass)[OUTCOME_NOTIFICATION]["message"] == message
    assert improvements.get("imp1")["history"][-1]["event"] == "interrupted"
    sent = await ws(panel.ws_decide, hass, improvement_id="imp1", decision="accept", note="")
    assert sent[1]["status"] == "applied"


@pytest.mark.asyncio
async def test_the_improver_never_writes_a_configuration_file_the_panel_saves_scripts_through_has_script_editor(
        home, monkeypatch):
    """Frenck's review (hacs/default 12953): an integration doesn't write the household's configuration files. Through
    an accept, an undo, and a change HA stopped midway that the panel finishes, every file written in HA's
    configuration folder outside its own storage (.storage, where HA keeps the registries, settings and the improver's
    stores) is written by the panel's half: HA's script editor, called by the panel as the signed-in admin."""
    import builtins
    import os
    import sys
    import threading
    from homeassistant.core import CoreState
    import editors
    hass = home.hass
    hass.set_state(CoreState.running)
    config, storage = os.path.realpath(hass.config.config_dir), os.path.realpath(hass.config.path(".storage"))
    written, local = [], threading.local()

    def owner(frame):  # The panel's half, if HA's script editor was called by it (Editors.save).
        while frame is not None:
            if frame.f_code.co_filename == editors.__file__ and frame.f_code.co_name == "save":
                return "panel"
            frame = frame.f_back
        return "not the panel"

    def wrote(path):
        if isinstance(path, (str, os.PathLike)):
            written.append((os.path.realpath(os.fspath(path)), getattr(local, "owner", None)
                            or owner(sys._getframe(2))))
    real_submit, real_open, real_os_open = hass.async_add_executor_job, builtins.open, os.open
    real_replace, real_rename = os.replace, os.rename

    def submit(target, *args):  # A file written in HA's executor: by whoever handed it the job.
        who = owner(sys._getframe(1))

        def job(*job_args):
            local.owner = who
            try:
                return target(*job_args)
            finally:
                local.owner = None
        return real_submit(job, *args)

    def opened(file, mode="r", *args, **kwargs):
        if any(m in mode for m in "wax+"):
            wrote(file)
        return real_open(file, mode, *args, **kwargs)

    def os_opened(path, flags, *args, **kwargs):
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC):
            wrote(path)
        return real_os_open(path, flags, *args, **kwargs)
    monkeypatch.setattr(hass, "async_add_executor_job", submit)
    monkeypatch.setattr(builtins, "open", opened)
    monkeypatch.setattr(os, "open", os_opened)
    monkeypatch.setattr(os, "replace", lambda src, dst, **kw: (wrote(dst), real_replace(src, dst, **kw))[1])
    monkeypatch.setattr(os, "rename", lambda src, dst, **kw: (wrote(dst), real_rename(src, dst, **kw))[1])

    def config_files(who):
        return [path for path, by in written if by == who
                and path.startswith(config + os.sep) and not path.startswith(storage + os.sep)]

    improvements = await reviewing(home, improvement(home))
    before = await home_now(home)
    assert (await ws(panel.ws_decide, hass, improvement_id="imp1", decision="accept", note=""))[1]["status"] == \
        "applied"
    assert (await ws(panel.ws_decide, hass, improvement_id="imp1", decision="undo", note=""))[1]["ok"]
    await hass.async_block_till_done()
    assert config_files("not the panel") == []
    assert os.path.realpath(hass.config.path("scripts.yaml")) in config_files("panel")  # It sees a script saved.
    writer = crashing_after(4)
    improvements.changesets = Changesets(hass, writer=writer)
    with pytest.raises(Crash):
        await improvements.decide("imp1", "accept", user_id="alex", editors=writer.editor(hass).save)
    improvements.changesets = Changesets(hass)
    await improvements.changesets.load()
    assert (await improvements.recover())["status"] == "waiting"  # HA started again.
    assert (await ws(panel.ws_changeset_finish, hass, type=f"{DOMAIN}/changeset/finish"))[1]["result"]
    await hass.async_block_till_done()
    assert await home_now(home) == before and config_files("not the panel") == []


@pytest.mark.asyncio
async def test_a_change_that_cant_be_undone_after_a_restart_needs_attention_and_says_what(home):
    improvements = await reviewing(home, improvement(home))
    writer = crashing_after(4)
    improvements.changesets = Changesets(home.hass, writer=writer)
    with pytest.raises(Crash):
        await improvements.decide("imp1", "accept", user_id="alex", editors=writer.editor(home.hass).save)
    await changeset.write(home.hass, "alias", "light.front", ["my light"])  # Alex renamed it before the restart.
    restarted = Improvements(home.hass, None)
    await restarted.start()
    assert (await restarted.recover())["status"] == "waiting"  # Its scripts wait for the panel.
    await restarted.recover(saves(home))
    item = restarted.get("imp1")
    assert item["status"] == "needs_attention"
    assert notifications(home.hass)[OUTCOME_NOTIFICATION]["message"] == (
        "Applying the improvement “Garage” was interrupted. Some changes couldn't be undone. "
        "light.front: It was changed by someone else meanwhile.")
    assert (await home_now(home))["aliases"] == ["my light"]  # Alex's name stays.
    assert "[Garage](/assist-improver?id=imp1)" in notifications(home.hass)[NOTIFICATION]["message"]
    restarted_again = Improvements(home.hass, None)
    await restarted_again.start()
    assert await restarted_again.recover(saves(home)) is None
    assert restarted_again.get("imp1")["history"] == item["history"]  # Nothing left to recover.


@pytest.mark.asyncio
async def test_the_restart_notification_names_the_improvement_as_the_household_knows_it(home):
    improvements = await reviewing(home, improvement(home, copy={"headline": "Open both garage doors in one go"}))
    writer = crashing_after(2)
    improvements.changesets = Changesets(home.hass, writer=writer)
    with pytest.raises(Crash):
        await improvements.decide("imp1", "accept", user_id="alex", editors=writer.editor(home.hass).save)
    restarted = Improvements(home.hass, None)
    await restarted.start()
    await restarted.recover(saves(home))
    assert "“Open both garage doors in one go”" in notifications(home.hass)[OUTCOME_NOTIFICATION]["message"]


# The notification and the sidebar count: what waits on the household.

@pytest.mark.asyncio
async def test_the_notification_lists_only_improvements_waiting_on_you_by_their_headline(home):
    improvements = await reviewing(
        home, improvement(home, "w", copy={"headline": "Open both garage doors in one go"}),
        improvement(home, "u", title="Unwritten idea", copy=None),  # Waiting for its write-up: not yet.
        improvement(home, "n", title="Half done", status="needs_attention"),
        improvement(home, "x", title="Applied idea", status="applied"))
    note = notifications(home.hass)[NOTIFICATION]
    assert note["title"] == "2 improvements to review"
    assert "[Open both garage doors in one go](/assist-improver?id=w)" in note["message"]
    assert "[Half done](/assist-improver?id=n)" in note["message"]
    assert "Unwritten idea" not in note["message"] and "Applied idea" not in note["message"]
    await improvements.decide("w", "decline", user_id="alex")
    note = notifications(home.hass)[NOTIFICATION]
    assert note["title"] == "1 improvement to review"
    assert note["message"].endswith("Review this improvement in [Assist Improver](/assist-improver).")


# Why it's safe: stated by code from the tests and the changeset, never by a model.

def step(kind, before, after, what="Thing", target=None):
    return {"kind": kind, "target": target or what, "before": before, "after": after, "what": what}


@pytest.mark.parametrize("steps, expected", [
    ([step("alias", [], ["desk"], "Desk"), step("household", {}, {}, "Household knowledge")],
     ["Only settings change: 1 name added, 1 household line. Nothing runs when you accept."]),
    ([step("exposure", True, False, "Porch light")],
     ["Only settings change: 1 device hidden from Assist. Nothing runs when you accept."]),
    ([step("script", None, {"alias": "Close garage"}, "Close garage"),
      step("exposure", False, True, "Close garage", "script.close_garage"),
      step("exposure", False, True, "Porch light", "light.porch"),
      step("exposure", False, True, "Reading", "scene.reading"), step("exposure", False, True, "Hall", "light.hall")],
     ["Only settings change: 1 new script, 1 script made visible to Assist, 2 devices made visible to Assist, 1 scene "
      "made visible to Assist. Nothing runs when you accept."]),
    ([step("script", {"a": 1}, {"a": 2}, "One"), step("script", {"b": 1}, {"b": 2}, "Two"),
      step("alias", [], ["x"]), step("alias", [], ["y"]), step("prompt", "a", "b"),
      step("prompt", "c", "d"), step("assistant_api", ["assist", "other"], ["assist"]),
      step("household", {}, {}), step("household", {}, {})],
     ["Only settings change: 2 scripts updated, 2 names added, 2 instruction changes, 1 assistant's LLM APIs "
      "narrowed, 2 household lines. Nothing runs when you accept."]),
    ([], []),  # Everything already in place: no setting changes, and it doesn't claim any.
    # What runs once it's accepted: an automation saved or turned on, live when it's triggered, its own save included
    # (P12.3.1): it doesn't claim nothing runs as it's accepted.
    ([step("automation", None, {"alias": "Porch at dusk"}, "Porch at dusk")],
     ["Changes: 1 new automation. The automation runs when it's triggered."]),
    ([step("automation_on", False, True, "Porch at night", "automation.porch_at_night")],
     ["Changes: 1 automation turned on. The automation runs when it's triggered."]),
    ([step("automation", {"alias": "A"}, {"alias": "A, later"}, "A"), step("automation", None, {"alias": "B"}, "B")],
     ["Changes: 1 automation updated, 1 new automation. The automations run when they're triggered."]),
    ([step("automation_on", True, False, "Porch at night", "automation.porch_at_night")],
     ["Only settings change: 1 automation turned off. Nothing runs when you accept."]),
    ([step("scene", None, {"name": "Reading"}, "Reading"), step("exposure", False, True, "Reading", "scene.reading")],
     ["Only settings change: 1 new scene, 1 scene made visible to Assist. Nothing runs when you accept."]),
    ([step("entity", {"name": None, "disabled_by": "user"}, {"name": None, "disabled_by": None}, "Spare sensor")],
     ["Only settings change: 1 device setting changed. Nothing runs when you accept; enabling Spare sensor reloads "
      "its integration."]),
    ([step("entity", {"disabled_by": "user"}, {"disabled_by": None}, "A"),
      step("entity", {"disabled_by": "user"}, {"disabled_by": None}, "B"),
      step("entity", {"name": None, "disabled_by": None}, {"name": "C", "disabled_by": None}, "C")],
     ["Only settings change: 3 device settings changed. Nothing runs when you accept; enabling A and B reloads their "
      "integrations."]),
], ids=["lines-and-names", "hiding", "control-gained", "plurals", "nothing-to-change", "new-automation",
        "automation-on", "automations", "automation-off", "new-scene-exposed", "enabled", "enabled-two"])
def test_why_its_safe_states_what_the_test_and_the_changeset_show(steps, expected):
    facts = panel.safety(steps)
    assert facts == ["Tested on your own conversations, replayed in a sandboxed copy of your home: nothing in your "
                     "home moved."] + expected + ["Checked against your Home Assistant first, applied all or "
                                                  "nothing, and one click to undo."]


@pytest.mark.asyncio
async def test_why_its_safe_never_comes_from_the_written_copy(home):
    item = improvement(home, copy={"status": "written", "headline": "Open both garage doors in one go",
                                   "benefit": "Say open the garage and both open.",
                                   "safety": ["Totally safe, trust me."]})
    planned = await plan_steps(home.hass, item)
    shown = panel.review(item, planned)
    assert shown["title"] == "Open both garage doors in one go" and shown["benefit"] == "Say open the garage and both open."
    assert shown["safety"] == panel.safety(planned[0])
    assert "Totally safe, trust me." not in shown["safety"]
    assert shown["safety"][1].startswith("Only settings change: ")  # Stated by code from the changeset.


@pytest.mark.asyncio
async def test_the_note_on_motion_shows_only_where_a_door_valve_lock_or_alarm_is_involved(home):
    """DETAILS P11.5: by the devices the improvement involves (its changes touch, its goals want), their domain and a
    cover's device class, or where a goal it helped is a sequence goal (presses standing for a motion, P9.7.5); never
    by a name, its cause's words, or what its replays merely ran with."""
    hass = home.hass
    lights = {**improvement(home), "changes": [{"kind": "registry", "entity_id": "light.desk", "aliases": ["the lamp"]}],
              "cause": "Assist didn't know cover.garage_door_left is the left garage door.", "devices": ["light.desk"]}
    assert panel.review(lights, ([], [], []), hass=hass)["physical"] is False
    for entity_id in ("lock.back", "alarm_control_panel.house", "valve.water_main"):
        assert panel.review({**lights, "devices": ["light.desk", entity_id]}, ([], [], []), hass=hass)["physical"]
    hass.states.async_set("cover.side_gate", "closed", {"device_class": "gate", "friendly_name": "Side gate"})
    hass.states.async_set("switch.relay_1", "off", {"friendly_name": "Garage door"})  # A relay: no door of its own.
    assert panel.review({**lights, "devices": ["cover.side_gate"]}, ([], [], []), hass=hass)["physical"] is True
    assert panel.review({**lights, "devices": ["switch.relay_1"]}, ([], [], []), hass=hass)["physical"] is False
    for kind, noted in (("sequence", True), ("state", False)):  # The relay's presses, helped from 3 turns to 1.
        test = tested(judged={"1:0": {"summary": "Open the garage", "turn": 1, "root": "1:0", "kind": kind,
                                      "devices": ["switch.relay_1"]}})
        relay = {**lights, "devices": ["switch.relay_1"], "test": test}
        assert panel.review(relay, ([], [], []), hass=hass)["physical"] is noted
    unhelped = tested(met_before=True, after=("Which?", "Which?", "Done."), judged={"1:0": {
        "summary": "Open the garage", "turn": 1, "root": "1:0", "kind": "sequence", "devices": ["switch.relay_1"]}})
    assert panel.review({**lights, "test": unhelped}, ([], [], []), hass=hass)["physical"] is False


@pytest.mark.asyncio
async def test_an_automations_week_runs_once_before_its_write_up_and_one_that_cant_run_is_said(home, monkeypatch):
    """DETAILS P12.2.24: an improvement that changes an automation gets its simulated week once, before it's written
    up; one that couldn't run says so on its review, which still says when the automation runs, and the improvement
    goes on."""
    from custom_components.assist_improver import evidence
    dusk = {"alias": "Porch at dusk", "triggers": [{"trigger": "time", "at": "18:00:00"}],
            "actions": [{"action": "light.turn_on", "target": {"entity_id": "light.porch"}}]}
    item = improvement(home, changes=[{"kind": "automation", "id": "dusk", "config": dusk}],
                       setup={"automations": {"dusk": {**dusk, "id": "dusk"}}})
    improvements = await reviewing(home, item, line_item(home, "imp2"))
    asked = []

    async def failing(hass, recorder, item_):
        asked.append(item_["id"])
        raise RuntimeError("Its worker stopped.")
    monkeypatch.setattr(evidence, "improvement_week", failing)
    for improvement_id in ("imp1", "imp1", "imp2"):
        await improvements.add_week(improvement_id, None)
    assert asked == ["imp1"]  # Once, and only for one with an automation.
    assert improvements.get("imp1")["week"] == {"failed": "Its worker stopped.", "automations": []}
    assert panel.review(improvements.get("imp1"), ([], [], []), hass=home.hass)["automations"] == [
        {"name": "Porch at dusk", "change": "new", "when": "At 18:00", "if": None, "failed": True}]
    assert "week" not in improvements.get("imp2") and improvements.get("imp1")["status"] == "awaiting_review"


@pytest.mark.asyncio
async def test_a_runs_automation_weeks_run_side_by_side_within_one_budget(home, monkeypatch):
    """DETAILS P12.2.24, P6.13: the weeks of a run's improvements run at once, so the write-up waits about one week's
    budget, not one for each."""
    from custom_components.assist_improver import evidence
    dusk = {"alias": "Porch at dusk", "triggers": [{"trigger": "time", "at": "18:00:00"}], "actions": []}
    items = [improvement(home, i, changes=[{"kind": "automation", "id": i, "config": dusk}],
                         setup={"automations": {i: {**dusk, "id": i}}}) for i in ("imp1", "imp2")]
    improvements = await reviewing(home, *items)
    running, both = set(), asyncio.Event()

    async def week(hass, recorder, item_):
        running.add(item_["id"])
        if len(running) == 2:
            both.set()
        async with asyncio.timeout(2):  # One after the other, the first would wait here for the second in vain.
            await both.wait()
        return {"automations": []}
    monkeypatch.setattr(evidence, "improvement_week", week)
    await improvements.add_weeks(["imp1", "imp2"], None)
    assert [improvements.get(i)["week"] for i in ("imp1", "imp2")] == [{"automations": []}] * 2


@pytest.mark.parametrize("triggers, when", [
    ([{"trigger": "time_pattern", "minutes": "/5"}], "Every 5 minutes"),
    ([{"trigger": "time_pattern", "minutes": "/1"}], "Every minute"),
    ([{"trigger": "time_pattern", "minutes": 0}], "At minute 0 of every hour"),
    ([{"trigger": "time_pattern", "hours": "/2", "minutes": 30}], "Every 2 hours at minute 30"),
    ([{"trigger": "time", "at": "23:00:00"},
      {"trigger": "state", "entity_id": "binary_sensor.hall_motion", "to": "off", "for": {"minutes": 10}}],
     "At 23:00 or when Hall motion changes to off for 10 minutes"),
], ids=["every-5-minutes", "every-minute", "on-the-hour", "every-2-hours-at-30", "either-trigger"])
@pytest.mark.asyncio
async def test_when_an_automation_runs_reads_as_a_sentence(home, triggers, when):
    """DETAILS P12.2.24: its triggers, any of which runs it, in words a person says."""
    from custom_components.assist_improver.weeks import words
    home.hass.states.async_set("binary_sensor.hall_motion", "off", {"friendly_name": "Hall motion"})
    assert words(home.hass, {"triggers": triggers, "actions": []})["when"] == when


def test_an_edit_that_changes_only_what_each_run_sets_shows_its_runs_added_and_dropped():
    """Kai's words: "for a change to one you have, the runs it adds and drops." An edit from brightness 40 to 255 runs
    at the same times and leaves the light on, but each run sets something else: those are runs added and dropped."""
    from custom_components.assist_improver.weeks import week
    run = lambda brightness: {"automation": "automation.evening", "id": "evening", "at": "2026-03-02T18:00:00+00:00",
                              "changed": [{"entity_id": "light.desk", "old": "off", "new": "on",
                                           "attributes": {"brightness": brightness}}]}  # noqa: E731
    shown = week({"with": {"days": 7, "runs": [run(255)]}, "without": {"days": 7, "runs": [run(40)]}},
                 {"id": "evening"})
    assert [(r["sign"], r["changed"][0]["attributes"]) for r in shown["runs"]] == [
        ("+", {"brightness": 255}), ("-", {"brightness": 40})]
    assert shown["result"].startswith("1 run added at ") and ", 1 dropped at " in shown["result"]


@pytest.mark.parametrize("times, result", [
    (["21:00"], "1 run at 21:00"), (["07:00", "21:00"], "2 runs at 07:00 and 21:00"),
    (["21:00", "06:30", "07:00"], "3 runs at 06:30, 07:00 and 21:00"),
    (["06:00", "06:30", "07:00", "21:00"], "4 runs at 4 different times")])
def test_a_weeks_runs_read_by_their_times_of_day_as_a_list_is_said(times, result):
    from homeassistant.util import dt as dt_util
    from custom_components.assist_improver.weeks import week
    at = lambda time: dt_util.now().replace(hour=int(time[:2]), minute=int(time[3:]), second=0,  # noqa: E731
                                            microsecond=0).isoformat()
    runs = [{"automation": "automation.evening", "id": "evening", "at": at(t), "changed": []} for t in times]
    assert week({"with": {"days": 7, "runs": runs}, "without": None}, {"id": "evening"})["result"] == result


@pytest.mark.asyncio
async def test_an_automation_turned_on_or_off_is_named_as_home_assistant_names_it(home):
    home.hass.states.async_set("automation.porch_at_night", "on",
                               {"id": "1700000000000", "friendly_name": "Porch at night"})
    item = improvement(home, status="declined", changes=[{"kind": "automation", "id": "1700000000000", "on": False},
                                                         {"kind": "automation", "id": "gone", "on": True}])
    assert [c["name"] for c in panel.review(item, hass=home.hass)["changes"]] == [
        "Turn off automation “Porch at night”", "Turn on automation “gone”"]


# What the review shows, and what a decision did.

@pytest.mark.parametrize("item, text", [
    ({"status": "declined", "outcome": {"status": "recorded"}}, "Thanks, your decision is recorded."),
    ({"status": "awaiting_review", "outcome": {"status": "recorded"}, "decisions": [{"decision": "revise"}]},
     "Thanks. The improver is revising it now."),  # A revision runs at once (SPEC.md P0.7).
    ({"status": "declined", "outcome": {"status": "recorded"}, "decisions": [{"decision": "decline"}]},
     "Thanks. Declined, and its conversation is archived."),  # SPEC.md P0.11.
    ({"status": "applied", "outcome": {"status": "done"}, "changes": [{"kind": "script", "id": "garage",
                                                                       "config": {"alias": "Garage"}}]},
     "Applied. Assist uses it from your next request; no script was run."),
    ({"status": "applied", "outcome": {"status": "done"}, "changes": [{"kind": "registry", "entity_id": "light.desk",
                                                                       "aliases": ["desk"]}]},
     "Applied. Assist uses it from your next request."),  # No script among the changes: none to say wasn't run.
    ({"status": "undone", "outcome": {"status": "done", "left": []}}, "Undone: what was there before is back."),
    ({"status": "undone", "outcome": {"status": "done", "left": [{"what": "Desk"}, {"what": "Porch"}]}},
     "Undone: what was there before is back. Left as you changed it: Desk; Porch"),
    ({"status": "conflict", "outcome": {"status": "conflict", "conflicts": [{"what": "Desk", "why": "Gone."},
                                                                           {"what": "Porch", "why": "Changed."}]}},
     "Nothing was changed: Desk: Gone.; Porch: Changed."),
    ({"status": "awaiting_review", "outcome": {"status": "rolled_back", "failure": {"what": "Desk",
                                                                                     "error": "disk full"}}},
     "Nothing was changed. Desk failed: disk full. The steps already done were undone, so you can try again."),
    ({"status": "needs_attention", "outcome": {"status": "needs_attention", "stuck": [{"what": "Desk",
                                                                                       "why": "read-only"}]}},
     "Some changes couldn't be undone after a failure. Desk: read-only."),
    ({"status": "awaiting_review", "outcome": {"status": "busy", "error": "Another change is still being applied."}},
     "Another change is still being applied."),
], ids=["recorded", "revise", "decline", "applied-script", "applied", "undone", "undone-left", "conflict",
        "rolled-back", "needs-attention", "busy"])
def test_what_a_decision_did_is_said_in_a_sentence_or_two(item, text):
    assert panel.outcome_text(item) == text


ROW = {"improved": True, "newly_met": [], "turns_before": 2, "turns_after": 2, "side_effects_gone": [], "feedback": []}


@pytest.mark.parametrize("test, result", [
    ({"improved": False, "newly_met": ["Garage open"]}, ["not improved"]),
    ({"newly_met": ["Garage open"], "turns_before": 3, "turns_after": 1}, ["goal now met"]),
    ({"turns_before": 3, "turns_after": 1}, ["3 → 1 turns"]),
    ({"side_effects_gone": ["light.porch"]}, ["side effect gone"]),
    ({"feedback": ["Keep it short."]}, ["follows your feedback"]),
    ({"turns_before": 3, "turns_after": 1, "side_effects_gone": ["light.porch", "light.hall"],
      "feedback": ["Keep it short."]}, ["3 → 1 turns", "side effects gone", "follows your feedback"]),
    ({}, ["improved"])])
def test_each_test_says_what_it_showed_in_a_few_words_one_chip_each(test, result):
    """DETAILS P17.21.3: each result its own chip."""
    assert panel.test_result({**ROW, **test}) == result


@pytest.mark.asyncio
async def test_what_a_decision_did_shows_on_its_review_for_a_couple_of_minutes(home, monkeypatch):
    from homeassistant.util import dt as dt_util
    improvements = await reviewing(home, improvement(home), improvement(home, "imp2"))
    await improvements.decide("imp1", "decline", reason="not_an_issue", user_id="alex")
    await users_edit(home.hass, "script", "press_garage", {"alias": "Mine", "sequence": []})
    await improvements.decide("imp2", "accept", user_id="alex", editors=saves(home))  # A conflict: not ok.
    shown = (await ws(panel.ws_get, home.hass, improvement_id="imp1"))[1]
    assert shown["recent_outcome"] == "Thanks. Declined, and its conversation is archived."
    assert shown["recent_outcome_ok"] is True
    shown = (await ws(panel.ws_get, home.hass, improvement_id="imp2"))[1]
    assert shown["recent_outcome"].startswith("Nothing was changed: ") and shown["recent_outcome_ok"] is False
    later = dt_util.utcnow() + timedelta(minutes=3)
    monkeypatch.setattr(dt_util, "utcnow", lambda: later)
    shown = (await ws(panel.ws_get, home.hass, improvement_id="imp1"))[1]
    assert shown["recent_outcome"] is None and shown["recent_outcome_ok"] is False


@pytest.mark.asyncio
async def test_a_decided_review_offers_the_next_one_waiting_until_youre_caught_up(home):
    improvements = await reviewing(home, improvement(home, title="Fix 1"), improvement(home, "imp2", title="Fix 2"))
    assert (await ws(panel.ws_get, home.hass, improvement_id="imp1"))[1]["next_pending"] == {"id": "imp2",
                                                                                            "title": "Fix 2"}
    await improvements.decide("imp1", "decline", user_id="alex")
    assert (await ws(panel.ws_get, home.hass, improvement_id="imp1"))[1]["next_pending"] == {"id": "imp2",
                                                                                            "title": "Fix 2"}
    await improvements.decide("imp2", "revise", note="Both doors", user_id="alex")  # Still up for review (P0.6).
    assert (await ws(panel.ws_get, home.hass, improvement_id="imp2"))[1]["next_pending"] is None
    await improvements.decide("imp2", "decline", user_id="alex")
    assert (await ws(panel.ws_get, home.hass, improvement_id="imp2"))[1]["next_pending"] is None


@pytest.mark.asyncio
async def test_a_decided_improvement_shows_its_changes_as_proposed_in_plain_words(home):
    changes = [
        {"kind": "preference", "for": "this_user", "text": " Short answers. "},
        {"kind": "preference", "for": "everyone", "text": "Doors close at 11.", "replaces": ["Doors close at 10."]},
        {"kind": "registry", "entity_id": "light.front", "aliases": ["front light"]},
        {"kind": "registry", "entity_id": "light.front", "aliases": ["porch", "front light"]},
        {"kind": "registry", "entity_id": "light.porch", "expose": True},
        {"kind": "registry", "entity_id": "light.porch", "expose": False},
        {"kind": "script", "id": "press_garage", "config": {"alias": "Press garage", "sequence": [
            {"action": "cover.open_cover", "target": {"entity_id": "cover.garage"}}]}},
        {"kind": "script", "id": "close_garage", "config": {"alias": "Close garage", "sequence": [
            {"action": "cover.close_cover", "target": {"entity_id": "cover.garage"}}, {"stop": "Closed."}]}}]
    item = improvement(home, status="declined", changes=changes,
                       provider={**proposal(home)["provider"], "title": "Kitchen"})
    shown = panel.review(item)
    assert all(c["proposed"] and c["rows"] == [] for c in shown["changes"])
    assert [c["name"] for c in shown["changes"]] == [
        "Add to Kitchen's instructions (for you): “Short answers.”",
        "Add to Kitchen's instructions (for everyone): “Doors close at 11.”, replacing “Doors close at 10.”",
        "Give light.front the alias “front light”",
        "Give light.front the aliases “porch”, “front light”",
        "Expose light.porch to Assist",
        "Hide light.porch from Assist",
        "Script “Press garage”",
        "Script “Close garage”"]
    assert [c["kind"] for c in shown["changes"]] == ["instructions", "instructions", "aliases", "aliases", "exposure",
                                                     "exposure", "script", "script"]
    assert shown["choices"] == []


@pytest.mark.asyncio
async def test_an_assistant_change_that_also_recommends_shows_what_it_applies_and_what_it_only_names(home):
    """An assistant change may set its instructions or LLM APIs and recommend another option too: the review lists
    what it applies as a change, and the recommendation by the options it names, marked as not applied."""
    edit = {"kind": "assistant", "assistant": "Kitchen", "target": "e1/s1", "text": "Answer in one sentence.",
            "recommend": {"web_search": True}}
    narrow = {"kind": "assistant", "assistant": "Kitchen", "target": "e1/s1", "llm_hass_api": ["assist"],
              "recommend": {"chat_model": "a larger model"}}
    shown = panel.review(improvement(home, status="declined", changes=[edit, narrow]))
    assert [(c["name"], c.get("proposed"), c.get("recommended")) for c in shown["changes"]] == [
        ("Add to Kitchen's instructions: “Answer in one sentence.”", True, None),
        ("Kitchen keeps only: assist", True, None),
        ("For Kitchen, consider: web_search: true", None, True),
        ("For Kitchen, consider: chat_model: a larger model", None, True)]
    planned = [{"kind": "prompt", "target": "e1/s1", "before": "Be brief.", "after": "Be brief.\nAnswer in one "
                "sentence.", "what": "Kitchen's instructions"}]
    shown = panel.review(improvement(home, changes=[edit]), planned=(planned, [], []))
    assert [(c["name"], c.get("recommended")) for c in shown["changes"]] == [
        ("Kitchen's instructions", None), ("For Kitchen, consider: web_search: true", True)]


@pytest.mark.asyncio
async def test_the_list_puts_improvements_waiting_on_you_first_and_leaves_unwritten_ones_out(home):
    await reviewing(home, improvement(home, "old", title="Old", status="declined", created="2026-01-01"),
                    improvement(home, "new", title="New", created="2026-01-03"),
                    improvement(home, "draft", title="Draft", copy=None, created="2026-01-04"),
                    improvement(home, "on", title="On", status="applied", created="2026-01-02"))
    listed = (await ws(panel.ws_list, home.hass))[1]["improvements"]
    assert listed[0] == {"id": "new", "title": "New", "status": "awaiting_review", "pending": True, "enabled": 0,
                         "removed": 2, "spared": 0, "followed": 0, "revision": False}
    assert {i["id"] for i in listed} == {"new", "old", "on"} and not any(i["pending"] for i in listed[1:])


# A hypothesis's changes as a sandbox setup (changes.py).

SOURCE = recording_turns(make_recording())[0]["source"]
SCRIPTS = {"fixture_press": {"alias": "Fixture press", "sequence": [
    {"action": "switch.turn_off", "target": {"entity_id": TARGET}}]}}


@pytest.mark.parametrize("change, why", [
    ({"kind": "registry", "entity_id": "light.nowhere", "expose": True},
     "there's no entity 'light.nowhere' in the household's home"),
    ({"kind": "script", "config": {"sequence": []}}, "names no item"),
    ({"kind": "script", "id": "fixture_press"}, "gives no configuration"),
    ({"kind": "automation", "id": "nope", "on": False}, "there's no automation with id 'nope'"),
    ({"kind": "memory", "text": "x"}, "unknown change kind")],
    ids=["registry-unknown-device", "script-no-id", "script-no-config", "automation-unknown", "unknown-kind"])
def test_a_change_that_cant_be_placed_in_the_recorded_home_stops_with_a_reason(change, why):
    with pytest.raises(CaseError, match=f"^Change 1 \\({change['kind']}\\): .*{why}"):
        setup_from_changes([change], source=SOURCE, prompt="Be brief.", scripts=SCRIPTS)


def test_an_edited_script_is_its_whole_configuration_and_the_households_own_is_never_edited_in_place():
    scripts = deepcopy(SCRIPTS)
    edited = {"alias": "Fixture press", "description": "Presses the relay once.", "sequence": [
        {"action": "switch.turn_on", "target": {"entity_id": TARGET}, "data": {"duration": 1}}, {"stop": "Pressed."}]}
    setup = setup_from_changes([{"kind": "script", "id": "fixture_press", "config": edited}],
                               source=SOURCE, prompt="Be brief.", scripts=scripts)
    assert setup["scripts"] == {"fixture_press": edited}
    assert scripts == SCRIPTS  # The household's own definitions are never edited in place.
    # Exposure stays as it was, and the instructions don't change: scripts aren't listed in them.
    assert setup["exposure"] == {} and setup["prompt"] is None and not setup["prompt_changed"]


def test_what_a_change_shows_assist_decides_whether_the_instructions_change():
    script_only = setup_from_changes([{"kind": "registry", "entity_id": SCRIPT, "expose": True}], source=SOURCE, prompt="Be brief.",
                                     scripts=SCRIPTS)
    assert script_only["exposure"] == {SCRIPT: True} and script_only["prompt"] is None
    hidden = setup_from_changes([{"kind": "registry", "entity_id": TARGET, "expose": False}], source=SOURCE,
                                prompt="Be brief.", scripts=SCRIPTS)
    assert hidden["exposure"] == {TARGET: False} and hidden["prompt"] == "Be brief." and hidden["prompt_changed"]
    unexposed = setup_from_changes([{"kind": "script", "id": "press_it", "expose": False, "config": {
        "alias": "Press it", "sequence": [{"action": "switch.turn_on", "target": {"entity_id": TARGET}}]}}],
                                   source=SOURCE, prompt="Be brief.", scripts=SCRIPTS)
    # Saved but kept from Assist, and nothing Assist reads changes.
    assert "press_it" in unexposed["scripts"] and unexposed["exposure"] == {"script.press_it": False}
    assert unexposed["prompt"] is None
