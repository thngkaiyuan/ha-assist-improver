"""A hypothesis's changes as a sandbox setup: what a replay changes in the recorded home.

Each change kind maps onto HA's own configuration: exposure, entity aliases,
the household knowledge, the instruction template of the assistant the
conversation used, and scripts in HA's script format. Code only places them;
the sandbox's HA validates and runs them, so anything HA can't load stops the
candidate instead of being guessed at.
"""

from copy import deepcopy
import re

from homeassistant.util import slugify

from .cases import CaseError

FIELD_KEY = re.compile(r"[a-z][a-z0-9_]{0,63}")  # As an AI tool's parameter names may be.
FIELD_USE = re.compile(r"{{\s*([a-z][a-z0-9_]*)\s*}}")  # A field's value in a step's data.
TEMPLATE = re.compile(r"{{|{%|{#")
TAKEN = {"context", "this", "none", "true", "false"}  # Home Assistant's own script variables, and template constants.
TARGETS = ("entity_id", "device_id", "area_id", "floor_id", "label_id")


def field_problems(change):
    """What's wrong with a proposed script's text inputs (fields), which Assist fills when it runs the script: each has
    a key (a lowercase name of its own), a name and a description, and a step's data uses it; a step's data names only
    those, as {{ key }}, and no other template, and never in what names a device; a stop message has none."""
    fields, found = change.get("fields") or [], []
    keys = [f.get("key") for f in fields]
    for field in fields:
        if not FIELD_KEY.fullmatch(field.get("key") or "") or field["key"] in TAKEN:
            found.append(f"a field's key is a lowercase name (letters, digits and _, up to 64) that isn't one of "
                         f"{', '.join(sorted(TAKEN))}: {field.get('key')!r} isn't one.")
        if not (field.get("name") or "").strip() or not (field.get("description") or "").strip():
            found.append(f"field {field.get('key')!r} needs a name and a description (what to give, its format).")
    if len(set(keys)) < len(keys):
        found.append("each field has a key of its own.")
    used = set()
    for step in change.get("steps") or []:
        if TEMPLATE.search(step.get("stop") or ""):
            found.append("a stop message is said as written: it uses no field or template.")
        for key, value in (step.get("data") or {}).items():
            if not isinstance(value, str):
                continue
            named = set()
            if TEMPLATE.search(FIELD_USE.sub(lambda m: named.add(m[1]) or "", value)):
                found.append(f"step data {value!r}: a step uses a field as {{{{ key }}}}, and no other template.")
            if named and key.endswith(TARGETS):
                found.append(f"step data {key}: a field gives words, never which device.")
            used |= named
    if unknown := sorted(used - set(keys)):
        found.append(f"the steps use {', '.join(unknown)}, which this change doesn't declare as fields.")
    if unused := [k for k in keys if k not in used]:
        found.append(f"no step's data uses the field {', '.join(map(repr, unused))}.")
    return found


def ha_fields(change):
    """A proposed script's fields in HA's script format: each a text Assist must give."""
    return {f["key"]: {"name": f["name"], "description": f["description"], "required": True, "selector": {"text": {}}}
            for f in change.get("fields") or []}


def ha_step(step):
    """A proposed script step in HA's script format; a closing stop step is HA's own stop action."""
    if "stop" in step:
        return {"stop": step["stop"]}
    out = {"action": step["service"], "target": {"entity_id": step["entity_id"]}}
    if step.get("data"):
        out["data"] = deepcopy(step["data"])
    return out


def setup_from_changes(changes, *, source, prompt, scripts, user_id=None, assistant=None):
    """{exposure, aliases, alias_removals (when any), household (when any), prompt, scripts, prompt_changed} for a
    sandbox, from a hypothesis's changes.

    source is the recorded home, prompt the recorded instruction template of the assistant the conversation used
    (assistant: its target), scripts the household's script definitions ({object id: config}), user_id the person
    speaking. Household lines go into the household knowledge (household.py), as applying puts them: everyone's, or
    the speaking person's for this_user; the replay reads them through Assist, as the assistant will. An edit of an
    assistant's own instructions changes this conversation's template only when it used that assistant.
    """
    from . import household, instructions
    exposure, aliases, removals, new_scripts = {}, {}, {}, {}
    template, prompt_changed, knowledge = prompt or "", False, None
    for n, change in enumerate(changes, 1):
        kind, where = change["kind"], f"Change {n} ({change['kind']})"
        entity_id = change.get("entity_id")
        if instructions.is_edit(change):
            if not change.get("target") or change["target"] != assistant:
                continue  # Another assistant's instructions: this conversation never read them.
            template, problems = instructions.edit(template, [change])
            if problems:
                raise CaseError(f"{where}: the lines it replaces aren't in the instructions this conversation's "
                                "assistant ran with")
            prompt_changed = True
        elif kind in ("preference", "prompt"):
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
            if problems := field_problems(change):
                raise CaseError(f"{where}: {problems[0]}")
            steps = [ha_step(s) for s in change.get("steps") or []]
            if entity_id:
                object_id = entity_id.split(".", 1)[1]
                if not entity_id.startswith("script.") or object_id not in scripts:
                    raise CaseError(f"{where}: {entity_id} isn't one of the household's scripts")
                definition = deepcopy(new_scripts.get(object_id) or scripts[object_id])
                if change.get("text"):
                    definition["description"] = change["text"]
                if steps:  # Its fields go with its steps.
                    definition["sequence"] = steps
                    definition.pop("fields", None)
                    if change.get("fields"):
                        definition["fields"] = ha_fields(change)
                new_scripts[object_id] = definition
            else:
                name = (change.get("script_name") or "").strip()
                object_id = slugify(name)
                if not object_id or not steps:
                    raise CaseError(f"{where}: a new script needs a name and steps")
                if object_id in scripts or f"script.{object_id}" in source["states"]:
                    raise CaseError(f"{where}: a script named {name} already exists")
                new_scripts[object_id] = {"alias": name, "description": change.get("text") or "", "sequence": steps,
                                          **({"fields": ha_fields(change)} if change.get("fields") else {})}
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
