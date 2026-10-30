"""What a session's analysis says, read the ways the roles need it: its friction by goal, the devices a goal names,
the session as a script, and what Assist can do with a device."""

from ..analyzer import root_turn, top_level

def short(recording_id):
    return recording_id[:8]


TRIAGED_NOTE = ("A quick check found every request here fulfilled smoothly, so its goals weren't extracted; read its "
                "turns for what was asked and what Assist did.")
UNANALYSED_NOTE = ("Not analysed, so its goals and friction weren't extracted; read its turns for what was asked and "
                   "what Assist did.")


# What an unmet goal's home_couldnt says, wherever a role reads it.
HOME_COULDNT = "its device was unavailable as it was asked for, so the home couldn't do it"
FRICTION_KINDS = {  # What each kind of friction means, for the roles that read it.
    "extra turn": "a turn the user spent getting a goal done (served: what for)",
    "unmet": "a goal that wasn't met: Assist guessed or missed what the user wanted, or its reply didn't give what "
             "they asked to know (why); with home_couldnt, the home couldn't do it",
    "ask": "settings the user's words left open, which the user then chose",
    "side effect": "a device Assist changed that no goal asked for and that didn't end as it started",
    "conduct": "what the household objected to in what Assist said or did in one of its turns (about_assist_turn), "
               "in their own words (quote), in a later turn or in the note it was sent in with",
}


def friction(analysis):
    """The friction in one analysed session, by the turn where it happened: extra turns (with the sub-goals they
    served; the note, after the last turn said, is one when it asks again for what a goal didn't get), goals left
    unmet and asks, side effects left (code-derived) and what the household objected to (conduct goals)."""
    goals = analysis.get("goals", [])
    out = []
    for goal in top_level(goals):  # Assist guessed or missed what the user wanted (DETAILS P9.11.4).
        if goal.get("reached") is False and goal["kind"] != "ask":
            out.append({"turn": goal["turn"], "kind": "unmet", "summary": goal["summary"],
                        **({"home_couldnt": HOME_COULDNT} if goal.get("home_couldnt") else {}),
                        **({"why": goal["answered"]["why"]} if goal.get("answered") else {})})
    said = analysis.get("turns")
    note = [said + 1] if said and any(g["turn"] == said + 1 and g["kind"] not in ("conduct", "thanks", "wrap-up")
                                      for g in goals) else []
    for turn in [*analysis.get("extra_turns", []), *note]:
        served = [g for g in goals if g.get("serves") is not None and g["turn"] == turn]
        out.append({"turn": turn, "kind": "extra turn",
                    "served": [{"summary": g["summary"], "for_goal_at_turn": g["serves"]} for g in served],
                    **({"in_note": True} if turn in note else {})})
    for goal in goals:
        if goal["kind"] == "ask" and goal.get("serves") is None:
            out.append({"turn": goal["turn"], "kind": "ask", "left_open": goal.get("entity_ids", []),
                        "summary": goal["summary"], "user_chose": goal.get("user_chose", [])})
    for item in analysis.get("side_effects_left") or []:
        out.append({"turn": item["turn"], "kind": "side effect",
                    **{k: item[k] for k in ("entity_id", "start", "end")}})
    for goal in goals:
        if goal["kind"] == "conduct":
            out.append({"turn": goal["turn"], "kind": "conduct", "for_goal_at_turn": goal.get("serves"),
                        **_conduct(goal), **({"in_note": True} if said and goal["turn"] > said else {})})
    return sorted(out, key=lambda f: f["turn"])


def goal_devices(analysis, turn):
    """The devices a goal (and the sub-goals serving it) wanted something from."""
    found = set()
    for goal in analysis.get("goals", []):
        serves = goal.get("serves")
        if goal["turn"] == turn or (serves is not None and root_turn(analysis.get("goals", []), serves) == turn):
            found.update([goal["entity_id"]] if goal.get("entity_id") else goal.get("entity_ids") or [])
    return found


def friction_by_goal(analysis):
    """Each goal turn that had friction, with that friction. Stage 3 settled which goal a turn served: an extra
    turn belongs to the goal its sub-goal serves, or else to the latest goal before it; an ask is its own. A side
    effect belongs to the goal whose turn made the change (the goal a sub-goal there serves, or else the latest goal
    up to that turn). What the household objected to belongs to the goal its conduct goal serves, never to the Assist
    turn it's about."""
    goals = analysis.get("goals", [])
    tops = sorted({g["turn"] for g in top_level(goals)})
    serving = {g["turn"]: root_turn(goals, g["serves"]) for g in goals if g.get("serves") is not None}
    groups = {}
    for item in friction(analysis):
        if item["kind"] in ("ask", "unmet"):
            owner = item["turn"]
        elif item["kind"] == "conduct":
            owner = root_turn(goals, item["for_goal_at_turn"])
        elif item["kind"] == "side effect":
            upto = [t for t in tops if t <= item["turn"]]
            owner = item["turn"] if item["turn"] in tops else serving.get(item["turn"]) or (
                upto[-1] if upto else item["turn"])
        else:
            served = [root_turn(goals, s["for_goal_at_turn"]) for s in item["served"]]
            earlier = [t for t in tops if t < item["turn"]]
            owner = served[0] if served else (earlier[-1] if earlier else item["turn"])
        groups.setdefault(owner, []).append(item)
    return groups


def goal_summary(analysis, turn):
    """A goal with friction in a few words: its goals' summaries."""
    return "; ".join(dict.fromkeys(g["summary"] for g in top_level(analysis.get("goals", [])) if g["turn"] == turn))


def removes(sessions, hypothesis):
    """The (session, turn) friction a hypothesis removes: every friction turn of each goal it names."""
    out = set()
    for ref in hypothesis.get("goals", []):
        analysis = sessions.analyses.get(ref["session"])
        if analysis:
            out |= {(ref["session"], f["turn"]) for f in friction_by_goal(analysis).get(ref["goal_turn"], [])}
    return out


def scenario(analysis, view):
    """One session as a script: each turn's words, the goals it started, and the friction it carried; then the note
    it was sent in with, as what it holds."""
    goals = analysis.get("goals", [])
    by_goal = friction_by_goal(analysis)
    owner = {f["turn"]: t for t, items in by_goal.items() for f in items if f["kind"] == "extra turn"}
    turns = []
    zone = view.digest.get("time_zone")
    for line in view.conversation():
        n = line["turn"]
        turn = {"turn": n, "at": _local(line["at"], zone), "user": line["user_says"].strip(),
                "assist": line["assistant_says"]}
        heard = view.heard_by[n - 1] if n <= len(view.heard_by) else None
        device = (view.source.get("devices", {}).get(heard or "") or {}) if heard else {}
        if device:
            turn["heard_by"] = device.get("name_by_user") or device.get("name")
        if line.get("assist_acted_on"):
            turn["assist_acted_on"] = line["assist_acted_on"]
        started = [{**{k: g[k] for k in ("summary", "kind", "entity_id", "state", "states", "brightness_pct", "fact",
                                         "reached") if k in g},
                    **({"home_couldnt": HOME_COULDNT} if g.get("home_couldnt") else {}),
                    **({"why": g["answered"]["why"]} if g.get("answered") and g.get("reached") is False else {})}
                   for g in top_level(goals) if g["turn"] == n and g["kind"] != "ask"]
        if started:
            turn["goals_started"] = started
        asks = [{"left_open": g.get("entity_ids", []), "summary": g["summary"], "user_chose": g.get("user_chose", [])}
                for g in top_level(goals) if g["turn"] == n and g["kind"] == "ask"]
        if asks:
            turn["settings_left_open"] = asks
        if n in owner:
            turn["extra_turn_for_goal_at_turn"] = owner[n]
            served = [g["summary"] for g in goals if g.get("serves") is not None and g["turn"] == n]
            if served:
                turn["served"] = served
        if conduct := [_objection(g, goals) for g in goals if g["kind"] == "conduct" and g["turn"] == n]:
            turn["conduct"] = conduct
        turns.append(turn)
    out = {"session": short(analysis["recording_id"]), "turns": turns, "goals_with_friction": sorted(by_goal)}
    said = analysis.get("turns")
    if said and (note := [g for g in goals if g["turn"] == said + 1]):  # What the note it was sent in with holds.
        out["note"] = {k: v for k, v in (
            ("conduct", [_objection(g, goals) for g in note if g["kind"] == "conduct"]),
            ("served", [g["summary"] for g in note if g["kind"] != "conduct"])) if v}
    if analysis.get("triaged"):  # A quick check found it smooth, so the analyzer didn't extract its goals.
        out["not_analysed"] = TRIAGED_NOTE
    elif analysis.get("not_analysed"):
        out["not_analysed"] = UNANALYSED_NOTE
    left = [{**{k: f[k] for k in ("entity_id", "start", "end")}, "changed_in_turn": f["turn"], "for_goal_at_turn": t}
            for t, items in by_goal.items() for f in items if f["kind"] == "side effect"]
    if left:
        out["side_effects_left"] = sorted(left, key=lambda f: f["changed_in_turn"])
    return out


def _conduct(goal):
    """What a conduct goal says the household objected to: their words, the Assist turn they objected to ("all":
    Assist in general), and the applied improvement that made Assist do it, if any."""
    return {"quote": goal.get("quote") or "", "about_assist_turn": goal.get("about"),
            **({"about_improvement": goal["improvement"]} if goal.get("improvement") else {})}


def _objection(goal, goals):
    """A conduct goal as the proposer and reviewer read it in a session's script, with the goal it counts on."""
    return {**_conduct(goal), "goal_turn": root_turn(goals, goal["serves"]) if goal.get("serves") is not None
            else goal["turn"]}


def _local(at, zone):
    """A turn's time in the household's time zone, as people read it (weekday and time)."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    try:
        moment = datetime.fromisoformat(at).astimezone(ZoneInfo(zone)) if zone else datetime.fromisoformat(at)
    except (ValueError, KeyError):
        return at
    return moment.strftime("%a %Y-%m-%d %H:%M")


# What Assist can do with an exposed device, by domain, as (tool name, what it does); see HA_PRIMER.
def capabilities(view, entity_id, found):
    """What Assist can and can't do with one device: what HA carried out when each Assist tool was tried on it."""
    card = view._card(entity_id)
    out = {"entity_id": entity_id, "name": card["name"], "area": card.get("area"), "exposed": card.get("exposed"),
           "aliases": view.aliases(entity_id)}
    facts = (found or {}).get(entity_id)
    if facts is None:
        out["assist_can"] = "unknown"
    elif not facts["exposed"]:
        out["assist_can"] = "nothing directly: it isn't exposed (a script could control it)"
    else:
        out["assist_can"] = [f"{c['what']} ({c['tool']})" for c in facts["assist_can"]] or [
            "nothing: no Assist tool carried out a command on it (a script could)"]
    return out
