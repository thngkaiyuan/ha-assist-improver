"""A recording keeps what Home Assistant has, whole (DETAILS P8.2): "fix all those problems ... as general and as
broadly and cleanly as possible, instead of ... specific patches for each problem" (Kai, 11-05). No list of what to
keep: each object as Home Assistant itself represents it, so a field no list names is kept too. No secrets layer:
credentials are a config entry's data, which nothing reads; what leaves Home Assistant has its credentials and
locations cut."""

import json
from types import MappingProxyType
from unittest.mock import patch

import attr
import pytest
from homeassistant.components.conversation import ConversationInput
from homeassistant.components.conversation import chat_log as ha_chat_log
from homeassistant.components.conversation.chat_log import AssistantContent, ToolResultContent
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import Context, ServiceRegistry
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.chat_session import async_get_chat_session
from homeassistant.helpers.json import json_bytes

from conftest import load_registries
from test_changeset import _original_async_call
from test_spec_retention_recording import listening, only_key, say  # noqa: F401 - listening is a fixture.
from custom_components.assist_improver import recorder as recorder_module
from custom_components.assist_improver.exploration import SessionView
from custom_components.assist_improver.replay import setup_now, today
from custom_components.assist_improver.serialization import REDACTED, exported
from custom_components.assist_improver.snapshot import provider_config, snapshot


def provider(domain, settings, entry_class=ConfigEntry):
    """A provider's config entry, its credential in its data, with one conversation subentry of these settings."""
    return entry_class(domain=domain, title="Provider", data={"api_key": "DO_NOT_RECORD"}, source="user", version=1,
                       minor_version=1, options={}, unique_id=None, discovery_keys=MappingProxyType({}),
                       state=ConfigEntryState.LOADED, subentries_data=[
                           {"subentry_type": "conversation", "title": "Assistant", "unique_id": None,
                            "subentry_id": f"{domain}-assistant", "data": settings}])


def public_fields(cls):
    return {f.name for f in attr.fields(cls) if not f.name.startswith("_")}


@pytest.mark.asyncio
async def test_a_recording_is_home_assistants_own_representation_a_field_no_list_names_among_it(hass, monkeypatch):
    """A State as State.as_dict has it; entity and device registry entries with every field their classes
    have (a child device naming its parent); a conversation subentry's data; hass.config as its as_dict has it; the
    voice assistant as Pipeline.to_json has it: each with something invented, which no list could name."""
    from homeassistant.components import assist_pipeline
    from homeassistant.components.assist_pipeline.models import Pipeline
    await load_registries(hass)
    entry = provider("fixture", {"prompt": "Be brief.", "invented": {"nested": [1]}})
    hass.config_entries._entries[entry.entry_id] = entry
    devices, entities = dr.async_get(hass), er.async_get(hass)
    hub = devices.async_get_or_create(config_entry_id=entry.entry_id, identifiers={("fixture", "hub")}, name="Hub",
                                      serial_number="S-1", hw_version="invented-hardware",
                                      connections={(dr.CONNECTION_NETWORK_MAC, "aa:bb:cc:dd:ee:ff")})
    port = devices.async_get_or_create_child(config_entry_id=entry.entry_id, identifiers={("fixture", "hub-1")},
                                             name="Port 1", parent_device_id=hub.id)
    meter = entities.async_get_or_create("sensor", "fixture", "power", device_id=hub.id,
                                         capabilities={"invented": [1]}, original_name="Power")
    entities.async_update_entity_options(meter.entity_id, "invented_integration", {"invented": True})
    hass.states.async_set(meter.entity_id, "5", {"unit_of_measurement": "W", "invented": {"nested": [1, 2]}})
    config = hass.config.as_dict()
    monkeypatch.setattr(type(hass.config), "as_dict", lambda self: {**config, "invented": "a setting"})
    source = snapshot(hass)

    assert source["states"][meter.entity_id] == json.loads(hass.states.get(meter.entity_id).as_dict_json)
    recorded = source["entities"][meter.entity_id]
    assert set(recorded) == public_fields(er.RegistryEntry)
    assert recorded["capabilities"] == {"invented": [1]}
    assert recorded["options"]["invented_integration"] == {"invented": True}
    assert set(source["devices"][hub.id]) == public_fields(dr.DeviceEntry)
    assert (source["devices"][hub.id]["serial_number"], source["devices"][hub.id]["hw_version"]) == (
        "S-1", "invented-hardware")
    assert set(source["devices"][port.id]) == public_fields(dr.ChildDeviceEntry)
    assert source["devices"][port.id]["parent_device_id"] == hub.id
    assert source["config"] == json.loads(json_bytes({**config, "invented": "a setting"}))
    assert provider_config(hass, "fixture")[0]["options"] == {"prompt": "Be brief.", "invented": {"nested": [1]}}

    pipeline = Pipeline(conversation_engine="conversation.fixture", conversation_language="en", language="en",
                        name="Home", stt_engine=None, stt_language=None, tts_engine=None, tts_language=None,
                        tts_voice=None, wake_word_entity=None, wake_word_id=None)
    whole = {**pipeline.to_json(), "invented": "a setting"}
    monkeypatch.setattr(Pipeline, "to_json", lambda self: whole)
    monkeypatch.setattr(assist_pipeline, "async_get_pipeline", lambda hass, pipeline_id=None: pipeline)
    assert recorder_module._pipeline_record(hass, pipeline.id) == json.loads(json.dumps(whole))


@pytest.mark.asyncio
async def test_every_setting_of_an_openai_assistant_is_recorded(hass):
    """An assistant's settings are its subentry's data, kept whole: OpenAI's, every one its integration names
    (a recording once kept only the settings Anthropic and OpenRouter name, and dropped 11 of OpenAI's)."""
    from homeassistant.components.openai_conversation import const
    await load_registries(hass)
    settings = {value: f"set {value}" for name, value in vars(const).items()
                if name.startswith("CONF_") and isinstance(value, str)}
    entry = provider("openai_conversation", settings)
    hass.config_entries._entries[entry.entry_id] = entry
    assert provider_config(hass, "openai_conversation")[0]["options"] == settings
    assert len(settings) > 20


class Untouchable(ConfigEntry):
    """Another integration's config entry: its data and options (credentials, say) fail the test if read."""

    def _read(self):
        raise AssertionError(f"{self.domain}'s config entry data or options were read")

    data = property(_read, lambda self, value: None)
    options = property(_read, lambda self, value: None)


@pytest.mark.asyncio
async def test_no_config_entrys_data_or_options_are_read_but_the_answering_providers_handed_on_unread(
        listening, effect_guards):
    """Other integrations' entries, whose data and options can't be touched, leave recording, today's setup and
    the views working; the answering provider's entry is the only one that reaches a replay's worker, as Home
    Assistant stores it; and an export, which may be posted publicly, cuts credentials and locations wherever they
    are (a secret resolved into a script, an assistant's settings, the home's, a tracker's and a phone's coordinates)
    and nothing an AI setting needs, while the store keeps them whole."""
    from homeassistant.setup import async_setup_component
    hass = listening.hass
    for domain in ("fixture_lock_cloud", "openai_conversation"):
        other = provider(domain, {"prompt": "Theirs."}, Untouchable)
        hass.config_entries._entries[other.entry_id] = other
    with pytest.raises(AssertionError):
        other.options  # noqa: B018 - touching it is the test.
    settings = {"prompt": "Be brief.", "llm_hass_api": ["assist"], "chat_model": "fixture-model", "max_tokens": 1024,
                "temperature": 0.5, "api_key": "sk-in-the-subentry"}
    mine = provider("fixture", settings)
    hass.config_entries._entries[mine.entry_id] = mine
    er.async_get(hass).async_get_or_create("conversation", "fixture", "assistant", suggested_object_id="fixture",
                                           config_entry=mine, config_subentry_id="fixture-assistant")
    hass.states.async_set("camera.porch", "idle", {"access_token": "t0k3n",
                                                   "entity_picture": "/api/camera_proxy/camera.porch?token=t0k3n"})
    hass.states.async_set("device_tracker.fixture_phone", "not_home", {"latitude": 51.5072, "longitude": -0.1276,
                                                                        "gps_accuracy": 12})
    hass.states.async_set("sensor.fixture_phone_geocoded_location", "Somewhere", {"Location": [40.7128, -74.006]})
    hass.config.latitude, hass.config.longitude = 48.8566, 2.3522
    step = {"action": "rest_command.fixture", "data": {"headers": {"Authorization": "Bearer resolved-from-secrets"}}}
    assert await async_setup_component(hass, "script", {"script": {"call_out": {"sequence": [step]}}})
    await say(hass, "kitchen-chat", "Is the kitchen light on?", "No, it's off.", api="assist")
    hass.states.async_set("camera.porch", "recording", {"access_token": "t0k3n"})
    recording = await listening.export(only_key(listening))
    source = recording["turns"][0]["source"]
    assert recording["turns"][0]["prepared"]["provider_config"][0]["options"] == settings
    assert source["states"]["camera.porch"]["attributes"]["access_token"] == "t0k3n"
    assert source["scripts"]["call_out"]["sequence"] == [step]
    assert (source["config"]["latitude"], source["config"]["longitude"]) == (48.8566, 2.3522)
    assert source["states"]["device_tracker.fixture_phone"]["attributes"]["latitude"] == 51.5072
    assert source["states"]["sensor.fixture_phone_geocoded_location"]["attributes"]["Location"] == [40.7128, -74.006]
    assert (await setup_now(hass))["home"]["states"]["camera.porch"]
    SessionView({**recording, "status": "session_expired"}).overview()
    assistant = (await today(hass, "conversation.fixture"))["assistant"]["entry"]
    assert (assistant["entry_id"], assistant["data"]) == (mine.entry_id, {"api_key": "DO_NOT_RECORD"})
    shared = exported(recording)
    for secret in ("t0k3n", "sk-in-the-subentry", "resolved-from-secrets", "51.5072", "-0.1276", "48.8566", "2.3522",
                   "40.7128", "-74.006"):
        assert secret not in json.dumps(shared), secret
    source = shared["turns"][0]["source"]
    assert source["states"]["camera.porch"]["attributes"] == {
        "access_token": REDACTED, "entity_picture": "/api/camera_proxy/camera.porch"}
    assert shared["turns"][0]["prepared"]["provider_config"][0]["options"] == {**settings, "api_key": REDACTED}
    assert source["scripts"]["call_out"]["sequence"][0]["data"] == {"headers": {"Authorization": REDACTED}}
    assert (source["config"]["latitude"], source["config"]["longitude"]) == (REDACTED, REDACTED)
    assert source["states"]["device_tracker.fixture_phone"]["attributes"] == {
        "latitude": REDACTED, "longitude": REDACTED, "gps_accuracy": 12}
    assert source["states"]["sensor.fixture_phone_geocoded_location"]["attributes"] == {"Location": REDACTED}
    assert effect_guards == []


def test_an_export_cuts_an_api_key_however_its_key_is_spelt_and_no_ai_setting():
    """A header's X-API-Key, apikey and access_key name a credential as api_key does; an AI setting names none."""
    credentials = {"X-API-Key": "k1", "apikey": "k2", "access_key": "k3"}
    settings = {"chat_model": "fixture-model", "max_tokens": 1024, "temperature": 0.5, "token_count": 3}
    assert exported({**credentials, **settings}) == {**dict.fromkeys(credentials, REDACTED), **settings}


@pytest.mark.asyncio
async def test_a_scripts_failing_step_is_the_call_its_error_goes_on(listening, effect_guards):
    """The action calls under a request are kept with its turn, each inside the call it was made in, and a tool's
    error goes on the last call made since Assist called it: inside a script, the script's own failing step, not the
    script (DETAILS P8.2.10)."""
    context, recording = await a_scripts_failing_step(listening)
    assert [(a["action"], a["data"], a["context"]["id"], a.get("under"), a.get("error"))
            for a in recording["turns"][0]["actions"]] == [
        ("script.arrive", {}, context.id, None, None),
        ("fixture.unlock", {"entity_id": "lock.gate"}, context.id, 0, None),
        ("fixture.open", {"entity_id": "cover.gate"}, context.id, 0, "Jammed")]


async def a_scripts_failing_step(listening):
    """A turn where Assist ran a script whose second step failed, as the recorder keeps it: its request's context and
    the recording."""
    from homeassistant.helpers.llm import ToolInput, ToolResult
    from homeassistant.setup import async_setup_component
    hass = listening.hass

    async def jammed(call):
        raise HomeAssistantError("Jammed")
    hass.services.async_register("fixture", "unlock", lambda call: None)
    hass.services.async_register("fixture", "open", jammed)
    assert await async_setup_component(hass, "script", {"script": {"arrive": {"sequence": [
        {"action": "fixture.unlock", "data": {"entity_id": "lock.gate"}},
        {"action": "fixture.open", "data": {"entity_id": "cover.gate"}}]}}})
    context = Context(user_id="alex")
    said = ConversationInput(text="I'm home.", context=context, conversation_id="arrival", device_id="fixture-phone",
                             satellite_id=None, language="en", agent_id="conversation.fixture")
    with (async_get_chat_session(hass, "arrival", context=context) as session,
          ha_chat_log.async_get_chat_log(hass, session, said) as log):
        await log.async_provide_llm_data(said.as_llm_context("fixture"), "assist")
        log.async_add_assistant_content_without_tools(AssistantContent(
            agent_id="conversation.fixture", tool_calls=[ToolInput("script_arrive", {}, id="call-1", external=True)]))
        with patch.object(ServiceRegistry, "async_call", _original_async_call()), pytest.raises(HomeAssistantError):
            await hass.services.async_call("script", "arrive", blocking=True, context=context)
        log.async_add_assistant_content_without_tools(ToolResultContent(
            agent_id="conversation.fixture", tool_call_id="call-1", tool_name="script_arrive",
            result=ToolResult(data={"error": "HomeAssistantError", "error_text": "Jammed"}, error=True)))
        log.async_add_assistant_content_without_tools(AssistantContent(agent_id="conversation.fixture",
                                                                       content="The gate is jammed."))
    await hass.async_block_till_done()
    return context, await listening.export(only_key(listening))


@pytest.mark.asyncio
async def test_an_error_home_assistant_raised_before_announcing_a_call_goes_on_no_call(listening, effect_guards):
    """A script's second step whose data Home Assistant's schema refuses is never announced, so it's not in the call
    log: its error goes on no call, not on the step before it, which ran (DETAILS P8.2.10). An error on a call keeps
    its type, as the chat log names it."""
    import probatio
    from homeassistant.helpers.llm import ToolInput, ToolResult
    from homeassistant.setup import async_setup_component
    from custom_components.assist_improver.recorder import acted
    hass = listening.hass
    rang = []
    hass.services.async_register("fixture", "press", lambda call: rang.append(call))
    hass.services.async_register("fixture", "set_minutes", lambda call: None,
                                 schema=probatio.Schema({"value": float}, extra=probatio.ALLOW_EXTRA))
    assert await async_setup_component(hass, "script", {"script": {"dryer_done": {"sequence": [
        {"action": "fixture.press", "data": {"entity_id": "button.chime"}},
        {"action": "fixture.set_minutes", "data": {"value": "half an hour"}}]}}})
    context = Context(user_id="alex")
    said = ConversationInput(text="Ring the chime.", context=context, conversation_id="dryer", device_id="fixture-phone",
                             satellite_id=None, language="en", agent_id="conversation.fixture")
    with (async_get_chat_session(hass, "dryer", context=context) as session,
          ha_chat_log.async_get_chat_log(hass, session, said) as log):
        await log.async_provide_llm_data(said.as_llm_context("fixture"), "assist")
        log.async_add_assistant_content_without_tools(AssistantContent(
            agent_id="conversation.fixture", tool_calls=[ToolInput("script_dryer_done", {}, id="call-1", external=True)]))
        with patch.object(ServiceRegistry, "async_call", _original_async_call()), pytest.raises(probatio.Invalid) as err:
            await hass.services.async_call("script", "dryer_done", blocking=True, context=context)
        log.async_add_assistant_content_without_tools(ToolResultContent(
            agent_id="conversation.fixture", tool_call_id="call-1", tool_name="script_dryer_done",
            result=ToolResult(data={"error": type(err.value).__name__, "error_text": str(err.value)}, error=True)))
        log.async_add_assistant_content_without_tools(AssistantContent(agent_id="conversation.fixture",
                                                                       content="It didn't work."))
    await hass.async_block_till_done()
    actions = (await listening.export(only_key(listening)))["turns"][0]["actions"]
    assert rang and [(a["action"], a.get("error")) for a in actions] == [("script.dryer_done", None),
                                                                        ("fixture.press", None)]
    jammed = [{"action": "fixture.open", "data": {}}]
    acted(jammed, {"role": "tool_result", "result": {"error": True, "data": {
        "error": "HomeAssistantError", "error_text": "Jammed"}}}, 0)
    assert jammed == [{"action": "fixture.open", "data": {}, "error": "Jammed", "error_type": "HomeAssistantError"}]


@pytest.mark.asyncio
async def test_a_turn_like_the_last_keeps_the_calls_its_tools_make_before_its_answer(listening, effect_guards):
    """Home Assistant gives a turn like the last one the same prompt and tools without telling the recorder, and an
    AI's tools run while its answer is still coming: a call made then is kept with its turn all the same, found by the
    chat log it's made in (DETAILS P8.2.10)."""
    hass = listening.hass
    hass.services.async_register("fixture", "unlock", lambda call: None)
    asked = []
    for words in ("Unlock the gate.", "Unlock it again."):
        asked.append(context := Context(user_id="alex"))
        said = ConversationInput(text=words, context=context, conversation_id="gate", device_id="fixture-phone",
                                 satellite_id=None, language="en", agent_id="conversation.fixture")
        with (async_get_chat_session(hass, "gate", context=context) as session,
              ha_chat_log.async_get_chat_log(hass, session, said) as log):
            await log.async_provide_llm_data(said.as_llm_context("fixture"), "assist")
            with patch.object(ServiceRegistry, "async_call", _original_async_call()):
                await hass.services.async_call("fixture", "unlock", {"entity_id": "lock.gate"}, blocking=True,
                                               context=context)
            log.async_add_assistant_content_without_tools(AssistantContent(agent_id="conversation.fixture",
                                                                           content="Unlocked."))
    await hass.async_block_till_done()
    turns = (await listening.export(only_key(listening)))["turns"]
    assert [t["prepared"]["context"]["context"]["id"] for t in turns] == [c.id for c in asked]
    assert [[(a["action"], a["context"]["id"]) for a in t["actions"]] for t in turns] == [
        [("fixture.unlock", c.id)] for c in asked]


@pytest.mark.asyncio
async def test_an_answer_while_a_recording_has_no_turn_is_reported_unless_it_started_mid_session(
        listening, effect_guards):
    """Home Assistant 2026.12 brings a conversation's first words with its chat log, which the recorder reads as the
    turn they start. Should a later version bring them some other way, an answer comes while the recording has no
    turn: reported (chat_log_events), never kept silently without its words. A recording that started mid-session
    (its conversation began before the recorder did) has no turn for an answer to earlier words: nothing's reported."""
    from custom_components.assist_improver import touchpoints
    from custom_components.assist_improver.recorder import Recorder
    from test_spec_retention_recording import as_in_2026_12
    hass = listening.hass
    late = Recorder(hass)
    await late.start()
    late.on_chat("begun-earlier", "content_added", {"content": {
        "role": "assistant", "agent_id": "conversation.fixture", "content": "Done."}})
    assert "chat_log_events" not in hass.data.get(touchpoints.BROKEN, {})
    await late.stop()

    def wordless(on_chat):  # Its first words, which 2026.12 brings with the created event, dropped.
        def heard(conversation_id, kind, data):
            if str(kind) == "created":
                log = data["chat_log"]
                data = {**data, "chat_log": {**log, "content": [c for c in log["content"] if c["role"] != "user"]}}
            on_chat(conversation_id, kind, data)
        return heard
    newer = Recorder(hass)
    newer.on_chat = as_in_2026_12(wordless(newer.on_chat))
    await newer.start()
    try:
        await say(hass, "kitchen-chat", "Is the kitchen light on?", "No, it's off.", api="assist")
    finally:
        await newer.stop()
    assert hass.data[touchpoints.BROKEN]["chat_log_events"] == "an answer came while the recording had no turn"
    assert effect_guards == []


@pytest.mark.asyncio
async def test_a_voice_satellite_starting_a_conversation_isnt_reported_as_an_answer_with_no_turn(
        listening, effect_guards):
    """A voice satellite can start a conversation itself (assist_satellite.start_conversation): Home Assistant opens a
    new chat log and adds the satellite's words as the assistant's, before anyone has spoken. That's no answer from
    the AI to words the recorder missed: nothing's reported, and the reply is the recording's first turn."""
    from custom_components.assist_improver import touchpoints
    hass = listening.hass
    with (async_get_chat_session(hass, context=None) as session,  # As the satellite entity does it.
          ha_chat_log.async_get_chat_log(hass, session) as log):
        log.async_add_assistant_content_without_tools(AssistantContent(
            agent_id="assist_satellite.hall", content="The washing is done. Shall I turn on the dryer?"))
    await hass.async_block_till_done()
    assert "chat_log_events" not in hass.data.get(touchpoints.BROKEN, {})
    await say(hass, session.conversation_id, "Yes, please.", "Done.", api="assist")
    recording = await listening.export(only_key(listening))
    assert [t["user"]["content"] for t in recording["turns"]] == ["Yes, please."]
    assert "chat_log_events" not in hass.data.get(touchpoints.BROKEN, {})
    assert effect_guards == []


@pytest.mark.asyncio
async def test_home_assistants_error_for_an_ai_it_couldnt_set_up_isnt_reported_as_a_turn_kept_incomplete(
        listening, effect_guards):
    """On Home Assistant 2026.12 (core cfbae777b) a turn whose AI can't be set up gets Home Assistant's "Error: ..."
    as its answer, the AI given nothing this turn: the recorder keeps the turn and reports nothing, as the check of
    what the AI was given is only for a turn whose words it was given (recorder._given_this_turn)."""
    from custom_components.assist_improver import touchpoints
    hass = listening.hass
    await say(hass, "kitchen-chat", "Is the kitchen light on?", "No, it's off.", api="assist")
    context = Context(user_id="alex")
    said = ConversationInput(text="And the hall?", context=context, conversation_id="kitchen-chat",
                             device_id="fixture-phone", satellite_id=None, language="en",
                             agent_id="conversation.fixture")
    with (async_get_chat_session(hass, "kitchen-chat", context=context) as session,
          ha_chat_log.async_get_chat_log(hass, session, said) as log):
        assert log.llm_api is not None  # The last turn's, still on the chat log.
        log.async_add_assistant_content_without_tools(AssistantContent(
            agent_id="conversation.fixture", content="Error: Error preparing LLM API"))
    await hass.async_block_till_done()
    recording = await listening.export(only_key(listening))
    assert [t["user"]["content"] for t in recording["turns"]] == ["Is the kitchen light on?", "And the hall?"]
    assert "chat_log_events" not in hass.data.get(touchpoints.BROKEN, {})
    assert effect_guards == []
