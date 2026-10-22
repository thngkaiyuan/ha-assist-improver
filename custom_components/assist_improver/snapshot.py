"""Read local state/registries without polling integrations or calling services."""

from homeassistant.components.homeassistant.exposed_entities import async_should_expose
from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er
from homeassistant.util.dt import utcnow

from .const import ATTRIBUTES, PROVIDER_OPTIONS
from .serialization import plain, SECRET


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
    states = {s.entity_id: state_record(s) for s in hass.states.async_all()}
    registry = er.async_get(hass)
    entities = {}
    for e in registry.entities.values():
        entities[e.entity_id] = plain({
            "name": e.name, "original_name": e.original_name,
            "aliases": er.async_get_entity_aliases(hass, e),
            # HA 2026.10 uses an ordered list with a computed-name sentinel.
            # Preserve the declaration separately from its resolved names.
            "alias_entries": [None if a is er.COMPUTED_NAME else a for a in e.aliases],
            "area_id": e.area_id, "device_id": e.device_id,
            "platform": e.platform, "config_entry_id": e.config_entry_id,
            "config_subentry_id": e.config_subentry_id,
            "disabled_by": e.disabled_by, "hidden_by": e.hidden_by,
            "has_state": e.entity_id in states,
        })
    exposure = {}
    for entity_id in states.keys() | entities.keys():
        try:
            exposure[entity_id] = async_should_expose(hass, "conversation", entity_id)
        except KeyError:
            exposure[entity_id] = None
    # Do not copy network connections, serial numbers or complete config entries.
    devices = {d.id: plain({"name": d.name, "name_by_user": d.name_by_user, "area_id": d.area_id,
                            "manufacturer": d.manufacturer, "model": d.model})
               for d in dr.async_get(hass).devices}
    areas = {a.id: {"name": a.name, "aliases": sorted(a.aliases)}
             for a in ar.async_get(hass).areas.values()}
    return {"at": utcnow().isoformat(), "time_zone": hass.config.time_zone,
            "home_name": hass.config.location_name, "states": states,
            "entities": entities, "devices": devices, "areas": areas, "exposure": exposure,
            "attribute_policy": "allowlist-v1; omitted attributes are not recorded"}


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
