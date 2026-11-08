"""What can be proposed, and how it's checked (DETAILS P7.5, P7.5.5, P7.7.1): each kind in Home Assistant's own
format, checked by Home Assistant's own validators, then the safety rules HA can't hold.

Kai: "I'd like us to fix all those problems, but ideally as general and as broadly and cleanly as possible, instead of
us putting in specific patches for each problem we find." On automations and scenes: "could we just support these?"
"""

from types import MappingProxyType

import pytest
import pytest_asyncio
import yaml
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.setup import async_setup_component

from conftest import assist_home
from custom_components.assist_improver import changes
from custom_components.assist_improver.hypotheses.schema import HYPOTHESES

RELAY, LAMP, BLIND = "switch.fixture_relay", "light.fixture_lamp", "cover.fixture_blind"
PRESS = {"alias": "Press", "sequence": [{"action": "switch.turn_off", "target": {"entity_id": RELAY}},
                                        {"action": "switch.turn_on", "target": {"entity_id": RELAY}}]}
NIGHT = {"id": "night", "alias": "Night", "triggers": [{"trigger": "time", "at": "22:00:00"}],
         "actions": [{"action": "light.turn_off", "target": {"entity_id": LAMP}}]}
EVENING = {"id": "evening", "name": "Evening", "entities": {LAMP: {"state": "on", "brightness": 120}}}
# Kept by the household's own configuration, not by an editor: no change may touch them (rule 3).
PACKAGED = {"id": "packaged", "alias": "Packaged", "triggers": [{"trigger": "time", "at": "06:00:00"}],
            "actions": [{"action": "light.turn_on", "target": {"entity_id": LAMP}}]}
HOME = {
    "states": {RELAY: {"state": "on", "attributes": {"friendly_name": "Fixture relay"}},
               LAMP: {"state": "off", "attributes": {"friendly_name": "Fixture lamp",
                                                     "supported_color_modes": ["brightness"]}},
               BLIND: {"state": "open", "attributes": {"friendly_name": "Fixture blind"}},
               "script.press": {"state": "off", "attributes": {"friendly_name": "Press"}},
               "automation.night": {"state": "on", "attributes": {"id": "night", "friendly_name": "Night"}},
               "automation.packaged": {"state": "on", "attributes": {"id": "packaged", "friendly_name": "Packaged"}},
               "scene.evening": {"state": "unknown", "attributes": {"id": "evening", "friendly_name": "Evening"}}},
    "entities": {RELAY: {"entity_id": RELAY, "aliases": [], "device_id": "box", "disabled_by": None},
                 LAMP: {"entity_id": LAMP, "aliases": ["Lamp"], "device_id": None, "disabled_by": None},
                 "sensor.fixture_spare": {"entity_id": "sensor.fixture_spare", "aliases": [], "device_id": "box",
                                          "disabled_by": "user", "config_entry_id": "relay_box"},
                 # Another integration's scene, disabled: it has no state, but it holds its entity ID.
                 "scene.movie": {"entity_id": "scene.movie", "aliases": [], "device_id": None, "platform": "hue",
                                 "unique_id": "hue-movie", "disabled_by": "integration", "config_entry_id": "hue"},
                 # An automation the household disabled: no config entry reloads it as it's enabled.
                 "automation.retired": {"entity_id": "automation.retired", "aliases": [], "device_id": None,
                                        "disabled_by": "user", "config_entry_id": None}},
    "devices": {"box": {"id": "box", "name": "Relay box", "name_by_user": None, "area_id": None}},
    "areas": {"hall": {"id": "hall", "name": "Hall", "aliases": []}},
    "exposure": {RELAY: True, LAMP: True},
    "scripts": {"press": PRESS}, "automations": [NIGHT, PACKAGED], "scenes": [EVENING],
}
TARGET = "provider/sub1"  # An assistant, as the sessions name it (with_targets).


@pytest_asyncio.fixture
async def hass(hass):
    """The offline HA with Home Assistant's editors' files, which keep every item but the packaged automation, and an
    assistant on Assist."""
    await assist_home(hass, {RELAY: ("on", "Fixture relay"), LAMP: ("off", "Fixture lamp")})
    assert await async_setup_component(hass, "scene", {})  # Its actions, scene.apply among them, as at home.
    for name, content in (("scripts.yaml", {"press": PRESS}), ("automations.yaml", [NIGHT]),
                          ("scenes.yaml", [EVENING])):
        open(hass.config.path(name), "w").write(yaml.safe_dump(content))
    provider = ConfigEntry(domain="fixture_provider", title="Assistant", data={}, source="user", version=1,
                           minor_version=1, options={}, unique_id=None, discovery_keys=MappingProxyType({}),
                           state=ConfigEntryState.NOT_LOADED, entry_id="provider", subentries_data=[{
                               "data": {"prompt": "Be brief.", "llm_hass_api": ["assist", "fixture_api"]},
                               "subentry_type": "conversation", "title": "Assistant", "unique_id": None,
                               "subentry_id": "sub1"}])
    hass.config_entries._entries[provider.entry_id] = provider
    return hass


async def found(hass, *proposed):
    return await changes.check(hass, list(proposed), HOME)


# One proposal per kind uses a field or option nothing here lists, and passes Home Assistant's own validation.
INVENTED = [
    {"kind": "script", "id": "press", "config": {
        **PRESS, "mode": "queued", "max": 3, "icon": "mdi:gesture-tap",
        "fields": {"times": {"selector": {"number": {"min": 1, "max": 3}}, "default": 1}},
        "sequence": [{"repeat": {"count": "{{ times }}", "sequence": PRESS["sequence"]}},
                     {"delay": {"milliseconds": 500}, "continue_on_error": True}, {"stop": "Pressed."}]}},
    {"kind": "automation", "id": "dusk", "config": {
        "id": "dusk", "alias": "Dusk", "mode": "restart", "description": "The lamp at dusk on weekdays.",
        "triggers": [{"trigger": "sun", "event": "sunset", "offset": "-00:15:00"}],
        "conditions": [{"condition": "time", "weekday": ["mon", "tue", "wed", "thu", "fri"]}],
        "actions": [{"action": "light.turn_on", "target": {"entity_id": LAMP}, "data": {"transition": 30}}]}},
    {"kind": "automation", "id": "night", "on": False},
    {"kind": "scene", "id": "evening", "config": {**EVENING, "icon": "mdi:weather-sunset",
                                                  "entities": {LAMP: {"state": "on", "brightness": 80}}}},
    {"kind": "registry", "entity_id": LAMP, "name": "Hall lamp", "aliases": ["Lamp", "Reading light"],
     "area_id": "hall", "hidden_by": "user"},
    {"kind": "registry", "entity_id": "sensor.fixture_spare", "disabled_by": None},
    {"kind": "registry", "device_id": "box", "name_by_user": "Hall relay", "area_id": "hall"},
    {"kind": "registry", "area_id": "hall", "aliases": ["Entrance"]},
    {"kind": "assistant", "assistant": "Assistant", "target": TARGET, "text": "Answer in one sentence.",
     "llm_hass_api": ["assist"], "recommend": {"chat_model": "a larger model", "web_search": False}},
    {"kind": "preference", "for": "this_user", "text": "The hall lamp is the reading light."},
]


@pytest.mark.asyncio
async def test_every_kind_takes_home_assistants_own_format_whole_checked_by_its_own_validators(hass):
    """Suggestions can be scripts, automations, scenes, device renames or exposure, or an assistant's instructions:
    each carried in Home Assistant's own format, any field or option it takes passing, HA's validators deciding."""
    HYPOTHESES({"hypotheses": [{"title": "t", "goals": [], "cause": "c", "support": [], "conflicts": [],
                                "expected": "e", "changes": INVENTED}]})  # The proposer's schema takes them whole.
    assert await found(hass, *INVENTED) == [[]] * len(INVENTED)
    # What Home Assistant refuses, it refuses in its own words: an option it doesn't know, a trigger it can't read.
    script, automation = INVENTED[0], INVENTED[1]
    refused = await found(hass, {**script, "config": {**script["config"], "mode": "sometimes"}},
                          {**automation, "config": {**automation["config"], "triggers": [{"trigger": "nowhere"}]}})
    assert refused[0][0].startswith("Home Assistant doesn't accept this script:") and "mode" in refused[0][0]
    assert refused[1][0].startswith("Home Assistant doesn't accept this automation:")


@pytest.mark.asyncio
async def test_a_scenes_targets_are_checked_against_what_their_domains_actions_set(hass):
    scene = lambda entities: {"kind": "scene", "id": "evening", "config": {**EVENING, "entities": entities}}
    problems = await found(hass, scene({LAMP: {"state": "dim"}}), scene({LAMP: {"state": "on", "flavour": "sweet"}}),
                           scene({"light.nowhere": "on"}))
    assert problems[0] == [f"{LAMP} can't be 'dim': it can be off, on."]
    assert problems[1][0].startswith(f"no action of light sets flavour on {LAMP}")
    assert problems[2] == ["the scene sets light.nowhere, which isn't in the home."]


PICKED = {"action": "light.turn_on", "target": {"entity_id": "{{ states.light | first }}"}}
PICKED_SAYS = "light.turn_on's target comes from a script field, a template or trigger data."
LAMP_ON = [{"condition": "state", "entity_id": LAMP, "state": "on"}]


@pytest.mark.parametrize("config, problem", [
    ({"alias": "Any", "fields": {"what": {"selector": {"entity": {}}}},
      "sequence": [{"action": "scene.apply", "data": {"entities": "{{ what }}"}}]},
     "scene.apply's entities comes from a script field, a template or trigger data."),
    ({"alias": "Any", "sequence": [{"service_template": "light.turn_{{ 'on' }}", "target": {"entity_id": LAMP}}]},
     "a step's action comes from a script field, a template or trigger data."),
    ({"alias": "Any", "fields": {"words": {"selector": {"text": {}}}},
      "sequence": [{"action": "conversation.process", "data": {"text": "{{ words }}"}}]},
     "conversation.process hands the choice of action to a model as it runs."),
    # A fixed text, even to Home Assistant's own agent, still leaves what's done to whatever answers it as it runs.
    ({"alias": "Any", "sequence": [{"action": "conversation.process",
                                    "data": {"text": "Lock up", "agent_id": "conversation.home_assistant"}}]},
     "conversation.process hands the choice of action to a model as it runs."),
    ({"alias": "Any", "fields": {"which": {"selector": {"text": {}}}},
      "sequence": [{"action": "script.turn_on", "target": {"entity_id": "script.press"},
                    "data": {"variables": {"which": "{{ which }}"}}}]},
     "script.turn_on passes variables from a script field, a template or trigger data."),
    ({"alias": "Any", "fields": {"which": {"selector": {"entity": {}}}},
      "sequence": [{"action": "light.turn_on", "target": {"entity_id": "{{ which }}"}}]},
     "light.turn_on's target comes from a script field, a template or trigger data."),
    # As Home Assistant runs it: what its validators make of it, not the shape it's written in.
    ({"alias": "Any", "sequence": [{"action": "automation.trigger", "target": {"entity_id": "automation.night"},
                                    "data": {"variables": {"who": "{{ states.lock | first }}"}}}]},
     "automation.trigger passes variables from a script field, a template or trigger data."),
    ({"alias": "Any", "sequence": [{"action": "script.press", "data": {"which": "{{ states.lock | first }}"}}]},
     "script.press passes variables from a script field, a template or trigger data."),
    ({"alias": "Any", "sequence": [{"action": "light.turn_on", "data": {"{{ 'entity_id' }}": "light.any"}}]},
     "light.turn_on's data comes from a template, which can pick what it acts on."),
    ({"alias": "Any", "sequence": [{"action": "light.turn_on", "data": "{{ {'entity_id': 'light.any'} }}"}]},
     "light.turn_on's data comes from a template, which can pick what it acts on."),
    # Not even a select field's: Home Assistant doesn't hold a call to its options (Assist's model calls a script with
    # any value), so it would pick any action or device.
    ({"alias": "Any", "fields": {"which": {"selector": {"select": {"options": [RELAY]}}}},
      "sequence": [{"action": "switch.turn_on", "target": {"entity_id": "{{ which }}"}}]},
     "switch.turn_on's target comes from a script field, a template or trigger data."),
    ({"alias": "Any", "fields": {"how": {"selector": {"select": {"options": ["light.turn_on", "light.turn_off"]}}}},
      "sequence": [{"action": "{{ how }}", "target": {"entity_id": LAMP}}]},
     "a step's action comes from a script field, a template or trigger data."),
    # Only a field taking a plain value (text, a number, ...) may: one picking a config entry, any field of an action
    # Home Assistant doesn't describe, and an event's data (handed to whatever it sets off) may not.
    ({"alias": "Any", "sequence": [{"action": "homeassistant.reload_config_entry",
                                    "data": {"entry_id": "{{ states.lock | first }}"}}]},
     "homeassistant.reload_config_entry's entry_id comes from a script field, a template or trigger data."),
    ({"alias": "Any", "sequence": [{"action": "python_script.do", "data": {"light": "{{ states.lock | first }}"}}]},
     "python_script.do's light comes from a script field, a template or trigger data."),
    ({"alias": "Any", "sequence": [{"event": "household_go", "event_data": {"entity": "{{ states.lock | first }}"}}]},
     "the household_go event passes data from a script field, a template or trigger data."),
    # A fixed value in data doesn't hide a templated one under the same key in data_template, which Home Assistant runs.
    ({"alias": "Any", "sequence": [{"action": "light.turn_off", "data": {"entity_id": LAMP},
                                    "data_template": {"entity_id": "{{ states.lock | first }}"}}]},
     "light.turn_off's entity_id comes from a script field, a template or trigger data."),
    ({"alias": "Any", "sequence": [{"action": "script.turn_on", "target": {"entity_id": "script.press"},
                                    "data": {"variables": {"who": "guest"}},
                                    "data_template": {"variables": {"who": "{{ states.lock | first }}"}}}]},
     "script.turn_on passes variables from a script field, a template or trigger data."),
    ({"alias": "Any", "sequence": [{"wait_for_trigger": [{"triggers": [
        {"trigger": "webhook", "webhook_id": "fixture-hook", "local_only": False}]}]}]},
     "a webhook trigger is local only (local_only: false would let anyone on the internet run it)."),
    # Wherever a step is nested, in each form Home Assistant takes.
    ({"alias": "Any", "sequence": [{"if": LAMP_ON, "then": [PICKED]}]}, PICKED_SAYS),
    ({"alias": "Any", "sequence": [{"if": LAMP_ON, "then": [{"delay": 1}], "else": [PICKED]}]}, PICKED_SAYS),
    ({"alias": "Any", "sequence": [{"sequence": [PICKED]}]}, PICKED_SAYS),
    ({"alias": "Any", "sequence": [{"choose": [{"conditions": LAMP_ON, "sequence": [{"delay": 1}]}],
                                    "default": [PICKED]}]}, PICKED_SAYS),
    ({"alias": "Any", "sequence": [{"parallel": [PICKED]}]}, PICKED_SAYS),
    ({"alias": "Any", "sequence": [{"parallel": [{"sequence": [PICKED]}]}]}, PICKED_SAYS),
    ({"alias": "Any", "sequence": [{"parallel": [[PICKED]]}]}, PICKED_SAYS),
    ({"alias": "Any", "sequence": [{"repeat": {"while": LAMP_ON, "sequence": [PICKED]}}]}, PICKED_SAYS),
    ({"alias": "Any", "sequence": [{"parallel": [{"sequence": [{"if": LAMP_ON, "then": [
        {"choose": [], "default": [{"repeat": {"while": LAMP_ON, "sequence": [PICKED]}}]}]}]}]}]}, PICKED_SAYS),
], ids=["scene-apply-entities", "service-template", "conversation-text", "conversation-fixed", "script-variables",
        "target-from-field", "automation-variables", "script-service-data", "templated-data-key",
        "templated-data-whole", "select-picks-target", "select-picks-action", "config-entry-field",
        "undescribed-field", "event-data", "data-template-under-data", "variables-template-under-data",
        "nested-webhook-wait", "in-if-then", "in-else", "in-sequence", "in-choose-default", "in-parallel-step",
        "in-parallel-sequence", "in-parallel-list", "in-repeat-while", "in-deep-mix"])
@pytest.mark.asyncio
async def test_no_action_or_target_takes_its_value_from_a_field_a_template_or_trigger_data(hass, config, problem):
    """Rule 2: what a step does and what it acts on are fixed in the configuration the household reviews."""
    assert problem in (await found(hass, {"kind": "script", "id": "any", "config": config}))[0]


@pytest.mark.asyncio
async def test_a_field_picks_only_between_fixed_branches_and_a_webhook_stays_local(hass):
    """A field may choose between actions and targets the configuration fixes, as branches on its value: whatever value
    a call brings, only those can run."""
    branch = lambda entity: {  # noqa: E731
        "conditions": [{"condition": "template", "value_template": f"{{{{ which == '{entity}' }}}}"}],
        "sequence": [{"action": "switch.turn_on", "target": {"entity_id": entity}}]}
    branches = {"kind": "script", "id": "pick", "config": {
        "alias": "Pick", "fields": {"which": {"selector": {"select": {"options": [RELAY, "switch.fixture_other"]}}}},
        "sequence": [{"choose": [branch(RELAY), branch("switch.fixture_other")]}]}}
    hook = lambda local: {"kind": "automation", "id": "hook", "config": {
        "id": "hook", "alias": "Hook", "triggers": [{"trigger": "webhook", "webhook_id": "fixture-hook",
                                                     "local_only": local}],
        "actions": [{"action": "light.turn_on", "target": {"entity_id": LAMP}}]}}
    nested = {"kind": "automation", "id": "hook", "config": {**hook(False)["config"], "triggers": [
        {"triggers": hook(False)["config"]["triggers"]}]}}  # Home Assistant flattens it into the list.
    picked, local, public, flattened = await found(hass, branches, hook(True), hook(False), nested)
    assert picked == [] and local == []
    assert public == flattened == [
        "a webhook trigger is local only (local_only: false would let anyone on the internet run it)."]


@pytest.mark.asyncio
async def test_a_new_scene_or_script_takes_an_entity_id_no_one_has(hass):
    """Home Assistant would give a new scene named Movie scene.movie_2 while another integration's disabled scene holds
    scene.movie: its review, exposure and replay would all name the other one."""
    movie = {"kind": "scene", "id": "movie", "config": {"id": "movie", "name": "Movie",
                                                        "entities": {LAMP: {"state": "on"}}}}
    lamp = {"kind": "script", "id": "lamp", "config": {"alias": "Lamp", "sequence": []}}
    home = {**HOME, "states": {**HOME["states"], "script.lamp": {"state": "off", "attributes": {}}}}
    assert await changes.check(hass, [movie, lamp], home) == [
        ["scene.movie exists already; give the new scene another name."],
        ["script.lamp exists already; give the new script another id."]]


@pytest.mark.asyncio
async def test_an_edit_leaves_an_automation_on_or_off_as_it_is(hass):
    """Saved with an initial_state other than its state now, an automation would be turned on or off by the save, and
    its undo (the old configuration saved back) leaves it so: Home Assistant restores the state it had last. Turning
    it on or off is its own change, which undo puts back."""
    edit = lambda initial: {"kind": "automation", "id": "night", "config": {**NIGHT, "initial_state": initial}}
    assert await found(hass, edit(False), edit(True)) == [
        ["its initial_state would turn it off as it's saved, which undo can't put back."], []]
    new = {"kind": "automation", "id": "dawn", "config": {**NIGHT, "id": "dawn", "initial_state": False}}
    assert await found(hass, new) == [[]]  # A new one is deleted by its undo.


@pytest.mark.parametrize("item_id", ["../../../services/script/press", "%2e%2e/events/x", "night?x=1", "a b"])
@pytest.mark.asyncio
async def test_an_items_id_is_a_plain_token_so_saving_it_reaches_only_its_editor(hass, item_id):
    """The panel saves an item at config/<kind>/config/<id>: an id that isn't a plain token could take that call to
    another of Home Assistant's endpoints (a script's service, an event), running something as the change is accepted."""
    automation = {"kind": "automation", "id": item_id, "config": {**NIGHT, "id": item_id}}
    scene = {"kind": "scene", "id": item_id, "config": {**EVENING, "id": item_id, "name": "Other"}}
    assert await found(hass, automation, scene) == [[f"its id is letters, digits, _ and - only, not {item_id!r}."]] * 2


@pytest.mark.asyncio
async def test_only_items_home_assistants_editors_keep_can_change(hass):
    """Rule 3: the packaged automation is the household's own configuration, which no editor saves: neither edited
    nor turned on or off here."""
    edit = {"kind": "automation", "id": "packaged", "config": {**PACKAGED, "alias": "Packaged, later"}}
    off = {"kind": "automation", "id": "packaged", "on": False}
    assert await found(hass, edit, off) == [["automation 'packaged' isn't kept by Home Assistant's automation editor "
                                             "(automations.yaml), so it can't be changed here."]] * 2


@pytest.mark.asyncio
async def test_an_assistant_change_never_adds_an_llm_api_and_recommends_only_other_options(hass):
    adds, same, recommends = await found(
        hass, {"kind": "assistant", "target": TARGET, "llm_hass_api": ["assist", "fixture_api", "another"]},
        {"kind": "assistant", "target": TARGET, "llm_hass_api": ["fixture_api", "assist"]},
        {"kind": "assistant", "target": TARGET, "recommend": {"llm_hass_api": ["more"], "prompt": "x"}})
    assert adds == ["an assistant change never adds an LLM API (another); it uses assist, fixture_api."]
    assert same == ["it uses these LLM APIs already."]
    assert recommends == ["recommend names options other than llm_hass_api and prompt, which a change sets."]


@pytest.mark.asyncio
async def test_a_registry_change_takes_only_what_home_assistants_registries_do(hass):
    problems = await found(
        hass, {"kind": "registry", "entity_id": LAMP, "area_id": "attic"},
        {"kind": "registry", "device_id": "box", "aliases": ["x"]},
        {"kind": "registry", "entity_id": RELAY, "disabled_by": None},
        {"kind": "registry", "entity_id": LAMP, "expose": True},
        {"kind": "registry", "entity_id": "automation.retired", "disabled_by": None})
    assert problems == [["there's no area 'attic'."],
                        ["a registry change on a device takes name_by_user, area_id, not aliases."],
                        [f"{RELAY} isn't disabled."], [f"{LAMP} is already exposed to Assist."],
                        ["only an entity whose config entry reloads as it's enabled is enabled here; "
                         "automation.retired has none."]]


@pytest.mark.asyncio
async def test_what_a_change_touches_is_read_as_home_assistant_reads_it(hass):
    """Readers: a script's and automation's Script (top_level=False) referenced_* sets, conditions' and triggers'
    own extractors, a scene's entities' keys, a registry change's entry."""
    automation = {"kind": "automation", "id": "dusk", "config": {
        **INVENTED[1]["config"], "triggers": [{"trigger": "state", "entity_id": BLIND, "to": "closed"}],
        "actions": [{"choose": [{"conditions": [{"condition": "state", "entity_id": RELAY, "state": "on"}],
                                 "sequence": [{"action": "light.turn_on", "target": {"area_id": "hall"}}]}]}]}}
    read = changes.referenced(hass, [automation, INVENTED[3], INVENTED[6]], HOME)
    assert read["entities"] == {BLIND, RELAY, LAMP, "scene.evening"}
    assert read["areas"] == {"hall"} and read["devices"] == {"box"}
