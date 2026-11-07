"""Simulate (simulate.py): an idea tested by replaying the conversation with the household's own assistant in a
sandbox: without the change (the before: the whole conversation, up to three tries, until one goes as the
conversation went) and with it (the after, branching from the before, leaving out the turns DETAILS P6.4 lets it
skip); both judged by measures.py. The model transport is scripted; the effect guards fail on any HA service call,
network access or DNS lookup."""

from copy import deepcopy
from datetime import datetime, timedelta
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from conftest import assist_home
from recordings import (PROFILE, REPLAY_ENTITIES, SCRIPT, TARGET, at_home, before, bind_agent, make_recording,
                        prepared_recording, recorded_today, relay_renamed, replay)
from test_ai_roles import ANALYSIS
from test_milestone1 import fake_entry
from custom_components.assist_improver import measures, simulate
from custom_components.assist_improver.cases import CaseError, recording_turns, replay_readiness, started_from
from custom_components.assist_improver.simulation import moved_by_others, setting_off

PORCH = "switch.porch"
PRESS_SCRIPT = {"alias": "Fixture press", "sequence": [{"action": "switch.turn_off", "target": {"entity_id": TARGET}},
                                                       {"action": "switch.turn_on", "target": {"entity_id": TARGET}}]}
EXPOSE = {"title": "Let Assist press the relay", "changes": [{"kind": "registry", "entity_id": SCRIPT, "expose": True}]}
GOALS = measures.goals_of({**ANALYSIS, "turns": 4})


@pytest_asyncio.fixture
async def hass(hass):
    """The offline HA with HA's real Assist tools; replays run them on a simulated copy of the recording."""
    return await assist_home(hass, REPLAY_ENTITIES)


def recording():
    """The fixture relay's four turns, with the scripts and automations the recorder keeps, and a porch switch nobody
    touched."""
    record = prepared_recording()
    for turn in record["turns"]:
        source = turn["source"]
        source.update(scripts={"fixture_press": deepcopy(PRESS_SCRIPT)}, automations=[])
        source["states"][PORCH] = {"entity_id": PORCH, "state": "off", "attributes": {"friendly_name": "Porch switch"}}
        source["entities"][PORCH] = {"name": "Porch switch", "aliases": [None], "area_id": "fixture_area"}
        source["exposure"][PORCH] = True
    return record


def pressed(*turns):
    """With the change: the press script, in one go, for each turn said."""
    return [block for turn in turns for block in (
        [{"type": "tool_use", "id": f"press-{turn}", "name": "script__fixture_press", "input": {}}],
        [{"type": "text", "text": "Pressed."}])]


async def setup(hass, monkeypatch, record, today=None, home=None, services=None):
    """The household's assistant as it's set up now (with the settings it ran with, or today: its options), and its
    home today as the recording found it, with the parts of it given in home in place of those, and the actions given
    (recordings.at_home)."""
    agent, client = await bind_agent(hass, record, today)
    at_home(monkeypatch, record, services, **(home or {}))
    runtime = SimpleNamespace(recorder=SimpleNamespace(export=AsyncMock(return_value=record)),
                              replays=simulate.Replays(hass))
    return agent, client, runtime


async def run(hass, runtime, goals=GOALS, hypothesis=EXPOSE, rid="synthetic-recording"):
    return await simulate.test(hass, fake_entry({"ai_task_entity_id": "ai_task.fixture"}), runtime, rid, goals,
                               hypothesis)


@pytest.mark.asyncio
async def test_both_replays_start_at_the_first_turn_on_the_assistant_as_it_is_set_up_now_and_touch_nothing_live(
        hass, monkeypatch, effect_guards):
    """A replay is the conversation said again to its assistant as the household has it now (DETAILS P6.2.2): its
    instructions and model today, not those it ran with."""
    record = recording()
    now = {**PROFILE, "prompt": "Today's instructions.", "chat_model": "todays-model", "max_tokens": 2000}
    agent, client, runtime = await setup(hass, monkeypatch, record, today=now)
    original, live = deepcopy(record), deepcopy(dict(agent.entry.subentries["fixture-anthropic"].data))
    hass.states.async_set(TARGET, "LIVE_VALUE")
    client.blocks = before(record) + pressed(1, 4)
    test = await run(hass, runtime)
    assert not client.blocks
    for request in client.requests:
        assert request["model"] == "todays-model" and request["max_tokens"] == 2000
        assert "Today's instructions." in request["system"][0]["text"]
    assert "Recorded template" not in json.dumps(client.requests) and "LIVE_VALUE" not in json.dumps(client.requests)
    # The before carries its own native history through every turn, never the recorded replies.
    last_before = client.requests[6]["messages"]
    blocks = [b for m in last_before if isinstance(m["content"], list) for b in m["content"]]
    assert any(b.get("signature") == "synthetic-signature-1" for b in blocks)
    assert "Recorded response" not in json.dumps(last_before)
    assert dict(agent.entry.subentries["fixture-anthropic"].data) == live
    assert hass.states.get(TARGET).state == "LIVE_VALUE"
    assert effect_guards == [] and record == original
    assert test["validated"] and test["gains"] == ["goals met", "turns per root goal"]  # Turn 2's finish too.
    assert test["before"]["said"] == [1, 2, 3, 4] and test["after"]["said"] == [1, 4]
    assert test["before"]["turns_per_root_goal"] == {"1:0": 3, "4:3": 1}
    assert test["after"]["turns_per_root_goal"] == {"1:0": 1, "4:3": 1}
    assert test["before"]["status"] == "reproduced" and test["after"]["status"] == "done"
    assert [r["assistant"] for r in test["after"]["replies"]] == ["Pressed.", "Pressed."]
    assert test["judged"]["2:1"] == {"summary": GOALS[1]["summary"], "turn": 2, "root": "1:0", "devices": [],
                                     "kind": "step"}
    assert test["tested"] == {"exposure": {SCRIPT: False}}  # Only what the change sets: the script stays as it is.
    assert test["provider"] == {"config_entry_id": "fixture-entry", "subentry_id": "fixture-anthropic",
                                "title": "Fixture assistant"}  # As it's set up now.
    assert test["recorded"][0]["you"] == "Toggle the fixture relay." and test["user_id"] == "fixture-user"


@pytest.mark.asyncio
async def test_each_replay_records_the_config_and_the_home_it_started_from(hass, monkeypatch, effect_guards):
    record = recording()
    _, client, runtime = await setup(hass, monkeypatch, record)
    client.blocks = before(record) + pressed(1, 4)
    test = await run(hass, runtime)
    source = record["turns"][0]["source"]
    assert test["before"]["started_from"] == test["after"]["started_from"] == started_from(source)
    assert test["before"]["config"] == {"scripts": {"fixture_press": PRESS_SCRIPT}, "exposed": sorted([PORCH, TARGET])}
    assert test["after"]["config"] == {"scripts": {"fixture_press": PRESS_SCRIPT}, "exposed": sorted([SCRIPT, PORCH, TARGET])}


@pytest.mark.asyncio
async def test_skipped_turns_leave_the_history_an_ordered_subset_of_the_users_words(hass, monkeypatch, effect_guards):
    record = recording()
    _, client, runtime = await setup(hass, monkeypatch, record)
    client.blocks = before(record) + pressed(1, 4)
    test = await run(hass, runtime)
    said = [m["content"] for m in client.requests[-1]["messages"]
            if m["role"] == "user" and isinstance(m["content"], str)]
    assert said == ["Toggle the fixture relay.", "Again, off then on."]
    # The trouble starts at turn 1: the test branches there, from the first turn, as a fresh conversation.
    assert runtime.replays.items[test["after"]["plan"]]["plan"]["branch"] == 1
    assert client.requests[7]["messages"] == [{"role": "user", "content": "Toggle the fixture relay."}]


def tries(runtime):
    """The baseline's tries kept, in order."""
    return sorted((r for r in runtime.replays.items.values() if r["plan"]["kind"] == "before"),
                  key=lambda r: r["plan"]["try"])


@pytest.mark.asyncio
async def test_a_baseline_whose_three_tries_stop_at_the_first_turn_unlike_the_recording_tests_nothing(
        hass, monkeypatch, effect_guards):
    """Each try is a whole replay from the first turn, stopped at the first turn that didn't go as recorded; none of
    three did, so the conversation is not reproducible, all three kept, the reason saying how many ran (DETAILS
    P6.6.5, P6.6.6)."""
    record = recording()
    _, client, runtime = await setup(hass, monkeypatch, record)
    # Turn 1 asks before it acts, every time: the relay the recorded Assist switched off stays on.
    client.blocks = [[{"type": "text", "text": "Which relay?"}]] * 3
    with pytest.raises(simulate.Untestable) as raised:
        await run(hass, runtime)
    assert str(raised.value) == "Tried 3 times without the change: turn 1 didn't do what it did in the conversation."
    assert raised.value.conversation and len(client.requests) == 3
    kept = tries(runtime)
    assert [r["plan"]["try"] for r in kept] == [1, 2, 3] and len(runtime.replays.items) == 3
    for one in kept:
        assert one["status"] == "not_reproducible" and [s["turn"] for s in one["steps"]] == [1]
        assert one["steps"][0]["matched"] is False
    assert effect_guards == []


@pytest.mark.asyncio
async def test_a_baseline_whose_first_try_doesnt_go_as_the_conversation_went_uses_the_second_and_runs_no_third(
        hass, monkeypatch, effect_guards):
    """The first try that goes as the conversation went is the baseline (Kai, 2026-11-05), and a settled test is
    never run again."""
    record = recording()
    _, client, runtime = await setup(hass, monkeypatch, record)
    client.blocks = [[{"type": "text", "text": "Which relay?"}]] + before(record) + pressed(1, 4)
    test = await run(hass, runtime)
    assert not client.blocks and test["validated"]
    first, second = tries(runtime)
    assert (first["status"], second["status"]) == ("not_reproducible", "reproduced")
    assert test["before"]["plan"] == second["plan"]["id"]
    assert runtime.replays.items[test["after"]["plan"]]["plan"]["before"] == second["plan"]["id"]
    asked, kept = len(client.requests), deepcopy(runtime.replays.items)
    again = await run(hass, runtime)
    assert len(client.requests) == asked and runtime.replays.items == kept
    assert {**again, "at": None} == {**test, "at": None}


@pytest.mark.asyncio
async def test_a_try_of_the_baseline_the_machinery_cut_off_isnt_counted(hass, monkeypatch, effect_guards):
    record = recording()
    _, client, runtime = await setup(hass, monkeypatch, record)
    client.blocks = [[{"type": "text", "text": "Which relay?"}]]  # Then the transport fails: try 2 is cut off.
    with pytest.raises(simulate.Transient):
        await run(hass, runtime)
    assert [r["status"] for r in tries(runtime)] == ["not_reproducible", "infrastructure_failure"]
    client.blocks = before(record) + pressed(1, 4)
    test = await run(hass, runtime)
    assert not client.blocks and test["validated"]
    assert [(r["plan"]["try"], r["status"]) for r in tries(runtime)] == [(1, "not_reproducible"), (2, "reproduced")]


# A turn its model never finished: a web search at the provider, and no reply after it.
UNFINISHED = [{"type": "server_tool_use", "id": "web", "name": "web_search", "input": {"query": "relay"}}]
TELL_PHONE = [[{"type": "tool_use", "id": "tell", "name": "intent__HassTurnOn", "input": {"name": "Tell phone"}}],
              [{"type": "text", "text": "Told."}]]
PHONE = {"notify": {"mobile_app_phone": "none"}}  # Today's action that tells the phone, which answers nothing.


@pytest.mark.asyncio
async def test_a_baseline_try_its_model_took_into_what_a_replay_cant_follow_is_followed_by_another(
        hass, monkeypatch, effect_guards):
    """Kai, 2026-11-05: "retry 3 times, and if the problem is reproducible at least once, we use that as the baseline
    for the fix". The first try's model never finished turn 1 (a web search, and no reply), which stops a replay as
    unsupported; another run of the model can take another path, so it's a try that didn't reproduce, and the second,
    which goes as the conversation went, is the baseline (P6.6.5)."""
    record = recording()
    _, client, runtime = await setup(hass, monkeypatch, record)
    client.blocks = [UNFINISHED] + before(record) + pressed(1, 4)
    test = await run(hass, runtime)
    assert not client.blocks and test["validated"]
    first, second = tries(runtime)
    assert (first["status"], second["status"]) == ("unsupported", "reproduced")
    assert first["error"]["turn"] == 1 and "didn't finish" in first["error"]["message"]
    assert test["before"]["plan"] == second["plan"]["id"] and effect_guards == []


@pytest.mark.asyncio
async def test_a_baseline_whose_three_tries_dont_go_as_it_went_is_not_reproducible_each_reason_said_once(
        hass, monkeypatch, effect_guards):
    """No try goes as the conversation went at turn 1: its model never finishes the turn, then runs the script that
    tells a phone instead (the phone's action stood in and logged, P6.2.13), then never finishes it again. None
    reproduced: the conversation is not reproducible, all three kept, and the reason says each try's once (P6.6.5)."""
    record = telling_the_phone(recording(), exposed=True)
    _, client, runtime = await setup(hass, monkeypatch, record, services=PHONE)
    client.blocks = [UNFINISHED, *TELL_PHONE, UNFINISHED]
    with pytest.raises(simulate.Untestable) as raised:
        await run(hass, runtime)
    kept = tries(runtime)
    assert [(r["plan"]["try"], r["status"]) for r in kept] == [
        (1, "unsupported"), (2, "not_reproducible"), (3, "unsupported")]
    unfinished = kept[0]["error"]["message"]
    assert unfinished == kept[2]["error"]["message"] and "didn't finish" in unfinished
    assert kept[1]["steps"][0]["outside"] == [{"action": "notify.mobile_app_phone", "data": {"message": "Porch on"},
                                               "answer": "not simulated"}]
    assert str(raised.value) == (f"Tried 3 times without the change: {unfinished}; turn 1 didn't do what it did in "
                                 "the conversation.")
    assert raised.value.conversation and not client.blocks and effect_guards == []


@pytest.mark.parametrize("why", ["seed", "identity"])
@pytest.mark.asyncio
async def test_a_baseline_whose_replay_cant_start_stops_at_once_with_no_second_try(hass, monkeypatch, effect_guards,
                                                                                    why):
    """A replay that can't start (its sandbox can't be set up as the home, or a turn it would say lacks who said it)
    would fail every try alike: the first stops the baseline, before any model call (P6.6.5)."""
    from custom_components.assist_improver.worker import RemoteSandbox
    record = recording()
    _, client, runtime = await setup(hass, monkeypatch, record)
    if why == "seed":
        real = RemoteSandbox.call

        async def seed_fails(self, op, **args):
            if op == "seed":
                raise CaseError("a device can't be set up")
            return await real(self, op, **args)
        monkeypatch.setattr(RemoteSandbox, "call", seed_fails)
    else:
        del record["turns"][1]["prepared"]["context"]["context"]["parent_id"]  # Turn 2's, not the first's.
    with pytest.raises(simulate.Untestable) as raised:
        await run(hass, runtime)
    assert str(raised.value) == {
        "seed": "The sandbox could not be set up as the household's home: a device can't be set up",
        "identity": "Cannot backfill missing historical identity"}[why]
    [only] = tries(runtime)
    assert only["status"] == "unsupported" and "turn" not in only["error"]
    assert raised.value.conversation and client.requests == [] and effect_guards == []


# Each turn its own root goal on the relay: off (as turn 1 left it), still off, off again (turn 3 switched it back on
# instead), and pressed. The trouble starts at turn 3.
LATE = measures.goals_of({**ANALYSIS, "turns": 4, "goals": [
    {"turn": 1, "summary": "Relay off", "kind": "state", "entity_id": TARGET, "state": "off"},
    {"turn": 2, "summary": "Relay still off", "kind": "state", "entity_id": TARGET, "state": "off"},
    {"turn": 3, "summary": "Relay left off", "kind": "state", "entity_id": TARGET, "state": "off"},
    {"turn": 4, **ANALYSIS["goals"][3]}]})


@pytest.mark.asyncio
async def test_a_test_whose_trouble_starts_at_a_later_turn_branches_from_the_baselines_home_and_history_there(
        hass, monkeypatch, effect_guards):
    """A test branches from its baseline at the first turn of the earliest root goal with trouble (Kai,
    2026-11-05): the turns before are the baseline's, never said again; its model reads them as the conversation so
    far, and the home is as the baseline left it, so switching the relay off at turn 3 finds it off already (as the
    conversation found it, it was on)."""
    record = recording()
    _, client, runtime = await setup(hass, monkeypatch, record)
    [off] = recording_turns(record)[0]["calls"]
    client.blocks = before(record) + [[{"type": "tool_use", "id": "off-3", "name": off["tool_name"],
                                        "input": off["tool_args"]}], [{"type": "text", "text": "Off."}]] + pressed(4)
    test = await run(hass, runtime, LATE)
    assert not client.blocks and test["validated"] and effect_guards == []
    without, with_change = (runtime.replays.items[test[k]["plan"]] for k in ("before", "after"))
    assert with_change["plan"]["branch"] == 3 and with_change["said"] == [1, 2, 3, 4]
    assert with_change["steps"][:2] == without["steps"][:2]
    assert with_change["steps"][2]["commands"] and with_change["steps"][2]["transitions"] == []
    assert test["before"]["goals"]["3:2"] == "unmet" and test["after"]["goals"]["3:2"] == "met"
    history = client.requests[7]["messages"]  # The first with the change: turn 3's.
    said = [m["content"] for m in history if m["role"] == "user" and isinstance(m["content"], str)]
    assert said == ["Toggle the fixture relay.", "Use the press script.", "Turn it back on."]
    blocks = [b for m in history if isinstance(m["content"], list) for b in m["content"]]
    assert {b["type"] for b in blocks} >= {"tool_use", "tool_result"}
    assert "Generated response 1." in json.dumps(history) and "Generated response 2." in json.dumps(history)


@pytest.mark.asyncio
async def test_the_before_is_judged_on_the_devices_the_recorded_assist_changed_and_must_go_as_it_went(
        hass, monkeypatch, effect_guards):
    record = recording()
    _, client, runtime = await setup(hass, monkeypatch, record)
    blocks = before(record)
    # In turn 2 (where the recorded Assist did nothing) it also switches the porch on: a difference elsewhere, so the
    # replay goes on, but the conversation never left that side effect.
    blocks[2] = [{"type": "tool_use", "id": "porch", "name": "intent__HassTurnOn",
                  "input": {"name": "Porch switch", "domain": ["switch"]}}]
    blocks.insert(3, [{"type": "text", "text": "Generated response 2."}])
    client.blocks = blocks * 3  # Each of the baseline's tries.
    with pytest.raises(simulate.Untestable, match="side effects") as raised:
        await run(hass, runtime)
    assert raised.value.conversation and not client.blocks
    for kept in tries(runtime):
        assert kept["status"] == "reproduced" and all(s["matched"] for s in kept["steps"])
        assert kept["steps"][1]["differences_elsewhere"] == {PORCH: {"replay": [["off", "on"]], "recorded": []}}


@pytest.mark.asyncio
async def test_a_before_whose_device_goal_didnt_go_as_it_went_is_untestable_before_any_check_models_call(
        hass, monkeypatch, effect_guards):
    """Code checks the before first: the porch the user asked for in turn 2 was never switched in the conversation,
    the replay switches it, so the conversation can't be tested, and what the household objected to is never sent to
    the judge for it."""
    record = recording()
    _, client, runtime = await setup(hass, monkeypatch, record)
    blocks = before(record)
    blocks[2] = [{"type": "tool_use", "id": "porch", "name": "intent__HassTurnOn",
                  "input": {"name": "Porch switch", "domain": ["switch"]}}]
    blocks.insert(3, [{"type": "text", "text": "Generated response 2."}])
    client.blocks = blocks * 3  # Each of the baseline's tries.
    analysis = {**ANALYSIS, "turns": 4, "goals": [
        *ANALYSIS["goals"], {"turn": 2, "summary": "Porch on", "kind": "state", "entity_id": PORCH, "state": "on",
                             "serves": 1},
        {"turn": 5, "summary": "The note", "kind": "conduct", "about": "all", "quote": "Say less", "serves": 1}]}

    async def check(*_):
        raise AssertionError("the judge was called")
    monkeypatch.setattr(simulate, "check_conduct", check)
    with pytest.raises(simulate.Untestable, match="goals 2:4 didn't end") as raised:
        await run(hass, runtime, measures.goals_of(analysis))
    assert raised.value.conversation and not client.blocks and effect_guards == []


NOTED = {**ANALYSIS, "turns": 4, "goals": [*ANALYSIS["goals"], {"turn": 5, "summary": "The note", "kind": "conduct",
                                                                   "about": "all", "quote": "Say less", "serves": 1}]}


def judging(*shows):
    """A stand-in judge answering in turn: each answer whether every turn it's asked about shows the objection (None:
    it couldn't answer); what it was asked: (quotes, {turn: reply}) per call."""
    asked, answers = [], list(shows)

    async def check(hass, entity_id, objections, steps, turns):
        asked.append(([o["quote"] for o in objections], [s["reply"] for s in steps]))
        shown = answers.pop(0)
        if shown is None:
            return {"status": "failed", "answers": [], "usage": {}}
        return {"status": "completed", "usage": {}, "answers": [
            {"goal": o["goal"], "turn": n, "shows": shown, "why": "Wordy." if shown else "Short."}
            for o in objections for n in o["judge"]]}
    return asked, answers, check


@pytest.mark.asyncio
async def test_an_objection_no_turn_of_the_before_shows_is_set_aside_and_the_test_goes_on(
        hass, monkeypatch, effect_guards):
    """Its devices went as the conversation went, so the judge is asked about the before's turns, once; none shows
    what the note objects to, so that trouble didn't happen again without the change: the note's goal is set aside
    (DETAILS P6.6.2), counted neither way, and the change is tested on the rest."""
    record = recording()
    _, client, runtime = await setup(hass, monkeypatch, record)
    client.blocks = before(record) + pressed(1, 4)
    asked, _, check = judging(False)
    monkeypatch.setattr(simulate, "check_conduct", check)
    test = await run(hass, runtime, measures.goals_of(NOTED))
    assert not client.blocks and effect_guards == [] and [quotes for quotes, _ in asked] == [["Say less"]]
    assert test["before"]["goals"]["5:4"] == test["after"]["goals"]["5:4"] == "indeterminate"
    assert test["after"]["conduct"]["5:4"] == {"state": "indeterminate", "answers": [], "set_aside": True}
    assert test["validated"] and test["gains"] == ["goals met", "turns per root goal"]  # The steps, not the note.


@pytest.mark.asyncio
async def test_a_wrap_up_the_before_finds_met_is_set_aside_counted_neither_way_and_still_said_with_the_change(
        hass, monkeypatch, effect_guards):
    """The household filed the relay in turn 4, though by then its press was done and nothing of it was open: without
    the change the filing is met, so the trouble it files didn't happen again and it can't be tested (DETAILS P6.4).
    Set aside, it's indeterminate in both replays and its turn is said with the change, so it's no turn saved."""
    record = recording()
    _, client, runtime = await setup(hass, monkeypatch, record)
    client.blocks = before(record) + pressed(1, 4)
    filed = measures.goals_of({**ANALYSIS, "turns": 4, "goals": [
        *ANALYSIS["goals"][:3], {"turn": 4, "summary": "Filed it", "kind": "wrap-up", "serves": 1}]})
    test = await run(hass, runtime, filed)
    assert not client.blocks and effect_guards == []
    assert test["before"]["goals"]["4:3"] == test["after"]["goals"]["4:3"] == "indeterminate"
    assert test["after"]["said"] == [1, 4] and test["after"]["turns_per_root_goal"] == {"1:0": 2}
    [after] = [r for r in runtime.replays.items.values() if r["plan"]["kind"] == "after"]
    assert after["plan"]["aside"] == ["4:3"]


@pytest.mark.asyncio
async def test_a_device_unreachable_in_the_conversation_stays_so_in_the_replay_and_the_conversation_can_be_tested(
        hass, monkeypatch, effect_guards):
    """A device can be unreachable: Home Assistant leaves it out of Assist's command, and it stays unavailable while
    Assist says it's done. The replay without the change goes as that conversation went (the device stays unavailable,
    as Home Assistant leaves it), so a change to how Assist talks can still be tested on it."""
    record = recording()
    for turn in record["turns"]:
        turn["source"]["states"][TARGET]["state"] = "unavailable"
    record["state_changes"] = []  # Every command Assist sent left the relay as it was.
    _, client, runtime = await setup(hass, monkeypatch, record)
    client.blocks = before(record) + pressed(1, 2, 3, 4)
    test = await run(hass, runtime)
    assert not client.blocks and effect_guards == []
    assert test["before"]["status"] == "reproduced" and test["after"]["status"] == "done"
    assert set(test["before"]["goals"].values()) == {"unmet"}  # As in the conversation: the relay never answered.
    assert not test["validated"]  # Pressing an unreachable relay meets no goal either.


@pytest.mark.asyncio
async def test_what_others_did_between_turns_reaches_the_replay_also_on_a_device_assist_changes_later(
        hass, monkeypatch, effect_guards):
    """The relay was unreachable when turn 1 switched it off, so nothing changed; it was back, off, by turn 2, and
    turns 3 and 4 switched it as recorded. Others' moves between the recorded turns reach the replay whatever Assist
    does to the device in other turns, so the replay without the change goes as the conversation went."""
    record = recording()
    record["turns"][0]["source"]["states"][TARGET]["state"] = "unavailable"
    record["state_changes"] = record["state_changes"][1:]  # Turn 1's command never reached the relay.
    _, client, runtime = await setup(hass, monkeypatch, record)
    client.blocks = before(record) + pressed(1, 2, 3, 4)
    test = await run(hass, runtime)
    assert not client.blocks and effect_guards == []
    assert test["before"]["status"] == "reproduced" and test["after"]["status"] == "done"


async def replayed_as_it_went(hass, monkeypatch, record):
    """The replay without the change, of every turn, as Simulate checks it against the recording."""
    from test_openrouter_replay import scripted
    _, client, runtime = await setup(hass, monkeypatch, record)
    open_router = record["turns"][0]["prepared"]["context"].get("platform") == "open_router"
    client.blocks = scripted(record) if open_router else before(record)
    return await simulate.replay_recording(hass, runtime.recorder, record["id"], last_turn=len(record["turns"]))


def filed_at_turn_2(record):
    """The fixture conversation with its second turn filed from Assist, the improver's filing tool among Assist's."""
    from custom_components.assist_improver.llm import FileImprovement
    tool = {"name": FileImprovement.name, "description": FileImprovement.description, "integration": "assist_improver",
            "parameters": {"type": "object", "properties": {"what_to_improve": {"type": "string"}}}}
    for turn in record["turns"]:
        turn["prepared"]["tools"].append(deepcopy(tool))
    content = record["chat_log"]["content"]
    asked = next(n for n, m in enumerate(content) if m.get("content") == "Use the press script.")
    call = {"tool_name": FileImprovement.name, "tool_args": {"what_to_improve": "It should press it"},
            "id": "call-2-file", "external": False}
    content[asked + 1:asked + 1] = [
        {"role": "assistant", "agent_id": "conversation.fixture", "tool_calls": [call], "created": content[asked]["created"]},
        {"role": "tool_result", "agent_id": "conversation.fixture", "tool_call_id": call["id"],
         "tool_name": call["tool_name"], "result": {"filed": True}, "created": content[asked]["created"]}]
    return record


@pytest.mark.asyncio
async def test_a_conversation_filed_from_assist_replays_with_its_filing(hass, monkeypatch, effect_guards):
    """Conversations filed from Assist (2026-10-28) had unsupported replays: a replay had no filing
    tool. It has the one the recorded Assist had, which answers as filing does and files nothing, so the replay goes
    as the conversation went, its filing among its steps (DETAILS P6.6.2)."""
    from custom_components.assist_improver.llm import FileImprovement
    found = await replayed_as_it_went(hass, monkeypatch, filed_at_turn_2(recording()))
    assert found["status"] == "reproduced", found.get("error")
    [filed] = [b for m in found["steps"][1]["output"] for b in m.get("tool_calls") or []]
    [answered] = [m["tool_result"] for m in found["steps"][1]["output"] if m.get("tool_call_id") == filed["id"]]
    assert filed["tool_name"] == FileImprovement.name and answered["filed"] is True and effect_guards == []


@pytest.mark.asyncio
async def test_what_a_device_reports_just_after_the_reply_is_still_the_turns(hass, monkeypatch, effect_guards):
    """A relay can report a press's last step just after Assist's reply, and Home Assistant puts it down to the
    request, as it does what a device reports within seconds of a command. The replay's relay, which reports at once,
    matches that turn."""
    record = recording()
    reply = record["chat_log"]["content"][-1]["created"]
    record["state_changes"][-1]["at"] = (datetime.fromisoformat(reply) + timedelta(milliseconds=60)).isoformat()
    found = await replayed_as_it_went(hass, monkeypatch, record)
    assert found["status"] == "reproduced" and effect_guards == []
    assert found["steps"][3]["recorded_transitions"] == [{"entity_id": TARGET, "old": "on", "new": "off"},
                                                         {"entity_id": TARGET, "old": "off", "new": "on"}]


@pytest.mark.asyncio
async def test_states_a_device_reports_on_its_own_on_the_way_dont_stop_the_replay_matching(
        hass, monkeypatch, effect_guards):
    """A device can show another state for a moment on its way, all under Assist's request. The replay's device goes
    straight there, and matches: what it set the device to comes in the recorded order and it ends the turn where it
    did."""
    record = recording()
    on = record["state_changes"][1]  # Turn 3: back on.
    bounce = [{**deepcopy(on), "old": deepcopy(on["new"]), "new": {**deepcopy(on["new"]), "state": "off"}},
              {**deepcopy(on), "old": {**deepcopy(on["new"]), "state": "off"}}]
    record["state_changes"][2:2] = bounce
    found = await replayed_as_it_went(hass, monkeypatch, record)
    assert found["status"] == "reproduced" and effect_guards == []
    assert len(found["steps"][2]["recorded_transitions"]) == 3 and len(found["steps"][2]["transitions"]) == 1
    # Not the other way round: a turn that left the device elsewhere doesn't match.
    record["state_changes"][3] = bounce[0]
    found = await replayed_as_it_went(hass, monkeypatch, record)
    assert found["status"] == "not_reproducible" and [s["turn"] for s in found["steps"]] == [1, 2, 3]


def pressing_at_turn_2(made):
    """The fixture conversation with Assist pressing the press script (exposed) in turn 2, which left the relay on, and
    what the press changed then: made(change), change(entity_id, old, new, seconds after the turn, by turn 2's request
    unless given)."""
    record = recording()
    for turn in record["turns"]:
        turn["source"]["exposure"][SCRIPT] = True
    for turn in record["turns"][2:]:
        turn["source"]["states"][TARGET]["state"] = "on"  # Turn 2's press left the relay on.
    messages, context = record["chat_log"]["content"], record["turns"][1]["prepared"]["context"]["context"]
    reply = next(i for i, m in enumerate(messages) if m.get("content") == "Recorded response 2.")
    at = datetime.fromisoformat(record["turns"][1]["source"]["at"])  # The reply comes a second later.
    def tick(seconds):
        return (at + timedelta(seconds=seconds)).isoformat()
    call = {"tool_name": "script__fixture_press", "tool_args": {}, "id": "press-2", "external": False}
    messages[reply:reply] = [
        {"role": "assistant", "agent_id": "conversation.fixture", "tool_calls": [call], "created": tick(0.2)},
        {"role": "tool_result", "agent_id": "conversation.fixture", "tool_call_id": "press-2",
         "tool_name": call["tool_name"], "result": {"result": {}}, "created": tick(0.6)}]
    def change(entity_id, old, new, seconds, by=context):
        return {"entity_id": entity_id, "at": tick(seconds), "old": {"entity_id": entity_id, "state": old,
                "attributes": {}}, "new": {"entity_id": entity_id, "state": new, "attributes": {}, "context": by}}
    del record["state_changes"][1]  # Turn 3 finds the relay on: nothing changes.
    record["state_changes"][1:1] = made(change)
    return record


@pytest.mark.asyncio
async def test_a_script_is_compared_by_its_start_not_by_when_it_got_done(hass, monkeypatch, effect_guards):
    """A script can still be running at Assist's reply, waiting on a slow device, and get done seconds later, past
    what Home Assistant puts down to the request. The replay's script gets done at once. Like a door that reports its
    movement, a script is compared by its setting off, not its getting there."""
    record = pressing_at_turn_2(lambda change: [
        change(SCRIPT, "off", "on", 0.3), change(TARGET, "off", "on", 0.4),
        change(SCRIPT, "on", "off", 17, {"id": "later", "parent_id": None, "user_id": None})])
    found = await replayed_as_it_went(hass, monkeypatch, record)
    assert found["status"] == "reproduced" and effect_guards == []
    assert [(t["entity_id"], t["new"]) for t in found["steps"][1]["transitions"]] == [
        (SCRIPT, "on"), (TARGET, "on"), (SCRIPT, "off")]
    # A script Assist stopped, running since an earlier turn, is still compared.
    stopped = [{"entity_id": SCRIPT, "old": "on", "new": "off"}]
    assert setting_off(stopped) == stopped


def test_only_others_moves_carry_over_between_turns_not_assists_own_once_home_assistant_stopped_naming_its_request():
    """Between turns only others' moves carry over (DETAILS P6.2.8). Assist's own include a door it set closing
    getting there after Home Assistant stopped putting it down to the request, also once the next turn has begun, and
    a step of a script it started that acts a turn later, under that turn's request. What someone else did still
    carries over."""
    record = recording()
    door, gate, hall = "cover.garage", "cover.gate", "light.hall"
    for number, turn in enumerate(record["turns"], 1):
        states = turn["source"]["states"]
        states[door] = {"entity_id": door, "state": "open" if number == 1 else "closed", "attributes": {}}
        states[gate] = {"entity_id": gate, "state": {3: "closing", 4: "closed"}.get(number, "open"), "attributes": {}}
        states[hall] = {"entity_id": hall, "state": "off" if number == 1 else "on", "attributes": {}}
        states[PORCH]["state"] = "on" if number < 3 else "off"
    turns = recording_turns(record)
    def at(turn, seconds):
        return (datetime.fromisoformat(turns[turn - 1]["source"]["at"]) + timedelta(seconds=seconds)).isoformat()
    def change(entity_id, old, new, when, context):
        attributes = turns[0]["source"]["states"][entity_id]["attributes"]
        return {"entity_id": entity_id, "at": when, "old": {"entity_id": entity_id, "state": old, "attributes": attributes},
                "new": {"entity_id": entity_id, "state": new, "attributes": attributes, "context": context}}
    request = record["turns"][0]["prepared"]["context"]["context"]
    record["state_changes"] += [
        change(door, "open", "closing", at(1, 0.2), request),
        change(door, "closing", "closed", turns[0]["ended"], {"id": "later", "parent_id": None, "user_id": None}),
        change(hall, "off", "on", at(1, 0.4), {"id": "alex", "parent_id": None, "user_id": "fixture-user"}),
        change(PORCH, "on", "off", at(2, 0.5), request),
        # The gate Assist set closing in turn 2 reports on its way and gets there only once turn 3 has begun.
        change(gate, "open", "closing", at(2, 0.2), record["turns"][1]["prepared"]["context"]["context"]),
        change(gate, "closing", "closing", at(3, 0.1), {"id": "later", "parent_id": None, "user_id": None}),
        change(gate, "closing", "closed", at(3, 0.3), {"id": "later", "parent_id": None, "user_id": None})]
    turns = recording_turns(record)
    assert moved_by_others(record, turns, turns[1]) == {hall: {"state": "on", "attributes": {}, "removed": []}}
    assert moved_by_others(record, turns, turns[2]) == moved_by_others(record, turns, turns[3]) == {}


KITCHEN = "light.kitchen"


def kitchen(brightness=None, **lit):
    """"Turn on the kitchen light." through the household's OpenRouter assistant: off before; the real light came on
    at `brightness` (the level it remembered, or the one Assist named), or showing lit (a colour)."""
    from test_openrouter_replay import openrouter_recording
    lit = lit or {"color_mode": "brightness", "brightness": brightness}
    modes = ["hs"] if "hs_color" in lit else ["brightness"]
    record = json.loads(json.dumps(openrouter_recording()).replace(TARGET, KITCHEN).replace(
        "Fixture relay", "Kitchen light").replace('"domain": ["switch"]', '"domain": ["light"]').replace(
        "Toggle the fixture relay.", "Turn on the kitchen light."))
    for n, turn in enumerate(record["turns"]):
        turn["source"].update(scripts={}, automations=[])
        turn["source"]["states"][KITCHEN].update(state="on" if n else "off", attributes={
            "friendly_name": "Kitchen light", "supported_color_modes": modes, **(lit if n else {})})
        turn["prepared"]["tools"].append({"name": "light__HassLightSet", "description": "Set a light",
                                          "integration": "light", "parameters": {"type": "object"}})
    first = recording_turns(record)[0]
    was = deepcopy(first["source"]["states"][KITCHEN])
    record["state_changes"] = [{"entity_id": KITCHEN, "at": "2026-01-01T00:00:04+00:00", "old": was, "new": {
        **was, "state": "on", "attributes": {**was["attributes"], **lit},
        "context": deepcopy(first["prepared"]["context"]["context"])}}]
    return record


@pytest.mark.parametrize("tool, args, brightness, status", [
    ("intent__HassTurnOn", {"domain": ["light"]}, 200, "reproduced"),  # On at the level only the real light knew.
    ("light__HassLightSet", {"brightness": 50}, 128, "reproduced"),
    ("light__HassLightSet", {"brightness": 30}, 128, "not_reproducible")])
@pytest.mark.asyncio
async def test_the_before_compares_a_lights_brightness_where_the_replay_knows_it(hass, monkeypatch, effect_guards, tool,
                                                                               args, brightness, status):
    from test_openrouter_replay import bind_openrouter, call, reply
    record = kitchen(brightness)
    await bind_openrouter(hass, record, [call(tool, {"name": "Kitchen light", **args}, "c1"), reply("Done.")]
                          + [reply("Ok.")] * 3)
    found = await (await replay(hass, monkeypatch, record)).run()
    assert found["status"] == status, found["steps"]
    assert found["steps"][0]["transitions"] == [{"entity_id": KITCHEN, "old": "off", "new": "on"}]
    assert effect_guards == []


@pytest.mark.parametrize("colour, status", [("red", "reproduced"), ("blue", "not_reproducible")])
@pytest.mark.asyncio
async def test_the_before_compares_every_setting_the_recorded_request_left_in_any_form(hass, monkeypatch, effect_guards,
                                                                                     colour, status):
    """DETAILS P6.6.1: a device's effects are its settings too, compared as a goal's are (simulation.same_settings):
    the real light came on red, shown in hue and saturation; a replay that sets it red by name goes as it went, one
    that sets it blue doesn't."""
    from test_openrouter_replay import bind_openrouter, call, reply
    record = kitchen(color_mode="hs", hs_color=[0.0, 100.0], brightness=255)
    await bind_openrouter(hass, record, [call("light__HassLightSet", {"name": "Kitchen light", "color": colour}, "c1"),
                                         reply("Done.")] + [reply("Ok.")] * 3)
    found = await (await replay(hass, monkeypatch, record)).run()
    assert found["status"] == status, found["steps"]
    assert found["steps"][0]["transitions"] == [{"entity_id": KITCHEN, "old": "off", "new": "on"}]
    assert effect_guards == []


@pytest.mark.parametrize("asked, status", [(80, "reproduced"), (10, "not_reproducible")])
@pytest.mark.asyncio
async def test_the_before_compares_a_device_whose_settings_alone_the_recorded_request_changed(
        hass, monkeypatch, effect_guards, asked, status):
    """DETAILS P6.6.1: the kitchen light was on at 20%, and the recorded request took it to 80%, its state unchanged:
    a replay that sets 80% goes as it went, one that sets 10% doesn't."""
    from test_openrouter_replay import bind_openrouter, call, reply
    record = kitchen(204)
    for n, turn in enumerate(record["turns"]):
        turn["source"]["states"][KITCHEN].update(state="on", attributes={
            "friendly_name": "Kitchen light", "supported_color_modes": ["brightness"], "color_mode": "brightness",
            "brightness": 204 if n else 51})
    was = deepcopy(record["turns"][0]["source"]["states"][KITCHEN])
    record["state_changes"][0].update(old=was, new={**record["state_changes"][0]["new"], "state": "on",
                                                    "attributes": {**was["attributes"], "brightness": 204}})
    await bind_openrouter(hass, record, [call("light__HassLightSet", {"name": "Kitchen light", "brightness": asked},
                                              "c1"), reply("Done.")] + [reply("Ok.")] * 3)
    found = await (await replay(hass, monkeypatch, record)).run()
    assert found["status"] == status, found["steps"]
    assert effect_guards == []


@pytest.mark.asyncio
async def test_the_before_compares_the_settings_assist_asked_for_not_what_a_device_reports_beyond(
        hass, monkeypatch, effect_guards):
    """DETAILS P6.6.1: the speaker was asked to play a playlist, and showed the track it was on, as a real one does;
    the replay's speaker, asked for the same playlist, shows the playlist. The settings compared are those the
    recorded call log asked for, so it goes as it went."""
    from test_openrouter_replay import bind_openrouter, call, openrouter_recording, reply
    speaker, playlist = "media_player.kitchen_speaker", "spotify:playlist:37i9dQZF1DXbITWG1ZJKYt"
    asked = {"media_content_id": playlist, "media_content_type": "playlist"}
    play = {"alias": "Fixture press", "sequence": [
        {"action": "media_player.play_media", "target": {"entity_id": speaker}, "data": asked}]}
    record = json.loads(json.dumps(openrouter_recording(scripts={"fixture_press": play})).replace(
        TARGET, speaker).replace("Fixture relay", "Kitchen speaker").replace('"domain": ["switch"]',
                                                                             '"domain": ["media_player"]'))
    for turn in record["turns"]:
        turn["source"]["states"][speaker].update(state="idle", attributes={
            "friendly_name": "Kitchen speaker", "supported_features": 512 | 16384 | 4 | 8, "volume_level": 0.3})
        turn["source"]["exposure"][SCRIPT] = True
    first = recording_turns(record)[0]
    was = deepcopy(first["source"]["states"][speaker])
    record["state_changes"] = [{"entity_id": speaker, "at": "2026-01-01T00:00:04+00:00", "old": was, "new": {
        **was, "state": "playing", "attributes": {**was["attributes"], "media_content_type": "music",
                                                  "media_content_id": "spotify:track:4uLU6hMCjMI75M1A2tKUQC"},
        "context": deepcopy(first["prepared"]["context"]["context"])}}]
    record["turns"][0]["actions"] = [{"action": SCRIPT, "data": {}},
                                     {"action": "media_player.play_media", "data": {"entity_id": speaker, **asked},
                                      "under": 0}]
    await bind_openrouter(hass, record, [call("script__fixture_press", {}, "s1"), reply("Playing.")]
                          + [reply("Ok.")] * 3)
    found = await (await replay(hass, monkeypatch, record)).run()
    assert found["status"] == "reproduced", found["steps"]
    assert effect_guards == []


@pytest.mark.parametrize("asks, status", [(False, "reproduced"), (True, "not_reproducible")])
@pytest.mark.asyncio
async def test_a_command_that_failed_at_home_must_fail_in_the_before_too(hass, monkeypatch, effect_guards, asks,
                                                                         status):
    """DETAILS P6.6.1 (#101): the recorded command to the light failed at home, leaving it off; a replay sending it
    fails it again (the sandbox refuses it as the home did) and goes as it went; one that only asks which light sends
    nothing to fail, though the light stays off either way, and doesn't."""
    from test_openrouter_replay import bind_openrouter, call, reply
    record = kitchen(200)
    record["state_changes"] = []
    for turn in record["turns"]:
        turn["source"]["states"][KITCHEN].update(state="off", attributes={
            "friendly_name": "Kitchen light", "supported_color_modes": ["brightness"]})
    record["turns"][0]["actions"] = [{"action": "light.turn_on", "data": {"entity_id": KITCHEN},
                                      "error": "The light didn't respond"}]
    first = [reply("Which light?")] if asks else [
        call("intent__HassTurnOn", {"name": "Kitchen light", "domain": ["light"]}, "c1"), reply("It didn't respond.")]
    await bind_openrouter(hass, record, first + [reply("Ok.")] * 3)
    found = await (await replay(hass, monkeypatch, record)).run()
    assert found["status"] == status, found["steps"]
    assert found["steps"][0]["transitions"] == []
    assert any(a.get("error") for a in found["steps"][0]["actions"]) is not asks and effect_guards == []


@pytest.mark.asyncio
async def test_a_value_home_assistant_refused_as_invalid_is_assists_slip_not_the_home_failing(hass, monkeypatch,
                                                                                             effect_guards):
    """DETAILS P9.10.6: Assist asked the hallway for 50 degrees and Home Assistant refused it as out of the
    thermostat's range (a ServiceValidationError): Assist's slip, not the home's. The home could do it, and a replay
    asking for 21 isn't refused for what was refused at home: Home Assistant checks its range itself."""
    from test_openrouter_replay import bind_openrouter, call, openrouter_recording, reply
    from custom_components.assist_improver import analyzer
    from custom_components.assist_improver.simulation import recorded
    hall = "climate.hallway"
    record = json.loads(json.dumps(openrouter_recording()).replace(TARGET, hall).replace(
        "Fixture relay", "Hallway").replace('"domain": ["switch"]', '"domain": ["climate"]'))
    for turn in record["turns"]:
        turn["source"].update(scripts={}, automations=[])
        turn["source"]["states"][hall].update(state="heat", attributes={
            "friendly_name": "Hallway", "hvac_modes": ["off", "heat"], "min_temp": 7, "max_temp": 35,
            "temperature": 18, "supported_features": 1})
        turn["prepared"]["tools"].append({"name": "climate__HassClimateSetTemperature", "description": "Set it",
                                          "integration": "climate", "parameters": {"type": "object"}})
    record["state_changes"] = []
    record["turns"][0]["actions"] = [{"action": "climate.set_temperature",
                                      "data": {"entity_id": [hall], "temperature": 50.0},
                                      "error": "Provided temperature 50.0 is not valid. Accepted range is 7 to 35.",
                                      "error_type": "ServiceValidationError"}]
    turns = recording_turns(record, replay=False)
    view = SimpleNamespace(source=turns[0]["source"], sources=[t["source"] for t in turns],
                           actions={1: record["turns"][0]["actions"]}, changes={})
    heat = {"turn": 1, "summary": "Hallway to 21", "kind": "state", "entity_id": hall, "state": "heat",
            "attributes": {"temperature": 21}}
    assert analyzer.home_couldnt(view, heat, [heat]) is None and recorded(record)["failed"] == []
    await bind_openrouter(hass, record, [call("climate__HassClimateSetTemperature", {"name": "Hallway",
                                                                                     "temperature": 21}, "c1"),
                                         reply("Done.")] + [reply("Ok.")] * 3)
    step = (await (await replay(hass, monkeypatch, record)).run())["steps"][0]
    assert step["commands"][0]["data"]["temperature"] == 21 and not any(
        (m.get("result") or {}).get("error") for m in step["output"] if m.get("role") == "tool_result")
    assert effect_guards == []


@pytest.mark.asyncio
async def test_a_tool_the_provider_runs_itself_runs_there_in_a_replay_labelled_as_an_outside_receipt(
        hass, monkeypatch, effect_guards):
    """The provider's own web search runs at the provider, as at home: the replay goes on, the call kept as the
    provider ran it, an outside receipt (DETAILS P6.2.12, P11.7)."""
    record = recording()
    _, client = await bind_agent(hass, record)
    client.blocks = before(record)
    client.blocks[0] = [
        {"type": "server_tool_use", "id": "web", "name": "web_search", "input": {"query": "relay"}},
        {"type": "web_search_tool_result", "tool_use_id": "web", "content": []}, *client.blocks[0]]
    found = await (await replay(hass, monkeypatch, record)).run()
    assert found["status"] == "reproduced", found["error"]
    assert found["steps"][0]["external_calls"] == [{"name": "web_search", "args": {"query": "relay"},
                                                     "provenance": "provider_execution"}]
    assert not client.blocks and effect_guards == []


@pytest.mark.asyncio
async def test_a_tool_the_assistant_doesnt_have_gets_home_assistants_own_answer_in_a_replay(hass, monkeypatch,
                                                                                             effect_guards):
    """A model asking for a tool the assistant doesn't have gets Home Assistant's own answer, as at home, and goes
    on: the turn didn't do what it did when recorded."""
    record = recording()
    _, client = await bind_agent(hass, record)
    client.blocks = [[{"type": "tool_use", "id": "unknown", "name": "UnknownTool", "input": {}}],
                     [{"type": "text", "text": "I couldn't do that."}]]
    found = await (await replay(hass, monkeypatch, record)).run()
    [answered] = [m for m in found["steps"][0]["output"] if m["role"] == "tool_result"]
    assert answered["tool_name"] == "UnknownTool" and "not found" in json.dumps(answered["tool_result"]).lower()
    assert found["status"] == "not_reproducible" and effect_guards == []


@pytest.mark.asyncio
async def test_the_providers_own_tool_search_runs_in_a_replay_and_its_result_passes_through(hass, monkeypatch,
                                                                                             effect_guards):
    record = recording()
    agent, client = await bind_agent(hass, record)
    client.blocks = before(record)
    # In turn 2 the provider searches the recorded tools itself: the recording has no receipt for this search.
    client.blocks[2] = [
        {"type": "server_tool_use", "id": "search", "name": "tool_search_tool_bm25", "input": {"query": "press"}},
        {"type": "tool_search_tool_result", "tool_use_id": "search", "content": {
            "type": "tool_search_tool_search_result",
            "tool_references": [{"type": "tool_reference", "tool_name": "intent__HassTurnOn"}]}},
        {"type": "text", "text": "Generated response 2: the press script is not exposed."}]
    found = await (await replay(hass, monkeypatch, record)).run()
    assert found["status"] == "reproduced", found["error"]
    assert [c["name"] for c in found["steps"][1]["external_calls"]] == ["tool_search_tool_bm25"]
    assert not client.blocks and effect_guards == []


@pytest.mark.asyncio
async def test_a_cancelled_replay_closes_its_sandbox(hass, monkeypatch, effect_guards):
    import asyncio
    from custom_components.assist_improver.worker import KEY
    record = recording()
    _, client = await bind_agent(hass, record)
    script, waiting = before(record), asyncio.Event()

    async def second_request_never_answers(request):  # Turn 1's calls ran in the sandbox; its reply never comes.
        if len(client.requests) > 1:
            waiting.set()
            await asyncio.Event().wait()
        return script.pop(0)
    client.reply = second_request_never_answers
    trial = await replay(hass, monkeypatch, record)
    task = asyncio.create_task(trial.run())
    await waiting.wait()
    assert trial.home.sandbox is not None
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert trial.entry["status"] == "cancelled" and trial.home.sandbox is None and not hass.data[KEY]  # Its worker.
    assert effect_guards == []


def telling_the_phone(record, exposed=False):
    """The household's script telling a phone (an action beyond the home: stood in and logged, never reaching a
    phone), run by an automation when the porch comes on; exposed, Assist can run it itself."""
    for turn in record["turns"]:
        source = turn["source"]
        source["scripts"]["tell_phone"] = {"alias": "Tell phone", "sequence": [
            {"action": "notify.mobile_app_phone", "data": {"message": "Porch on"}}]}
        source["automations"] = [{"id": "porch_tell", "alias": "Porch tells phone",
                                  "triggers": [{"trigger": "state", "entity_id": PORCH, "to": "on"}],
                                  "actions": [{"action": "script.tell_phone"}]}]
        source["exposure"]["script.tell_phone"] = exposed
    return record


@pytest.mark.asyncio
async def test_what_others_moves_set_off_between_turns_isnt_the_replays_doing(hass, monkeypatch, effect_guards):
    """Someone turns the porch on between turns 1 and 2, and the household's automation tells a phone, which no
    sandbox can (stood in): that isn't Assist's doing, so turn 2 replays on."""
    record = telling_the_phone(recording())
    for turn in record["turns"][1:]:
        turn["source"]["states"][PORCH]["state"] = "on"
    _, client = await bind_agent(hass, record)
    client.blocks = before(record)
    found = await (await replay(hass, monkeypatch, record)).run()
    assert found["status"] == "reproduced", found["error"]
    assert [s["turn"] for s in found["steps"]] == [1, 2, 3, 4] and effect_guards == []


@pytest.mark.asyncio
async def test_an_action_beyond_the_home_is_stood_in_and_logged_as_an_outside_receipt_and_the_replay_goes_on(
        hass, monkeypatch, effect_guards):
    """Assist runs the script that tells the phone: the phone's action is stood in, answered empty and marked not
    simulated, an outside receipt (DETAILS P6.2.13, P11.7), never pretending it reached the phone; the replay goes
    on."""
    record = telling_the_phone(recording(), exposed=True)
    _, client = await bind_agent(hass, record)
    client.blocks = TELL_PHONE + [[{"type": "text", "text": "Ok."}]] * 3
    found = await (await replay(hass, monkeypatch, record, services=PHONE)).run()
    assert found["status"] == "not_reproducible"  # Turn 1 told the phone, not the relay.
    step = found["steps"][0]
    assert step["outside"] == [{"action": "notify.mobile_app_phone", "data": {"message": "Porch on"},
                                "answer": "not simulated"}]
    assert step["not_simulated"] == ["notify.mobile_app_phone"] and effect_guards == []


@pytest.mark.asyncio
async def test_a_conversation_whose_prompt_home_assistant_words_differently_now_replays_and_reproduces(
        hass, monkeypatch, effect_guards):
    """Home Assistant 2026.11 reworded a line of Assist's prompt, and every conversation recorded on 2026.10 stopped
    replaying. A replay is the conversation said again on today's setup (DETAILS P6.2.4): the model reads what Home
    Assistant builds now, its assistant's instructions in it, and the conversation goes as it went."""
    record = recording()
    then = "You are a voice assistant, as Home Assistant 2026.10 worded it."
    for message in [t["preparations"][0]["content"][0] for t in record["turns"]] + [record["chat_log"]["content"][0]]:
        message["content"] = then
    assert record["core_version"] == "2026.10.2"
    _, client = await bind_agent(hass, record)
    client.blocks = before(record)
    found = await (await replay(hass, monkeypatch, record)).run()
    assert found["status"] == "reproduced", found["error"]
    assert [s["turn"] for s in found["steps"]] == [1, 2, 3, 4] and not client.blocks
    for request in client.requests:
        assert then not in request["system"][0]["text"] and "Recorded template" in request["system"][0]["text"]
    assert effect_guards == []


@pytest.mark.asyncio
async def test_trouble_an_edit_at_home_has_fixed_since_doesnt_come_back_in_the_replay_so_theres_no_fix_to_test(
        hass, monkeypatch, effect_guards):
    """In the conversation the press script switched the porch on along with the relay; the household has since
    edited it to press only the relay. The replay runs it as it is now (DETAILS P6.2.4): turn 2 leaves the porch off,
    so it doesn't go as it went, and the conversation can't be tested: nothing is left to fix."""
    careless = {**PRESS_SCRIPT, "sequence": [*PRESS_SCRIPT["sequence"],
                                             {"action": "switch.turn_on", "target": {"entity_id": PORCH}}]}
    record = pressing_at_turn_2(lambda change: [
        change(SCRIPT, "off", "on", 0.3), change(TARGET, "off", "on", 0.4), change(PORCH, "off", "on", 0.5),
        change(SCRIPT, "on", "off", 0.6)])
    for turn in record["turns"]:
        turn["source"]["scripts"]["fixture_press"] = deepcopy(careless)  # As the household had it then.
    for turn in record["turns"][2:]:
        turn["source"]["states"][PORCH]["state"] = "on"
    _, client, runtime = await setup(hass, monkeypatch, record)  # The control: the script as it was then.
    client.blocks = before(record)
    plan, today = await simulate.planned(hass, record)
    assert (await simulate.Trial(hass, plan, record, today).run())["status"] == "reproduced"
    at_home(monkeypatch, record, scripts={"fixture_press": PRESS_SCRIPT})  # As it is now.
    client.blocks = before(record)[:4] * 3  # Each of the baseline's tries, to turn 2.
    with pytest.raises(simulate.Untestable, match="turn 2 didn't do what it did") as raised:
        await run(hass, runtime)
    assert raised.value.conversation and effect_guards == [] and not client.blocks
    for kept in tries(runtime):
        assert kept["status"] == "not_reproducible" and [s["turn"] for s in kept["steps"]] == [1, 2]
        assert kept["steps"][0]["matched"] and kept["steps"][1]["matched"] is False


@pytest.mark.asyncio
async def test_a_device_gone_from_the_home_is_gone_from_the_replay(hass, monkeypatch, effect_guards):
    """The household has removed the porch switch since the conversation: the replay runs without it, and goes as
    the conversation went, which never touched it. With the relay it was about gone too, it can't go as it went: the
    conversation is one a replay can't test, said as much."""
    record = recording()
    gone = lambda *ids: {k: {e: v for e, v in record["turns"][0]["source"][k].items() if e not in ids}  # noqa: E731
                         for k in ("states", "entities", "exposure")}
    _, client, runtime = await setup(hass, monkeypatch, record, home=gone(PORCH))
    client.blocks = before(record) + pressed(1, 4)
    test = await run(hass, runtime)
    assert test["before"]["status"] == "reproduced" and test["validated"] and not client.blocks
    assert test["before"]["config"]["exposed"] == [TARGET]
    at_home(monkeypatch, record, **gone(PORCH, TARGET))
    client.blocks = before(record)[:2] * 3  # Each of the baseline's tries, to turn 1.
    with pytest.raises(simulate.Untestable, match="turn 1 didn't do what it did") as raised:
        await run(hass, runtime)
    assert raised.value.conversation and effect_guards == []
    kept = [r for r in runtime.replays.items.values() if r["status"] == "not_reproducible"]
    assert len(kept) == 3 and all([s["turn"] for s in r["steps"]] == [1] and r["steps"][0]["matched"] is False
                                  for r in kept)


@pytest.mark.asyncio
async def test_a_device_whose_entity_id_changed_since_is_the_same_device_in_the_replay(hass, monkeypatch,
                                                                                       effect_guards):
    """The household has changed the relay's entity ID in Home Assistant's settings since the conversation, and its
    press script with it. The replay finds the relay under its new entity ID by what that keeps (its integration,
    device and the name its integration gives it, DETAILS P6.2.4): it starts as it was then, the conversation goes as
    it went, and the goals, the recording's, follow it. The recording itself stays as it was."""
    workshop = "switch.workshop_relay"
    record, home = relay_renamed(recording(), workshop)
    home["states"][workshop]["state"] = "off"  # Today; it was on as the conversation started, and turn 1 switches it off.
    kept = deepcopy(record)
    _, client, runtime = await setup(hass, monkeypatch, record, home=home)
    client.blocks = before(record) + pressed(1, 4)
    test = await run(hass, runtime)
    assert test["before"]["status"] == "reproduced" and test["validated"] and not client.blocks
    assert test["before"]["config"]["exposed"] == [PORCH, workshop]
    devices = {e for goal in test["judged"].values() for e in goal["devices"]}
    assert workshop in devices and TARGET not in devices
    assert record == kept and effect_guards == []


def test_an_entity_id_changed_since_is_known_only_by_what_a_new_entity_id_keeps():
    """An entity gone since and one new since are one entity renamed only when they alone share a domain, an
    integration and its entry, a device and the name the integration gives it; one with neither a device nor such a
    name, or two alike, can't be told apart, and stay gone."""
    from custom_components.assist_improver.snapshot import renamed
    lamp, helper = {"platform": "hue", "config_entry_id": "e", "device_id": "d", "original_name": None}, {
        "platform": "input_boolean", "config_entry_id": None, "device_id": None, "original_name": None}
    record = {"turns": [{"source": {"entities": {"light.lamp": lamp, "input_boolean.old": helper}}}]}
    assert renamed(record, {"entities": {"light.desk": lamp, "input_boolean.new": helper}}) == {
        "light.lamp": "light.desk"}
    assert renamed(record, {"entities": {"light.desk": lamp, "light.desk_2": lamp}}) == {}
    assert renamed(record, {"entities": {"switch.lamp": lamp, "light.lamp": lamp}}) == {}


def test_an_entity_id_changed_since_is_its_registry_entry_where_both_sides_keep_its_id():
    """Recordings since entries are kept whole carry each entity's registry id, as today's home does: the same id is
    the same entity under its new entity ID, however alike two entities are (DETAILS P6.2.4)."""
    from custom_components.assist_improver.snapshot import renamed
    lamp = {"id": "entry-1", "platform": "hue", "config_entry_id": "e", "device_id": "d", "original_name": None}
    twin = {**lamp, "id": "entry-2"}
    record = {"turns": [{"source": {"entities": {"light.lamp": lamp, "light.twin": twin}}}]}
    assert renamed(record, {"entities": {"light.desk": lamp, "light.desk_2": twin}}) == {
        "light.lamp": "light.desk", "light.twin": "light.desk_2"}
    assert renamed(record, {"entities": {"light.lamp": lamp, "light.twin": twin}}) == {}


@pytest.mark.asyncio
async def test_an_assistant_whose_provider_isnt_loaded_settles_as_one_a_replay_cant_run(hass, monkeypatch,
                                                                                          effect_guards):
    """A sandbox loads a custom provider only as the household has it loaded (replay.custom_providers): an assistant
    whose provider isn't can't be replayed, said as much, rather than failing every run."""
    from types import MappingProxyType
    from homeassistant import config_entries
    from homeassistant.helpers import entity_registry as er
    record = recording()
    _, client = await bind_agent(hass, record)
    entry = config_entries.ConfigEntry(
        domain="custom_provider", title="Custom", data={"api_key": "x"}, source="user", version=1, minor_version=1,
        options={}, unique_id=None, discovery_keys=MappingProxyType({}), entry_id="custom-entry",
        subentries_data=[{"data": {"prompt": "p"}, "subentry_id": "custom-sub", "subentry_type": "conversation",
                          "title": "Custom", "unique_id": None}])
    hass.config_entries._entries[entry.entry_id] = entry
    registry = er.async_get(hass)
    registry.async_remove("conversation.fixture")
    registry.async_get_or_create("conversation", "custom_provider", "custom-sub", suggested_object_id="fixture",
                                 config_entry=entry, config_subentry_id="custom-sub")
    found = await (await replay(hass, monkeypatch, record)).run()
    assert found["status"] == "unsupported" and "isn't loaded" in found["error"]["message"]
    assert client.requests == [] and effect_guards == []


@pytest.mark.asyncio
async def test_an_assistant_on_recommended_settings_replays_on_the_model_home_assistant_runs_it_on(hass, monkeypatch,
                                                                                                  effect_guards):
    """An Anthropic assistant left on Home Assistant's recommended settings names no model: HA runs it on its own
    default chat model. Its replay runs on the same, HA picking it in the sandbox as at home, with nothing for the
    household to set (DETAILS P6.2.2); HA's defaults fill the rest of its options."""
    from homeassistant.components.anthropic.const import DEFAULT
    record = recording()
    # As HA's flow leaves a new assistant: recommended, no model named.
    _, client = await bind_agent(hass, record, {"recommended": True, "prompt": "Recorded template",
                                                "llm_hass_api": ["assist"]})
    client.models.append(DEFAULT["chat_model"])
    client.blocks = before(record)
    found = await (await replay(hass, monkeypatch, record)).run()
    assert found["status"] == "reproduced", found["error"]
    assert {r["model"] for r in client.requests} == {DEFAULT["chat_model"]} and effect_guards == []


@pytest.mark.asyncio
async def test_a_replay_runs_with_the_rights_the_person_speaking_has_today(hass, monkeypatch, effect_guards):
    """Home Assistant gives an assistant's admin-only LLM API (its "Home Assistant" one) to administrators alone: a
    replay of someone who isn't one fails as their requests do at home, never with an administrator's rights."""
    from homeassistant.components.conversation import ChatLog, ConverseError
    from homeassistant.core import Context
    from homeassistant.helpers import llm
    from conftest import household_user
    hass.auth.async_get_user.return_value = household_user("Fixture user", "system-users")
    record = recording()
    _, client = await bind_agent(hass, record, {"recommended": True, "prompt": "Recorded template",
                                                "llm_hass_api": ["assist", llm.LLM_API_HOME_ASSISTANT]})
    client.models.append("claude-haiku-4-5")
    client.blocks = before(record)
    context = llm.LLMContext(platform="anthropic", context=Context(user_id="fixture-user"), language="en",
                             assistant="conversation", device_id=None)
    with pytest.raises(ConverseError) as home:  # At home.
        await ChatLog(hass, "fixture-home").async_provide_llm_data(
            context, user_llm_hass_api=["assist", llm.LLM_API_HOME_ASSISTANT], user_llm_prompt="Recorded template")
    found = await (await replay(hass, monkeypatch, record)).run()
    assert found["error"] == {"type": "HomeAssistantError", "turn": 1,
                              "message": home.value.response.speech["plain"]["speech"]}
    assert client.requests == [] and effect_guards == []


@pytest.mark.asyncio
async def test_simulates_replay_tokens_are_not_the_proposers(hass, monkeypatch, effect_guards):
    """A test runs inside the proposer's call (its tool), on Home Assistant's real AI Task path: the tokens the
    household's assistant spends in Simulate's replays are the replays' usage, not the proposer's. The proposer's call
    reports only its own model's usage (none, for this scripted one), while each replay reports its own."""
    import probatio
    from homeassistant.helpers import llm
    from ha_task import serve, use
    from custom_components.assist_improver.exploration import SessionAPI
    from custom_components.assist_improver.reflective import run_role
    record = recording()
    _, client, runtime = await setup(hass, monkeypatch, record)
    client.blocks = before(record) + pressed(1, 4)
    tested = {}

    class TestIt(llm.Tool):  # Stands for test_idea: one test of the idea, on this conversation.
        name, description, parameters = "test_it", "Test the idea.", probatio.Schema({})
        integration = "assist_improver"

        async def async_call(self, hass, tool_input, llm_context):
            tested.update(await run(hass, runtime))
            return llm.ToolResult(data={"validated": tested["validated"]})

    class Proposer:
        async def answer(self, hass, role, payload, tools):
            await use(hass, tools["test_it"])
            return "Tested."
    hass.states.async_set("ai_task.opus", "unknown")
    serve(hass, Proposer(), ("ai_task.opus",))
    proposed = await run_role(hass, role="hypothesis_generator", entity_id="ai_task.opus", instructions="i",
                              payload={}, structure=None, llm_api=SessionAPI(hass, tools=[TestIt()]),
                              allow_paid_inference=True)
    assert proposed["status"] == "completed" and tested["validated"], proposed["error"]
    replays = [runtime.replays.get(tested[kind]["plan"]) for kind in ("before", "after")]
    assert all((replay.get("usage") or {}).get("events") for replay in replays)
    assert proposed["usage"]["events"] == []


@pytest.mark.asyncio
async def test_settled_replays_are_reused_and_ones_the_machinery_cut_off_run_again(hass, monkeypatch, effect_guards):
    record = recording()
    _, client, runtime = await setup(hass, monkeypatch, record)
    async def unreachable(request):
        raise OSError("synthetic transport failure")
    client.reply = unreachable
    with pytest.raises(simulate.Transient, match="Connection error"):
        await run(hass, runtime)
    [kept] = runtime.replays.items.values()
    assert kept["status"] == "infrastructure_failure"
    client.reply = None  # The transport works again: the before runs again, then the after.
    client.blocks = before(record) + pressed(1, 4)
    first = await run(hass, runtime)
    asked = len(client.requests)
    again = await run(hass, runtime)  # Both settled: nothing replays.
    assert len(client.requests) == asked and {**again, "at": None} == {**first, "at": None}
    after = runtime.replays.items[first["after"]["plan"]]
    await runtime.replays.save({**after, "status": "interrupted"})  # A restart cut it off: only it runs again.
    client.blocks = pressed(1, 4)
    assert (await run(hass, runtime))["validated"] and len(client.requests) == asked + 4
    running = {**after, "status": "running"}
    await runtime.replays.save(running)
    reloaded = simulate.Replays(hass)
    await reloaded.start()
    assert reloaded.get(after["plan"]["id"])["status"] == "interrupted"


@pytest.mark.asyncio
async def test_a_kept_replay_stands_only_for_the_replay_engine_that_made_it(hass, monkeypatch, effect_guards):
    """A change to how replays run makes a replay kept before it a different test, never reused."""
    record = recording()
    _, client, runtime = await setup(hass, monkeypatch, record)
    client.blocks = before(record) + pressed(1, 4)
    first = await run(hass, runtime)
    asked = len(client.requests)
    await run(hass, runtime)
    assert len(client.requests) == asked  # The same engine: both kept replays stand.
    monkeypatch.setattr(simulate, "REPLAY_ENGINE", "a-changed-engine")
    client.blocks = before(record) + pressed(1, 4)
    again = await run(hass, runtime)
    assert not client.blocks and len(client.requests) == 2 * asked  # Both replayed afresh, the old ones kept.
    assert again["before"]["plan"] != first["before"]["plan"] and again["validated"]
    assert first["before"]["plan"] in runtime.replays.items


@pytest.mark.asyncio
async def test_a_setup_change_between_the_baseline_and_a_test_runs_the_baseline_again(hass, monkeypatch,
                                                                                    effect_guards):
    """A comparison runs on one setup (DETAILS P6.7.1): once the household changes its setup (here, renames the
    room), a test replays the conversation without the change again, its tries from the first, on the setup as it is
    now, and the old replays stay as history. The devices' states are the conversation's, so they're no change."""
    record = recording()
    _, client, runtime = await setup(hass, monkeypatch, record)
    client.blocks = [[{"type": "text", "text": "Which relay?"}]] + before(record) + pressed(1, 4)
    first = await run(hass, runtime)
    assert [r["plan"]["try"] for r in tries(runtime)] == [1, 2]
    asked = len(client.requests)
    states = deepcopy(record["turns"][0]["source"]["states"])
    states[PORCH]["state"] = "on"  # Today the porch is on: replays still start as the conversation found it.
    at_home(monkeypatch, record, states=states)
    await run(hass, runtime)
    assert len(client.requests) == asked  # The same setup: the kept replays stand.
    at_home(monkeypatch, record, areas={"fixture_area": {"name": "Hall"}})
    client.blocks = before(record) + pressed(1, 4)
    again = await run(hass, runtime)
    assert not client.blocks and len(client.requests) == 2 * asked - 1  # Replayed afresh, the first try reproducing.
    assert runtime.replays.items[again["before"]["plan"]]["plan"]["try"] == 1 and again["validated"]
    assert {r["plan"]["id"] for r in tries(runtime)} > {first["before"]["plan"], again["before"]["plan"]}
    assert {first["before"]["plan"], first["after"]["plan"]} < set(runtime.replays.items)
    assert effect_guards == []


def test_a_plans_setup_changes_with_what_a_replay_depends_on_only():
    """The setup a plan names (DETAILS P6.7.1) changes with what a replay depends on, the assistant's settings among
    it, never with the devices' states, a registry entry's time stamps, the provider entry's data (its credentials),
    or how what Assist can't see is named (a router renames a laptop it tracks to its hardware address)."""
    today = recorded_today(recording())
    today["home"]["entities"]["device_tracker.laptop"] = {"name": None, "original_name": "Laptop", "aliases": [],
                                                          "area_id": None, "modified_at": "2026-11-05T08:00:00"}
    today["assistant"]["entry"]["data"] = {"api_key": "sk-old"}
    today["home"]["exposure"]["device_tracker.laptop"] = False
    plan = simulate.setup_digest(today)

    def changed(path, value):
        found = deepcopy(today)
        *where, key = path
        target = found
        for step in where:
            target = target[step]
        target[key] = value
        return simulate.setup_digest(found) != plan
    assert not changed(("home", "entities", "device_tracker.laptop", "original_name"), "02:00:00:00:00:01")
    assert not changed(("home", "entities", "device_tracker.laptop", "modified_at"), "2026-11-06T08:00:00")
    assert not changed(("home", "states", TARGET, "state"), "off")
    assert not changed(("assistant", "entry", "data", "api_key"), "sk-new")
    assert changed(("home", "entities", TARGET, "name"), "Workshop relay")
    assert changed(("home", "entities", "device_tracker.laptop", "area_id"), "fixture_area")
    assert changed(("assistant", "entry", "subentries", 0, "data", "output_modalities"), ["text", "image"])


def test_the_replay_engines_version_follows_the_code_not_the_version_an_install_sets(tmp_path):
    (tmp_path / "sandbox.py").write_text("SANDBOX = 1\n")
    (tmp_path / "const.py").write_text('VERSION = "0.40.1.dev5"\n')
    (tmp_path / "hypotheses").mkdir()
    (tmp_path / "hypotheses" / "run.py").write_text("RUN = 1\n")
    first = simulate.engine_version(tmp_path)
    (tmp_path / "const.py").write_text('VERSION = "0.40.1.dev6"\n')  # An install that changes nothing else.
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "x.py").write_text("compiled\n")
    assert simulate.engine_version(tmp_path) == first
    (tmp_path / "hypotheses" / "run.py").write_text("RUN = 2\n")  # A change to the code, anywhere in it.
    assert simulate.engine_version(tmp_path) != first
    assert simulate.REPLAY_ENGINE == simulate.engine_version(Path(simulate.__file__).parent)


OBJECTED = {**ANALYSIS, "turns": 4, "goals": [ANALYSIS["goals"][0], ANALYSIS["goals"][1], {
    "turn": 3, "summary": "Not like that", "kind": "conduct", "about": 1, "quote": "Turn it back on", "serves": 1},
    ANALYSIS["goals"][3]]}


@pytest.mark.asyncio
async def test_an_objections_turn_is_skipped_once_no_reply_so_far_shows_it_and_said_while_one_does(
        hass, monkeypatch, effect_guards):
    """DETAILS P6.4: the replay with the change asks the judge about turn 3's objection before it would say it, over
    the replies so far; none shows it, so turn 3 isn't said, and the objection is met. Without the change every turn
    is said and the reply it objected to shows it. One call before, one in the replay with the change; none after it
    (each answer is kept with the replay)."""
    record = recording()
    _, client, runtime = await setup(hass, monkeypatch, record)
    client.blocks = before(record) + pressed(1, 4)
    asked, answers, check = judging(True, False)
    monkeypatch.setattr(simulate, "check_conduct", check)
    test = await run(hass, runtime, measures.goals_of(OBJECTED))
    assert not client.blocks and not answers and len(asked) == 2 and asked[1][1] == ["Pressed."]
    assert test["after"]["said"] == [1, 4] and test["after"]["goals"]["3:2"] == "met"
    assert test["before"]["goals"]["3:2"] == "unmet" and test["validated"]


@pytest.mark.asyncio
async def test_a_verdict_the_replay_with_the_change_couldnt_get_cuts_it_off_and_it_runs_again_keeping_the_before(
        hass, monkeypatch, effect_guards):
    """A verdict that decides whether a turn is said is part of its replay (DETAILS P6.3.5.1): the judge couldn't
    answer there, so the replay is cut off (Transient) and runs again whole; the replay without the change, settled,
    is kept and not judged again."""
    record = recording()
    _, client, runtime = await setup(hass, monkeypatch, record)
    client.blocks = before(record) + pressed(1) + pressed(1, 4)
    asked, answers, check = judging(True, None, False)
    monkeypatch.setattr(simulate, "check_conduct", check)
    goals = measures.goals_of(OBJECTED)
    with pytest.raises(simulate.Transient, match="couldn't finish"):
        await run(hass, runtime, goals)
    test = await run(hass, runtime, goals)
    assert not client.blocks and not answers and len(asked) == 3
    [entry] = [r for r in runtime.replays.items.values() if r["plan"]["kind"] == "before"]
    assert entry["status"] == "reproduced" and test["after"]["said"] == [1, 4]


@pytest.mark.asyncio
async def test_the_judge_gets_the_turn_objected_to_and_each_replay_turn_with_what_assist_did_and_its_results(
        hass, monkeypatch, effect_guards):
    """DETAILS P6.1.9: the judge is asked about a replay turn with the facts in hand: the turn the household objected
    to, as recorded, and each replay turn, each with what the user said, every tool call with Home Assistant's result,
    the reply, and what the home did (the recording's changes, or what the replay's sandbox logged), in that order."""
    from custom_components.assist_improver import reflective
    record = recording()
    _, client, runtime = await setup(hass, monkeypatch, record)
    client.blocks = before(record) + pressed(1, 3, 4)
    sent = []

    async def judge(hass, **call):
        sent.append(call["payload"])
        return {"status": "completed", "usage": {}, "output": {"answers": [
            {"goal": o["goal"], "turn": n, "shows": True, "why": "As before."}
            for o in call["payload"]["objections"] for n in o["judge"]]}}
    monkeypatch.setattr(reflective, "run_role", judge)
    objected = {**ANALYSIS, "turns": 4, "goals": [*ANALYSIS["goals"][:2], {
        "turn": 3, "summary": "Not like that", "kind": "conduct", "about": 1, "quote": "Turn it back on", "serves": 1},
        ANALYSIS["goals"][3]]}
    await run(hass, runtime, measures.goals_of(objected))
    [objection] = sent[0]["objections"]
    turn, user, did, reply, home = objection["objected_to"]
    assert (turn, user, reply) == ({"turn": 1}, {"user_said": "Toggle the fixture relay."},
                                   {"assistant_replied": "Recorded response 1."})
    assert home == {"home_did": {"changed": ["switch.fixture_relay: on → off"]}}
    [call] = did["assistant_did"]
    assert call["called"] == "intent__HassTurnOff" and call["with"] == {"name": "Fixture relay", "domain": ["switch"]}
    assert '"success": [{"name": "Fixture relay"' in call["result"]
    first = sent[0]["replay"][0]
    assert [list(part) for part in first] == [["turn"], ["user_said"], ["assistant_did"], ["assistant_replied"],
                                              ["home_did"]]
    [replayed] = first[2]["assistant_did"]
    assert replayed["called"] == "intent__HassTurnOff" and "Fixture relay" in replayed["result"]
    assert first[3] == {"assistant_replied": "Generated response 1."} and objection["judge"] == [1, 2]
    assert first[4] == {"home_did": {"called": [  # Its call log: the intent's call, and the one that made.
        'homeassistant.turn_off {"entity_id": "switch.fixture_relay"}',
        'switch.turn_off {"entity_id": ["switch.fixture_relay"]}'], "changed": ["switch.fixture_relay: on → off"]}}


FILED = {**ANALYSIS, "turns": 4, "goals": [*ANALYSIS["goals"][:3], {
    "turn": 4, "summary": "Filed it", "kind": "wrap-up", "serves": 1}, {
    "turn": 5, "summary": "The note", "kind": "conduct", "about": "all", "quote": "Say less", "serves": 1}]}


def judging_by(shows):
    """A stand-in judge answering by a rule: shows(turn, reply); what it was asked: each call's {goal: turns}."""
    asked = []

    async def check(hass, entity_id, objections, steps, turns):
        replies = {s["turn"]: s["reply"] for s in steps}
        asked.append({o["quote"]: o["judge"] for o in objections})
        return {"status": "completed", "usage": {}, "answers": [
            {"goal": o["goal"], "turn": n, "shows": shows(n, replies[n]), "why": "."}
            for o in objections for n in o["judge"]]}
    return asked, check


@pytest.mark.asyncio
async def test_a_filing_whose_note_objects_only_to_its_own_reply_is_set_aside_and_said_with_the_change(
        hass, monkeypatch, effect_guards):
    """The replay with the change decides a filing at its own turn, on the replies before it (DETAILS P6.4). Without the
    change the note's objection shows only in the filing's own reply, so by that rule nothing was open when it was
    filed: the filing is set aside and said with the change too, so its turn is no gain, and the note stays unmet."""
    record = recording()
    _, client, runtime = await setup(hass, monkeypatch, record)
    client.blocks = before(record) + pressed(1, 4)
    _, check = judging_by(lambda turn, reply: turn == 4)
    monkeypatch.setattr(simulate, "check_conduct", check)
    test = await run(hass, runtime, measures.goals_of(FILED))
    assert not client.blocks and test["after"]["said"] == [1, 4]
    assert test["before"]["goals"]["4:3"] == test["after"]["goals"]["4:3"] == "indeterminate"
    assert test["before"]["goals"]["5:4"] == test["after"]["goals"]["5:4"] == "unmet"


@pytest.mark.asyncio
async def test_at_a_filings_turn_the_note_is_judged_on_the_replies_so_far_and_closed_the_filing_isnt_said(
        hass, monkeypatch, effect_guards):
    """The note's objection shows in every reply without the change, so the filing is open there. With it, the judge
    is asked about the note at the filing's turn, over the one reply said so far ("Pressed."): it doesn't show it, so
    the filing isn't said. Two calls in all: one without the change, one in the replay with it; the note's answers
    after the replay are kept ones. The after plan names the judge's model."""
    record = recording()
    _, client, runtime = await setup(hass, monkeypatch, record)
    client.blocks = before(record) + pressed(1)
    asked, check = judging_by(lambda turn, reply: reply != "Pressed.")
    monkeypatch.setattr(simulate, "check_conduct", check)
    test = await run(hass, runtime, measures.goals_of(FILED))
    assert not client.blocks and test["after"]["said"] == [1]
    assert asked == [{"Say less": [1, 2, 3, 4]}, {"Say less": [1]}]
    assert test["after"]["goals"]["4:3"] == test["after"]["goals"]["5:4"] == "met"
    assert test["before"]["goals"]["4:3"] == test["before"]["goals"]["5:4"] == "unmet" and test["validated"]
    [after] = [r for r in runtime.replays.items.values() if r["plan"]["kind"] == "after"]
    assert after["plan"]["judge"] == "ai_task.fixture"


@pytest.mark.asyncio
async def test_at_a_filings_turn_an_earlier_objection_said_anyway_is_judged_and_closed_the_filing_isnt_said(
        hass, monkeypatch, effect_guards):
    """An objection on a turn said anyway (a thanks) isn't judged there: its answer can't change what's said. At the
    filing's turn it's asked about, over the replies before its own turn, so a fix that answers it doesn't say the
    filing (DETAILS P6.4)."""
    record = recording()
    _, client, runtime = await setup(hass, monkeypatch, record)
    client.blocks = before(record) + pressed(1, 3)
    asked, check = judging_by(lambda turn, reply: reply != "Pressed.")
    monkeypatch.setattr(simulate, "check_conduct", check)
    thanked = {**ANALYSIS, "turns": 4, "goals": [*ANALYSIS["goals"][:2], {
        "turn": 3, "summary": "Thanks", "kind": "thanks", "serves": 1}, {
        "turn": 3, "summary": "Not like that", "kind": "conduct", "about": 1, "quote": "Not like that", "serves": 1}, {
        "turn": 4, "summary": "Filed it", "kind": "wrap-up", "serves": 1}]}
    test = await run(hass, runtime, measures.goals_of(thanked))
    assert not client.blocks and test["after"]["said"] == [1, 3]
    assert asked[1:] == [{"Not like that": [1]}]
    assert test["after"]["goals"]["3:3"] == test["after"]["goals"]["4:4"] == "met" and test["validated"]


ASKED_AGAIN = {**ANALYSIS, "turns": 4, "goals": [
    {"turn": 1, "summary": "Whether the relay is on", "kind": "fact", "fact": "The relay's state", "entity_ids": [TARGET],
     "reached": True, "answered": {"turn": 4, "gives": True, "why": "Says it."}},
    *({"turn": n, "summary": "Asks again", "kind": "step", "serves": 1} for n in (2, 3, 4))]}


def answering_by(gives, shown=None):
    """A stand-in judge of answers, by a rule: gives(turn, reply); what it was asked: each call's questions; shown:
    each call's turns, as the judge reads them, if a list is given."""
    asked = []

    async def check(hass, entity_id, questions, turns):
        replies = {turn[0]["turn"]: turn[3]["assistant_replied"] for turn in turns}
        asked.append(questions)
        if shown is not None:
            shown.append(turns)
        return {"status": "completed", "usage": {}, "answers": [
            {"goal": q["goal"], "turn": q["turn"], "gives": gives(q["turn"], replies[q["turn"]]), "why": "."}
            for q in questions]}
    return asked, check


@pytest.mark.asyncio
async def test_a_question_answered_on_the_first_reply_with_the_change_saves_the_turns_spent_asking_again(
        hass, monkeypatch, effect_guards):
    """DETAILS P9.10.4: without the change the replies go as in the conversation (wrong three times, right the fourth);
    with it, the first reply gives the answer, so the judge, asked just before turn 2 about the reply so far, closes
    the steps and no other turn is said: the question is met in 1 turn instead of 4. The judge sees the home as the
    replay had it."""
    record = recording()
    _, client, runtime = await setup(hass, monkeypatch, record)
    client.blocks = before(record) + pressed(1)
    shown = []
    asked, check = answering_by(lambda turn, reply: reply == "Pressed." or turn == 4, shown)
    monkeypatch.setattr(simulate, "check_facts", check)
    test = await run(hass, runtime, measures.goals_of(ASKED_AGAIN))
    assert not client.blocks and test["after"]["said"] == [1] and test["validated"]
    assert any(turn[4:] and "home_did" in turn[4] for turns in shown for turn in turns)  # What the home did, too.
    assert [[q["turn"] for q in questions] for questions in asked] == [[1, 2, 3, 4], [1]]  # Before; then mid-replay.
    [device] = asked[1][0]["home"]
    assert device["entity_id"] == TARGET and device["state"] == "on"  # As the replay had it when turn 1 started.
    assert test["before"]["goals"]["1:0"] == test["after"]["goals"]["1:0"] == "met"
    assert [test["before"]["goals"][k] for k in ("2:1", "3:2", "4:3")] == ["unmet"] * 3
    assert [test["after"]["goals"][k] for k in ("2:1", "3:2", "4:3")] == ["met"] * 3
    assert test["after"]["facts"]["1:0"]["answers"] == [{"turn": 1, "gives": True, "why": "."}]
    assert (test["before"]["turns_per_root_goal"]["1:0"], test["after"]["turns_per_root_goal"]["1:0"]) == (4, 1)


@pytest.mark.asyncio
async def test_a_turn_with_a_step_under_a_question_and_an_objection_is_skipped_once_the_judge_closes_both(
        hass, monkeypatch, effect_guards):
    """Both judges are asked when their answers together decide the turn: the first reply with the change answers the
    question and doesn't do what was objected to, so turn 2 (asking again, and objecting) isn't said, nor the steps
    after it. Each judge's verdicts are kept with the baseline replay, neither overwriting the other's."""
    record = recording()
    _, client, runtime = await setup(hass, monkeypatch, record)
    client.blocks = before(record) + pressed(1)
    _, facts = answering_by(lambda turn, reply: reply == "Pressed." or turn == 4)
    _, objections = judging_by(lambda turn, reply: reply != "Pressed.")
    monkeypatch.setattr(simulate, "check_facts", facts)
    monkeypatch.setattr(simulate, "check_conduct", objections)
    both = {**ASKED_AGAIN, "goals": [*ASKED_AGAIN["goals"], {
        "turn": 2, "summary": "Not like that", "kind": "conduct", "about": 1, "quote": "Say less", "serves": 1}]}
    test = await run(hass, runtime, measures.goals_of(both))
    assert not client.blocks and test["after"]["said"] == [1] and test["validated"]
    assert test["after"]["goals"]["2:4"] == test["after"]["goals"]["2:1"] == "met"
    [kept] = [r for r in runtime.replays.items.values() if r["plan"]["kind"] == "before"]
    answers = [a for v in kept["verdicts"].values() for found in v["answers"].values() for a in found]
    assert any("shows" in a for a in answers) and any("gives" in a for a in answers)


@pytest.mark.asyncio
async def test_a_filing_under_a_question_answered_on_the_first_reply_isnt_said(hass, monkeypatch, effect_guards):
    """A filing straight after the question (no step between): the judge is asked about the reply so far just before
    it, and once that reply gave the answer, the filing isn't said (DETAILS P6.4)."""
    record = recording()
    _, client, runtime = await setup(hass, monkeypatch, record)
    client.blocks = before(record) + pressed(1)
    asked, check = answering_by(lambda turn, reply: reply == "Pressed." or turn == 4)
    monkeypatch.setattr(simulate, "check_facts", check)
    filed = {**ASKED_AGAIN, "goals": [ASKED_AGAIN["goals"][0], {
        "turn": 2, "summary": "Filed it", "kind": "wrap-up", "serves": 1},
        *({"turn": n, "summary": "Asks again", "kind": "step", "serves": 1} for n in (3, 4))]}
    test = await run(hass, runtime, measures.goals_of(filed))
    assert not client.blocks and test["after"]["said"] == [1] and test["after"]["goals"]["2:1"] == "met"
    assert asked[1] and [q["turn"] for q in asked[1]] == [1]  # Asked just before the filing, about reply 1.


@pytest.mark.asyncio
async def test_a_question_the_baseline_answers_right_first_time_is_set_aside_and_then_nothing_is_left_to_remove(
        hass, monkeypatch, effect_guards):
    """DETAILS P6.6.2: the baseline's first reply gave the answer where the conversation's didn't, so the question can't
    be tested (set aside, with the steps under it); with nothing else the household had trouble with happening again,
    the conversation can't be tested at all (P6.6.5)."""
    record = recording()
    _, client, runtime = await setup(hass, monkeypatch, record)
    client.blocks = before(record) * 3  # Each of the baseline's tries.
    _, check = answering_by(lambda turn, reply: True)
    monkeypatch.setattr(simulate, "check_facts", check)
    with pytest.raises(simulate.Untestable, match="nothing the household had trouble with happened again") as raised:
        await run(hass, runtime, measures.goals_of(ASKED_AGAIN))
    assert raised.value.conversation and len(tries(runtime)) == 3


@pytest.mark.asyncio
async def test_a_judges_verdict_that_couldnt_be_given_after_a_replay_is_asked_again_alone_without_a_new_replay(
        hass, monkeypatch, effect_guards):
    """DETAILS P6.3.5.1: the judge couldn't answer for the replay with the change, so the test is cut off; asked again,
    the judge answers on the kept replay, and the before's verdict is kept too."""
    record = recording()
    _, client, runtime = await setup(hass, monkeypatch, record)
    goals = measures.goals_of(NOTED)
    asked, answers, check = judging(True, None, False)
    monkeypatch.setattr(simulate, "check_conduct", check)
    client.blocks = before(record) + pressed(1, 4)
    with pytest.raises(simulate.Transient, match="objected to"):
        await run(hass, runtime, goals)
    replayed = len(client.requests)
    test = await run(hass, runtime, goals)
    assert len(client.requests) == replayed and not answers  # The judge again, alone; the before's verdict kept.
    assert asked[0][0] == ["Say less"] and asked[2][1] == ["Pressed.", "Pressed."]
    assert test["before"]["goals"]["5:4"] == "unmet" and test["after"]["goals"]["5:4"] == "met"
    assert [a["shows"] for a in test["before"]["conduct"]["5:4"]["answers"]] == [True] * 4
    assert test["after"]["conduct"]["5:4"]["answers"] == [{"turn": 1, "shows": False, "why": "Short."},
                                                          {"turn": 4, "shows": False, "why": "Short."}]
    assert test["judged"]["5:4"]["quote"] == "Say less" and test["judged"]["5:4"]["about"] == "all"


@pytest.mark.asyncio
async def test_a_change_the_replay_cant_run_is_the_ideas_untestable_not_the_conversations(
        hass, monkeypatch, effect_guards):
    """Untestable, it gives the flow no test to keep, so it spends none of the round's tests (DETAILS P6.3.5)."""
    record = recording()
    _, client, runtime = await setup(hass, monkeypatch, record)
    client.blocks = before(record) + [UNFINISHED]
    with pytest.raises(simulate.Untestable, match="didn't finish") as raised:
        await run(hass, runtime)
    assert not raised.value.conversation
    asked = len(client.requests)
    with pytest.raises(simulate.Untestable, match="didn't finish"):  # Settled: not replayed again.
        await run(hass, runtime)
    assert len(client.requests) == asked and effect_guards == []


@pytest.mark.asyncio
async def test_an_idea_that_only_edits_another_assistants_instructions_replays_nothing(hass, monkeypatch):
    record = recording()
    _, client, runtime = await setup(hass, monkeypatch, record)
    other = {"title": "Other", "changes": [{"kind": "assistant", "target": "other-entry/other", "text": "Be brief."}]}
    with pytest.raises(simulate.Untestable, match=simulate.NOT_REACHED) as raised:
        await run(hass, runtime, hypothesis=other)
    assert not raised.value.conversation and not client.requests and not runtime.replays.items


def test_a_recording_made_in_sandbox_mode_cant_be_replayed():
    """Its devices were simulated, so a replay would have nothing to match: it would pass whatever Assist did."""
    record = recording()
    record["practice"] = {"simulated_changes": [], "unsimulated_actions": []}  # As earlier versions left it.
    with pytest.raises(simulate.Untestable, match="sandbox mode") as raised:
        simulate.before_plan(record, recorded_today(record))
    assert raised.value.conversation


def test_readiness_reads_every_turn_and_the_before_plan_doesnt_depend_on_the_goals():
    record = recording()
    readiness = replay_readiness(record)
    assert readiness["model_baseline_ready"], readiness
    today = recorded_today(record)
    plan = simulate.before_plan(record, today)
    assert plan["user_inputs"] == [t["user"]["content"] for t in record["turns"]]
    assert plan["setup"] == simulate.setup_digest(today)
    after = simulate.after_plan(plan, {"exposure": {SCRIPT: True}}, GOALS)
    assert after["before"] == plan["id"] and after["id"] != simulate.after_plan(plan, {}, GOALS)["id"]


def test_a_turn_reads_only_the_words_said_up_to_it():
    record = make_recording()
    turn = recording_turns(record)[0]
    assert [m["content"] for m in turn["history"] if m["role"] == "user"] == ["Toggle the fixture relay."]
    record["turns"][0]["preparations"][0]["content"].append(record["turns"][1]["user"])
    with pytest.raises(CaseError, match="future-contaminated"):
        recording_turns(record)


def test_a_plan_notes_what_the_replay_cant_claim():
    """Hosted features the assistant has on now stay on in its replays, run at the provider (DETAILS P6.2.12)."""
    record = make_recording()
    today = recorded_today(record)
    assert not any("hosted_tools" in n for n in simulate.before_plan(record, today)["notes"])
    today["provider"]["options"]["code_execution"] = True
    assert "hosted_tools_kept_on; provider_execution_is_not_intercepted_by_HA" in simulate.before_plan(
        record, today)["notes"]


@pytest.mark.asyncio
async def test_a_deleted_conversations_replays_go_with_it(hass):
    replays = simulate.Replays(hass)
    await replays.start()
    for rid, plan in (("one", "a"), ("two", "b")):
        await replays.save({"plan": {"id": plan}, "recording_id": rid, "status": "done"})
    assert replays.recordings() == {"one", "two"}
    await replays.drop(["one"])
    assert replays.recordings() == {"two"} and replays.get("a") is None


def test_the_judge_hears_what_a_replays_sandbox_spoke_where_the_scripts_result_says_nothing():
    """A household's broadcast revision: a script spoke the message on every speaker, its own
    result empty, and Assist replied "Sent to the speakers." The judge, asked whether that reply is the unchecked
    claim they objected to, is shown what the replay's call log has spoken and where, beside the tool call; the
    action is met by it (DETAILS P9.10.5)."""
    players = ["media_player.upstairs", "media_player.kitchen"]
    step = {"turn": 1, "input": "Tell Jordan and Riley to come in", "output": [
        {"role": "assistant", "tool_calls": [{"id": "s", "tool_name": "script__announce_on_speakers",
                                              "tool_args": {"message": "Jordan and Riley, please come in."}}]},
        {"role": "tool_result", "tool_call_id": "s", "tool_name": "script__announce_on_speakers", "tool_result": {}},
        {"role": "assistant", "content": "Sent to the speakers."}],
        "actions": [{"action": "script.announce_on_speakers", "data": {"message": "Jordan and Riley, please come in."}},
                    {"action": "tts.speak", "data": {"media_player_entity_id": players, "entity_id": ["tts.speech"],
                                                     "message": "Jordan and Riley, please come in."}, "under": 0}],
        "transitions": [], "changes": []}
    turn = simulate._replayed_turn(step, [{"source": {}}])
    assert turn[3] == {"assistant_replied": "Sent to the speakers."} and turn[4] == {"home_did": {"spoke": [
        {"message": "Jordan and Riley, please come in.", "on": sorted([*players, "tts.speech"])}], "called": [
        'script.announce_on_speakers {"message": "Jordan and Riley, please come in."}']}}  # It names no device.
    assert "what the home did" in simulate.CONDUCT_INSTRUCTIONS and "what the home did" in simulate.FACT_INSTRUCTIONS
    assert simulate._replayed_turn({**step, "actions": []}, [{"source": {}}])[4:] == []  # Nothing done: none.
    home = SimpleNamespace(source={"states": {}, "entities": {}}, sources=[{"states": {}}], changes={},
                           calls={1: []}, actions={1: step["actions"]})
    come_in = {"turn": 1, "summary": "Come in", "kind": "action", "outcome": "announce", "entity_ids": players}
    assert measures.judge(measures.goals_of({"turns": 1, "goals": [come_in]}), home, [1], {})["goals"]["1:0"] == (
        "met")  # Spoken on both speakers, whatever the words.


@pytest.mark.parametrize("satellite", ["idle", "unavailable"])
def test_one_call_log_reads_the_same_recorded_and_replayed(satellite):
    """DETAILS P9.10.5: a script announced dinner to the kitchen area. Read against the recorded turn's home, as
    measures read it, the replay's call log says what the recorded one does: spoken on the kitchen's satellite, or,
    with it unavailable as the turn began, on none, so the judge isn't shown what measures say didn't happen."""
    sat = "assist_satellite.kitchen"
    source = {"states": {sat: {"state": satellite, "attributes": {}}},
              "entities": {sat: {"area_id": "kitchen", "device_id": "d1"}}, "devices": {"d1": {}},
              "areas": {"kitchen": {}}}
    actions = [{"action": "script.say_dinner", "data": {}},
               {"action": "assist_satellite.announce", "data": {"area_id": "kitchen", "message": "Dinner"}, "under": 0}]
    items = [{"role": "assistant", "tool_calls": [{"id": "s", "tool_name": "script__say_dinner", "tool_args": {}}]},
             {"role": "tool_result", "tool_call_id": "s", "tool_name": "script__say_dinner", "result": {}},
             {"role": "assistant", "content": "Done."}]
    recorded = simulate._home_did(items, actions, source, [])
    replayed = simulate._replayed_turn({"turn": 1, "input": "Say dinner", "output": items, "actions": actions,
                                        "changes": []}, [{"source": source}])[4]["home_did"]
    assert replayed == {k: v for k, v in recorded.items() if v}
    assert recorded["spoke"] == ([{"message": "Dinner", "on": [sat]}] if satellite == "idle" else [
        {"message": "Dinner", "on": []}])
    view = SimpleNamespace(source=source, sources=[source], calls={1: []}, actions={1: actions}, changes={})
    goal = {"turn": 1, "summary": "Dinner", "kind": "action", "outcome": "announce", "entity_ids": [sat]}
    assert measures.judge(measures.goals_of({"turns": 1, "goals": [goal]}), view, [1], {})["goals"]["1:0"] == (
        "met" if satellite == "idle" else "unmet")


@pytest.mark.asyncio
async def test_a_device_whose_command_failed_at_home_refuses_it_the_same_way_in_both_replays(hass, monkeypatch,
                                                                                           effect_guards):
    """Turn 1 switched the relay off and the relay refused it at home: in the replay without the change and in the one
    with it, the relay refuses turn 1's command with the recorded error, as at home (DETAILS P11.4), and takes later
    turns' as it did then; the replay without the change goes as the conversation went."""
    record = recording()
    record["turns"][0]["actions"] = [{"action": "switch.turn_off", "data": {"entity_id": TARGET},
                                      "error": "The relay didn't answer"}]
    record["state_changes"] = record["state_changes"][1:]  # The relay stayed on; someone switched it off by turn 2.
    _, client, runtime = await setup(hass, monkeypatch, record)
    client.blocks = before(record) + pressed(1, 2, 3, 4)
    test = await run(hass, runtime)
    assert not client.blocks and effect_guards == []
    assert test["before"]["status"] == "reproduced" and test["after"]["status"] == "done"
    without, with_it = (runtime.replays.items[test[kind]["plan"]]["steps"][0] for kind in ("before", "after"))
    for first in (without, with_it):
        assert [c["service"] for c in first["commands"]] == ["switch.turn_off"]
        assert [t for t in first["transitions"] if t["entity_id"] == TARGET] == []
    assert without["output"][1]["result"]["error"]  # Home Assistant's intent says it failed, as at home.
    assert with_it["output"][1]["tool_result"] == {"error": "HomeAssistantError",
                                                   "error_text": "The relay didn't answer"}


@pytest.mark.asyncio
async def test_an_automation_asking_a_model_agent_in_a_replay_makes_no_model_call_its_ask_an_outside_receipt(
        hass, monkeypatch, effect_guards):
    """The household's automation hands the relay switching off to the household's assistant (conversation.process
    on its model's agent): in a replay that's a model call beyond the turn's own, so it's stood in, answered empty and
    logged as an outside receipt (DETAILS P11.7); the model is asked only the turns' own requests."""
    record = recording()
    for turn in record["turns"]:
        turn["source"]["automations"] = [{"id": "relay_asks", "alias": "Relay asks", "triggers": [
            {"trigger": "state", "entity_id": TARGET, "to": "off"}], "actions": [
            {"action": "conversation.process", "data": {"agent_id": "conversation.fixture", "text": "Lock up"}}]}]
    _, client = await bind_agent(hass, record)
    client.blocks = before(record)
    asked = len(client.blocks)
    found = await (await replay(hass, monkeypatch, record)).run()
    assert found["status"] == "reproduced", found["error"]
    assert found["steps"][0]["outside"] == [{"action": "conversation.process", "answer": "not simulated", "data": {
        "agent_id": "conversation.fixture", "text": "Lock up"}}]
    assert len(client.requests) == asked and not client.blocks and effect_guards == []


@pytest.mark.asyncio
async def test_an_automation_asking_an_ai_task_in_a_replay_makes_no_model_call_and_goes_on(hass, monkeypatch,
                                                                                         effect_guards):
    """The household's automation asks an AI Task each thing it does, then switches the porch on. The assistant's
    provider sets up AI Tasks when it loads in the replay, after the sandbox stood them in: each ask is still a model
    call beyond the turn's own, stood in, answered empty and logged as an outside receipt (DETAILS P11.3), and the
    automation goes on to the porch."""
    from homeassistant.components.ai_task import AITaskService
    asks = [s for s in AITaskService if s != AITaskService.CLEAR_IMAGES]
    record = recording()
    for turn in record["turns"]:
        turn["source"]["automations"] = [{"id": "relay_asks", "alias": "Relay asks", "triggers": [
            {"trigger": "state", "entity_id": TARGET, "to": "off"}], "actions": [
            *({"action": f"ai_task.{s}", "data": {"task_name": "Ask", "instructions": "Say hi"},
               "response_variable": "answer"} for s in asks),
            {"action": "switch.turn_on", "target": {"entity_id": PORCH}}]}]
    _, client = await bind_agent(hass, record)
    client.blocks = before(record)
    asked = len(client.blocks)
    found = await (await replay(hass, monkeypatch, record)).run()
    assert found["status"] == "reproduced", found["error"]
    first = found["steps"][0]
    assert [o["action"] for o in first["outside"]] == [f"ai_task.{s}" for s in asks]
    assert {o["answer"] for o in first["outside"]} == {"not simulated"}
    assert [c["entity_id"] for c in first["commands"] if c["service"] == "switch.turn_on"] == [PORCH]
    assert len(client.requests) == asked and not client.blocks and effect_guards == []
