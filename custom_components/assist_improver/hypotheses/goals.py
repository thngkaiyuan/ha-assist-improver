"""What a session's analysis says, read the ways the roles need it: its friction by goal, the devices a goal names,
the session as a script, and what Assist can do with a device."""

from ..analyzer import top_level

def short(recording_id):
    return recording_id[:8]


TRIAGED_NOTE = ("A quick check found every request here fulfilled smoothly, so its goals weren't extracted; read its "
                "turns for what was asked and what Assist did.")
UNANALYSED_NOTE = ("Not analysed, so its goals and friction weren't extracted; read its turns for what was asked and "
                   "what Assist did.")


def friction(analysis):
    """The turns with friction in one analysed session: extra turns (with the sub-goals they served) and asks."""
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
    turn belongs to the goal its sub-goal serves, or else to the latest goal before it; an ask is its own."""
    tops = sorted({g["turn"] for g in top_level(analysis.get("goals", []))})
    groups = {}
    for item in friction(analysis):
        if item["kind"] == "ask":
            owner = item["turn"]
        else:
            served = [s["for_goal_at_turn"] for s in item["served"]]
            earlier = [t for t in tops if t < item["turn"]]
            owner = served[0] if served else (earlier[-1] if earlier else item["turn"])
        groups.setdefault(owner, []).append(item)
    return groups


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
    owner = {f["turn"]: t for t, items in friction_by_goal(analysis).items() for f in items if f["kind"] != "ask"}
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
        turns.append(turn)
    out = {"session": short(analysis["recording_id"]), "turns": turns,
           "goals_with_friction": sorted(friction_by_goal(analysis))}
    if analysis.get("triaged"):  # A quick check found it smooth, so the analyzer didn't extract its goals.
        out["not_analysed"] = TRIAGED_NOTE
    elif analysis.get("not_analysed"):
        out["not_analysed"] = UNANALYSED_NOTE
    if analysis.get("side_effects_left"):
        out["side_effects_left"] = analysis["side_effects_left"]
    return out


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
