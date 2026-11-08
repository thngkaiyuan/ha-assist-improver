"""The bug hunt's findings on the simulate stage and replay fidelity, fixed: each test names the behaviour Kai agreed
to. All inference is scripted; replays run in the inline sandbox (offline), the timed automations' in a replay
worker."""

import asyncio
import json
import subprocess

import pytest
import pytest_asyncio

from conftest import assist_home
from kit import press_script
from recordings import (PROFILE, REPLAY_ENTITIES, TARGET, at_home, bind_agent, make_recording, prepared_recording,
                        recorded_today, replay)
from custom_components.assist_improver import worker as worker_module
from custom_components.assist_improver.simulate import Trial, after_plan, planned, setup_for

POPEN = subprocess.Popen  # Before the effect guards: one test starts a replay worker for real (test_worker.py).
PRESS = {"title": "Press the relay with one script", "cause": "No press tool", "expected": "One turn",
         "support": [], "conflicts": [], "goals": [{"session": "synthetic-recording", "goal_turn": 1}],
         "changes": [press_script()]}


@pytest_asyncio.fixture
async def hass(hass):
    """The offline HA with HA's real Assist tools; replays run them on a sandboxed copy of the recording."""
    return await assist_home(hass, REPLAY_ENTITIES)


async def before(hass, monkeypatch, record, messages, through=None):
    """The replay without a change, through the user's OpenRouter assistant answering with these messages (through
    that turn)."""
    from test_openrouter_replay import bind_openrouter
    await bind_openrouter(hass, record, messages)
    return await (await replay(hass, monkeypatch, record, through=through)).run()


# The replay without a change judges the devices the recorded Assist changed; the model's other variations are notes.

@pytest.mark.asyncio
async def test_a_real_homes_replay_varying_only_elsewhere_still_reproduces_with_a_note(hass, monkeypatch,
                                                                                       effect_guards):
    """docs/DESIGN.md, "The baseline comes first": the replay without a change reproduces when, every turn, the
    devices Assist changed behave as recorded; a model's other variations are kept as notes. Kai, 2026-10-23: the
    interactions don't have to be exactly the same."""
    from test_openrouter_replay import WORDED, call, openrouter_recording, reply
    record = openrouter_recording()  # A conversation in the real home: its snapshots are the real states.
    for turn in record["turns"]:
        turn["source"]["states"]["switch.porch"] = {"entity_id": "switch.porch", "state": "off",
                                                    "attributes": {"friendly_name": "Porch"}}
        turn["source"]["exposure"]["switch.porch"] = True
    result = await before(hass, monkeypatch, record, [WORDED[0], call("intent__HassTurnOn", {"name": "Porch"}, "c2"),
                                         reply("Done, and the porch light is on."), *WORDED[2:]])
    assert result["status"] == "reproduced", result["error"]
    assert result["steps"][0]["differences_elsewhere"] == {"switch.porch": {"replay": [["off", "on"]], "recorded": []}}
    assert effect_guards == []


# A replay of the command Assist made reproduces on a device that reports its movement (a garage door, a lock).

GARAGE = "cover.garage_door"


def garage_recording():
    """"Close the garage door." in the real home: Assist closed it with HA's own intent, and HA recorded what the door
    reported while the turn lasted, "closing" (it reports "closed" once it's down, after the reply)."""
    from test_openrouter_replay import openrouter_recording
    record = json.loads(json.dumps(openrouter_recording()).replace(TARGET, GARAGE).replace("Fixture relay", "Garage door")
                        .replace('"domain": ["switch"]', '"domain": ["cover"]'))
    for turn in record["turns"]:
        turn["source"]["states"][GARAGE].update(state="open", attributes={
            "friendly_name": "Garage door", "device_class": "garage", "supported_features": 3})
    first = record["state_changes"][0]
    first["old"]["state"], first["new"]["state"] = "open", "closing"
    return record


@pytest.mark.asyncio
async def test_the_same_command_on_a_door_that_reports_its_movement_reproduces(hass, monkeypatch, effect_guards):
    """Kai, 2026-10-26: simulation accuracy comes first, replays match what Assist saw and did; DESIGN.md, "The baseline
    comes first": the baseline reproduces when the devices the assistant changed behave as recorded, and DESIGN.md: only
    the devices are emulated, the rest is HA's own engine. The replay makes the very call Assist made and HA closes the
    same door."""
    from test_openrouter_replay import call, reply
    result = await before(hass, monkeypatch, garage_recording(), [call("intent__HassTurnOff", {"name": "Garage door", "domain": [
        "cover"]}, "c1"), reply("Closing the garage door.")], through=1)
    [step] = result["steps"]
    assert step["transitions"] and step["transitions"][0]["new"] in ("closing", "closed")  # HA closed the door.
    assert result["status"] == "reproduced", (step["transitions"], step["recorded_transitions"])
    assert effect_guards == []


@pytest.mark.asyncio
async def test_a_household_script_that_waits_for_the_door_to_close_finishes_in_a_replay(hass, effect_guards):
    """A device that reports its movement still gets there within the call, as HA's own device would, its in-between
    report kept: a household script that closes the garage and waits until it's closed (which Assist's script tool
    waits for) finishes, and the door reports closing, then closed."""
    from custom_components.assist_improver.cases import recording_turns
    from custom_components.assist_improver.replay import SandboxHome
    record = garage_recording()
    scripts = {"close_the_garage": {"alias": "Close the garage", "sequence": [
        {"action": "cover.close_cover", "target": {"entity_id": GARAGE}},
        {"wait_template": "{{ is_state('" + GARAGE + "', 'closed') }}"}]}}
    turn = recording_turns(record)[0]
    home = SandboxHome(hass, record, turn, "garage-script", recorded_today(record, scripts=scripts, automations=[]),
                       change={"exposure": {"script.close_the_garage": True}})
    try:
        assert "script__close_the_garage" in [t["name"] for t in (await home.assist(turn))["tools"]]
        await asyncio.wait_for(home.sandbox.call("tool", tool_name="script__close_the_garage", tool_args={}), 10)
        world = await home.snapshot()
    finally:
        await home.stop()
    assert [(t["old"], t["new"]) for t in world["transitions"] if t["entity_id"] == GARAGE] == [
        ("open", "closing"), ("closing", "closed")]
    assert effect_guards == []


@pytest.mark.asyncio
async def test_what_the_door_getting_there_sets_off_is_the_replays_own(hass, monkeypatch, effect_guards):
    """Whatever a device's movement causes is recorded as the replay's: the household's automation that turns the garage
    light off once the door is closed changes the light in the replay's own transitions, as the household's automation
    did in the conversation."""
    from test_openrouter_replay import call, reply
    light = "switch.garage_light"
    record = garage_recording()
    for turn in record["turns"]:
        turn["source"]["states"][light] = {"entity_id": light, "state": "on", "attributes": {"friendly_name": "Light"}}
        turn["source"]["scripts"] = {}
        turn["source"]["automations"] = [{"id": "garage_light", "alias": "Garage light off once it's closed",
                                          "triggers": [{"trigger": "state", "entity_id": GARAGE, "to": "closed"}],
                                          "actions": [{"action": "switch.turn_off", "target": {"entity_id": light}}]}]
    result = await before(hass, monkeypatch, record, [
        call("intent__HassTurnOff", {"name": "Garage door", "domain": ["cover"]}, "c1"),
        reply("Closing the garage door.")], through=1)
    [step] = result["steps"]
    assert result["status"] == "reproduced", result["error"]
    assert {"entity_id": light, "old": "on", "new": "off"} in step["transitions"], step["transitions"]
    assert effect_guards == []


@pytest.mark.asyncio
async def test_a_replay_goes_on_when_the_next_turn_began_while_the_door_was_still_moving(hass, monkeypatch,
                                                                                         effect_guards):
    """The user spoke again while the garage door was still closing, so the next turn's snapshot has it "closing"; the
    replay's door has got there ("closed"). That's the device's travel time, not a different effect: the replay goes
    on to the next turn, as setting_off compares a turn."""
    from test_openrouter_replay import call, reply
    record = garage_recording()
    record["turns"][1]["source"]["states"][GARAGE]["state"] = "closing"
    result = await before(hass, monkeypatch, record, [
        call("intent__HassTurnOff", {"name": "Garage door", "domain": ["cover"]}, "c1"),
        reply("Closing the garage door."), reply("No script.")], through=2)
    assert result["status"] == "reproduced", result["error"]
    assert [s["turn"] for s in result["steps"]] == [1, 2]
    assert effect_guards == []


# A replay shows the model the instructions as Home Assistant renders them at the conversation's time, not the replay's.

@pytest.mark.asyncio
async def test_a_replay_renders_the_instructions_as_of_the_conversations_time(hass, monkeypatch, effect_guards):
    """Kai, 2026-10-26: replays must match what Assist actually saw; 2026-10-26: the date and time stay in
    the recommended facts (instructions.RECOMMENDED date_time) the proposer adds to an assistant's instructions. Once
    an assistant's instructions say the time, a fix that shows in them (here a household line) must still be testable
    on that assistant's conversations, and its model must read the time the conversation happened."""
    from test_assistant_instructions import one_turn
    from custom_components.assist_improver.instructions import FACTS
    template = "You are a voice assistant for Home Assistant.\n" + FACTS["date_time"].line + "\n"
    record = prepared_recording()
    then = "It's Thursday 1 January 2026, 00:00."  # When the conversation happened (UTC).
    hypothesis = {**PRESS, "title": "Say how a relay is pressed", "changes": [
        {"kind": "preference", "text": "A relay is pressed off, then on.", "for": "everyone"}]}
    agent, client = await bind_agent(hass, record, {**PROFILE, "prompt": template})
    at_home(monkeypatch, record)
    before, today = await planned(hass, record)
    setup, _, _ = setup_for(hypothesis, record, today, hass)
    client.blocks = one_turn() + [[{"type": "text", "text": "Ok."}]] * 3
    result = await Trial(hass, after_plan(before, setup, []), record, today, setup=setup).run()
    assert result["status"] != "unsupported", result["error"]
    assert then in client.requests[0]["system"][0]["text"]  # The model read the conversation's time.
    assert result["status"] == "done", result["error"]
    assert effect_guards == []


@pytest.mark.asyncio
async def test_assists_date_and_time_tool_answers_with_the_conversations_time_in_a_replay(hass, monkeypatch,
                                                                                          effect_guards):
    """Kai, 2026-10-26: replays must match what Assist actually saw, its tools' answers included. Home
    Assistant's Assist offers llm__GetDateTime (it then leaves the date and time out of the instructions); in a replay
    of a conversation from 1 January 2026 it must say 1 January 2026, not the day the replay runs."""
    record = prepared_recording()
    _, client = await bind_agent(hass, record)
    client.blocks = [[{"type": "tool_use", "id": "when", "name": "llm__GetDateTime", "input": {}}],
                     [{"type": "text", "text": "It's New Year's Day."}]]
    found = await (await replay(hass, monkeypatch, record, through=1)).run()
    [answered] = [m for m in found["steps"][0]["output"] if m["role"] == "tool_result"]
    date = answered["tool_result"].get("date")
    assert date == "2026-01-01", answered  # make_recording's turn 1: 1 January 2026, UTC.
    assert effect_guards == []


@pytest.mark.asyncio
async def test_the_households_timed_automations_dont_fire_in_a_replay_of_an_earlier_conversation(hass, monkeypatch):
    """One clock (Kai, 2026-11-05): a replay of a conversation from 1 January 2026 in the replay worker, as on a real
    install, runs on that day's time, the scheduler too: an automation on a time of day, a time pattern or sunset, none
    due for hours, fires nothing (frozen, every one was due at once and fired in a loop)."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    from custom_components.assist_improver.cases import recording_turns
    from custom_components.assist_improver.replay import SandboxHome
    monkeypatch.setattr(subprocess, "Popen", POPEN)
    monkeypatch.setattr(worker_module, "INLINE", False)
    record = make_recording()
    then = datetime.fromisoformat(record["turns"][0]["source"]["at"])
    later = then.astimezone(ZoneInfo(hass.config.time_zone)) + timedelta(hours=12)  # The sandbox's zone: the home's.
    press = [{"action": "switch.toggle", "target": {"entity_id": TARGET}}]
    automations = [{"id": kind, "alias": kind, "triggers": [trigger], "actions": press} for kind, trigger in (
        ("time", {"trigger": "time", "at": later.strftime("%H:%M:%S")}),
        ("pattern", {"trigger": "time_pattern", "hours": later.hour, "minutes": 0, "seconds": 0}),
        ("sunset", {"trigger": "sun", "event": "sunset"}))]
    home = SandboxHome(hass, record, recording_turns(record)[0], "timed-automations",
                       recorded_today(record, scripts={}, automations=automations))
    try:
        await home.start()
        await asyncio.sleep(1)
        world = await home.snapshot()
        assert world["commands"] == [] and world["transitions"] == [], world
    finally:
        await home.stop()
        await worker_module.shutdown(hass)


@pytest.mark.asyncio
async def test_a_script_assist_runs_and_the_automation_it_sets_off_read_the_conversations_time(hass, effect_guards):
    """What Assist sets off runs as of the conversation, as Assist's own date and time tool does. A household script
    Assist runs picks its branch by the time (here: within the hour of the conversation, 1 January 2026), and so does
    the automation the script's change triggers: in a replay months later both take the conversation's branch."""
    from custom_components.assist_improver.cases import recording_turns
    from custom_components.assist_improver.replay import SandboxHome
    then = "{{ (now() - as_datetime('2026-01-01T00:00:02+00:00')).total_seconds() | abs < 3600 }}"
    porch = "switch.porch"
    scripts = {"night_mode": {"alias": "Night mode", "sequence": [{"choose": [
        {"conditions": [{"condition": "template", "value_template": then}],
         "sequence": [{"action": "switch.turn_off", "target": {"entity_id": TARGET}}]}],
        "default": [{"action": "switch.turn_on", "target": {"entity_id": porch}}]}]}}
    automations = [{"id": "after_the_relay", "alias": "Porch on after the relay, that night",
                    "triggers": [{"trigger": "state", "entity_id": TARGET, "to": "off"}],
                    "conditions": [{"condition": "template", "value_template": then}],
                    "actions": [{"action": "switch.turn_on", "target": {"entity_id": porch}}]}]
    record = make_recording()
    for turn in record["turns"]:
        turn["source"]["states"][porch] = {"entity_id": porch, "state": "off", "attributes": {"friendly_name": "Porch"}}
    turn = recording_turns(record)[0]
    home = SandboxHome(hass, record, turn, "timed-script", recorded_today(record, scripts=scripts,
                                                                           automations=automations),
                       change={"exposure": {"script.night_mode": True}})
    try:
        await home.assist(turn)
        await home.sandbox.call("tool", tool_name="script__night_mode", tool_args={})
        world = await home.snapshot()
    finally:
        await home.stop()
    devices = [t for t in world["transitions"] if t["entity_id"] in (TARGET, porch)]  # Not the script's own.
    assert devices == [{"entity_id": TARGET, "old": "on", "new": "off"},
                       {"entity_id": porch, "old": "off", "new": "on"}], world["transitions"]
    assert effect_guards == []
