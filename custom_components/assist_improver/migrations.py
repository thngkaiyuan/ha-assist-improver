"""One-time upgrades of what earlier versions left behind, so the rest of the code reads one shape (the config
entry's minor version 2):

- the option tests_per_cycle (its name before 0.17.4) becomes tests_per_goal;
- the "Practice (simulated home)" assistant 0.6.0 added, and its Assist pipeline, are removed (practice mode replaced
  it in 0.6.2);
- Repairs items from 0.18 to 0.20 (improvement_*) and up to 0.33 (household_*) are removed;
- recordings that kept aliases the 0.1.0 way (Home Assistant's raw list, the entity's own name as 0) are rewritten
  the current way (the names, and alias_entries with None for its own name).
"""

import logging

from homeassistant.helpers import entity_registry as er, issue_registry as ir
from homeassistant.helpers.start import async_at_started

from .const import CONF_TESTS_PER_GOAL, DOMAIN

_LOGGER = logging.getLogger(__name__)
MINOR_VERSION = 2
OLD_ISSUES = ("improvement_", "household_")


async def async_migrate(hass, entry):
    """Bring an entry and its stores up to MINOR_VERSION; True when done."""
    if entry.version != 1:
        return False
    if entry.minor_version < 2:
        options = dict(entry.options)
        if "tests_per_cycle" in options:
            options.setdefault(CONF_TESTS_PER_GOAL, options["tests_per_cycle"])
            del options["tests_per_cycle"]
        for domain, issue_id in list(ir.async_get(hass).issues):
            if domain == DOMAIN and issue_id.startswith(OLD_ISSUES):
                ir.async_delete_issue(hass, DOMAIN, issue_id)
        rewritten = await _recordings(hass)
        entry_id = entry.entry_id
        async_at_started(hass, lambda _hass: hass.async_create_task(
            _remove_practice_assistant(hass, entry_id), "assist_improver_practice_cleanup"))
        hass.config_entries.async_update_entry(entry, options=options, minor_version=2)
        _LOGGER.info("Assist Improver: migrated to 1.2 (%s recordings' aliases rewritten)", rewritten)
    return True


def current_aliases(entity, shown=None):
    """An entity entry's aliases the current way, from a 0.1.0 one (an entry with alias_entries is current). shown:
    the name its state showed then (friendly_name), which is the name Home Assistant computed for it."""
    if "alias_entries" in entity:
        return entity
    raw = entity.get("aliases") or [0]  # None at all: Assist knows it by its own name.
    own = shown or entity.get("name") or entity.get("original_name") or ""
    return {**entity, "alias_entries": [a if isinstance(a, str) else None for a in raw],
            "aliases": [a if isinstance(a, str) else own for a in raw if isinstance(a, str) or own]}


def upgraded(recording):
    """A recording with every turn's entity entries the current way; None if nothing needed changing."""
    from .retention import pack, unpack
    packed = bool(recording.get("packed"))
    recording = unpack(recording)
    changed = False
    for turn in recording.get("turns", []):
        entities = (turn.get("source") or {}).get("entities")
        if not isinstance(entities, dict):
            continue
        states = (turn.get("source") or {}).get("states") or {}
        for entity_id, entity in entities.items():
            if isinstance(entity, dict) and "alias_entries" not in entity:
                shown = ((states.get(entity_id) or {}).get("attributes") or {}).get("friendly_name")
                entities[entity_id] = current_aliases(entity, shown)
                changed = True
    if not changed:
        return None
    return pack(recording) if packed else recording


async def _recordings(hass):
    from homeassistant.helpers.storage import Store
    from .const import STORAGE_VERSION
    index = ((await Store(hass, STORAGE_VERSION, f"{DOMAIN}.index", private=True).async_load()) or {}).get(
        "sessions", {})
    rewritten = 0
    for key in index:
        store = Store(hass, STORAGE_VERSION, f"{DOMAIN}.session_{key}", private=True, atomic_writes=True)
        recording = await store.async_load()
        if recording and (new := upgraded(recording)) is not None:
            await store.async_save(new)
            rewritten += 1
    return rewritten


async def _remove_practice_assistant(hass, entry_id):
    """0.6.0's practice assistant and the Assist pipelines that used it."""
    agent = er.async_get(hass).async_get_entity_id("conversation", DOMAIN, f"{entry_id}-practice")
    if agent is None:
        return
    from homeassistant.components.assist_pipeline.pipeline import KEY_ASSIST_PIPELINE
    if (pipelines := hass.data.get(KEY_ASSIST_PIPELINE)) is not None:
        for pipeline in list(pipelines.pipeline_store.async_items()):
            if pipeline.conversation_engine == agent:
                await pipelines.pipeline_store.async_delete_item(pipeline.id)
    er.async_get(hass).async_remove(agent)
