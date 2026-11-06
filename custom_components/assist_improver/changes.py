"""A hypothesis's changes, each in Home Assistant's own format: what can be proposed, how it's checked, what it
touches, and what a sandbox runs (DETAILS P7.5).

The kinds, each mirroring one place Home Assistant keeps it:
- script, automation, scene: one item of Home Assistant's editors, by its id there (a script's object ID, an
  automation's or scene's configuration ID), its whole configuration as that editor takes it (config); an
  automation turned on or off (on). A new script or scene is exposed to Assist unless expose is false.
- registry: what Home Assistant's registry updates take: an entity's name, aliases, area_id, hidden_by, disabled_by
  (only cleared, to enable it) and exposure to Assist (expose); a device's name_by_user and area_id; an area's aliases
  (area_id alone).
- assistant: one assistant's own instructions (text in place of the lines in replaces, as instructions.edit places
  them) and its LLM APIs, only ever fewer (llm_hass_api); any other option is a recommendation (recommend), named for
  the household to change by hand, never applied or replayed.
- preference: a household line (household.py), for everyone or the person speaking.

Checks (check): Home Assistant's own validators (scripts' and automations' schemas and its async trigger, condition
and action validators; a scene's own schema, and each entity's target against its domain's services and options),
then what they can't check: the safety rules (DETAILS P7.5): never a config entry's data (no kind reaches it); no
action, and no service field but a plain value's, from a script field, a template or trigger data (unsafe); and only
items Home Assistant's editors keep (editor_items). Readers (referenced) read what a change touches as Home Assistant
does: a script's Script (top_level=False) and its referenced_* sets, conditions' and triggers' own extractors, a
scene's entities' keys.
"""

from copy import deepcopy
import os
import re

import probatio

from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.template import Template, is_template_string
from homeassistant.util import slugify

from . import touchpoints
from .cases import CaseError

CONFIG_KINDS = ("script", "automation", "scene")  # Items of Home Assistant's editors.
# An editor item's id, which ends the address of its editor's endpoint (config/<kind>/config/<id>): a plain token.
ITEM_ID = re.compile(r"[A-Za-z0-9_-]+")
EDITORS = {"script": "script_config_path", "automation": "automation_config_path", "scene": "scene_config_path"}
PLATFORMS = {"script": "script", "automation": "automation", "scene": "homeassistant"}  # Each kind's entity platform.
ENTITY_FIELDS = ("name", "aliases", "area_id", "hidden_by", "disabled_by", "expose")  # A registry change's, by entry.
DEVICE_FIELDS = ("name_by_user", "area_id")
AREA_FIELDS = ("aliases",)
# The selectors of a service field that takes a plain value (rule 2), never what the action acts on: any other field
# (an entity's, a config entry's, an object's, an undescribed one) picks what it acts on, or hands values on.
VALUE_SELECTORS = {"text", "number", "select", "boolean", "date", "time", "datetime", "duration", "constant",
                   "color_rgb", "color_temp", "language", "country", "location", "theme", "icon", "state", "attribute",
                   "template", "numeric_threshold", "unit_of_measurement", "device_class", "state_class", "qr_code"}
TARGET_KEYS = tuple(str(k) for k in cv.ENTITY_SERVICE_FIELDS)
NESTED = ("sequence", "then", "else", "default")  # Where a step holds more steps.
TEXT_DOMAINS = {"text", "input_text", "date", "time", "datetime", "input_datetime"}  # A scene sets any text there.
NUMBER_DOMAINS = {"number", "input_number"}  # A number within the entity's min and max.
OPTIONS = ("options", "hvac_modes", "operation_list")  # The states an entity lists for itself.
# What of a setup (setup_from_changes's) can change how Home Assistant resolves a recorded call; the rest (household
# lines, instructions, LLM APIs) changes only what the model reads.
RESOLVING = ("exposure", "aliases", "alias_removals", "scripts", "automations", "automation_on", "scenes", "registry")


def kind_of(change):
    """A registry change's entry (entity, device or area), as Home Assistant's registry updates name it."""
    return "entity" if change.get("entity_id") else "device" if change.get("device_id") else "area"


def automation_entity(home, automation_id):
    """The entity of the home's automation with this configuration ID, or None."""
    return _by_id(home, "automation", automation_id)


def _by_id(home, domain, item_id):
    return next((e for e, s in (home.get("states") or {}).items() if e.startswith(f"{domain}.")
                 and str(((s or {}).get("attributes") or {}).get("id")) == str(item_id)), None)


def item_entity(home, change):
    """An editor item's entity: a script's by its object ID; an automation's or scene's by the configuration ID its
    state names, a new scene's as Home Assistant will make it (new_entity); None for a new automation."""
    kind, item_id = change["kind"], change["id"]
    if kind == "script":
        return f"script.{item_id}"
    found = _by_id(home, kind, item_id)
    return found or (new_entity(home, change) if kind == "scene" and existing(home, kind, item_id) is None else None)


def existing(home, kind, item_id):
    """The home's configuration of an editor kind's item with this id, or None (a script by its object ID)."""
    if kind == "script":
        return (home.get("scripts") or {}).get(item_id)
    return next((i for i in home.get(f"{kind}s") or [] if str(i.get("id")) == str(item_id)), None)


def new_entity(home, change):
    """The entity a new script or scene gets: a script's by its object ID, a scene's by its name, as Home Assistant
    makes them."""
    if change["kind"] == "script":
        return f"script.{change['id']}"
    return f"scene.{slugify((change.get('config') or {}).get('name') or change['id'])}"


def new_entity_problem(kind, item_id, config, states, entries):
    """What's wrong with the entity a new script or scene gets (new_entity), or None: an entity ID Home Assistant has
    already (a state, or another item's registry entry: a disabled scene of another integration, say) would give it
    another one than its review, exposure and replay name. states, entries: by entity ID (entries: registry entries,
    their platform and unique_id)."""
    entity = new_entity({}, {"kind": kind, "id": item_id, "config": config})
    entry = entries.get(entity)
    own = entry is not None and (entry.get("platform"), str(entry.get("unique_id"))) == (PLATFORMS[kind], item_id)
    if not own and (entry is not None or entity in states):
        return f"{entity} exists already; give the new {kind} another {'id' if kind == 'script' else 'name'}."
    return None


async def editor_items(hass, kind):
    """The items Home Assistant's editor for a kind keeps, read as its own GET reads them (load_yaml of its file):
    {id: configuration}, a script by its object ID, an automation or scene by its configuration ID."""
    from homeassistant.util.yaml import load_yaml
    path = hass.config.path(touchpoints.get(hass, EDITORS[kind]))
    found = await hass.async_add_executor_job(lambda: load_yaml(path) if os.path.isfile(path) else None)
    if kind == "script":
        return {str(k): v for k, v in found.items()} if isinstance(found, dict) else {}
    return {str(i["id"]): i for i in found or [] if isinstance(i, dict) and i.get("id") is not None}


# Checking.

async def check(hass, changes, home):
    """What's wrong with each change (a list per change, in order; empty: nothing), checked by Home Assistant's own
    validators on this instance, the safety rules and the home (today's setup: snapshot.py's). An assistant change
    names its assistant by target (the sessions' with_targets)."""
    from homeassistant.helpers.service import async_get_all_descriptions
    descriptions = await async_get_all_descriptions(hass)
    editors = {kind: await editor_items(hass, kind) for kind in CONFIG_KINDS
               if any(c.get("kind") == kind for c in changes)}
    assistants = {c["target"]: apis_now(hass, c["target"]) for c in changes
                  if c.get("kind") == "assistant" and c.get("target")}
    return [await _problems(hass, change, home, descriptions, editors, assistants) for change in changes]


def apis_now(hass, target):
    """The LLM APIs an assistant (target: "config entry/subentry") uses now, or None when it isn't set up."""
    entry_id, _, subentry_id = (target or "").partition("/")
    entry = hass.config_entries.async_get_entry(entry_id)
    subentry = entry.subentries.get(subentry_id) if entry else None
    if subentry is None:
        return None
    apis = subentry.data.get("llm_hass_api") or []
    return [apis] if isinstance(apis, str) else list(apis)


async def _problems(hass, change, home, descriptions, editors, assistants):
    kind = change.get("kind")
    if kind in CONFIG_KINDS:
        return await _config_problems(hass, change, home, descriptions, editors[kind])
    if kind == "registry":
        return registry_problems(change, home)
    if kind == "assistant":
        return assistant_problems(change, assistants)
    return []


def not_kept(kind, item_id):
    """Rule 3's refusal of an item Home Assistant's editor for its kind doesn't keep."""
    return f"{kind} {item_id!r} isn't kept by Home Assistant's {kind} editor ({kind}s.yaml), so it can't be changed here."


async def _config_problems(hass, change, home, descriptions, editor):
    """An editor item's problems: its id, that the editor keeps it, then Home Assistant's validators and rule 2."""
    kind, item_id = change["kind"], change.get("id")
    if not isinstance(item_id, str) or not item_id.strip():
        return [f"a {kind} change names its item by id (its {'object ID' if kind == 'script' else 'configuration ID'})."]
    if kind == "automation" and "on" in change:
        entity = automation_entity(home, item_id)
        if "config" in change:
            return ["an automation change gives its config, or turns it on or off (on), not both."]
        if entity is None:
            return [f"there's no automation with id {item_id!r}."]
        if item_id not in editor:  # Rule 3.
            return [not_kept(kind, item_id)]
        if (((home.get("states") or {}).get(entity) or {}).get("state") == "on") == change["on"]:
            return [f"{entity} is already {'on' if change['on'] else 'off'}."]
        return []
    if problem := id_problem(item_id):
        return [problem]
    config, have = change.get("config"), existing(home, kind, item_id)
    if not isinstance(config, dict):
        return [f"a {kind} change gives its whole configuration (config), as Home Assistant's {kind} editor takes it."]
    if have is not None and item_id not in editor:  # Rule 3.
        return [not_kept(kind, item_id)]
    if kind != "script" and str(config.get("id", item_id)) != item_id:
        return [f"its config's id is {config.get('id')!r}, not the change's {item_id!r}."]
    found = []
    if have is None:
        if kind == "script" and cv.slug(item_id) != item_id:
            found.append(f"a new script's id is its object ID: lowercase letters, digits and _ ({cv.slug(item_id)!r}).")
        elif kind in ("script", "scene") and (problem := new_entity_problem(
                kind, item_id, config, home.get("states") or {}, home.get("entities") or {})):
            found.append(problem)
        elif kind == "script" and item_id in editor:
            found.append(f"script {item_id!r} exists already in scripts.yaml.")
    elif "expose" in change:
        found.append(f"expose is for a new {kind}; an existing one's exposure is a registry change.")
    validated, problems = await validated_item(hass, kind, config, descriptions)
    if validated is not None and kind == "scene":
        problems = await _scene_problems(hass, validated, home, descriptions)
    if validated is not None and kind == "automation" and have is not None:
        entity = automation_entity(home, item_id)
        problems += initial_state_problems(validated, ((home.get("states") or {}).get(entity) or {}).get("state"))
    return found + problems


def initial_state_problems(validated, state):
    """An edited automation's initial_state (validated), against its state now (on or off): one that differs would
    turn it on or off as it's saved, and its undo wouldn't turn it back (Home Assistant restores its last state)."""
    if "initial_state" not in validated or state not in ("on", "off") or validated["initial_state"] == (state == "on"):
        return []
    return [f"its initial_state would turn it {'on' if validated['initial_state'] else 'off'} as it's saved, which "
            "undo can't put back."]


def id_problem(item_id):
    """What's wrong with an editor item's id (ITEM_ID), or None: anything else could take its editor's address
    elsewhere."""
    return None if ITEM_ID.fullmatch(item_id) else f"its id is letters, digits, _ and - only, not {item_id!r}."


async def validated_item(hass, kind, config, descriptions):
    """(validated, problems) of an editor item's configuration: Home Assistant's validators for its kind, then rule 2
    (unsafe); validated is None when Home Assistant doesn't accept it. Applying runs it again on the item as it would
    be saved."""
    try:
        validated = await VALIDATORS[kind](hass, {k: v for k, v in config.items() if not (kind != "script" and k == "id")})
    except (probatio.Invalid, ValueError) as err:
        return None, [f"Home Assistant doesn't accept this {kind}: {_said(err, config)}"]
    except Exception as err:  # noqa: BLE001 - a validator's refusal (HomeAssistantError and its kin) is the answer.
        return None, [f"Home Assistant doesn't accept this {kind}: {err}"]
    return validated, [] if kind == "scene" else unsafe(kind, validated, descriptions)


def _said(err, config):
    from probatio.humanize import humanize_error
    return humanize_error(config, err) if isinstance(err, probatio.Invalid) else str(err)


async def _script(hass, config):
    """A script's configuration, as Home Assistant's script integration validates it: its schema, then its actions."""
    from homeassistant.helpers.script import async_validate_actions_config
    validated = touchpoints.get(hass, "script_schema")(deepcopy(config))
    validated["sequence"] = await async_validate_actions_config(hass, validated["sequence"])
    return validated


def automation_schema():
    """An automation's configuration's static schema, from public pieces as Home Assistant's own is made: the plural
    key renames, then the script helper's schema over the trigger, condition and action schemas."""
    from homeassistant.helpers import script
    return probatio.All(
        cv.renamed("trigger", "triggers"), cv.renamed("action", "actions"), cv.renamed("condition", "conditions"),
        script.make_script_schema({
            probatio.Optional("alias"): cv.string, probatio.Optional("description"): cv.string,
            probatio.Optional("initial_state"): cv.boolean, probatio.Optional("trace"): dict,
            probatio.Required("triggers"): cv.TRIGGER_SCHEMA, probatio.Optional("conditions"): cv.CONDITIONS_SCHEMA,
            probatio.Optional("variables"): cv.SCRIPT_VARIABLES_SCHEMA,
            probatio.Optional("trigger_variables"): cv.SCRIPT_VARIABLES_SCHEMA,
            probatio.Required("actions"): cv.SCRIPT_SCHEMA}, script.SCRIPT_MODE_SINGLE))


async def _automation(hass, config):
    """An automation's configuration: the static schema, then Home Assistant's async trigger, condition and action
    validators."""
    from homeassistant.helpers.condition import async_validate_conditions_config
    from homeassistant.helpers.script import async_validate_actions_config
    from homeassistant.helpers.trigger import async_validate_trigger_config
    validated = automation_schema()(deepcopy(config))
    validated["triggers"] = await async_validate_trigger_config(hass, validated["triggers"])
    if validated.get("conditions"):
        validated["conditions"] = await async_validate_conditions_config(hass, validated["conditions"])
    validated["actions"] = await async_validate_actions_config(hass, validated["actions"])
    return validated


async def _scene(hass, config):
    """A scene's configuration, through Home Assistant's own scene platform's schema: its entities as States."""
    schema = touchpoints.get(hass, "scene_schema")
    return schema({"platform": "homeassistant", "states": [deepcopy(config)]})["states"][0]


VALIDATORS = {"script": _script, "automation": _automation, "scene": _scene}


async def _scene_problems(hass, validated, home, descriptions):
    """Each of a scene's entities, its target checked against its domain's services and options: an entity of the
    home's, of a domain whose integration reproduces states, a state its domain or the entity lists, and attributes a
    service of its domain sets (or as the entity has them now: Home Assistant's scene editor keeps every attribute)."""
    from homeassistant.loader import IntegrationNotFound, async_get_integration
    found, states, entities = [], home.get("states") or {}, home.get("entities") or {}
    for entity_id, target in (validated.get("entities") or {}).items():
        domain = entity_id.split(".", 1)[0]
        if entity_id not in states and entity_id not in entities:
            found.append(f"the scene sets {entity_id}, which isn't in the home.")
            continue
        try:
            reproduces = (await async_get_integration(hass, domain)).platforms_exists(("reproduce_state",))
        except IntegrationNotFound:
            reproduces = False
        if not reproduces:
            found.append(f"a scene can't set {entity_id}: Home Assistant's {domain} integration doesn't reproduce "
                         "states.")
            continue
        now = ((states.get(entity_id) or {}).get("attributes")) or {}
        settable = settable_attributes(domain, descriptions)
        if left := sorted(k for k, v in target.attributes.items() if k not in settable and now.get(k) != v):
            found.append(f"no action of {domain} sets {', '.join(left)} on {entity_id} (they set "
                         f"{', '.join(sorted(settable)) or 'nothing'}).")
        if (allowed := valid_states(domain, now, descriptions)) is not None and target.state not in allowed:
            found.append(f"{entity_id} can't be {target.state!r}: it can be {', '.join(sorted(allowed))}.")
        elif domain in NUMBER_DOMAINS and not _in_range(target.state, now):
            found.append(f"{entity_id} can't be {target.state!r}: it takes a number from {now.get('min')} to "
                         f"{now.get('max')}.")
    return found


def settable_attributes(domain, descriptions):
    """The attributes a domain's actions set: each field of its actions, by the attribute it sets (standin.FIELDS)."""
    from .standin import FIELDS
    out = set()
    for service in (descriptions.get(domain) or {}).values():
        for field in _fields(service):
            renamed = FIELDS.get(field, field)
            out.add(renamed.get(domain, field) if isinstance(renamed, dict) else renamed)
    return out


def valid_states(domain, attributes, descriptions):
    """The states an entity of this domain can be set to: its domain's own (the states its commands leave it in, in
    Home Assistant's enums), on and off where its domain turns on and off, and those it lists itself (options, HVAC
    modes, operation modes); None where any text or number goes (TEXT_DOMAINS, NUMBER_DOMAINS)."""
    from .standin import _GONE, _NOW, _table
    if domain in TEXT_DOMAINS or domain in NUMBER_DOMAINS:
        return None
    out = {str(state) for state, _ in (_table().get(domain) or {}).values()
           if state is not None and state not in (_NOW, _GONE) and not callable(state)}
    if {"turn_on", "turn_off"} <= set(descriptions.get(domain) or {}):
        out |= {"on", "off"}
    for key in OPTIONS:
        out |= {str(o) for o in attributes.get(key) or []}
    return out


def _in_range(state, attributes):
    try:
        value = float(state)
    except (TypeError, ValueError):
        return False
    low, high = attributes.get("min"), attributes.get("max")
    return (low is None or value >= float(low)) and (high is None or value <= float(high))


def _fields(service):
    """An action's fields by name, those in its sections too."""
    out = {}
    for name, field in (service.get("fields") or {}).items():
        out.update(_fields(field) if "fields" in field and "selector" not in field else {name: field})
    return out


def registry_problems(change, home):
    """A registry change, checked against today's registries: the entry it names, only the fields that entry takes,
    an area that exists, and an entity enabled only where its device and integration are, and only one of a config
    entry, which Home Assistant reloads as it's enabled (another loads at a later reload or restart: an automation,
    which then runs unreviewed)."""
    entry = kind_of(change)
    fields = {"entity": ENTITY_FIELDS, "device": DEVICE_FIELDS, "area": AREA_FIELDS}[entry]
    given = [k for k in (*ENTITY_FIELDS, *DEVICE_FIELDS) if k in change and not (entry == "area" and k == "area_id")]
    on = f"{'a' if entry == 'device' else 'an'} {entry}"
    if extra := [k for k in given if k not in fields]:
        return [f"a registry change on {on} takes {', '.join(fields)}, not {', '.join(extra)}."]
    if not given:
        return [f"a registry change on {on} gives what to set: {', '.join(fields)}."]
    if entry == "area":
        area = (home.get("areas") or {}).get(change.get("area_id"))
        if area is None:
            return [f"there's no area {change.get('area_id')!r}."]
        return _aliases_problems(change, area.get("aliases"))
    found = []
    if change.get("area_id") is not None and change["area_id"] not in (home.get("areas") or {}):
        found.append(f"there's no area {change['area_id']!r}.")
    if entry == "device":
        device = (home.get("devices") or {}).get(change["device_id"])
        if device is None:
            return [f"there's no device {change['device_id']!r}."]
        if all(device.get(k) == change[k] for k in given):
            found.append(f"device {change['device_id']} has these already.")
        return found
    entity_id = change["entity_id"]
    record, state = (home.get("entities") or {}).get(entity_id), (home.get("states") or {}).get(entity_id)
    if record is None and state is None:
        return [f"there's no entity {entity_id!r}."]
    if record is None and given != ["expose"]:
        return [f"{entity_id} has no registry entry, so only its exposure (expose) can change."]
    if "aliases" in change:
        found += _aliases_problems(change, record.get("aliases"))
    if "disabled_by" in change:
        device = (home.get("devices") or {}).get(record.get("device_id")) or {}
        if record.get("disabled_by") is None:
            found.append(f"{entity_id} isn't disabled.")
        elif record["disabled_by"] in ("device", "config_entry") or device.get("disabled_by"):
            found.append(f"{entity_id} is disabled through its device or integration, so it stays disabled.")
        elif not record.get("config_entry_id"):  # An automation, say: it would load, and run, at the next reload.
            found.append(f"only an entity whose config entry reloads as it's enabled is enabled here; {entity_id} has "
                         "none.")
    if "expose" in change and bool((home.get("exposure") or {}).get(entity_id)) == change["expose"]:
        found.append(f"{entity_id} is already {'exposed to' if change['expose'] else 'hidden from'} Assist.")
    if record is not None and all(record.get(k) == change[k] for k in given) \
            and not {"expose", "aliases", "disabled_by"} & set(given):
        found.append(f"{entity_id} has these already.")
    return found


def _aliases_problems(change, current):
    aliases = change.get("aliases") or []
    if any(not (a or "").strip() for a in aliases) or len(set(aliases)) < len(aliases):
        return ["aliases are names, each once."]
    if [a for a in current or [] if a is not None] == aliases:
        return ["it has these aliases already."]
    return []


def assistant_problems(change, assistants):
    """An assistant change's LLM APIs, only ever fewer, and its recommendations, of options other than these two."""
    found = []
    if not any(k in change for k in ("text", "replaces", "llm_hass_api", "recommend")):
        found.append("an assistant change gives text or replaces (its instructions), llm_hass_api (fewer LLM APIs), "
                     "or recommend (another option, for the household to change by hand).")
    if "llm_hass_api" in change and change.get("target"):
        now = assistants.get(change["target"])
        now = [now] if isinstance(now, str) else list(now or [])
        if added := [a for a in change["llm_hass_api"] if a not in now]:
            found.append(f"an assistant change never adds an LLM API ({', '.join(added)}); it uses "
                         f"{', '.join(now) or 'none'}.")
        elif sorted(change["llm_hass_api"]) == sorted(now):
            found.append("it uses these LLM APIs already.")
    if taken := sorted({"prompt", "llm_hass_api"} & set(change.get("recommend") or {})):
        found.append(f"recommend names options other than {' and '.join(taken)}, which a change sets.")
    return found


# Rule 2: no action, and no service field but a plain value's, from a script field, a template or trigger data; never
# conversation.process.

SCRIPT_SERVICES = ("turn_on", "turn_off", "toggle", "reload")  # A script's own service takes its variables as data.


def unsafe(kind, validated, descriptions):
    """What of a script's or automation's actions breaks rule 2 (DETAILS P7.5.5), read as Home Assistant runs them
    (validated: its validators' output, nested trigger lists flattened, templates as Template): an action, a call's
    target, a service field but one whose selector takes a plain value (VALUE_SELECTORS; never TARGET_KEYS), variables
    passed into another script or automation, or an event's data, that takes its value from a script field, a
    template or trigger data (each reaches a value only through a template); any call of conversation.process, which
    hands the choice of action to a model as it runs; and a webhook trigger that isn't local only. No script field is
    an exception, a select's neither: Home Assistant doesn't hold a call (Assist's, say) to a select's options. A
    field picks between fixed actions and targets only as branches on its value (choose, if)."""
    found = []
    for trigger in _list(validated.get("triggers")):
        found += _trigger_problems(trigger)
    for step in _list(validated.get("sequence" if kind == "script" else "actions")):
        found += _step_problems(step, descriptions)
    return list(dict.fromkeys(found))


def _list(value):
    return [v for v in (value if isinstance(value, list) else [value]) if isinstance(v, dict)]


def _templated(value):
    """Whether a value takes anything from a template: a string in it (or a Template, as validated) that's a
    template."""
    if isinstance(value, dict):
        return any(_templated(k) or _templated(v) for k, v in value.items())
    if isinstance(value, list):
        return any(_templated(v) for v in value)
    text = value.template if isinstance(value, Template) else value
    return isinstance(text, str) and is_template_string(text)


def _trigger_problems(trigger):
    if (trigger.get("trigger") or trigger.get("platform")) == "webhook" and trigger.get("local_only") is False:
        return ["a webhook trigger is local only (local_only: false would let anyone on the internet run it)."]
    return []


def _step_problems(step, descriptions):
    found = []
    for key in NESTED:
        for inner in _list(step.get(key)):
            found += _step_problems(inner, descriptions)
    for option in _list(step.get("choose")):
        for inner in _list(option.get("sequence")):
            found += _step_problems(inner, descriptions)
    for branch in _list(step.get("parallel")):
        for inner in (_list(branch["sequence"]) if "sequence" in branch and not _action(branch) else [branch]):
            found += _step_problems(inner, descriptions)
    if isinstance(step.get("repeat"), dict):
        for inner in _list(step["repeat"].get("sequence")):
            found += _step_problems(inner, descriptions)
    for trigger in _list(step.get("wait_for_trigger")):
        found += _trigger_problems(trigger)
    if "event" in step and _templated([step.get("event_data"), step.get("event_data_template")]):
        found.append(f"the {step['event']} event passes data from a script field, a template or trigger data.")
    for key in TARGET_KEYS:
        if key in step and _templated(step[key]):
            found.append(f"a step's {key} comes from a script field, a template or trigger data.")
    action = step.get("action", step.get("service"))
    if action is None and "service_template" not in step:
        return found
    if "service_template" in step or not isinstance(action, str) or _templated(action):
        return found + ["a step's action comes from a script field, a template or trigger data."]
    domain, _, service = action.partition(".")
    if any(not isinstance(step.get(k), dict) for k in ("data", "data_template") if step.get(k) is not None):
        return found + [f"{action}'s data comes from a template, which can pick what it acts on."]
    # Each of data and data_template whole, every key of both: Home Assistant gives data_template the last word.
    datas = [step.get(k) or {} for k in ("data", "data_template")]
    fields = _fields((descriptions.get(domain) or {}).get(service) or {})
    if "target" in step and _templated(step["target"]):
        found.append(f"{action}'s target comes from a script field, a template or trigger data.")
    for key, value in (item for data in datas for item in data.items()):
        if _templated(key):
            found.append(f"{action}'s data comes from a template, which can pick what it acts on.")
            continue
        selector = next(iter((fields.get(key) or {}).get("selector") or {}), None)
        if (key in TARGET_KEYS or selector not in VALUE_SELECTORS) and _templated(value):
            found.append(f"{action}'s {key} comes from a script field, a template or trigger data.")
    if action == "conversation.process":  # Whatever its text and agent: a model would choose what's done.
        found.append("conversation.process hands the choice of action to a model as it runs.")
    if any(_templated(data if domain == "script" and service not in SCRIPT_SERVICES else data.get("variables"))
           for data in datas):
        found.append(f"{action} passes variables from a script field, a template or trigger data.")
    return found


def _action(step):
    value = step.get("action", step.get("service"))
    return value if isinstance(value, str) else None


# Readers: what a change touches, as Home Assistant reads it.

def referenced(hass, changes, home):
    """{entities, devices, areas} the changes touch, as Home Assistant reads them: each script's and automation's
    actions by its Script (top_level=False, nothing runs) and its referenced_* sets, an automation's conditions and
    triggers by Home Assistant's own extractors, a scene's entities (its configuration's keys), a registry change's
    entry, and each item's own entity (a new script's or scene's as Home Assistant will make it). A configuration Home
    Assistant can't read touches only what names it."""
    out = {"entities": set(), "devices": set(), "areas": set()}
    for change in changes or []:
        kind, config = change.get("kind"), change.get("config")
        if kind == "registry":
            entry = kind_of(change)
            out[{"entity": "entities", "device": "devices", "area": "areas"}[entry]].add(change.get(f"{entry}_id"))
            if entry != "area" and change.get("area_id"):
                out["areas"].add(change["area_id"])
            continue
        if kind not in CONFIG_KINDS or not change.get("id"):
            continue
        out["entities"].add(item_entity(home, change))
        if not isinstance(config, dict) or hass is None:  # Only Home Assistant reads a configuration.
            continue
        try:
            _read(hass, kind, config, out)
        except (probatio.Invalid, ValueError, KeyError, TypeError):
            continue
    out["entities"].discard(None)
    return out


def _read(hass, kind, config, out):
    from homeassistant.helpers import condition, trigger
    from homeassistant.helpers.script import Script
    if kind == "scene":
        out["entities"] |= set(config.get("entities") or {})
        return
    if kind == "automation":
        validated = automation_schema()({k: deepcopy(v) for k, v in config.items() if k != "id"})
        for item in validated.get("triggers") or []:  # Read before a platform's own schema lists one entity.
            out["entities"] |= set(cv.entity_ids(trigger.async_extract_entities(item)))
            out["devices"] |= set(cv.ensure_list(trigger.async_extract_devices(item)))
        for item in validated.get("conditions") or []:
            out["entities"] |= condition.async_extract_entities(item)
            out["devices"] |= condition.async_extract_devices(item)
        sequence = validated["actions"]
    else:
        sequence = cv.SCRIPT_SCHEMA(deepcopy(config.get("sequence")))
    script = Script(hass, sequence, "assist_improver_change", kind, top_level=False)
    out["entities"] |= script.referenced_entities
    out["devices"] |= script.referenced_devices
    out["areas"] |= script.referenced_areas


def stops(config):
    """Every stop step's message in a script's or automation's actions, nested ones too: what it has Assist say."""
    out = []

    def walk(steps):
        for step in _list(steps):
            if isinstance(step.get("stop"), str) and step["stop"].strip():
                out.append(step["stop"])
            for key in NESTED:
                walk(step.get(key))
            for option in _list(step.get("choose")):
                walk(option.get("sequence"))
            for branch in _list(step.get("parallel")):
                walk(branch.get("sequence") if "sequence" in branch else [branch])
            if isinstance(step.get("repeat"), dict):
                walk(step["repeat"].get("sequence"))
    if isinstance(config, dict):
        walk(config.get("sequence", config.get("actions", config.get("action"))))
    return out


# What a sandbox runs.

def setup_from_changes(changes, *, source, prompt, scripts, user_id=None, assistant=None):
    """A hypothesis's changes as a sandbox runs them, on the home it's seeded as (source: snapshot.at_moment's,
    today's setup at the conversation's moment; scripts: today's script definitions by object ID), with the
    instructions of the assistant the conversation used as they are now (prompt; assistant: its target) and the person
    speaking (user_id): {exposure, aliases, prompt, scripts, prompt_changed}, with alias_removals, household,
    automations ({id: configuration}), automation_on ({entity_id: on}), scenes ({id: configuration}), registry
    ({entities, devices, areas}: {id: the fields it sets}) and llm_hass_api where a change gives them (so a setup
    without them keeps its digest). Household lines go into the household knowledge, as applying puts them; an edit of
    an assistant's instructions or LLM APIs changes this conversation's only when it used that assistant; a
    recommendation is never replayed. Only what a sandbox needs is checked here; check() checks the rest first."""
    from . import household, instructions
    out = {"exposure": {}, "aliases": {}, "scripts": {}}
    removals, automations, switched, scenes, apis = {}, {}, {}, {}, None
    registry = {"entities": {}, "devices": {}, "areas": {}}
    template, prompt_changed, knowledge = prompt or "", False, None
    for n, change in enumerate(changes, 1):
        kind, where = change.get("kind"), f"Change {n} ({change.get('kind')})"
        if kind == "assistant":
            if not change.get("target") or change["target"] != assistant:
                continue  # Another assistant's: this conversation never read it.
            if instructions.is_edit(change):
                template, problems = instructions.edit(template, [change])
                if problems:
                    raise CaseError(f"{where}: the lines it replaces aren't in the instructions this conversation's "
                                    "assistant has")
                prompt_changed = True
            if "llm_hass_api" in change:
                apis = list(change["llm_hass_api"])
        elif kind == "preference":
            base = knowledge if knowledge is not None else source.get("household") or household.EMPTY
            knowledge, template, problems = household.edit(base, [change], user_id, template)
            if problems:
                raise CaseError(f"{where}: the text it replaces isn't in the household knowledge or the "
                                "assistant's instructions")
            prompt_changed = True
        elif kind == "registry":
            entry = kind_of(change)
            target = change.get(f"{entry}_id")
            known = (source.get(f"{entry}s" if entry != "entity" else "entities") or {})
            if target not in known and not (entry == "entity" and target in (source.get("states") or {})):
                raise CaseError(f"{where}: there's no {entry} {target!r} in the household's home")
            if entry == "entity" and "expose" in change:
                out["exposure"][target] = change["expose"]
                prompt_changed = prompt_changed or not target.startswith("script.")  # Scripts aren't listed.
            if entry == "entity" and "aliases" in change:
                now = [a for a in known[target].get("aliases") or [] if a is not None]
                if added := [a for a in change["aliases"] if a not in now]:
                    out["aliases"][target] = added
                if gone := [a for a in now if a not in change["aliases"]]:
                    removals[target] = gone
                prompt_changed = True  # Assist's device list shows aliases.
            fields = {k: change[k] for k in {"entity": ("name", "area_id", "hidden_by", "disabled_by"),
                                             "device": DEVICE_FIELDS, "area": AREA_FIELDS}[entry] if k in change}
            if fields:
                registry[{"entity": "entities", "device": "devices", "area": "areas"}[entry]][target] = fields
                prompt_changed = True  # Names and areas show in Assist's device list.
        elif kind in CONFIG_KINDS:
            item_id = change.get("id")
            if not item_id:
                raise CaseError(f"{where}: names no item (id)")
            if kind == "automation" and "on" in change:
                if (entity := automation_entity(source, item_id)) is None:
                    raise CaseError(f"{where}: there's no automation with id {item_id!r}")
                switched[entity] = change["on"]
                continue
            if not isinstance(change.get("config"), dict):
                raise CaseError(f"{where}: gives no configuration")
            config = deepcopy(change["config"])
            new = existing(source if kind != "script" else {"scripts": scripts}, kind, item_id) is None
            if kind == "script":
                out["scripts"][item_id] = config
            else:
                (automations if kind == "automation" else scenes)[item_id] = {**config, "id": item_id}
            if new and kind != "automation":  # Hidden too, when it says so: new entities may be exposed by default.
                out["exposure"][new_entity(source, change)] = change.get("expose", True)
        else:
            raise CaseError(f"{where}: unknown change kind")
    setup = {**out, "prompt": template if prompt_changed else None, "prompt_changed": prompt_changed}
    if removals:  # Only when an alias goes, so plans without one keep their IDs.
        setup["alias_removals"] = removals
    if knowledge is not None:  # Only with household lines.
        setup["household"] = knowledge
    for key, value in (("automations", automations), ("automation_on", switched), ("scenes", scenes)):
        if value:
            setup[key] = value
    if any(registry.values()):
        setup["registry"] = {k: v for k, v in registry.items() if v}
    if apis is not None:
        setup["llm_hass_api"] = apis
    return setup


def changed(home, setup):
    """The home a sandbox is seeded as with a change on top (setup: setup_from_changes's): its scripts, automations and
    scenes (by id: in place of the home's, or new), automations turned on or off (as Home Assistant restores them),
    exposure, and its registries as Home Assistant's updates leave them (an entity enabled with no state yet shows as
    unknown until its integration reports, which a stand-in never does on its own). Aliases are the registry's too."""
    if not setup:
        return home
    out = deepcopy(home)
    out["scripts"] = {**(out.get("scripts") or {}), **deepcopy(setup.get("scripts") or {})}
    for key in ("automations", "scenes"):
        if new := setup.get(key):
            kept = [i for i in out.get(key) or [] if i.get("id") is None or str(i["id"]) not in new]
            out[key] = kept + deepcopy(list(new.values()))
    states = out.setdefault("states", {})
    for entity_id, on in (setup.get("automation_on") or {}).items():
        if entity_id in states:
            states[entity_id] = {**states[entity_id], "state": "on" if on else "off"}
    out["exposure"] = {**(out.get("exposure") or {}), **(setup.get("exposure") or {})}
    entities = out.setdefault("entities", {})
    for entity_id in {*(setup.get("aliases") or {}), *(setup.get("alias_removals") or {})}:
        if entity_id in entities:
            gone = set((setup.get("alias_removals") or {}).get(entity_id) or [])
            entities[entity_id]["aliases"] = [a for a in entities[entity_id].get("aliases") or [] if a not in gone] \
                + [a for a in (setup.get("aliases") or {}).get(entity_id) or []]
    registry = setup.get("registry") or {}
    for entity_id, fields in (registry.get("entities") or {}).items():
        if entity_id in entities:
            entities[entity_id] = {**entities[entity_id], **fields}
            if "disabled_by" in fields and entity_id not in states:
                states[entity_id] = {"entity_id": entity_id, "state": "unknown", "attributes": {}}
    for key in ("devices", "areas"):
        for item_id, fields in (registry.get(key) or {}).items():
            if item_id in (out.get(key) or {}):
                out[key][item_id] = {**out[key][item_id], **fields}
    return out
