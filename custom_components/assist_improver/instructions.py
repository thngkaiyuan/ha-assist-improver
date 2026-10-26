"""Each assistant's own instructions: which assistant a conversation used, the template it ran with, the edits a
"prompt" change makes there, and the facts recommended for every assistant's instructions.

An assistant (a conversation agent: one of a provider's config subentries) has its own instructions, a template Home
Assistant renders for every request with ha_name, user_name and llm_context (components/conversation/chat_log.py),
before Assist's own part and the household knowledge. Household knowledge is shared by every assistant (household.py);
a fault in one assistant's own instructions (a wrong rule, or a fact they don't give Assist) is fixed there, by a
"prompt" change that names the assistant. Code derives which assistant a conversation used, the template it ran with,
and whether a recommended fact is already there; the sandbox says whether an edited template renders (evidence.py).
"""

from dataclasses import dataclass
import re

from homeassistant.helpers import llm


@dataclass(frozen=True)
class Fact:
    """A fact every assistant's instructions should give Assist, and the template line that gives it."""

    key: str
    what: str
    line: str  # In Home Assistant's template language: what Assist reads once rendered.
    why: str
    markers: tuple  # Template code that already gives it (compared without spaces).


# Not the asking device's area: Assist's own part says it ("You are in area ...", components/intent/llm.py) when the
# device has one. The date and time are here: Home Assistant appends them to the instructions only when Assist has no
# date-and-time tool, and 2026.10's llm integration always gives it one (GetDateTime), so Assist knows the time only
# when it thinks to ask.
RECOMMENDED = (
    Fact("speaker", "who's speaking",
         "{% if user_name %}The person speaking is {{ user_name }}.{% endif %}",
         "Requests like \"my study\" or \"my lights\" depend on who asks, and Home Assistant only says it when the "
         "instructions do.",
         ("user_name", "llm_context.context.user_id")),
    Fact("device", "the device the request came from",
         "{% if llm_context.device_id and device_name(llm_context.device_id) %}The request came from "
         "{{ device_name(llm_context.device_id) }}.{% endif %}",
         "Home Assistant says which area the asking device is in, when it has one, but never which device it is "
         "(a phone, a speaker).",
         ("llm_context.device_id",)),
    Fact("date_time", "the date and time",
         "It's {{ now().strftime('%A %-d %B %Y, %H:%M') }}.",
         "Home Assistant leaves the date and time out of the instructions when Assist has its date-and-time tool, as "
         "it does by default: Assist knows them only when it thinks to ask (\"is it late?\", \"tonight\").",
         ("now().strftime('%A", 'now().strftime("%A', "now().date()", "now().isoformat()")),
    Fact("time_zone", "the household's time zone",
         "The household's time zone is {{ now().tzinfo }}.",
         "Home Assistant names the time zone only when Assist asks its date-and-time tool, and then as a short code "
         "(EDT, +08).",
         ("tzinfo", "%Z", "%z")),
)
FACTS = {fact.key: fact for fact in RECOMMENDED}


def _squash(text):
    return re.sub(r"\s+", "", text or "")


def target(config):
    """An assistant's address in Home Assistant ("config entry/subentry"), from a recorded provider configuration;
    None when the recording doesn't say."""
    config = config or {}
    if config.get("config_entry_id") and config.get("subentry_id"):
        return f"{config['config_entry_id']}/{config['subentry_id']}"
    return None


def assistant_config(configs):
    """Of a turn's recorded provider configurations, the one of the assistant that answered: the only one; None when
    several stayed recorded (which one answered wasn't known: cases.py calls that ambiguous)."""
    return configs[0] if len(configs or []) == 1 else None


def ran_with(recording):
    """Which assistant a conversation used and how its instructions read then, from its first prepared turn:
    {target, title, template, rendered, user_id, device_id}, or None (also when which assistant answered isn't
    known). rendered is what the model read of the instructions (the recorded system prompt before Assist's own
    part), where the recording shows it."""
    for turn in recording.get("turns") or []:
        prepared = turn.get("prepared") or {}
        configs = prepared.get("provider_config") or []
        if not configs:
            continue
        if (config := assistant_config(configs)) is None:
            return None
        system = next((m.get("content") for m in ((turn.get("preparations") or [{}])[0].get("content") or [])
                       if m.get("role") == "system"), None)
        api_prompt = prepared.get("api_prompt")
        rendered = system.split(api_prompt, 1)[0] if system and api_prompt and api_prompt in system else None
        context = prepared.get("context") or {}
        return {"target": target(config), "title": config.get("title"),
                "template": (config.get("options") or {}).get("prompt"), "rendered": rendered,
                "user_id": (context.get("context") or {}).get("user_id"), "device_id": context.get("device_id")}
    return None


def live(hass, target):
    """An assistant as Home Assistant has it now: {title, template}, or None when it's no longer set up."""
    entry_id, _, subentry_id = (target or "").partition("/")
    entries = getattr(hass, "config_entries", None)
    entry = entries.async_get_entry(entry_id) if entries is not None and entry_id else None
    sub = entry.subentries.get(subentry_id) if entry is not None else None
    if sub is None:
        return None
    return {"title": sub.title, "template": sub.data.get("prompt")}


def effective(template):
    """The instructions an assistant uses: its template, or Home Assistant's default ones when it has none."""
    return template if (template or "").strip() else llm.DEFAULT_INSTRUCTIONS_PROMPT


def is_edit(change):
    """Whether a change edits one assistant's own instructions (a prompt change naming the assistant)."""
    return change.get("kind") == "prompt" and bool(change.get("assistant") or change.get("target"))


def household_line(change):
    """Whether a change is a line for the household knowledge: a preference, or a prompt line from before prompt
    changes named an assistant."""
    return change.get("kind") == "preference" or (change.get("kind") == "prompt" and not is_edit(change))


def edit(template, changes):
    """(template, problems): an assistant's instructions with these changes' edits: each change's text in place of
    the first line it replaces (the others removed), or added at the end; a change without text only removes. Empty
    instructions are Home Assistant's default ones, edited. A replaced line that isn't there has changed since: a
    problem, unless the new text is there already. Text already there isn't added twice."""
    text, problems = template or "", []
    for change in changes:
        new = (change.get("text") or "").strip("\n")
        if (new.strip() or change.get("replaces")) and not text.strip():  # Editing the default instructions.
            text = llm.DEFAULT_INSTRUCTIONS_PROMPT
        placed = not new.strip() or new in text
        for old in change.get("replaces") or []:
            old = old.strip("\n")
            if not old.strip():
                continue
            if old in text:
                text = text.replace(old, new, 1) if not placed else _remove(text, old)
                placed = True
            elif not (new.strip() and new in text):
                problems.append(f"The instruction line it replaces has changed since it was tested: {old!r}")
        if not placed:
            ending = "\n" if text.endswith("\n") else ""
            text = (text.rstrip("\n") + "\n" + new if text.strip() else new) + ending
    return text, problems


def _remove(text, old):
    """The text without the first occurrence of old: a whole line goes with its line break."""
    start = text.index(old)
    end = start + len(old)
    if (start == 0 or text[start - 1] == "\n") and end < len(text) and text[end] == "\n":
        end += 1
    return text[:start] + text[end:]


def carried(text):
    """The recommended facts a line gives, by the template code it uses."""
    squashed = _squash(text)
    return [fact.key for fact in RECOMMENDED if any(_squash(m) in squashed for m in fact.markers)]


def given(template, rendered=None, values=None):
    """{fact: how} for the recommended facts an assistant's instructions already give, by the template code they use:
    adding one of those again would only repeat it. (One written out in words, such as the speaker's name, is left
    to the roles to judge: maybe_there.)"""
    squashed, found = _squash(effective(template)), {}
    for fact in RECOMMENDED:
        if marker := next((m for m in fact.markers if _squash(m) in squashed), None):
            found[fact.key] = f"its template uses {marker}"
    return found


def maybe_there(template, rendered=None, values=None):
    """{fact: why} for the recommended facts whose value (the speaker's name, the asking device's name, the time zone)
    is written in the instructions or what the model read of them: maybe given in words, for the roles to judge."""
    found = {}
    for fact in RECOMMENDED:
        value = (values or {}).get(fact.key)
        if value and any(re.search(rf"(?<!\w){re.escape(value)}(?!\w)", t or "") for t in (template, rendered)):
            found[fact.key] = f"it says {value}: maybe given already, in words"
    return found


def missing(template, rendered=None, values=None):
    """The recommended facts an assistant's instructions don't give by template code, each as the roles read it: what,
    the line that gives it, why, and (maybe_there) a hint when its value is written there in words."""
    present, hints = given(template, rendered, values), maybe_there(template, rendered, values)
    return [{"fact": fact.key, "what": fact.what, "line": fact.line, "why": fact.why,
             **({"maybe_there": hints[fact.key]} if fact.key in hints else {})}
            for fact in RECOMMENDED if fact.key not in present]


def for_roles():
    """The recommended facts as the proposer's and reviewer's instructions list them."""
    return "\n".join(f"- {fact.what}: {fact.line}\n  Why: {fact.why}" for fact in RECOMMENDED)
