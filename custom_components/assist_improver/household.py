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
    knowledge = deepcopy(knowledge or EMPTY)
    return {"everyone": list(knowledge.get("everyone") or []),
            "people": {k: list(v) for k, v in (knowledge.get("people") or {}).items() if v}}


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
        update_issues(self.hass)


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


async def async_remove(hass):
    """Uninstalling: each assistant on the household API goes back to Assist, with the household's lines written
    into its own instructions (everyone's lines, then each person's in their block), so nothing learned is lost."""
    household = hass.data.get(KEY)
    knowledge = household.knowledge if household else normalized(await Household(hass).store.async_load())
    for entry, sub in assistants_using(hass):
        apis = [llm.LLM_API_ASSIST if a == API_ID else a for a in sub.data.get("llm_hass_api") or []]
        prompt = sub.data.get("prompt") or ""
        lines = "\n".join(knowledge["everyone"])
        for user_id, mine in knowledge["people"].items():
            lines += (f"\n{{% if llm_context.context and llm_context.context.user_id == '{user_id}' %}}\n"
                      + "\n".join(mine) + "\n{% endif %}")
        hass.config_entries.async_update_subentry(
            entry, sub, data={**sub.data, "llm_hass_api": list(dict.fromkeys(apis)),
                              "prompt": (prompt.rstrip("\n") + "\n" + lines.strip("\n") + "\n") if lines.strip() else prompt})


ISSUE_PREFIX = "household_"  # One Repairs item per assistant that controls the home but doesn't read the knowledge.


def update_issues(hass):
    """Repairs lists each assistant that controls the home without reading the household knowledge (only once the
    knowledge has lines: before that there is nothing to miss). Fixing one switches it (repairs.py)."""
    from homeassistant.helpers import issue_registry as ir
    household = hass.data.get(KEY)
    knowledge = normalized(household.knowledge if household else None)
    has_lines = bool(knowledge["everyone"] or any(knowledge["people"].values()))
    wanted = {f"{ISSUE_PREFIX}{sub.subentry_id}": (entry, sub) for entry, sub, using in assistants(hass)
              if has_lines and not using}
    for (domain, issue_id) in list(ir.async_get(hass).issues):
        if domain == DOMAIN and issue_id.startswith(ISSUE_PREFIX) and issue_id not in wanted:
            ir.async_delete_issue(hass, DOMAIN, issue_id)
    for issue_id, (entry, sub) in wanted.items():
        ir.async_create_issue(hass, DOMAIN, issue_id, is_fixable=True, severity=ir.IssueSeverity.WARNING,
                              translation_key="household_assistant", translation_placeholders={"assistant": sub.title},
                              data={"target": f"{entry.entry_id}/{sub.subentry_id}", "assistant": sub.title})
