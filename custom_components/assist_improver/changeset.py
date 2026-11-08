"""Applying an accepted improvement to Home Assistant: all of its changes, or none of them.

HA keeps scripts, automations, scenes, registry entries, exposure and the assistant's instructions in separate places
with no shared transaction, so a changeset is made atomic here:

1. Plan every step first, each with the value HA has now (before) and the one it
   gets (after), checked three ways against what the replays were tested on; a
   value the user changed since is a conflict, reported instead of overwritten.
   HA's validators check each editor item and the instruction templates here; its
   editors check each item whole before they write it (a refusal undoes the rest).
2. Apply in a safe order (ORDER, DETAILS P12.5.9): names, registry entries and
   fewer LLM APIs; then scripts and scenes, saved but inert until run; then
   exposure (a new script or scene is seen by Assist from here); then the
   household knowledge and assistants' own instructions, which tell the assistant
   to use the rest; automations last, live once saved. Undo runs the reverse,
   automations first. An edit of one assistant's instructions goes to that
   assistant, and is a conflict when its instructions changed after the
   improvement was tested.
3. A journal is saved before each write; a failed step undoes the done steps in
   reverse order. A journal left unfinished (HA stopped, or the panel went away)
   is undone from where it stands: at startup as far as the improver writes
   itself, then its editor items by the panel when it's next opened. Meanwhile an
   automation the cut-off apply saved is turned off (Changesets._hold), as soon as
   the panel goes or the improver loads again (before HA arms any trigger), and
   one HA loads later as soon as it does.
4. Every written value is read back before the changeset counts as applied.

The improver never writes a configuration file itself: the panel saves scripts, automations and scenes through Home
Assistant's own editors, with the signed-in admin's session (panel.EditorChannel), and HA validates, writes and reloads
them. The rest is written here, through HA's APIs: its services (an automation on or off), its registries' updates and
config subentries (an assistant's, whose reload must succeed or the step is undone).
"""

import asyncio
from copy import deepcopy
import json

from homeassistant.components.homeassistant import async_should_expose
from homeassistant.core import callback
from homeassistant.exceptions import TemplateError
from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er
from homeassistant.helpers.event import async_track_state_added_domain
from homeassistant.helpers.storage import Store

from . import changes as kinds, household, touchpoints
from .const import DOMAIN, STORAGE_VERSION
from .household import normalized
from .instructions import edit, effective, household_line, is_edit, live

# The safe order (DETAILS P12.5.9): names, registry entries and fewer LLM APIs (assistant_api); scripts and scenes,
# inert until run; exposure, which lands on a new script's or scene's registry entry once it's there; the household
# knowledge and instructions (prompt), which tell the assistant to use the rest; automations last, live once saved.
ORDER = ("alias", "entity", "device", "area", "assistant_api", "script", "scene", "exposure", "household", "prompt",
         "automation", "automation_on")
LOAD_TIMEOUT = 30  # Seconds HA may take to load an item the panel saved, let go of one it deleted, or reload.
# Where Home Assistant registers an editor item's entity (domain, platform); its unique ID is the item's id.
ENTITIES = {kind: (kind, platform) for kind, platform in kinds.PLATFORMS.items()}  # A kind is its domain.
# What Home Assistant fires once its editor's reload after a save is done (its script editor's reload fires nothing).
RELOADED = {"automation": "automation_reloaded", "scene": "scene_reloaded"}


CHANGED_SINCE = "It was changed in Home Assistant after the improvement was tested."  # A conflict on the tested base.
BUSY = "Another change is still being applied."  # Another is refused while one applies, or is left open (busy).
REGISTRIES = {"entity": "entities", "device": "devices", "area": "areas"}  # A registry step's kind, and tested's key.


class Conflict(Exception):
    """HA's value isn't the one the step was planned against."""


class NoPanel(Exception):
    """A script, automation or scene step needs the panel, which saves them through HA's editors, and none is there (or
    it went away): the change stays in the journal, from where it stands, until a panel finishes it
    (Changesets.resume)."""


def _same(a, b):
    """Whether two values are the same as they survive JSON in a browser, which the panel saves editor items through:
    a whole-number float (30.0) arrives as an integer (30)."""
    return json.dumps(_sent(a), sort_keys=True, default=str) == json.dumps(_sent(b), sort_keys=True, default=str)


def _sent(value):
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        return {k: _sent(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sent(v) for v in value]
    return value


# Reading and writing each kind of value, through HA's own APIs.

def _aliases(entry):
    """An entity's aliases as plain values: None for its own name (HA's COMPUTED_NAME)."""
    return [None if a is er.COMPUTED_NAME else a for a in entry.aliases]


def _registry(hass, kind, target):
    """A registry entry's fields a registry change sets (changes.py), as plain values; None when it's gone. Read here
    from Home Assistant's registries."""
    if kind == "entity":
        entry = er.async_get(hass).async_get(target)
        fields = ("name", "area_id", "hidden_by", "disabled_by")
    elif kind == "device":
        entry, fields = dr.async_get(hass).async_get(target), kinds.DEVICE_FIELDS
    else:
        entry, fields = ar.async_get(hass).async_get_area(target), kinds.AREA_FIELDS
    if entry is None:
        return None
    found = {f: getattr(entry, f) for f in fields}
    if "aliases" in found:
        found["aliases"] = sorted(found["aliases"])
    return {k: v.value if hasattr(v, "value") else v for k, v in found.items()}


async def read(hass, kind, target):
    """A step's value as Home Assistant has it now. An editor item is read as its editor's GET reads it, here, not
    through the panel: a review is planned as the page opens, and a change cut off is finished at startup, both before
    any panel's saver is there."""
    if kind in kinds.CONFIG_KINDS:
        return deepcopy((await kinds.editor_items(hass, kind)).get(target))
    if kind == "automation_on":
        state = hass.states.get(target)
        return None if state is None else state.state == "on"
    if kind in REGISTRIES:
        return _registry(hass, kind, target)
    if kind == "alias":
        entry = er.async_get(hass).async_get(target)
        return None if entry is None else _aliases(entry)
    if kind == "exposure":
        return async_should_expose(hass, "conversation", target)
    if kind in ("prompt", "assistant_api"):
        entry_id, subentry_id = target.split("/")
        entry = hass.config_entries.async_get_entry(entry_id)
        sub = entry and entry.subentries.get(subentry_id)
        if sub is None:
            return None
        if kind == "prompt":
            return sub.data.get("prompt")
        apis = sub.data.get("llm_hass_api") or []
        return [apis] if isinstance(apis, str) else list(apis)
    if kind == "household":
        return household.normalized(getattr(hass.data.get(household.KEY), "knowledge", None))
    raise ValueError(kind)


async def write(hass, kind, target, value):
    """Everything but an editor item, which only the panel saves (Changesets._write)."""
    if kind == "alias":
        aliases = [er.COMPUTED_NAME if a is None else a for a in value]  # None: its own name (_aliases).
        er.async_get(hass).async_update_entity(target, aliases=aliases)
    elif kind == "exposure":
        touchpoints.get(hass, "exposure")(hass, "conversation", target, value)  # No public API exposes one.
    elif kind == "automation_on":
        await hass.services.async_call("automation", "turn_on" if value else "turn_off", {"entity_id": target},
                                       blocking=True)
    elif kind == "entity":
        hider, disabler = er.RegistryEntryHider, er.RegistryEntryDisabler
        er.async_get(hass).async_update_entity(
            target, name=value["name"], area_id=value["area_id"],
            hidden_by=value["hidden_by"] and hider(value["hidden_by"]),
            disabled_by=value["disabled_by"] and disabler(value["disabled_by"]))  # Enabled: its integration reloads.
    elif kind == "device":
        dr.async_get(hass).async_update_device(target, **{f: value[f] for f in kinds.DEVICE_FIELDS})
    elif kind == "area":
        ar.async_get(hass).async_update(target, aliases=set(value["aliases"]))
    elif kind in ("prompt", "assistant_api"):
        entry_id, subentry_id = target.split("/")
        entry = hass.config_entries.async_get_entry(entry_id)
        sub = entry.subentries[subentry_id]
        key = "prompt" if kind == "prompt" else "llm_hass_api"
        await _reloaded(hass, entry, lambda: hass.config_entries.async_update_subentry(
            entry, sub, data={**sub.data, key: value}))
    elif kind == "household":
        await (await household.async_setup(hass)).save(value)
    else:
        raise ValueError(kind)


async def _reloaded(hass, entry, update):
    """An assistant's config subentry updated (update), and the reload its integration does on that waited for: a
    provider loaded before that isn't loaded after it is a failure, so the step is undone."""
    from homeassistant.config_entries import ConfigEntryState
    was = entry.state
    update()  # Its update listeners start at once, a reload taking the entry's setup lock.
    async with asyncio.timeout(LOAD_TIMEOUT):
        with touchpoints.relied_on("entry_setup_lock"):
            async with entry.setup_lock:
                pass
    if was is ConfigEntryState.LOADED and entry.state is not ConfigEntryState.LOADED:
        raise Conflict(unloaded(entry))


def unloaded(entry):
    """Why an assistant's step failed: its integration didn't load again, in Home Assistant's words where it gives
    them (its config entry's reason), quoted, never inside parentheses."""
    return "Its integration didn't load again" + (f", saying “{entry.reason}”" if entry.reason else "") + "."


def _provider(hass, step):
    """The config entry of an assistant's step (its target: entry/subentry); None for any other step."""
    if step["kind"] in ("prompt", "assistant_api"):
        return hass.config_entries.async_get_entry(step["target"].split("/")[0])
    return None


async def saved(hass, editors, kind, item_id, value):
    """An editor item saved by the panel (editors: its saver) through Home Assistant's editor for its kind, or deleted
    (value None), then waited for until HA has loaded or let go of it (loaded). HA's editors reload after they answer,
    so what their reload fires is listened for from before the save."""
    reloaded = asyncio.Event() if value is not None and kind in RELOADED else None

    @callback
    def done(_event):
        reloaded.set()
    unsubscribe = hass.bus.async_listen(RELOADED[kind], done) if reloaded else None
    try:
        await editors(kind, item_id, value)
        await loaded(hass, kind, item_id, value, reloaded)
    finally:
        if unsubscribe:
            unsubscribe()


async def loaded(hass, kind, item_id, value, reloaded=None):
    """Waits until HA has loaded an editor item the panel saved (its registry entry is there, so exposing it lands on
    it; and reloaded, the event its editor's reload fires, if given), or let go of one it deleted (value None).
    Conflict if it doesn't."""
    registry, changed = er.async_get(hass), asyncio.Event()

    @callback
    def registry_updated(_event):
        changed.set()
    unsubscribe = hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, registry_updated)
    try:
        async with asyncio.timeout(LOAD_TIMEOUT):
            while (registry.async_get_entity_id(*ENTITIES[kind], item_id) is None) != (value is None):
                changed.clear()
                await changed.wait()
            if reloaded is not None:
                await reloaded.wait()
    except TimeoutError:
        raise Conflict("Home Assistant didn't load it." if value is not None
                       else "Home Assistant didn't remove it.") from None
    finally:
        unsubscribe()


# Planning: every step, checked before anything is written.

async def plan_steps(hass, proposal):
    """(steps, already, conflicts) for a proposal: {changes, setup, tested (simulate.tested_against's), provider,
    user_ids}.

    tested holds the values the replays ran on (the household's setup then); a live value that's neither that nor
    the proposed one was changed by the user since, and is a conflict. An item Home Assistant's editor doesn't keep is
    a conflict too, and so is one its validators or the safety rules refuse now. user_ids: the people it was tested
    for (a line for the person speaking goes to each)."""
    from homeassistant.helpers.service import async_get_all_descriptions
    from homeassistant.helpers.template import Template
    setup, tested = proposal["setup"], proposal["tested"]
    steps, already, conflicts = [], [], []

    def add(kind, target, before, after, base, what):
        if _same(before, after):
            already.append({"kind": kind, "target": target, "what": what})
        elif base is not _ANY and not _same(before, base):
            conflicts.append({"kind": kind, "target": target, "what": what,
                              "why": CHANGED_SINCE})
        else:
            steps.append({"kind": kind, "target": target, "before": before, "after": after, "what": what})

    descriptions = await async_get_all_descriptions(hass) if any(setup.get(f"{k}s") for k in kinds.CONFIG_KINDS) \
        else {}
    new = set()  # The entities of new scripts and scenes: no exposure was there to be tested on.
    for kind in kinds.CONFIG_KINDS:
        stored = await kinds.editor_items(hass, kind) if setup.get(f"{kind}s") else {}
        for item_id, config in sorted((setup.get(f"{kind}s") or {}).items()):
            base = (tested.get(f"{kind}s") or {}).get(item_id)  # None: a new item.
            live = deepcopy(stored.get(item_id))
            what = config.get("alias") or config.get("name") or item_id
            if problem := kinds.id_problem(item_id):  # It ends the address the panel saves it at.
                conflicts.append({"kind": kind, "target": item_id, "what": what,
                                  "why": problem[:1].upper() + problem[1:]})
                continue
            if kind == "script" and live is not None:
                config = keep_unchanged_steps(config, live)
            # Safety rule 3: one it was tested on that the editor doesn't keep, or one Home Assistant runs now with its
            # id that it doesn't keep (saving it would give two items one id).
            if live is None and (base is not None and not _same(base, config)
                                 or base is None and _elsewhere(hass, kind, item_id)):
                conflicts.append({"kind": kind, "target": item_id, "what": what,
                                  "why": f"It isn't kept by Home Assistant's {kind} editor ({kind}s.yaml), so it "
                                         "can't be changed here."})
                continue
            if live is None and kind != "automation" and (problem := _taken(hass, kind, item_id, config)):
                conflicts.append({"kind": kind, "target": item_id, "what": what, "why": problem})
                continue
            if live is None and kind != "automation":
                new.add(kinds.new_entity({}, {"kind": kind, "id": item_id, "config": config}))
            # As Home Assistant's validators and the safety rules take it now; the editor checks it whole as it saves.
            validated, problems = await kinds.validated_item(hass, kind, config, descriptions)
            if not problems and kind == "automation" and live is not None:  # Saved, it stays on or off as it is.
                entity_id = er.async_get(hass).async_get_entity_id(*ENTITIES[kind], item_id)
                state = hass.states.get(entity_id) if entity_id else None
                problems = kinds.initial_state_problems(validated, state and state.state)
            if problems:
                conflicts += [{"kind": kind, "target": item_id, "what": what, "why": p} for p in problems]
                continue
            add(kind, item_id, live, config, base, what)
    kept = await kinds.editor_items(hass, "automation") if setup.get("automation_on") else {}
    for entity_id, on in sorted((setup.get("automation_on") or {}).items()):
        live = await read(hass, "automation_on", entity_id)
        if live is None:
            conflicts.append({"kind": "automation_on", "target": entity_id, "what": entity_id,
                              "why": "The automation is no longer in Home Assistant."})
            continue
        if (entry := er.async_get(hass).async_get(entity_id)) is None or entry.unique_id not in kept:  # Rule 3.
            conflicts.append({"kind": "automation_on", "target": entity_id, "what": _name(hass, entity_id),
                              "why": "It isn't kept by Home Assistant's automation editor (automations.yaml), so it "
                                     "can't be changed here."})
            continue
        add("automation_on", entity_id, live, on, (tested.get("automation_on") or {}).get(entity_id, _ANY),
            _name(hass, entity_id))
    for kind, key in REGISTRIES.items():
        for target, fields in sorted(((setup.get("registry") or {}).get(key) or {}).items()):
            live = _registry(hass, kind, target)
            if live is None:
                conflicts.append({"kind": kind, "target": target, "what": target,
                                  "why": f"The {kind} is no longer in Home Assistant."})
                continue
            was = ((tested.get("registry") or {}).get(key) or {}).get(target)
            after = {**live, **fields, **({"aliases": sorted(fields["aliases"])} if "aliases" in fields else {})}
            base = _ANY if was is None else {**live, **was,
                                             **({"aliases": sorted(was["aliases"] or [])} if "aliases" in was else {})}
            add(kind, target, live, after, base, _registry_name(hass, kind, target))
    registry = er.async_get(hass)
    for entity_id, added in sorted(setup.get("aliases", {}).items()):
        entry = registry.async_get(entity_id)
        if entry is None:
            conflicts.append({"kind": "alias", "target": entity_id, "what": entity_id,
                              "why": "The device is no longer in Home Assistant."})
            continue
        live = _aliases(entry) or [None]  # No aliases: Assist knows it by its own name.
        removed = set(setup.get("alias_removals", {}).get(entity_id, []))
        after = [a for a in live if a not in removed] + [a for a in added if a not in live]
        add("alias", entity_id, _aliases(entry), after, _ANY, _name(hass, entity_id))
    for entity_id, exposed in sorted(setup.get("exposure", {}).items()):
        base = _ANY if entity_id in new else tested["exposure"].get(entity_id, False)
        add("exposure", entity_id, async_should_expose(hass, "conversation", entity_id), exposed, base,
            _name(hass, entity_id, setup.get("scripts", {})))
    prompts = {}  # {target: [instructions now, as they become, base, name]}: one step per assistant.
    if any(household_line(c) for c in proposal["changes"]):
        # Household lines go into the household knowledge, which every assistant on Assist reads; a line they
        # replace in the tested assistant's own instructions comes out of those. No assistant is switched.
        provider = proposal["provider"]
        target = f"{provider['config_entry_id']}/{provider['subentry_id']}"
        title = provider.get("title") or "Assist"
        apis = await read(hass, "assistant_api", target)
        if apis is None:
            conflicts.append({"kind": "prompt", "target": target, "what": title,
                              "why": "The assistant it was tested on is no longer set up."})
        else:
            live_prompt = await read(hass, "prompt", target)
            live = await read(hass, "household", "household")
            knowledge, prompt, problems = household.edit(live, proposal["changes"], proposal["user_ids"], live_prompt)
            if prompt != live_prompt:
                try:
                    Template(prompt, hass).ensure_valid()
                except TemplateError as err:  # A template HA can't parse never reaches the assistant.
                    problems.append(f"Home Assistant can't read the edited instructions: {err}")
            if llm_assist() not in household.as_assist(apis):
                problems.append("The assistant doesn't use Assist, so it can't read the household knowledge.")
            if problems:
                conflicts += [{"kind": "household", "target": "household", "what": "Household knowledge", "why": p}
                              for p in problems]
            else:
                add("household", "household", live, knowledge, _ANY, "Household knowledge")
                if prompt != live_prompt:
                    prompts[target] = [live_prompt, prompt, _ANY, title]
    edits = {}
    for change in proposal["changes"]:
        if is_edit(change):
            edits.setdefault(change.get("target"), []).append(change)
    for target, changes in edits.items():
        # An edit of one assistant's own instructions: on its instructions as they are now, which must be those it
        # was tested with.
        title = _assistant_title(hass, target) or changes[0].get("assistant") or "Assist"
        live_prompt = await read(hass, "prompt", target) if target else None
        if not target or await read(hass, "assistant_api", target) is None:
            conflicts.append({"kind": "prompt", "target": target, "what": f"{title}'s instructions",
                              "why": "The assistant it was tested on is no longer set up."})
            continue
        base = tested.get("prompts", {}).get(target, _ANY)  # Changed since: a conflict (add), unless already done.
        before, after, _, _ = prompts.get(target) or [live_prompt, live_prompt, _ANY, title]
        after, problems = edit(after, changes)
        if problems:
            changed = base is not _ANY and not _same(live_prompt, base)
            conflicts += [{"kind": "prompt", "target": target, "what": f"{title}'s instructions", "why": p} for p in (
                [CHANGED_SINCE] if changed else problems)]
            continue
        prompts[target] = [before, after, base, title]
    for change in proposal["changes"]:  # Safety rule 2: only ever fewer LLM APIs, as the assistant has them now.
        if change.get("kind") != "assistant" or "llm_hass_api" not in change:
            continue
        target = change.get("target")
        title = _assistant_title(hass, target) or change.get("assistant") or "Assist"
        live = await read(hass, "assistant_api", target) if target else None
        if live is None:
            conflicts.append({"kind": "assistant_api", "target": target, "what": f"{title}'s LLM APIs",
                              "why": "The assistant it was tested on is no longer set up."})
        elif added := sorted(set(change["llm_hass_api"]) - set(live)):
            conflicts.append({"kind": "assistant_api", "target": target, "what": f"{title}'s LLM APIs",
                              "why": f"It would add LLM APIs ({', '.join(added)}), which a change never does."})
        else:
            add("assistant_api", target, live, list(change["llm_hass_api"]),
                (tested.get("apis") or {}).get(target, _ANY), f"{title}'s LLM APIs")
    for target, (before, after, base, title) in prompts.items():
        if target in edits:
            try:
                Template(after, hass).ensure_valid()
            except TemplateError as err:  # A template HA can't parse never reaches the assistant.
                conflicts.append({"kind": "prompt", "target": target, "what": f"{title}'s instructions",
                                  "why": f"Home Assistant can't read the edited instructions: {err}"})
                continue
        add("prompt", target, before, after, base, f"{title}'s instructions")
    steps.sort(key=lambda s: ORDER.index(s["kind"]))
    return steps, already, conflicts


_ANY = object()  # A merged value: any current value is a valid base.


def _elsewhere(hass, kind, item_id):
    """Whether Home Assistant runs an item of this kind with this id now: its entity is loaded (not one the registry
    only remembers, restored)."""
    entity_id = er.async_get(hass).async_get_entity_id(*ENTITIES[kind], item_id)
    state = hass.states.get(entity_id) if entity_id else None
    return state is not None and not state.attributes.get("restored")


def _taken(hass, kind, item_id, config):
    """What's wrong with the entity ID a new script or scene would get, as Home Assistant has its states and registry
    now (changes.new_entity_problem), or None."""
    entity_id = kinds.new_entity({}, {"kind": kind, "id": item_id, "config": config})
    entry = er.async_get(hass).async_get(entity_id)
    entries = {entity_id: {"platform": entry.platform, "unique_id": entry.unique_id}} if entry else {}
    return kinds.new_entity_problem(kind, item_id, config, {entity_id} if hass.states.get(entity_id) else set(),
                                    entries)


def _assistant_title(hass, target):
    """An assistant's name in Home Assistant now, or None."""
    return (live(hass, target) or {}).get("title")


def llm_assist():
    from homeassistant.helpers.llm import LLM_API_ASSIST
    return LLM_API_ASSIST


def _registry_name(hass, kind, target):
    """What the user calls a registry entry: an entity's or area's name, a device's own."""
    if kind == "entity":
        return _name(hass, target)
    if kind == "device":
        device = dr.async_get(hass).async_get(target)
        return (device.name_by_user or device.name or target) if device else target
    area = ar.async_get(hass).async_get_area(target)
    return area.name if area else target


def _name(hass, entity_id, scripts=None):
    """What the user calls a device or script: its name, else its entity ID."""
    state = hass.states.get(entity_id)
    if state is not None and state.attributes.get("friendly_name"):
        return state.attributes["friendly_name"]
    object_id = entity_id.split(".", 1)[1]
    return ((scripts or {}).get(object_id) or {}).get("alias") or entity_id


def keep_unchanged_steps(config, live):
    """The proposed script with the household's own copy of each step it keeps unchanged (HA's script editor adds
    empty metadata and data), so applying changes only what the improvement changes."""
    def bare(step):
        return {k: v for k, v in step.items() if not (k in ("metadata", "data") and not v)} \
            if isinstance(step, dict) else step
    existing = list(live.get("sequence") or [])
    sequence = [deepcopy(next((s for s in existing if bare(s) == bare(step)), step))
                for step in config.get("sequence") or []]
    return {**config, "sequence": sequence}


class Changesets:
    """Applies and undoes planned steps with a journal, and finishes one left open (HA stopped, or the panel went away
    mid-change) from where it stands. One change at a time."""

    def __init__(self, hass, *, writer=write, reader=read):
        self.hass, self.write, self.read = hass, writer, reader
        self.store = Store(hass, STORAGE_VERSION, f"{DOMAIN}.changeset_journal", private=True, atomic_writes=True)
        self.journal = None
        self._lock = asyncio.Lock()  # A change applying, or one left open being finished.
        self._unwatch = None  # While an automation is held (_hold): any HA loads is held too, if the apply saved it.

    async def load(self):
        """The journal HA last saved: a change left open, if any, refuses others until it's finished (resume)."""
        self.journal = ((await self.store.async_load()) or {}).get("journal")

    @property
    def busy(self):
        """A change applying, or one left open: no other starts until it's finished."""
        return bool(self.journal) or self._lock.locked()

    async def _save(self):
        await self.store.async_save({"journal": self.journal})

    async def _finish(self):
        self.journal = None
        await self._save()
        self.stop()

    def stop(self):
        """No automation HA loads is held any more (the change is finished, or the improver unloads)."""
        if self._unwatch:
            self._unwatch()
            self._unwatch = None

    async def hold(self):
        """A change left open by an apply: each automation it saved is turned off now (_hold), as the improver loads,
        before HA arms any trigger, and again as HA loads one later (_loaded); the undo waits for HA to have started
        (resume)."""
        async with self._lock:
            if self.journal and self.journal["action"] == "apply":
                self._cut_off()
                await self._hold()

    async def _write(self, step, value, editors):
        """One step's value written: a script, automation or scene by the panel (editors), through HA's editor for it,
        once HA has loaded it (saved); anything else here."""
        if step["kind"] not in kinds.CONFIG_KINDS:
            await self.write(self.hass, step["kind"], step["target"], value)
            return
        if editors is None:
            raise NoPanel
        await saved(self.hass, editors, step["kind"], step["target"], value)

    async def _hold(self):
        """An apply cut off once it saved an automation (they go last), with no panel to undo it: each automation it
        saved that's still as it saved it is turned off until the panel finishes the undo (DETAILS P12.11), and one HA
        loads later (a save that landed after the panel went, or HA loading its automations after the improver) as
        soon as it does. held: whether each was on, so an edited one is turned on again once the panel has put it
        back (_held_back)."""
        journal, registry = self.journal, er.async_get(self.hass)
        if journal["action"] == "apply" and self._unwatch is None:
            self._unwatch = async_track_state_added_domain(self.hass, "automation", self._loaded)
        held = journal["back"].setdefault("held", {})
        for step in journal["steps"][:journal["back"]["count"]] if journal["action"] == "apply" else []:
            if step["kind"] != "automation" or step["target"] in held:
                continue
            entity_id = registry.async_get_entity_id(*ENTITIES["automation"], step["target"])
            state = self.hass.states.get(entity_id) if entity_id else None
            if state is None or not _same(await self.read(self.hass, "automation", step["target"]), step["after"]):
                continue
            held[step["target"]] = state.state == "on"
            if held[step["target"]]:
                await self.write(self.hass, "automation_on", entity_id, False)
        await self._save()

    @callback
    def _loaded(self, _event):
        self.hass.async_create_task(self.hold(), "assist_improver_hold")

    async def _held_back(self, step):
        """An edited automation the panel put back that was held (_hold) while on: on again."""
        if step["kind"] != "automation" or step["before"] is None or not (
                self.journal["back"].get("held") or {}).get(step["target"]):
            return
        entity_id = er.async_get(self.hass).async_get_entity_id(*ENTITIES["automation"], step["target"])
        if entity_id:
            await self.write(self.hass, "automation_on", entity_id, True)

    async def _loaded_again(self, step):
        """An assistant's step put back, its integration loaded when the change began: loaded again. A setup that
        failed with the change's value took away the listener that reloads it on an update, so it's reloaded here;
        Conflict if it still doesn't load."""
        from homeassistant.config_entries import ConfigEntryState
        entry = _provider(self.hass, step)
        if entry is None or entry.entry_id not in self.journal.get("loaded", ()) or (
                entry.state is ConfigEntryState.LOADED):
            return
        async with asyncio.timeout(LOAD_TIMEOUT):
            await self.hass.config_entries.async_reload(entry.entry_id)
        if entry.state is not ConfigEntryState.LOADED:
            raise Conflict(unloaded(entry))

    async def run(self, improvement_id, action, steps, editors=None):
        """Write each step in order. All of them, verified: {status: done}. Otherwise every done step is undone:
        {status: rolled_back} with the failure, or {status: needs_attention} if an undo failed too. editors: the panel
        saving a script, automation or scene (panel.EditorChannel.save); without it, steps with one are refused before
        anything is written. NoPanel: the panel went away mid-change, which is left open for the next one (resume)."""
        if self.busy:
            raise Conflict(BUSY)
        if editors is None and any(s["kind"] in kinds.CONFIG_KINDS for s in steps):
            # Only a panel saves editor items, so this reaches only a panel: one whose saver is gone, or an older one.
            raise Conflict("Reload the page and try again.")
        async with self._lock:
            from homeassistant.config_entries import ConfigEntryState
            # The assistants whose integration is loaded now, so loaded again if their step goes back (_loaded_again).
            loaded = sorted({e.entry_id for s in steps if (e := _provider(self.hass, s))
                             and e.state is ConfigEntryState.LOADED})
            self.journal = {"improvement_id": improvement_id, "action": action, "steps": deepcopy(steps), "done": -1,
                            "loaded": loaded}
            await self._save()  # Before any write.
            failure = None
            for index, step in enumerate(steps):
                self.journal["done"] = index  # In flight: recovery checks this step too.
                await self._save()
                written = None  # Not written; "maybe": its write failed; "yes": it returned.
                try:
                    live = await self.read(self.hass, step["kind"], step["target"])
                    if not _same(live, step["before"]):
                        raise Conflict(f"{step['what']} changed while the improvement was being applied.")
                    written = "maybe"
                    await self._write(step, step["after"], editors)
                    written = "yes"
                    if not _same(await self.read(self.hass, step["kind"], step["target"]), step["after"]):
                        raise Conflict(f"{step['what']} didn't take the new value.")
                except NoPanel:  # Left open for the next panel, any automation it saved held off meanwhile.
                    self._cut_off()
                    await self._hold()
                    raise
                except Exception as err:  # Any failure undoes the changeset; a crash (BaseException) leaves the journal.
                    failure = {"step": index, "what": step["what"], "error": str(err) or type(err).__name__}
                    break
            if failure is None:
                await self._finish()
                return {"status": "done", "steps": deepcopy(steps)}
            # Changed before the write: that step isn't ours to undo. Written, but HA holds something else: ours.
            self.journal["back"] = {"count": failure["step"] + bool(written), "in_flight": written == "maybe",
                                    "ours": written == "yes", "failure": failure, "undone": [], "stuck": []}
            await self._save()
            outcome = await self._back(editors)
            return {k: outcome[k] for k in ("status", "failure", "undone", "stuck")}

    async def resume(self, editors=None, late=None):
        """A change left open, its done steps undone in reverse from where it stands: without the panel (editors) as
        far as the improver writes itself, stopping at a script, automation or scene step, which waits for the panel
        (an automation the apply saved is turned off meanwhile: _hold). The outcome once all are back ({improvement_id,
        action, status: rolled_back or needs_attention, undone, stuck}); {status: waiting} while such a step waits, or
        while late() (a save a panel didn't answer in time may still land: nothing is read before it has); None when
        no change is open."""
        async with self._lock:  # After a change applying now: it may leave itself open.
            if not self.journal:
                return None
            journal = self.journal
            if late and late():
                return {"improvement_id": journal["improvement_id"], "action": journal["action"], "status": "waiting"}
            if "back" not in journal:
                self._cut_off()
                await self._save()
            try:
                return await self._back(editors)
            except NoPanel:
                return {"improvement_id": journal["improvement_id"], "action": journal["action"], "status": "waiting"}

    def _cut_off(self):
        """The open journal cut off going forward: its done steps go back from the one in flight, which may not have
        been written (_back)."""
        if "back" not in self.journal:
            self.journal["back"] = {"count": self.journal["done"] + 1, "in_flight": True, "ours": False,
                                    "failure": None, "undone": [], "stuck": []}

    async def _back(self, editors):
        """The open journal's done steps (back: count) undone in reverse: a value still as written goes back; one
        already back is left; anything else is stuck (someone changed it meanwhile, or the undo failed) and reported.
        in_flight: the last may not have been written (a write that failed, or HA stopping), so it goes back only if it
        holds the new value (otherwise it's stuck too, never skipped). ours: the last was written, but HA holds
        something other than it was given (its read-back failed), which goes back too. Progress is saved step by step,
        so NoPanel (an editor item's step and no panel) leaves the rest for the next one, any automation it saved
        turned off meanwhile (_hold). The outcome, once all are back."""
        journal = self.journal
        back = journal["back"]
        while back["count"]:
            step = journal["steps"][back["count"] - 1]
            try:
                live = await self.read(self.hass, step["kind"], step["target"])
                if not _same(live, step["before"]) and (_same(live, step["after"]) or back.get("ours")):
                    await self._write(step, step["before"], editors)
                    if not _same(await self.read(self.hass, step["kind"], step["target"]), step["before"]):
                        raise Conflict("It didn't take its earlier value.")
                    await self._held_back(step)
                    await self._loaded_again(step)
                    back["undone"].append(step["what"])
                elif not _same(live, step["before"]):  # Never left silently: reported, so it's seen to.
                    back["stuck"].append({"what": step["what"], "why": (
                        "It's neither as it was nor as the change set it." if back["in_flight"]
                        else "It was changed by someone else meanwhile.")})
            except NoPanel:
                await self._hold()
                raise
            except Exception as err:
                back["stuck"].append({"what": step["what"], "why": str(err) or type(err).__name__})
            back["count"] -= 1
            back["in_flight"] = back["ours"] = False
            await self._save()
        outcome = {"improvement_id": journal["improvement_id"], "action": journal["action"],
                   "status": "needs_attention" if back["stuck"] else "rolled_back", "undone": back["undone"],
                   "stuck": back["stuck"], **({"failure": back["failure"]} if back["failure"] else {})}
        await self._finish()
        return outcome


def undo_steps(applied, live, kept=None):
    """Steps that undo an applied changeset: each applied step reversed, newest first. A value several improvements
    share (the household knowledge, an assistant's instructions, a device's names) that changed since has only this
    one's own part taken out (taken_back), while that part is as it left it. Otherwise a value that's no longer the one
    applied (the user changed it again) is left alone and reported. live: {(kind, target): current value}. kept:
    {(kind, target): {line or name: {id, title} of the other applied improvement that adds it too}} (a household line
    as (whose, line), whose: "everyone" or a user ID): it stays, and is reported with that improvement (used_by, owner)
    and where it is (kind, target, whose), so it can be handed over to it (its undo takes it out then)."""
    steps, left = [], []
    for step in reversed(applied):
        key = (step["kind"], step["target"])
        now, keep = live[key], (kept or {}).get(key) or {}
        if _same(now, step["after"]) and not keep:
            steps.append({**step, "before": step["after"], "after": step["before"]})
            continue
        if _same(now, step["before"]):
            continue
        taken = taken_back(step, now, keep)
        if taken is None:
            if _same(now, step["after"]):
                steps.append({**step, "before": step["after"], "after": step["before"]})
            else:
                left.append({"what": step["what"], "why": "It was changed again after the improvement was applied."})
            continue
        back, still = taken
        left += [{"what": step["what"], "kept": k[1] if isinstance(k, tuple) else k,
                  "why": "Another applied improvement adds it too.", "used_by": keep[k]["title"],
                  "owner": keep[k]["id"], "kind": step["kind"], "target": step["target"],
                  **({"whose": k[0]} if isinstance(k, tuple) else {})} for k in still]
        if not _same(back, now):
            steps.append({**step, "before": now, "after": back})
    return steps, left


def readable(hass, steps, left):
    """(steps, left): undo steps checked as applying checks them (plan_steps): instructions Home Assistant can't read
    are left as they are and reported, and nothing is kept there for another improvement."""
    from homeassistant.helpers.template import Template
    out = []
    for step in steps:
        if step["kind"] == "prompt" and step["after"]:
            try:
                Template(step["after"], hass).ensure_valid()
            except TemplateError as err:  # A template HA can't parse never reaches the assistant.
                left = [l for l in left if (l.get("kind"), l.get("target")) != (step["kind"], step["target"])] + [
                    {"what": step["what"], "why": f"Home Assistant can't read the edited instructions: {err}"}]
                continue
        out.append(step)
    return out, left


def without(kind, value, keys):
    """A shared value without these lines or names (keys as undo_steps' kept has them): what an improvement's undo
    takes out as its own once another's undo handed them over."""
    if kind == "household":
        value = normalized(value)
        for whose, line in keys:
            lines = value["everyone"] if whose == "everyone" else value["people"].get(whose, [])
            if line in lines:
                lines.remove(line)
        return normalized(value)
    if kind == "alias":
        return [a for a in value or [] if a not in keys]
    if kind == "prompt" and value:  # Each key a whole text, its lines together.
        lines = value.split("\n")
        for text in keys:
            run = text.split("\n")
            if (at := _run_at(lines, run)) is not None:
                del lines[at:at + len(run)]
        return "\n".join(lines)
    return value


def _run_at(lines, run):
    """Where these lines come together in lines, first; None if they don't."""
    return next((n for n in range(len(lines) - len(run) + 1) if lines[n:n + len(run)] == run), None)


def taken_back(step, live, keep=()):
    """(value, kept) for a shared value as it is now with only this step's own part taken out: what it added gone,
    what it replaced or removed back where it was. The household knowledge line by line (everyone's, and each
    person's), a device's names, an assistant's instructions line by line. What it added that's in keep stays, and
    is in kept. None when that part isn't as the step left it, or for any other kind."""
    kind, before, after = step["kind"], step["before"], step["after"]
    if kind == "household":
        before, after, live = normalized(before), normalized(after), normalized(live)
        lists = {"everyone": (before["everyone"], after["everyone"], live["everyone"])} | {
            user_id: tuple(value["people"].get(user_id, []) for value in (before, after, live))
            for user_id in dict.fromkeys([*live["people"], *after["people"], *before["people"]])}
        backs = {whose: _lines_back(*values, {line for who, line in keep if who == whose})
                 for whose, values in lists.items()}
        if None in backs.values():
            return None
        people = {whose: back for whose, (back, _) in backs.items() if whose != "everyone"}
        return (normalized({"everyone": backs["everyone"][0], "people": people}),
                [(whose, line) for whose, (_, still) in backs.items() for line in still])
    if kind == "alias" and live is not None:  # No aliases: Assist knows it by its own name (plan_steps).
        return _lines_back(before or [None], after, live or [None], keep)
    if kind == "prompt" and live is not None:
        base = effective(before)  # Empty instructions are Home Assistant's default ones, which an edit started from.
        taken = _lines_back(base.split("\n"), after.split("\n"), live.split("\n"), keep)
        if taken is None:
            return None
        text = "\n".join(taken[0])
        return (before if text == base else text), taken[1]
    return None


def _lines_back(before, after, live, keep=()):
    """(lines, kept): live with the edit before → after taken back, block by block: each block the edit wrote goes
    back to what was there, found in live where the edit left it (lines it took out go back after the line before
    them). What it wrote that's in keep stays (kept): a line, or a whole text of several lines, all of them together
    (never one line of it: a template's {% endif %} alone breaks the instructions). None when a block isn't in live as
    the edit left it."""
    from difflib import SequenceMatcher
    found = {}  # A line of after: where live has it, when live kept it.
    for a, b, size in SequenceMatcher(None, after, live, autojunk=False).get_matching_blocks():
        found.update({a + k: b + k for k in range(size)})
    edits, kept = [], []
    for tag, i1, i2, j1, j2 in SequenceMatcher(None, after, before, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        if i1 < i2:  # Lines the edit wrote: each still there, together.
            spots = [found.get(i) for i in range(i1, i2)]
            if None in spots or spots != list(range(spots[0], spots[0] + len(spots))):
                return None
            start, end = spots[0], spots[-1] + 1
        elif i1 == 0:
            start = end = 0
        elif (prior := found.get(i1 - 1)) is not None:
            start = end = prior + 1
        else:
            return None
        written, staying = after[i1:i2], set()
        for text in keep:
            run = text.split("\n")
            if (at := _run_at(written, run)) is not None:
                staying.update(range(at, at + len(run)))
                kept.append(text)
        edits.append((start, end, before[j1:j2] + [written[n] for n in sorted(staying)]))
    out = list(live)
    for start, end, lines in sorted(edits, key=lambda e: e[0], reverse=True):
        out[start:end] = lines
    return out, kept
