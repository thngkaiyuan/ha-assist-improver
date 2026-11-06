"""Read local state/registries without polling integrations or calling services.

A snapshot of the home is taken in two steps, so a recorded turn holds the event loop only for what has to be read
there: taken(hass) on the event loop, at the moment of the turn (the State objects, registry entries and script and
automation configurations as they are then, by reference: Home Assistant replaces them, never changes them; exposure,
which can write the entity registry; and what's small), then home(taken) in any thread, which serializes it.
snapshot(hass) does both at once."""

from copy import deepcopy

from homeassistant.components.homeassistant import async_should_expose
from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er
from homeassistant.helpers.entity_component import EntityComponent
from homeassistant.util.dt import utcnow
from homeassistant.util.hass_dict import HassKey

from . import touchpoints
from .const import ATTRIBUTES, PROVIDER_OPTIONS
from .household import KEY as HOUSEHOLD
from .serialization import plain, SECRET

# No public accessor gives script or automation configurations: they're read from the entity components the two
# integrations keep in hass.data under their domains.
SCRIPTS: HassKey[EntityComponent] = HassKey("script")
AUTOMATIONS: HassKey[EntityComponent] = HassKey("automation")
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


def _definitions(hass):
    """The script and automation configurations, each the one Home Assistant holds (a reload replaces it), as far as
    they can be read (touchpoints.guarded)."""
    scripts, automations = {}, []
    with touchpoints.guarded(hass, "script_configs"):
        for entity in _entities(hass, SCRIPTS):
            if entity.raw_config:
                scripts[entity.entity_id.split(".", 1)[1]] = dict(entity.raw_config)
    with touchpoints.guarded(hass, "automation_configs"):
        for entity in _entities(hass, AUTOMATIONS):
            if entity.raw_config:
                automations.append(dict(entity.raw_config))
    return scripts, automations


def _entities(hass, key):
    """The entities of the integration keeping its component under this key, once it's set up (a KeyError: it keeps
    it elsewhere now)."""
    return hass.data[key].entities if key in hass.config.components else ()


def template_actions(hass):
    """{template entity: the entities its own actions act on}, as Home Assistant's script engine finds them: what HA
    itself switches, under the request, when the entity is commanded (a template light's helper). An entity whose
    actions can't be read this way (Home Assistant changed them) acts on none known, which is reported: a recording
    never fails for it."""
    from homeassistant.helpers.entity_platform import async_get_platforms
    found, failed = {}, []
    with touchpoints.guarded(hass, "template_actions"):
        for platform in async_get_platforms(hass, "template"):
            for entity_id, entity in platform.entities.items():
                try:  # No public API gives a template entity's actions, YAML ones included; its scripts hold them.
                    acts_on = {e for script in entity._action_scripts.values()  # noqa: SLF001
                               for e in script.referenced_entities} - {entity_id}
                except Exception as err:  # noqa: BLE001 - see the docstring.
                    failed.append(err)
                    continue
                if acts_on:
                    found[entity_id] = sorted(acts_on)
        if failed:
            raise failed[0]
    return found


def live_script_definitions(hass, entities):
    """Current UI/YAML script configs; later observations, never historical snapshots."""
    from .cases import CaseError
    definitions = {}
    for entity in entities:
        config = None
        with touchpoints.guarded(hass, "script_configs"):
            script = hass.data[SCRIPTS].get_entity(entity) if SCRIPTS in hass.config.components else None
            config = script.raw_config if script is not None else None
        if not config:
            raise CaseError(f"Script {entity} has no readable configuration")
        definitions[entity] = deepcopy(dict(config))
    return definitions


def renamed(recording, home):
    """{entity ID in a recording: its entity ID in today's home (a snapshot)}, for each entity whose ID was changed
    since (DETAILS P6.2.4): an entity gone since and one new since are the same when they're the only two sharing a
    domain, an integration and its entry, a device and the name the integration gives it, none of which a new entity ID
    changes. One with neither a device nor such a name has nothing to be known by."""
    then = {e: x for turn in recording.get("turns") or []
            for e, x in ((turn.get("source") or {}).get("entities") or {}).items()}
    now = home.get("entities") or {}

    def unmatched(entities, others):
        found = {}
        for entity_id, x in entities.items():
            if entity_id not in others and (x.get("device_id") or x.get("original_name")):
                found.setdefault((entity_id.split(".", 1)[0], x.get("platform"), x.get("config_entry_id"),
                                  x.get("device_id"), x.get("original_name")), []).append(entity_id)
        return found
    gone, new = unmatched(then, now), unmatched(now, then)
    return {ids[0]: new[key][0] for key, ids in gone.items() if len(ids) == 1 and len(new.get(key) or ()) == 1}


def renaming(value, renames):
    """A recording, or what's read from one (its goals), with each entity ID renames has as today's (renamed's),
    wherever it's whole: a key or a string."""
    if isinstance(value, dict):
        return {renames.get(k, k): renaming(v, renames) for k, v in value.items()}
    if isinstance(value, list):
        return [renaming(v, renames) for v in value]
    return renames.get(value, value) if isinstance(value, str) else value


def on_today(recording, home):
    """A recording with each entity whose ID was changed since under its ID in today's home (renamed)."""
    renames = renamed(recording, home)
    return renaming(recording, renames) if renames else recording


def at_moment(home, source):
    """Today's home (a snapshot) at a recorded moment (a recording's snapshot then, on_today's, DETAILS P6.2.4): the
    recorded time, and each device as it was then, where it's still there; one that's new since starts as it is now,
    and one that's gone since is absent. Everything else (areas, devices, names, aliases, what Assist can see, scripts,
    automations, the household knowledge) is today's."""
    then, states = source.get("states") or {}, {}
    for entity_id, now in (home.get("states") or {}).items():
        was = then.get(entity_id)
        if was is None:
            states[entity_id] = now
            continue
        attributes = {k: v for k, v in (was.get("attributes") or {}).items() if k != "friendly_name"}
        if "friendly_name" in (now.get("attributes") or {}):  # A name is setup: today's.
            attributes["friendly_name"] = now["attributes"]["friendly_name"]
        states[entity_id] = {**was, "attributes": attributes}
    return {**home, "at": source.get("at"), "states": states}


def assist_config(source, setup=None):
    """What Assist was set up with in a replay: the scripts ({object id: config}) the sandbox was seeded with, with a
    change's on top, and the entities exposed to it (sorted)."""
    setup = setup or {}
    exposure = {**(source.get("exposure") or {}), **(setup.get("exposure") or {})}
    return {"scripts": {**(source.get("scripts") or {}), **(setup.get("scripts") or {})},
            "exposed": sorted(e for e, exposed in exposure.items() if exposed)}
