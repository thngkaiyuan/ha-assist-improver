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

from .analyzer import ASSISTANT, announced, destination, question_run, reached, root_turn, side_effects_left
from .cases import calls_in

CONVERSATION = None  # As conduct: the conversation as it went, where every reaction is open (the user reacted).
MEASURES = ("goals met", "turns per root goal", "side effects left")


def goals_of(analysis, revisions=()):
    """Every goal of the conversation, keyed as the flow keys them: each analysis goal ("turn:i", labelled with its
    turn, or "note" for the note's turn), then each revision note's ("revision:i", labelled "revision:<round>").
    revisions: [(round, revision record)] in order."""
    said = int(analysis.get("turns") or 0)
    out = []
    for i, goal in enumerate(analysis.get("goals") or []):
        note = said and goal.get("turn") == said + 1
        out.append({**goal, "key": f"{goal.get('turn')}:{i}", "label": "note" if note else goal.get("turn")})
    for number, revision in revisions:
        if goal := revision.get("goal"):
            out.append({**goal, "key": f"revision:{len(out)}", "label": f"revision:{number}"})
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
    return goal.get("kind") in ("state", "sequence") and reached(_before(view, turn), goal, window) is True


def _turns_of(goals, action):
    """An action's request's turns from its own on, over which it's judged (DETAILS P9.10.5)."""
    tree = {g["label"] for g in goals if root_of(goals, g) == root_of(goals, action)}
    return sorted(t for t in tree if isinstance(t, int) and t >= action["turn"])


def _done_by(goals, view, action, turn):
    """An action as a turn starts: whether its turns before it spoke it (analyzer.announced); None when that can't be
    told."""
    return announced(view, action, [t for t in _turns_of(goals, action) if t < turn])


def question_state(goal, turn, facts):
    """A question as a turn starts (turn None: as the replay ended): met when the latest reply of its run the judge
    answered for by then gives what the answer needs, unmet when it doesn't, indeterminate when none was (P9.10.4).
    facts: {fact root's key: [{turn, gives}]}."""
    answers = sorted((a for a in (facts or {}).get(goal["key"]) or [] if turn is None or a["turn"] < turn),
                     key=lambda a: a["turn"])
    return "indeterminate" if not answers else "met" if answers[-1]["gives"] else "unmet"


def told_by(goals, actions):
    """What's told from these actions' calls (keys): themselves, the steps towards them and the filings under their
    requests."""
    roots = {root_of(goals, g) for g in goals if g["key"] in actions}
    return set(actions) | {g["key"] for g in goals if g.get("kind") == "step" and any(
        w["key"] in actions for w in destination(goals, g))} | {
        g["key"] for g in goals if g.get("kind") == "wrap-up" and root_of(goals, g) in roots}


def run_of(goals, root):
    """The turns of a question's run said to Assist (analyzer.question_run): its own, then each of its steps', in
    order; one in the note isn't said."""
    said = {g["turn"] for g in goals if isinstance(g.get("label"), int)}
    return [t for t in question_run(goals, root) if t in said]


def under(goals, question):
    """The keys of what hangs on a question's answer: the question, its steps (run_of) and the filings under it."""
    return {question["key"]} | {g["key"] for g in goals if g.get("kind") == "step"
                                and (wants := destination(goals, g)) and len(wants) == 1 and wants[0] is question} | {
        g["key"] for g in goals if g.get("kind") == "wrap-up" and root_of(goals, g) == question["key"]}


def as_answered(goals):
    """Each judged question's answers in the conversation as it went (P9.10.4): the analysis judged the reply that
    ends its run, and each reply a step followed gave nothing (the step says so)."""
    out = {}
    for root in goals:
        if root.get("kind") == "fact" and root.get("serves") is None and root.get("reached") is not None:
            last = (root.get("answered") or {}).get("turn", root["turn"])
            out[root["key"]] = [{"turn": t, "gives": bool(root["reached"]) if t == last else False}
                                for t in run_of(goals, root) if t <= last]
    return out


def _step(goals, view, windows, step, turn, facts=None):
    """A step as a turn starts: met once what it wants was met (DETAILS P2.3.2), unmet while not; indeterminate when
    that can't be told (settings left open, a question no reply was judged on)."""
    wants = destination(goals, step)
    if len(wants) == 1 and wants[0].get("kind") == "fact":  # A question: as the judge found its run's replies.
        return question_state(wants[0], turn, facts)
    if not wants or any(g.get("kind") not in ("state", "sequence", "action") for g in wants):
        return "indeterminate"
    found = [_done_by(goals, view, g, turn) if g.get("kind") == "action" else
             _reached_by(view, g, windows[g["key"]], turn) for g in wants]
    return "met" if all(found) else "unmet" if False in found else "indeterminate"


def _open(goals, view, windows, goal, turn, states, aside, facts=None):
    """Whether a goal is still open as a turn starts, for saying a turn and for a wrap-up: a part or repair not reached
    yet, a step whose goal isn't met yet, an objection not judged closed (states: what a judge said; None: not yet),
    anything that can't be told, and a goal set aside. A thanks and a wrap-up never hold one open."""
    kind = goal.get("kind")
    if goal["key"] in aside:
        return True
    if kind in ("thanks", "wrap-up"):
        return False
    if kind in ("state", "sequence") and goal.get("serves") is not None:
        return not _reached_by(view, goal, windows[goal["key"]], turn)
    if kind == "action" and goal.get("serves") is not None:
        return _done_by(goals, view, goal, turn) is not True
    if kind == "step":
        return _step(goals, view, windows, goal, turn, facts) != "met"
    if kind == "conduct":
        return (states or {}).get(goal["key"]) != "met"
    return True


def _wrap_up(goals, view, windows, goal, states, aside, facts=None):
    """A wrap-up as its turn starts: met once its root goal was met and nothing else of the root's is open (an earlier
    sub-goal, or one in the note or a revision note); indeterminate when the root can't be told."""
    keyed, turn = {g["key"]: g for g in goals}, goal["turn"]
    root = keyed[root_of(goals, goal)]
    if root.get("kind") == "fact":
        if (answered := question_state(root, turn, facts)) == "indeterminate":
            return answered
        met = answered == "met"
    elif root.get("kind") == "action":
        if (met := _done_by(goals, view, root, turn)) is None:
            return "indeterminate"
    elif root.get("kind") not in ("state", "sequence"):
        return "indeterminate"
    else:
        met = _reached_by(view, root, windows[root["key"]], turn)
    others = [g for g in goals if g is not goal and g is not root and root_of(goals, g) == root["key"]
              and not (isinstance(g["label"], int) and g["label"] >= turn)]
    return "met" if met and not any(
        _open(goals, view, windows, g, turn, states, aside, facts) for g in others) else "unmet"


def wrap_up(goals, view, goal, conduct, aside=(), facts=None):
    """A wrap-up's state as its turn starts (_wrap_up), with what the judge said of the objections and questions by
    then."""
    return _wrap_up(goals, view, _windows(goals), goal, conduct, aside, facts)


def judge(goals, view, said, conduct=CONVERSATION, aside=(), facts=None):
    """How the conversation went in this view: {goals: {key: met | unmet | indeterminate}, goals_met, counted: {root:
    [turn labels]}, turns_per_root_goal: {root: n}, side_effects_left}.

    A conduct goal is as the judge found it (conduct: {key: met, unmet or indeterminate}; one not judged can't be
    told). A question (a fact root) is as the judge found the latest reply of its run (facts: {key: [{turn, gives}]};
    in the conversation, as the analysis judged it, as_answered). A thanks is met once said. A state or sequence goal
    is met when Assist reached it; one it missed is unmet, or indeterminate when its turn wasn't said. A step is met
    when what it wants was met before its turn; a wrap-up when its root goal was, with nothing else of the root's open.
    A fact asked beside a device request, or an ask, can't be told from the home: indeterminate. In the conversation
    as it went (CONVERSATION), every reaction that can be told is unmet: the user reacted. A goal set aside (aside:
    keys) can't be told either way: indeterminate.

    A root goal took each turn of its tree that was said (the note and revision notes are never said: what they ask
    for is a goal of its own)."""
    said, windows = list(said), _windows(goals)
    facts = as_answered(goals) if conduct is CONVERSATION and facts is None else facts
    states = {}
    for goal in goals:
        key, kind = goal["key"], goal.get("kind")
        if key in aside:
            states[key] = "indeterminate"
        elif kind == "conduct":
            states[key] = "unmet" if conduct is CONVERSATION else conduct.get(key, "indeterminate")
        elif kind == "thanks":
            states[key] = "met" if goal.get("turn") in said else "indeterminate"
        elif kind in ("state", "sequence"):
            met = reached(view, goal, windows[key])
            states[key] = "met" if met else "unmet" if goal.get("turn") in said else "indeterminate"
        elif kind == "step":
            states[key] = _step(goals, view, windows, goal, goal["turn"], facts)
        elif kind == "action":
            found = announced(view, goal, [t for t in _turns_of(goals, goal) if t in said])
            states[key] = "met" if found else "unmet" if found is False and goal.get("turn") in said else (
                "indeterminate")
        elif kind == "fact" and goal.get("serves") is None:
            states[key] = question_state(goal, None, facts)
        else:
            states[key] = "indeterminate"
    for goal in goals:
        if goal.get("kind") == "wrap-up" and goal["key"] not in aside:
            states[goal["key"]] = _wrap_up(goals, view, windows, goal, states, aside, facts)
    if conduct is CONVERSATION:
        states.update({g["key"]: "unmet" for g in goals
                       if g.get("kind") in ("step", "wrap-up") and states[g["key"]] != "indeterminate"})
    counted = {root["key"]: sorted({g["label"] for g in goals if root_of(goals, g) == root["key"]
                                    and g["label"] in said and isinstance(g["label"], int)})
               for root in goals if root.get("serves") is None}
    return {"goals": states, "goals_met": [k for k, s in states.items() if s == "met"], "counted": counted,
            "turns_per_root_goal": {k: len(v) for k, v in counted.items()},
            "home_couldnt": sorted(g["key"] for g in goals if g.get("serves") is None and g.get("home_couldnt")),
            "side_effects_left": side_effects_left(view, goals)}


def needed(goals, view, turn, aside=(), conduct=None, facts=None):
    """Whether a replay with the change says this recorded turn (DETAILS P6.4), by what it did before it: always one
    with a root goal, a thanks (it's met only by being said) or no goal at all; else only while a goal on it is open
    (_open): an objection while a reply so far shows it (conduct: the judge's states so far, the note's and revision
    notes' at a wrap-up's turn; one not judged is open), a step under a question while no reply of its run so far gave
    what it needs (facts: the judge's answers so far), a wrap-up while its root goal isn't met or anything else of the
    root's is open."""
    here = [g for g in goals if g["label"] == turn]
    if not here or any(g.get("serves") is None or g.get("kind") == "thanks" for g in here):
        return True
    windows = _windows(goals)
    for goal in here:
        if goal.get("kind") == "wrap-up":
            if goal["key"] in aside or _wrap_up(goals, view, windows, goal, conduct, aside, facts) != "met":
                return True
        elif _open(goals, view, windows, goal, turn, conduct, aside, facts):
            return True
    return False


def set_aside(before, conversation, goals):
    """The goals a replay without the change can't test (DETAILS P6.4, P6.6.2): a wrap-up or an objection it found
    met, or couldn't tell, though the conversation reacted, so the trouble didn't happen again. Set aside, it's open
    for saying turns and indeterminate in both replays."""
    return {g["key"] for g in goals if g.get("kind") in ("wrap-up", "conduct")
            and before["goals"].get(g["key"]) in ("met", "indeterminate")
            and conversation["goals"].get(g["key"]) == "unmet"}


def answered_otherwise(goals, facts):
    """The questions a replay without the change didn't answer as the conversation did (P6.6.2 for questions, the goal
    design 2026-10-29): its verdict differs at a reply of the root's run, or the conversation's couldn't be told. Set
    aside, with the steps and wrap-up under them. facts: the replay's answers, {key: [{turn, gives}]}."""
    went = as_answered(goals)
    return {k for k, answers in facts.items()
            if k not in went or any({a["turn"]: a["gives"] for a in answers}.get(t["turn"]) != t["gives"]
                                    for t in went[k])}


def replay_view(turns, session_view, steps):
    """What the home did in a replay, as judge() reads it: from the first recorded turn's home, each step's changes
    (by Assist, in the step's turn) and its lights' brightness, with what people did to devices neither the recorded
    Assist nor the replay had touched by then, and, on the devices the recorded Assist changed, what others moved
    between the turns it said, as the replay's sandbox applies it (simulation.moved_by_others); and what each step
    called and spoke (calls). turns: the recording's turns; steps: the replay's, each {turn, output (Assist's
    messages), spoken (the sandbox's log), transitions: [{entity_id, old, new}], brightness: {light: [old, new] on
    0-255}}."""
    from .simulation import moved_by_others
    assistant = {e for e, cs in session_view.changes.items() if any(c["by"] in ASSISTANT for c in cs)}
    changes, touched, said = {}, {}, 1
    for step in steps:
        number = step["turn"]
        at = datetime.fromisoformat(turns[number - 1]["source"]["at"])
        for turn in turns[said:number]:  # What others moved before this turn, each turn the replay passed.
            for entity, move in moved_by_others(session_view.recording, turns, turn).items():
                if entity in assistant and "state" in move:
                    was = (changes.get(entity) or [{}])[-1].get("new") or (
                        (turns[0]["source"].get("states") or {}).get(entity) or {}).get("state")
                    changes.setdefault(entity, []).append({
                        "at": turn["source"]["at"], "during_turn": turn["number"] - 1, "old": was,
                        "new": move["state"], "by": "someone else, between turns"})
        said = max(said, number)
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
    for entity, recorded in session_view.changes.items():
        if entity in assistant:
            continue
        for change in recorded:
            if change["by"] not in ASSISTANT and change["during_turn"] < touched.get(entity, 10**9):
                changes.setdefault(entity, []).append(dict(change))
    # What each turn called, and what it spoke wherever it was called from (a script's too, logged by the sandbox).
    calls = {step["turn"]: calls_in(step.get("output") or []) + step.get("spoken", []) for step in steps}
    return SimpleNamespace(source=turns[0]["source"], calls=calls, scripts_seen=True,
                           changes={e: sorted(cs, key=lambda c: c["at"]) for e, cs in changes.items()})


def judged_of(goals, devices_of):
    """What a test judged, for its record: {key: {summary, turn, root, devices, kind, home_couldnt?, and for a conduct
    goal quote and about}}. devices_of(goal): the devices a goal wants something from."""
    return {g["key"]: {"summary": g.get("summary"), "turn": g["label"], "root": root_of(goals, g),
                       "devices": sorted(devices_of(g)), "kind": g.get("kind"),
                       **({"home_couldnt": True} if g.get("home_couldnt") else {}),
                       **({"quote": g.get("quote"), "about": g.get("about")} if g.get("kind") == "conduct" else {})}
            for g in goals}


def _roots(replay):
    return set(replay["turns_per_root_goal"])


def compared(after, before):
    """The root goals whose turns compare (DETAILS P6.1.5, P6.1.6): those met in both replays, and those unmet in both
    because the home couldn't reach them (home_couldnt: their device was unavailable as it was asked for). One the home
    could reach, unmet both ways, isn't: a change that gives up on it saves turns by abandoning the request. One met
    before and not now is worse on goals met, whatever its turns."""
    couldnt = set(before.get("home_couldnt") or ())
    return {r for r in _roots(before) if before["goals"].get(r) == after["goals"].get(r)
            and (before["goals"].get(r) == "met" or before["goals"].get(r) == "unmet" and r in couldnt)}


def _newly_met(after, before):
    return {k for k, s in after["goals"].items() if s == "met" and before["goals"].get(k) != "met"}


def worse(after, before):
    """The measures a replay is worse on than another: a goal met before that's unmet now, a root goal whose turns
    compare (compared) that takes more turns, more side effects left."""
    alike = compared(after, before)
    return sorted(m for m, bad in (
        ("goals met", any(s == "met" and after["goals"].get(k) == "unmet" for k, s in before["goals"].items())),
        ("turns per root goal", any(after["turns_per_root_goal"][r] > before["turns_per_root_goal"][r] for r in alike)),
        ("side effects left", len(after["side_effects_left"]) > len(before["side_effects_left"]))) if bad)


def easier(after, before):
    """Whether a replay is strictly better than another (P0.1.1): better on one measure and worse on none. No order
    between the measures."""
    return bool(gains(after, before)) and not worse(after, before)


def gains(after, before):
    """The measures a replay is strictly better on than another, of MEASURES: a goal met now that wasn't before (a
    note's or other feedback's too: P0.3), a root goal whose turns compare (compared: met both ways, or unmet both ways
    because the home couldn't reach it) in fewer turns, fewer side effects left."""
    alike = compared(after, before)
    return sorted(m for m, better in (
        ("goals met", bool(_newly_met(after, before))),
        ("turns per root goal", any(after["turns_per_root_goal"][r] < before["turns_per_root_goal"][r] for r in alike)),
        ("side effects left", len(after["side_effects_left"]) < len(before["side_effects_left"]))) if better)


def went_as_it_went(before, conversation, leave_out=()):
    """None when a replay without the change went as the conversation went (DETAILS P6.6): the same state for every
    goal, the same turns counted for each root goal, and side effects left on the same devices; else why not.
    leave_out: keys of goals not compared (those a model judges, before it has)."""
    differ = sorted(k for k in conversation["goals"]
                    if k not in leave_out and before["goals"].get(k) != conversation["goals"][k])
    if differ:
        return f"Without the change, goals {', '.join(differ)} didn't end as they did in the conversation."
    if before["counted"] != conversation["counted"]:
        return "Without the change, the goals didn't take the turns they took in the conversation."
    if {s["entity_id"] for s in before["side_effects_left"]} != {s["entity_id"] for s in conversation["side_effects_left"]}:
        return "Without the change, the side effects left weren't those the conversation left."
    return None


def helped(before, after, judged):
    """The root goals a change helped: a goal of its tree met with it and not without, one whose turns compare
    (compared) in fewer turns, or owning a side effect that's gone. A side effect belongs to the root whose tree has a
    goal at its turn, else the latest root before."""
    out = {judged[k]["root"] for k in _newly_met(after, before)} | {
        r for r in compared(after, before) if after["turns_per_root_goal"][r] < before["turns_per_root_goal"][r]}
    gone = {s["entity_id"] for s in before["side_effects_left"]} - {s["entity_id"] for s in after["side_effects_left"]}
    for entity in gone:
        turn = next(s["turn"] for s in before["side_effects_left"] if s["entity_id"] == entity)
        at = [g["root"] for g in judged.values() if g["turn"] == turn]
        earlier = sorted((g["turn"], k) for k, g in judged.items()
                         if k == g["root"] and isinstance(g["turn"], int) and g["turn"] <= turn)
        if owner := (at[0] if at else earlier[-1][1] if earlier else None):
            out.add(owner)
    return sorted(out)
