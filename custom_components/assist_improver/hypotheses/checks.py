"""Checks of a generator's or reviewer's answer against the sessions and the household; empty means valid."""

import re

import probatio

from .. import changes
from ..exploration import applied_too_early
from ..household import normalized
from ..instructions import FACTS, carried, edit, effective, is_edit, target
from .goals import friction_by_goal, goal_summary
from .schema import HYPOTHESES


def problems(sessions, answer, schema=HYPOTHESES):
    """What is wrong with a generator answer, checked against the sessions and the household; empty means valid."""
    try:
        schema(answer)
    except probatio.Invalid as err:
        return [f"The answer does not match the format: {err}"]
    found = []
    template = known_lines(sessions)
    items = answer["hypotheses"] if schema is HYPOTHESES else [answer["revised"]] if answer.get("revised") else []
    facts_added = {}  # {(assistant target, fact): hypothesis}: a recommended fact is added in one place only.
    for n, hypothesis in enumerate(items, 1):
        where = f"Hypothesis {n} ({hypothesis['title']!r})"
        for ref in hypothesis["goals"]:
            analysis = sessions.analyses.get(ref["session"])
            if analysis is None:
                found.append(f"{where}: there is no session {ref['session']!r}; list_sessions shows them.")
            elif ref["goal_turn"] not in friction_by_goal(analysis):
                found.append(f"{where}: session {ref['session']} has no goal with friction at turn {ref['goal_turn']}; "
                             f"its goals with friction start at turns {sorted(friction_by_goal(analysis)) or 'none'}.")
        if not hypothesis["goals"]:
            found.append(f"{where}: name the goals it fixes (session and goal turn).")
        if not hypothesis["support"]:
            found.append(f"{where}: list in support the sessions the rule agrees with.")
        for field in ("support", "conflicts"):
            for ref in hypothesis[field]:
                known_session = sessions.briefs.get(ref["session"])
                if known_session is None:
                    found.append(f"{where}: {field} names no session {ref['session']!r}.")
                elif ref.get("turn") is not None and not 1 <= ref["turn"] <= known_session["turns"]:
                    found.append(f"{where}: {field}: session {ref['session']} has no turn {ref['turn']}.")
        for change in hypothesis["changes"]:
            found += _change_problems(where, change, sessions, template)
        found += _edit_problems(sessions, where, hypothesis, facts_added)
    if schema is HYPOTHESES:  # A question for the household leaves the goals open until it's answered.
        found += _addressed_problems(sessions, answer) + (
            [] if (answer.get("question_for_user") or "").strip() else _open_goals(sessions, answer))
    return found


def _addressed_problems(sessions, answer):
    """already_addressed, checked: an improvement list_improvements shows, a goal with friction no hypothesis also
    fixes, and an improvement applied only after the session (one applied before it didn't stop the friction)."""
    existing, found = {i["improvement"]: i for i in sessions.existing}, []
    fixed = {(r["session"], r["goal_turn"]) for h in answer.get("hypotheses", []) for r in h["goals"]}
    for ref in answer.get("already_addressed") or []:
        where, item = f"already_addressed {ref['session']}:{ref['goal_turn']}", existing.get(ref["improvement"])
        analysis, known_session = sessions.analyses.get(ref["session"]), sessions.briefs.get(ref["session"])
        if analysis is None or ref["goal_turn"] not in friction_by_goal(analysis):
            found.append(f"{where}: there's no goal with friction there.")
        elif item is None:
            found.append(f"{where}: there's no improvement {ref['improvement']!r}; list_improvements shows "
                         f"{', '.join(sorted(existing)) or 'none'}.")
        elif (ref["session"], ref["goal_turn"]) in fixed:
            found.append(f"{where}: a hypothesis fixes it too; keep it in one place.")
        elif not ref["why"].strip():
            found.append(f"{where}: say why {item['title']!r} addresses it.")
        elif known_session is not None and (why := applied_too_early(item, known_session["started"])):
            found.append(f"{where}: {why} Fix it, or list it in not_fixed with why.")
    return found


def open_goals(sessions, answer):
    """Goals with friction that neither a hypothesis nor not_fixed addresses."""
    addressed = {(r["session"], r["goal_turn"]) for h in answer.get("hypotheses", []) for r in h["goals"]}
    addressed |= {(r["session"], r["goal_turn"]) for r in answer.get("not_fixed") or [] if r["why"].strip()}
    addressed |= {(r["session"], r["goal_turn"]) for r in answer.get("already_addressed") or [] if r["why"].strip()}
    return [(s, turn) for s, analysis in sorted(sessions.analyses.items())
            for turn in sorted(friction_by_goal(analysis)) if (s, turn) not in addressed]


def _open_goals(sessions, answer):
    left = open_goals(sessions, answer)
    if not left:
        return []
    names = [f"{s}:{t} ({goal_summary(sessions.analyses[s], t)})" for s, t in left]
    return [f"These goals still have friction that no hypothesis fixes: {', '.join(names)}. Find a fix for each "
            "(an idea the reviewer rejected may have a different fix), list it in already_addressed if an existing "
            "improvement covers it, or in not_fixed with why the friction is outside the assistant's control."]


def household_knowledge(sessions):
    """The household knowledge as it is now (lines every assistant on Assist reads), in today's setup."""
    return normalized(sessions.household.source.get("household"))


def known_lines(sessions):
    """Every instruction line a change can replace: the assistant's instructions and the household knowledge."""
    template, _ = instructions(sessions)
    knowledge = household_knowledge(sessions)
    lines = knowledge["everyone"] + [l for mine in knowledge["people"].values() for l in mine]
    return (template or "") + "\n" + "\n".join(lines) if template is not None or lines else None


def instructions(sessions):
    """(template, configuration) of the newest recorded assistant: its instructions as they are now (as it ran with
    them when it's no longer set up, or isn't told apart), or None."""
    assistant = getattr(sessions.assistant_view, "assistant", None)
    record = sessions.assistants().get(target(assistant)) if assistant else None
    return (sessions.current_template(record) if record else (assistant or {}).get("options", {}).get("prompt"),
            assistant)


def _edit_problems(sessions, where, hypothesis, facts_added):
    """A hypothesis's assistant changes (edits of one assistant's own instructions), checked: an assistant the sessions
    used, text to add or lines to replace, replaced lines that are in its current instructions, instructions Home
    Assistant renders once edited (when rendered: SubmitHypotheses renders them first), no recommended fact its
    instructions, another hypothesis or an existing improvement give already, and no goal on an assistant whose
    instructions are all it changes."""
    found, edits = [], {}
    for change in sessions.with_targets([c for c in hypothesis["changes"] if c["kind"] == "assistant"]):
        name = change.get("assistant")
        if not (name or "").strip():
            found.append(f"{where}: an assistant change is for one assistant: name it in assistant "
                         f"({sessions.assistant_names()}); a line for every assistant is a preference.")
            continue
        if not change.get("target"):
            found.append(f"{where}: there's no assistant {name!r}; the assistants are {sessions.assistant_names()}.")
            continue
        if not is_edit(change):
            continue
        record = sessions.assistants()[change["target"]]
        if not (change.get("text") or "").strip() and not change.get("replaces"):
            found.append(f"{where}: an edit of {record['name']}'s instructions needs text to add, or lines in replaces "
                         "to remove.")
        if change.get("for") == "this_user":
            found.append(f"{where}: {record['name']}'s instructions are read for everyone who uses it; for one person, "
                         "write the condition into the line, or make it a preference for this_user.")
        current = effective(sessions.current_template(record))
        for line in change.get("replaces") or []:
            if line.strip() not in current:
                found.append(f"{where}: replaces {line!r}, which isn't in {record['name']}'s current instructions; "
                             "quote lines exactly as read_assistant_instructions shows them.")
        edits.setdefault(change["target"], []).append(change)
    for target, changes in edits.items():
        record = sessions.assistants()[target]
        template, _ = sessions.edited(target, changes)
        rendered = (sessions.renders or {}).get((target, template)) or {}
        if rendered.get("error"):
            found.append(f"{where}: Home Assistant can't render {record['name']}'s instructions with this change: "
                         f"{rendered['error']}")
        replaced = [line for c in changes for line in c.get("replaces") or []]
        kept = edit(sessions.current_template(record), [{"replaces": replaced}])[0] if replaced else None
        given = sessions.facts_given(record, kept)  # What stays of its instructions gives these already.
        for fact in dict.fromkeys(f for c in changes for f in carried(c.get("text") or "")):
            what = FACTS[fact].what
            if fact in given:
                found.append(f"{where}: {record['name']}'s instructions already give {what} ({given[fact]}); don't add "
                             "it again.")
            elif (target, fact) in facts_added:
                found.append(f"{where}: {facts_added[(target, fact)]} adds {what} to {record['name']}'s instructions "
                             "too; keep it in one hypothesis.")
            elif title := _improvement_adding(sessions.existing, target, fact):
                found.append(f"{where}: the improvement {title!r} already adds {what} to {record['name']}'s "
                             "instructions.")
            facts_added.setdefault((target, fact), where)
    if edits and all(is_edit(c) for c in hypothesis["changes"]):
        for ref in hypothesis["goals"]:
            used = sessions.assistant_of(ref["session"])
            if used is not None and used["target"] not in edits:
                found.append(f"{where}: {ref['session']}:{ref['goal_turn']} was a conversation with {used['name']}, "
                             "whose instructions this doesn't change, so it can't fix it.")
    return found


def _improvement_adding(existing, target, fact):
    """The title of an improvement (waiting for review, or applied) that adds this fact to this assistant's
    instructions, or None."""
    for item in existing or []:
        if any(is_edit(c) and c.get("target") == target and fact in carried(c.get("text") or "")
               for c in item.get("changes") or []):
            return item["title"]
    return None


def _change_problems(where, change, sessions, template=None):
    """One change's problems: a preference's line, and what changes.py checks for the other kinds (Home Assistant's
    own validators and the safety rules, run by the sessions' check_changes, for an editor item or an assistant's LLM
    APIs; today's registries for a registry change)."""
    kind, found = change["kind"], []
    if kind == "preference" and not (change.get("text") or "").strip():
        found.append(f"{where}: a {kind} change needs text: the line for Assist's instructions.")
    if kind == "preference" and not change.get("for"):
        found.append(f"{where}: say who a {kind} line is for: everyone (household facts), or this_user (the person "
                     "in the sessions: their own lines in the household knowledge).")
    for line in change.get("replaces") or []:
        if kind == "preference" and (template is None or line.strip() not in template):
            found.append(f"{where}: replaces {line!r}, which isn't in Assist's current instructions or the household "
                         "knowledge; quote lines exactly as read_assistant_instructions shows them.")
    if kind in changes.CONFIG_KINDS or kind == "assistant" and "llm_hass_api" in change:
        checked = sessions.checked_of(change)
        found += [f"{where}: {p}" for p in (checked if checked is not None else [UNCHECKED])]
    elif kind == "registry":
        found += [f"{where}: {p}" for p in changes.registry_problems(change, sessions.home())]
    elif kind == "assistant":
        found += [f"{where}: {p}" for p in changes.assistant_problems(change, {})]
    return found


UNCHECKED = "Home Assistant hasn't checked this change; submit it again."  # check_changes never ran on it.


# A sentence that tells Assist what to say: simple phrases, enough for the reviewer to see the talk a change adds.
# Not the household's own words ("when the user says 'my TV'", "if they don't say which"), nor a self-check ("confirm
# that it worked").
_TELL = re.compile(r"\b(?:tell (?:the user|them|him|her|the household)|let (?:the user|them|him|her) know|mention|"
                   r"announce|confirm to (?:the user|them|him|her))\b", re.IGNORECASE)
_SAY = re.compile(r"\bsay\b", re.IGNORECASE)
_SPEAKER = re.compile(r"\b(?:user|users|he|she|they|people|someone|anyone|household|person|you)\s+(?:(?:don't|doesn't|"
                      r"didn't|do not|does not|just|only|may|might|will|would|can)\s+)?$", re.IGNORECASE)


def tells_what_to_say(sentence):
    """Whether a sentence tells Assist what to say: an instruction to tell, mention or announce, or a "say" no one in
    the household is the subject of."""
    return bool(_TELL.search(sentence)) or any(not _SPEAKER.search(sentence[:m.start()])
                                               for m in _SAY.finditer(sentence))


def _sentences(text):
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text or "") if s.strip()]


def speech(hypothesis, scripts=None):
    """What a draft would have Assist say, listed by code for the reviewer: each stop message of a script with its
    words (nested ones too), each sentence in a line or a script's description that tells Assist what to say, and the
    talk it removes (in lines it replaces, and in the description or stop messages of a script it edits: scripts, the
    current definitions by object id; what such an edit keeps isn't listed). Empty when it touches none."""
    out = []
    for change in hypothesis.get("changes") or []:
        kind, config = change.get("kind"), change.get("config") if isinstance(change.get("config"), dict) else {}
        where = change.get("id") or change.get("entity_id") or change.get("assistant") or kind
        old = (scripts or {}).get(change.get("id")) if kind == "script" else None  # An edit: only what changes.
        stops = changes.stops(config) if kind == "script" else []
        out += [{"change": where, "kind": "stop message", "text": stop, "words": len(stop.split())} for stop in stops
                if not old or stop not in changes.stops(old)]
        text = config.get("description") if kind == "script" else change.get("text")
        said = [s for s in _sentences(text) if tells_what_to_say(s)]
        out += [{"change": where, "kind": "tells Assist what to say", "text": s, "words": len(s.split())} for s in said
                if not old or s not in _sentences(old.get("description"))]
        removed = [s for line in change.get("replaces") or [] for s in _sentences(line) if tells_what_to_say(s)]
        if old:
            removed += [s for s in changes.stops(old) if s not in stops]
            removed += [s for s in _sentences(old.get("description")) if tells_what_to_say(s) and s not in said]
        out += [{"change": where, "kind": "removes", "text": s, "words": len(s.split())} for s in removed]
    return out
