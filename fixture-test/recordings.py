"""The suite's shared recordings: the fixture relay's four-turn conversation (make_recording), with the household's
full assistant profile (prepared_recording); a recording as the recorder keeps it, with the scripts and automations
the household had (as_kept); its assistant set up today and bound to the scripted provider (bind_agent), and that
assistant doing what a recording shows (before: Simulate's replay without the change)."""

from copy import deepcopy
from datetime import datetime, timedelta, UTC
from types import SimpleNamespace

from custom_components.assist_improver.cases import recording_turns

TARGET = "switch.fixture_relay"
SCRIPT = "script.fixture_press"
# What the offline HA exposes for replays; the script stays unexposed, as recorded.
REPLAY_ENTITIES = {TARGET: ("on", "Fixture relay")}
PROFILE = {"chat_model": "fixture-no-model", "prompt": "Recorded template",
           "llm_hass_api": ["assist"], "max_tokens": 3000, "effort": "low",
           "code_execution": True, "tool_search": True, "web_search": True,
           "web_fetch": True, "prompt_caching": "prompt", "recommended": False}


def make_recording():
    """Toggle the fixture relay (off), ask about the press script, turn it back on, then off and on again."""
    clock = datetime(2026, 1, 1, tzinfo=UTC)
    def tick():
        nonlocal clock
        clock += timedelta(seconds=1)
        return clock.isoformat()
    def state(entity, value):
        return {"entity_id": entity, "state": value, "attributes": {"friendly_name": "Fixture relay" if entity == TARGET else "Fixture press"}}
    schema = {"type": "object", "properties": {"name": {"type": "string"},
                "domain": {"type": "array", "items": {"type": "string"}}}, "additionalProperties": False}
    tools = [{"name": f"intent__HassTurn{direction}", "description": f"Turn {direction}",
              "integration": "intent", "parameters": schema} for direction in ["On", "Off"]]
    tools.append({"name": "homeassistant__GetLiveContext", "description": "Get recorded state", "integration": "homeassistant",
                  "parameters": {"type": "object", "properties": {"domain": {"type": "string"}}, "additionalProperties": False}})
    messages = [{"role": "system", "content": "Frozen instructions and fixture inventory."}]
    record = {"format": 1, "id": "synthetic-recording", "core_version": "2026.10.2", "status": "session_expired",
              "limitations": [], "turns": [], "state_changes": [], "chat_log": {"content": messages}}
    current = "on"
    for number, (text, directions) in enumerate([
        ("Toggle the fixture relay.", ["Off"]),
        ("Use the press script.", []),
        ("Turn it back on.", ["On"]),
        ("Again, off then on.", ["Off", "On"]),
    ], 1):
        user = {"role": "user", "content": text, "created": tick()}; messages.append(user)
        source = {"at": tick(), "states": {TARGET: state(TARGET, current), SCRIPT: state(SCRIPT, "off")},
                  "entities": {e: {"name": n, "aliases": [None], "area_id": "fixture_area"}  # As snapshot.py records them.
                               for e, n in ((TARGET, "Fixture relay"), (SCRIPT, "Fixture press"))},
                  "exposure": {TARGET: True, SCRIPT: False}, "devices": {}, "areas": {"fixture_area": {"name": "Fixture room"}}}
        prepared = {"context": {"platform": "anthropic", "context": {"id": f"fixture-context-{number}", "parent_id": None, "user_id": "fixture-user"},
                                "language": "en", "assistant": "conversation", "device_id": "fixture-phone"},
                    "tools": deepcopy(tools), "provider_config": [{"options": {"chat_model": "fixture-no-model"}}],
                    "provider_model_capabilities": {}}
        record["turns"].append({"user": deepcopy(user), "source": source, "prepared": prepared,
                                 "preparations": [{"content": deepcopy(messages)}]})
        for direction in directions:
            call = {"tool_name": f"intent__HassTurn{direction}", "tool_args": {"name": "Fixture relay", "domain": ["switch"]},
                    "id": f"call-{number}-{direction}", "external": False}
            messages.append({"role": "assistant", "agent_id": "conversation.fixture", "tool_calls": [call], "created": tick()})
            after = direction.lower()
            record["state_changes"].append({"entity_id": TARGET, "at": tick(), "old": state(TARGET, current), "new": {
                **state(TARGET, after), "context": deepcopy(prepared["context"]["context"])}})
            current = after
            result = {"data": {"speech": {}, "response_type": "action_done", "data": {
                "success": [{"name": "Fixture relay", "type": "entity", "id": TARGET}], "failed": []}}, "error": False}
            messages.append({"role": "tool_result", "agent_id": "conversation.fixture", "tool_call_id": call["id"], "tool_name": call["tool_name"], "result": result, "created": tick()})
        messages.append({"role": "assistant", "agent_id": "conversation.fixture", "content": f"Recorded response {number}.", "created": tick()})
    return record


def prepared_recording():
    """make_recording with the household's full assistant profile (PROFILE) and enough tools for HA's provider
    tool search."""
    record = make_recording()
    record["limitations"] = ["provider_native_history_omitted; replay adapter must check compatibility"]
    for turn in record["turns"]:
        turn["prepared"]["provider_config"] = [{"config_entry_id": "fixture-entry",
            "subentry_id": "fixture-anthropic", "options": deepcopy(PROFILE)}]
        # Enough tools to activate HA's actual provider tool-search path.
        turn["prepared"]["tools"] += [{"name": f"FixtureExtra{i}", "description": "Unused fixture tool",
            "integration": "fixture", "parameters": {"type": "object", "properties": {}}} for i in range(8)]
    return record


# The relay as Home Assistant's entity registry knows it: what changing its entity ID keeps.
RELAY_IDENTITY = {"platform": "fixture", "config_entry_id": "relay-entry", "device_id": "relay-device",
                  "original_name": "Relay"}


def relay_renamed(record, new):
    """(record, today's home): the relay in each turn's home with its registry identity (RELAY_IDENTITY), and the home
    as the household has it today, the relay's entity ID changed to new in Home Assistant's settings: everywhere,
    the household's scripts too, its identity kept (at_home's home)."""
    import json
    for turn in record["turns"]:
        turn["source"]["entities"][TARGET].update(RELAY_IDENTITY)
    source = record["turns"][0]["source"]
    home = {k: deepcopy(source[k]) for k in ("states", "entities", "exposure", "scripts") if k in source}
    return record, json.loads(json.dumps(home).replace(f'"{TARGET}"', f'"{new}"'))


def as_kept(hass, record):
    """A recording as the recorder keeps it: each turn with the scripts and automations the household had then (one
    that has its own keeps them)."""
    from custom_components.assist_improver.snapshot import _definitions
    scripts, automations = _definitions(hass)
    for turn in record["turns"]:
        turn["source"].setdefault("scripts", deepcopy(scripts))
        turn["source"].setdefault("automations", deepcopy(automations))
    return record


async def bind_agent(hass, record, today=None):
    """The recording's assistant as the household has it in Home Assistant today: its provider's entry and
    conversation subentry (the recorded ones), set up with the settings it ran with, or today's (today: its options)
    when given, and the agent. A replay's sandbox loads it from there, as it's set up now. Its model is this test's
    scripted provider (conftest.provider: client.blocks are its replies, client.requests what it was asked), returned
    with the agent."""
    from types import MappingProxyType
    import conftest
    from homeassistant import config_entries
    from homeassistant.helpers import entity_registry as er
    from homeassistant.config_entries import HANDLERS
    from homeassistant.loader import async_get_integration
    provider = record["turns"][0]["prepared"]["provider_config"][0]
    domain = "open_router" if record["turns"][0]["prepared"]["context"].get("platform") == "open_router" else "anthropic"
    agent_id = next(m["agent_id"] for t in recording_turns(record) for m in t["messages"] if m["role"] == "assistant")
    if getattr(hass, "config_entries", None) is None:
        hass.config_entries = config_entries.ConfigEntries(hass, {})
        await hass.config_entries.async_initialize()
    from homeassistant.helpers import device_registry as dr
    for registry in (dr, er):
        if registry.DATA_REGISTRY not in hass.data:
            if registry is dr:
                dr.async_setup(hass)
            await registry.async_load(hass, load_empty=True)
    from homeassistant import loader
    if loader.DATA_INTEGRATIONS not in hass.data:
        loader.async_setup(hass)
    await (await async_get_integration(hass, domain)).async_get_platform("config_flow")
    flow = HANDLERS[domain]
    entry = config_entries.ConfigEntry(
        domain=domain, title="Household assistant", data={"api_key": "sk-fixture"}, source="user",
        version=flow.VERSION, minor_version=flow.MINOR_VERSION, options={}, unique_id=None,
        discovery_keys=MappingProxyType({}), entry_id=provider["config_entry_id"],
        subentries_data=[{"data": deepcopy(today or provider["options"]), "subentry_id": provider["subentry_id"],
                          "subentry_type": "conversation", "title": "Fixture assistant", "unique_id": None}])
    if hass.config_entries.async_get_entry(entry.entry_id) is None:  # Bound once per Home Assistant.
        hass.config_entries._entries[entry.entry_id] = entry
    entry = hass.config_entries.async_get_entry(entry.entry_id)
    er.async_get(hass).async_get_or_create("conversation", domain, provider["subentry_id"],
                                           suggested_object_id=agent_id.split(".", 1)[1], config_entry=entry,
                                           config_subentry_id=provider["subentry_id"])
    import asyncio
    client = conftest.PROVIDER
    model = "model" if domain == "open_router" else "chat_model"
    client.models = sorted({(today or provider["options"]).get(model) or "fixture-no-model",
                            provider["options"].get(model) or "fixture-no-model"})
    client.loop = asyncio.get_running_loop()
    return SimpleNamespace(entity_id=agent_id, entry=entry), client


def _made(cls, given, **defaults):
    """A registry entry made by Home Assistant's own class from a fixture's fields, every other field it needs at its
    empty value (None) or defaults'."""
    import dataclasses
    import attr
    if attr.has(cls):
        found = [(f.name, f.default is attr.NOTHING) for f in attr.fields(cls) if f.init]
    else:
        found = [(f.name, f.default is f.default_factory is dataclasses.MISSING) for f in dataclasses.fields(cls)
                 if f.init]
    names, needed = {n for n, _ in found}, {n: None for n, required in found if required}
    return cls(**{**needed, **defaults, **{k: v for k, v in given.items() if k in names}})


def kept_home(home):
    """A fixture's home with its registries as the recorder keeps them (snapshot.home): each entity, device and area
    a whole record, made by Home Assistant's own class from the fields the fixture gives (unless given, an entity's
    unique ID is its object ID, an automation's the ID of the automation of its name, as Home Assistant gives them; its
    platform "fixture", a device integration's, or its domain where Home Assistant's own integration has it: a
    script's; its name, class, unit and features as its integration gave them, its state's; its aliases its name
    alone, as Home Assistant gives a new entity), as a sandbox loads them."""
    from homeassistant.const import Platform
    from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er
    from custom_components.assist_improver.serialization import plain
    from custom_components.assist_improver.snapshot import entity_record, whole

    automations = {a.get("alias"): str(a["id"]) for a in home.get("automations") or [] if a.get("id")}

    def entity(entity_id, given):
        aliases = [er.COMPUTED_NAME if a is None else a for a in given.get("aliases", [None])]
        domain, object_id = entity_id.split(".", 1)
        shown = ((home.get("states") or {}).get(entity_id) or {}).get("attributes") or {}
        named = given.get("original_name") or shown.get("friendly_name")
        unique_id = automations.get(named, object_id) if domain == "automation" else object_id
        return entity_record(_made(er.RegistryEntry, {**given, "aliases": aliases}, entity_id=entity_id,
                                   unique_id=unique_id, platform="fixture" if domain in set(Platform) else domain,
                                   supported_features=shown.get("supported_features") or 0, has_entity_name=False,
                                   original_name=shown.get("friendly_name"),
                                   original_device_class=shown.get("device_class"),
                                   unit_of_measurement=shown.get("unit_of_measurement"),
                                   id=entity_id, created_at=datetime(2026, 1, 1, tzinfo=UTC)))

    def device(device_id, given):
        given = {**given, **{k: {tuple(i) for i in given[k]} for k in ("identifiers", "connections") if k in given}}
        return plain(whole(_made(dr.DeviceEntry, given, id=device_id)))

    def area(area_id, given):
        return plain(whole(_made(ar.AreaEntry, {**given, "aliases": set(given.get("aliases") or ())}, id=area_id)))
    return {**home, "entities": {e: entity(e, x) for e, x in (home.get("entities") or {}).items()},
            "devices": {d: device(d, x) for d, x in (home.get("devices") or {}).items()},
            "areas": {a: area(a, x) for a, x in (home.get("areas") or {}).items()}}


def recorded_today(record, services=None, **home):
    """Today's setup (replay.today's) where the household's Home Assistant is as the recording's first turn found it
    (home: what's different now, its scripts say), its registries as the recorder keeps them (kept_home): its home,
    the tools Assist had, the asking device when one of them set timers, the actions it has beyond the sandbox's own
    (services: {domain: {service: whether it answers}}), and the assistant that answered with the settings it ran
    with, its agent's registry entry as bound (bind_agent)."""
    turn = record["turns"][0]
    ctx, tools = turn["prepared"]["context"], [t["name"] for t in turn["prepared"]["tools"]]
    config = {"config_entry_id": "fixture-entry", "subentry_id": "fixture-anthropic", "title": None,
              **deepcopy(turn["prepared"]["provider_config"][0])}
    source = {**deepcopy(turn["source"]), **deepcopy(home)}
    agent = next((m["agent_id"] for t in recording_turns(record) for m in t["messages"] if m["role"] == "assistant"),
                 None)
    if agent in (source.get("entities") or {}):
        source["entities"][agent] = {**source["entities"][agent], "unique_id": config["subentry_id"],
                                     "config_entry_id": config["config_entry_id"],
                                     "config_subentry_id": config["subentry_id"]}
    today = {"home": kept_home(source), "tools": tools,
             "timers": [ctx["device_id"]] if ctx.get("device_id") and "HassStartTimer" in tools else [],
             "services": services or {}}
    today["provider"] = config
    today["assistant"] = {"entry": {"domain": ctx.get("platform") or "anthropic", "entry_id": config["config_entry_id"],
                                    "subentries": [{"subentry_id": config["subentry_id"], "subentry_type": "conversation",
                                                    "title": config["title"], "data": config["options"]}]},
                          "agent": {"entity_id": "conversation.fixture"}}
    return today


def at_home(monkeypatch, record, services=None, **home):
    """The household's Home Assistant today, as replays and code's checks read it (replay.setup_now), is as the
    recording's first turn found it, with any parts of its home given in place of those, and the actions given
    (recorded_today)."""
    from custom_components.assist_improver import replay

    async def now(hass):
        return {k: v for k, v in recorded_today(record, services, **home).items()
                if k in ("home", "tools", "timers", "services")}
    monkeypatch.setattr(replay, "setup_now", now)


async def replay(hass, monkeypatch, record, services=None, **kwargs):
    """Simulate's replay without the change (simulate.Trial, not run yet) on today's setup: the household's assistant
    as bound (bind_agent) and its home as the recording found it, with the actions given (at_home)."""
    from custom_components.assist_improver import simulate
    at_home(monkeypatch, record, services)
    plan, today = await simulate.planned(hass, record)
    return simulate.Trial(hass, plan, record, today, **kwargs)


def before(record):
    """The household's assistant replaying the conversation as it went, turn by turn (Simulate's replay without the
    change): each turn's recorded calls, with its reasoning, then a reply."""
    blocks = []
    for turn in recording_turns(record):
        if turn["calls"]:
            blocks.append([{"type": "thinking", "thinking": "Synthetic reasoning.",
                            "signature": f"synthetic-signature-{turn['number']}"}] + [
                {"type": "tool_use", "id": c["id"], "name": c["tool_name"], "input": c["tool_args"]}
                for c in turn["calls"]])
        blocks.append([{"type": "text", "text": f"Generated response {turn['number']}."}])
    return blocks
