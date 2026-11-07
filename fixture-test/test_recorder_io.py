"""The recorder's writes: it keeps every state change, as the keep rule says (digest.kept), and saves an active
recording when a turn is added, when it closes, and otherwise at most every SAVE_EVERY seconds, serializing off the
event loop."""

import asyncio
from copy import deepcopy
import json
from pathlib import Path
import sys

import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from custom_components.assist_improver import recorder as recorder_module
from custom_components.assist_improver.cases import recording_turns
from custom_components.assist_improver.digest import READINGS_SHOWN, build_digest, kept, machinery, requests
from custom_components.assist_improver.exploration import SessionView
from custom_components.assist_improver.recorder import SAVE_EVERY, Recorder, _frozen
from custom_components.assist_improver.simulation import recorded_transitions
from homeassistant.core import Context
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.storage import Store

from conftest import load_registries
from kit import candidate_turn, improver  # noqa: F401 - improver is a fixture.
from recordings import before, make_recording


@pytest_asyncio.fixture
async def home(hass):
    await load_registries(hass)
    hass.states.async_set("light.desk", "off", {"brightness": 10})
    hass.states.async_set("switch.fan", "off")
    hass.states.async_set("sensor.power", "1")
    yield hass


@pytest.fixture
def saves(monkeypatch):
    """The recordings saved, in order (their keys)."""
    found = []
    original = Store.async_save

    async def counted(store, data):
        if store.key.startswith("assist_improver.session_"):
            found.append(store.key.removeprefix("assist_improver.session_"))
        return await original(store, data)
    monkeypatch.setattr(Store, "async_save", counted)
    return found


async def started(hass):
    recorder = Recorder(hass)
    await recorder.start()
    recorder.on_chat("io", "created", {"chat_log": {"content": []}})
    recorder.on_chat("io", "content_added", {"content": {"role": "user", "content": "Hello"}})
    return recorder, next(iter(recorder.index))


@pytest.mark.asyncio
async def test_every_change_is_kept_but_the_improvers_own_ai_tasks(home, effect_guards):
    """Kai, 11-05: readings are kept, labelled and ranked last, never cut (DETAILS P8.2.9). The recorder cuts only
    the improver's own AI Tasks (P8.1.4); another AI Task of the household's is kept like any device."""
    from homeassistant.config_entries import ConfigEntry, ConfigEntryState
    from types import MappingProxyType
    from custom_components.assist_improver.const import CONF_AI_TASK, DOMAIN
    hass = home
    mine = ConfigEntry(domain=DOMAIN, title="Assist Improver", data={}, source="user", version=1, minor_version=1,
                       options={CONF_AI_TASK: "ai_task.improver"}, unique_id=None,
                       discovery_keys=MappingProxyType({}), state=ConfigEntryState.LOADED, subentries_data=[])
    hass.config_entries._entries[mine.entry_id] = mine
    recorder, key = await started(hass)
    recorder.sessions[key]["turns"][-1]["prepared"] = {"context": {"context": {"id": "request-1"}}}
    hass.states.async_set("device_tracker.phone", "home")
    hass.states.async_set("sensor.power", "2", {"unit_of_measurement": "W"})
    hass.states.async_set("light.desk", "off", {"brightness": 10, "friendly_name": "Desk"})
    hass.states.async_set("switch.fan", "unavailable")
    hass.states.async_set("ai_task.improver", "2026-10-24T10:00:00+00:00")  # The improver's own AI call: cut.
    hass.states.async_set("ai_task.household", "2026-10-24T10:00:00+00:00")  # The household's: kept.
    hass.states.async_set("button.doorbell", "2026-10-24T10:00:00+00:00", context=Context(id="request-1"))
    await hass.async_block_till_done()
    data = await recorder.export(key)
    assert [c["entity_id"] for c in data["state_changes"]] == [
        "device_tracker.phone", "sensor.power", "light.desk", "switch.fan", "ai_task.household", "button.doorbell"]
    assert "flaps_not_kept" not in data
    await recorder.stop()
    assert effect_guards == []


def _change(entity_id, old, new, context=None, old_attributes=None, new_attributes=None, at=None):
    return {"entity_id": entity_id, "at": at or "2026-01-01T00:00:02+00:00",
            "old": {"entity_id": entity_id, "state": old, "attributes": old_attributes or {}},
            "new": {"entity_id": entity_id, "state": new, "attributes": new_attributes or {},
                    "context": {"id": "x", "parent_id": None, "user_id": None, **(context or {})}}}


def test_the_keep_rule_cuts_only_the_improvers_ai_tasks_labels_machinery_and_readings_and_keeps_the_rest():
    """digest.kept, the one rule for the recorder, the sandbox and every reader (DETAILS P8.2.9): the improver's own
    AI Tasks cut; the answering voice assistant's agent, speech and wake word entities and its device's assist
    satellite labelled machinery, never an outcome (the same device's speaker is the home's); a change nobody caused
    that leaves the state as it was, or a counter's, proximity's, image's, or a sensor's with a unit, a state class or
    a numeric device class, labelled a reading; every other change kept unlabelled: a household's own change too."""
    record = make_recording()
    record["turns"][0]["prepared"]["context"]["device_id"] = "kitchen-satellite"
    record["turns"][0]["pipeline"] = {"conversation_engine": "conversation.chat", "stt_engine": "stt.cloud",
                                      "tts_engine": "tts.cloud", "wake_word_entity": "wake_word.local"}
    record["turns"][0]["source"]["entities"].update({
        "assist_satellite.kitchen": {"device_id": "kitchen-satellite"},
        "media_player.kitchen_radio": {"device_id": "kitchen-satellite"},
        "assist_satellite.hall": {"device_id": "hall-satellite"}})
    gears, asked = machinery(record), requests(record)
    assert gears == {"conversation.fixture", "conversation.chat", "stt.cloud", "tts.cloud", "wake_word.local",
                     "assist_satellite.kitchen"}  # The agent that answered, as its replies name it; its pipeline's.

    def label(change, cut=()):
        return kept(change, asked, gears, cut)
    assert label(_change("ai_task.improver", "a", "b"), cut={"ai_task.improver"}) is None
    assert label(_change("ai_task.household", "a", "b"), cut={"ai_task.improver"}) == "change"
    for entity_id in ("conversation.fixture", "assist_satellite.kitchen", "tts.cloud"):
        assert label(_change(entity_id, "idle", "responding", {"id": "fixture-context-1"})) == "machinery"
    assert label(_change("media_player.kitchen_radio", "idle", "playing", {"id": "fixture-context-1"})) == "change"
    assert label(_change("assist_satellite.hall", "idle", "listening")) == "change"
    readings = [_change("sensor.power", "1", "2", new_attributes={"unit_of_measurement": "W"}),
                _change("sensor.energy", "1", "2", new_attributes={"state_class": "total_increasing"}),
                _change("sensor.humidity", "40", "41", new_attributes={"device_class": "humidity"}),
                _change("counter.visits", "1", "2"), _change("proximity.home", "3", "2"), _change("image.door", "a", "b"),
                _change("media_player.tv", "playing", "playing", {}, {"media_title": "One"}, {"media_title": "Two"}),
                _change("climate.hall", "heat", "heat", {}, {"current_temperature": 19.5}, {"current_temperature": 19.7})]
    assert [label(c) for c in readings] == ["reading"] * len(readings)
    household = [_change("binary_sensor.door", "off", "on"), _change("sensor.mode", "a", "b"),
                 _change("sensor.next_alarm", "a", "b", new_attributes={"device_class": "timestamp"}),
                 _change("switch.fan", "on", "unavailable"),
                 _change("sensor.power", "2", "3", {"user_id": "alex"}, new_attributes={"unit_of_measurement": "W"}),
                 _change("light.desk", "on", "on", {"parent_id": "automation-run"}, {"brightness": 3}, {"brightness": 9}),
                 _change("sensor.power", "3", "4", {"id": "fixture-context-1"}, new_attributes={"unit_of_measurement": "W"})]
    assert [label(c) for c in household] == ["change"] * len(household)


def test_the_views_read_the_kept_changes_machinery_left_out_readings_last():
    """Views and comparisons read only what the recorder kept, as the keep rule labels it, and filter nothing again:
    the answering assistant's own entity is never what a request did; a reading is kept and listed after everything
    else, one line per entity with how many, the first and the last; a household's change is listed as a change, one
    that moved only attributes too."""
    record = make_recording()  # Answered by conversation.fixture.
    request = record["turns"][0]["prepared"]["context"]["context"]["id"]
    at = [f"2026-01-01T00:00:0{n}.500000+00:00" for n in (2, 3, 4)]
    record["state_changes"] += [
        _change("conversation.fixture", "a", "b", {"id": request}, at=at[0]),
        *(_change("sensor.power", str(n), str(n + 1), new_attributes={"unit_of_measurement": "W"}, at=t)
          for n, t in enumerate(at)),
        _change("binary_sensor.door", "off", "on", at=at[1]),
        _change("light.lamp", "on", "on", {"id": request}, {"brightness": 50}, {"brightness": 200}, at=at[2])]
    view = SessionView(record)
    assert "conversation.fixture" not in view.changes
    assert [c.get("reading") for c in view.changes["sensor.power"]] == [True, True, True]
    assert "reading" not in view.changes["binary_sensor.door"][0]
    turn = view.read_turn(1)
    assert "binary_sensor.door" in [c["entity_id"] for c in turn["state_changes_during_turn"]]
    assert "sensor.power" not in [c["entity_id"] for c in turn["state_changes_during_turn"]]
    assert [(r["entity_id"], r["readings"], r["first"]["new"], r["last"]["new"]) for r in turn["readings_elsewhere"]] == [
        ("sensor.power", 3, "1", "3")]
    assert list(turn)[-1] == "readings_elsewhere"
    digest = build_digest(record)
    assert [(r["entity_id"], r["readings"]) for r in digest["readings"]] == [("sensor.power", 3)]
    assert list(digest)[-1] == "readings"
    assert "sensor.power" not in [c["entity_id"] for c in digest["relevant_entities"]]
    lamp = next(c for c in digest["relevant_entities"] if c["entity_id"] == "light.lamp")  # Only its brightness moved.
    assert [(c["old"], c["new"], c["attributes_changed"]) for c in lamp["changes_during_session"]] == [
        ("on", "on", ["brightness"])]
    turns = recording_turns(record, replay=False)
    assert "conversation.fixture" not in [t["entity_id"] for t in recorded_transitions(record, turns[0])]


def test_a_views_upfront_size_stays_bounded_with_2000_readings():
    """Readings never crowd out what happened: 2,000 of them, from 400 sensors, add a bounded listing to what a view
    gives upfront (the overview, a turn, the digest), a line each for the entities with the most first and the rest
    named, never counted away: each one's line is in its device details."""
    record = make_recording()
    baseline = SessionView(deepcopy(record))
    sizes = lambda view: [len(json.dumps(x)) for x in (view.overview(), view.read_turn(1), view.digest)]  # noqa: E731
    before = sizes(baseline)
    record["state_changes"] += [
        _change(f"sensor.reading_{n % 400}", str(n), str(n + 1), new_attributes={"unit_of_measurement": "W"},
                at=f"2026-01-01T00:00:02.{n:06d}+00:00") for n in range(2000)]
    view = SessionView(record)
    turn = view.read_turn(1)
    assert len(turn["readings_elsewhere"]) == READINGS_SHOWN and len(turn["more_entities_with_readings"]) == 400 - 25
    named = {r["entity_id"] for r in turn["readings_elsewhere"]} | set(turn["more_entities_with_readings"])
    assert named == {f"sensor.reading_{n}" for n in range(400)} == {
        r["entity_id"] for r in view.digest["readings"]} | set(view.digest["more_entities_with_readings"])
    last = turn["more_entities_with_readings"][-1]
    record["turns"][0]["source"]["states"][last] = {"state": "0", "attributes": {"unit_of_measurement": "W"}}
    assert SessionView(record).device_details(last)["readings"]["readings"] == 5
    assert all(after - was < 20_000 for after, was in zip(sizes(view), before, strict=True))


@pytest.mark.asyncio
async def test_a_recording_keeps_the_voice_assistant_its_requests_ran_through(hass, monkeypatch, effect_guards):
    """The voice assistant (Assist pipeline) a conversation ran through is kept with it, as the voice device it was
    asked through chooses it (its Assistant select; "preferred": Home Assistant's preferred one): its settings say
    what Assist may do (handle commands itself first, say). Where the device can't say (asked from the app, a device
    with an assistant per wake word, a turn Home Assistant answered itself), Home Assistant's own log of its runs does,
    whose intent step names the conversation; with neither, none is kept: the household's view names the assistant by
    the AI that answered."""
    from types import MappingProxyType, SimpleNamespace
    import homeassistant.components.assist_pipeline as assist_pipeline
    from homeassistant.components.assist_pipeline.pipeline import KEY_ASSIST_PIPELINE
    from homeassistant.components.conversation import AssistantContent, ConversationInput
    from homeassistant.components.conversation.chat_log import async_get_chat_log
    from homeassistant.config_entries import ConfigEntry, ConfigEntryState
    from homeassistant.util.ulid import ulid_now
    from homeassistant.helpers.chat_session import async_get_chat_session
    from conftest import assist_home
    await assist_home(hass, {})
    def pipeline(pipeline_id, name):  # As Home Assistant keeps one, an invented setting among it.
        return SimpleNamespace(id=pipeline_id, name=name, to_json=lambda: {
            "id": pipeline_id, "name": name, "conversation_engine": "conversation.household", "invented": [1]})
    pipelines = [pipeline("p-nova", "Nova"), pipeline("p-home", "Home")]
    monkeypatch.setattr(assist_pipeline, "async_get_pipelines", lambda hass: pipelines)
    monkeypatch.setattr(assist_pipeline, "async_get_pipeline", lambda hass, pipeline_id=None: next(
        (p for p in pipelines if p.id == pipeline_id), pipelines[1]))
    hass.config.components.add("assist_pipeline")
    runs = {}  # Home Assistant's log of its last runs, by pipeline.
    monkeypatch.setitem(hass.data, KEY_ASSIST_PIPELINE, SimpleNamespace(pipeline_debug=runs))

    def ran(pipeline_id):
        """A new conversation, run through this pipeline: its run's intent step names it."""
        conversation_id = f"asked-{ulid_now()}"  # Not a ULID: Home Assistant keeps it, as one it was given.
        runs.setdefault(pipeline_id, {})[ulid_now()] = SimpleNamespace(events=[
            SimpleNamespace(type="intent-start", data={"conversation_id": conversation_id})])
        return conversation_id
    satellites = ConfigEntry(domain="esphome", title="Satellites", data={}, source="user", version=1,
                             minor_version=1, options={}, unique_id=None, discovery_keys=MappingProxyType({}),
                             state=ConfigEntryState.LOADED, subentries_data=[])
    hass.config_entries._entries[satellites.entry_id] = satellites  # As HA's own test helpers add one.

    selects = {}

    def device(name, chosen):
        """A voice device with an Assistant select per wake word, set to these assistants."""
        made = dr.async_get(hass).async_get_or_create(config_entry_id=satellites.entry_id,
                                                      identifiers={("esphome", name)}, name=name)
        for n, assistant in enumerate(chosen):
            entity = er.async_get(hass).async_get_or_create(
                "select", "esphome", f"{name}-{n}", device_id=made.id, config_entry=satellites,
                translation_key="pipeline_n" if n else "pipeline")
            selects.setdefault(name, entity.entity_id)
            hass.states.async_set(entity.entity_id, assistant)
        return made.id

    recorder = Recorder(hass)
    await recorder.start()

    async def asked(device_id, conversation_id=None, ai=True, api="assist"):
        said = ConversationInput(text="Lights on", context=Context(), conversation_id=conversation_id,
                                 device_id=device_id, satellite_id=None, language="en",
                                 agent_id="conversation.household")
        with async_get_chat_session(hass, conversation_id, context=said.context) as session, \
                async_get_chat_log(hass, session, said) as chat_log:
            if ai:  # As an AI replies: through async_add_assistant_content.
                await chat_log.async_provide_llm_data(said.as_llm_context("conversation.household"), api)
                async for _ in chat_log.async_add_assistant_content(AssistantContent(
                        agent_id="conversation.household", content="Done.")):
                    pass
            else:  # Home Assistant's own reply.
                chat_log.async_add_assistant_content_without_tools(AssistantContent(
                    agent_id="conversation.home_assistant", content="Done."))
        return session.conversation_id

    def kept(conversation_id):
        return recorder.sessions[recorder.keys[conversation_id]].get("pipeline_id")
    kitchen, hall = device("kitchen", ["Nova"]), device("hall", ["preferred"])
    in_the_kitchen = await asked(kitchen)
    assert kept(in_the_kitchen) == "p-nova" and kept(await asked(hall)) == "p-home"  # With no run in the log.
    turn = recorder.sessions[recorder.keys[in_the_kitchen]]["turns"][0]  # Each turn keeps it whole, as it ran.
    assert turn["pipeline"] == pipelines[0].to_json()
    hass.states.async_set(selects["kitchen"], "Home")
    await asked(kitchen, in_the_kitchen)
    assert kept(in_the_kitchen) == "p-nova"  # Its first, kept.
    bedroom = device("bedroom", ["Nova", "Home"])
    assert kept(await asked(bedroom)) is None  # Which wake word: not known to the device, nor in the log.
    assert kept(await asked(bedroom, ran("p-home"))) == "p-home"
    assert kept(await asked(None, ran("p-nova"))) == "p-nova"  # The app or a browser: no voice device.
    assert kept(await asked(kitchen, ran("p-home"), api=None)) == "p-home"  # An AI without Assist: device not known.
    assert kept(await asked(device("porch", ["Nova"]), ran("p-nova"), ai=False)) == "p-nova"  # HA answered.
    assert kept(await asked(None)) is None  # An action's request: no voice assistant.
    monkeypatch.setitem(hass.data, KEY_ASSIST_PIPELINE, SimpleNamespace(pipeline_debug=None))  # HA changed its log.
    assert kept(await asked(None, ran("p-nova"))) is None and recorder.capture_error is None
    await recorder.stop()


@pytest.mark.asyncio
async def test_saved_when_a_turn_is_added_when_it_closes_and_otherwise_every_15_seconds(
        home, saves, monkeypatch, effect_guards):
    hass = home
    monkeypatch.setattr(recorder_module, "SAVE_DELAY", 3600)  # The test ticks by hand.
    recorder, key = await started(hass)
    await recorder.flush(due_only=True)
    assert saves == [key]  # A new recording with its first turn.
    hass.states.async_set("light.desk", "on")
    await hass.async_block_till_done()
    await recorder.flush(due_only=True)
    assert saves == [key] and key in recorder.dirty  # A change waits...
    recorder.saved[key] -= SAVE_EVERY
    await recorder.flush(due_only=True)
    assert saves == [key] * 2 and not recorder.dirty  # ...until the last save is SAVE_EVERY seconds old.
    recorder.on_chat("io", "content_added", {"content": {"role": "user", "content": "Again"}})
    await recorder.flush(due_only=True)
    assert saves == [key] * 3  # A new turn is saved at once.
    hass.states.async_set("light.desk", "off")
    await hass.async_block_till_done()
    recorder.on_chat("io", "deleted", {})
    await recorder.flush(due_only=True)
    assert saves == [key] * 4 and key not in recorder.sessions  # Closing saves it, packed, and it leaves memory.
    stored = await recorder.store(key).async_load()
    assert stored["packed"] and stored["status"] == "session_expired"
    assert [c["new"]["state"] for c in stored["state_changes"]] == ["on", "off"]
    await recorder.stop()
    assert effect_guards == []


@pytest.mark.asyncio
async def test_the_timer_saves_a_change_once_it_is_due(home, saves, monkeypatch, effect_guards):
    hass = home
    monkeypatch.setattr(recorder_module, "SAVE_DELAY", 0.01)
    monkeypatch.setattr(recorder_module, "SAVE_EVERY", 0.5)
    recorder, key = await started(hass)
    await asyncio.sleep(0.1)
    assert saves == [key]
    hass.states.async_set("light.desk", "on")
    await asyncio.sleep(0.05)
    assert saves == [key] and recorder.timer is not None
    await asyncio.sleep(0.7)
    assert saves == [key] * 2 and not recorder.dirty and recorder.timer is None  # No chat log to watch: no ticks.
    await recorder.stop()
    assert effect_guards == []


def test_a_save_gets_a_copy_that_later_changes_do_not_reach():
    source = {"states": {"light.desk": {"state": "off"}}}
    session = {"status": "active", "events": [{"type": "created"}], "turns": [{"user": {}, "source": source}],
               "state_changes": [], "limitations": []}
    frozen = _frozen(session)
    expected = deepcopy(frozen)
    session["status"] = "session_expired"
    session["events"].append({"type": "deleted"})
    session["state_changes"].append({"entity_id": "light.desk"})
    session["limitations"].append("capture_error:ValueError")
    session["turns"][-1]["prepared"] = {"context": {}}
    session["turns"][-1].setdefault("preparations", []).append({})
    session["turns"].append({"user": {}, "source": {}})
    assert frozen == expected
    assert frozen["turns"][0]["source"] is source  # What's recorded is shared, not copied.


@pytest.mark.asyncio
async def test_recordings_serialize_off_the_event_loop(home):
    assert Recorder(home).store("any")._serialize_in_event_loop is False


@pytest.mark.asyncio
async def test_a_readers_copy_is_made_off_the_event_loop(home, monkeypatch, effect_guards):
    """Each reader gets its own copy of the whole recording, unpacked: megabytes to copy, so off the event loop, for a
    recording still going (read from memory) and a stored one alike."""
    from test_retention import on_the_event_loop
    on_loop, unpack = [], recorder_module.unpack

    def watched(recording):
        on_loop.append(on_the_event_loop())
        return unpack(recording)
    monkeypatch.setattr(recorder_module, "unpack", watched)
    recorder, key = await started(home)
    live = await recorder.export(key)
    live["turns"][0]["user"]["content"] = "Changed by a reader"
    assert recorder.sessions[key]["turns"][0]["user"]["content"] == "Hello"  # The reader's own copy.
    recorder.on_chat("io", "deleted", {})
    await recorder.flush()
    assert key not in recorder.sessions and (await recorder.export(key))["turns"][0]["user"]["content"] == "Hello"
    assert on_loop == [False, False]
    await recorder.stop()
    assert effect_guards == []


@pytest.mark.asyncio
async def test_deleting_an_unsaved_recording_leaves_nothing_to_save(home, effect_guards):
    hass = home
    recorder, key = await started(hass)
    await recorder.delete(key)
    await recorder.flush()
    assert await recorder.store(key).async_load() is None
    assert key not in recorder.index and not recorder.dirty and recorder.last_error is None
    await recorder.stop()
    assert effect_guards == []


@pytest.mark.asyncio
async def test_home_assistants_cleanup_of_a_role_chat_log_records_nothing(improver):
    """Each AI role's call runs in a chat session of Home Assistant's, which HA cleans up minutes later, outside the
    role's call: none of them is a conversation of the household's. After a round through every stage, HA's cleanup
    six minutes on adds nothing to the recorder's index."""
    from datetime import timedelta
    from homeassistant.helpers.chat_session import DATA_CHAT_SESSION_CLEANUP
    from homeassistant.util.dt import utcnow
    await improver.arrives(improver.friction)
    improver.client.blocks = before(improver.friction) + candidate_turn()
    run = await improver.run()
    assert run["status"] == "done", run["error"]
    recorder = improver.runtime.recorder
    known = set(recorder.index)
    improver.hass.data[DATA_CHAT_SESSION_CLEANUP]._cleanup(utcnow() + timedelta(minutes=6))
    await improver.hass.async_block_till_done()
    assert sorted(set(recorder.index) - known) == []


@pytest.mark.asyncio
async def test_home_assistants_cleanup_ends_a_household_conversation_whatever_context_set_its_timer(home):
    """HA's chat-session cleanup runs in the context its timer was set in: an AI role's, when a role's call opened the
    first chat session (as each run's triage does). A household conversation it cleans up then still ends its
    recording."""
    from datetime import timedelta
    from homeassistant.components import conversation
    from homeassistant.components.conversation.chat_log import async_get_chat_log
    from homeassistant.helpers.chat_session import DATA_CHAT_SESSION_CLEANUP, async_get_chat_session
    from homeassistant.util.dt import utcnow
    from custom_components.assist_improver.reflective import role_marker
    hass = home
    recorder = Recorder(hass)
    await recorder.start()
    said = conversation.ConversationInput(text="Turn on the desk", context=Context(), conversation_id=None,
                                          device_id=None, satellite_id=None, language="en",
                                          agent_id="conversation.household")
    with async_get_chat_session(hass, context=said.context) as session, \
            async_get_chat_log(hass, session, said) as chat_log:
        chat_log.async_add_assistant_content_without_tools(
            conversation.AssistantContent(agent_id="conversation.household", content="Done."))
    [key] = recorder.index
    with role_marker("triage"):
        hass.data[DATA_CHAT_SESSION_CLEANUP]._cleanup(utcnow() + timedelta(minutes=6))
    await hass.async_block_till_done()
    assert recorder.index[key]["status"] == "session_expired"
    await recorder.stop()


@pytest.mark.asyncio
async def test_a_turns_home_is_read_as_the_words_come_and_serialized_off_the_event_loop(home, monkeypatch,
                                                                                         effect_guards):
    """Each turn's snapshot of the home is what Home Assistant held as the user's words came, read then on the event
    loop and serialized (megabytes in a large home) in the executor, then attached to that turn: a change right after
    the words isn't in it, and a reader's copy or a save waits for it."""
    from test_retention import on_the_event_loop
    from custom_components.assist_improver import snapshot as snapshot_module
    on_loop, record = [], snapshot_module.state_record

    def watched(state):
        on_loop.append(on_the_event_loop())
        return record(state)
    monkeypatch.setattr(snapshot_module, "state_record", watched)
    recorder, key = await started(home)
    home.states.async_set("light.desk", "on")  # Right after the words.
    recorder.on_chat("io", "content_added", {"content": {"role": "user", "content": "Again"}})
    assert recorder.index[key]["updated"] == recorder.sessions[key]["last_activity"]  # The index keeps just the time.
    first, second = (await recorder.export(key))["turns"]
    assert first["source"]["states"]["light.desk"]["state"] == "off"
    assert second["source"]["states"]["light.desk"]["state"] == "on"
    assert first["source"]["at"] <= second["source"]["at"] and second["user"]["content"] == "Again"
    recorder.on_chat("io", "content_added", {"content": {"role": "user", "content": "Once more"}})
    await recorder.flush()
    stored = await recorder.store(key).async_load()
    assert [t["user"]["content"] for t in stored["turns"] if t.get("source")] == ["Hello", "Again", "Once more"]
    assert on_loop and not any(on_loop)
    await recorder.stop()
    assert effect_guards == []


@pytest.mark.asyncio
async def test_a_watched_chat_log_is_read_again_only_once_it_grew(home, monkeypatch, effect_guards):
    """While a conversation goes on, the recorder watches its chat log for what Home Assistant adds without saying (a
    final reply, at times): it reads it again only once the log grew, not every second for minutes."""
    from homeassistant.components.conversation.chat_log import AssistantContent, ChatLog, UserContent
    recorder, key = await started(home)
    log, reads = ChatLog(home, "io"), []
    log.content.append(UserContent(content="Hello"))
    as_dict = log.as_dict
    monkeypatch.setattr(log, "as_dict", lambda: reads.append(len(log.content)) or as_dict())
    recorder.logs[key] = log  # As the recorder holds it once the assistant prepares its answer.
    for _ in range(3):
        await recorder.flush(due_only=True)
    assert reads == [2]
    log.content.append(AssistantContent(agent_id="conversation.household", content="Hi."))
    for _ in range(3):
        await recorder.flush(due_only=True)
    assert reads == [2, 3]
    assert recorder.sessions[key]["chat_log"]["content"][-1]["content"] == "Hi."  # What it added is kept.
    await recorder.stop()
    assert effect_guards == []


@pytest.mark.asyncio
async def test_what_isnt_kept_of_a_conversation_goes_once_home_assistant_ends_it(home, effect_guards):
    """A chat log another integration's AI Task prepares isn't one of the household's conversations: once Home
    Assistant ends it, nothing of it stays (its ID, or that it's ignored), however many there are. A household
    conversation's ID stays while its recording does, and goes with it; one queued for deletion as it went on leaves
    nothing once both are done."""
    recorder = Recorder(home)
    await recorder.start()
    tasks = [f"task-{n}" for n in range(3)]
    for conversation_id in tasks:
        recorder.on_chat(conversation_id, "created", {"chat_log": {"content": []}})
        recorder.on_chat(conversation_id, "updated", {"chat_log": {"content": [{"role": "system", "content": "x"}]}})
    await home.async_block_till_done()
    assert recorder.ignored == set(tasks) and not recorder.index
    for conversation_id in tasks:
        recorder.on_chat(conversation_id, "deleted", {})
    assert recorder.ignored == set() and recorder.keys == {}
    recorder.on_chat("io", "created", {"chat_log": {"content": []}})
    recorder.on_chat("io", "content_added", {"content": {"role": "user", "content": "Hello"}})
    recorder.on_chat("io", "deleted", {})
    await recorder.flush()
    key = recorder.keys["io"]
    assert recorder.index[key]["status"] == "session_expired" and key not in recorder.sessions
    await recorder.delete(key)
    assert recorder.keys == {}
    recorder.on_chat("queued", "created", {"chat_log": {"content": []}})  # Queued for deletion as it goes on.
    recorder.on_chat("queued", "content_added", {"content": {"role": "user", "content": "Hello"}})
    await recorder.flush()
    await recorder.forget_later(queued := recorder.keys["queued"])
    recorder.on_chat("queued", "deleted", {})
    assert await recorder.forget_queued() == [queued]
    assert recorder.ignored == set() and recorder.keys == {} and not recorder.index
    await recorder.stop()
    assert effect_guards == []
