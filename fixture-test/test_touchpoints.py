"""What the improver relies on in Home Assistant beyond its public API (touchpoints.py): each resolves on the installed
Home Assistant, none is imported where the integration loads, the integration sets up without any one of them, and a
break is never silent: logged once, shown as its feature's Repairs notice, which goes once the feature works again."""

import ast
import importlib
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import probatio
import pytest
import pytest_asyncio
from homeassistant.components import conversation
from homeassistant.core import Context
from homeassistant.helpers import chat_session, entity_registry as er, issue_registry as ir

from anthropic_fixture import anthropic_agent
from conftest import assist_home
from recordings import REPLAY_ENTITIES, before
from test_ai_roles import anthropic_entry, register_task
from test_interception import TARGET, register_api
from test_milestone1 import fake_entry, recording_hass, request  # noqa: F401 - recording_hass is a fixture.
from test_simulate import pressed, recording, run, setup
from custom_components.assist_improver import (ai_setup, async_setup_entry, async_unload_entry, recorder,
                                               simulate, touchpoints)
from custom_components.assist_improver.const import DOMAIN

PACKAGE = Path(touchpoints.__file__).parent


def unresolved():
    """Each touchpoint the installed Home Assistant doesn't have as the improver relies on it, and where it's used."""
    failures = []
    for name, point in touchpoints.TOUCHPOINTS.items():
        try:
            touchpoints.resolve(name)
        except touchpoints.Broken as err:
            failures.append(f"{name}, used in {point.used_in}: {err.reason}")
    return failures


def test_every_touchpoint_resolves_on_this_home_assistant():
    failures = unresolved()
    assert not failures, "Home Assistant changed what Assist Improver relies on:\n" + "\n".join(failures)


def _owner(path):
    """What holds a touchpoint, and its name there."""
    module, _, attribute = path.partition(":")
    *parents, name = attribute.split(".")
    found = importlib.import_module(module)
    for part in parents:
        found = getattr(found, part)
    return found, name


@pytest.mark.parametrize("name", sorted(touchpoints.TOUCHPOINTS))
def test_a_touchpoint_home_assistant_removed_fails_the_check_naming_it_and_where_its_used(name, monkeypatch):
    point = touchpoints.TOUCHPOINTS[name]
    monkeypatch.delattr(*_owner(point.path))
    assert f"{name}, used in {point.used_in}: {point.path} is missing" in unresolved()


def test_a_parameter_home_assistant_dropped_fails_the_check_too(monkeypatch):
    from homeassistant.components.intent.timers import TimerManager
    monkeypatch.setattr(TimerManager, "start_timer", lambda self, device_id: None)
    assert ("timer_start, used in sandbox.py: homeassistant.components.intent.timers:TimerManager.start_timer has no "
            "conversation_command, conversation_agent_id") in unresolved()


def test_no_home_assistant_name_beyond_its_public_api_is_imported():
    """A component's public API is its root's __all__; anything else is a touchpoint, loaded where it's used."""
    found = []
    for path in sorted(PACKAGE.rglob("*.py")):
        where = path.relative_to(PACKAGE)
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                found += [f"{where}:{node.lineno} {alias.name}" for alias in node.names
                          if alias.name.startswith("homeassistant.components.") and alias.name.count(".") > 2]
            elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith("homeassistant.components."):
                if node.module.count(".") > 2:
                    found.append(f"{where}:{node.lineno} {node.module}")
                    continue
                public = getattr(importlib.import_module(node.module), "__all__", ())
                found += [f"{where}:{node.lineno} {node.module}.{alias.name}" for alias in node.names
                          if alias.name not in public]
    assert found == []


def _issues(hass):
    return sorted(issue for domain, issue in ir.async_get(hass).issues if domain == DOMAIN
                  and issue.startswith("update_"))


def _gone(monkeypatch, name):
    """Home Assistant without this touchpoint."""
    point = touchpoints.TOUCHPOINTS[name]
    monkeypatch.setitem(touchpoints.TOUCHPOINTS, name, point._replace(path=point.path + "_gone"))


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(touchpoints.TOUCHPOINTS))
async def test_the_integration_sets_up_without_any_one_touchpoint(recording_hass, monkeypatch, name):
    """Only the feature it serves stops: the improver loads with its actions, and a break setup came across shows as
    its feature's notice, gone once the improver unloads."""
    hass = recording_hass
    _gone(monkeypatch, name)
    entry = fake_entry()
    assert await async_setup_entry(hass, entry) is None
    try:
        assert entry.runtime_data.recorder is not None and hass.services.has_service(DOMAIN, "status")
        broken = hass.data.get(touchpoints.BROKEN) or {}
        assert set(broken) <= {name}
        assert _issues(hass) == ([f"update_{touchpoints.TOUCHPOINTS[name].feature}"] if broken else [])
    finally:
        await async_unload_entry(hass, entry)
    assert _issues(hass) == [] and touchpoints.BROKEN not in hass.data


@pytest.mark.asyncio
async def test_a_constant_home_assistant_moved_stops_only_what_reads_it_and_is_reported(recording_hass, monkeypatch):
    """The file Home Assistant's script editor keeps, and how long HA puts a device's report down to a request, are
    read where they're used: one HA moved stops only what reads it, and shows its feature's notice."""
    from custom_components.assist_improver import changeset, simulation
    hass = recording_hass
    for name in ("script_config_path", "recent_context"):
        _gone(monkeypatch, name)
    with pytest.raises(touchpoints.Broken):
        await changeset.read(hass, "script", "fixture_press")
    turn = {"source": {"at": "2026-11-05T10:00:00+00:00"}, "ended": "2026-11-05T10:00:05+00:00", "prepared": {}}
    with pytest.raises(touchpoints.Broken):
        simulation.recorded_transitions({"state_changes": []}, turn)
    assert set(hass.data[touchpoints.BROKEN]) == {"script_config_path", "recent_context"}
    assert _issues(hass) == ["update_changes", "update_replays"]


@pytest.mark.asyncio
async def test_a_break_is_logged_once_shown_once_per_feature_and_goes_when_its_check_passes(recording_hass,
                                                                                            monkeypatch, caplog):
    hass = recording_hass
    touchpoints.report(hass, "chat_log_events", "turn 1 was recorded without what its AI was given")
    touchpoints.report(hass, "chat_log_events", "turn 2 was recorded without what its AI was given")
    touchpoints.report(hass, "pipeline_debug", "reading it failed with KeyError")
    logged = [r.getMessage() for r in caplog.records if r.name == touchpoints.__name__ and r.levelno == logging.ERROR]
    assert len(logged) == 2 and "chat_log_events, which recorder.py, sandbox.py uses, stopped working" in logged[0]
    assert "Assist Improver needs an update for this Home Assistant version" in logged[0]
    assert _issues(hass) == ["update_recording"]
    touchpoints.ok(hass, "chat_log_events")
    assert _issues(hass) == ["update_recording"]  # Its feature still has a break.
    touchpoints.ok(hass, "pipeline_debug")
    assert _issues(hass) == []
    _gone(monkeypatch, "conversation_trace")
    with pytest.raises(touchpoints.Broken, match="conversation_trace"):
        touchpoints.get(hass, "conversation_trace")
    touchpoints.ok(hass, "conversation_trace")  # Still missing: only an update brings it back.
    assert _issues(hass) == ["update_usage"]


def _ai(hass, answers):
    """An AI with Assist's tools, answering with these texts; ask() has it answer one request (in a conversation of
    its own, unless given one's ID) and gives the conversation's ID."""
    agent, client, subentry = anthropic_agent(hass)
    client.blocks = [[{"type": "text", "text": text}] for text in answers]
    provider_entry = SimpleNamespace(entry_id="fixture-entry", subentries={"fixture": SimpleNamespace(
        subentry_type="conversation", subentry_id=subentry.subentry_id, title=subentry.title,
        data=dict(subentry.data))}, data={})
    hass.config_entries.async_entries = lambda domain=None: [provider_entry] if domain == "anthropic" else []
    api = register_api(hass)
    hass.auth = SimpleNamespace(async_get_user=AsyncMock(return_value=SimpleNamespace(name="Fixture user")))
    hass.states.async_set(TARGET, "off", {"options": ["off", "high"]})

    async def ask(conversation_id=None):
        context = Context(user_id="fixture-user", parent_id="fixture-parent")
        api.worlds[context.id], api.calls[context.id] = {TARGET: "off"}, []
        result = await agent.async_process(request(agent, context, conversation_id))
        await hass.async_block_till_done()
        return result.conversation_id
    return agent, ask


@pytest.mark.asyncio
async def test_a_turn_recorded_without_what_its_ai_was_given_is_reported_until_one_is(recording_hass, monkeypatch,
                                                                                       effect_guards, caplog):
    """A replay needs what each turn's AI was given; should Home Assistant stop telling the recorder (no update
    event, as for a turn it gave the last turn's prompt, nor a chat log that says it), the recording is kept and the
    break reported, once."""
    hass = recording_hass
    _, ask = _ai(hass, [f"Answer {n}." for n in range(3)])
    silent = [True]
    hear, given = recorder.Recorder.on_chat, recorder._as_given
    monkeypatch.setattr(recorder.Recorder, "on_chat", lambda self, conversation_id, kind, data: None if (
        silent[0] and kind == "updated") else hear(self, conversation_id, kind, data))
    monkeypatch.setattr(recorder, "_as_given", lambda chat_log: None if silent[0] else given(chat_log))
    entry = fake_entry()
    await async_setup_entry(hass, entry)
    try:
        await ask()
        await ask()
        assert len(entry.runtime_data.recorder.index) == 2  # Still recorded.
        assert _issues(hass) == ["update_recording"]
        assert [r.getMessage() for r in caplog.records if r.name == touchpoints.__name__][0].startswith(
            "chat_log_events, which recorder.py, sandbox.py uses, stopped working")
        assert len([r for r in caplog.records if r.name == touchpoints.__name__]) == 1
        silent[0] = False
        await ask()
        assert _issues(hass) == []
    finally:
        await async_unload_entry(hass, entry)


@pytest.mark.asyncio
async def test_a_turn_something_else_answered_after_the_ai_isnt_checked(recording_hass, effect_guards):
    """A sentence trigger or Home Assistant's own intents answering a later turn of the AI's conversation: the AI was
    given nothing for it, and nothing is reported."""
    hass = recording_hass
    agent, ask = _ai(hass, ["First answer.", "Last answer."])
    entry = fake_entry()
    await async_setup_entry(hass, entry)
    try:
        conversation_id = await ask()
        for answers in ("sentence_trigger", conversation.HOME_ASSISTANT_AGENT):  # As assist_pipeline adds them.
            words = conversation.ConversationInput(
                text="Good night", context=Context(user_id="fixture-user"), conversation_id=conversation_id,
                device_id="fixture-phone", satellite_id=None, language="en", agent_id=agent.entity_id)
            with chat_session.async_get_chat_session(hass, conversation_id, context=words.context) as session, \
                    conversation.async_get_chat_log(hass, session, words) as chat_log:
                chat_log.async_add_assistant_content_without_tools(
                    conversation.AssistantContent(agent_id=answers, content="Good night!"))
            await hass.async_block_till_done()
        assert _issues(hass) == [] and touchpoints.BROKEN not in hass.data
        await ask(conversation_id)
        [session] = entry.runtime_data.recorder.sessions.values()
        assert ["prepared" in turn for turn in session["turns"]] == [True, False, False, True]
    finally:
        await async_unload_entry(hass, entry)


@pytest.mark.asyncio
async def test_a_chat_log_event_without_what_the_recorder_reads_is_reported(recording_hass):
    hass = recording_hass
    entry = fake_entry()
    await async_setup_entry(hass, entry)
    try:
        entry.runtime_data.recorder.on_chat("fixture-conversation", "content_added", {"contents": {"role": "user"}})
        assert hass.data[touchpoints.BROKEN] == {
            "chat_log_subscribe": "a content_added event came without its content"}
        assert _issues(hass) == ["update_recording"]
    finally:
        await async_unload_entry(hass, entry)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["not kept", "not asked"])
async def test_a_setting_a_new_ai_task_lost_is_reported_until_one_keeps_them_all(recording_hass, change):
    """Setting up an AI Task reads back what its provider's flow kept: a setting the flow dropped, or no longer asks
    for (Home Assistant renamed it), is reported; the new AI Task is still used."""
    hass = recording_hass
    entry = anthropic_entry({"recommended": True})
    register_task(hass, entry)
    dropped = ["effort"]

    async def init(handler, context):
        asked = ("name", "recommended", "chat_model", "max_tokens", "effort")
        if change == "not asked":
            asked = tuple(key for key in asked if key not in dropped)
        return {"type": "form", "flow_id": "f", "errors": None,
                "data_schema": probatio.Schema({probatio.Optional(key): object for key in asked})}

    async def configure(flow_id, answer):
        sid = f"sub-{len(entry.subentries) + 1}"
        kept = {key: value for key, value in answer.items() if key != "name" and key not in dropped}
        entry.subentries = {**entry.subentries, sid: SimpleNamespace(subentry_type="ai_task_data", data=kept)}
        er.async_get(hass).async_get_or_create("ai_task", "anthropic", f"uid-{sid}", config_entry=entry,
                                               config_subentry_id=sid, suggested_object_id=f"new_{sid}")
        hass.states.async_set(f"ai_task.new_{sid.replace('-', '_')}", "unknown")
        return {"type": "create_entry"}
    hass.config_entries.subentries = SimpleNamespace(async_init=init, async_configure=configure)
    assert await ai_setup.create_ai_task(hass, entry.entry_id, "recommended") == ("ai_task.new_sub_2", None)
    assert hass.data[touchpoints.BROKEN] == {"ai_task_flow": "a new AI Task didn't keep effort"}
    assert _issues(hass) == ["update_ai_models"]
    dropped.clear()
    assert await ai_setup.create_ai_task(hass, entry.entry_id, "recommended") == ("ai_task.new_sub_3", None)
    assert _issues(hass) == []


@pytest_asyncio.fixture
async def replay_hass(hass):
    return await assist_home(hass, REPLAY_ENTITIES)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["sandbox_exposure", "chat_log_trace"])  # As the home is seeded; as a turn runs.
async def test_a_break_in_a_replays_worker_stops_it_as_the_machinerys_fault_never_a_verdict(replay_hass, monkeypatch,
                                                                                            effect_guards, name):
    """The replay ends as cut off (tried again on a later run), never as unsupported or not reproducible, and the real
    instance shows the notice."""
    hass = replay_hass
    _, client, runtime = await setup(hass, monkeypatch, recording())
    _gone(monkeypatch, name)
    with pytest.raises(simulate.Transient, match=name):
        await run(hass, runtime)
    [kept] = runtime.replays.items.values()
    assert kept["status"] == "infrastructure_failure" and kept["error"]["type"] == "Broken"
    assert client.requests == [] and _issues(hass) == ["update_replays"]


@pytest.mark.asyncio
async def test_a_provider_that_stops_reporting_a_replays_usage_shows_the_usage_notice_and_the_test_goes_on(
        replay_hass, monkeypatch, effect_guards):
    """A replay hears what the provider reports of each turn on the turn's chat log: a turn the model finished with
    no usage, from a provider that reported it on its replays before (or on the replay's other turns), means it
    stopped reporting it there. A provider that never reported it (Ollama, OpenRouter) is no break; one that reports
    it again takes the notice away."""
    from homeassistant.components.anthropic import entity
    hass = replay_hass
    record = recording()
    _, client, runtime = await setup(hass, monkeypatch, record)
    real, silent = entity.AnthropicDeltaStream.trace_usage, lambda stream, chat_log: None
    for reports, issues in ((silent, []), (real, []), (silent, ["update_usage"]), (real, [])):
        monkeypatch.setattr(entity.AnthropicDeltaStream, "trace_usage", reports)
        runtime.replays.items = {f"earlier-{k}": v for k, v in runtime.replays.items.items()}  # Run again, kept.
        client.blocks = before(record) + pressed(1, 4)
        test = await run(hass, runtime)
        assert test["validated"] and test["before"]["status"] == "reproduced" and _issues(hass) == issues
        if issues:
            assert hass.data[touchpoints.BROKEN]["provider_usage"] == "anthropic reported no usage for a replay's turn"


@pytest.mark.asyncio
@pytest.mark.parametrize("moved", ["groups", "policy"])
async def test_a_person_home_assistant_no_longer_reads_as_relied_on_stops_the_replay_with_the_notice(
        replay_hass, monkeypatch, effect_guards, moved):
    """The real instance reads the person speaking before their replay starts: a user record without their groups, or
    HA's control policy moved, stops the replay as the machinery's fault, with the notice."""
    from homeassistant.auth.permissions import const
    hass = replay_hass
    _, client, runtime = await setup(hass, monkeypatch, recording())
    if moved == "groups":
        hass.auth.async_get_user.return_value = SimpleNamespace(name="Fixture user", is_admin=False)
    else:
        monkeypatch.delattr(const, "POLICY_CONTROL")
    with pytest.raises(simulate.Transient, match="auth_user"):
        await run(hass, runtime)
    [kept] = runtime.replays.items.values()
    assert kept["status"] == "infrastructure_failure" and kept["error"]["type"] == "Broken"
    assert client.requests == [] and _issues(hass) == ["update_replays"]
