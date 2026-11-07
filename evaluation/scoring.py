"""Score journey-analyzer output against hand-made labels; deterministic, no model.

Both sides use the goals shape in LABELS.md. Each labelled device goal (a root,
a device asked for with it, or a repair) must appear with the same turn, device
and value, whichever of a turn's devices the answer makes its root; a turn where
a labelled goal starts must have a goal of its own there (never only a sub-goal
of an earlier one); no device goal may be invented; labelled not-requests must be
recognised, and the derived extra turns must match. Each labelled reaction (a
step, conduct, wrap-up or thanks) must appear as one of its kind at its turn under
the same root goal, a conduct goal about the same Assist turn (or one the label
also accepts); whether it quotes the labelled span of the household's words, word
for word, is a soft finding, since any span of them will do. Fact goals need a
human or separate judge, so only their turn is checked. Every check is reported
separately so a failure says exactly what differed.

Where the user's words left settings open, the label has an ask goal: the
controls that must be left open for them (must_include), the controls an ask may
cover (allowed) and what they meant (intended). These are judgement calls, so
they give soft findings for a person to read, never a pass or fail.
"""

import re

from custom_components.assist_improver.analyzer import CUT, REACTIONS, extra_turns, root_turn, top_level
from custom_components.assist_improver.cases import CaseError
from custom_components.assist_improver.targets import TOLERANCE, target


def _same_value(expected, actual):
    if expected["kind"] != actual["kind"]:
        return False
    if expected["kind"] == "sequence":
        goal, got = expected["states"], actual.get("states") or []
        # A sequence may also be written from the state before the first change (on, off, on for off, on).
        return got == goal or (len(got) == len(goal) + 1 and got[1:] == goal and got[0] != goal[0])
    if expected["kind"] == "state":
        if actual.get("state") not in [expected["state"], *expected.get("or_state", [])]:
            return False
        # Its settings, as targets reads a goal's (a label's brightness_pct among them): a brightness within tolerance.
        wanted, got = target(expected)[1], target(actual)[1]
        return all(isinstance(got.get(k), (int, float)) and abs(got[k] - v) <= TOLERANCE * 100
                   if k == "brightness_pct" else got.get(k) == v for k, v in wanted.items())
    return True  # Facts: only the turn is checked.


def _key(goal):
    return (goal["turn"], goal.get("entity_id") or f"fact:{goal.get('fact', '')}")


def score_recording(predicted, label, turn_count=None):
    """turn_count: the session's turns, for the extra-turns check (defaults to the highest turn either names)."""
    if predicted.get("recording_id") != label.get("recording_id"):
        raise CaseError("Prediction and label are for different recordings")
    checks = []

    def check(name, passed, detail=None):
        checks.append({"check": name, "passed": bool(passed), **({"detail": detail} if detail else {})})

    every = predicted.get("goals", []) + predicted.get("sub_goals", [])  # A label read as an answer has both.
    got = top_level(every)
    # Its device goals, whichever it makes a turn's root: the root, a device asked for with it, a repair.
    devices = {(w["turn"], w["entity_id"]): w for w in every
               if w.get("entity_id") and w["kind"] in ("state", "sequence")}
    used, spoken = set(), set()  # The device goals and actions a labelled goal took.
    findings = []
    asks = [g for g in label.get("goals", []) if g["kind"] == "ask"]
    for ask in asks:
        findings += _ask_findings(ask, every, predicted.get("question_for_user"))
    open_to_user = {e for a in asks for e in a.get("allowed", [])}
    for turn in sorted({g["turn"] for g in label.get("goals", [])}):  # A labelled fact may be the answer's thanks.
        thanked = all(g["kind"] == "fact" for g in label["goals"] if g["turn"] == turn) and any(
            w["turn"] == turn and w["kind"] == "thanks" for w in every)
        check(f"turn {turn}: a goal starts", thanked or any(w["turn"] == turn for w in got))
    labelled = label.get("goals", []) + [g for g in label.get("sub_goals", [])
                                         if g.get("kind") in ("state", "sequence", "action")]
    for goal in labelled:
        if goal["kind"] == "ask":
            continue
        if goal.get("any_of"):  # The user would accept the goal on any of these devices.
            name = f"turn {goal['turn']}: any of {', '.join(goal['any_of'])} {goal.get('state') or goal.get('states')}"
            hits = [(goal["turn"], e) for e in goal["any_of"] if (goal["turn"], e) in devices
                    and _same_value(goal, devices[(goal["turn"], e)])]
            used.update(hits)
            check(name, bool(hits), None if hits else {"expected": goal, "predicted": [w for w in got if w["turn"] == goal["turn"]]})
            continue
        name = f"turn {goal['turn']}: {goal.get('entity_id') or 'fact'} {goal.get('states') or goal.get('state') or ''}".rstrip()
        if goal["kind"] == "action":  # By its outcome and the devices it's for, which decide it's met.
            done = [w for w in every if w["turn"] == goal["turn"] and w["kind"] == "action"
                    and w.get("outcome") == goal.get("outcome")]
            hit = next((w for w in done if set(w.get("entity_ids") or []) == set(goal.get("entity_ids") or [])
                        and id(w) not in spoken), None)
            spoken.add(id(hit))
            check(f"turn {goal['turn']}: {goal.get('outcome')} on {', '.join(sorted(goal.get('entity_ids') or []))}",
                  hit, None if hit else {"expected": goal, "predicted": done})
            continue
        if goal["kind"] == "fact":  # A thanks there will do (always a sub-goal).
            ok = any(w["turn"] == goal["turn"] for w in got if w["kind"] == "fact") or any(
                w["turn"] == goal["turn"] for w in every if w["kind"] == "thanks")
            check(f"{name} (fact; turn only)", ok)
            facts = [w for w in every if w["turn"] == goal["turn"] and w["kind"] == "fact"]
            named = {e for w in facts for e in w.get("entity_ids") or []}
            if "entity_ids" in goal and facts and named != set(goal["entity_ids"]):  # Soft: the devices it's about.
                findings.append(f"turn {goal['turn']}: the fact names {sorted(named)}; the label's devices are "
                                f"{sorted(goal['entity_ids'])}")
            continue
        actual = devices.get((goal["turn"], goal["entity_id"]))
        if actual is not None:
            used.add((goal["turn"], goal["entity_id"]))
        check(name, actual is not None and _same_value(goal, actual),
              {"expected": goal, "predicted": actual or [w for w in got if w.get("entity_id") == goal["entity_id"]]})
    optional = {(w["turn"], w["entity_id"]): w for w in label.get("optional_goals", [])}
    question = predicted.get("question_for_user")
    count = turn_count or max([w["turn"] for w in every + label.get("goals", [])]
                              + [n["turn"] for n in predicted.get("not_requests", []) + label.get("not_requests", [])]
                              + [0])
    have_extra = extra_turns(count, every, predicted.get("not_requests", []), question)
    # A turn where the label accepts any reading that leaves it not extra (it can't lead to a fix).
    free = {n["turn"] for n in label.get("not_requests", []) if n.get("any_without_interventions")} - set(have_extra)
    invented = []
    for key, goal in devices.items():
        if key in used or key[0] in free or key[1] in open_to_user:  # Guesses on open settings are findings.
            continue
        if key in optional and _same_value(optional[key], goal):
            continue
        invented.append(goal)
    invented += [w for w in every if w["kind"] == "action" and id(w) not in spoken and w["turn"] not in free]
    check("no invented goals", not invented, {"invented": invented} if invented else None)

    predicted_not = {n["turn"] for n in predicted.get("not_requests", [])}
    for item in label.get("not_requests", []):
        turn = item["turn"]
        ok = turn in predicted_not
        no_device_goal = not any(w["turn"] == turn and w.get("entity_id") for w in got)
        if not ok and item.get("also_acceptable"):
            # Some labels also accept the turn read as a fact goal (such as "check the assistant hears me").
            ok = no_device_goal and any(w["turn"] == turn and w["kind"] == "fact" for w in got)
        if not ok and item.get("question_acceptable"):
            # Or a question back to the user, where the session alone can't show what the turn was.
            ok = no_device_goal and bool(question)
        if not ok and item.get("any_without_interventions"):
            ok = turn in free
        check(f"turn {turn}: not a request ({item['what']})", ok)

    matched = set()  # The answer's reactions already matched to a labelled one: each stands for one.
    for sub in label.get("sub_goals", []):
        if sub.get("kind") in REACTIONS:
            findings += _sub_goal(sub, label, every, check, matched)
    want_extra = extra_turns(count, label.get("goals", []) + label.get("sub_goals", []),
                             label.get("not_requests", []))
    check("extra turns", want_extra == have_extra, {"expected": want_extra, "predicted": have_extra})
    return {"recording_id": label["recording_id"], "passed": all(c["passed"] for c in checks), "checks": checks,
            "findings": findings}


def _sub_goal(sub, label, every, check, matched):
    """Check one labelled reaction: the answer has one of its kind at its turn, under the same root goal, and for a
    conduct goal about the same Assist turn; a label's about may list the turns it accepts (the household's words
    don't pick one), each under that turn's own root goal. Each of the answer's reactions stands for one labelled
    one (matched: their ids), the one quoting the label's words first. Soft finding: a conduct goal that quotes another
    span of their words."""
    labelled = label.get("goals", []) + label.get("sub_goals", [])
    abouts = sub.get("about") if isinstance(sub.get("about"), list) else [sub.get("about")]
    root = root_turn(labelled, sub.get("serves"))
    about = f", about {' or '.join(map(str, abouts))}" if sub.get("kind") == "conduct" else ""
    name = f"turn {sub['turn']}: {sub.get('kind')} under the goal at turn {root}{about}"

    def fits(g):
        if g["turn"] != sub["turn"] or g.get("serves") is None or g["kind"] != sub.get("kind") or id(g) in matched:
            return False
        if sub.get("kind") != "conduct":
            return root_turn(every, g["serves"]) == root
        about = g.get("about")[0] if isinstance(g.get("about"), list) else g.get("about")  # A label read as one.
        if about not in abouts:
            return False
        under = root if len(abouts) == 1 or not isinstance(about, int) else root_turn(labelled, about)
        return root_turn(every, g["serves"]) == under
    fitting = [g for g in every if fits(g)]
    found = next((g for g in fitting if _words(g.get("quote")) == _words(sub.get("quote"))), None) or next(
        iter(fitting), None)
    check(name, found, None if found else {"expected": sub,
                                           "predicted": [g for g in every if g["turn"] == sub["turn"]]})
    if found is None:
        return []
    matched.add(id(found))
    if sub.get("kind") == "conduct" and _words(found.get("quote")) != _words(sub.get("quote")):
        return [f"turn {sub['turn']}: quoted {found.get('quote')!r}; the label's span is {sub.get('quote')!r}"]
    return []


def _words(quote):
    """A quote's words, as analyzer.quoted compares them: case, punctuation and the kind of apostrophe aside."""
    return [re.findall(r"\w+(?:'\w+)*", part.casefold().replace("\u2019", "'").replace("\u2018", "'"))
            for part in CUT.split(quote or "") if part.strip()]


def _ask_findings(ask, goals, question):
    """Soft findings for one labelled ask: too narrow, too broad, guessed, or a free-text question instead."""
    turn, must, allowed = ask["turn"], set(ask.get("must_include", [])), set(ask.get("allowed", []))
    intended = {i["entity_id"]: i.get("state") for i in ask.get("intended", [])}
    asked = {e for g in goals if g["kind"] == "ask" and g["turn"] == turn for e in g.get("entity_ids") or []}
    guessed = {g["entity_id"]: g.get("state") or g.get("states") for g in goals
               if g.get("entity_id") in must | allowed and g["kind"] != "ask"}
    found = []
    if not asked and question:
        found.append(f"turn {turn}: a free-text question instead of an ask: {question!r}")
    for entity in sorted(must - asked):
        if entity in guessed:
            right = guessed[entity] == intended.get(entity)
            found.append(f"turn {turn}: guessed {entity} = {guessed[entity]!r}"
                         + (" (what the user meant, but unstated)" if right else f"; the user meant {intended.get(entity)!r}"))
        else:
            found.append(f"turn {turn}: ask too narrow: {entity} not left open (the user meant {intended.get(entity)!r})")
    broad = sorted(asked - allowed) if allowed else []
    if broad:
        found.append(f"turn {turn}: ask too broad: also {', '.join(broad)}")
    if asked and not found:
        found.append(f"turn {turn}: asked about {', '.join(sorted(asked))}, as expected")
    return found


def score_set(predictions, labels, turn_counts=None):
    """predictions and labels keyed by recording ID; a missing prediction fails."""
    reports = []
    for recording_id, label in sorted(labels.items()):
        if recording_id not in predictions:
            reports.append({"recording_id": recording_id, "passed": False,
                            "checks": [{"check": "prediction present", "passed": False}]})
            continue
        reports.append(score_recording(predictions[recording_id], label, (turn_counts or {}).get(recording_id)))
    checks = [c for r in reports for c in r["checks"]]
    return {"recordings": len(reports), "recordings_passed": sum(r["passed"] for r in reports),
            "checks": len(checks), "checks_passed": sum(c["passed"] for c in checks),
            "findings": sum(len(r.get("findings", [])) for r in reports), "reports": reports}
