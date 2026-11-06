"""The home as Home Assistant has it, read whole: its own objects, never a hand-made selection of them (DETAILS P8.2.5).

A recording keeps the moment once, in its first turn (states, registries, hass.config, exposure, the definitions of
scripts, automations and scenes, to-do items and the household knowledge); a later turn keeps what moves: the states,
and the time. A snapshot is taken in two steps, so a recorded turn holds the event loop only for a quick read:
taken(hass) on the event loop (the State objects and registry entries by reference: Home Assistant replaces them,
never changes them; exposure, which can write the entity registry; the scenes and to-do items read ahead into the
cache, refresh()), then home(taken) in any thread, which serializes it. snapshot(hass) does both at once.

Cut, with the reason: a scene made with scene.create has no definition (its state's entity_id list is what there is);
private caches of Home Assistant's registry classes (fields starting with an underscore) aren't data."""

from copy import deepcopy
from dataclasses import fields, is_dataclass

import attr
from homeassistant.components.homeassistant import async_should_expose
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import (area_registry as ar, device_registry as dr, entity_registry as er,
                                   floor_registry as fr, label_registry as lr)
from homeassistant.util.dt import utcnow
from homeassistant.util.hass_dict import HassKey
from homeassistant.util.json import json_loads

from . import touchpoints
from .const import DOMAIN
from .household import KEY as HOUSEHOLD
from .serialization import plain

AHEAD: HassKey[dict] = HassKey(f"{DOMAIN}_read_ahead")  # The reads that await, cached: {"scenes": [...], "todo": {}}


def state_record(state):
    """A State whole, as Home Assistant writes it (State.as_dict); None for none."""
    if state is None:
        return None
    try:
        return json_loads(state.as_dict_json)  # Home Assistant's own, cached on the State.
    except TypeError:  # An attribute its encoder can't write: piece by piece (plain).
        return plain(state.as_dict())


def snapshot(hass):
    return home(taken(hass))


def taken(hass, moment=True):
    """What a snapshot reads of Home Assistant, on the event loop: home() serializes it, in any thread. moment: the
    whole moment (a recording's first turn, today's setup); else what moves, for a later turn: the states, and the
    time."""
    read = {"at": utcnow().isoformat(), "states": hass.states.async_all()}
    if not moment:
        return read
    entries, devices = list(er.async_get(hass).entities.values()), dr.async_get(hass)
    exposure = {}
    for entity_id in {s.entity_id for s in read["states"]} | {e.entity_id for e in entries}:
        try:
            exposure[entity_id] = async_should_expose(hass, "conversation", entity_id)
        except KeyError:
            exposure[entity_id] = None
    scripts, automations = _definitions(hass)
    household, ahead = hass.data.get(HOUSEHOLD), hass.data.get(AHEAD) or {}
    return {**read, "time_zone": hass.config.time_zone, "home_name": hass.config.location_name,
            "config": hass.config.as_dict(), "entries": entries,
            "devices": [*devices.devices, *devices.child_devices],  # A child device names its parent.
            "areas": list(ar.async_get(hass).areas.values()), "floors": list(fr.async_get(hass).floors.values()),
            "labels": list(lr.async_get(hass).labels.values()), "exposure": exposure, "scripts": scripts,
            "automations": automations, "scenes": ahead.get("scenes"), "todo": ahead.get("todo"),
            # The household knowledge then, and whether Assist read it, so a replay rebuilds the same prompt.
            "household": plain(getattr(household, "knowledge", None)),
            "household_on": bool(getattr(household, "on", False))}


def home(taken):
    """The snapshot as a recording keeps it: what taken() read, serialized (a copy; nothing of Home Assistant's)."""
    out = {"at": taken["at"], "states": {s.entity_id: state_record(s) for s in taken["states"]}}
    if "entries" not in taken:  # A later turn's.
        return out
    return {**out, "time_zone": taken["time_zone"], "home_name": taken["home_name"], "config": plain(taken["config"]),
            "entities": {e.entity_id: entity_record(e) for e in taken["entries"]},
            "devices": {d.id: plain(whole(d)) for d in taken["devices"]},
            "areas": {a.id: plain(whole(a)) for a in taken["areas"]},
            "floors": {f.floor_id: plain(whole(f)) for f in taken["floors"]},
            "labels": {label.label_id: plain(whole(label)) for label in taken["labels"]},
            "exposure": taken["exposure"], "scripts": plain(taken["scripts"]),  # plain() copies.
            "automations": plain(taken["automations"]), "scenes": deepcopy(taken["scenes"]),
            "todo": deepcopy(taken["todo"]), "household": taken["household"], "household_on": taken["household_on"]}


def whole(entry):
    """A registry entry with every field its class has (Home Assistant's registries are attrs classes and
    dataclasses with no as_dict of their own; as_partial_dict leaves fields out), but its private caches."""
    names = [f.name for f in (attr.fields(type(entry)) if attr.has(type(entry)) else fields(entry))]
    return {name: getattr(entry, name) for name in names if not name.startswith("_")}


def entity_record(entry):
    """An entity registry entry whole (whole()), its aliases as Home Assistant stores them: the entity's own computed
    name as None."""
    return plain({**whole(entry), "aliases": [None if a is er.COMPUTED_NAME else a for a in entry.aliases]})


async def refresh(hass, what=("scenes", "todo")):
    """Read ahead what can only be awaited, for taken() to find in the cache: the scenes' definitions, and each to-do
    list's items. A read that fails keeps the last one."""
    ahead = hass.data.setdefault(AHEAD, {})
    if "scenes" in what and "scene" in hass.config.components:  # Set up: its scenes have definitions.
        with touchpoints.guarded(hass, "scene_configs"):
            try:  # The load Home Assistant's own scene reload runs: scenes.yaml, configuration.yaml and packages.
                conf = await touchpoints.get(hass, "scene_configs")(hass, "scene")
            except (HomeAssistantError, OSError):  # A half-edited configuration.yaml: the last definitions stay.
                conf = None
            if conf is not None:
                ahead["scenes"] = scenes_of(conf)
    if "todo" in what and "todo" in hass.config.components:
        lists = hass.states.async_entity_ids("todo")
        try:
            found = await hass.services.async_call("todo", "get_items", {"entity_id": lists}, blocking=True,
                                                   return_response=True) if lists else {}
        except HomeAssistantError:  # A list that can't answer: the last items stay.
            return
        ahead["todo"] = plain(found)


def scenes_of(conf):
    """The scenes of Home Assistant's own scene platform in a processed scene configuration, each as its editor writes
    it: each entity's state and attributes."""
    out = []
    for platform in conf.get("scene") or []:
        if platform.get("platform") != "homeassistant":
            continue
        for scene in platform.get("states") or []:
            entities = {e: {"state": s.state, **s.attributes} if hasattr(s, "state") else s
                        for e, s in (scene.get("entities") or {}).items()}
            out.append(plain({**scene, "entities": entities}))
    return out


def subentry_of(hass, agent_id):
    """The config subentry a conversation agent belongs to (several assistants can share one provider entry)."""
    return getattr(er.async_get(hass).async_get(agent_id), "config_subentry_id", None) if agent_id else None


def provider_config(hass, platform, subentry=None):
    """The assistants of a provider as a turn keeps them: each conversation subentry's data whole (its settings;
    credentials are the provider entry's, never read here); only the answering one's, once known (subentry)."""
    output = []
    for entry in hass.config_entries.async_entries(platform):
        for sub in entry.subentries.values():
            if sub.subentry_type == "conversation" and subentry in (None, sub.subentry_id):
                output.append({"config_entry_id": entry.entry_id, "subentry_id": sub.subentry_id,
                               "title": sub.title, "options": plain(dict(sub.data))})
    return output


def answering(configs, subentry):
    """Of a turn's provider configurations (provider_config gives every conversation subentry of the provider), the
    one of the assistant that answered, by its subentry; all of them while that isn't known."""
    mine = [c for c in configs if subentry and c.get("subentry_id") == subentry]
    return mine if len(mine) == 1 else configs


def _definitions(hass):
    """The script and automation configurations, each the one Home Assistant holds (a reload replaces it), as far as
    they can be read (touchpoints.guarded)."""
    scripts, automations = {}, []
    with touchpoints.guarded(hass, "script_configs"):
        for entity in _entities(hass, "script").values():
            if entity.raw_config:
                scripts[entity.entity_id.split(".", 1)[1]] = dict(entity.raw_config)
    with touchpoints.guarded(hass, "automation_configs"):
        for entity in _entities(hass, "automation").values():
            if entity.raw_config:
                automations.append(dict(entity.raw_config))
    return scripts, automations


def _entities(hass, domain):
    """{entity_id: entity} of an integration's own entity platforms (none until it's set up): a script's or
    automation's holds its configuration (raw_config), as its integration's config websocket gives it."""
    platforms = touchpoints.get(hass, "entity_platforms")(hass, domain)
    return {entity_id: entity for platform in platforms for entity_id, entity in platform.entities.items()}


def renamed(recording, home):
    """{entity ID in a recording: its entity ID in today's home (a snapshot)}, for each entity whose ID was changed
    since (DETAILS P6.2.4): the same registry entry (its id), where both sides record one; for a recording from before
    entries were recorded whole, an entity gone since and one new since are the same when they're the only two sharing
    a domain, an integration and its entry, a device and the name the integration gives it, none of which a new entity
    ID changes. One with neither a device nor such a name has nothing to be known by."""
    then = {e: x for turn in recording.get("turns") or []
            for e, x in ((turn.get("source") or {}).get("entities") or {}).items()}
    now = home.get("entities") or {}
    by_id = {x["id"]: e for e, x in now.items() if x.get("id")}
    found = {e: by_id[x["id"]] for e, x in then.items() if x.get("id") in by_id and by_id[x["id"]] != e}
    then = {e: x for e, x in then.items() if not x.get("id")}

    def unmatched(entities, others):
        out = {}
        for entity_id, x in entities.items():
            if entity_id not in others and (x.get("device_id") or x.get("original_name")):
                out.setdefault((entity_id.split(".", 1)[0], x.get("platform"), x.get("config_entry_id"),
                                x.get("device_id"), x.get("original_name")), []).append(entity_id)
        return out
    gone, new = unmatched(then, now), unmatched({e: x for e, x in now.items() if e not in found.values()}, then)
    return {**found, **{ids[0]: new[key][0] for key, ids in gone.items()
                        if len(ids) == 1 and len(new.get(key) or ()) == 1}}


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
    recorded time, and each device as it was then, where it's still there: its state and attributes then, but for
    what's setup, today's: its name, and what it can do (the attributes its registry entry lists as capabilities,
    and its supported features). One that's new since starts as it is now, and one that's gone since is absent.
    Everything else (areas, devices, names, aliases, what Assist can see, scripts, automations, the household
    knowledge) is today's."""
    then, states, entities = source.get("states") or {}, {}, home.get("entities") or {}
    for entity_id, now in (home.get("states") or {}).items():
        was = then.get(entity_id)
        if was is None:
            states[entity_id] = now
            continue
        today = now.get("attributes") or {}
        setup = {"friendly_name", "supported_features", *((entities.get(entity_id) or {}).get("capabilities") or {})}
        attributes = {k: v for k, v in (was.get("attributes") or {}).items() if k not in setup}
        states[entity_id] = {**was, "attributes": {**attributes, **{k: today[k] for k in setup if k in today}}}
    return {**home, "at": source.get("at"), "states": states}


def assist_config(source, setup=None):
    """What Assist was set up with in a replay: the scripts ({object id: config}) the sandbox was seeded with, with a
    change's on top, and the entities exposed to it (sorted)."""
    setup = setup or {}
    exposure = {**(source.get("exposure") or {}), **(setup.get("exposure") or {})}
    return {"scripts": {**(source.get("scripts") or {}), **(setup.get("scripts") or {})},
            "exposed": sorted(e for e, exposed in exposure.items() if exposed)}
