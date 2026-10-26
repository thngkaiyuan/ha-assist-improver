"""Household knowledge: the instruction lines improvements add, kept in one place for every assistant.

Every assistant that controls Home Assistant through Assist reads them: Home
Assistant's Assist gathers a prompt from each integration's llm platform, and this
integration's (llm.py) adds the household's lines, for everyone and for the person
speaking. One switch in the settings turns them off (on by default): off, or with
no lines yet, Assist is exactly Home Assistant's own. Home Assistant doesn't say
which assistant is asking, so it's every assistant or none.

Up to 0.37 the lines came through a second LLM API, "Assist with household
knowledge", chosen instead of Assist; an assistant could tick both and read every
tool twice. migrations.py moves those assistants back to Assist, and only replays
of conversations recorded through it still use it (LegacyAPI, in a sandbox). On
uninstall, the lines are written into each assistant's own instructions, so
nothing learned is lost (see async_remove).
"""

import re
from copy import deepcopy

from homeassistant.helpers import llm
from homeassistant.helpers.storage import Store

from .const import CONF_HOUSEHOLD, DOMAIN, STORAGE_VERSION

API_ID = DOMAIN  # The household API up to 0.37: what assistants and recordings of then still name.
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
    from .instructions import household_line
    knowledge, problems = normalized(knowledge), []
    for change in changes:
        if not household_line(change):  # An edit of one assistant's own instructions isn't a household line.
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


def prompt(hass, llm_context):
    """What Assist reads of the household knowledge for one request: the lines for everyone and the person speaking,
    or None while the switch is off or nothing is learned. Ends with a blank line, before Home Assistant's own part."""
    household = hass.data.get(KEY)
    if household is None or not household.on:
        return None
    text = render(household.knowledge, getattr(getattr(llm_context, "context", None), "user_id", None))
    return f"{text}\n" if text else None


class Household:
    """The knowledge and its store; Assist reads it on every request (llm.py)."""

    def __init__(self, hass):
        self.hass = hass
        self.store = Store(hass, STORAGE_VERSION, f"{DOMAIN}.household", private=True, atomic_writes=True)
        self.knowledge = normalized(EMPTY)

    @property
    def on(self):
        """The settings' Household knowledge switch, on by default; off while the improver is disabled or gone."""
        entries = self.hass.config_entries.async_entries(DOMAIN)
        return bool(entries) and not entries[0].disabled_by and bool(entries[0].options.get(CONF_HOUSEHOLD, True))

    async def load(self):
        self.knowledge = normalized(await self.store.async_load())

    async def save(self, knowledge):
        self.knowledge = normalized(knowledge)
        await self.store.async_save(self.knowledge)


def switched_off(hass):
    """Whether the household turned the Household knowledge switch off (an improver set up, its setting off)."""
    entries = hass.config_entries.async_entries(DOMAIN)
    return bool(entries) and not entries[0].options.get(CONF_HOUSEHOLD, True)


class Fixed:
    """A sandbox's household knowledge: the lines one replay reads, as recorded or as a change would make them."""

    on = True

    def __init__(self, knowledge):
        self.knowledge = normalized(knowledge)


def serve(hass, knowledge):
    """In a sandbox: its Assist reads these lines, through this integration's llm platform as on the real instance."""
    hass.data[KEY] = Fixed(knowledge)
    hass.config.components.add(DOMAIN)  # Home Assistant's Assist asks the platforms of loaded integrations.


class LegacyAPI(llm.API):
    """The household API up to 0.37: Assist, with the household's lines appended to its prompt. Only a sandbox serves
    it now, to replay a conversation recorded through it as it was."""

    def __init__(self, hass, knowledge):
        super().__init__(hass=hass, id=API_ID, name=API_NAME)
        self.knowledge = knowledge

    async def async_get_api_instance(self, llm_context):
        assist = await llm.async_get_api(self.hass, llm.LLM_API_ASSIST, llm_context)
        user_id = llm_context.context.user_id if llm_context.context else None
        extra = render(self.knowledge, user_id)
        return llm.APIInstance(api=self, api_prompt=assist.api_prompt + (f"\n\n{extra}" if extra else ""),
                               llm_context=llm_context, tools=assist.tools,
                               custom_serializer=assist.custom_serializer)


async def async_setup(hass):
    """Load the knowledge, once per run: it outlives the entry's reloads, so Assist never misses it while the
    integration reloads."""
    if KEY in hass.data:
        return hass.data[KEY]
    household = Household(hass)
    await household.load()
    hass.data[KEY] = household
    return household


def api_ids(apis):
    """An assistant's or recording's LLM APIs as a list: stored as a list, one name, or a merged API's id (a|b)."""
    if isinstance(apis, str):
        return apis.split("|")
    return list(apis or [])


def as_assist(apis):
    """The APIs with the household API (up to 0.37) replaced by Assist, each once."""
    return list(dict.fromkeys(llm.LLM_API_ASSIST if a == API_ID else a for a in api_ids(apis)))


def assistants(hass):
    """[(config entry, subentry)] of conversation assistants that control Home Assistant through Assist (or still
    through the household API, up to 0.37)."""
    out = []
    for entry in hass.config_entries.async_entries():
        for sub in entry.subentries.values():
            apis = api_ids(sub.data.get("llm_hass_api"))
            if sub.subentry_type == "conversation" and (API_ID in apis or llm.LLM_API_ASSIST in apis):
                out.append((entry, sub))
    return out


def back_to_assist(hass):
    """Every assistant still on the household API (up to 0.37) uses Assist instead, once (migrations.py): Assist gives
    the lines itself now, and an assistant that ticked both reads each tool once again. Also one that keeps its API in
    its entry's options (an integration without assistants as subentries). How many changed."""
    changed = 0
    for entry, sub in assistants(hass):
        if API_ID in api_ids(sub.data.get("llm_hass_api")):
            hass.config_entries.async_update_subentry(
                entry, sub, data={**sub.data, "llm_hass_api": as_assist(sub.data["llm_hass_api"])})
            changed += 1
    for entry in hass.config_entries.async_entries():
        if API_ID in api_ids(entry.options.get("llm_hass_api")):
            hass.config_entries.async_update_entry(
                entry, options={**entry.options, "llm_hass_api": as_assist(entry.options["llm_hass_api"])})
            changed += 1
    return changed


def pipelines(hass):
    """Home Assistant's assistants (Assist pipelines), the preferred one first: [{id, name, preferred}]."""
    try:
        from homeassistant.components.assist_pipeline.pipeline import KEY_ASSIST_PIPELINE
        store = hass.data[KEY_ASSIST_PIPELINE].pipeline_store
    except (ImportError, KeyError, AttributeError):
        return []
    preferred = store.async_get_preferred_item()
    out = [{"id": pipeline.id, "name": pipeline.name, "preferred": pipeline.id == preferred}
           for pipeline in store.data.values()]
    return sorted(out, key=lambda p: not p["preferred"])


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
    """An assistant's instructions without the household's lines (everyone's, and each person's block, as uninstalling
    writes them there): an assistant on Assist reads them from the household knowledge, so not twice."""
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


async def async_remove(hass, on=True):
    """Uninstalling: while the switch is on, each assistant on Assist gets the household's lines written into its own
    instructions (everyone's lines, then each person's in their block), so nothing learned is lost; one still on the
    household API (up to 0.37) goes back to Assist. Instructions left empty were Home Assistant's default ones: the
    lines go after those."""
    household = hass.data.get(KEY)
    knowledge = normalized(household.knowledge if household else await Household(hass).store.async_load())
    lines = "\n".join(literal(line) for line in knowledge["everyone"])
    for user_id, mine in knowledge["people"].items():
        lines += "\n" + person_block(user_id, mine)
    for entry, sub in assistants(hass):
        data = {**sub.data}
        if API_ID in api_ids(sub.data.get("llm_hass_api")):
            data["llm_hass_api"] = as_assist(sub.data["llm_hass_api"])
        if on and lines.strip():
            prompt = sub.data.get("prompt") or llm.DEFAULT_INSTRUCTIONS_PROMPT
            data["prompt"] = prompt.rstrip("\n") + "\n" + lines.strip("\n") + "\n"
        hass.config_entries.async_update_subentry(entry, sub, data=data)
    if household is not None:  # Read from the instructions now: never twice, even before Home Assistant restarts.
        household.knowledge = normalized(EMPTY)
