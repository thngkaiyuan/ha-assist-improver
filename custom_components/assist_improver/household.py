"""Household knowledge: the instruction lines improvements add, kept in one place for every assistant.

An assistant reads them through an LLM API this integration provides, "Assist with
household knowledge": Home Assistant's own Assist API (the same tools, the same
prompt) with the household's lines appended, for everyone and for the person
speaking. An assistant chooses it instead of "Assist" once; after that, every
accepted line reaches it, and every other assistant that chose it.

With no lines yet, it gives exactly what Assist gives. On uninstall, assistants
are switched back to Assist with the lines written into their own instructions,
so nothing learned is lost (see async_remove).
"""

import re
from copy import deepcopy

from homeassistant.helpers import llm
from homeassistant.helpers.storage import Store

from .const import DOMAIN, STORAGE_VERSION

API_ID = DOMAIN
API_NAME = "Assist with household knowledge"
KEY = f"{DOMAIN}_household"  # hass.data: the Household (knowledge and its store).
EMPTY = {"everyone": [], "people": {}}
HEADING = "Household knowledge (facts and preferences the household has confirmed; follow them):"


def normalized(knowledge):
    """The knowledge as every reader takes it: each line on one line (a line typed over several is joined), and no
    person without lines."""
    knowledge = deepcopy(knowledge or EMPTY)
    return {"everyone": [_one_line(line) for line in knowledge.get("everyone") or []],
            "people": {k: [_one_line(line) for line in v] for k, v in (knowledge.get("people") or {}).items() if v}}


def _one_line(line):
    return " ".join(str(line).split())


def render(knowledge, user_id=None):
    """The household's lines as Assist reads them: everyone's, then the speaking person's; empty without lines."""
    knowledge = normalized(knowledge)
    lines = knowledge["everyone"] + (knowledge["people"].get(user_id, []) if user_id else [])
    return HEADING + "\n" + "\n".join(f"- {line}" for line in lines) if lines else ""


def as_text(knowledge, names=None):
    """The knowledge as one text, for diffs: everyone's lines, then each person's (by name where known)."""
    knowledge = normalized(knowledge)
    out = ["For everyone:"] + [f"- {line}" for line in knowledge["everyone"]]
    for user_id, lines in sorted(knowledge["people"].items()):
        out += ["", f"For {(names or {}).get(user_id) or user_id}:"] + [f"- {line}" for line in lines]
    return "\n".join(out)


def edit(knowledge, changes, user_id, prompt=None):
    """(knowledge, prompt, problems) with the changes' instruction lines merged into the household knowledge.

    A line for this_user goes in the speaking person's lines, any other in everyone's. A line a change replaces is
    removed where it is: the household knowledge, or the assistant's own instructions (lines written there before).
    One that's in neither has changed since the change was tested: a problem, unless the new line is there already."""
    knowledge, problems = normalized(knowledge), []
    for change in changes:
        if change["kind"] not in ("preference", "prompt"):
            continue
        text = (change.get("text") or "").strip()
        mine = change.get("for") == "this_user" and user_id
        target = knowledge["people"].setdefault(user_id, []) if mine else knowledge["everyone"]
        for old in change.get("replaces") or []:
            lists = [knowledge["everyone"], *knowledge["people"].values()]
            holder = next((l for l in lists if old.strip() in l), None)
            if holder is not None:
                holder.remove(old.strip())
            elif prompt and old in prompt:
                prompt = prompt.replace(old, "", 1)
            elif text not in target:
                problems.append(f"The instruction line it replaces has changed since it was tested: {old!r}")
        if text and text not in target:
            target.append(text)
    knowledge["people"] = {k: v for k, v in knowledge["people"].items() if v}
    return knowledge, prompt, problems


class HouseholdAPI(llm.API):
    """Assist, with the household's lines appended to its prompt. The same tools, so nothing else changes."""

    def __init__(self, hass, knowledge=None):
        super().__init__(hass=hass, id=API_ID, name=API_NAME)
        self._fixed = knowledge  # A sandbox's fixed knowledge; otherwise the household's current one.

    @property
    def knowledge(self):
        if self._fixed is not None:
            return self._fixed
        household = self.hass.data.get(KEY)
        return household.knowledge if household else EMPTY

    async def async_get_api_instance(self, llm_context):
        assist = await llm.async_get_api(self.hass, llm.LLM_API_ASSIST, llm_context)
        user_id = llm_context.context.user_id if llm_context.context else None
        extra = render(self.knowledge, user_id)
        return llm.APIInstance(api=self, api_prompt=assist.api_prompt + (f"\n\n{extra}" if extra else ""),
                               llm_context=llm_context, tools=assist.tools,
                               custom_serializer=assist.custom_serializer)


class Household:
    """The knowledge and its store; the API reads it on every request."""

    def __init__(self, hass):
        self.hass = hass
        self.store = Store(hass, STORAGE_VERSION, f"{DOMAIN}.household", private=True, atomic_writes=True)
        self.knowledge = normalized(EMPTY)

    async def load(self):
        self.knowledge = normalized(await self.store.async_load())

    async def save(self, knowledge):
        self.knowledge = normalized(knowledge)
        await self.store.async_save(self.knowledge)


async def async_setup(hass):
    """Load the knowledge and offer the API, once per run (it outlives the entry's reloads, so an assistant using it
    never finds it missing while the integration reloads)."""
    if KEY in hass.data:
        return hass.data[KEY]
    household = Household(hass)
    await household.load()
    hass.data[KEY] = household
    llm.async_register_api(hass, HouseholdAPI(hass))
    return household


def assistants(hass):
    """[(config entry, subentry, uses the household API)] of conversation assistants that control Home Assistant
    (through Assist, or already through the household API)."""
    out = []
    for entry in hass.config_entries.async_entries():
        for sub in entry.subentries.values():
            apis = sub.data.get("llm_hass_api") or []
            apis = [apis] if isinstance(apis, str) else apis
            if sub.subentry_type == "conversation" and (API_ID in apis or llm.LLM_API_ASSIST in apis):
                out.append((entry, sub, API_ID in apis))
    return out


def pipelines(hass):
    """Home Assistant's assistants (Assist pipelines), the preferred one first, each with the assistant behind it and
    whether it reads the household knowledge: [{id, name, preferred, target, using}]; target and using are None for an
    assistant that doesn't control Home Assistant through Assist (Home Assistant's own, for example)."""
    try:
        from homeassistant.components.assist_pipeline.pipeline import KEY_ASSIST_PIPELINE
        store = hass.data[KEY_ASSIST_PIPELINE].pipeline_store
    except (ImportError, KeyError, AttributeError):
        return []
    from homeassistant.helpers import entity_registry as er
    registry, preferred = er.async_get(hass), store.async_get_preferred_item()
    using = {f"{entry.entry_id}/{sub.subentry_id}": on for entry, sub, on in assistants(hass)}
    out = []
    for pipeline in store.data.values():
        item = registry.async_get(pipeline.conversation_engine or "")
        target = f"{item.config_entry_id}/{item.config_subentry_id}" if item and item.config_subentry_id else None
        out.append({"id": pipeline.id, "name": pipeline.name, "preferred": pipeline.id == preferred,
                    "target": target if target in using else None, "using": using.get(target)})
    return sorted(out, key=lambda p: not p["preferred"])


def assistants_using(hass):
    """[(config entry, subentry)] of conversation assistants that chose the household API."""
    out = []
    for entry in hass.config_entries.async_entries():
        for sub in entry.subentries.values():
            apis = sub.data.get("llm_hass_api")
            if sub.subentry_type == "conversation" and API_ID in (apis if isinstance(apis, list) else [apis]):
                out.append((entry, sub))
    return out


def literal(line):
    """A line as plain text in a template: one that looks like template code ({{, {%, {#) is kept from running,
    even one that itself says {% endraw %} (written out as a string, outside the raw block)."""
    if not any(m in line for m in ("{{", "{%", "{#")):
        return line
    body = ENDRAW.sub(lambda m: "{% endraw %}{{ '" + m.group(0) + "' }}{% raw %}", line)
    return f"{{% raw %}}{body}{{% endraw %}}"


ENDRAW = re.compile(r"\{%-?\s*endraw\s*-?%\}")


def person_block(user_id, lines):
    """One person's lines in an assistant's own instructions, read only when that person is speaking."""
    return (f"{{% if llm_context.context and llm_context.context.user_id == '{user_id}' %}}\n"
            + "\n".join(literal(line) for line in lines) + "\n{% endif %}")


def without_lines(prompt, knowledge):
    """An assistant's instructions without the household's lines, as uninstalling wrote them there (everyone's lines,
    and each person's block): an assistant that reads the household knowledge again (after a reinstall) shouldn't
    read them twice."""
    if not prompt:
        return prompt
    knowledge = normalized(knowledge)
    text = prompt
    for user_id, mine in knowledge["people"].items():
        text = text.replace(person_block(user_id, mine), "")
    everyone = set(knowledge["everyone"]) | {literal(line) for line in knowledge["everyone"]}
    kept = [line for line in text.split("\n") if line.strip() not in everyone]
    cleaned = "\n".join(kept)
    return prompt if cleaned == prompt else cleaned.rstrip("\n")


async def use_household(hass, target):
    """Switch one assistant ("entry/subentry") from Assist to the household API, taking out of its instructions any
    lines the household knowledge already gives it. False when it's gone or doesn't use Assist."""
    from .changeset import read, write
    apis = await read(hass, "assistant_api", target)
    if apis is None or (API_ID not in apis and llm.LLM_API_ASSIST not in apis):
        return False
    if API_ID not in apis:
        household = hass.data.get(KEY)
        prompt = await read(hass, "prompt", target)
        if (cleaned := without_lines(prompt, household.knowledge if household else EMPTY)) != prompt:
            await write(hass, "prompt", target, cleaned)
        await write(hass, "assistant_api", target,
                    list(dict.fromkeys(API_ID if a == llm.LLM_API_ASSIST else a for a in apis)))
    return True


async def async_remove(hass):
    """Uninstalling: each assistant on the household API goes back to Assist, with the household's lines written
    into its own instructions (everyone's lines, then each person's in their block), so nothing learned is lost."""
    household = hass.data.get(KEY)
    knowledge = normalized(household.knowledge if household else await Household(hass).store.async_load())
    for entry, sub in assistants_using(hass):
        stored = sub.data.get("llm_hass_api") or []
        apis = [llm.LLM_API_ASSIST if a == API_ID else a for a in ([stored] if isinstance(stored, str) else stored)]
        prompt = sub.data.get("prompt") or ""
        lines = "\n".join(literal(line) for line in knowledge["everyone"])
        for user_id, mine in knowledge["people"].items():
            lines += "\n" + person_block(user_id, mine)
        hass.config_entries.async_update_subentry(
            entry, sub, data={**sub.data, "llm_hass_api": list(dict.fromkeys(apis)),
                              "prompt": (prompt.rstrip("\n") + "\n" + lines.strip("\n") + "\n") if lines.strip() else prompt})

