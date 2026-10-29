"""SPEC.md P0.1.1's measures, by code alone: how a conversation, or a replay of it, fulfilled its goals and with
what effort. Fulfilment is goals met; effort is turns per root goal and side effects left.

judge() gives every goal of the conversation (each analysis goal, the note's, each revision note's) a state, counts
the turns each root goal took, and lists the side effects left, from what the home did (a view: its start states and
each change, with the turn it happened in). easier() compares two judged replays: strictly better, on one measure
and worse on none (P0.1.1); went_as_it_went() checks that a replay without the change went the way the
conversation went (P0.1: easier than it was for the household).
"""

from datetime import datetime, timedelta
from types import SimpleNamespace

from .analyzer import ASSISTANT, reached, root_turn, side_effects_left

CONVERSATION = None  # As verdicts: the conversation as it went, whose feedback asks for something it didn't get.
MEASURES = ("goals met", "turns per root goal", "side effects left")


def goals_of(analysis, revisions=()):
    """Every goal of the conversation, keyed as the flow keys them: each analysis goal ("turn:i", labelled with its
    turn, or "note" for the note's turn), then each revision note's ("revision:i", labelled "revision:<round>").
    feedback: the texts a goal asks Assist to follow (the note's, a turn's feedback, a revision note), else None.
    revisions: [(round, revision record)] in order."""
    said = int(analysis.get("turns") or 0)
    by_turn = {}
    for item in analysis.get("feedback") or []:
        by_turn.setdefault(item.get("turn"), []).append(item.get("what") or "")
    out = []
    for i, goal in enumerate(analysis.get("goals") or []):
        note = said and goal.get("turn") == said + 1
        texts = by_turn.get(None) if note else by_turn.get(goal.get("turn")) if goal.get("kind") == "fact" else None
        out.append({**goal, "key": f"{goal.get('turn')}:{i}", "label": "note" if note else goal.get("turn"),
                    "feedback": list(texts) if texts else None})
    for number, revision in revisions:
        if goal := revision.get("goal"):
            out.append({**goal, "key": f"revision:{len(out)}", "label": f"revision:{number}",
                        "feedback": [revision.get("note") or goal.get("summary") or ""]})
    return out


def root_of(goals, goal):
    """The key of the root goal a goal hangs from (itself for a root)."""
    if goal.get("serves") is None:
        return goal["key"]
    turn = root_turn([g for g in goals if isinstance(g.get("turn"), int)], goal["serves"])
    return next((g["key"] for g in goals if g.get("turn") == turn and g.get("serves") is None), goal["key"])


def _windows(goals):
    """Each goal's window, as the analysis judges it: a root's runs to the next root on its device (its sub-goals'
    turns count for it), a sub-goal's to the next goal on its device; None to the end."""
    roots = [g for g in goals if g.get("serves") is None]
    out = {}
    for goal in goals:
        among = roots if goal.get("serves") is None else goals
        later = [g["turn"] for g in among if g is not goal and isinstance(g.get("turn"), int)
                 and isinstance(goal.get("turn"), int) and g.get("entity_id") == goal.get("entity_id")
                 and g["turn"] > goal["turn"]]
        out[goal["key"]] = min(later) if later else None
    return out


def _before(view, turn):
    """The view as it stood when a turn started."""
    return SimpleNamespace(source=view.source, changes={e: [c for c in cs if c["during_turn"] < turn]
                                                        for e, cs in view.changes.items()})


def _reached_by(view, goal, window, turn):
    return (goal.get("kind") in ("state", "sequence") and not goal.get("feedback")
            and reached(_before(view, turn), goal, window) is True)


def judge(goals, view, said, verdicts=CONVERSATION):
    """How the conversation went in this view: {goals: {key: met | unmet | indeterminate}, goals_met, counted: {root:
    [turn labels]}, turns_per_root_goal: {root: n}, side_effects_left}.

    A feedback goal is met when every text it asks for was followed (verdicts: {text: bool}); in the conversation as
    it went (CONVERSATION), unmet: it asked for what it didn't get. A thanks is met once said. A state or sequence goal
    is met when Assist reached it; one it missed is unmet, or indeterminate when its turn wasn't said. A fact or an ask
    can't be told from the home: indeterminate.

    A root goal took each turn of its tree that was said (the note and revision notes are never said: what they ask
    for is a goal of its own)."""
    said, windows = list(said), _windows(goals)
    states = {}
    for goal in goals:
        if goal.get("feedback"):
            states[goal["key"]] = ("unmet" if verdicts is CONVERSATION
                                   else "met" if all(verdicts.get(t) for t in goal["feedback"]) else "unmet")
        elif goal.get("kind") == "thanks":
            states[goal["key"]] = "met" if goal.get("turn") in said else "indeterminate"
        elif goal.get("kind") in ("state", "sequence"):
            met = reached(view, goal, windows[goal["key"]])
            states[goal["key"]] = "met" if met else "unmet" if goal.get("turn") in said else "indeterminate"
        else:
            states[goal["key"]] = "indeterminate"
    counted = {root["key"]: sorted({g["label"] for g in goals if root_of(goals, g) == root["key"]
                                    and g["label"] in said and isinstance(g["label"], int)})
               for root in goals if root.get("serves") is None}
    return {"goals": states, "goals_met": [k for k, s in states.items() if s == "met"], "counted": counted,
            "turns_per_root_goal": {k: len(v) for k, v in counted.items()},
            "side_effects_left": side_effects_left(view, [g for g in goals if g.get("kind") != "feedback"])}


def skippable(goals, view, turn):
    """Whether a replay with the change needn't say this recorded turn (DETAILS P6.4): it has goals, none a root or
    feedback, and each is met already, or its root is, by what the replay did before it. A thanks is never met before
    it's said."""
    here = [g for g in goals if g["label"] == turn]
    if not here or any(g.get("serves") is None or g.get("feedback") for g in here):
        return False
    windows, keyed = _windows(goals), {g["key"]: g for g in goals}
    return all(_reached_by(view, g, windows[g["key"]], turn)
               or _reached_by(view, keyed[root_of(goals, g)], windows[root_of(goals, g)], turn) for g in here)


def replay_view(turns, session_view, steps):
    """What the home did in a replay, as judge() reads it: from the first recorded turn's home, each step's changes
    (by Assist, in the step's turn) and its lights' brightness, with what people did to devices neither the recorded
    Assist nor the replay had touched by then. turns: the recording's turns; steps: the replay's, each {turn,
    transitions: [{entity_id, old, new}], brightness: {light: [old, new] on 0-255}}."""
    changes, touched = {}, {}
    for step in steps:
        number = step["turn"]
        at = datetime.fromisoformat(turns[number - 1]["source"]["at"])
        last = {}
        for n, move in enumerate(step.get("transitions") or [], 1):
            change = {"at": (at + timedelta(milliseconds=n)).isoformat(), "during_turn": number, "old": move["old"],
                      "new": move["new"], "by": "assistant"}
            changes.setdefault(move["entity_id"], []).append(change)
            last[move["entity_id"]] = change
            touched.setdefault(move["entity_id"], number)
        for light, (old, new) in (step.get("brightness") or {}).items():
            pct = [None if v is None else round(v * 100 / 255) for v in (old, new)]
            if light in last:
                last[light].setdefault("attributes_changed", {})["brightness_pct"] = pct
            else:
                state = (changes.get(light) or [{}])[-1].get("new") or \
                    ((turns[0]["source"].get("states") or {}).get(light) or {}).get("state")
                changes.setdefault(light, []).append({
                    "at": (at + timedelta(milliseconds=999)).isoformat(), "during_turn": number, "old": state,
                    "new": state, "by": "assistant", "attributes_changed": {"brightness_pct": pct}})
                touched.setdefault(light, number)
    assistant = {e for e, cs in session_view.changes.items() if any(c["by"] in ASSISTANT for c in cs)}
    for entity, recorded in session_view.changes.items():
        if entity in assistant:
            continue
        for change in recorded:
            if change["by"] not in ASSISTANT and change["during_turn"] < touched.get(entity, 10**9):
                changes.setdefault(entity, []).append(dict(change))
    return SimpleNamespace(source=turns[0]["source"],
                           changes={e: sorted(cs, key=lambda c: c["at"]) for e, cs in changes.items()})


def judged_of(goals, devices_of):
    """What a test judged, for its record: {key: {summary, turn, root, devices, feedback?}}. devices_of(goal): the
    devices a goal wants something from."""
    return {g["key"]: {"summary": g.get("summary"), "turn": g["label"], "root": root_of(goals, g),
                       "devices": sorted(devices_of(g)), **({"feedback": g["feedback"]} if g.get("feedback") else {})}
            for g in goals}


def _roots(replay):
    return set(replay["turns_per_root_goal"])


def _met(replay, keys):
    return {k for k in keys if replay["goals"].get(k) == "met"}


def _newly_met(after, before):
    return {k for k, s in after["goals"].items() if s == "met" and before["goals"].get(k) != "met"}


def worse(after, before):
    """The measures a replay is worse on than another: a goal met before that's unmet now, a root goal met both
    ways that takes more turns, more side effects left."""
    roots = _roots(before)
    both = _met(after, roots) & _met(before, roots)
    return sorted(m for m, bad in (
        ("goals met", any(s == "met" and after["goals"].get(k) == "unmet" for k, s in before["goals"].items())),
        ("turns per root goal", any(after["turns_per_root_goal"][r] > before["turns_per_root_goal"][r] for r in both)),
        ("side effects left", len(after["side_effects_left"]) > len(before["side_effects_left"]))) if bad)


def easier(after, before):
    """Whether a replay is strictly better than another (P0.1.1): better on one measure and worse on none. No order
    between the measures."""
    return bool(gains(after, before)) and not worse(after, before)


def gains(after, before):
    """The measures a replay is strictly better on than another, of MEASURES: a goal met now that wasn't before (a
    note's or other feedback's too: P0.3), a root goal met both ways in fewer turns, fewer side effects left."""
    roots = _roots(before)
    both = _met(after, roots) & _met(before, roots)
    return sorted(m for m, better in (
        ("goals met", bool(_newly_met(after, before))),
        ("turns per root goal", any(after["turns_per_root_goal"][r] < before["turns_per_root_goal"][r] for r in both)),
        ("side effects left", len(after["side_effects_left"]) < len(before["side_effects_left"]))) if better)


def went_as_it_went(before, conversation):
    """None when a replay without the change went as the conversation went (DETAILS P6.6): the same state for every
    goal, the same turns counted for each root goal, and side effects left on the same devices; else why not."""
    if before["goals"] != conversation["goals"]:
        differ = sorted(k for k in conversation["goals"] if before["goals"].get(k) != conversation["goals"][k])
        return f"Without the change, goals {', '.join(differ)} didn't end as they did in the conversation."
    if before["counted"] != conversation["counted"]:
        return "Without the change, the goals didn't take the turns they took in the conversation."
    if {s["entity_id"] for s in before["side_effects_left"]} != {s["entity_id"] for s in conversation["side_effects_left"]}:
        return "Without the change, the side effects left weren't those the conversation left."
    return None


def helped(before, after, judged):
    """The root goals a change helped: a goal of its tree met with it and not without, met in fewer turns, or owning a
    side effect that's gone. A side effect belongs to the root whose tree has a goal at its turn, else the latest root
    before."""
    roots = _roots(before)
    out = {judged[k]["root"] for k in _newly_met(after, before)} | {
        r for r in _met(after, roots) & _met(before, roots)
        if after["turns_per_root_goal"][r] < before["turns_per_root_goal"][r]}
    gone = {s["entity_id"] for s in before["side_effects_left"]} - {s["entity_id"] for s in after["side_effects_left"]}
    for entity in gone:
        turn = next(s["turn"] for s in before["side_effects_left"] if s["entity_id"] == entity)
        at = [g["root"] for g in judged.values() if g["turn"] == turn]
        earlier = sorted((g["turn"], k) for k, g in judged.items()
                         if k == g["root"] and isinstance(g["turn"], int) and g["turn"] <= turn)
        if owner := (at[0] if at else earlier[-1][1] if earlier else None):
            out.add(owner)
    return sorted(out)
