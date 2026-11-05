"""Read local state/registries without polling integrations or calling services.

A snapshot of the home is taken in two steps, so a recorded turn holds the event loop only for what has to be read
there: taken(hass) on the event loop, at the moment of the turn (the State objects, registry entries and script and
automation configurations as they are then, by reference: Home Assistant replaces them, never changes them; exposure,
which can write the entity registry; and what's small), then home(taken) in any thread, which serializes it.
snapshot(hass) does both at once."""

from copy import deepcopy

from homeassistant.components.automation import DATA_COMPONENT as AUTOMATIONS
from homeassistant.components.homeassistant.exposed_entities import async_should_expose
from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er
from homeassistant.helpers.entity_component import EntityComponent
from homeassistant.util.dt import utcnow
from homeassistant.util.hass_dict import HassKey

from .const import ATTRIBUTES, PROVIDER_OPTIONS
from .household import KEY as HOUSEHOLD
from .serialization import plain, SECRET

# No public accessor gives script or automation configurations: they're read from the entity components the two
# integrations keep in hass.data (automation's typed key; script keeps its component under its domain, untyped).
SCRIPTS: HassKey[EntityComponent] = HassKey("script")
# What a recording's states keep of their attributes, as its snapshot says (allowlist-v1, in older recordings: the
# replay engine's attributes alone).
ATTRIBUTE_POLICY = ("allowlist-v2: the replay engine's attributes and what Assist's live context shows (Home "
                    "Assistant 2026.10's list); omitted attributes are not recorded")


def state_record(state):
    if state is None:
        return None
    if SECRET.search(state.entity_id):
        return {"entity_id": state.entity_id, "state": "[redacted]", "attributes": {}}
    return {"entity_id": state.entity_id, "state": state.state,
            "attributes": plain({k: v for k, v in state.attributes.items() if k in ATTRIBUTES}),
            "last_changed": plain(state.last_changed), "last_updated": plain(state.last_updated),
            "context": plain(state.context.as_dict())}


def snapshot(hass):
    return home(taken(hass))


def taken(hass):
    """What a snapshot reads of Home Assistant, on the event loop: home() serializes it, in any thread."""
    states, entries = hass.states.async_all(), list(er.async_get(hass).entities.values())
    exposure = {}
    for entity_id in {s.entity_id for s in states} | {e.entity_id for e in entries}:
        try:
            exposure[entity_id] = async_should_expose(hass, "conversation", entity_id)
        except KeyError:
            exposure[entity_id] = None
    scripts, automations = _definitions(hass)
    household = hass.data.get(HOUSEHOLD)
    return {"at": utcnow().isoformat(), "time_zone": hass.config.time_zone, "home_name": hass.config.location_name,
            "states": states, "entries": entries,
            "aliases": {e.entity_id: er.async_get_entity_aliases(hass, e) for e in entries},
            "devices": list(dr.async_get(hass).devices),
            "areas": {a.id: {"name": a.name, "aliases": sorted(a.aliases)} for a in ar.async_get(hass).areas.values()},
            "exposure": exposure, "scripts": scripts, "automations": automations,
            "template_actions": template_actions(hass),
            # The household knowledge then, and whether Assist read it, so a replay rebuilds the same prompt.
            "household": plain(getattr(household, "knowledge", None)),
            "household_on": bool(getattr(household, "on", False))}


def home(taken):
    """The snapshot as a recording keeps it: what taken() read, serialized (a copy; nothing of Home Assistant's)."""
    states = {s.entity_id: state_record(s) for s in taken["states"]}
    entities = {}
    for e in taken["entries"]:
        entities[e.entity_id] = plain({
            "name": e.name, "original_name": e.original_name,
            "aliases": taken["aliases"][e.entity_id],
            # HA 2026.10 uses an ordered list with a computed-name sentinel.
            # Preserve the declaration separately from its resolved names.
            "alias_entries": [None if a is er.COMPUTED_NAME else a for a in e.aliases],
            "area_id": e.area_id, "device_id": e.device_id,
            "platform": e.platform, "config_entry_id": e.config_entry_id,
            "config_subentry_id": e.config_subentry_id,
            "disabled_by": e.disabled_by, "hidden_by": e.hidden_by,
            "has_state": e.entity_id in states,
        })
        if sensor_options := _sensor_options(e):  # Only where set.
            entities[e.entity_id]["sensor_options"] = sensor_options
    # Do not copy network connections, serial numbers or complete config entries.
    devices = {d.id: plain({"name": d.name, "name_by_user": d.name_by_user, "area_id": d.area_id,
                            "manufacturer": d.manufacturer, "model": d.model})
               for d in taken["devices"]}
    return {"at": taken["at"], "time_zone": taken["time_zone"], "home_name": taken["home_name"], "states": states,
            "entities": entities, "devices": devices, "areas": taken["areas"], "exposure": taken["exposure"],
            "scripts": plain(taken["scripts"]), "automations": plain(taken["automations"]),  # plain() copies.
            "template_actions": taken["template_actions"], "household": taken["household"],
            "household_on": taken["household_on"], "attribute_policy": ATTRIBUTE_POLICY}


def _sensor_options(entry):
    """A sensor's display precision, as its registry entry holds it: Home Assistant's live context rounds the sensor's
    state to it. Nothing else of the entry's options."""
    options = entry.options.get("sensor") or {}
    return {k: options[k] for k in ("display_precision", "suggested_display_precision") if options.get(k) is not None}


def provider_config(hass, platform):
    """Capture relevant conversation subentries only, never API credentials."""
    output = []
    for entry in hass.config_entries.async_entries(platform):
        for sub in entry.subentries.values():
            if sub.subentry_type == "conversation":
                output.append({"config_entry_id": entry.entry_id, "subentry_id": sub.subentry_id,
                               "title": sub.title, "options": plain({k: v for k, v in sub.data.items()
                                                                      if k in PROVIDER_OPTIONS})})
    return output


def answering(configs, subentry):
    """Of a turn's provider configurations (provider_config gives every conversation subentry of the provider), the
    one of the assistant that answered, by its subentry; all of them while that isn't known."""
    mine = [c for c in configs if subentry and c.get("subentry_id") == subentry]
    return mine if len(mine) == 1 else configs


def subentry_of(hass, agent_id):
    """The config subentry a conversation agent belongs to (several assistants can share one provider entry)."""
    from homeassistant.helpers import entity_registry as er
    return getattr(er.async_get(hass).async_get(agent_id), "config_subentry_id", None) if agent_id else None


def live_definitions(hass):
    """The script and automation configurations, as the household has them now (a copy)."""
    return deepcopy(_definitions(hass))


def _definitions(hass):
    """The script and automation configurations, each the one Home Assistant holds (a reload replaces it)."""
    scripts, automations = {}, []
    if (component := hass.data.get(SCRIPTS)) is not None:
        for entity in getattr(component, "entities", ()):
            if getattr(entity, "raw_config", None):
                scripts[entity.entity_id.split(".", 1)[1]] = dict(entity.raw_config)
    if (component := hass.data.get(AUTOMATIONS)) is not None:
        for entity in getattr(component, "entities", ()):
            if getattr(entity, "raw_config", None):
                automations.append(dict(entity.raw_config))
    return scripts, automations


def template_actions(hass):
    """{template entity: the entities its own actions act on}, as Home Assistant's script engine finds them: what HA
    itself switches, under the request, when the entity is commanded (a template light's helper). An entity whose
    actions can't be read this way (Home Assistant changed them) acts on none known: a recording never fails for it."""
    from homeassistant.helpers.entity_platform import async_get_platforms
    found = {}
    for platform in async_get_platforms(hass, "template"):
        for entity_id, entity in platform.entities.items():
            try:  # No public API gives a template entity's actions, YAML ones included; its scripts hold them.
                acts_on = {e for script in getattr(entity, "_action_scripts", {}).values()
                           for e in script.referenced_entities} - {entity_id}
            except Exception:  # noqa: BLE001 - see the docstring.
                continue
            if acts_on:
                found[entity_id] = sorted(acts_on)
    return found


def live_script_definitions(hass, entities):
    """Current UI/YAML script configs; later observations, never historical snapshots."""
    from .cases import CaseError
    component = hass.data.get(SCRIPTS)
    definitions = {}
    for entity in entities:
        script = component.get_entity(entity) if component else None
        if script is None or not getattr(script, "raw_config", None):
            raise CaseError(f"Script {entity} has no readable configuration")
        definitions[entity] = deepcopy(dict(script.raw_config))
    return definitions


def assist_config(source, setup=None):
    """What Assist was set up with in a replay: the recorded scripts ({object id: config}) with a change's on top, and
    the entities exposed to it (sorted), as the sandbox is seeded with them."""
    setup = setup or {}
    exposure = {**(source.get("exposure") or {}), **(setup.get("exposure") or {})}
    return {"scripts": {**(source.get("scripts") or {}), **(setup.get("scripts") or {})},
            "exposed": sorted(e for e, exposed in exposure.items() if exposed)}
