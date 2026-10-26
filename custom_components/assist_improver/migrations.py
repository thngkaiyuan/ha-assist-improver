"""One-time upgrades of what earlier versions left behind, so the rest of the code reads one shape (the config
entry's minor version 3):

- the option tests_per_cycle (its name before 0.17.4) becomes tests_per_goal;
- the "Practice (simulated home)" assistant 0.6.0 added, and its Assist pipeline, are removed (practice mode replaced
  it in 0.6.2);
- Repairs items from 0.18 to 0.20 (improvement_*) and up to 0.33 (household_*) are removed;
- recordings that kept aliases the 0.1.0 way (Home Assistant's raw list, the entity's own name as 0) are rewritten
  the current way (the names, and alias_entries with None for its own name).

Minor version 3 (0.35.0): where each taken-in conversation waits moves from a record in the run log
(id pipeline-state) to the pipeline's own store.

Minor version 4 (0.35.3): a recording kept every conversation configuration of its assistant's provider; with two
assistants on one provider (say Opus and Haiku under one Anthropic entry), a replay couldn't tell which one answered.
Each recording keeps the one of the assistant that answered it.

Minor version 5 (household knowledge in Assist): Assist itself gives the household's lines now, so every assistant on
the household API ("Assist with household knowledge") uses Assist instead, each API once (an assistant that ticked both
read every tool twice). The Household knowledge switch starts on.

Minor version 6 (no fix found): a goal that earlier proposals listed in not_fixed with a reason, or whose tests
already reached the tests-per-goal limit with no improvement for it, is recorded as no fix found, once
(Improvements.unfixed): no later run spends anything on it unless its conversation is sent in again. A goal of an
improvement waiting for a revision, or one whose accept found Home Assistant changed (conflict), gets its budget
afresh from that decision, as it would now.
"""

import logging

from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er, issue_registry as ir
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.storage import Store

from .const import CONF_HOUSEHOLD, CONF_TESTS_PER_GOAL, DEFAULT_TESTS_PER_GOAL, DOMAIN, STORAGE_VERSION

_LOGGER = logging.getLogger(__name__)
MINOR_VERSION = 6
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
        rewritten = await _recordings(hass, upgraded)
        entry_id = entry.entry_id

        @callback  # On the event loop: HA runs a plain function given to async_at_started in a thread.
        def cleanup(_hass):
            hass.async_create_task(_remove_practice_assistant(hass, entry_id), "assist_improver_practice_cleanup")
        async_at_started(hass, cleanup)
        hass.config_entries.async_update_entry(entry, options=options, minor_version=2)
        _LOGGER.info("Assist Improver: migrated to 1.2 (%s recordings' aliases rewritten)", rewritten)
    if entry.minor_version < 3:
        moved = await _pipeline_state(hass)
        hass.config_entries.async_update_entry(entry, minor_version=3)
        _LOGGER.info("Assist Improver: migrated to 1.3 (%s waiting conversations moved)", moved)
    if entry.minor_version < 4:
        from .snapshot import subentry_of
        narrowed = await _recordings(hass, lambda recording: answered(recording, lambda a: subentry_of(hass, a)))
        hass.config_entries.async_update_entry(entry, minor_version=4)
        _LOGGER.info("Assist Improver: migrated to 1.4 (%s recordings keep only their assistant's configuration)",
                     narrowed)
    if entry.minor_version < 5:
        from .household import back_to_assist
        moved = back_to_assist(hass)
        hass.config_entries.async_update_entry(entry, options={CONF_HOUSEHOLD: True, **entry.options}, minor_version=5)
        _LOGGER.info("Assist Improver: migrated to 1.5 (%s assistants back on Assist, which reads the household "
                     "knowledge)", moved)
    if entry.minor_version < 6:
        found = await _no_fix_found(hass, int(entry.options.get(CONF_TESTS_PER_GOAL, DEFAULT_TESTS_PER_GOAL)))
        hass.config_entries.async_update_entry(entry, minor_version=6)
        _LOGGER.info("Assist Improver: migrated to 1.6 (no fix found for %s goals)", len(found))
    return True


def no_fix_found(runs, improvements, recordings, per_goal):
    """{goal: {at, by, why}} of goals to record as no fix found, from a run log, an improvements store and the
    recordings kept: each one an earlier proposal listed in not_fixed with a reason (by proposer), or whose tests
    reached per_goal (by limit); none an improvement covers or already recorded, and only goals with friction in their
    conversation's latest analysis. A goal any improvement covers is its own (a revision or a conflict gets its
    budget afresh: fresh_found)."""
    from homeassistant.util.dt import utcnow
    from .hypotheses import friction_by_goal
    from .improvements import Improvements
    from .pipeline import not_fixed
    from .reflective import RunIndex
    index, covered = RunIndex(runs), set(improvements.get("unfixed", {}))
    for item in improvements.get("improvements", {}).values():
        covered |= set(Improvements._covers(item))
    open_goals = set()
    for rid, analysis in index.analyzed.items():
        if rid in recordings:
            try:
                open_goals |= {f"{rid[:8]}:{turn}" for turn in friction_by_goal(analysis)}
            except (KeyError, TypeError):  # An analysis in an older shape: nothing to settle in it.
                continue
    found, now = {}, utcnow().isoformat()
    for run in runs:
        if run.get("kind") == "hypothesis_generator" and run.get("status") == "completed":
            for goal, why in not_fixed(run.get("output") or {}).items():
                found[goal] = {"at": now, "by": "proposer", "why": why.strip()[:500]}
    for goal, times in index.tried.items():
        if len(times) >= per_goal:
            found.setdefault(goal, {"at": now, "by": "limit", "why": f"Its {per_goal} tests found no fix."})
    return {goal: entry for goal, entry in found.items() if goal in open_goals and goal not in covered}


def fresh_found(improvements):
    """{goal: when} of goals whose budget starts afresh: those of an improvement waiting for a revision, or whose
    accept found a conflict, from that decision (unless one is kept already)."""
    from .improvements import Improvements
    out = {}
    for item in improvements.get("improvements", {}).values():
        if item.get("status") in ("revision_requested", "conflict") and item.get("decisions"):
            out.update(dict.fromkeys(Improvements._covers(item), item["decisions"][-1].get("at") or ""))
    return {goal: at for goal, at in out.items() if goal not in improvements.get("fresh", {})}


async def _no_fix_found(hass, per_goal):
    """Minor version 6: the goals no_fix_found finds, recorded in the improvements store, and the budgets that start
    afresh (fresh_found). Those recorded."""
    runs = ((await Store(hass, STORAGE_VERSION, f"{DOMAIN}.role_runs", private=True).async_load()) or {}).get(
        "runs", [])
    store = Store(hass, STORAGE_VERSION, f"{DOMAIN}.improvements", private=True, atomic_writes=True)
    saved = (await store.async_load()) or {}
    recordings = ((await Store(hass, STORAGE_VERSION, f"{DOMAIN}.index", private=True).async_load()) or {}).get(
        "sessions", {})
    found, fresh = no_fix_found(runs, saved, set(recordings), per_goal), fresh_found(saved)
    if found or fresh:
        await store.async_save({**saved, "unfixed": {**saved.get("unfixed", {}), **found},
                                "fresh": {**saved.get("fresh", {}), **fresh}})
    return found


def answered(recording, subentry_of):
    """A recording whose turns keep only the answering assistant's provider configuration; None if nothing
    needed changing (one configuration already, or the assistant can't be found any more)."""
    from .retention import pack, unpack
    from .snapshot import answering
    packed = bool(recording.get("packed"))
    recording = unpack(recording)
    contents = [c for turn in recording.get("turns", []) for p in turn.get("preparations") or []
                for c in (p or {}).get("content") or []] + list((recording.get("chat_log") or {}).get("content") or [])
    agents = {c.get("agent_id") for c in contents if c.get("role") == "assistant" and c.get("agent_id")}
    subentry = subentry_of(agents.pop()) if len(agents) == 1 else None
    changed = False
    for turn in recording.get("turns", []):
        configs = (turn.get("prepared") or {}).get("provider_config") or []
        if len(configs) > 1 and len(mine := answering(configs, subentry)) == 1:
            turn["prepared"]["provider_config"] = mine
            changed = True
    if not changed:
        return None
    return pack(recording) if packed else recording


async def _pipeline_state(hass):
    """The run log's pipeline-state record becomes the pipeline's own store; the number of conversations it held."""
    from .pipeline import STORE
    runs_store = Store(hass, STORAGE_VERSION, f"{DOMAIN}.role_runs", private=True, atomic_writes=True)
    saved = await runs_store.async_load()
    runs = (saved or {}).get("runs", [])
    state = [run for run in runs if run.get("kind") == "pipeline_state"]
    if not state:
        return 0
    at = state[-1].get("at") or {}
    await Store(hass, 1, STORE, private=True, atomic_writes=True).async_save({"at": at})
    await runs_store.async_save({**saved, "runs": [run for run in runs if run.get("kind") != "pipeline_state"]})
    return len(at)


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


async def _recordings(hass, upgrade):
    """Each stored recording rewritten by upgrade (a new recording, or None for one that's fine); how many were."""
    from homeassistant.helpers.storage import Store
    from .const import STORAGE_VERSION
    index = ((await Store(hass, STORAGE_VERSION, f"{DOMAIN}.index", private=True).async_load()) or {}).get(
        "sessions", {})
    rewritten = 0
    for key in index:
        store = Store(hass, STORAGE_VERSION, f"{DOMAIN}.session_{key}", private=True, atomic_writes=True)
        recording = await store.async_load()
        if recording and (new := upgrade(recording)) is not None:
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
