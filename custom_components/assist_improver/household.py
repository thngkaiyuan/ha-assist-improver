"""Household knowledge: the instruction lines improvements add, kept in one place for every assistant.

Every assistant that controls Home Assistant through Assist reads them: Home
Assistant's Assist gathers a prompt from each integration's llm platform, and this
integration's (llm.py) adds the household's lines, for everyone and for the person
speaking. With no lines yet, or while the improver is disabled, Assist is exactly
Home Assistant's own. Home Assistant doesn't say which assistant is asking, so it's
every assistant or none.

Up to 0.37 the lines came through a second LLM API, "Assist with household
knowledge", chosen instead of Assist; an assistant could tick both and read every
tool twice.

Uninstalling writes nothing into any assistant's instructions: the settings'
Uninstall shows the lines to save first, and removing the improver from Home
Assistant's own page leaves them in a notification (async_remove).
"""

from copy import deepcopy

from homeassistant.components import persistent_notification
from homeassistant.components.assist_pipeline import PipelineNotFound, async_get_pipeline, async_get_pipelines
from homeassistant.helpers import llm
from homeassistant.helpers.storage import Store
from homeassistant.util.hass_dict import HassKey

from .const import DOMAIN, STORAGE_VERSION
from .instructions import household_line

API_ID = DOMAIN  # The household API up to 0.37: what recordings of then still name.
API_NAME = "Assist with household knowledge"
# hass.data, not runtime_data: Assist reads the household (llm.py) while the entry reloads, and removing reads it.
KEY: HassKey["Household | Fixed"] = HassKey(f"{DOMAIN}_household")
EMPTY = {"everyone": [], "people": {}}
HEADING = "Household knowledge (facts and preferences the household has confirmed; follow them):"
NOTICE = f"{DOMAIN}_household"  # The notification removing the improver leaves, with the lines to keep.
# hass.data, as removing the entry reads it: set while the settings' Uninstall removes the improver, whose step showed
# the lines already.
SHOWN: HassKey[bool] = HassKey(f"{DOMAIN}_shown")
GOODBYE = ("Sorry to go. Here's what your assistants learned: save it, and add it to their instructions if they slip "
           "without it.")


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

    A line for this_user goes in the speaking person's lines (user_id; a list: each of those people's), any other in
    everyone's; with no one speaking (None, or None among them) it goes in everyone's too, and with no one known (an
    empty list: the conversations it was tested on are gone) it's a problem. A line a change replaces is removed where
    it is: from each list the new line goes in that holds it; else, for a person's line, from everyone's, and for
    everyone's, from each person's; else from the assistant's own instructions (lines written there before). One
    that's in none of those has changed since the change was tested: a problem, unless the new line is there already."""
    knowledge, problems = normalized(knowledge), []
    people = list(user_id) if isinstance(user_id, (list, tuple)) else [user_id]
    for change in changes:
        if not household_line(change):  # An edit of one assistant's own instructions isn't a household line.
            continue
        text = (change.get("text") or "").strip()
        if change.get("for") == "this_user" and not people:
            problems.append("Who it's for isn't known: the conversations it was tested on are gone.")
            continue
        mine = change.get("for") == "this_user" and all(people)
        targets = [knowledge["people"].setdefault(p, []) for p in people] if mine else [knowledge["everyone"]]
        for old in change.get("replaces") or []:
            holders = [l for l in targets if old.strip() in l] or [
                l for l in ([knowledge["everyone"]] if mine else knowledge["people"].values()) if old.strip() in l]
            if holders:
                for holder in holders:
                    holder.remove(old.strip())
            elif prompt and old in prompt:
                prompt = prompt.replace(old, "", 1)
            elif any(text not in target for target in targets):
                problems.append(f"The instruction line it replaces has changed since it was tested: {old!r}")
        for target in targets:
            if text and text not in target:
                target.append(text)
    knowledge["people"] = {k: v for k, v in knowledge["people"].items() if v}
    return knowledge, prompt, problems


def prompt(hass, llm_context):
    """What Assist reads of the household knowledge for one request: the lines for everyone and the person speaking,
    or None while the improver is off or nothing is learned. Ends with a blank line, before Home Assistant's own
    part."""
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
        """On while the improver is set up; off while it's disabled or gone."""
        entries = self.hass.config_entries.async_entries(DOMAIN)
        return bool(entries) and not entries[0].disabled_by

    async def load(self):
        self.knowledge = normalized(await self.store.async_load())

    async def save(self, knowledge):
        self.knowledge = normalized(knowledge)
        await self.store.async_save(self.knowledge)


class Fixed:
    """A sandbox's household knowledge: the lines one replay reads, as recorded or as a change would make them."""

    on = True

    def __init__(self, knowledge):
        self.knowledge = normalized(knowledge)


async def serve(hass, knowledge):
    """In a sandbox: its Assist reads these lines, through this integration's llm platform as on the real instance."""
    from homeassistant.setup import async_setup_component
    hass.data[KEY] = Fixed(knowledge)
    if not await async_setup_component(hass, DOMAIN, {}):  # Assist asks the platforms of set-up integrations.
        raise RuntimeError("The sandbox could not set up the improver")


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


def _assistants(hass):
    """Home Assistant's assistants (Assist pipelines) and the preferred one's ID; none before they're loaded."""
    try:
        found = async_get_pipelines(hass)
    except KeyError:  # Not loaded yet.
        return [], None
    try:
        preferred = async_get_pipeline(hass).id
    except PipelineNotFound:
        preferred = None
    return found, preferred


def pipelines(hass):
    """Home Assistant's assistants (Assist pipelines), the preferred one first: [{id, name, preferred}]."""
    found, preferred = _assistants(hass)
    out = [{"id": pipeline.id, "name": pipeline.name, "preferred": pipeline.id == preferred} for pipeline in found]
    return sorted(out, key=lambda p: not p["preferred"])


def voice_assistant(hass, pipeline_id, agent_id):
    """The name of the voice assistant (Assist pipeline) a conversation ran through, whose settings let Home Assistant
    answer a request first: the one the recording kept; else the one using its AI (any AI, when the recording names
    none) that answers first, if just one; else the AI's own name; else Assist."""
    kept = {pipeline.id: pipeline for pipeline in _assistants(hass)[0]}
    if pipeline_id in kept:
        return kept[pipeline_id].name
    first = [p for p in kept.values() if p.prefer_local_intents and (
        p.conversation_engine == agent_id if agent_id else p.conversation_engine != "conversation.home_assistant")]
    if len(first) == 1:
        return first[0].name
    if agent_id and (state := hass.states.get(agent_id)) is not None:
        return state.name
    return "Assist"


def plain(knowledge, names=None):
    """The household's lines as one plain text to paste into an assistant's instructions: everyone's, then each
    person's under their name (a person Home Assistant no longer knows: as removed)."""
    knowledge = normalized(knowledge)
    bullets = lambda lines: "\n".join(f"- {line}" for line in lines)
    parts = [bullets(knowledge["everyone"])] if knowledge["everyone"] else []
    parts += [f"For {(names or {}).get(user_id) or 'a removed user'}:\n{bullets(lines)}"
              for user_id, lines in sorted(knowledge["people"].items())]
    return "\n\n".join(parts)


async def user_names(hass, user_ids):
    """{user ID: name} as Home Assistant names its users; one it no longer knows is left out."""
    found = {}
    for user_id in user_ids:
        user = await hass.auth.async_get_user(user_id) if getattr(hass, "auth", None) else None
        if getattr(user, "name", None):
            found[user_id] = user.name
    return found


async def async_remove(hass):
    """Removing the improver writes nothing into any assistant's instructions: removed from Home Assistant's own page,
    it leaves the household's lines in one notification to save; the settings' Uninstall showed them in its step
    (SHOWN)."""
    household = hass.data.get(KEY)
    knowledge = normalized(household.knowledge if household else await Household(hass).store.async_load())
    if household is not None:  # Its store goes: a reinstall before Home Assistant restarts starts with no lines.
        household.knowledge = normalized(EMPTY)
    if hass.data.get(SHOWN) or knowledge == EMPTY:
        return
    text = plain(knowledge, await user_names(hass, knowledge["people"]))
    persistent_notification.async_create(hass, f"{GOODBYE}\n\n```\n{text}\n```", title="Assist Improver",
                                         notification_id=NOTICE)
