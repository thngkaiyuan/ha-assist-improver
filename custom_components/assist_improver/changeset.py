"""Applying an accepted improvement to Home Assistant: all of its changes, or none of them.

HA keeps scripts, aliases, exposure and the assistant's instructions in separate
places with no shared transaction, so a changeset is made atomic here:

1. Plan every step first, each with the value HA has now (before) and the one it
   gets (after), checked three ways against what the replays were tested on; a
   value the user changed since is a conflict, reported instead of overwritten.
   HA validates scripts and instruction templates before anything is written.
2. Apply in a safe order: scripts (saved, not exposed: inert), aliases, exposure,
   then the household knowledge and assistants' own instructions (instruction
   lines tell the assistant to use the rest). Nothing runs. An edit of one
   assistant's instructions goes to that assistant, and is a conflict when its
   instructions changed after the improvement was tested.
3. A journal is saved before each write; a failed step undoes the done steps in
   reverse order. A journal left unfinished by a restart is undone at startup.
4. Every written value is read back before the changeset counts as applied.
"""

from copy import deepcopy
import json

from homeassistant.components.homeassistant.exposed_entities import async_expose_entity, async_should_expose
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store

from .const import DOMAIN, STORAGE_VERSION

# assistant_api: an assistant switched to the household API (up to 0.37), last; still undone, never planned now.
ORDER = ("script", "alias", "exposure", "household", "prompt", "assistant_api")
SCRIPTS_FILE = "scripts.yaml"  # Where HA's script editor keeps scripts (homeassistant.config.SCRIPT_CONFIG_PATH).


class Conflict(Exception):
    """HA's value isn't the one the step was planned against."""


def _same(a, b):
    return json.dumps(a, sort_keys=True, default=str) == json.dumps(b, sort_keys=True, default=str)


# Reading and writing each kind of value, through HA's own APIs.

async def _read_scripts(hass):
    from homeassistant.util.yaml import load_yaml
    import os
    path = hass.config.path(SCRIPTS_FILE)
    return (await hass.async_add_executor_job(lambda: load_yaml(path) if os.path.isfile(path) else None)) or {}


async def _write_scripts(hass, scripts):
    """As HA's script editor saves: the whole file, written atomically, then a script reload."""
    from homeassistant.util.file import write_utf8_file_atomic
    from homeassistant.util.yaml import dump
    contents = dump(scripts)  # Before opening the file, so a dump error can't truncate it.
    await hass.async_add_executor_job(write_utf8_file_atomic, hass.config.path(SCRIPTS_FILE), contents)
    await hass.services.async_call("script", "reload", blocking=True)


async def read(hass, kind, target):
    if kind == "script":
        return deepcopy((await _read_scripts(hass)).get(target))
    if kind == "alias":
        entry = er.async_get(hass).async_get(target)
        return None if entry is None else er._serialize_aliases(entry.aliases)
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
        from . import household
        return household.normalized(getattr(hass.data.get(household.KEY), "knowledge", None))
    raise ValueError(kind)


async def write(hass, kind, target, value):
    if kind == "script":
        scripts = await _read_scripts(hass)
        if value is None:
            scripts.pop(target, None)
        else:
            scripts[target] = deepcopy(value)
        await _write_scripts(hass, scripts)
        if value is None:  # As HA's script editor deletes: the script's registry entry goes too.
            registry = er.async_get(hass)
            if entity_id := registry.async_get_entity_id("script", "script", target):
                registry.async_remove(entity_id)
    elif kind == "alias":
        er.async_get(hass).async_update_entity(target, aliases=er._deserialize_aliases(value))
    elif kind == "exposure":
        async_expose_entity(hass, "conversation", target, value)
    elif kind in ("prompt", "assistant_api"):
        entry_id, subentry_id = target.split("/")
        entry = hass.config_entries.async_get_entry(entry_id)
        sub = entry.subentries[subentry_id]
        key = "prompt" if kind == "prompt" else "llm_hass_api"
        hass.config_entries.async_update_subentry(entry, sub, data={**sub.data, key: value})
    elif kind == "household":
        from . import household
        await (await household.async_setup(hass)).save(value)
    else:
        raise ValueError(kind)


# Planning: every step, checked before anything is written.

async def plan_steps(hass, proposal):
    """(steps, already, conflicts) for a proposal: {changes, setup, tested: {scripts, exposure}, provider, user_id}.

    tested holds the values the replays started from (the recorded home); a live value that's neither that nor
    the proposed one was changed by the user since, and is a conflict."""
    from homeassistant.components.script.config import async_validate_config_item
    from homeassistant.exceptions import HomeAssistantError
    from homeassistant.helpers.template import Template
    import probatio
    setup, tested = proposal["setup"], proposal["tested"]
    steps, already, conflicts = [], [], []

    def add(kind, target, before, after, base, what):
        if _same(before, after):
            already.append({"kind": kind, "target": target, "what": what})
        elif base is not _ANY and not _same(before, base):
            conflicts.append({"kind": kind, "target": target, "what": what,
                              "why": "It was changed in Home Assistant after the improvement was tested."})
        else:
            steps.append({"kind": kind, "target": target, "before": before, "after": after, "what": what})

    stored = await _read_scripts(hass)
    for object_id, config in sorted(setup.get("scripts", {}).items()):
        base = tested["scripts"].get(object_id)  # None: a new script.
        live = deepcopy(stored.get(object_id))
        what = config.get("alias") or object_id
        if live is not None:
            config = keep_unchanged_steps(config, live)
        if base is not None and live is None and not _same(base, config):
            conflicts.append({"kind": "script", "target": object_id, "what": what,
                              "why": "It isn't stored in scripts.yaml (where the script editor keeps scripts), so it "
                                     "can't be changed here."})
            continue
        try:
            await async_validate_config_item(hass, object_id, config)
        except (probatio.Invalid, HomeAssistantError) as err:
            conflicts.append({"kind": "script", "target": object_id, "what": what,
                              "why": f"Home Assistant doesn't accept it: {err}"})
            continue
        add("script", object_id, live, config, base, what)
    registry = er.async_get(hass)
    for entity_id, added in sorted(setup.get("aliases", {}).items()):
        entry = registry.async_get(entity_id)
        if entry is None:
            conflicts.append({"kind": "alias", "target": entity_id, "what": entity_id,
                              "why": "The device is no longer in Home Assistant."})
            continue
        live = er._serialize_aliases(entry.aliases) or [None]  # No aliases: Assist knows it by its own name.
        removed = set(setup.get("alias_removals", {}).get(entity_id, []))
        after = [a for a in live if a not in removed] + [a for a in added if a not in live]
        add("alias", entity_id, er._serialize_aliases(entry.aliases), after, _ANY, _name(hass, entity_id))
    for entity_id, exposed in sorted(setup.get("exposure", {}).items()):
        base = tested["exposure"].get(entity_id, False)
        add("exposure", entity_id, async_should_expose(hass, "conversation", entity_id), exposed, base,
            _name(hass, entity_id, setup.get("scripts", {})))
    from .instructions import edit, household_line, is_edit
    prompts = {}  # {target: [instructions now, as they become, base, name]}: one step per assistant.
    if any(household_line(c) for c in proposal["changes"]):
        # Household lines go into the household knowledge, which every assistant on Assist reads; a line they
        # replace in the tested assistant's own instructions comes out of those. No assistant is switched.
        from . import household
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
            knowledge, prompt, problems = household.edit(live, proposal["changes"], proposal.get("user_id"),
                                                         live_prompt)
            if prompt != live_prompt:
                try:
                    Template(prompt, hass).ensure_valid()
                except Exception as err:  # A template HA can't parse never reaches the assistant.
                    problems.append(f"Home Assistant can't read the edited instructions: {err}")
            if llm_assist() not in household.as_assist(apis):
                problems.append("The assistant doesn't use Assist, so it can't read the household knowledge.")
            if household.switched_off(hass):  # Nothing would read the lines (nor those it takes out of instructions).
                problems.append("It's off in the settings, so no assistant would read it.")
            if problems:
                conflicts += [{"kind": "household", "target": "household", "what": "Household knowledge", "why": p}
                              for p in problems]
            else:
                # A household line its own instructions hold too (an uninstall wrote it there, say) comes out of them:
                # it reads the line once, from the household knowledge.
                prompt = household.without_lines(prompt, knowledge)
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
                ["It was changed in Home Assistant after the improvement was tested."] if changed else problems)]
            continue
        prompts[target] = [before, after, base, title]
    for target, (before, after, base, title) in prompts.items():
        if target in edits:
            try:
                Template(after, hass).ensure_valid()
            except Exception as err:  # A template HA can't parse never reaches the assistant.
                conflicts.append({"kind": "prompt", "target": target, "what": f"{title}'s instructions",
                                  "why": f"Home Assistant can't read the edited instructions: {err}"})
                continue
        add("prompt", target, before, after, base, f"{title}'s instructions")
    steps.sort(key=lambda s: ORDER.index(s["kind"]))
    return steps, already, conflicts


_ANY = object()  # A merged value: any current value is a valid base.


def _assistant_title(hass, target):
    """An assistant's name in Home Assistant now, or None."""
    from .instructions import live
    return (live(hass, target) or {}).get("title")


def llm_assist():
    from homeassistant.helpers.llm import LLM_API_ASSIST
    return LLM_API_ASSIST


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
    """Applies and undoes planned steps with a journal, and recovers an interrupted one at startup."""

    def __init__(self, hass, *, writer=write, reader=read):
        self.hass, self.write, self.read = hass, writer, reader
        self.store = Store(hass, STORAGE_VERSION, f"{DOMAIN}.changeset_journal", private=True, atomic_writes=True)
        self.journal = None

    async def start(self):
        """An unfinished journal means HA stopped mid-change: undo what it did. Returns the outcome, if any."""
        saved = await self.store.async_load()
        self.journal = (saved or {}).get("journal")
        if not self.journal:
            return None
        journal = self.journal
        undone, stuck = await self._roll_back(journal["steps"][:journal["done"] + 1])
        outcome = {"improvement_id": journal["improvement_id"], "action": journal["action"],
                   "status": "needs_attention" if stuck else "rolled_back", "undone": undone, "stuck": stuck}
        await self._finish()
        return outcome

    async def _save(self):
        await self.store.async_save({"journal": self.journal})

    async def _finish(self):
        self.journal = None
        await self._save()

    async def run(self, improvement_id, action, steps):
        """Write each step in order. All of them, verified: {status: done}. Otherwise every done step is undone:
        {status: rolled_back} with the failure, or {status: needs_attention} if an undo failed too."""
        if self.journal:
            raise Conflict("Another change is still being applied.")
        self.journal = {"improvement_id": improvement_id, "action": action, "steps": deepcopy(steps), "done": -1}
        await self._save()  # Before any write.
        failure = None
        for index, step in enumerate(steps):
            self.journal["done"] = index  # In flight: recovery checks this step too.
            await self._save()
            writing = False
            try:
                live = await self.read(self.hass, step["kind"], step["target"])
                if not _same(live, step["before"]):
                    raise Conflict(f"{step['what']} changed while the improvement was being applied.")
                writing = True
                await self.write(self.hass, step["kind"], step["target"], step["after"])
                if not _same(await self.read(self.hass, step["kind"], step["target"]), step["after"]):
                    raise Conflict(f"{step['what']} didn't take the new value.")
            except Exception as err:  # Any failure undoes the changeset; a crash (BaseException) leaves the journal.
                failure = {"step": index, "what": step["what"], "error": str(err) or type(err).__name__,
                           "written": writing}  # Changed before the write: that step isn't ours to undo.
                break
        if failure is None:
            await self._finish()
            return {"status": "done", "steps": deepcopy(steps)}
        written = failure.pop("written")
        undone, stuck = await self._roll_back(steps[:failure["step"] + written], in_flight=written)
        await self._finish()
        return {"status": "needs_attention" if stuck else "rolled_back", "failure": failure, "undone": undone,
                "stuck": stuck}

    async def _roll_back(self, steps, in_flight=True):
        """Undo steps in reverse: a value still as written goes back; one already back is left; anything else is
        stuck (someone changed it meanwhile, or the undo failed) and reported. in_flight: the last step may not have
        been written (a write that failed, or HA stopping), so it goes back only if it holds the new value."""
        undone, stuck = [], []
        for index, step in reversed(list(enumerate(steps))):
            try:
                live = await self.read(self.hass, step["kind"], step["target"])
                if _same(live, step["before"]):
                    continue
                if not _same(live, step["after"]):
                    if in_flight and index == len(steps) - 1:
                        continue  # Not written by this change: whatever is there isn't ours to undo.
                    stuck.append({"what": step["what"], "why": "It was changed by someone else meanwhile."})
                    continue
                await self.write(self.hass, step["kind"], step["target"], step["before"])
                if not _same(await self.read(self.hass, step["kind"], step["target"]), step["before"]):
                    raise Conflict("It didn't take its earlier value.")
                undone.append(step["what"])
            except Exception as err:
                stuck.append({"what": step["what"], "why": str(err) or type(err).__name__})
        return undone, stuck


def undo_steps(applied, live):
    """Steps that undo an applied changeset: each applied step reversed, newest first. A value that's no longer the
    one applied (the user changed it again) is left alone and reported. live: {(kind, target): current value}."""
    steps, left = [], []
    for step in reversed(applied):
        if _same(live[(step["kind"], step["target"])], step["after"]):
            steps.append({**step, "before": step["after"], "after": step["before"]})
        elif not _same(live[(step["kind"], step["target"])], step["before"]):
            left.append({"what": step["what"], "why": "It was changed again after the improvement was applied."})
    return steps, left
