"""Checks of a generator's or reviewer's answer against the sessions and the household; empty means valid."""

import probatio

from ..analyzer import top_level
from ..exploration import applied_too_early
from .goals import friction_by_goal
from .schema import HYPOTHESES


def problems(sessions, answer, schema=HYPOTHESES):
    """What is wrong with a generator answer, checked against the sessions and the household; empty means valid."""
    try:
        schema(answer)
    except probatio.Invalid as err:
        return [f"The answer does not match the format: {err}"]
    known, found = set(sessions.household._entities()), []
    template = known_lines(sessions)
    items = answer["hypotheses"] if schema is HYPOTHESES else [answer["revised"]] if answer.get("revised") else []
    for n, hypothesis in enumerate(items, 1):
        where = f"Hypothesis {n} ({hypothesis['title']!r})"
        for ref in hypothesis["goals"]:
            analysis = sessions.analyses.get(ref["session"])
            if analysis is None:
                found.append(f"{where}: there is no session {ref['session']!r}; list_sessions shows them.")
            elif ref["goal_turn"] not in friction_by_goal(analysis):
                found.append(f"{where}: session {ref['session']} has no goal with friction at turn {ref['goal_turn']}; "
                             f"its goals with friction start at turns {sorted(friction_by_goal(analysis)) or 'none'}.")
            elif (title := sessions.settled.get((ref["session"], ref["goal_turn"]))) is not None:
                found.append(f"{where}: {ref['session']}:{ref['goal_turn']} already has an improvement ({title!r}); "
                             "leave it out.")
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
            found += _change_problems(where, change, known, sessions.household, template)
    if schema is HYPOTHESES:
        found += _addressed_problems(sessions, answer) + _open_goals(sessions, answer)
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
            for turn in sorted(friction_by_goal(analysis)) if (s, turn) not in addressed and (s, turn) not in sessions.settled]


def _open_goals(sessions, answer):
    left = open_goals(sessions, answer)
    if not left:
        return []
    names = [f"{s}:{t} ({'; '.join(dict.fromkeys(g['summary'] for g in top_level(sessions.analyses[s].get('goals', []))
                                                  if g['turn'] == t))})" for s, t in left]
    return [f"These goals still have friction that no hypothesis fixes: {', '.join(names)}. Find a fix for each "
            "(an idea the reviewer rejected may have a different fix), list it in already_addressed if an existing "
            "improvement covers it, or in not_fixed with why the friction is outside the assistant's control."]


def household_knowledge(sessions):
    """The household knowledge as last recorded (lines every assistant on the household API reads)."""
    from ..household import normalized
    return normalized(sessions.household.source.get("household"))


def known_lines(sessions):
    """Every instruction line a change can replace: the assistant's instructions and the household knowledge."""
    template, _ = instructions(sessions)
    knowledge = household_knowledge(sessions)
    lines = knowledge["everyone"] + [l for mine in knowledge["people"].values() for l in mine]
    return (template or "") + "\n" + "\n".join(lines) if template is not None or lines else None


def instructions(sessions):
    """The assistant's instructions template as last recorded, or None."""
    assistant = getattr(sessions.assistant_view, "assistant", None)
    return (assistant or {}).get("options", {}).get("prompt"), assistant


def _change_problems(where, change, known, household, template=None):
    kind, found = change["kind"], []
    if kind in ("preference", "prompt") and not (change.get("text") or "").strip():
        found.append(f"{where}: a {kind} change needs text: the line for Assist's instructions.")
    if kind in ("preference", "prompt") and not change.get("for"):
        found.append(f"{where}: say who a {kind} line is for: everyone (household facts), or this_user (the person "
                     "in the sessions: their own lines in the household knowledge).")
    for line in change.get("replaces") or []:
        if kind in ("preference", "prompt") and (template is None or line.strip() not in template):
            found.append(f"{where}: replaces {line!r}, which isn't in Assist's current instructions or the household "
                         "knowledge; quote lines exactly as read_assistant_instructions shows them.")
    if kind in ("alias", "exposure"):
        entity = change.get("entity_id")
        if entity not in known:
            found.append(f"{where}: an {kind} change needs the entity_id of a device in this household; {entity!r} isn't one.")
        elif kind == "alias":
            if not (change.get("alias") or "").strip():
                found.append(f"{where}: an alias change needs alias: the new name.")
            missing = [a for a in change.get("replaces") or [] if a not in household.aliases(entity)]
            if missing:
                found.append(f"{where}: {entity} has no alias {', '.join(map(repr, missing))} to replace; its aliases are "
                             f"{household.aliases(entity) or 'none'}.")
        elif kind == "exposure":
            expose, exposed = change.get("expose", True), household._card(entity).get("exposed")
            if expose and exposed:
                found.append(f"{where}: {entity} is already exposed to Assist.")
            if not expose and not exposed:
                found.append(f"{where}: {entity} isn't exposed to Assist, so there's nothing to hide.")
    if kind == "script":
        existing = change.get("entity_id")
        if existing:  # Editing a script that exists: its description (text) and/or its steps.
            if not existing.startswith("script.") or existing not in known:
                found.append(f"{where}: {existing!r} isn't a script in this household; omit entity_id for a new script.")
            elif not (change.get("text") or "").strip() and not change.get("steps"):
                found.append(f"{where}: editing {existing} needs a new description (text) or new steps.")
        elif not (change.get("script_name") or "").strip() or not change.get("steps"):
            found.append(f"{where}: a new script needs script_name and steps (or give entity_id to edit an existing one).")
        steps = change.get("steps") or []
        for n, step in enumerate(steps, 1):
            if "stop" in step:
                if n != len(steps) or not step["stop"].strip():
                    found.append(f"{where}: a stop step comes last and carries the message for the user.")
                continue
            domain, _, service = step["service"].partition(".")
            if not service or step["entity_id"] not in known:
                found.append(f"{where}: script step {step['service']!r} on {step['entity_id']!r}: use domain.service "
                             "and a device in this household.")
            elif domain not in (step["entity_id"].split(".")[0], "homeassistant"):
                found.append(f"{where}: script step {step['service']!r} doesn't match {step['entity_id']}'s domain.")
    return found
