"""A hypothesis's changes as a sandbox setup: what a replay changes in the recorded home.

Each change kind maps onto HA's own configuration: exposure, entity aliases,
the assistant's instruction template, and scripts in HA's script format. Code
only places them; the sandbox's HA validates and runs them, so anything HA
can't load stops the candidate instead of being guessed at.
"""

from copy import deepcopy

from homeassistant.util import slugify

from .cases import CaseError


def ha_step(step):
    """A proposed script step in HA's script format; a closing stop step is HA's own stop action."""
    if "stop" in step:
        return {"stop": step["stop"]}
    out = {"action": step["service"], "target": {"entity_id": step["entity_id"]}}
    if step.get("data"):
        out["data"] = deepcopy(step["data"])
    return out


def setup_from_changes(changes, *, source, prompt, scripts, user_id=None):
    """{exposure, aliases, alias_removals (when any), household (when any), prompt, scripts, prompt_changed} for a
    sandbox, from a hypothesis's changes.

    source is the recorded home, prompt the assistant's recorded instruction template, scripts the household's
    script definitions ({object id: config}), user_id the person speaking. Instruction lines go into the household
    knowledge (household.py), as applying puts them: everyone's, or the speaking person's for this_user; the replay
    reads them through Assist with household knowledge, as the assistant will.
    """
    from . import household
    exposure, aliases, removals, new_scripts = {}, {}, {}, {}
    template, prompt_changed, knowledge = prompt or "", False, None
    for n, change in enumerate(changes, 1):
        kind, where = change["kind"], f"Change {n} ({change['kind']})"
        entity_id = change.get("entity_id")
        if kind in ("preference", "prompt"):
            base = knowledge if knowledge is not None else source.get("household") or household.EMPTY
            knowledge, template, problems = household.edit(base, [change], user_id, template)
            if problems:
                raise CaseError(f"{where}: the text it replaces isn't in the household knowledge or the "
                                "assistant's instructions")
            prompt_changed = True
        elif kind == "alias":
            alias = (change.get("alias") or "").strip()
            if entity_id not in source["states"] or not alias:
                raise CaseError(f"{where}: needs a recorded device and an alias")
            aliases.setdefault(entity_id, []).append(alias)
            for old in change.get("replaces") or []:
                removals.setdefault(entity_id, []).append(old)
            prompt_changed = True  # Assist's device list shows aliases.
        elif kind == "exposure":
            if entity_id not in source["states"]:
                raise CaseError(f"{where}: {entity_id} isn't in the recorded home")
            exposure[entity_id] = change.get("expose", True)
            prompt_changed = prompt_changed or not entity_id.startswith("script.")  # Scripts aren't listed.
        elif kind == "script":
            steps = [ha_step(s) for s in change.get("steps") or []]
            if entity_id:
                object_id = entity_id.split(".", 1)[1]
                if not entity_id.startswith("script.") or object_id not in scripts:
                    raise CaseError(f"{where}: {entity_id} isn't one of the household's scripts")
                definition = deepcopy(new_scripts.get(object_id) or scripts[object_id])
                if change.get("text"):
                    definition["description"] = change["text"]
                if steps:
                    definition["sequence"] = steps
                new_scripts[object_id] = definition
            else:
                name = (change.get("script_name") or "").strip()
                object_id = slugify(name)
                if not object_id or not steps:
                    raise CaseError(f"{where}: a new script needs a name and steps")
                if object_id in scripts or f"script.{object_id}" in source["states"]:
                    raise CaseError(f"{where}: a script named {name} already exists")
                new_scripts[object_id] = {"alias": name, "description": change.get("text") or "", "sequence": steps}
                if change.get("expose", True):
                    exposure[f"script.{object_id}"] = True
        else:
            raise CaseError(f"{where}: unknown change kind")
    setup = {"exposure": exposure, "aliases": aliases, "prompt": template if prompt_changed else None,
             "scripts": new_scripts, "prompt_changed": prompt_changed}
    if removals:  # Only when an alias replaces another, so plans without one keep their IDs.
        setup["alias_removals"] = removals
    if knowledge is not None:  # Only with instruction lines.
        setup["household"] = knowledge
    return setup
