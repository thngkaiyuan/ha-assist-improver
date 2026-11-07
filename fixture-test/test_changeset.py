"""Applying an improvement to HA: every change kind applied, verified and undone, all-or-nothing under faults.

Runs on the offline HA with its real registries, exposure settings, script,
automation and scene editors (their endpoints, as the panel calls them: editors.py)
and config subentries. Faults are injected at every step: a write that fails, a value changed
midway, and a crash followed by a fresh start that finds the journal.
"""

import asyncio
import json
from types import MappingProxyType, SimpleNamespace

import pytest
import pytest_asyncio
from homeassistant.components.homeassistant.exposed_entities import async_expose_entity, async_should_expose
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er, issue_registry as ir
from homeassistant.setup import async_setup_component

from conftest import assist_home, serve
from editors import Connection, Editors, command, open_panel
from custom_components.assist_improver import changeset
from custom_components.assist_improver import household
from custom_components.assist_improver.changeset import Changesets, plan_steps
from custom_components.assist_improver.const import DOMAIN
from custom_components.assist_improver.improvements import NOTIFICATION, Improvements

USER = "0123456789abcdef0123456789abcdef"
PROMPT = ("You are a voice assistant for Home Assistant.\n"
          f"{{% if llm_context.context and llm_context.context.user_id == '{USER}' %}}\nAlex likes short answers.\n{{% endif %}}\n")
GARAGE = {"alias": "Press garage", "sequence": [{"action": "cover.open_cover", "target": {"entity_id": "cover.garage"}}]}


@pytest_asyncio.fixture
async def home(hass, effect_guards, monkeypatch):
    from homeassistant.core import ServiceRegistry
    guarded, original = ServiceRegistry.async_call, _original_async_call()

    async def allow_configuration(registry, domain, service, *args, **kwargs):
        # Reloading scripts, automations and scenes is configuration, not a device action: HA's own editors do it after
        # saving. Turning an automation on or off is a setting too (a change's automation_on).
        if (domain, service) in CONFIGURATION:
            return await original(registry, domain, service, *args, **kwargs)
        return await guarded(registry, domain, service, *args, **kwargs)
    monkeypatch.setattr(ServiceRegistry, "async_call", allow_configuration)
    await assist_home(hass, {"cover.garage": ("closed", "Garage door"), "light.porch": ("off", "Porch light")})
    open(hass.config.path("configuration.yaml"), "w").write(
        "script: !include scripts.yaml\nautomation: !include automations.yaml\nscene: !include scenes.yaml\n")
    open(hass.config.path("scripts.yaml"), "w").write(
        "press_garage:\n  alias: Press garage\n  sequence:\n  - action: cover.open_cover\n"
        "    target:\n      entity_id: cover.garage\n")
    open(hass.config.path("automations.yaml"), "w").write(  # The household's own: one on, one off.
        "- id: porch_at_dusk\n  alias: Porch at dusk\n  triggers:\n  - trigger: time\n    at: '18:00:00'\n"
        "  actions:\n  - action: light.turn_on\n    target:\n      entity_id: light.porch\n"
        "- id: porch_at_night\n  alias: Porch at night\n  initial_state: false\n  triggers:\n  - trigger: time\n"
        "    at: '23:00:00'\n  actions:\n  - action: light.turn_off\n    target:\n      entity_id: light.porch\n")
    open(hass.config.path("scenes.yaml"), "w").write("[]\n")
    from homeassistant import config as conf_util
    config = await conf_util.async_hass_config_yaml(hass)
    for domain in ("script", "automation", "scene"):
        assert await async_setup_component(hass, domain, config)
    await hass.async_block_till_done()
    async_expose_entity(hass, "conversation", "script.press_garage", False)
    registry = er.async_get(hass)
    assert registry.async_get_or_create("light", "recorded", "front", suggested_object_id="front").entity_id == "light.front"
    provider = ConfigEntry(domain="fixture_provider", title="Assistant", data={}, source="user", version=1,
                           minor_version=1, options={}, unique_id=None, discovery_keys=MappingProxyType({}),
                           state=ConfigEntryState.NOT_LOADED, subentries_data=[{
                               "data": {"prompt": PROMPT, "chat_model": "fixture", "llm_hass_api": ["assist"]}, "subentry_type": "conversation",
                               "title": "Assistant", "unique_id": None, "subentry_id": "sub1"}])
    hass.config_entries._entries[provider.entry_id] = provider
    study = ar.async_get(hass).async_create("Study")
    device = dr.async_get(hass).async_get_or_create(config_entry_id=provider.entry_id, identifiers={("recorded", "d1")},
                                                    name="Front light")
    registry.async_update_entity("light.front", device_id=device.id)
    return SimpleNamespace(hass=hass, provider=provider, study=study.id, device=device.id)


# The services a change may call or HA's editors call after a save: configuration, never a device action.
CONFIGURATION = {("script", "reload"), ("automation", "reload"), ("scene", "reload"), ("automation", "turn_on"),
                 ("automation", "turn_off")}


def _original_async_call():
    """HA's own service call (conftest guards it; the guard keeps a reference in its closure)."""
    from homeassistant.core import ServiceRegistry
    guard = ServiceRegistry.async_call
    for cell in guard.__closure__ or ():
        if callable(cell.cell_contents) and getattr(cell.cell_contents, "__name__", "") == "async_call":
            return cell.cell_contents
    raise AssertionError("The service guard changed")


def tested(summary="Open the garage", said=("Open the garage", "Both", "Both doors"),
           replies=("Which door?", "Which?", "Done."), after=("Done.",), met_before=True, **fields):
    """A test of a change on its conversation (records.Test, as simulate.test keeps it): one root goal, said in every
    turn without the change and in the first len(after) with it, met with it (and without it, unless not
    met_before); validated when that's a goal more met, or fewer turns."""
    root = "1:0"

    def replay(status, turns, answers, met):
        numbers = list(range(1, len(turns) + 1))
        return {"plan": status, "status": status, "said": numbers,
                "replies": [{"turn": n, "you": you, "assistant": a} for n, you, a in zip(numbers, turns, answers)],
                "goals": {root: "met" if met else "unmet"}, "goals_met": [root] if met else [],
                "turns_per_root_goal": {root: len(numbers)}, "counted": {root: numbers}, "side_effects_left": [],
                "started_from": {"at": "2026-10-01T08:00:00+00:00", "home": "home"},
                "config": {"scripts": {}, "exposed": []}, "conduct": {}}
    gains = ["goals met"] if not met_before else ["turns per root goal"] if len(after) < len(said) else []
    return {"idea": "Garage", "changes": "changes", "hypothesis": {"title": "Garage", "changes": []},
            "at": "2026-10-01T09:00:00+00:00", "judged": {root: {"summary": summary, "turn": 1, "root": root,
                                                                "devices": []}},
            "before": replay("reproduced", said, replies, met_before),
            "after": replay("done", said[:len(after)], after, True), "gains": gains, "validated": bool(gains),
            "setup": {}, "tested": {}, "provider": {}, "user_id": USER,
            "recorded": [{"you": y, "assist": a} for y, a in zip(said, replies)], "notes": [], **fields}


def proposal(home, **extra):
    """Common change kinds: an edited script, a new one (exposed), an alias that replaces none, a line for Alex; tested
    on one conversation of Alex's (tested)."""
    return {"id": "imp1", "title": "Garage", "conversation": "s1000000conversation", "round": 1, "test": tested(),
            "fixes": ["s1000000:1"], "devices": [], "changes": [
        {"kind": "script", "id": "press_garage", "config": {**GARAGE, "description": "Opens the garage door"}},
        {"kind": "script", "id": "close_garage", "config": {"alias": "Close garage", "description": "", "sequence": [
            {"action": "cover.close_cover", "target": {"entity_id": "cover.garage"}}]}},
        {"kind": "registry", "entity_id": "light.front", "aliases": ["front light"]},
        {"kind": "preference", "for": "this_user", "text": "The garage means both garage doors."}],
        "setup": {"scripts": {"press_garage": {**GARAGE, "description": "Opens the garage door"},
                              "close_garage": {"alias": "Close garage", "description": "", "sequence": [
                                  {"action": "cover.close_cover", "target": {"entity_id": "cover.garage"}}]}},
                  "exposure": {"script.press_garage": True, "script.close_garage": True},
                  "aliases": {"light.front": ["front light"]}},
        "tested": {"scripts": {"press_garage": GARAGE}, "exposure": {"script.press_garage": False,
                                                                    "script.close_garage": False}},
        "provider": {"config_entry_id": home.provider.entry_id, "subentry_id": "sub1"}, "user_ids": [USER], **extra}


async def values(home, steps):
    return {(s["kind"], s["target"]): await changeset.read(home.hass, s["kind"], s["target"]) for s in steps}


def faulty(hass, act):
    """(writer, editor): the changeset's writes, an editor item's through HA's editor for it (as the panel saves it),
    counted together from 1, in order; act(n, write) does the nth (write() writes it)."""
    count = {"n": 0}

    async def around(write):
        count["n"] += 1
        await act(count["n"], write)

    async def writer(hass, kind, target, value):
        await around(lambda: changeset.write(hass, kind, target, value))

    class Editor(Editors):
        async def save(self, kind, item_id, config):
            await around(lambda: Editors.save(self, kind, item_id, config))
    return writer, Editor(hass)


async def users_edit(hass, kind, target, value):
    """The household changes a value in Home Assistant: a script, automation or scene in its editor, the rest through
    HA's APIs."""
    if kind in ("script", "automation", "scene"):
        await changeset.saved(hass, Editors(hass).save, kind, target, value)
    else:
        await changeset.write(hass, kind, target, value)


@pytest.mark.asyncio
async def test_common_change_kinds_apply_in_a_safe_order_verify_and_undo(home):
    hass = home.hass
    steps, already, conflicts = await plan_steps(hass, proposal(home))
    assert not conflicts and not already
    # Names first, then the scripts (saved, inert until run), their exposure once HA has loaded them, and the household
    # knowledge, which every assistant on Assist reads: no assistant is switched.
    assert [s["kind"] for s in steps] == ["alias", "script", "script", "exposure", "exposure", "household"]
    before = await values(home, steps)
    editor = Editors(hass)
    result = await Changesets(hass).run("imp1", "apply", steps, editor.save)
    assert result["status"] == "done"
    # The scripts went through HA's script editor, as the panel saves them.
    assert editor.calls == [("POST", "script", "close_garage"), ("POST", "script", "press_garage")]
    assert hass.states.get("script.close_garage") is not None  # HA loaded the new script; nothing ran it.
    assert await editor.get("script", "close_garage") == steps[1]["after"]  # As HA's script editor opens it.
    assert async_should_expose(hass, "conversation", "script.close_garage")
    assert "front light" in er.async_get(hass).async_get("light.front").aliases
    assert hass.data[household.KEY].knowledge == {"everyone": [], "people": {USER: ["The garage means both garage doors."]}}
    assert home.provider.subentries["sub1"].data["llm_hass_api"] == ["assist"]
    assert home.provider.subentries["sub1"].data["prompt"] == PROMPT  # The assistant's own instructions are untouched.
    # Undo: newest first, back to exactly what was there.
    undo, left = changeset.undo_steps(result["steps"], await values(home, steps))
    assert not left
    assert (await Changesets(hass).run("imp1", "undo", undo, editor.save))["status"] == "done"
    assert editor.calls[2:] == [("POST", "script", "press_garage"), ("DELETE", "script", "close_garage")]
    assert await values(home, steps) == before
    await hass.async_block_till_done()
    assert hass.states.get("script.close_garage") is None and await editor.get("script", "close_garage") is None


DUSK = {"id": "porch_at_dusk", "alias": "Porch at dusk", "triggers": [{"trigger": "time", "at": "18:00:00"}],
        "actions": [{"action": "light.turn_on", "target": {"entity_id": "light.porch"}}]}
READING = {"id": "reading", "name": "Reading", "entities": {"light.porch": {"state": "on", "brightness": 120}}}
EVENING = {"id": "reading_in_the_evening", "alias": "Reading in the evening",
           "triggers": [{"trigger": "time", "at": "20:00:00"}],
           "actions": [{"action": "scene.turn_on", "target": {"entity_id": "scene.reading"}}]}


def mixed(home):
    """A changeset of every kind there is: a script edited, a new scene, a new automation and an edited one, one turned
    on, an entity renamed into an area, a device moved, an area's aliases, a name, a device exposed, a household line
    and an edit of the assistant's own instructions; tested on what HA has now (the porch light hidden from Assist)."""
    async_expose_entity(home.hass, "conversation", "light.porch", False)
    later = {**DUSK, "triggers": [{"trigger": "time", "at": "18:30:00"}]}
    target = f"{home.provider.entry_id}/sub1"
    changes = [{"kind": "script", "id": "press_garage", "config": {**GARAGE, "description": "Opens the garage door"}},
               {"kind": "scene", "id": "reading", "config": READING},
               {"kind": "automation", "id": "reading_in_the_evening", "config": EVENING},
               {"kind": "automation", "id": "porch_at_dusk", "config": later},
               {"kind": "automation", "id": "porch_at_night", "on": True},
               {"kind": "registry", "entity_id": "light.front", "name": "Front", "area_id": home.study,
                "aliases": ["front light"]},
               {"kind": "registry", "device_id": home.device, "area_id": home.study},
               {"kind": "registry", "area_id": home.study, "aliases": ["den"]},
               {"kind": "preference", "for": "everyone", "text": "The study is the den."},
               {"kind": "assistant", "assistant": "Assistant", "target": target, "text": "Answer in a sentence."}]
    return proposal(home, changes=changes, setup={
        "scripts": {"press_garage": changes[0]["config"]}, "scenes": {"reading": READING},
        "automations": {"reading_in_the_evening": EVENING, "porch_at_dusk": later},
        "automation_on": {"automation.porch_at_night": True},
        "exposure": {"light.porch": True}, "aliases": {"light.front": ["front light"]},
        "registry": {"entities": {"light.front": {"name": "Front", "area_id": home.study}},
                     "devices": {home.device: {"area_id": home.study}}, "areas": {home.study: {"aliases": ["den"]}}}},
        tested={"scripts": {"press_garage": GARAGE}, "automations": {"porch_at_dusk": DUSK},
                "automation_on": {"automation.porch_at_night": False}, "exposure": {"light.porch": False},
                "registry": {"entities": {"light.front": {"name": None, "area_id": None}},
                             "devices": {home.device: {"area_id": None}}, "areas": {home.study: {"aliases": []}}},
                "prompts": {target: PROMPT}})


@pytest.mark.asyncio
async def test_a_mixed_changeset_applies_settings_then_items_then_lines_then_automations_and_undoes_in_reverse(home):
    """DETAILS P12.5.9: names, registry entries and fewer LLM APIs; scripts and scenes, saved through HA's editors and
    inert until run; exposure; the household knowledge and instructions; automations last, live once saved. Undo is
    the reverse, automations first, and puts back exactly what was there: an edited item saved back, a new one deleted."""
    hass = home.hass
    steps, already, conflicts = await plan_steps(hass, mixed(home))
    assert not conflicts and not already
    assert [s["kind"] for s in steps] == ["alias", "entity", "device", "area", "script", "scene", "exposure",
                                          "household", "prompt", "automation", "automation", "automation_on"]
    before = await values(home, steps)
    editor = Editors(hass)
    result = await Changesets(hass).run("imp1", "apply", steps, editor.save)
    assert result["status"] == "done", result
    assert editor.calls == [("POST", "script", "press_garage"), ("POST", "scene", "reading"),
                            ("POST", "automation", "porch_at_dusk"), ("POST", "automation", "reading_in_the_evening")]
    # Each where Home Assistant keeps it, loaded, and nothing ran: the new scene isn't on, no light moved.
    assert await editor.get("scene", "reading") == READING and await editor.get("automation", "porch_at_dusk") == {
        **DUSK, "triggers": [{"trigger": "time", "at": "18:30:00"}]}
    assert hass.states.get("scene.reading").state == "unknown" and hass.states.get("light.porch").state == "off"
    assert hass.states.get("automation.reading_in_the_evening").state == "on"
    assert hass.states.get("automation.porch_at_night").state == "on"
    entry = er.async_get(hass).async_get("light.front")
    assert (entry.name, entry.area_id, "front light" in entry.aliases) == ("Front", home.study, True)
    assert dr.async_get(hass).async_get(home.device).area_id == home.study
    assert ar.async_get(hass).async_get_area(home.study).aliases == {"den"}
    assert async_should_expose(hass, "conversation", "light.porch")
    assert home.provider.subentries["sub1"].data["prompt"] == PROMPT + "Answer in a sentence.\n"
    undo, left = changeset.undo_steps(result["steps"], await values(home, steps))
    assert not left and [s["kind"] for s in undo] == [s["kind"] for s in reversed(steps)]
    assert (await Changesets(hass).run("imp1", "undo", undo, editor.save))["status"] == "done"
    assert editor.calls[4:] == [("DELETE", "automation", "reading_in_the_evening"), ("POST", "automation", "porch_at_dusk"),
                                ("DELETE", "scene", "reading"), ("POST", "script", "press_garage")]
    assert await values(home, steps) == before
    await hass.async_block_till_done()
    assert hass.states.get("automation.reading_in_the_evening") is None and hass.states.get("scene.reading") is None
    assert hass.states.get("automation.porch_at_night").state == "off"


@pytest.mark.parametrize("kind", ["registry", "scene"])
@pytest.mark.asyncio
async def test_a_step_home_assistant_keeps_otherwise_than_written_is_undone_with_the_rest(home, kind):
    """A step whose write went through but whose read-back finds something else (Home Assistant normalised it) was
    written by this change: rolling back puts it back too, so nothing stays applied while the panel says nothing was
    changed (P12.5.5, P12.5.13)."""
    hass = home.hass
    reading = {"id": "reading", "name": "Reading", "entities": {"light.porch": {"state": "on"}}}

    async def writer(hass_, kind_, target, value):  # Home Assistant keeps the new name otherwise than it was given.
        kept = kind_ == "entity" and value["name"] == "Front"
        await changeset.write(hass_, kind_, target, {**value, "name": "Front, as kept"} if kept else value)

    class Editor(Editors):  # Home Assistant's scene editor keeps the new scene otherwise than it was given.
        async def save(self, kind_, item_id, config):
            await Editors.save(self, kind_, item_id, config and {**config, "icon": "mdi:sofa"})
    setup = ({"registry": {"entities": {"light.front": {"name": "Front"}}}} if kind == "registry"
             else {"scenes": {"reading": reading}})
    steps, _, conflicts = await plan_steps(hass, proposal(home, changes=[], setup={**setup, "exposure": {}},
                                                          tested={"exposure": {}}))
    assert not conflicts and [s["kind"] for s in steps] == ["entity" if kind == "registry" else "scene"]
    before = await values(home, steps)
    result = await Changesets(hass, writer=writer).run("imp1", "apply", steps, Editor(hass).save)
    assert result["status"] == "rolled_back" and "didn't take the new value" in result["failure"]["error"]
    assert result["undone"] == [steps[0]["what"]] and result["stuck"] == []
    assert await values(home, steps) == before


@pytest.mark.asyncio
async def test_whole_number_floats_through_the_browser_apply_and_undo(home):
    """The panel saves editor items through the browser, whose JSON writes 120.0 as 120: a scene proposed with one, and
    an automation whose own YAML has one (40.0), apply and undo, each read back as Home Assistant then has it."""
    hass = home.hass
    with open(hass.config.path("automations.yaml"), "a") as file:
        file.write("- id: warm\n  alias: Warm\n  triggers:\n  - trigger: time\n    at: '07:00:00'\n  actions:\n"
                   "  - action: light.turn_on\n    target:\n      entity_id: light.porch\n    data:\n"
                   "      brightness_pct: 40.0\n")
    await hass.services.async_call("automation", "reload", {"id": "warm"}, blocking=True)
    was = await changeset.read(hass, "automation", "warm")
    assert was["actions"][0]["data"]["brightness_pct"] == 40.0
    warm = {**was, "actions": [{**was["actions"][0], "data": {"brightness_pct": 80.0}}]}
    reading = {**READING, "entities": {"light.porch": {"state": "on", "brightness": 120.0}}}
    steps, _, conflicts = await plan_steps(hass, proposal(home, changes=[], setup={
        "automations": {"warm": warm}, "scenes": {"reading": reading}, "exposure": {}},
        tested={"automations": {"warm": was}, "exposure": {}}))
    assert not conflicts
    editor = Editors(hass)
    result = await Changesets(hass).run("imp1", "apply", steps, editor.save)
    assert result["status"] == "done", result
    assert (await editor.get("scene", "reading"))["entities"]["light.porch"]["brightness"] == 120
    undo, left = changeset.undo_steps(result["steps"], await values(home, steps))
    assert not left and (await Changesets(hass).run("imp1", "undo", undo, editor.save))["status"] == "done"
    assert (await changeset.read(hass, "automation", "warm"))["actions"][0]["data"]["brightness_pct"] == 40


RELOADING_PROVIDER = {  # An assistant's integration that reloads on an update, as HA's AI providers do.
    "manifest.json": json.dumps({"domain": "fixture_reloading_provider", "name": "Fixture reloading provider",
                                 "version": "1.0.0", "codeowners": [], "requirements": [], "config_flow": True,
                                 "iot_class": "cloud_polling", "documentation": "https://example.invalid"}),
    "config_flow.py": """from homeassistant import config_entries


class Flow(config_entries.ConfigFlow, domain="fixture_reloading_provider"):
    VERSION = 1
""",
    "__init__.py": """from homeassistant.exceptions import ConfigEntryNotReady


async def async_setup_entry(hass, entry):
    entry.async_on_unload(entry.add_update_listener(reload))  # HA removes it when a setup fails.
    prompt = entry.subentries["sub9"].data["prompt"]
    if "Answer in a sentence." in prompt:  # Fails with the change's instructions.
        if "Briefly." in prompt:
            raise ConfigEntryNotReady("Not ready")
        return False
    return True


async def reload(hass, entry):
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass, entry):
    return True
"""}


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["Answer in a sentence.", "Answer in a sentence. Briefly."])
async def test_an_assistant_change_whose_integration_doesnt_load_again_is_undone(home, tmp_path, monkeypatch, text):
    """An assistant's change is its config subentry updated, which reloads its integration: a provider loaded before
    that isn't loaded after it (its setup failed, or isn't ready) is the step's failure, so the change is undone, and
    the provider is loaded again with its old value, though its failed setup took away the listener that reloads it."""
    from homeassistant import loader
    from homeassistant.config_entries import ConfigEntryState
    from homeassistant.helpers import frame
    hass = home.hass
    folder = tmp_path / "integrations" / "custom_components" / "fixture_reloading_provider"
    folder.mkdir(parents=True)
    for name, body in RELOADING_PROVIDER.items():
        (folder / name).write_text(body)
    monkeypatch.syspath_prepend(str(tmp_path / "integrations"))
    hass.data.pop(loader.DATA_CUSTOM_COMPONENTS, None)  # Found again, with this one.
    frame.async_setup(hass)  # As Home Assistant's start sets it up: a custom integration's setup reports through it.
    provider = ConfigEntry(domain="fixture_reloading_provider", title="Assistant", data={}, source="user", version=1,
                           minor_version=1, options={}, unique_id=None, discovery_keys=MappingProxyType({}),
                           subentries_data=[{"data": {"prompt": PROMPT, "llm_hass_api": ["assist"]},
                                             "subentry_type": "conversation", "title": "Assistant",
                                             "unique_id": None, "subentry_id": "sub9"}])
    await hass.config_entries.async_add(provider)
    await hass.async_block_till_done()
    assert provider.state is ConfigEntryState.LOADED
    target = f"{provider.entry_id}/sub9"
    steps, _, conflicts = await plan_steps(hass, proposal(home, changes=[
        {"kind": "assistant", "assistant": "Assistant", "target": target, "text": text}], setup={}, tested={}))
    assert not conflicts and [s["kind"] for s in steps] == ["prompt"]
    result = await Changesets(hass).run("imp1", "apply", steps)
    await hass.async_block_till_done()
    assert result["status"] == "rolled_back" and "didn't load again" in result["failure"]["error"], result
    assert provider.subentries["sub9"].data["prompt"] == PROMPT and provider.state is ConfigEntryState.LOADED
    await hass.config_entries.async_unload(provider.entry_id)


@pytest.mark.asyncio
async def test_a_change_with_a_script_is_refused_without_the_panel_and_nothing_is_written(home):
    """Only the panel saves scripts, automations and scenes, through HA's editors: a change with one, from anywhere
    else (no saver), is refused before anything is written, and no journal is left."""
    hass = home.hass
    steps, _, _ = await plan_steps(hass, proposal(home))
    before = await values(home, steps)
    changesets = Changesets(hass)
    with pytest.raises(changeset.Conflict, match="Reload the page and try again"):
        await changesets.run("imp1", "apply", steps)
    assert await values(home, steps) == before and changesets.journal is None
    assert (await changesets.run("imp1", "apply", [s for s in steps if s["kind"] != "script"]))["status"] == "done"
    steps, _, _ = await plan_steps(hass, mixed(home))
    for kind in ("automation", "scene"):
        with pytest.raises(changeset.Conflict, match="Reload the page and try again"):
            await Changesets(hass).run("imp2", "apply", [s for s in steps if s["kind"] == kind])


@pytest.mark.asyncio
@pytest.mark.parametrize("failing", range(6))
async def test_a_write_failing_at_any_step_leaves_nothing_applied(home, failing):
    hass = home.hass
    steps, _, _ = await plan_steps(hass, proposal(home))
    before = await values(home, steps)

    async def flaky(n, write):
        if n == failing + 1:
            raise OSError("disk full")
        await write()
    writer, editor = faulty(hass, flaky)
    result = await Changesets(hass, writer=writer).run("imp1", "apply", steps, editor.save)
    assert result["status"] == "rolled_back"
    assert result["failure"]["step"] == failing and not result["stuck"]
    assert await values(home, steps) == before
    # The scripts the panel saved are undone by the panel too, newest first, wherever the failure came.
    saved = max(0, min(failing - 1, 2))
    assert editor.calls == [("POST", "script", "close_garage"), ("POST", "script", "press_garage")][:saved] + [
        ("POST", "script", "press_garage"), ("DELETE", "script", "close_garage")][2 - saved:]


@pytest.mark.asyncio
async def test_a_script_home_assistant_refuses_to_save_rolls_back_and_says_its_reason(home):
    """HA's script editor validates what the panel saves: a refusal is the step's failure, in HA's words."""
    hass = home.hass
    steps, _, _ = await plan_steps(hass, proposal(home))
    before = await values(home, steps)
    steps[2]["after"] = {"alias": "Press", "sequence": [{"action": "not a service"}]}  # Past the plan's check.
    result = await Changesets(hass).run("imp1", "apply", steps, Editors(hass).save)
    assert result["status"] == "rolled_back" and result["failure"]["step"] == 2
    assert result["failure"]["error"].startswith("Message malformed")
    assert await values(home, steps) == before


@pytest.mark.asyncio
async def test_a_saved_item_is_waited_for_on_the_registrys_changes_not_by_polling(home, monkeypatch):
    """HA's editors reload after they answer: the change waits for the item's registry entry to come (or go), looking
    at the registry when it changes and only then, and for an automation or scene for its editor's reload to be done
    (what it fires); nothing in time is a conflict."""
    hass, registry = home.hass, er.async_get(home.hass)
    looked, look = [], registry.async_get_entity_id
    monkeypatch.setattr(registry, "async_get_entity_id", lambda *key: looked.append(key) or look(*key))
    waiting = hass.async_create_task(changeset.loaded(hass, "script", "new_script", {"alias": "New", "sequence": []}))
    await asyncio.sleep(0.3)
    assert not waiting.done() and len(looked) == 1  # Once, then nothing until the registry changes.
    registry.async_get_or_create("script", "script", "new_script")  # As HA's reload adds it.
    await asyncio.wait_for(waiting, 5)
    reloaded = asyncio.Event()
    waiting = hass.async_create_task(changeset.loaded(hass, "automation", "porch_at_dusk", DUSK, reloaded))
    await asyncio.sleep(0.1)
    assert not waiting.done()  # Its registry entry is there, but HA hasn't reloaded it yet.
    reloaded.set()
    await asyncio.wait_for(waiting, 5)
    monkeypatch.setattr(changeset, "LOAD_TIMEOUT", 0.1)
    with pytest.raises(changeset.Conflict, match="Home Assistant didn't remove it."):
        await changeset.loaded(hass, "script", "new_script", None)


@pytest.mark.asyncio
# Not 3: exposing a script this change created a moment before; undoing the change removes the script itself.
@pytest.mark.parametrize("changed", [1, 2, 4, 5])
async def test_a_value_changed_midway_stops_the_change_and_undoes_it_without_touching_the_users_value(home, changed):
    hass = home.hass
    steps, _, _ = await plan_steps(hass, proposal(home))
    before = await values(home, steps)

    async def meddling(n, write):
        await write()
        if n == changed:  # Someone edits the next step's value right after this write.
            nxt = steps[changed]
            await users_edit(hass, nxt["kind"], nxt["target"], _users_value(nxt))
    writer, editor = faulty(hass, meddling)
    result = await Changesets(hass, writer=writer).run("imp1", "apply", steps, editor.save)
    assert result["status"] == "rolled_back" and result["failure"]["step"] == changed
    after = await values(home, steps)
    nxt = steps[changed]
    assert after.pop((nxt["kind"], nxt["target"])) == _users_value(nxt)  # The user's edit stays.
    assert after == {k: v for k, v in before.items() if k != (nxt["kind"], nxt["target"])}


def _users_value(step):
    return {"script": {"alias": "Mine", "sequence": []}, "alias": ["mine"], "exposure": not step["before"],
            "prompt": "My own prompt", "household": {"everyone": ["Mine"], "people": {}},
            "assistant_api": ["mine"]}[step["kind"]]


class Crash(BaseException):
    """HA stopping mid-change: nothing in the apply catches it."""


@pytest.mark.asyncio
@pytest.mark.parametrize("crash", range(6))
@pytest.mark.parametrize("written", [False, True])
async def test_a_crash_at_any_step_is_undone_at_the_next_start_and_its_scripts_when_the_panel_opens(home, crash,
                                                                                                    written):
    """written: the crash comes right after the step's write (before its journal entry), or before it. The next start
    undoes what the improver writes itself back to the last script written, which only the panel saves: saved, its
    exposure taken back, and nothing tells an assistant to use it, so it's inert until the panel finishes the change
    (the name applied before it stays until then)."""
    hass = home.hass
    steps, _, _ = await plan_steps(hass, proposal(home))
    before = await values(home, steps)

    async def crashing(n, write):
        if n == crash + 1:
            if written:
                await write()
            raise Crash
        await write()
    writer, editor = faulty(hass, crashing)
    with pytest.raises(Crash):
        await Changesets(hass, writer=writer).run("imp1", "apply", steps, editor.save)
    assert await values(home, steps) != before or (crash == 0 and not written)
    started = Changesets(hass)  # The next start finds the journal.
    await started.load()
    with pytest.raises(changeset.Conflict):  # Left open: nothing else applies meanwhile.
        await started.run("imp2", "apply", steps[:1])
    outcome = await started.resume()
    scripts_written = crash > 1 or (crash == 1 and written)
    if scripts_written:  # The scripts wait for the panel, inert: what came after them is back.
        assert outcome == {"improvement_id": "imp1", "action": "apply", "status": "waiting"}
        now = await values(home, steps)
        later = lambda found: {k: v for k, v in found.items() if k[0] not in ("script", "alias")}  # noqa: E731
        assert later(now) == later(before)
        outcome = await started.resume(Editors(hass).save)  # The panel opens.
    assert outcome["status"] == "rolled_back" and outcome["improvement_id"] == "imp1"
    assert await values(home, steps) == before
    again = Changesets(hass)
    await again.load()
    assert await again.resume() is None  # Finished: nothing left to recover.


@pytest.mark.asyncio
@pytest.mark.parametrize("written", [False, True])
async def test_an_apply_cut_off_among_its_automations_turns_them_off_at_the_next_start_until_the_panel_undoes_it(
        home, written):
    """DETAILS P12.11: automations go last, live once saved. HA stopping as the new automation is saved (written: just
    after its save, or just before): the next start turns off each automation the change saved, so none runs half
    applied, and waits for the panel, which deletes the new one and saves the edited one back, on again as it was."""
    hass = home.hass
    steps, _, _ = await plan_steps(hass, mixed(home))
    assert [s["target"] for s in steps[-3:-1]] == ["porch_at_dusk", "reading_in_the_evening"]  # Edited, then new.
    before = await values(home, steps)
    assert hass.states.get("automation.porch_at_dusk").state == "on"

    async def crashing(n, write):
        if n == len(steps) - 1:  # The new automation's save.
            if written:
                await write()
            raise Crash
        await write()
    writer, editor = faulty(hass, crashing)
    with pytest.raises(Crash):
        await Changesets(hass, writer=writer).run("imp1", "apply", steps, editor.save)
    await hass.async_block_till_done()  # What HA had saved, loaded as it starts again.
    assert hass.states.get("automation.porch_at_dusk").state == "on"
    assert (hass.states.get("automation.reading_in_the_evening") is not None) is written
    started = Changesets(hass)
    await started.load()
    assert (await started.resume())["status"] == "waiting"
    held = ["automation.porch_at_dusk"] + ["automation.reading_in_the_evening"] * written
    assert {e: hass.states.get(e).state for e in held} == dict.fromkeys(held, "off")
    assert await started.resume() == {"improvement_id": "imp1", "action": "apply", "status": "waiting"}  # Still held.
    finishing = Editors(hass)
    outcome = await started.resume(finishing.save)  # The panel opens.
    assert outcome["status"] == "rolled_back", outcome
    assert finishing.calls == [("DELETE", "automation", "reading_in_the_evening")] * written + [
        ("POST", "automation", "porch_at_dusk"), ("DELETE", "scene", "reading"), ("POST", "script", "press_garage")]
    await hass.async_block_till_done()
    assert await values(home, steps) == before
    assert hass.states.get("automation.porch_at_dusk").state == "on"  # As it was.
    assert hass.states.get("automation.reading_in_the_evening") is None


@pytest.mark.asyncio
async def test_an_apply_whose_panel_goes_among_its_automations_turns_them_off_at_once_until_the_panel_undoes_it(home):
    """DETAILS P12.11: the panel going away as the new automation is saved holds the edited one it saved off at once,
    not only at the next start; the next panel puts it back, on again as it was."""
    hass = home.hass
    steps, _, _ = await plan_steps(hass, mixed(home))
    before = await values(home, steps)

    async def leaving(n, write):
        if n == len(steps) - 1:  # The new automation's save: the panel is gone.
            raise changeset.NoPanel
        await write()
    writer, editor = faulty(hass, leaving)
    changesets = Changesets(hass, writer=writer)
    with pytest.raises(changeset.NoPanel):
        await changesets.run("imp1", "apply", steps, editor.save)
    assert hass.states.get("automation.porch_at_dusk").state == "off"
    finishing = Editors(hass)
    outcome = await changesets.resume(finishing.save)  # A panel opens.
    assert outcome["status"] == "rolled_back", outcome
    await hass.async_block_till_done()
    assert await values(home, steps) == before and hass.states.get("automation.porch_at_dusk").state == "on"


@pytest.mark.asyncio
async def test_an_apply_cut_off_among_its_automations_holds_them_off_before_home_assistant_arms_their_triggers(home):
    """DETAILS P12.11: HA stopping just after an apply saved an automation that runs as HA starts. The improver
    loading again holds it off before HA arms any trigger, so it doesn't run half applied as HA starts; the undo
    still waits for HA to have started."""
    hass = home.hass
    item = mixed(home)
    at_start = {**EVENING, "triggers": [{"trigger": "homeassistant", "event": "start"}]}
    item["changes"][2]["config"] = item["setup"]["automations"]["reading_in_the_evening"] = at_start
    steps, _, conflicts = await plan_steps(hass, item)
    assert not conflicts and steps[-2]["target"] == "reading_in_the_evening"

    async def crashing(n, write):
        await write()
        if n == len(steps) - 1:  # Just after the new automation's save.
            raise Crash
    writer, editor = faulty(hass, crashing)
    with pytest.raises(Crash):
        await Changesets(hass, writer=writer).run("imp1", "apply", steps, editor.save)
    await hass.async_block_till_done()
    assert hass.states.get("automation.reading_in_the_evening").state == "on"
    runs = []
    hass.bus.async_listen("automation_triggered", lambda event: runs.append(event.data["entity_id"]))
    await Improvements(hass, None).start()  # The improver loading again, as HA starts.
    await hass.async_start()
    await hass.async_block_till_done()
    assert runs == []
    assert hass.states.get("automation.reading_in_the_evening").state == "off"


@pytest.mark.asyncio
async def test_a_new_automation_whose_save_lands_after_the_panel_went_is_held_off_as_it_loads(home):
    """DETAILS P12.11: the panel goes as it saves the new automation, and the save lands after the change stopped
    waiting for it: the automation is held off as Home Assistant loads it, and the next panel deletes it."""
    hass = home.hass
    steps, _, _ = await plan_steps(hass, mixed(home))
    new = steps[-2]
    assert new["target"] == "reading_in_the_evening" and new["before"] is None

    async def leaving(n, write):
        if n == len(steps) - 1:  # The new automation's save, sent as the panel goes.
            raise changeset.NoPanel
        await write()
    writer, editor = faulty(hass, leaving)
    changesets = Changesets(hass, writer=writer)
    with pytest.raises(changeset.NoPanel):
        await changesets.run("imp1", "apply", steps, editor.save)
    assert hass.states.get("automation.reading_in_the_evening") is None
    await Editors(hass).save("automation", new["target"], new["after"])  # The save lands.
    await hass.async_block_till_done()
    assert hass.states.get("automation.reading_in_the_evening").state == "off"
    finishing = Editors(hass)
    outcome = await changesets.resume(finishing.save)  # A panel opens.
    assert outcome["status"] == "rolled_back", outcome
    assert finishing.calls[0] == ("DELETE", "automation", "reading_in_the_evening")
    await hass.async_block_till_done()
    assert hass.states.get("automation.reading_in_the_evening") is None
    assert hass.states.get("automation.porch_at_dusk").state == "on"


@pytest.mark.asyncio
async def test_an_undo_cut_off_among_its_scripts_waits_whole_for_the_panel_then_puts_it_all_back(home):
    """An undo writes its scripts after the instruction lines and exposure: cut off among them, the next start writes
    nothing (putting the instruction lines back before the scripts they name would break the safe order), and the
    panel puts the scripts back first, then the rest: all of it in place again."""
    hass = home.hass
    steps, _, _ = await plan_steps(hass, proposal(home))
    editor = Editors(hass)
    applied = (await Changesets(hass).run("imp1", "apply", steps, editor.save))["steps"]
    in_place = await values(home, steps)
    undo, _ = changeset.undo_steps(applied, in_place)

    async def crashing(n, write):  # The first script's undo is written, then HA stops.
        await write()
        if n == len(undo) - 2:
            raise Crash
    writer, cut = faulty(hass, crashing)
    with pytest.raises(Crash):
        await Changesets(hass, writer=writer).run("imp1", "undo", undo, cut.save)
    half = await values(home, steps)
    started = Changesets(hass)
    await started.load()
    assert (await started.resume())["status"] == "waiting" and await values(home, steps) == half
    finishing = Editors(hass)
    outcome = await started.resume(finishing.save)
    assert outcome["status"] == "rolled_back" and outcome["action"] == "undo"
    assert finishing.calls == [("POST", "script", "press_garage")]  # The script back first.
    assert await values(home, steps) == in_place


@pytest.mark.asyncio
async def test_values_changed_since_testing_are_conflicts_and_nothing_is_written(home):
    hass = home.hass
    async_expose_entity(hass, "conversation", "light.porch", False)
    item = proposal(home, setup={**proposal(home)["setup"], "exposure": {"light.porch": True}},
                    tested={"scripts": {"press_garage": {"alias": "Old", "sequence": []}},
                            "exposure": {"light.porch": True}})
    steps, _, conflicts = await plan_steps(hass, item)
    assert {c["target"] for c in conflicts} >= {"press_garage", "light.porch"}
    # A line it replaces that the user has since edited away is a conflict too.
    _, _, problems = household.edit(household.EMPTY, [{"kind": "preference", "for": "everyone", "text": "New",
                                                       "replaces": ["A line that's gone"]}], USER, PROMPT)
    assert problems


@pytest.mark.asyncio
async def test_an_item_no_editor_keeps_is_a_conflict_and_nothing_is_written(home):
    """Safety rule 3 (DETAILS P7.5.5): a script the household keeps outside scripts.yaml, which Home Assistant's
    script editor doesn't save, isn't changed here, whatever it was tested on; and an item Home Assistant's validators
    or the safety rules refuse now is a conflict too (rule 2: one that asks conversation.process, even a fixed text)."""
    elsewhere = {"alias": "Elsewhere", "sequence": [{"action": "light.turn_on", "target": {"entity_id": "light.porch"}}]}
    unsafe = {"alias": "Unsafe", "fields": {"which": {"selector": {"entity": {}}}},
              "sequence": [{"action": "light.turn_on", "target": {"entity_id": "{{ which }}"}}]}
    asks = {"alias": "Asks", "sequence": [{"action": "conversation.process", "data": {"text": "Lock up"}}]}
    item = proposal(home, changes=[], setup={"scripts": {"elsewhere": {**elsewhere, "description": "Now"},
                                                         "unsafe": unsafe, "asks": asks}, "exposure": {}},
                    tested={"scripts": {"elsewhere": elsewhere}, "exposure": {}})
    steps, _, conflicts = await plan_steps(home.hass, item)
    assert steps == []
    assert conflicts == [
        {"kind": "script", "target": "asks", "what": "Asks",
         "why": "conversation.process hands the choice of action to a model as it runs."},
        {"kind": "script", "target": "elsewhere", "what": "Elsewhere", "why": "It isn't kept by Home Assistant's "
                                                                               "script editor (scripts.yaml), so it can't be changed here."},
        {"kind": "script", "target": "unsafe", "what": "Unsafe",
         "why": "light.turn_on's target comes from a script field, a template or trigger data."}]


@pytest.mark.parametrize("restored", [False, True])
@pytest.mark.asyncio
async def test_a_new_item_whose_id_home_assistant_now_runs_elsewhere_is_a_conflict(home, restored):
    """Safety rule 3 at plan: an automation new when tested, whose id the household has since given one of its own
    outside the editor (a package), is a conflict, not a second automation with that id. One the registry only
    remembers (restored, nothing runs it) is saved as new."""
    hass = home.hass
    er.async_get(hass).async_get_or_create("automation", "automation", "porch_at_dawn",
                                           suggested_object_id="porch_at_dawn")
    hass.states.async_set("automation.porch_at_dawn", "unavailable" if restored else "on",
                          {"id": "porch_at_dawn", **({"restored": True} if restored else {})})
    dawn = {**DUSK, "id": "porch_at_dawn", "alias": "Porch at dawn"}
    steps, _, conflicts = await plan_steps(hass, proposal(home, changes=[], setup={"automations": {
        "porch_at_dawn": dawn}, "exposure": {}}, tested={"exposure": {}}))
    if restored:
        assert not conflicts and [(s["kind"], s["target"]) for s in steps] == [("automation", "porch_at_dawn")]
    else:
        assert steps == [] and conflicts == [{
            "kind": "automation", "target": "porch_at_dawn", "what": "Porch at dawn",
            "why": "It isn't kept by Home Assistant's automation editor (automations.yaml), so it can't be changed here."}]


@pytest.mark.asyncio
async def test_turning_on_or_off_an_automation_no_editor_keeps_is_a_conflict(home):
    """Safety rule 3 at plan, for turning one on or off too: an automation the household keeps in a package, which
    Home Assistant's automation editor doesn't save, isn't changed here."""
    hass = home.hass
    er.async_get(hass).async_get_or_create("automation", "automation", "porch_at_dawn",
                                           suggested_object_id="porch_at_dawn")
    hass.states.async_set("automation.porch_at_dawn", "on", {"id": "porch_at_dawn", "friendly_name": "Porch at dawn"})
    steps, _, conflicts = await plan_steps(hass, proposal(home, changes=[], setup={
        "automation_on": {"automation.porch_at_dawn": False, "automation.porch_at_night": True}, "exposure": {}},
        tested={"exposure": {}}))
    assert [(s["kind"], s["target"]) for s in steps] == [("automation_on", "automation.porch_at_night")]
    assert conflicts == [{
        "kind": "automation_on", "target": "automation.porch_at_dawn", "what": "Porch at dawn",
        "why": "It isn't kept by Home Assistant's automation editor (automations.yaml), so it can't be changed here."}]


@pytest.mark.asyncio
async def test_an_edit_whose_initial_state_would_turn_its_automation_on_or_off_is_a_conflict(home):
    """Porch at night is off: saved with initial_state true it would be on, and saving its old configuration back on
    undo would leave it on (Home Assistant restores its last state). Checked against its state as it's planned."""
    night = {"id": "porch_at_night", "alias": "Porch at night", "initial_state": True,
             "triggers": [{"trigger": "time", "at": "23:30:00"}],
             "actions": [{"action": "light.turn_off", "target": {"entity_id": "light.porch"}}]}
    steps, _, conflicts = await plan_steps(home.hass, proposal(home, changes=[], setup={"automations": {
        "porch_at_night": night}, "exposure": {}}, tested={"exposure": {}}))
    assert steps == [] and conflicts == [{
        "kind": "automation", "target": "porch_at_night", "what": "Porch at night",
        "why": "its initial_state would turn it on as it's saved, which undo can't put back."}]


@pytest.mark.asyncio
async def test_a_new_scene_whose_entity_id_another_integration_holds_is_a_conflict(home):
    """A disabled scene of another integration holds scene.reading: Home Assistant would give the new Reading
    scene.reading_2, and the exposure planned for scene.reading would expose the other one."""
    er.async_get(home.hass).async_get_or_create("scene", "hue", "hue-reading", suggested_object_id="reading",
                                                disabled_by=er.RegistryEntryDisabler.INTEGRATION)
    steps, _, conflicts = await plan_steps(home.hass, proposal(home, changes=[], setup={
        "scenes": {"reading": READING}, "exposure": {"scene.reading": True}}, tested={"exposure": {}}))
    assert [c for c in conflicts if c["kind"] == "scene"] == [{
        "kind": "scene", "target": "reading", "what": "Reading",
        "why": "scene.reading exists already; give the new scene another name."}]


@pytest.mark.parametrize("expose", [False, True])
@pytest.mark.asyncio
async def test_a_new_scene_is_exposed_to_assist_or_kept_hidden_as_its_change_says(home, expose):
    """Home Assistant exposes new scenes to Assist by default: one whose change says expose: false is hidden after
    its save, as its replay tested it."""
    from homeassistant.components.homeassistant.const import DATA_EXPOSED_ENTITIES
    from custom_components.assist_improver.changes import setup_from_changes
    hass = home.hass
    hass.data[DATA_EXPOSED_ENTITIES].async_set_expose_new_entities("conversation", True)
    quiet = {"id": "quiet", "name": "Quiet", "entities": {"light.porch": {"state": "off"}}}
    setup = setup_from_changes([{"kind": "scene", "id": "quiet", "config": quiet, "expose": expose}],
                               source={"states": {}, "scenes": []}, prompt="", scripts={})
    assert setup["exposure"] == {"scene.quiet": expose}
    steps, _, conflicts = await plan_steps(hass, proposal(home, changes=[], setup=setup,
                                                          tested={"exposure": {"scene.quiet": False}}))
    assert not conflicts
    assert (await Changesets(hass).run("imp1", "apply", steps, Editors(hass).save))["status"] == "done"
    assert async_should_expose(hass, "conversation", "scene.quiet") is expose


@pytest.mark.asyncio
async def test_an_item_whose_id_isnt_a_plain_token_is_a_conflict_and_nothing_is_written(home):
    """The panel saves an item at config/<kind>/config/<id>: one whose id could take that call elsewhere in Home
    Assistant's API is a conflict as it's planned, whatever it was checked with before."""
    sneaky = "../../../services/script/press_garage"
    item = proposal(home, changes=[], setup={"automations": {sneaky: {**DUSK, "id": sneaky}}, "exposure": {}},
                    tested={"exposure": {}})
    steps, _, conflicts = await plan_steps(home.hass, item)
    assert steps == [] and conflicts == [{"kind": "automation", "target": sneaky, "what": DUSK["alias"],
                                          "why": f"Its id is letters, digits, _ and - only, not {sneaky!r}."}]


@pytest.mark.asyncio
async def test_an_assistant_change_writes_only_its_instructions_and_never_adds_an_llm_api(home):
    """Its instructions and fewer LLM APIs are the only options a change writes, each through its config subentry,
    the rest of which stays as it was; a recommendation is never applied; and an LLM API it would add is a
    conflict."""
    hass, target = home.hass, f"{home.provider.entry_id}/sub1"
    edit = {"kind": "assistant", "assistant": "Assistant", "target": target, "text": "Answer in a sentence."}
    advice = {"kind": "assistant", "assistant": "Assistant", "target": target,
              "recommend": {"chat_model": "a larger model", "web_search": False}}
    item = proposal(home, changes=[edit, advice], setup={}, tested={})
    steps, _, conflicts = await plan_steps(hass, item)
    assert not conflicts and [s["kind"] for s in steps] == ["prompt"]
    before = dict(home.provider.subentries["sub1"].data)
    assert (await Changesets(hass).run("imp1", "apply", steps))["status"] == "done"
    assert home.provider.subentries["sub1"].data == {**before, "prompt": PROMPT + "Answer in a sentence.\n"}
    adds = {"kind": "assistant", "assistant": "Assistant", "target": target, "llm_hass_api": ["assist", "more"]}
    steps, _, conflicts = await plan_steps(hass, proposal(home, changes=[adds], setup={}, tested={}))
    assert steps == [] and conflicts == [{"kind": "assistant_api", "target": target, "what": "Assistant's LLM APIs",
                                          "why": "It would add LLM APIs (more), which a change never does."}]


def test_instruction_lines_go_where_they_belong():
    lines = lambda text, who="everyone", replaces=None: [{"kind": "preference", "for": who, "text": text,
                                                          **({"replaces": replaces} if replaces else {})}]
    knowledge, prompt, problems = household.edit(household.EMPTY, lines("Doors close at 10."), USER, PROMPT)
    assert knowledge == {"everyone": ["Doors close at 10."], "people": {}} and prompt == PROMPT and not problems
    # A person's line replacing one written in the assistant's own instructions: moved into the household knowledge.
    knowledge, prompt, problems = household.edit(knowledge, lines("Alex likes very short answers.", "this_user",
                                                                  ["Alex likes short answers.\n"]), USER, PROMPT)
    assert not problems and "Alex likes short answers." not in prompt
    assert knowledge["people"] == {USER: ["Alex likes very short answers."]}
    again, _, _ = household.edit(knowledge, lines("Doors close at 10."), USER, prompt)
    assert again == knowledge  # Already there: nothing added twice.
    replaced, _, _ = household.edit(knowledge, lines("Doors close at 11.", replaces=["Doors close at 10."]), USER, prompt)
    assert replaced["everyone"] == ["Doors close at 11."]
    # What the assistant reads: everyone's lines, then the speaking person's only.
    assert household.render(replaced, USER).splitlines()[1:] == ["- Doors close at 11.", "- Alex likes very short answers."]
    assert household.render(replaced, "someone else").splitlines()[1:] == ["- Doors close at 11."]
    assert household.render(household.EMPTY, USER) == ""


@pytest.mark.asyncio
async def test_the_notification_links_each_improvement_to_its_review_and_clears_when_decided(home):
    hass = home.hass
    from homeassistant.components import persistent_notification
    improvements = Improvements(hass, None)
    await improvements.start()
    for n in (1, 2):
        improvements.items[f"imp{n}"] = {**proposal(home, id=f"imp{n}", title=f"Fix {n}"), "cause": "",
                                         "copy": {"status": "failed"},  # Written up (the generator's title stands).
                                         "status": "awaiting_review", "created": str(n),
                                         "decisions": [], "application": None, "history": []}
    improvements._sync()
    current = persistent_notification._async_get_or_create_notifications(hass)
    assert current[NOTIFICATION]["title"] == "2 improvements to review"
    assert current[NOTIFICATION]["message"].endswith("Review these improvements in [Assist Improver](/assist-improver).")
    assert "[Fix 1](/assist-improver?id=imp1)" in current[NOTIFICATION]["message"]
    assert not [i for d, i in ir.async_get(hass).issues if d == DOMAIN]
    await improvements.decide("imp1", "decline", reason="not_an_issue", user_id="alex")
    assert current[NOTIFICATION]["title"] == "1 improvement to review"
    await improvements.decide("imp2", "accept", user_id="alex", editors=Editors(hass).save)
    assert NOTIFICATION not in current and improvements.get("imp2")["status"] == "applied"


def test_applying_keeps_the_households_own_copy_of_unchanged_steps():
    from custom_components.assist_improver.changeset import keep_unchanged_steps
    press = {"action": "switch.turn_on", "metadata": {}, "target": {"entity_id": "switch.door"}, "data": {}}
    live = {"alias": "Press", "sequence": [press]}
    proposed = {"alias": "Press", "description": "Presses the door",
                "sequence": [{"action": "switch.turn_on", "target": {"entity_id": "switch.door"}}, {"stop": "Pressed."}]}
    kept = keep_unchanged_steps(proposed, live)
    assert kept["sequence"] == [press, {"stop": "Pressed."}] and kept["description"] == "Presses the door"


def test_diffs_number_lines_as_a_diff_viewer_does():
    from custom_components.assist_improver.panel import numbered_diff
    rows = numbered_diff(["a", "b", "c", "d", "e", "f", "g", "h"], ["a", "B", "c", "d", "e", "f", "g", "h", "i"])
    assert [(r["sign"], r.get("old"), r.get("new")) for r in rows] == [
        (" ", 1, 1), ("-", 2, None), ("+", None, 2), (" ", 3, 3), (" ", 4, 4), ("gap", None, None),
        (" ", 7, 7), (" ", 8, 8), ("+", None, 9)]


@pytest.mark.asyncio
async def test_the_panel_reviews_and_decides_through_the_same_list(home):
    from kit import runtime_with_flow
    from custom_components.assist_improver import panel
    hass = home.hass
    improvements = Improvements(hass, None)
    await improvements.start()
    improvements.items["imp1"] = {**proposal(home), "cause": "Why", "copy": {"status": "failed"},
                                  "status": "awaiting_review", "created": "1", "decisions": [], "application": None,
                                  "history": []}
    improvements._sync()
    serve(hass, runtime_with_flow(hass, improvements=improvements, recorder=SimpleNamespace(index={})))
    connection, editor = Connection(), Editors(hass)
    await open_panel(hass, connection, editor)  # The panel, saving scripts on its connection.
    listed = await command(hass, connection, panel.ws_list)
    assert listed[1]["improvements"][0]["pending"] is True
    review = (await command(hass, connection, panel.ws_get, improvement_id="imp1"))[1]
    assert [c["kind"] for c in review["changes"]] == ["aliases", "script", "script", "exposure", "exposure", "household"]
    script = review["changes"][2]
    assert script["added"] and any(r["sign"] == "+" and r["new"] for r in script["rows"])
    assert review["choices"][0] == {"value": "accept", "label": "Accept changes"}
    decided = await command(hass, connection, panel.ws_decide, improvement_id="imp1", decision="accept", note="")
    assert decided[0] == "ok" and decided[1]["status"] == "applied"
    assert [call[:2] for call in editor.calls] == [("POST", "script")] * 2  # Its scripts, saved by the panel.
    assert not [i for d, i in ir.async_get(hass).issues if d == DOMAIN]  # Repairs follows the panel's decision.
    assert (await command(hass, connection, panel.ws_get, improvement_id="nope"))[:2] == ("error", "not_found")


async def _ws(handler, hass, connection, msg):
    """A websocket command's own coroutine, without HA's admin and async wrappers."""
    inner = handler
    while hasattr(inner, "__wrapped__"):
        inner = inner.__wrapped__
    result = inner(hass, connection, msg)
    if hasattr(result, "__await__"):
        await result


@pytest.mark.asyncio
async def test_the_sidebar_title_counts_improvements_waiting_for_review(home, monkeypatch):
    from homeassistant.components import frontend
    from custom_components.assist_improver import panel
    hass = home.hass
    titles = []
    monkeypatch.setattr(frontend, "async_register_built_in_panel",
                        lambda hass, **kw: titles.append(kw["sidebar_title"]))
    improvements = Improvements(hass, None)
    await improvements.start()
    improvements.items["imp1"] = {**proposal(home), "cause": "", "copy": {"status": "failed"},
                                  "status": "awaiting_review", "created": "1", "decisions": [], "application": None,
                                  "history": []}
    serve(hass, SimpleNamespace(improvements=improvements))
    hass.data[panel.PANEL_KEY] = True
    improvements._sync()
    await improvements.decide("imp1", "decline", user_id="alex")
    assert titles == ["Assist Improver (1)", "Assist Improver"]


def _second_assistant(hass, apis, prompt="Hi."):
    other = ConfigEntry(domain="fixture_provider_2", title="Other", data={}, source="user", version=1, minor_version=1,
                        options={}, unique_id=None, discovery_keys=MappingProxyType({}), state=ConfigEntryState.NOT_LOADED,
                        subentries_data=[{"data": {"prompt": prompt, "llm_hass_api": apis}, "subentry_type": "conversation",
                                          "title": "Haiku", "unique_id": None, "subentry_id": "sub2"}])
    hass.config_entries._entries[other.entry_id] = other
    return other


@pytest.mark.asyncio
async def test_household_lines_switch_no_assistant_every_one_on_assist_reads_them(home):
    hass = home.hass
    await (await household.async_setup(hass)).save({"everyone": ["Doors close at 10."], "people": {}})
    other = _second_assistant(hass, ["assist"], prompt="Hi.\nDoors close at 10.\n")
    steps, _, conflicts = await plan_steps(hass, proposal(home))
    assert not conflicts and not [s for s in steps if s["kind"] == "assistant_api"]
    assert (await Changesets(hass).run("imp1", "apply", steps, Editors(hass).save))["status"] == "done"
    assert other.subentries["sub2"].data == {"prompt": "Hi.\nDoors close at 10.\n", "llm_hass_api": ["assist"]}
    assert home.provider.subentries["sub1"].data["llm_hass_api"] == ["assist"]


@pytest.mark.asyncio
async def test_an_old_switch_onto_the_household_api_still_undoes(home):
    """Up to 0.37 an improvement with household lines also switched assistants onto the household API (a real one
    switched three). Its recorded steps still undo: the assistants go back to Assist, and the household knowledge goes
    back too."""
    hass = home.hass
    target = f"{home.provider.entry_id}/sub1"
    other = _second_assistant(hass, [household.API_ID])
    knowledge = {"everyone": ["Doors close at 10."], "people": {}}
    await (await household.async_setup(hass)).save(knowledge)
    await changeset.write(hass, "assistant_api", target, [household.API_ID])
    improvements = Improvements(hass, None)
    await improvements.start()
    improvements.items["imp1"] = {**proposal(home), "cause": "",
                                  "copy": {"status": "failed"}, "status": "applied", "created": "1", "decisions": [],
                                  "history": [], "application": {"at": "1", "by": "alex", "already": [], "steps": [
        {"kind": "household", "target": "household", "before": household.EMPTY, "after": knowledge,
         "what": "Household knowledge"},
        {"kind": "assistant_api", "target": target, "before": ["assist"], "after": [household.API_ID],
         "what": "Assistant: use Assist with household knowledge"},
        {"kind": "assistant_api", "target": f"{other.entry_id}/sub2", "before": ["assist"], "after": [household.API_ID],
         "what": "Haiku: use Assist with household knowledge"}]}}
    item = await improvements.decide("imp1", "undo", user_id="alex")
    assert item["status"] == "awaiting_review" and item["outcome"]["status"] == "done" and not item["outcome"]["left"]
    assert home.provider.subentries["sub1"].data["llm_hass_api"] == ["assist"]
    assert other.subentries["sub2"].data["llm_hass_api"] == ["assist"]
    assert hass.data[household.KEY].knowledge == household.EMPTY


@pytest.mark.asyncio
async def test_household_knowledge_lines_are_added_changed_and_removed_from_the_panel(home):
    from custom_components.assist_improver import panel
    hass = home.hass
    store = await household.async_setup(hass)
    await store.save({"everyone": ["The garage door is a button."], "people": {"alex": ["My TV is the bedroom one."]}})
    sent = []
    connection = SimpleNamespace(user=SimpleNamespace(id="alex", name="Alex", is_admin=True),
                                 send_result=lambda i, r: sent.append(("ok", r)),
                                 send_error=lambda i, code, m: sent.append(("error", code)))
    await _ws(panel.ws_household_edit, hass, connection, {"id": 1, "scope": "everyone", "action": "add", "text": " Quiet hours start at 22:00. "})
    await _ws(panel.ws_household_edit, hass, connection, {"id": 2, "scope": "alex", "action": "update", "index": 0,
                                                          "old": "My TV is the bedroom one.", "text": "My TV is the living room one."})
    await _ws(panel.ws_household_edit, hass, connection, {"id": 3, "scope": "everyone", "action": "delete", "index": 0,
                                                          "old": "Something else"})
    assert sent[-1] == ("error", "changed")  # The line no longer reads like that: nothing is removed.
    await _ws(panel.ws_household_edit, hass, connection, {"id": 4, "scope": "everyone", "action": "delete", "index": 0,
                                                          "old": "The garage door is a button."})
    assert store.knowledge == {"everyone": ["Quiet hours start at 22:00."], "people": {"alex": ["My TV is the living room one."]}}


def test_the_editor_writes_plainly_or_is_told_what_to_fix():
    from custom_components.assist_improver import editor
    assert editor.problems({"headline": "Expose garage door controls via scripts",
                            "benefit": "Say “open the garage doors” and both open in one request."}) == []
    found = editor.problems({"headline": "garage via script.garage_door_press.", "benefit": "The user can " + "x" * 240})
    assert len(found) >= 4 and any("entity ID" in f for f in found) and any("'the user'" in f for f in found)
    item = {"title": "T", "cause": "C", "provider": {"title": "Kitchen"},
            "changes": [{"kind": "registry", "entity_id": "script.press", "expose": True}], "test": tested("Open it")}
    shown = editor.payload(item, {"script.press": "Press garage"})
    assert shown["changes"] == ["Expose “Press garage” to Assist"] and shown["tested"][0]["result"] == "1 turn(s) instead of 3"


@pytest.mark.asyncio
async def test_the_editor_reads_an_automation_turned_on_or_off_by_its_name(home, monkeypatch):
    """The write-up model reads the changes as the review lists them: an automation by its name in Home Assistant, not
    its configuration ID."""
    from custom_components.assist_improver import editor, reflective
    read = {}

    async def run_role(hass, **kwargs):
        read.update(kwargs["payload"])
        return {}
    monkeypatch.setattr(reflective, "run_role", run_role)
    item = {"title": "T", "cause": "C", "provider": {"title": "Kitchen"}, "test": tested("Open it"),
            "changes": [{"kind": "automation", "id": "porch_at_dusk", "on": False}]}
    await editor.write(home.hass, item, "ai_task.small")
    assert read["changes"] == ["Turn off automation “Porch at dusk”"]


@pytest.mark.asyncio
async def test_the_review_and_the_editor_say_what_was_fixed_what_is_still_open_and_what_the_home_couldnt_do(home):
    """A goal the home couldn't do either way (its device unavailable as asked) that a change reaches in fewer turns;
    one objection it fixed, one a reply with it still shows (said twice), one it couldn't tell (set aside). The
    review's row names the device as Home Assistant does, its open lines are the household's words, each once, with
    the reply (its place among those said with the change) and why, the chip counts them, and the editor is told
    each, so it never writes what's still open as fixed."""
    from custom_components.assist_improver import editor, panel
    from custom_components.assist_improver.improvements import row
    hass = home.hass
    hass.states.async_set("climate.den_heater", "unavailable", {"friendly_name": "Den heater"})
    test = tested("Heat the den", said=("Heat the den", "Heat it", "Please"), replies=("No.", "No.", "No."),
                  after=("Sent.", "Sent."), met_before=False, validated=True)
    test["after"]["said"] = test["after"]["counted"]["1:0"] = [1, 3]  # Turn 2 not said with the change.
    test["after"]["replies"][1]["turn"] = 3
    test["after"]["goals"]["1:0"], test["after"]["goals_met"] = "unmet", []
    test["before"]["home_couldnt"] = test["after"]["home_couldnt"] = ["1:0"]
    test["judged"]["1:0"].update(home_couldnt=True, devices=["climate.den_heater"])  # As kept since it was added.
    for key, quote in (("2:1", "Say which"), ("3:2", "Don't guess"), ("3:3", "Don't guess"), ("4:4", "Less chat")):
        test["judged"][key] = {"summary": quote, "turn": int(key[0]), "root": "1:0", "devices": [], "kind": "conduct",
                               "quote": quote, "about": 1}
    test["before"]["goals"].update({"2:1": "unmet", "3:2": "unmet", "3:3": "unmet", "4:4": "indeterminate"})
    test["after"]["goals"].update({"2:1": "met", "3:2": "unmet", "3:3": "unmet", "4:4": "indeterminate"})
    guessed = {"state": "unmet", "answers": [{"turn": 1, "shows": False, "why": "."},
                                             {"turn": 3, "shows": True, "why": "It guessed."}]}
    test["after"]["conduct"] = {"2:1": {"state": "met", "answers": [{"turn": 1, "shows": False, "why": "Named it."}]},
                                "3:2": guessed, "3:3": guessed,
                                "4:4": {"state": "indeterminate", "answers": [], "set_aside": True}}
    shown = row(test)
    assert shown["goals"] == [{"goal": "Heat the den", "home_couldnt": ["climate.den_heater"]}]
    assert shown["open"][0]["reply"] == 2  # The second reply the replay with the change shows.
    item = {**proposal(home), "test": test, "cause": "C", "status": "awaiting_review", "created": "1",
            "decisions": [], "application": None, "history": []}
    review = panel.review(item, ([], [], []), hass=hass)
    [tested_on] = review["tests"]
    assert tested_on["summary"] == "Heat the den: the home couldn't do it (Den heater was unavailable)"
    assert review["followed"] == 1 and review["still_open"] == [
        {"quote": "\u201cDon't guess\u201d", "reply": 2, "why": "It guessed."},
        {"quote": "\u201cLess chat\u201d", "set_aside": True}]
    assert (tested_on["open"], tested_on["happened"]) == (2, 1)  # The chip counts the section's lines.
    assert editor.payload(item, editor.entity_names(hass, item))["tested"][0]["result"] == (
        "2 turn(s) instead of 3; no longer does what you pointed out: \u201cSay which\u201d; still does what you "
        "pointed out: \u201cDon't guess\u201d; the home couldn't do it: Den heater was unavailable when asked")


def test_the_editor_takes_what_is_fixed_and_what_is_open_from_the_verdicts_never_the_replies():
    """A household's broadcast revision: with the change, the judge found Assist no longer did what they
    pointed out (a conduct goal met), while the broadcast itself read unmet. The editor, told only the first, wrote
    "your complaint remains open" from the replayed reply: a contradiction on screen. It's told both from the verdicts:
    the complaint fixed, the broadcast still not done; and a broadcast that couldn't be told isn't "not done"."""
    from custom_components.assist_improver import editor
    quote = "You should have told me no speaker got it, not that it was sent"
    test = tested("Broadcast asking them to come in", said=("Broadcast on the speakers", "That didn't work"),
                  replies=("Sent.", "Filed."), after=("Done.", "Filed."), met_before=False, validated=True)
    test["after"]["goals"]["1:0"], test["after"]["goals_met"] = "unmet", ["3:1"]
    test["judged"]["3:1"] = {"summary": "Confirmed a broadcast no speaker got", "turn": "note", "root": "1:0",
                             "devices": [], "kind": "conduct", "quote": quote, "about": 1}
    test["before"]["goals"]["3:1"], test["after"]["goals"]["3:1"] = "unmet", "met"
    test["after"]["conduct"] = {"3:1": {"state": "met", "answers": [{"turn": 1, "shows": False, "why": "Checked."}]}}
    item = {"title": "Broadcast with a script", "cause": "C", "changes": [], "test": test}
    assert editor.payload(item)["tested"][0]["result"] == (
        f"no longer does what you pointed out: \u201c{quote}\u201d; still not done: Broadcast asking them to come in")
    assert "never from the replies" in editor.INSTRUCTIONS
    test["after"]["goals"]["1:0"] = "indeterminate"  # Couldn't be told: neither fixed nor still open.
    assert editor.payload(item)["tested"][0]["result"] == f"no longer does what you pointed out: \u201c{quote}\u201d"


def test_the_editor_is_never_told_a_turn_count_that_didnt_change():
    """A change that meets one part of a request (the left door, of both garage doors) in as many turns, the request
    itself still unmet: the editor was told "0 turn(s) instead of 0", garbage on screen. It's told the part now met
    and what's still not done, a count only where the turns went down; and the goal itself now met says that alone.
    One met before a step asked again, in as many turns (that turn still said for another request), says so."""
    from custom_components.assist_improver import editor
    test = tested("Open both garage doors", said=("Open both garage doors", "Both"), replies=("Which?", "Done."),
                  after=("Which?", "Done."), met_before=False, validated=True)
    test["after"]["goals"]["1:0"], test["after"]["goals_met"] = "unmet", ["1:1"]
    test["judged"]["1:1"] = {"summary": "Left garage door open", "turn": 1, "root": "1:0", "devices": [],
                             "kind": "state"}
    test["before"]["goals"]["1:1"], test["after"]["goals"]["1:1"] = "unmet", "met"
    item = {"title": "Both doors", "cause": "C", "changes": [], "test": test}
    result = lambda: editor.payload(item)["tested"][0]["result"]  # noqa: E731
    assert result() == "part of the goal now met: Left garage door open; still not done: Open both garage doors"
    test["after"]["goals"]["1:0"] = "met"
    assert result() == "goal now met (it went unmet before)"  # Its part goes with it.
    del test["judged"]["1:1"], test["before"]["goals"]["1:1"], test["after"]["goals"]["1:1"]
    test["before"]["goals"]["1:0"] = "met"  # Met both ways, at turn 2 without the change and at turn 1 with it.
    test["judged"]["2:1"] = {"summary": "Asked again", "turn": 2, "root": "1:0", "devices": [], "kind": "step"}
    test["before"]["goals"]["2:1"], test["after"]["goals"]["2:1"] = "unmet", "met"
    assert result() == "done before you had to ask again: Open both garage doors"


@pytest.mark.asyncio
async def test_written_copy_names_the_improvement_and_safety_is_stated_by_code(home, monkeypatch):
    from custom_components.assist_improver import editor, panel
    hass = home.hass
    improvements = Improvements(hass, None)
    await improvements.start()
    improvements.items["imp1"] = {**proposal(home), "cause": "Why", "status": "awaiting_review",
                                  "created": "1", "decisions": [], "application": None, "history": []}

    async def write(hass, item, entity_id):
        return {"status": "written", "entity_id": entity_id, "headline": "Expose garage door controls via scripts",
                "benefit": "Say open the garage doors and both open."}
    monkeypatch.setattr(editor, "write", write)
    assert await improvements.write_copy("ai_task.small", ["imp1"]) == []  # None failed.
    assert improvements.items["imp1"]["copy"]["entity_id"] == "ai_task.small" and improvements.unwritten() == []
    steps, already, _ = await plan_steps(hass, improvements.get("imp1"))
    shown = panel.review(improvements.get("imp1"), (steps, already, []))
    assert shown["title"] == "Expose garage door controls via scripts" and shown["benefit"].startswith("Say open")
    assert shown["safety"][0].startswith("Tested on your own conversations, replayed in a sandboxed copy")
    assert "Only settings change: 1 name added, 1 new script, 1 script updated" in shown["safety"][1]
    assert shown["safety"][-1].startswith("Checked against your Home Assistant first")
    assert shown["tests"][0]["result"] == "3 → 1 turns" and shown["tests"][0]["summary"] == "Open the garage"


@pytest.mark.asyncio
async def test_two_decisions_at_once_never_lose_the_undo_record(home):
    import asyncio
    improvements = Improvements(home.hass, None)
    await improvements.start()
    improvements.items["imp1"] = {**proposal(home), "cause": "", "copy": {"status": "failed"},
                                  "status": "awaiting_review", "created": "1",
                                  "decisions": [], "application": None, "history": []}
    save = Editors(home.hass).save
    first, second = await asyncio.gather(improvements.decide("imp1", "accept", user_id="alex", editors=save),
                                         improvements.decide("imp1", "accept", user_id="alex", editors=save),
                                         return_exceptions=True)
    # The second waited for the first, then saw it applied: it can't apply again over the first's undo record.
    from custom_components.assist_improver.cases import CaseError
    assert first["status"] == "applied" and isinstance(second, CaseError)
    item = improvements.get("imp1")
    assert item["status"] == "applied" and item["application"]["steps"]
