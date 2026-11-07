"""Integration lifecycle, capture, persistence and real Anthropic adapter offline."""

from copy import deepcopy
from pathlib import Path
import json
import shutil
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conftest import load_registries, loaded_for
from custom_components.assist_improver import async_setup_entry, async_unload_entry
from custom_components.assist_improver.const import DOMAIN, VERSION
from custom_components.assist_improver.recorder import Recorder
from custom_components.assist_improver.serialization import plain
from custom_components.assist_improver.snapshot import snapshot
from homeassistant.components import conversation
from homeassistant.core import Context, HomeAssistant, ServiceCall, ServiceRegistry
from homeassistant import config_entries, loader
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.chat_session import DATA_CHAT_SESSION
from homeassistant.helpers.storage import Store
from homeassistant.exceptions import Unauthorized

from anthropic_fixture import anthropic_agent
from test_changeset import _original_async_call
from test_interception import register_api, TARGET


def fake_entry(options=None):
    """The parts of a ConfigEntry that setup uses; its tasks are Home Assistant's own."""
    return SimpleNamespace(entry_id="fixture-improver", options=options or {}, async_on_unload=lambda unsub: None,
                           add_update_listener=lambda listener: (lambda: None),
                           async_create_task=lambda hass, target, name=None, eager_start=True: hass.async_create_task(
                               target, name, eager_start),
                           async_create_background_task=lambda hass, target, name, eager_start=True:
                           hass.async_create_background_task(target, name, eager_start))


@pytest_asyncio.fixture
async def recording_hass(hass):
    if er.DATA_REGISTRY not in hass.data:  # A module's hass may already have them (conftest.assist_home).
        await load_registries(hass)
    hass.config_entries = SimpleNamespace(async_loaded_entries=loaded_for(hass), async_entries=lambda domain=None: [])
    yield hass


def request(agent, context, conversation_id=None):
    return conversation.ConversationInput(
        text="Set the fixture heater to high.", context=context,
        conversation_id=conversation_id, device_id="fixture-phone", satellite_id=None,
        language="en", agent_id=agent.entity_id,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("uses_a_tool", [True, False])
async def test_with_two_assistants_on_one_provider_a_recording_keeps_the_one_that_answered(recording_hass,
                                                                                           monkeypatch, uses_a_tool):
    """Opus and Haiku under one Anthropic entry, say: the recording keeps only the answering assistant's configuration,
    so a replay knows which to use (P8.2.3), whether or not the reply used a tool (HA sends no event for a reply itself;
    on a real Home Assistant, a plain answer left its recording ambiguous, so it couldn't be tested)."""
    hass = recording_hass
    agent, client, subentry = anthropic_agent(hass)
    if not uses_a_tool:
        client.blocks = [[{"type": "text", "text": "The kitchen light is off."}]]
    other = SimpleNamespace(subentry_type="conversation", subentry_id="other-assistant", title="Other",
                            data={"chat_model": "other-model"})
    mine = SimpleNamespace(subentry_type="conversation", subentry_id=subentry.subentry_id, title=subentry.title,
                           data=dict(subentry.data))
    provider_entry = SimpleNamespace(entry_id="fixture-entry", subentries={"a": other, "b": mine}, data={})
    hass.config_entries.async_entries = lambda domain=None: [provider_entry] if domain == "anthropic" else []
    monkeypatch.setattr("custom_components.assist_improver.recorder.subentry_of",
                        lambda hass, agent_id: subentry.subentry_id if agent_id == agent.entity_id else None)
    api = register_api(hass)
    hass.auth = SimpleNamespace(async_get_user=AsyncMock(return_value=SimpleNamespace(name="Fixture user")))
    context = Context(user_id="fixture-user", parent_id="fixture-parent")
    api.worlds[context.id] = {TARGET: "off"}
    api.calls[context.id] = []
    hass.states.async_set(TARGET, "off", {"options": ["off", "high"]})
    entry = fake_entry()
    await async_setup_entry(hass, entry)
    try:
        await agent.async_process(request(agent, context))
        await hass.async_block_till_done()
        recorder = entry.runtime_data.recorder
        exported = await recorder.export(next(iter(recorder.index)))
        assert [c["subentry_id"] for c in exported["turns"][0]["prepared"]["provider_config"]] == [subentry.subentry_id]
    finally:
        await async_unload_entry(hass, entry)


@pytest.mark.asyncio
async def test_anthropic_record_export_restart_and_unload(recording_hass, effect_guards):
    hass = recording_hass
    agent, client, subentry = anthropic_agent(hass)
    provider_entry = SimpleNamespace(entry_id="fixture-entry", subentries={"fixture": SimpleNamespace(
        subentry_type="conversation", subentry_id=subentry.subentry_id, title=subentry.title,
        data=dict(subentry.data))}, data={"api_key": "DO_NOT_RECORD_KEY"})  # The credential: the entry's, never read.
    hass.config_entries.async_entries = lambda domain=None: [provider_entry] if domain == "anthropic" else []
    api = register_api(hass)
    hass.auth = SimpleNamespace(async_get_user=AsyncMock(return_value=SimpleNamespace(name="Fixture user")))
    context = Context(user_id="fixture-user", parent_id="fixture-parent")
    api.worlds[context.id] = {TARGET: "off"}
    api.calls[context.id] = []
    hass.states.async_set(TARGET, "off", {"options": ["off", "high"], "access_token": "KEPT_AT_HOME"})
    entry = fake_entry()
    await async_setup_entry(hass, entry)
    assert hass.services.has_service(DOMAIN, "export_recording")
    before_config = deepcopy(subentry.data)
    result = await agent.async_process(request(agent, context))
    await hass.async_block_till_done()
    recorder = entry.runtime_data.recorder
    assert len(recorder.index) == 1
    key = next(iter(recorder.index))
    exported = await recorder.export(key)
    assert len(client.requests) == 2
    assert result.response.speech["plain"]["speech"] == "Fixture finished."
    assert exported["chat_log"]["content"][-1]["content"] == "Fixture finished."
    assert any(c["role"] == "tool_result" for c in exported["chat_log"]["content"])
    turn = exported["turns"][0]
    assert turn["source"]["states"][TARGET]["state"] == "off"
    assert turn["prepared"]["context"]["device_id"] == "fixture-phone"
    assert turn["prepared"]["context"]["platform"] == "anthropic"
    assert turn["prepared"]["context"]["context"] == dict(context.as_dict())
    assert turn["prepared"]["tools"][0]["name"] == "FixtureSetOption"
    assert turn["preparations"][0]["content"][-1]["role"] == "user"
    assert "DO_NOT_RECORD" not in json.dumps(exported)
    assert turn["source"]["states"][TARGET]["attributes"]["access_token"] == "KEPT_AT_HOME"  # In the household's HA.
    with patch.object(ServiceRegistry, "async_call", _original_async_call()):  # The improver's own action.
        shared = await hass.services.async_call(DOMAIN, "export_recording", {"recording_id": key}, blocking=True,
                                                return_response=True)
    assert shared["turns"][0]["source"]["states"][TARGET]["attributes"] == {  # Leaving it.
        "options": ["off", "high"], "access_token": "**REDACTED**"}
    assert subentry.data == before_config
    assert hass.states.get(TARGET).state == "off"
    assert api.worlds[context.id][TARGET] == "high"
    assert recorder.last_error is None
    # End-of-session is recorded separately from user destination/completion.
    hass.data[DATA_CHAT_SESSION][result.conversation_id].async_cleanup()
    await recorder.flush()
    assert (await recorder.export(key))["status"] == "session_expired"
    assert await async_unload_entry(hass, entry) is None
    assert not hass.services.has_service(DOMAIN, "export_recording")
    # Storage is real HA Store under config/.storage, not an in-memory substitute.
    fresh = Recorder(hass)
    await fresh.start()
    assert (await fresh.export(key)) == await recorder.export(key)
    await fresh.stop()
    assert effect_guards == []


@pytest.mark.asyncio
async def test_restart_marks_partial_and_preserves_records(recording_hass, effect_guards):
    r = Recorder(recording_hass)
    await r.start()
    r.on_chat("partial", "created", {"chat_log": {"content": []}})
    key = next(iter(r.index))
    await r.stop()
    fresh = Recorder(recording_hass)
    await fresh.start()
    record = await fresh.export(key)
    assert record["status"] == "interrupted"
    assert "capture_interrupted_by_restart" in record["limitations"]
    fresh.on_chat("partial", "created", {"chat_log": {"content": []}})
    assert len(fresh.index) == 2  # Same conversation never overwrites its old capture.
    await fresh.stop()
    assert effect_guards == []


@pytest.mark.asyncio
async def test_save_failure_retains_pending_data(recording_hass, effect_guards, monkeypatch):
    r = Recorder(recording_hass)
    await r.start()
    r.on_chat("failure", "created", {"chat_log": {"content": []}})
    key = next(iter(r.index))
    original = Store.async_save
    async def fail(*args):
        raise OSError("synthetic failure")
    monkeypatch.setattr(Store, "async_save", fail)
    await r.flush()
    assert key in r.dirty and r.last_error == "OSError"
    monkeypatch.setattr(Store, "async_save", original)
    await r.flush()
    assert not r.dirty and r.last_error is None
    assert await r.export(key)
    await r.stop()
    assert effect_guards == []


def test_serialization_is_home_assistants_own_never_an_objects_repr_and_redacts_nothing():
    """No secrets layer (Kai, 11-05: "it still stays private within the user's HA, right?"): what Home Assistant
    holds is copied as its own encoder writes it; what that can't write is named by its type, never its repr."""
    assert plain({"api_key": "secret", "nested": {"refresh_token": "secret"}}) == {
        "api_key": "secret", "nested": {"refresh_token": "secret"}}
    class Sensitive:
        def __repr__(self):
            raise AssertionError("repr must not be called")
    assert plain(Sensitive()) == {"omitted_type": "Sensitive"}
    context = Context(user_id="fixture-user", parent_id="fixture-parent", id="fixture-context")
    context.origin_event = Sensitive()
    assert plain(context) == {"id": "fixture-context", "parent_id": "fixture-parent", "user_id": "fixture-user"}
    assert plain(Context(id="anonymous")) == {"id": "anonymous", "parent_id": None, "user_id": None}


@pytest.mark.asyncio
async def test_a_decimal_is_kept_as_assists_live_context_shows_it(hass, effect_guards):
    """Home Assistant's live context shows a Decimal (an integration's temperature, say) as its text: a recording
    keeps that same text."""
    from decimal import Decimal
    from homeassistant.components.homeassistant.exposed_entities import async_expose_entity
    from homeassistant.components.homeassistant.llm import async_get_exposed_entities
    from conftest import assist_home
    from custom_components.assist_improver.snapshot import state_record
    await assist_home(hass, {})
    hass.states.async_set("water_heater.tank", "eco", {"temperature": Decimal("48.50")})
    async_expose_entity(hass, "conversation", "water_heater.tank", True)
    shown = async_get_exposed_entities(hass, "conversation")["water_heater.tank"]["attributes"]
    kept = json.loads(json.dumps(state_record(hass.states.get("water_heater.tank"))))["attributes"]
    assert shown == kept == {"temperature": "48.50"} and effect_guards == []


@pytest.mark.asyncio
async def test_aliases_preserve_computed_name_and_user_order(recording_hass):
    registry = er.async_get(recording_hass)
    entity = registry.async_get_or_create(
        "script", "fixture", "alias-fixture", original_name="Fixture press",
    )
    registry.async_update_entity(
        entity.entity_id, aliases=["All fixture relays", er.COMPUTED_NAME, "Another name"],
    )
    captured = snapshot(recording_hass)["entities"][entity.entity_id]
    assert captured["aliases"] == ["All fixture relays", None, "Another name"]  # As HA stores them: None, its name.


@pytest.mark.asyncio
async def test_state_changes_and_disabled_inventory(recording_hass, effect_guards):
    hass = recording_hass
    entity = er.async_get(hass).async_get_or_create("light", "fixture", "disabled-fixture",
        disabled_by=er.RegistryEntryDisabler.USER, original_name="Disabled fixture")
    hass.states.async_set("sensor.fixture_api_key", "STAYS_AT_HOME")
    r = Recorder(hass)
    await r.start()
    r.on_chat("fixture-state", "created", {"chat_log": {"content": []}})
    r.on_chat("fixture-state", "content_added", {"content": {"role": "user", "content": "Hello"}})
    key = next(iter(r.index))
    hass.states.async_set("light.fixture", "on")
    await hass.async_block_till_done()
    data = await r.export(key)
    assert data["turns"][0]["source"]["entities"][entity.entity_id]["disabled_by"] == "user"
    assert entity.entity_id not in data["turns"][0]["source"]["states"]  # A disabled entity has no live state.
    assert any(s["entity_id"] == "light.fixture" and s["new"]["state"] == "on" for s in data["state_changes"])
    assert data["turns"][0]["source"]["states"]["sensor.fixture_api_key"]["state"] == "STAYS_AT_HOME"  # Not redacted.
    await r.stop()
    count = len(r.index)
    r.on_chat("after-disable", "created", {})
    hass.states.async_set("light.fixture", "off")
    await hass.async_block_till_done()
    assert len(r.index) == count
    assert effect_guards == []


@pytest.mark.asyncio
async def test_restore_into_new_homeassistant_runtime(recording_hass, tmp_path, effect_guards):
    r = Recorder(recording_hass)
    await r.start()
    r.on_chat("restore-fixture", "created", {"chat_log": {"content": []}})
    key = next(iter(r.index))
    await r.stop()
    destination = tmp_path / "restored-config"
    await recording_hass.async_add_executor_job(shutil.copytree,
        Path(recording_hass.config.config_dir) / ".storage", destination / ".storage")
    restored_hass = HomeAssistant(str(destination))
    restored = Recorder(restored_hass)
    try:
        await restored.start()
        assert (await restored.export(key))["conversation_id"] == "restore-fixture"
        assert restored.index[key]["status"] == "interrupted"
    finally:
        await restored.stop()
        await restored_hass.async_stop()
    assert effect_guards == []


@pytest.mark.asyncio
async def test_a_recording_from_another_ha_version_replays_like_any_other(effect_guards, monkeypatch):
    """A replay runs on today's Home Assistant, whichever version recorded the conversation (DETAILS P6.2.4)."""
    import homeassistant.const
    from recordings import make_recording
    from custom_components.assist_improver.cases import replay_readiness
    monkeypatch.setattr(homeassistant.const, "__version__", "2099.1.0")
    readiness = replay_readiness(make_recording())  # Recorded on 2026.10.2.
    assert readiness["model_baseline_ready"] and readiness["gaps"] == []
    assert not any("2026.10.2" in n for n in readiness["notes"])
    assert effect_guards == []


@pytest.mark.asyncio
async def test_real_loader_and_config_flow(hass, effect_guards, monkeypatch):
    from custom_components.assist_improver.config_flow import AssistantImproverConfigFlow
    loader.async_setup(hass)
    hass.config_entries = config_entries.ConfigEntries(hass, {})
    await hass.config_entries.async_initialize()
    integration = await loader.async_get_integration(hass, DOMAIN)
    assert integration.version == VERSION
    assert integration.requirements == []
    flow = AssistantImproverConfigFlow()
    flow.hass = hass
    flow.handler = DOMAIN
    flow.context = {"source": "user"}
    assert (await flow.async_step_user())["type"] == "form"
    result = await flow.async_step_user({})
    assert result["type"] == "create_entry" and result["data"] == {}
    monkeypatch.setattr(hass.config_entries, "async_has_entries", lambda domain, **kwargs: domain == DOMAIN)
    again = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})  # Once only (manifest):
    assert again["type"] == "abort" and again["reason"] == "single_instance_allowed"  # Home Assistant refuses another.
    assert effect_guards == []


@pytest.mark.asyncio
async def test_admin_export_permission_boundary(recording_hass, effect_guards):
    hass = recording_hass
    entry = fake_entry()
    await async_setup_entry(hass, entry)
    try:
        # Invoke our registered read-only handler in this isolated runtime,
        # leaving the real-service-call tripwire in place.
        status = hass.services.async_services()[DOMAIN]["status"].job.target
        hass.auth = SimpleNamespace(async_get_user=AsyncMock(return_value=SimpleNamespace(is_admin=False)))
        call = ServiceCall(hass, DOMAIN, "status", context=Context(user_id="fixture-visitor"), return_response=True)
        with pytest.raises(Unauthorized):
            await status(call)
        hass.auth.async_get_user.return_value = SimpleNamespace(is_admin=True)
        assert (await status(call))["recording"] is True
    finally:
        await async_unload_entry(hass, entry)
    assert effect_guards == []


@pytest.mark.asyncio
async def test_shutdown_does_not_remove_the_fired_stop_listener_again(recording_hass, caplog):
    from homeassistant.const import EVENT_HOMEASSISTANT_STOP
    from custom_components.assist_improver import async_setup_entry, async_unload_entry
    hass = recording_hass
    entry = fake_entry(); await async_setup_entry(hass, entry)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()
    await async_unload_entry(hass, entry)
    assert "Unable to remove unknown job listener" not in caplog.text
    assert not entry.runtime_data.recorder.running


@pytest.mark.asyncio
async def test_households_get_only_the_bug_report_services(recording_hass):
    from custom_components.assist_improver import DEVELOPER_SERVICES, SERVICES
    hass = recording_hass
    entry = fake_entry({})
    await async_setup_entry(hass, entry)
    try:
        assert set(hass.services.async_services().get(DOMAIN, {})) == set(SERVICES) == {"status", "export_recording"}
    finally:
        await async_unload_entry(hass, entry)
    developer = fake_entry({"developer_services": True})  # Hidden setting: debugging and evaluation rounds.
    await async_setup_entry(hass, developer)
    try:
        assert set(hass.services.async_services().get(DOMAIN, {})) == set(SERVICES) | set(DEVELOPER_SERVICES)
    finally:
        await async_unload_entry(hass, developer)
    assert not hass.services.async_services().get(DOMAIN)


@pytest.mark.asyncio
async def test_diagnostics_say_where_time_and_space_go_without_any_conversation_or_user_id(recording_hass):
    from custom_components.assist_improver.diagnostics import async_get_config_entry_diagnostics
    hass = recording_hass
    entry = fake_entry({"recording_opt_out": ["5f0c1a2b3c4d4e5f8a9b0c1d2e3f4a5b"]})  # Left by an older version.
    await async_setup_entry(hass, entry)
    try:
        await entry.runtime_data.role_runs.record({
            "id": "r1", "kind": "pipeline", "status": "done", "trigger": "manual", "started": "2026-01-01T00:00:00+00:00",
            "progress": {"timings": {"analyze": 12.5}, "problems": [{"stage": "propose", "error": "RuntimeError: x"}]}})
        await entry.runtime_data.role_runs.record({"id": "c1", "role": "journey_analyzer", "status": "failed",
                                                   "started": "2026-01-01T00:00:01+00:00", "seconds": 4.2,
                                                   "error": {"message": "Rate limited"}})
        report = await async_get_config_entry_diagnostics(hass, entry)
        assert set(report) >= {"versions", "settings", "models", "storage", "recordings", "runs", "role_calls",
                               "sandboxes", "improvements", "household", "recorder"}
        assert report["runs"][-1]["progress"]["timings"] == {"analyze": 12.5}
        assert report["role_calls"]["journey_analyzer"] == {"calls": 1, "failed": 1, "seconds": 4.2, "slowest": 4.2,
                                                           "errors": ["Rate limited"]}
        assert "recordings" in report["storage"] and "started" in report["sandboxes"]
        assert "5f0c1a2b3c4d4e5f8a9b0c1d2e3f4a5b" not in json.dumps(report, default=str)
        assert report["settings"] == {k: v for k, v in entry.options.items() if k != "recording_opt_out"}
    finally:
        await async_unload_entry(hass, entry)
