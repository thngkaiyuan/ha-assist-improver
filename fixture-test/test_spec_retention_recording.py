"""Testable specs: recording and retention.

What a recording captures per turn, how it closes and is saved, how a closed one is packed losslessly, how older ones
are compacted (keeping the conversation and the devices it involved) and still read by every role but never
replayed, which ones stay in full, how the run log is tidied, and what the status and export_recording actions return.
Sources: DETAILS.md (P8. Recording, intake and privacy), INSTALL.md (Inspect recordings, Storage and lifecycle),
strings.json, and the docstrings of recorder.py, snapshot.py, serialization.py and retention.py.
"""

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
import json
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock

import openai
import probatio
import pytest
import pytest_asyncio
from homeassistant.components import ai_task
from homeassistant.components.conversation import ConversationInput
from homeassistant.components.conversation import chat_log as ha_chat_log
from homeassistant.components.conversation.chat_log import AssistantContent, ToolResultContent
from homeassistant.components.homeassistant.exposed_entities import async_expose_entity
from homeassistant.components.open_router.ai_task import OpenRouterAITaskEntity
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import Context, ServiceCall, State
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError, Unauthorized
from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er, llm
from homeassistant.helpers.chat_session import DATA_CHAT_SESSION, async_get_chat_session
from homeassistant.helpers.storage import Store
from homeassistant.util.dt import utcnow

from conftest import assist_home, load_registries, loaded_for, translated
from ha_task import stand_in
from recordings import TARGET, make_recording
from test_exploration import Transport
from test_milestone1 import fake_entry, recording_hass  # noqa: F401 (fixture)
from test_retention import with_home
from kit import seat
from custom_components.assist_improver import async_setup_entry, async_unload_entry, hypotheses, retention, triage
from custom_components.assist_improver import recorder as recorder_module, snapshot as snapshot_module
from custom_components.assist_improver.cases import CaseError, recording_turns, replayable_turns
from custom_components.assist_improver.const import DOMAIN
from custom_components.assist_improver.exploration import SessionView
from custom_components.assist_improver.recorder import Recorder
from custom_components.assist_improver.reflective import RoleRuns, run_role
from custom_components.assist_improver.serialization import plain
from custom_components.assist_improver.snapshot import (provider_config, snapshot,
                                                        state_record)

LIGHT, SENSOR, LOCK = "light.kitchen", "sensor.garden", "lock.back_door"


# --- Helpers ---------------------------------------------------------------------------------------------------------

@pytest_asyncio.fixture
async def listening(hass):
    """A recorder listening in an offline HA with Assist's real chat log, registries, exposure, an area and a device."""
    await assist_home(hass, {})
    kitchen = ar.async_get(hass).async_create("Kitchen", aliases={"Cooking"})
    provider = ConfigEntry(domain="fixture_provider", title="Lamps", data={"api_key": "DO_NOT_RECORD_KEY"},
                           source="user", version=1, minor_version=1, options={}, unique_id=None,
                           discovery_keys=MappingProxyType({}), state=ConfigEntryState.LOADED, subentries_data=[])
    hass.config_entries._entries[provider.entry_id] = provider
    lamp = dr.async_get(hass).async_get_or_create(
        config_entry_id=provider.entry_id, identifiers={("fixture", "lamp-1")}, name="Kitchen lamp",
        manufacturer="Fixture Co", model="L1", serial_number="SERIAL-DO-NOT-RECORD",
        connections={(dr.CONNECTION_NETWORK_MAC, "aa:bb:cc:dd:ee:ff")})
    dr.async_get(hass).async_update_device(lamp.id, area_id=kitchen.id)
    registry = er.async_get(hass)
    registry.async_get_or_create("light", "fixture", "kitchen", suggested_object_id="kitchen", device_id=lamp.id)
    registry.async_update_entity(LIGHT, name="Kitchen light", area_id=kitchen.id, aliases=["Big light"])
    hass.states.async_set(LIGHT, "off", {"friendly_name": "Kitchen light", "brightness": None,
                                         "entity_picture": "/local/lamp.png"})
    hass.states.async_set(SENSOR, "12", {"friendly_name": "Garden temperature", "unit_of_measurement": "°C"})
    async_expose_entity(hass, "conversation", LIGHT, True)
    async_expose_entity(hass, "conversation", SENSOR, False)
    await hass.async_block_till_done()
    recorder = Recorder(hass)
    await recorder.start()
    recorder.area, recorder.lamp = kitchen, lamp
    try:
        yield recorder
    finally:
        await recorder.stop()


async def say(hass, conversation_id, text, reply, *, during=None, native=None, api=None):
    """One Assist turn through Home Assistant's own chat log: the user's words, the prompt prepared (with this
    assistant API, by default none), then the reply (with the provider's own copy of it, `native`, if given).
    `during` runs while the assistant is working on it."""
    context = Context(user_id="alex")
    user_input = ConversationInput(text=text, context=context, conversation_id=conversation_id,
                                   device_id="fixture-phone", satellite_id=None, language="en",
                                   agent_id="conversation.fixture")
    with (async_get_chat_session(hass, conversation_id, context=context) as session,
          ha_chat_log.async_get_chat_log(hass, session, user_input) as log):
        await log.async_provide_llm_data(user_input.as_llm_context("fixture"), api)
        if during:
            during()
        log.async_add_assistant_content_without_tools(AssistantContent(agent_id="conversation.fixture", content=reply,
                                                                       native=native))
    await hass.async_block_till_done()
    return session.conversation_id


def as_in_2026_12(on_chat):
    """A recorder's chat log events as Home Assistant 2026.12 sends them (core 4ecbfa925): a new chat log comes with
    the user's first words already in its created event, and no content_added for them. Home Assistant 2026.11 sends
    the created event, then at once the content_added of those words: the created event waits for it."""
    held = {}

    def reshaped(conversation_id, kind, data):
        if (created := held.pop(conversation_id, None)) is not None:
            content = data.get("content") or {}
            if str(kind) == "content_added" and content.get("role") == "user":
                log = created["chat_log"]
                on_chat(conversation_id, "created", {**created, "chat_log": {**log, "content": [*log["content"],
                                                                                                content]}})
                return
            on_chat(conversation_id, "created", created)
        if str(kind) == "created":
            held[conversation_id] = data
        else:
            on_chat(conversation_id, kind, data)
    return reshaped


def close(hass, conversation_id):
    """Home Assistant ends the conversation (its chat session is cleaned up)."""
    hass.data[DATA_CHAT_SESSION][conversation_id].async_cleanup()


def only_key(recorder):
    assert len(recorder.index) == 1, recorder.index
    return next(iter(recorder.index))


def recording(rid, started, *, turns=True, status="session_expired"):
    """A finished conversation as the recorder stores it (the fixture conversation, under another ID)."""
    record = {**make_recording(), "id": rid, "started": started, "status": status}
    if not turns:
        record.update(turns=[], state_changes=[])
    return record


async def arrive(recorder, record):
    """A conversation the recorder kept: its file and its index entry."""
    await recorder.store(record["id"]).async_save(record)
    recorder.index[record["id"]] = {"id": record["id"], "started": record["started"], "status": record["status"],
                                    "user_turns": len(record["turns"])}


async def newest(recorder, now, count=retention.KEEP_NEWEST, days=1):
    """`count` conversations from the last few days: the newest the tidy-up keeps in full."""
    for n in range(count):
        await arrive(recorder, recording(f"recent-{n:02d}", (now - timedelta(days=days, minutes=n)).isoformat()))


async def stored(recorder, key):
    """A recording's file as it is on disk."""
    return await recorder.store(key).async_load()


async def service(hass, name, data=None, user_id=None):
    """Call one of the integration's actions as Home Assistant would (an administrator's, or a visitor's, call)."""
    registered = hass.services.async_services()[DOMAIN][name]
    call = ServiceCall(hass, DOMAIN, name, registered.schema(data or {}), context=Context(user_id=user_id),
                       return_response=True)
    return await registered.job.target(call)


def ha_tool_result(failed=(), success=()):
    """An Assist tool result as Home Assistant 2026.10's chat log writes it (ToolResultContent.as_dict)."""
    data = {"speech": {}, "response_type": "action_done", "data": {
        "targets": [], "success": [{"name": n, "type": "entity", "id": e} for e, n in success],
        "failed": [{"name": n, "type": "entity", "id": e} for e, n in failed]}}
    return plain(ToolResultContent(agent_id="conversation.fixture", tool_call_id="call-lock", tool_name="HassTurnOff",
                                   result=llm.ToolResult(data=data)).as_dict())


# --- What a recording captures per turn ------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_conversation_is_recorded_as_it_happens_not_when_home_assistant_closes_it(listening, effect_guards):
    hass = listening.hass
    await say(hass, "kitchen-chat", "Is the kitchen light on?", "No, it's off.")
    key = only_key(listening)
    await listening.flush()
    on_disk = await stored(listening, key)
    assert on_disk["status"] == "active" and on_disk["conversation_id"] == "kitchen-chat"
    # HA leaves out the final assistant notification for a new log: the recorder reads the log itself.
    assert [(m["role"], m.get("content")) for m in on_disk["chat_log"]["content"][1:]] == [
        ("user", "Is the kitchen light on?"), ("assistant", "No, it's off.")]
    assert (await listening.export(key))["turns"][0]["user"]["content"] == "Is the kitchen light on?"
    assert effect_guards == []


@pytest.mark.asyncio
async def test_a_conversation_is_recorded_the_same_when_its_first_words_come_with_its_chat_log(
        listening, monkeypatch, effect_guards):
    """Home Assistant 2026.12 opens a conversation's chat log with the user's first words in it and says nothing more
    of them; 2026.11 opens it empty, then adds them. Heard both ways at once, the conversation is recorded the same:
    its first turn starts as the words come, with the home as it was before Assist acted, and nothing twice."""
    hass = listening.hass
    clock = utcnow()  # One clock for both recordings.
    for module in (recorder_module, snapshot_module):
        monkeypatch.setattr(module, "utcnow", lambda: clock)
    newer = Recorder(hass)
    newer.on_chat = as_in_2026_12(newer.on_chat)
    await newer.start()
    try:
        await say(hass, "kitchen-chat", "Is the kitchen light on?", "Now it is.", api="assist",
                  during=lambda: hass.states.async_set(LIGHT, "on", {"friendly_name": "Kitchen light"}))
        await say(hass, "kitchen-chat", "Turn it off.", "Done.", api="assist")
        older, recorded = [await r.export(only_key(r)) for r in (listening, newer)]
    finally:
        await newer.stop()
    assert [e["type"] for e in older["events"][:2]] == ["created", "content_added"]  # As 2026.11 sends them.
    assert [t["user"]["content"] for t in recorded["turns"]] == ["Is the kitchen light on?", "Turn it off."]
    assert [t["source"]["states"][LIGHT]["state"] for t in recorded["turns"]] == ["off", "on"]
    assert "prepared" in recorded["turns"][0] and recorded["id"] != older["id"]
    assert {**recorded, "id": None} == {**older, "id": None}
    assert effect_guards == []


@pytest.mark.asyncio
async def test_a_turn_home_assistant_answers_itself_is_recorded_too(listening, effect_guards):
    """P8.1, P8.3.5: a request Home Assistant handles itself (a local intent, without the AI model) goes through the
    same common chat log, so it's one of the household's Assist conversations and is recorded (such
    as "Turn off the kitchen light"); only what bypasses that chat log can't be."""
    hass = listening.hass
    user_input = ConversationInput(text="Turn off the kitchen light.", context=Context(user_id="alex"),
                                   conversation_id="local-chat", device_id="fixture-phone", satellite_id=None,
                                   language="en", agent_id="conversation.home_assistant")
    with (async_get_chat_session(hass, "local-chat", context=user_input.context) as session,
          ha_chat_log.async_get_chat_log(hass, session, user_input) as log):
        log.async_add_assistant_content_without_tools(AssistantContent(agent_id="conversation.home_assistant",
                                                                       content="Turned off the light."))
    await hass.async_block_till_done()
    recording = await listening.export(only_key(listening))
    assert recording["conversation_id"] == "local-chat"
    assert [t["user"]["content"] for t in recording["turns"]] == ["Turn off the kitchen light."]
    assert effect_guards == []


@pytest.mark.asyncio
async def test_each_turn_records_the_users_words_and_the_home_as_it_was_then(listening, effect_guards):
    hass = listening.hass
    await say(hass, "kitchen-chat", "Is the kitchen light on?", "No, it's off.")
    await say(hass, "kitchen-chat", "Turn it on.", "Done.",
              during=lambda: hass.states.async_set(LIGHT, "on", {"friendly_name": "Kitchen light", "brightness": 200},
                                                   context=Context(user_id="alex")))
    data = await listening.export(only_key(listening))
    first, second = data["turns"]
    assert [first["user"]["content"], second["user"]["content"]] == ["Is the kitchen light on?", "Turn it on."]
    # Each snapshot is taken as the user speaks, before Assist acts on their words.
    assert first["source"]["states"][LIGHT]["state"] == "off" and second["source"]["states"][LIGHT]["state"] == "off"
    assert any(c["entity_id"] == LIGHT and c["new"]["state"] == "on" and c["new"]["context"]["user_id"] == "alex"
               for c in data["state_changes"])  # The change during the turn, with who made it.
    source = first["source"]  # The moment, whole, once: in the first turn.
    entity = source["entities"][LIGHT]
    assert entity["aliases"] == ["Big light"] and entity["area_id"] == listening.area.id
    assert entity["device_id"] == listening.lamp.id and LIGHT in source["states"]
    device = source["devices"][listening.lamp.id]
    assert (device["name"], device["area_id"], device["manufacturer"], device["model"]) == (
        "Kitchen lamp", listening.area.id, "Fixture Co", "L1")
    area = source["areas"][listening.area.id]
    assert (area["name"], area["aliases"]) == ("Kitchen", ["Cooking"])
    assert source["exposure"][LIGHT] is True and source["exposure"][SENSOR] is False
    assert source["time_zone"] == hass.config.time_zone and source["home_name"] == hass.config.location_name
    assert {"scripts", "automations", "scenes", "todo", "household", "config", "floors", "labels", "at"} <= set(source)
    assert set(second["source"]) == {"at", "states"}  # A later turn: what moves.
    # The prompt Assist had for each turn (up to the user's words), and what it lacked without an assistant API.
    assert [(m["role"], m.get("content")) for m in second["preparations"][0]["content"][1:]] == [
        ("user", "Is the kitchen light on?"), ("assistant", "No, it's off."), ("user", "Turn it on.")]
    assert "No selected LLM API" in first["context_limitations"][0]
    assert effect_guards == []


@pytest.mark.asyncio
async def test_each_turn_records_what_assist_was_given_when_home_assistant_keeps_the_last_turns_prompt(
        listening, effect_guards):
    """Home Assistant gives a turn like the last one (same assistant, person, device and API) the same prompt and
    tools, with no update to the chat log: the turn records them all the same (DETAILS P8.2.2), with its own context,
    so it can be replayed."""
    hass = listening.hass
    await say(hass, "kitchen-chat", "Is the kitchen light on?", "No, it's off.", api="assist")
    await say(hass, "kitchen-chat", "Turn it on.", "Done.", api="assist")
    data = await listening.export(only_key(listening))
    first, second = data["turns"]
    assert second["prepared"]["api_id"] == "assist" and second["prepared"]["tools"] == first["prepared"]["tools"]
    assert second["preparations"][0]["content"][0] == first["preparations"][0]["content"][0]  # Turn 1's prompt.
    contexts = [t["prepared"]["context"]["context"]["id"] for t in data["turns"]]
    assert contexts[0] != contexts[1]  # Its own request.
    assert [(m["role"], m.get("content")) for m in second["preparations"][0]["content"][1:]] == [
        ("user", "Is the kitchen light on?"), ("assistant", "No, it's off."), ("user", "Turn it on.")]
    assert len(recording_turns(data | {"status": "session_expired"})) == 2
    assert effect_guards == []


@pytest.mark.asyncio
async def test_a_recording_keeps_the_devices_whole_but_never_a_credential(listening, effect_guards):
    """A device's serial number and network address are its registry entry's, kept like the rest of it; an
    integration's credentials are its config entry's, which nothing reads (DETAILS P8.11.1)."""
    hass = listening.hass
    await say(hass, "kitchen-chat", "Is the kitchen light on?", "No, it's off.")
    text = json.dumps(await listening.export(only_key(listening)))
    assert "SERIAL-DO-NOT-RECORD" in text and "aa:bb:cc:dd:ee:ff" in text
    assert "DO_NOT_RECORD_KEY" not in text
    assert effect_guards == []


def test_a_state_is_kept_whole_as_home_assistant_writes_it():
    """Every attribute, as Home Assistant's own State.as_dict has it: no allowlist (DETAILS P8.2.5), and no
    redaction, whatever an entity or attribute is named (Kai, 11-05: "it still stays private within the user's HA")."""
    for state in (State(LIGHT, "on", {"friendly_name": "Kitchen light", "brightness": 120,
                                      "entity_picture": "/local/lamp.png", "ip_address": "10.0.0.7"}),
                  State("sensor.router_password", "hunter2", {"friendly_name": "Router", "invented": {"a": [1]}})):
        assert state_record(state) == json.loads(json.dumps(state.as_dict(), default=str))
    assert state_record(None) is None  # A device that didn't exist yet (or any more).


def test_nothing_is_redacted_and_what_was_said_stays_as_said():
    said = {"role": "user", "content": "The guest wifi password is hunter2, tell the guests."}
    assert plain(said) == said
    headers = {"Authorization": "Bearer x", "headers": {"Cookie": "c", "accept": "json"}}
    assert plain(headers) == headers


def test_every_recorded_value_is_plain_json():
    class Mode(Enum):
        HEAT = "heat"

    @dataclass
    class Reading:
        value: float
        mode: Mode

    class Client:
        def __repr__(self):
            raise AssertionError("an object's repr is never recorded")

    value = {"nan": float("nan"), "inf": float("inf"), "ok": 1.5, "mode": Mode.HEAT, "tags": {"a"}, "pair": (1, 2),
             "at": datetime(2026, 1, 1, tzinfo=UTC), "reading": Reading(2.0, Mode.HEAT), 3: "numeric key",
             "client": Client()}
    assert plain(value) == {"nan": None, "inf": None, "ok": 1.5, "mode": "heat", "tags": ["a"], "pair": [1, 2],
                            "at": "2026-01-01T00:00:00+00:00", "reading": {"value": 2.0, "mode": "heat"},
                            "3": "numeric key", "client": {"omitted_type": "Client"}}
    json.dumps(plain(value), allow_nan=False)  # Strict JSON: nothing a store or an export can't write.


@pytest.mark.asyncio
async def test_provider_settings_are_recorded_without_credentials(hass):
    settings = {"prompt": "Be brief.", "chat_model": "fixture-model", "custom_header": "x", "invented": {"a": [1]}}
    entry = ConfigEntry(domain="fixture_provider", title="Claude", data={"api_key": "DO_NOT_RECORD"}, source="user",
                        version=1, minor_version=1, options={}, unique_id=None, discovery_keys=MappingProxyType({}),
                        state=ConfigEntryState.LOADED, subentries_data=[
                            {"subentry_type": "conversation", "title": "Assistant", "unique_id": None,
                             "subentry_id": "conv", "data": settings},
                            {"subentry_type": "ai_task_data", "title": "Task", "unique_id": None,
                             "subentry_id": "task", "data": {"chat_model": "fixture-small"}}])
    hass.config_entries = SimpleNamespace(async_loaded_entries=loaded_for(hass), async_entries=lambda domain=None: [entry] if domain == "fixture_provider"
                                          else [])
    assert provider_config(hass, "fixture_provider") == [{  # Every setting; the credentials are the entry's.
        "config_entry_id": entry.entry_id, "subentry_id": "conv", "title": "Assistant", "options": settings}]
    assert "DO_NOT_RECORD" not in json.dumps(provider_config(hass, "fixture_provider"))
    assert provider_config(hass, "another_provider") == []


@pytest.mark.asyncio
async def test_scripts_and_automations_are_recorded_as_the_household_has_them(hass):
    """Read as Home Assistant's own script and automation integrations hold them, YAML ones included."""
    from homeassistant import loader
    from homeassistant.setup import async_setup_component
    loader.async_setup(hass)
    await load_registries(hass)
    press = {"alias": "Press the relay", "sequence": [{"action": "switch.toggle",
                                                       "target": {"entity_id": "switch.relay"}}]}
    night = {"id": "night", "alias": "Night", "triggers": [], "actions": []}
    assert await async_setup_component(hass, "script", {"script": {"press": dict(press)}})
    assert await async_setup_component(hass, "automation", {"automation": [dict(night)]})
    captured = snapshot(hass)
    assert captured["scripts"] == {"press": press} and captured["automations"] == [night]
    press["alias"] = night["alias"] = "Edited later"
    assert captured["scripts"]["press"]["alias"] == "Press the relay"  # A copy: later edits don't rewrite history.
    assert captured["automations"][0]["alias"] == "Night"


@pytest.mark.asyncio
async def test_state_changes_are_recorded_only_while_the_conversation_is_open(listening, effect_guards):
    hass = listening.hass
    await say(hass, "kitchen-chat", "Turn on the kitchen light.", "Done.",
              during=lambda: hass.states.async_set(LIGHT, "on", {"friendly_name": "Kitchen light"},
                                                   context=Context(user_id="alex")))
    key = only_key(listening)
    close(hass, "kitchen-chat")
    hass.states.async_set(LIGHT, "off", {"friendly_name": "Kitchen light"})  # After it ended: not part of it.
    await hass.async_block_till_done()
    data = await listening.export(key)
    assert data["status"] == "session_expired"
    assert [c["new"]["state"] for c in data["state_changes"] if c["entity_id"] == LIGHT] == ["on"]
    assert effect_guards == []


@pytest.mark.asyncio
async def test_a_reply_with_provider_only_history_is_marked_for_replays_to_check(listening, effect_guards):
    hass = listening.hass
    await say(hass, "kitchen-chat", "Is the kitchen light on?", "No, it's off.", native=object())
    key = only_key(listening)
    await listening.flush()
    await listening.flush()  # Noted once, however often the log is read.
    notes = [n for n in (await listening.export(key))["limitations"] if n.startswith("provider_native_history")]
    assert notes == ["provider_native_history_omitted; replay adapter must check compatibility"]
    assert effect_guards == []


@pytest.mark.asyncio
async def test_a_recording_that_started_mid_conversation_says_so(recording_hass, effect_guards):
    recorder = Recorder(recording_hass)
    await recorder.start()
    try:
        recorder.on_chat("joined-late", "content_added", {"content": {"role": "assistant", "content": "Done."}})
        # Reloaded while Assist answers a later turn: the first event seen is the update as the turn closes.
        recorder.on_chat("reloaded", "updated", {"chat_log": {"content": [
            {"role": "system", "content": "Be brief."}, {"role": "user", "content": "And the fan."}]}})
        assert len(recorder.index) == 2
        for key in recorder.index:
            assert "recording_started_mid_session" in (await recorder.export(key))["limitations"]
    finally:
        await recorder.stop()
    assert effect_guards == []


@pytest.mark.asyncio
async def test_a_recorder_error_never_breaks_the_assistant_and_is_reported(recording_hass, effect_guards):
    recorder = Recorder(recording_hass)
    await recorder.start()
    try:
        recorder.on_chat("odd", "created", {"chat_log": {"content": []}})
        recorder.on_chat("odd", "content_added", {"content": "not a message"})  # Raises inside; never to HA.
        key = only_key(recorder)
        assert recorder.status()["last_error"] == "AttributeError"
        recorder.on_chat("odd", "content_added", {"content": {"role": "user", "content": "Hello"}})
        await recorder.flush()
        data = await recorder.export(key)
        assert "capture_error:AttributeError" in data["limitations"]  # The gap is explicit in the recording.
        assert data["turns"][0]["user"]["content"] == "Hello"  # And recording goes on.
        assert recorder.status()["last_error"] == "AttributeError"  # Still reported after a good save.
    finally:
        await recorder.stop()
    assert effect_guards == []


@pytest.mark.asyncio
async def test_the_improvers_own_ai_calls_are_never_recorded_as_household_conversations(
        hass, effect_guards, monkeypatch):
    await assist_home(hass, {})
    recorder = Recorder(hass)
    await recorder.start()

    async def generate(hass, *, task_name, entity_id, instructions, structure=None, llm_api=None, **_):
        """A role's AI Task answering through its own chat log. Its words come first, so only the role marker keeps
        it out (HA's AI Task prepares its prompt first, which the recorder drops anyway)."""
        with (async_get_chat_session(hass, context=None) as session,
              ha_chat_log.async_get_chat_log(hass, session) as log):
            log.async_add_user_content(ha_chat_log.UserContent(content=instructions))
            log.async_add_assistant_content_without_tools(AssistantContent(agent_id=entity_id, content="{}"))
        return SimpleNamespace(data={}, conversation_id=session.conversation_id)
    monkeypatch.setattr(ai_task, "async_generate_data", generate)
    try:
        run = await run_role(hass, role="journey_analyzer", entity_id="ai_task.fixture", instructions="Read this.",
                             payload={}, structure=None, allow_paid_inference=True)
        await hass.async_block_till_done()
        assert run["status"] == "completed" and recorder.index == {}
        await say(hass, "assist-chat", "Hi", "Hello.")
        assert len(recorder.index) == 1  # An Assist conversation alongside is kept.
    finally:
        await recorder.stop()
    assert effect_guards == []


@pytest.mark.asyncio
async def test_another_integrations_ai_task_call_isnt_recorded_whether_or_not_it_answers(listening, effect_guards):
    """P8.1: only the household's Assist conversations are recorded. Home Assistant's AI Task goes through the same
    chat log as Assist, so another integration's AI Task call (a morning summary, say; here through HA's real AI Task
    path) isn't kept: not when it answers, nor when its provider fails before answering, and not when its
    chat session ends later. The control: an Assist conversation alongside it is kept."""
    hass = listening.hass

    def task(entity_id, transport):
        entity = OpenRouterAITaskEntity(SimpleNamespace(entry_id="router", runtime_data=transport),
                                        SimpleNamespace(subentry_id=entity_id, title="Digest",
                                                        data={"model": "fixture/model"}))
        entity.hass, entity.entity_id = hass, entity_id
        entity.async_write_ha_state = lambda: None
        entity.platform = SimpleNamespace(domain="open_router")
        return entity

    class Down:
        """The provider fails before answering."""
        chat = SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(side_effect=openai.OpenAIError("down"))))
    answers = Transport([SimpleNamespace(role="assistant", content="A quiet day.", tool_calls=None)])
    entities = {e.entity_id: e for e in (task("ai_task.morning_summary", answers), task("ai_task.down", Down()))}
    stand_in(hass, entities.get)
    digest = await ai_task.async_generate_data(hass, task_name="Morning summary",
                                               entity_id="ai_task.morning_summary",
                                               instructions="Summarise the night in one line.")
    assert digest.data == "A quiet day."
    with pytest.raises(HomeAssistantError):
        await ai_task.async_generate_data(hass, task_name="Morning summary", entity_id="ai_task.down",
                                          instructions="Summarise the night in one line.")
    await say(hass, "kitchen-chat", "Is the kitchen light on?", "No, it's off.")
    close(hass, digest.conversation_id)  # Its chat session ends later: no new recording either.
    await listening.flush()
    await hass.async_block_till_done()
    assert (await listening.export(only_key(listening)))["conversation_id"] == "kitchen-chat"
    assert effect_guards == []


# --- Closing and flushing --------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_writes_are_saved_on_their_own_within_about_a_second(recording_hass, effect_guards):
    recorder = Recorder(recording_hass)
    await recorder.start()
    try:
        recorder.on_chat("debounced", "created", {"chat_log": {"content": []}})
        key = only_key(recorder)
        await asyncio.sleep(0.5)  # INSTALL.md: writes are debounced by one second, so not saved at once...
        assert recorder.status()["pending_writes"] == 1 and await stored(recorder, key) is None
        await asyncio.sleep(1.0)  # ...but saved about a second later.
        await recording_hass.async_block_till_done()
        assert recorder.status()["pending_writes"] == 0
        assert (await stored(recorder, key))["conversation_id"] == "debounced"
    finally:
        await recorder.stop()
    assert effect_guards == []


@pytest.mark.asyncio
async def test_a_closed_recording_is_stored_packed_and_read_back_whole(listening, effect_guards):
    hass = listening.hass
    await say(hass, "kitchen-chat", "Is the kitchen light on?", "No, it's off.")
    await say(hass, "kitchen-chat", "Turn it on.", "Done.")
    hass.states.async_set(LIGHT, "on", {"friendly_name": "Kitchen light"}, context=Context(user_id="alex"))
    await say(hass, "kitchen-chat", "Thanks.", "You're welcome.")
    key = only_key(listening)
    while_open = await listening.export(key)
    close(hass, "kitchen-chat")
    await listening.flush()
    on_disk = await stored(listening, key)
    assert on_disk["packed"] and listening.index[key]["stored"] == "packed"
    assert "source" not in on_disk["turns"][2] and on_disk["turns"][2]["source_changes"]
    assert len(json.dumps(on_disk)) < len(json.dumps(while_open))
    exported = await listening.export(key)  # Readers get every turn's full snapshot back.
    assert exported["turns"] == while_open["turns"] and exported["state_changes"] == while_open["state_changes"]
    assert exported["turns"][2]["source"]["states"][LIGHT]["state"] == "on"
    assert "packed" not in exported
    fresh = Recorder(hass)  # And so does the next start of Home Assistant.
    await fresh.start()
    try:
        assert await fresh.export(key) == exported
    finally:
        await fresh.stop()
    assert effect_guards == []


@pytest.mark.asyncio
async def test_a_capture_that_goes_quiet_for_15_minutes_is_closed_and_kept(listening, effect_guards, monkeypatch):
    hass = listening.hass
    await say(hass, "quiet-chat", "Is the kitchen light on?", "No, it's off.")
    key = only_key(listening)
    await listening.flush()
    sooner = utcnow() + timedelta(minutes=14)  # Quiet, but not yet for 15 minutes: still open.
    monkeypatch.setattr(recorder_module, "utcnow", lambda: sooner)
    await listening.flush()
    assert listening.index[key]["status"] == "active"
    later = utcnow() + timedelta(minutes=16)  # Home Assistant never said the conversation ended.
    monkeypatch.setattr(recorder_module, "utcnow", lambda: later)
    await listening.flush()
    data = await listening.export(key)
    assert data["status"] == "capture_timed_out" and listening.index[key]["status"] == "capture_timed_out"
    assert any("15 minutes" in note for note in data["limitations"])
    assert data["chat_log"]["content"][-1]["content"] == "No, it's off."  # Nothing captured is lost.
    assert (await stored(listening, key))["packed"]
    assert effect_guards == []


@pytest.mark.asyncio
async def test_export_returns_a_copy_that_cannot_change_the_recording(recording_hass, effect_guards):
    recorder = Recorder(recording_hass)
    await recorder.start()
    try:
        recorder.on_chat("copy", "created", {"chat_log": {"content": []}})
        recorder.on_chat("copy", "content_added", {"content": {"role": "user", "content": "Hello"}})
        key = only_key(recorder)
        exported = await recorder.export(key)
        exported["turns"].clear()
        exported["status"] = "tampered"
        again = await recorder.export(key)
        assert again["status"] == "active" and again["turns"][0]["user"]["content"] == "Hello"
    finally:
        await recorder.stop()
    assert effect_guards == []


# --- Packing losslessly ----------------------------------------------------------------------------------------------

def changing_home():
    """A closed recording whose home changes between turns: devices come and go, settings change."""
    record = with_home(make_recording(), extra=5)
    # Household knowledge only in the first snapshot: gone from the later ones.
    record["turns"][0]["source"].update(household={"everyone": ["Be brief."]}, time_zone="Europe/London")
    second = record["turns"][1]["source"]
    second["states"].pop("light.other_1")
    second["entities"].pop("light.other_1")
    second["states"]["light.new"] = {"entity_id": "light.new", "state": "on", "attributes": {}}
    second["time_zone"] = "Asia/Singapore"
    second["scripts"] = {"press": {"sequence": []}}  # New in a later snapshot.
    third = record["turns"][2]["source"]
    third["exposure"] = {TARGET: False}  # Exposure changed and narrowed.
    return record


def test_packing_is_lossless_when_devices_come_and_go_between_turns():
    record = changing_home()
    restored = retention.unpack(retention.pack(deepcopy(record)))
    assert [t["source"] for t in restored["turns"]] == [t["source"] for t in record["turns"]]
    assert {k: v for k, v in restored.items() if k != "events"} == {k: v for k, v in record.items() if k != "events"}


def test_packing_keeps_a_device_new_in_a_later_turn_even_when_its_value_is_unknown():
    record = make_recording()
    record["turns"][1]["source"]["exposure"]["light.added_later"] = None
    restored = retention.unpack(retention.pack(deepcopy(record)))
    assert restored["turns"][1]["source"]["exposure"] == record["turns"][1]["source"]["exposure"]


def test_the_events_every_reader_needs_survive_packing_and_copies_lose_only_their_data():
    record = make_recording()
    record["events"] = [{"at": "1", "type": "created", "data": {"chat_log": {"content": ["first"]}}},
                        {"at": "2", "type": "content_added", "data": {"content": {"role": "user", "content": "Hi"}}},
                        {"at": "3", "type": "updated", "data": {"chat_log": {"content": ["copy"]}}},
                        {"at": "4", "type": "log_observed", "data": {"content": ["copy"]}},
                        {"at": "5", "type": "deleted", "data": {}}]
    packed = retention.pack(record)
    assert packed["events"] == record["events"][:2] + [{"at": "3", "type": "updated"},
                                                       {"at": "4", "type": "log_observed"}, record["events"][4]]


def test_packing_happens_once_and_only_to_a_finished_recording():
    for status in ("session_expired", "interrupted", "capture_timed_out"):  # Every way a recording finishes.
        assert retention.pack({**make_recording(), "status": status})["packed"]
    packed = retention.pack(make_recording())
    assert retention.pack(packed) is packed  # Already packed: stored as is.
    assert retention.pack({**make_recording(), "status": "active"}).get("packed") is None  # Still growing.
    no_turns = retention.pack({**make_recording(), "turns": []})
    assert no_turns["packed"] and retention.unpack(deepcopy(no_turns))["turns"] == []
    plain_record = make_recording()
    assert retention.unpack(plain_record) is plain_record and retention.unpack(None) is None  # Nothing to rebuild.


# --- Compaction ------------------------------------------------------------------------------------------------------

def home_with_devices():
    """The fixture conversation in a bigger home with device registry entries, scripts, automations, template
    entities' own actions, and a sensor reporting on its own during the conversation."""
    record = with_home(make_recording())
    for turn in record["turns"]:
        source = turn["source"]
        source["entities"][TARGET]["device_id"] = "dev-relay"
        source["entities"]["light.other_0"]["device_id"] = "dev-other"
        source["devices"] = {"dev-relay": {"name": "Relay box"}, "dev-other": {"name": "Other lamp"}}
        source["exposure"].update({f"light.other_{n}": True for n in range(40)})
        source["scripts"] = {"press": {"sequence": []}}
        source["automations"] = [{"id": "night"}]
        source["scenes"] = [{"id": "evening", "name": "Evening", "entities": {TARGET: {"state": "on"}}}]
        source["todo"] = {"todo.shopping": {"items": [{"summary": "Milk", "status": "needs_action"}]}}
    record["state_changes"].append({"entity_id": "sensor.power", "at": record["turns"][0]["user"]["created"],
                                    "old": {"state": "1"}, "new": {"state": "2", "context": {"id": "x"}}})
    return record


def test_compaction_keeps_the_conversation_its_tool_calls_and_the_involved_devices_with_their_devices_and_areas():
    record = home_with_devices()
    compacted = retention.unpack(retention.compact(record))
    assert compacted["chat_log"] == record["chat_log"]  # The conversation and every tool call and result.
    assert [t["user"] for t in compacted["turns"]] == [t["user"] for t in record["turns"]]
    assert [t["prepared"] for t in compacted["turns"]] == [t["prepared"] for t in record["turns"]]
    source = compacted["turns"][0]["source"]
    assert set(source["states"]) == set(source["entities"]) == set(source["exposure"]) == {TARGET}
    assert source["entities"][TARGET] == record["turns"][0]["source"]["entities"][TARGET]  # Names, area, device.
    assert source["devices"] == {"dev-relay": {"name": "Relay box"}}  # Its own device; the others go.
    assert source["areas"] == record["turns"][0]["source"]["areas"]
    assert not {"scripts", "automations", "scenes", "todo"} & set(source)
    assert {c["entity_id"] for c in compacted["state_changes"]} == {TARGET}
    assert compacted["compact"] is True


def test_only_devices_a_person_or_assist_changed_during_the_conversation_count_as_involved():
    record = make_recording()
    first = record["turns"][0]["user"]["created"]
    last = record["turns"][-1]["user"]["created"]

    def shift(at, **delta):
        return (datetime.fromisoformat(at) + timedelta(**delta)).isoformat()
    record["state_changes"] = [
        {"entity_id": "light.asked", "at": shift(first, seconds=2), "new": {"context": {"user_id": "alex"}}},
        {"entity_id": "cover.by_automation", "at": shift(first, seconds=3), "new": {"context": {"parent_id": "c"}}},
        {"entity_id": "sensor.by_itself", "at": shift(first, seconds=4), "new": {"context": {"id": "c2"}}},
        {"entity_id": "light.before", "at": shift(first, seconds=-30), "new": {"context": {"user_id": "alex"}}},
        {"entity_id": "light.long_after", "at": shift(last, minutes=5), "new": {"context": {"user_id": "alex"}}},
        {"entity_id": "light.gone", "at": shift(first, seconds=5), "new": None},
    ]
    # Assist tried the lock and failed (written as the fixture writes tool results): it acted on it all the same.
    record["chat_log"]["content"].append({
        "role": "tool_result", "tool_call_id": "call-lock", "tool_name": "HassTurnOff", "result": {"data": {
            "response_type": "action_done", "data": {"success": [], "failed": [{"name": "Back door", "id": LOCK}]}}}})
    # TARGET: Assist acted on it.
    assert retention.involved(record) == {"light.asked", "cover.by_automation", TARGET, LOCK}


def test_a_device_assist_acted_on_is_kept_when_compacting_a_real_home_assistant_recording():
    record = make_recording()
    for message in record["chat_log"]["content"]:  # As HA's chat log writes tool results: result and tool_result.
        if message["role"] == "tool_result":
            message.update(ha_tool_result(success=[(TARGET, "Fixture relay")]), tool_call_id=message["tool_call_id"])
    record["chat_log"]["content"].append(ha_tool_result(failed=[(LOCK, "Back door")]))  # Failed: no state change.
    record["state_changes"] = []  # Nothing changed state (a voice satellite request carries no user either).
    for turn in record["turns"]:
        turn["source"]["states"][LOCK] = {"entity_id": LOCK, "state": "unlocked", "attributes": {}}
        turn["source"]["entities"][LOCK] = {"name": "Back door", "aliases": [], "area_id": "fixture_area"}
    assert retention.involved(record) >= {TARGET, LOCK}
    kept = retention.unpack(retention.compact(record))["turns"][0]["source"]["states"]
    assert {TARGET, LOCK} <= set(kept)


def test_compacting_again_or_from_the_packed_file_gives_the_same_digest():
    record = home_with_devices()
    record["turns"][2]["source"]["states"]["light.other_5"]["state"] = "on"  # Not involved, changed in a later turn.
    once = retention.compact(record)
    assert retention.compact(once) == once
    assert retention.compact(retention.pack(record)) == once  # The tidy-up compacts what's on disk: packed.
    record["turns"][1].pop("source")  # A turn without a home snapshot compacts too.
    assert "source" not in retention.unpack(retention.compact(record))["turns"][1]


def test_a_compacted_recording_is_read_by_the_analyzer_the_quick_check_and_the_proposer_and_reviewer():
    full, digest = with_home(make_recording()), retention.unpack(retention.compact(with_home(make_recording())))
    digest["id"] = "compacted"
    view = SessionView(digest)
    assert view.overview()["conversation"] == SessionView(full).overview()["conversation"]  # The analyzer's input.
    assert triage.payload(view)["turns"] and triage.payload(view)["devices_involved"][0]["entity_id"] == TARGET
    sessions = hypotheses.Sessions([triage.unanalysed("compacted")], [view])  # The proposer's and reviewer's.
    assert sessions.views[hypotheses.short("compacted")].conversation()[0]["user_says"] == "Toggle the fixture relay."


def test_a_compacted_recording_is_never_replayed_or_used_to_seed_the_sandbox():
    full = {**with_home(make_recording()), "id": "full-older"}
    newer = with_home(make_recording())
    for turn in newer["turns"]:  # Recorded a day later.
        turn["user"]["created"] = turn["user"]["created"].replace("2026-01-01", "2026-01-02")
    for message in newer["chat_log"]["content"]:
        if message.get("created"):
            message["created"] = message["created"].replace("2026-01-01", "2026-01-02")
    compacted = {**retention.unpack(retention.compact(newer)), "id": "compacted-newer"}
    with pytest.raises(CaseError, match="compacted"):
        recording_turns(compacted)
    assert replayable_turns(compacted) == []
    sessions = hypotheses.Sessions([triage.unanalysed(full["id"]), triage.unanalysed(compacted["id"])],
                                   [SessionView(full), SessionView(compacted)])
    assert sessions.household.recording["id"] == "full-older"  # The household as of a session a sandbox can replay.


# --- The daily tidy-up of recordings ---------------------------------------------------------------------------------

@pytest_asyncio.fixture
async def kept(recording_hass):
    """A recorder over Home Assistant's real storage, for the tidy-up."""
    recorder = Recorder(recording_hass)
    await recorder.start()
    try:
        yield recorder
    finally:
        await recorder.stop()


@pytest.mark.asyncio
async def test_recordings_with_no_turns_are_deleted_however_recent_and_open_ones_are_left_alone(kept, effect_guards):
    now = utcnow()
    await arrive(kept, recording("handled-by-ha", (now - timedelta(minutes=5)).isoformat(), turns=False))
    await arrive(kept, recording("still-open", (now - timedelta(minutes=1)).isoformat(), turns=False,
                                 status="active"))
    await arrive(kept, recording("interrupted", (now - timedelta(days=90)).isoformat(), turns=False,
                                 status="interrupted"))
    done = await retention.prune(kept, referenced=set(), now=now)
    assert done["deleted"] == 2 and set(kept.index) == {"still-open"}
    for gone in ("handled-by-ha", "interrupted"):
        assert await stored(kept, gone) is None  # Its file is gone too.
        with pytest.raises(ValueError, match="Unknown recording ID"):
            await kept.export(gone)
    fresh = Recorder(kept.hass)
    await fresh.start()
    try:
        assert "handled-by-ha" not in fresh.index  # The index on disk says so too.
    finally:
        await fresh.stop()
    assert effect_guards == []


@pytest.mark.asyncio
async def test_the_newest_20_recordings_stay_in_full_however_old(kept, effect_guards):
    now = utcnow()
    for n in range(22):  # Months old, the newest first.
        await arrive(kept, recording(f"old-{n:02d}", (now - timedelta(days=100 + n)).isoformat()))
    for n in range(3):  # Newer, but with no turns: they don't take a place among the newest.
        await arrive(kept, recording(f"empty-{n}", (now - timedelta(days=1)).isoformat(), turns=False))
    done = await retention.prune(kept, referenced=set(), now=now)
    assert done == {"deleted": 3, "packed": 20, "compacted": 2}
    for n in range(20):
        data = await kept.export(f"old-{n:02d}")
        assert not data.get("compact") and recording_turns(data)  # Still replayable: they seed the sandbox.
    for n in (20, 21):
        assert (await kept.export(f"old-{n:02d}"))["compact"] and kept.index[f"old-{n:02d}"]["stored"] == "compact"
    assert effect_guards == []


@pytest.mark.asyncio
async def test_recordings_from_the_last_30_days_stay_in_full(kept, effect_guards):
    now = utcnow()
    await newest(kept, now)
    await arrive(kept, recording("thirty-days", (now - timedelta(days=30)).isoformat()))
    await arrive(kept, recording("a-month-ago", (now - timedelta(days=29)).isoformat()))
    await arrive(kept, recording("older", (now - timedelta(days=30, minutes=1)).isoformat()))
    await retention.prune(kept, referenced=set(), now=now)
    assert not (await kept.export("thirty-days")).get("compact")
    assert not (await kept.export("a-month-ago")).get("compact")
    assert (await kept.export("older"))["compact"]
    assert effect_guards == []


@pytest.mark.asyncio
async def test_recordings_an_improvement_or_a_case_uses_stay_in_full(kept, effect_guards):
    now = utcnow()
    await newest(kept, now)
    old = (now - timedelta(days=200)).isoformat()
    for rid in ("improved-session-0001", "case-session-0001", "unused-session-0001"):
        await arrive(kept, recording(rid, old))
    done = await retention.prune(kept, referenced={"improved-session-0001", "case-session-0001"}, now=now)
    assert done["compacted"] == 1
    assert not (await kept.export("improved-session-0001")).get("compact")
    assert not (await kept.export("case-session-0001")).get("compact")
    assert (await kept.export("unused-session-0001"))["compact"]
    assert effect_guards == []


@pytest.mark.asyncio
async def test_compaction_in_the_tidy_up_keeps_the_devices_its_analysis_named(kept, effect_guards):
    now = utcnow()
    await newest(kept, now)
    await arrive(kept, {**with_home(recording("analysed", (now - timedelta(days=60)).isoformat())), "id": "analysed"})
    await retention.prune(kept, referenced=set(), now=now, named={"analysed": {"light.other_7"}})
    data = await kept.export("analysed")
    assert data["compact"] and set(data["turns"][0]["source"]["states"]) == {TARGET, "light.other_7"}
    assert effect_guards == []


@pytest.mark.asyncio
async def test_the_tidy_up_goes_on_past_a_recording_whose_file_is_missing(kept, effect_guards):
    now = utcnow()
    await newest(kept, now)
    kept.index["lost-file"] = {"id": "lost-file", "started": (now - timedelta(days=90)).isoformat(),
                               "status": "session_expired", "user_turns": 2}
    await arrive(kept, recording("old", (now - timedelta(days=91)).isoformat()))
    done = await retention.prune(kept, referenced=set(), now=now)
    assert done["compacted"] == 1 and (await kept.export("old"))["compact"]
    with pytest.raises(ValueError, match="unavailable"):
        await kept.export("lost-file")
    # The next day it goes past the missing file again, and the index says how the rest are stored: nothing to redo.
    assert await retention.prune(kept, referenced=set(), now=now) == {"deleted": 0, "packed": 0, "compacted": 0}
    assert effect_guards == []


@pytest.mark.asyncio
async def test_the_tidy_up_runs_once_home_assistant_has_started_and_keeps_what_the_pipeline_needs(
        recording_hass, effect_guards):
    from custom_components.assist_improver.diagnostics import async_get_config_entry_diagnostics
    hass = recording_hass
    entry = fake_entry({})
    await async_setup_entry(hass, entry)
    runtime = entry.runtime_data
    recorder = runtime.recorder
    try:
        now = utcnow()
        await newest(recorder, now)
        old = (now - timedelta(days=120)).isoformat()
        for rid in ("improvd1-old", "casebk01-old", "analysd1-old", "unused01-old"):
            await arrive(recorder, {**with_home(recording(rid, old)), "id": rid})
        await arrive(recorder, recording("noturns1", old, turns=False))
        runtime.improvements.items["imp1"] = {"id": "imp1", "status": "applied", "title": "Press",
                                               "conversation": "improvd1-old", "fixes": ["improvd1:1"]}
        runtime.replays.items["plan1"] = {"plan": {"id": "plan1", "kind": "before"}, "recording_id": "casebk01-old",
                                          "status": "reproduced"}
        for rid in ("improvd1-old", "casebk01-old", "analysd1-old"):
            # Their rounds ended: only what uses them keeps them in full; the analysis names its devices.
            conv = seat(SimpleNamespace(recorder=recorder, runtime=runtime), rid, "Your household",
                        outcome="no trouble found")
            conv["rounds"]["1"]["records"]["Analyze"] = {"attempts": [], "analysis": {
                "goals": [{"turn": 1, "entity_id": "light.other_3"}] if rid == "analysd1-old" else []}}
        await runtime.role_runs.record({"id": "a1", "kind": "journey_analyzer", "recording_id": "analysd1-old",
                                         "status": "completed", "finished": old, "output": {"big": "x" * 500},
                                         "analysis": {"goals": [{"turn": 1, "entity_id": "light.other_3"}]}})
        await runtime.role_runs.record({"id": "old-call", "role": "hypothesis_reviewer", "status": "completed",
                                         "started": old, "finished": old})
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
        await hass.async_block_till_done(wait_background_tasks=True)
        report = (await async_get_config_entry_diagnostics(hass, entry))["retention"]
        # The never-analysed one is waiting for a run, so it stays in full however old it is. The run log's
        # analysis is diagnostics: the flow record keeps what the stages found.
        assert {k: report[k] for k in ("deleted", "packed", "compacted", "runs")} == {
            "deleted": 1, "packed": 23, "compacted": 1, "runs": {"deleted": 2}}
        assert not (await recorder.export("improvd1-old")).get("compact")
        assert not (await recorder.export("casebk01-old")).get("compact")
        analysed = await recorder.export("analysd1-old")
        assert analysed["compact"] and "light.other_3" in analysed["turns"][0]["source"]["states"]
        assert not (await recorder.export("unused01-old")).get("compact")
        assert "noturns1" not in recorder.index
        assert runtime.role_runs.runs == []
    finally:
        await async_unload_entry(hass, entry)
    assert effect_guards == []


# --- The run log tidy-up ---------------------------------------------------------------------------------------------

def test_an_analysis_in_the_run_log_stays_whole_for_30_days_then_goes_as_diagnostics():
    """What the stages found about a conversation is in its flow record, kept as long as the conversation; the run
    log's copy of its analysis is diagnostics."""
    now = utcnow()
    analysis = {"id": "a", "kind": "journey_analyzer", "recording_id": "r1", "status": "completed",
                "analysis": {"goals": []}, "output": {"raw": "reply"}, "usage": {"events": [1]}}
    fresh = {**analysis, "finished": (now - timedelta(days=29)).isoformat()}
    assert retention.prune_runs([fresh], {"r1"}, now=now) == ([fresh], {"deleted": 0})
    old = {**analysis, "finished": (now - timedelta(days=31)).isoformat()}
    assert retention.prune_runs([old], {"r1"}, now=now) == ([], {"deleted": 1})


def test_diagnostics_go_after_30_days_and_runs_with_their_timings_after_a_year():
    now = utcnow()
    at = lambda days: (now - timedelta(days=days)).isoformat()
    runs = [{"id": "call-29", "role": "hypothesis_reviewer", "status": "completed", "finished": at(29)},
            {"id": "call-31", "role": "hypothesis_reviewer", "status": "completed", "finished": at(31)},
            {"id": "run-364", "kind": "pipeline", "status": "done", "started": at(364), "progress": {"timings": {}}},
            {"id": "run-366", "kind": "pipeline", "status": "done", "started": at(366)},
            {"id": "timeless", "role": "editor", "status": "completed"}]  # From before every record had a time.
    kept, done = retention.prune_runs(runs, set(), now=now)
    assert [r["id"] for r in kept] == ["call-29", "run-364"] and done == {"deleted": 3}
    assert kept[1]["progress"] == {"timings": {}}  # Stage timings stay with the run.
    assert retention.prune_runs([], set(), now=now) == ([], {"deleted": 0})


def test_no_analysis_or_proposal_outlives_30_days_in_the_run_log():
    """A conversation's latest analysis is its flow record's (flow.Flow.analysis), and no stage reads a proposal back
    from the log: both are diagnostics there."""
    now = utcnow()
    old = (now - timedelta(days=90)).isoformat()
    runs = [{"id": "first", "kind": "journey_analyzer", "recording_id": "r1", "status": "completed", "finished": old,
             "analysis": {"goals": [1]}},
            {"id": "proposal-1", "kind": "hypothesis_generator", "status": "completed", "finished": old,
             "output": {"hypotheses": []}},
            {"id": "second", "kind": "journey_analyzer", "recording_id": "r1", "status": "completed", "finished": old,
             "analysis": {"goals": [2]}},
            {"id": "proposal-2", "kind": "hypothesis_generator", "status": "completed", "finished": old,
             "output": {"hypotheses": [{"title": "Alias"}]}, "reply": "long"}]
    assert retention.prune_runs(runs, {"r1"}, now=now) == ([], {"deleted": 4})


def test_a_held_verdict_is_kept_until_its_conversation_is_analysed():
    now = utcnow()
    old = (now - timedelta(days=90)).isoformat()
    held = {"id": "held", "kind": "journey_analyzer", "recording_id": "r1", "status": "held", "finished": old,
            "digest_sha256": "d", "triage": {"status": "completed", "output": {"smooth": False}}}
    assert retention.prune_runs([held], {"r1"}, now=now)[0] == [held]  # Whole: the analysis reuses its verdict.
    analysed = {"id": "done", "kind": "journey_analyzer", "recording_id": "r1", "status": "completed",
                "finished": old, "analysis": {"goals": []}}
    assert retention.prune_runs([held, analysed], {"r1"}, now=now)[0] == []  # Analysed since: neither is needed.
    assert retention.prune_runs([held], set(), now=now)[0] == []  # Its conversation is gone.


@pytest.mark.asyncio
async def test_the_run_log_tidy_up_is_saved_and_survives_a_restart(recording_hass):
    now = utcnow()
    runs = RoleRuns(recording_hass)
    await runs.start()
    await runs.record({"id": "old-call", "role": "editor", "status": "completed",
                       "finished": (now - timedelta(days=40)).isoformat()})
    await runs.record({"id": "new-call", "role": "editor", "status": "completed",
                       "finished": (now - timedelta(days=1)).isoformat()})
    assert await runs.prune(set(), now=now) == {"deleted": 1}
    again = RoleRuns(recording_hass)
    await again.start()
    assert [r["id"] for r in again.runs] == ["new-call"]
    assert await again.prune(set(), now=now) == {"deleted": 0}


# --- The status and export_recording actions -------------------------------------------------------------------------

@pytest_asyncio.fixture
async def installed(recording_hass):
    """The integration set up in an offline Home Assistant, with an administrator and a visitor."""
    hass = recording_hass
    hass.auth = SimpleNamespace(async_get_user=AsyncMock(side_effect=lambda user_id: SimpleNamespace(
        is_admin=user_id == "admin", name=user_id)))
    entry = fake_entry({})
    await async_setup_entry(hass, entry)
    try:
        yield SimpleNamespace(hass=hass, recorder=entry.runtime_data.recorder)
    finally:
        await async_unload_entry(hass, entry)


@pytest.mark.asyncio
async def test_export_recording_returns_the_full_recording_to_administrators_only(installed, effect_guards):
    hass, recorder = installed.hass, installed.recorder
    record = with_home(make_recording())
    record.update(id="closed-01", started=utcnow().isoformat())
    await arrive(recorder, retention.pack(record))  # Stored packed, as every closed recording is.
    exported = await service(hass, "export_recording", {"recording_id": "closed-01"}, user_id="admin")
    assert [t["source"] for t in exported["turns"]] == [t["source"] for t in record["turns"]]  # Every snapshot whole.
    assert exported["chat_log"] == record["chat_log"] and "packed" not in exported
    with pytest.raises(Unauthorized):  # It holds household conversations.
        await service(hass, "export_recording", {"recording_id": "closed-01"}, user_id="visitor")
    with pytest.raises(probatio.Invalid):
        hass.services.async_services()[DOMAIN]["export_recording"].schema({})  # A recording ID is required.
    assert effect_guards == []


@pytest.mark.asyncio
async def test_export_recording_includes_what_is_still_being_recorded(installed, effect_guards):
    hass, recorder = installed.hass, installed.recorder
    recorder.on_chat("open-chat", "created", {"chat_log": {"content": []}})
    recorder.on_chat("open-chat", "content_added", {"content": {"role": "user", "content": "Hello"}})
    key = only_key(recorder)
    exported = await service(hass, "export_recording", {"recording_id": key}, user_id="admin")
    assert exported["status"] == "active" and exported["turns"][0]["user"]["content"] == "Hello"
    assert recorder.status()["pending_writes"] == 0  # Exporting saves what was pending first.
    assert effect_guards == []


@pytest.mark.asyncio
async def test_export_recording_says_when_a_recording_is_unknown_or_unavailable(installed, effect_guards):
    hass, recorder = installed.hass, installed.recorder
    await translated(hass)
    with pytest.raises(ServiceValidationError, match="^Unknown recording ID$"):  # The caller's mistake, translated.
        await service(hass, "export_recording", {"recording_id": "no-such-recording"}, user_id="admin")
    recorder.index["lost"] = {"id": "lost", "status": "session_expired", "user_turns": 1}
    with pytest.raises(HomeAssistantError, match="^Recording is unavailable$") as unavailable:
        await service(hass, "export_recording", {"recording_id": "lost"}, user_id="admin")
    assert unavailable.value.translation_key == "recording_unavailable"  # Translated too.
    assert effect_guards == []


@pytest.mark.asyncio
async def test_export_recording_returns_a_compacted_recording_as_kept(installed, effect_guards):
    hass, recorder = installed.hass, installed.recorder
    record = {**with_home(make_recording()), "id": "compacted-01", "started": "2020-01-01T00:00:00+00:00"}
    await arrive(recorder, retention.compact(record))
    exported = await service(hass, "export_recording", {"recording_id": "compacted-01"}, user_id="admin")
    assert exported["compact"] and set(exported["turns"][3]["source"]["states"]) == {TARGET}
    assert exported["chat_log"] == record["chat_log"]
    assert effect_guards == []


@pytest.mark.asyncio
async def test_status_counts_recordings_pending_writes_and_the_last_save_error(installed, effect_guards, monkeypatch):
    hass, recorder = installed.hass, installed.recorder
    assert await service(hass, "status", user_id="admin") == {"recording": True, "sessions": 0, "pending_writes": 0,
                                                              "last_error": None}
    await arrive(recorder, recording("closed-01", utcnow().isoformat()))  # A finished one, kept on disk, counts.
    assert await service(hass, "status", user_id="admin") == {"recording": True, "sessions": 1, "pending_writes": 0,
                                                              "last_error": None}
    recorder.on_chat("first", "created", {"chat_log": {"content": []}})
    recorder.on_chat("second", "created", {"chat_log": {"content": []}})
    assert await service(hass, "status", user_id="admin") == {"recording": True, "sessions": 3, "pending_writes": 2,
                                                              "last_error": None}
    original = Store.async_save

    async def disk_full(*_args):
        raise OSError("synthetic: disk full")
    monkeypatch.setattr(Store, "async_save", disk_full)
    await recorder.flush()
    assert await service(hass, "status", user_id="admin") == {"recording": True, "sessions": 3, "pending_writes": 2,
                                                              "last_error": "OSError"}
    monkeypatch.setattr(Store, "async_save", original)
    await recorder.flush()
    assert await service(hass, "status", user_id="admin") == {"recording": True, "sessions": 3, "pending_writes": 0,
                                                              "last_error": None}
    with pytest.raises(Unauthorized):
        await service(hass, "status", user_id="visitor")
    assert effect_guards == []


@pytest.mark.asyncio
async def test_status_shows_what_its_description_promises(installed):
    """P8.17.2: Recording status tells administrators only whether it's recording, how many recordings there are,
    pending writes and the last error, and its description promises no more."""
    component = Path(__file__).parents[1] / "custom_components" / DOMAIN
    shown = await service(installed.hass, "status", user_id="admin")
    assert set(shown) == {"recording", "sessions", "pending_writes", "last_error"}
    for strings in (component / "strings.json", component / "translations" / "en.json"):
        assert json.loads(strings.read_text())["services"]["status"]["description"] == "Show recording status."


@pytest.mark.asyncio
async def test_status_says_recording_has_stopped_once_the_integration_is_stopped(recording_hass):
    recorder = Recorder(recording_hass)
    await recorder.start()
    assert recorder.status()["recording"] is True
    await recorder.stop()
    assert recorder.status() == {"recording": False, "sessions": 0, "pending_writes": 0, "last_error": None}
