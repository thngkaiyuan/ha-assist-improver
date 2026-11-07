"""Replays without and with a change through HA's real OpenRouter assistant; the model transport is scripted.

The user's own OpenRouter assistant runs each replay as it's set up now, loaded in the replay's sandbox; its settings
at home are never changed, and the home's live states never reach the model. The effect guards fail on any HA service
call, network access or DNS lookup.
"""

from copy import deepcopy
import json

import pytest

from recordings import REPLAY_ENTITIES, TARGET, at_home, bind_agent, make_recording, recorded_today, replay
from custom_components.assist_improver.cases import recording_turns, replay_readiness
from custom_components.assist_improver.simulate import Trial, after_plan, before_plan, planned, setup_for
import pytest_asyncio
from conftest import assist_home

PROFILE = {"model": "fixture/recorded-model", "prompt": "Recorded template", "llm_hass_api": ["assist"],
           "web_search": "off"}

@pytest_asyncio.fixture
async def hass(hass):
    """The offline HA with HA's real Assist tools; replays run them on a simulated copy of the recording."""
    return await assist_home(hass, REPLAY_ENTITIES)


def openrouter_recording(platform="open_router", options=PROFILE, scripts=None):
    record = make_recording()
    record["turns"][0]["source"].update(scripts=scripts or {}, automations=[])  # As the recorder keeps them.
    for turn in record["turns"]:
        turn["prepared"]["context"]["platform"] = platform
        turn["prepared"]["provider_config"] = [{"config_entry_id": "fixture-entry",
            "subentry_id": "fixture-openrouter", "options": deepcopy(options)}]
    return record


async def bind_openrouter(hass, record, messages=()):
    """The recording's OpenRouter assistant as the household has it today (recordings.bind_agent), its model the
    scripted provider answering with these messages, in order."""
    agent, client = await bind_agent(hass, record)
    client.blocks = list(messages)
    return agent, client


def call(name, args, call_id):
    return {"role": "assistant", "content": None, "tool_calls": [
        {"type": "function", "id": call_id, "function": {"name": name, "arguments": json.dumps(args)}}]}


def reply(text):
    return {"role": "assistant", "content": text}


def scripted(record):
    """The recorded decisions, replayed as provider responses: each recorded message's tool calls together, as the
    model made them."""
    messages = []
    for turn in recording_turns(record):
        for message in turn["messages"]:
            if message["role"] == "assistant" and (calls := [c for c in message.get("tool_calls") or []
                                                              if not c.get("external")]):
                messages.append({"role": "assistant", "content": None, "tool_calls": [
                    call(c["tool_name"], c["tool_args"], c["id"])["tool_calls"][0] for c in calls]})
        messages.append(reply(f"Generated response {turn['number']}."))
    return messages


@pytest.mark.asyncio
async def test_the_replay_without_a_change_reproduces_through_the_users_openrouter_assistant(hass, monkeypatch,
                                                                                             effect_guards):
    record = openrouter_recording()
    assert replay_readiness(record)["model_baseline_ready"]
    agent, transport = await bind_openrouter(hass, record, scripted(record))
    original, live_options = deepcopy(record), deepcopy(dict(agent.entry.subentries["fixture-openrouter"].data))
    hass.states.async_set(TARGET, "LIVE_VALUE")
    trial = await replay(hass, monkeypatch, record)
    assert trial.plan["platform"] == "open_router" and trial.plan["provider_tested"]
    result = await trial.run()
    assert result["status"] == "reproduced", result  # Scripted decisions, not model evidence.
    assert [s["turn"] for s in result["steps"]] == [1, 2, 3, 4]
    assert transport.requests and not transport.blocks
    for request in transport.requests:
        assert request["model"] == PROFILE["model"]
        assert "Recorded template" in request["messages"][0]["content"]  # Its instructions, in what HA builds now.
    assert "LIVE_VALUE" not in json.dumps(transport.requests)
    assert dict(agent.entry.subentries["fixture-openrouter"].data) == live_options
    assert [t["new"] for s in result["steps"] for t in s["transitions"]] == ["off", "on", "off", "on"]
    assert hass.states.get(TARGET).state == "LIVE_VALUE" and effect_guards == []
    assert record == original


@pytest.mark.asyncio
async def test_the_replay_with_the_change_runs_through_the_users_openrouter_assistant(hass, monkeypatch,
                                                                                      effect_guards):
    from test_simulate import EXPOSE, PRESS_SCRIPT
    record = openrouter_recording(scripts={"fixture_press": PRESS_SCRIPT})
    agent, transport = await bind_openrouter(hass, record, [call("script__fixture_press", {}, "script-1"),
                                                            reply("Fixture press completed.")] + [reply("Ok.")] * 3)
    hass.states.async_set(TARGET, "LIVE_VALUE")
    at_home(monkeypatch, record)
    before, today = await planned(hass, record)
    setup, _, _ = setup_for(EXPOSE, record, today, hass)
    result = await Trial(hass, after_plan(before, setup, []), record, today, setup=setup).run()
    assert result["status"] == "done", result
    pressed = [t["new"] for t in result["steps"][0]["transitions"] if t["entity_id"] == TARGET]
    assert pressed == ["off", "on"] and len(transport.requests) == 5
    for request in transport.requests:
        assert request["model"] == PROFILE["model"]
        assert "script__fixture_press" in {t["function"]["name"] for t in request["tools"] if "function" in t}
    assert "LIVE_VALUE" not in json.dumps(transport.requests)
    assert hass.states.get(TARGET).state == "LIVE_VALUE" and effect_guards == []


def test_untested_providers_replay_labelled_and_hosted_search_is_noted():
    record = openrouter_recording(platform="fixture_provider")
    assert replay_readiness(record)["model_baseline_ready"]
    plan = before_plan(record, recorded_today(record))
    assert "provider_not_yet_tested_for_replay" in plan["notes"] and not plan["provider_tested"]
    assert not any("hosted_tools" in n for n in plan["notes"])  # web_search "off" is no hosted tool.
    record = openrouter_recording(options={**PROFILE, "web_search": "tool"})
    assert any("hosted_tools" in n for n in before_plan(record, recorded_today(record))["notes"])


async def before(hass, monkeypatch, record, messages):
    """The replay without a change, through the user's OpenRouter assistant answering with these messages."""
    agent, transport = await bind_openrouter(hass, record, messages)
    return await (await replay(hass, monkeypatch, record)).run(), transport


# The model words its calls differently from the recording (by name, not with its domain): the same effects on the
# home still match.
WORDED = [call("intent__HassTurnOff", {"name": "Fixture relay"}, "c1"), reply("Done."), reply("No script."),
          call("intent__HassTurnOn", {"name": "Fixture relay"}, "c3"), reply("Back on."),
          call("intent__HassTurnOff", {"name": "Fixture relay"}, "c4"),
          call("intent__HassTurnOn", {"name": "Fixture relay"}, "c5"), reply("Off and on again.")]


@pytest.mark.asyncio
async def test_a_replay_is_judged_by_its_effects_however_the_model_words_its_calls(hass, monkeypatch, effect_guards):
    result, _ = await before(hass, monkeypatch, openrouter_recording(), WORDED)
    assert result["status"] == "reproduced", result
    assert [s["matched"] for s in result["steps"]] == [True, True, True, True] and effect_guards == []


@pytest.mark.asyncio
async def test_different_effects_are_not_a_reproduction(hass, monkeypatch, effect_guards):
    record = openrouter_recording()
    result, transport = await before(hass, monkeypatch, record, [reply("I can't do that.")])  # Turn 1 changed nothing.
    assert result["status"] == "not_reproducible" and not result["steps"][-1]["matched"]
    assert len(result["steps"]) == 1 and len(transport.requests) == 1  # Stopped there.


@pytest.mark.asyncio
async def test_a_replay_varying_only_elsewhere_still_reproduces_with_a_note(hass, monkeypatch, effect_guards):
    hass.states.async_set("light.porch", "off")  # Not in the recording: the replay can't touch it.
    record = openrouter_recording()
    record["turns"][0]["source"]["states"]["switch.porch"] = {"state": "off", "attributes": {"friendly_name": "Porch"}}
    record["turns"][0]["source"]["exposure"]["switch.porch"] = True
    messages = [WORDED[0], call("intent__HassTurnOn", {"name": "Porch"}, "c2"), reply("Done, and the porch light is on."),
                *WORDED[2:]]
    result, _ = await before(hass, monkeypatch, record, messages)
    step = result["steps"][0]
    assert result["status"] == "reproduced" and step["matched"], result
    assert step["differences_elsewhere"] == {"switch.porch": {"replay": [["off", "on"]], "recorded": []}}
    assert effect_guards == []
