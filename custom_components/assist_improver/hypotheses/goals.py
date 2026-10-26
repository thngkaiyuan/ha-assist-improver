"""What a session's analysis says, read the ways the roles need it: its friction by goal, the devices a goal names,
the session as a script, and what Assist can do with a device."""

from ..analyzer import side_effects_left, top_level

def short(recording_id):
    return recording_id[:8]


TRIAGED_NOTE = ("A quick check found every request here fulfilled smoothly, so its goals weren't extracted; read its "
                "turns for what was asked and what Assist did.")
UNANALYSED_NOTE = ("Not analysed, so its goals and friction weren't extracted; read its turns for what was asked and "
                   "what Assist did.")


FRICTION_KINDS = {  # What each kind of friction means, for the roles that read it.
    "extra turn": "a turn the user spent getting a goal done (served: what for)",
    "ask": "settings the user's words left open, which the user then chose",
    "side effect": "a device Assist changed that no goal asked for and that didn't end as it started",
    "feedback": "what the household said about how Assist behaved (in a turn, or in the note it was sent in with)",
}


def last_turn(analysis):
    """The session's last turn: recorded with the analysis, else the latest any part of it names."""
    named = [g["turn"] for g in analysis.get("goals", [])] + [n["turn"] for n in analysis.get("not_requests", [])] + [
        *analysis.get("extra_turns", []), *analysis.get("pending_turns", []),
        *(f["turn"] for f in analysis.get("feedback") or [] if f.get("turn") is not None)]
    return analysis.get("turns") or max(named, default=1)


def side_effects(analysis):
    """The side effects left that count as friction: those whose start was known (an older analysis kept a sleeping
    car's too), each with the turn whose change left it; an older analysis didn't keep that turn, so its side effects
    go with its first goal (the proposer's script, and a test's case, read the turn from the recording)."""
    from ..analyzer import UNKNOWN_STATES
    first = min((g["turn"] for g in top_level(analysis.get("goals", []))), default=1)
    return [{**item, "turn": item.get("turn") or first} for item in analysis.get("side_effects_left") or []
            if item.get("start") not in (None, *UNKNOWN_STATES)]


def friction(analysis):
    """The friction in one analysed session, by the turn where it happened: extra turns (with the sub-goals they
    served), asks, side effects left (code-derived) and feedback (a note's at the session's last turn)."""
    goals = analysis.get("goals", [])
    out = []
    for turn in analysis.get("extra_turns", []):
        served = [g for g in goals if g.get("serves") is not None and g["turn"] == turn]
        out.append({"turn": turn, "kind": "extra turn",
                    "served": [{"summary": g["summary"], "for_goal_at_turn": g["serves"]} for g in served]})
    for goal in goals:
        if goal["kind"] == "ask" and goal.get("serves") is None:
            out.append({"turn": goal["turn"], "kind": "ask", "left_open": goal.get("entity_ids", []),
                        "summary": goal["summary"], "user_chose": goal.get("user_chose", [])})
    for item in side_effects(analysis):
        out.append({"turn": item["turn"], "kind": "side effect",
                    **{k: item[k] for k in ("entity_id", "start", "end")}})
    for item in analysis.get("feedback") or []:
        note = item.get("turn") is None
        out.append({"turn": last_turn(analysis) if note else item["turn"], "kind": "feedback", "what": item["what"],
                    "about_goal_at_turn": item.get("about_turn"), **({"in_note": True} if note else {}),
                    **({"improvement": item["improvement"]} if item.get("improvement") else {})})
    return sorted(out, key=lambda f: f["turn"])


def goal_devices(analysis, turn):
    """The devices a goal (and the sub-goals serving it) wanted something from."""
    found = set()
    for goal in analysis.get("goals", []):
        if goal["turn"] == turn or goal.get("serves") == turn:
            found.update([goal["entity_id"]] if goal.get("entity_id") else goal.get("entity_ids") or [])
    return found


def friction_by_goal(analysis):
    """Each goal turn that had friction, with that friction. Stage 3 settled which goal a turn served: an extra
    turn belongs to the goal its sub-goal serves, or else to the latest goal before it; an ask is its own. A side
    effect belongs to the goal whose turn made the change (the goal a sub-goal there serves, or else the latest goal
    up to that turn). Feedback belongs to the goal it's about; general feedback is friction on its own, at its turn
    (a note's: the session's last turn), and is tested on the goal before it (validation.targets)."""
    goals = analysis.get("goals", [])
    tops = sorted({g["turn"] for g in top_level(goals)})
    serving = {g["turn"]: g["serves"] for g in goals if g.get("serves") is not None}
    groups = {}
    for item in friction(analysis):
        if item["kind"] == "ask":
            owner = item["turn"]
        elif item["kind"] == "feedback":
            about = item.get("about_goal_at_turn")
            owner = about if about in tops else item["turn"]
        elif item["kind"] == "side effect":
            upto = [t for t in tops if t <= item["turn"]]
            owner = item["turn"] if item["turn"] in tops else serving.get(item["turn"]) or (
                upto[-1] if upto else item["turn"])
        else:
            served = [s["for_goal_at_turn"] for s in item["served"]]
            earlier = [t for t in tops if t < item["turn"]]
            owner = served[0] if served else (earlier[-1] if earlier else item["turn"])
        groups.setdefault(owner, []).append(item)
    return groups


def goal_summary(analysis, turn):
    """A goal with friction in a few words: its goals' summaries, or, for general feedback on its own, the feedback."""
    said = dict.fromkeys(g["summary"] for g in top_level(analysis.get("goals", [])) if g["turn"] == turn)
    said = said or dict.fromkeys(f"Feedback: {f['what']}" for f in friction_by_goal(analysis).get(turn, [])
                                 if f["kind"] == "feedback")
    return "; ".join(said)


def feedback_goal(analysis, turn):
    """The goal turn a test replays for friction at `turn`: the goal there, or, for general feedback on its own,
    the latest goal before it (the replies the household reacted to); None if there's none."""
    tops = sorted({g["turn"] for g in top_level(analysis.get("goals", []))})
    if turn in tops:
        return turn
    earlier = [t for t in tops if t < turn]
    return earlier[-1] if earlier else None


def removes(sessions, hypothesis):
    """The (session, turn) friction a hypothesis removes: every friction turn of each goal it names."""
    out = set()
    for ref in hypothesis.get("goals", []):
        analysis = sessions.analyses.get(ref["session"])
        if analysis:
            out |= {(ref["session"], f["turn"]) for f in friction_by_goal(analysis).get(ref["goal_turn"], [])}
    return out


def scenario(analysis, view):
    """One session as a script: each turn's words, the goals it started, and the friction it carried."""
    goals = analysis.get("goals", [])
    by_goal = friction_by_goal(analysis)
    owner = {f["turn"]: t for t, items in by_goal.items() for f in items if f["kind"] == "extra turn"}
    said = {}  # Feedback said in each turn, with the goal it counts on.
    for t, items in by_goal.items():
        for f in items:
            if f["kind"] == "feedback" and not f.get("in_note"):
                said.setdefault(f["turn"], []).append(_feedback(f, t))
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
        started = [{k: g[k] for k in ("summary", "kind", "entity_id", "state", "states", "brightness_pct", "reached")
                    if k in g} for g in top_level(goals) if g["turn"] == n and g["kind"] != "ask"]
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
        if n in said:
            turn["feedback"] = said[n]
        turns.append(turn)
    out = {"session": short(analysis["recording_id"]), "turns": turns, "goals_with_friction": sorted(by_goal)}
    note = [_feedback(f, t) for t, items in by_goal.items() for f in items if f.get("in_note")]
    if note:  # What the note it was sent in with asks for.
        out["feedback_in_note"] = note
    if analysis.get("triaged"):  # A quick check found it smooth, so the analyzer didn't extract its goals.
        out["not_analysed"] = TRIAGED_NOTE
    elif analysis.get("not_analysed"):
        out["not_analysed"] = UNANALYSED_NOTE
    # An earlier version kept no turn with its side effects (they count on the first goal): the recording says which.
    unknown = {s["entity_id"] for s in analysis.get("side_effects_left") or [] if not s.get("turn")}
    made = {s["entity_id"]: s["turn"] for s in side_effects_left(view, goals)} if unknown else {}
    left = [{**{k: f[k] for k in ("entity_id", "start", "end")},
             "changed_in_turn": made.get(f["entity_id"], f["turn"]) if f["entity_id"] in unknown else f["turn"],
             "for_goal_at_turn": t} for t, items in by_goal.items() for f in items if f["kind"] == "side effect"]
    if left:
        out["side_effects_left"] = sorted(left, key=lambda f: f["changed_in_turn"])
    return out


def _feedback(item, goal_turn):
    """A piece of feedback as the proposer and reviewer read it: what, and what it's about."""
    about = item.get("about_goal_at_turn")
    return {"what": item["what"], **({"about_goal_at_turn": about} if about is not None
                                     else {"general": True, "goal_turn": goal_turn}),
            **({"about_improvement": item["improvement"]} if item.get("improvement") else {})}


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
