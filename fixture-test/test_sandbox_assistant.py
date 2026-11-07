"""The household's assistant loads in a replay's sandbox as Home Assistant loads it at home: from its own config entry,
by its provider's own integration, its agent at the home's entity id, as it's set up now. Its provider's key stays in
the sandbox's worker, gone with it."""

import pytest

from conftest import assist_home, inside
from recordings import prepared_recording, recorded_today
from custom_components.assist_improver.replay import SandboxHome, assistant_of

KEY = "sk-fixture-household-key"
AGENT = "conversation.fixture"  # The recording's assistant (recordings.make_recording).


async def household_assistant(hass, domain="anthropic", today=None, **entry_fields):
    """The household's assistant, as Home Assistant keeps it: its provider's entry (the recording's provider entry and
    subentry, at the versions HA writes today) with its conversation subentry set up today (today: its data), and the
    agent's registry entry. Never set up here: only read."""
    from types import MappingProxyType
    from homeassistant.config_entries import ConfigEntry, HANDLERS
    from homeassistant.helpers import entity_registry as er
    from homeassistant.loader import async_get_integration
    await assist_home(hass, {})
    await (await async_get_integration(hass, domain)).async_get_platform("config_flow")
    flow = HANDLERS[domain]
    entry = ConfigEntry(domain=domain, title="Household assistant", data={"api_key": KEY}, source="user",
                        version=flow.VERSION, minor_version=flow.MINOR_VERSION, options={}, unique_id=None,
                        discovery_keys=MappingProxyType({}), entry_id="fixture-entry", **entry_fields,
                        subentries_data=[{"data": today or {"prompt": "Today's instructions.",
                                                            "chat_model": "fixture-no-model",
                                                            "llm_hass_api": ["assist"]},
                                          "subentry_id": "fixture-anthropic", "subentry_type": "conversation",
                                          "title": "Fixture assistant", "unique_id": None}])
    hass.config_entries._entries[entry.entry_id] = entry
    er.async_get(hass).async_get_or_create("conversation", domain, "fixture-anthropic", suggested_object_id="fixture",
                                           config_entry=entry, config_subentry_id="fixture-anthropic")
    return entry


def with_its_agent(record):
    """As the recorder keeps a home: the agent itself among its entities and states."""
    for turn in record["turns"]:
        source = turn["source"]
        source["entities"][AGENT] = {"name": None, "original_name": "Fixture assistant", "aliases": [],
                                     "area_id": None, "platform": "anthropic", "unique_id": "fixture-anthropic",
                                     "config_entry_id": "fixture-entry", "config_subentry_id": "fixture-anthropic"}
        source["states"][AGENT] = {"entity_id": AGENT, "state": "unknown",
                                   "attributes": {"friendly_name": "Fixture assistant"}}
    return record


async def loaded(hass, record):
    """The recording's home, its assistant as the household has it set up now, loaded in a sandbox."""
    today = {**recorded_today(record), "assistant": assistant_of(hass, AGENT)}
    home = SandboxHome(hass, record, record["turns"][0], "improver-replay-fixture", today)
    return home, await home.load_assistant()


def the_agent(home):
    from homeassistant.helpers.entity_component import DATA_INSTANCES
    copy = inside(home.sandbox).hass
    return copy, copy.data[DATA_INSTANCES]["conversation"].get_entity(AGENT)


@pytest.mark.asyncio
async def test_the_households_assistant_loads_in_a_sandbox_as_it_is_set_up_now(hass, provider, effect_guards):
    """Its provider's integration sets it up from its own entry with the household's key (only the provider's model
    list), the agent at the home's entity id, though the home holds that id as one of its states. Its subentry runs
    on its settings now, whatever it ran with in the conversation."""
    now = {"prompt": "Today's instructions.", "chat_model": "another-model", "web_search": True,
           "llm_hass_api": ["assist"], "unkept_setting": 1, "web_fetch_max_uses": 3}
    await household_assistant(hass, today=now)
    record = with_its_agent(prepared_recording())
    home, agent = await loaded(hass, record)
    try:
        copy, entity = the_agent(home)
        entry = copy.config_entries.async_entries("anthropic")[0]
        assert agent == AGENT and entity is not None and entity.entity_id == AGENT and entry.state.value == "loaded"
        assert dict(entry.subentries["fixture-anthropic"].data) == now
        assert set(provider.asked) == {f"GET /v1/models {KEY}"} and effect_guards == []  # The household's own key.
    finally:
        await home.stop()


@pytest.mark.asyncio
async def test_an_assistant_of_another_provider_loads_by_its_own_integration(hass, provider, effect_guards):
    """OpenRouter's integration lists its models from its own address: it loads the same way, from its entry."""
    provider.models = ["deepseek/fixture"]
    await household_assistant(hass, "open_router", today={"model": "deepseek/fixture", "prompt": "Today's.",
                                                          "llm_hass_api": ["assist"]})
    home, agent = await loaded(hass, prepared_recording())
    try:
        assert agent == AGENT and the_agent(home)[1].model == "deepseek/fixture"
        assert provider.asked == [f"GET /api/v1/models {KEY}"] and effect_guards == []
    finally:
        await home.stop()


@pytest.mark.asyncio
async def test_a_household_that_disables_new_entities_still_has_its_assistant_in_the_sandbox(hass, provider,
                                                                                            effect_guards):
    """At home the agent exists, whatever the entry says of entities still to come; in the sandbox it isn't new."""
    await household_assistant(hass, pref_disable_new_entities=True)
    home, agent = await loaded(hass, prepared_recording())
    try:
        assert agent == AGENT and the_agent(home)[1] is not None and effect_guards == []
    finally:
        await home.stop()


@pytest.mark.asyncio
async def test_the_ai_task_the_improver_adds_through_anthropics_own_flow_has_high_effort(hass, provider, effect_guards):
    """The recommended AI Task, added through Anthropic's own add-AI-Task flow (ai_setup.create_ai_task) where the
    household's Anthropic entry is set up (here, a sandbox's Home Assistant, on the scripted provider): Claude Opus
    5.5 with high effort and a 16000-token output limit, which the improver finds strong enough; reading them back
    finds nothing Home Assistant changed."""
    from custom_components.assist_improver import ai_setup, touchpoints
    provider.models = ["claude-opus-5-5", "fixture-no-model"]
    await household_assistant(hass)
    home, _ = await loaded(hass, with_its_agent(prepared_recording()))

    async def add(copy):
        entity_id, reason = await ai_setup.create_ai_task(copy, "fixture-entry", "recommended")
        [task] = [s for s in copy.config_entries.async_get_entry("fixture-entry").subentries.values()
                  if s.subentry_type == "ai_task_data"]
        return entity_id, reason, dict(task.data), ai_setup.assess(copy, entity_id), copy.data.get(touchpoints.BROKEN)
    try:
        entity_id, reason, data, assessed, broken = await inside(home.sandbox).run(add)
    finally:
        await home.stop()
    assert reason is None and entity_id.startswith("ai_task.") and not broken
    assert (data["chat_model"], data["effort"], data["max_tokens"]) == ("claude-opus-5-5", "high", 16000)
    assert assessed["ok"] and assessed["effort"] == "high" and effect_guards == []
