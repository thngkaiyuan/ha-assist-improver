"""From the generator's hypotheses to tested candidates; code runs every step, models only where judgement is needed.

For each hypothesis and each goal it names (up to the tests-per-goal setting per goal,
over the goal's lifetime): build the goal's case from the analysis, replay the recorded
conversation as the baseline (reproduced when its effects on the home match the
recording), then replay with the hypothesis's changes, the user's recorded
messages in order, until the goal is met. A small model checks that each
replayed follow-up still fits the replies around it, and, for a goal the
household gave feedback on, that the replies follow it where the baseline's
didn't; code checks the side effects left before are gone, once the baseline
showed them again.
"""

from copy import deepcopy

from homeassistant.components import conversation
import probatio

from .analyzer import top_level
from .cases import CaseError, digest
from .candidates import CandidateTrial, prepare_change_candidate
from .const import CONF_TESTS_PER_GOAL, DEFAULT_TESTS_PER_GOAL, NO_FIX
from .failures import aside, closing, contained
from .hypotheses import feedback_goal, friction_by_goal
from .reflective import counts_as_tried, run_role
from .settings import role_model
from .snapshot import live_definitions
from .trace import ids, span, tag

CHECK_ROLE = "followup_check"
CHECK_INSTRUCTIONS = """A conversation with a home assistant was replayed with a change to the assistant. The user's messages after the first were replayed exactly as the user originally sent them, so some may no longer fit. For each follow-up below you see the assistant's reply before it, the user's message, and the assistant's reply after it. Say whether the message still broadly makes sense after the reply before it (fits), with a short reason. A message that answers or reacts to something the assistant didn't say does not fit."""
CHECK_STRUCTURE = probatio.Schema({probatio.Required("verdicts"): [{
    probatio.Required("turn"): int, probatio.Required("fits"): bool, probatio.Required("why"): str}]})
# The follow-up check's sibling: whether a replay with a change follows what the household said about how Assist
# behaved. The same small model; it judges only the replies.
FEEDBACK_ROLE = "feedback_check"
FEEDBACK_INSTRUCTIONS = """A household told their home assistant how it should behave (feedback, below). A change to the assistant was then tested by replaying the same conversation: you see what the user said in each turn and what the assistant replied with the change. For each piece of feedback, say whether the replies follow it (followed), with a short reason. Judge only what the feedback asks for, from the replies' words; whether the devices did the right thing is checked separately. When a reply does what the feedback asked against, or the feedback asks for something the replies don't show, it is not followed."""
FEEDBACK_STRUCTURE = probatio.Schema({probatio.Required("verdicts"): [{
    probatio.Required("feedback"): str, probatio.Required("followed"): bool, probatio.Required("why"): str}]})


def targets(hypothesis, analyses):
    """Each goal the hypothesis names that a replay can judge by its effects on the home: its session, turns and
    destination, with the side effects it must not leave (unwanted: {device: its start}; its case runs through the
    turn that made each, cases.create_case) and the feedback its replies must follow. General feedback, friction on
    its own, replays the goal before it. Returns (testable goals, reasons the others can't be judged)."""
    found, skipped = [], []
    if not hypothesis.get("goals"):
        return found, ["The hypothesis names no goals."]
    for goal_ref in hypothesis["goals"]:
        name = f"{goal_ref['session'][:8]}:{goal_ref['goal_turn']}"
        analysis = analyses.get(goal_ref["session"][:8])
        if analysis is None:
            skipped.append(f"{name}: no analysis for this session.")
            continue
        items = friction_by_goal(analysis).get(goal_ref["goal_turn"], [])
        unwanted = {f["entity_id"]: f["start"] for f in items if f["kind"] == "side effect"}
        feedback = list(dict.fromkeys(f["what"] for f in items if f["kind"] == "feedback"))
        turn = feedback_goal(analysis, goal_ref["goal_turn"]) if feedback else goal_ref["goal_turn"]
        if turn is None:
            skipped.append(f"{name}: no goal before this feedback to replay.")
            continue
        goals = [g for g in top_level(analysis.get("goals", [])) if g["turn"] == turn]
        sequences = {g["entity_id"]: g["states"] for g in goals if g["kind"] == "sequence"}
        states = {g["entity_id"]: g["state"] for g in goals if g["kind"] == "state"}
        for ask in (g for g in goals if g["kind"] == "ask"):  # What the user chose for settings they left open.
            states.update({c["entity_id"]: c["state"] for c in ask.get("user_chose", [])})
        if sequences and states or not (sequences or states):
            skipped.append(f"{name}: its goals mix sequences and states, or none is a device state.")
            continue
        if sequences and any(set(s) - {"on", "off"} for s in sequences.values()):
            skipped.append(f"{name}: only on/off sequences can be judged by their effects yet.")
            continue
        extra = sorted(f["turn"] for f in friction_by_goal(analysis).get(turn, []) if f["kind"] == "extra turn")
        # Unmet: a goal never reached, or settings the user left open that Assist guessed or missed.
        unmet = any(g["kind"] == "ask" or g.get("reached") is False for g in goals)
        # Controls the user would accept being set (an ask's allowed controls, or the ones their words left open).
        acceptable = sorted({e for g in goals if g["kind"] == "ask" for e in (g.get("allowed") or g.get("entity_ids") or [])})
        if not extra and not unmet and not unwanted and not feedback:
            skipped.append(f"{name}: no friction to fix (no extra turns, the goal was met, no side effect left, no "
                           "feedback).")
            continue
        # General feedback replays the goal before it, through that goal's turns, so the goal is reached; that goal's
        # extra turns or unmet settings aren't this one's friction (its own test counts them), so it's judged on its
        # own: the feedback, and any side effect on it.
        own = turn == goal_ref["goal_turn"]
        borrowed = not own
        found.append({"recording_id": analysis["recording_id"], "session": goal_ref["session"][:8], "goal": name,
                      "goal_turn": goal_ref["goal_turn"], "first_turn": turn, "last_turn": max([turn, *extra]),
                      "intervention_turns": [] if borrowed else extra,
                      "friction": (0 if borrowed else len(extra) + unmet) + len(unwanted) + len(feedback),
                      "unmet": unmet and own, "acceptable": acceptable, "unwanted": unwanted, "feedback": feedback,
                      "summary": goals[0]["summary"],
                      "criteria": ({"kind": "transition_sequence", "sequences": sequences} if sequences
                                   else {"kind": "state", "states": states})})
    return sorted(found, key=lambda f: -f["friction"]), skipped


async def run_baseline(manager, case):
    """Replay the recorded conversation; reproduced when every turn's effects match the recording. A baseline
    settled under the current plan is reused; one settled under an older plan is run again under the new one."""
    plan = (await manager.prepare(case["id"]))["plan"]
    if case["baseline"].get("plan", {}).get("id") == plan["id"] and case["baseline"]["status"] != "not_run":
        return case
    return await manager.run(case["id"], plan_id=plan["id"], allow_paid_inference=True)


async def check_followups(hass, entity_id, followups):
    """The small model's verdict on each replayed follow-up; None when no check model is set."""
    if not followups or not entity_id:
        return None
    record = await run_role(hass, role=CHECK_ROLE, entity_id=entity_id, instructions=CHECK_INSTRUCTIONS,
                            payload={"followups": followups}, structure=CHECK_STRUCTURE, allow_paid_inference=True)
    verdicts = (record.get("output") or {}).get("verdicts", [])
    return {"status": record["status"], "verdicts": verdicts, "error": record.get("error"),
            "all_fit": record["status"] == "completed" and all(v["fits"] for v in verdicts), "usage": record["usage"]}


async def check_feedback(hass, entity_id, feedback, turns):
    """The small model's verdict on whether a replay's replies follow each piece of the household's feedback:
    {status, verdicts, followed (all of it), error, usage}. Without a model it can't say (status not_run). turns:
    [{turn, input, reply}]."""
    if not entity_id:
        return {"status": "not_run", "verdicts": [], "followed": None, "error": {"message": "No model to check with"}}
    record = await run_role(hass, role=FEEDBACK_ROLE, entity_id=entity_id, instructions=FEEDBACK_INSTRUCTIONS,
                            payload={"feedback": list(feedback),
                                     "replay": [{"turn": t["turn"], "user": t["input"], "assistant": t["reply"]}
                                                for t in turns]},
                            structure=FEEDBACK_STRUCTURE, allow_paid_inference=True)
    verdicts = (record.get("output") or {}).get("verdicts", [])
    judged = record["status"] == "completed" and len(verdicts) >= len(feedback)
    return {"status": "completed" if judged else "failed", "verdicts": verdicts, "error": record.get("error"),
            "followed": judged and all(v["followed"] for v in verdicts), "usage": record["usage"]}


async def check_baseline_feedback(hass, entry, casebook, case):
    """The baseline's replies checked against the household's feedback, once per baseline and kept with it (the
    feedback it judged, so a case whose feedback changed is checked again): a fix is judged on feedback only where
    the replies without it didn't follow it. A check that couldn't run is not kept. Returns the case."""
    baseline, feedback = case["baseline"], case.get("follow_feedback") or []
    kept = baseline.get("feedback_check") or {}
    if not feedback or (kept.get("status") == "completed" and kept.get("feedback") == feedback):
        return case
    replies = [{"turn": s["turn"], "input": s["input"],
                "reply": next((m["content"] for m in reversed(s.get("output") or [])
                               if m.get("role") == "assistant" and m.get("content")), "")}
               for s in baseline.get("steps", [])]
    heard = await check_feedback(hass, role_model(entry, "followup_check") or role_model(entry, "editor"), feedback,
                                 replies)
    if heard["status"] != "completed":
        return {**case, "baseline": {**baseline, "feedback_check": {**heard, "feedback": list(feedback)}}}
    async with casebook.lock:
        stored = casebook.get(case["id"])
        if stored["baseline"].get("plan", {}).get("id") != baseline.get("plan", {}).get("id"):
            raise CaseError("The baseline changed while its replies were checked")
        stored["baseline"]["feedback_check"] = {**heard, "feedback": list(feedback)}
        return await casebook._save(stored)


async def test_hypotheses(hass, entry, runtime, *, hypotheses, analyses, per_goal=None, skip_goals=(), earlier=None,
                          allow_paid_inference=False):
    """Test each hypothesis on every goal instance it names that a replay can judge, most friction first. A goal
    instance is a goal as it came up in one conversation (session and turn): the same goal phrased differently must be
    fixed in each. No instance gets more than `per_goal` tests with a fix in its lifetime (the tests-per-goal setting
    by default): earlier, {goal: tests it had in earlier runs}, counts too. Only a test that says something about the
    goal counts there (reflective.TRIED): its fix judged, or it can't be replayed; a failure of the provider, the
    sandbox or the code doesn't. Its baseline runs once. skip_goals: {goal: why} of goals that are settled (an
    improvement, or no fix found). A test that fails unexpectedly fails alone, kept in the run's problems; as such a
    failure doesn't use up the goal's tests, a goal whose tests failed run after run is set aside (failures.py)."""
    if not allow_paid_inference:
        raise CaseError("Paid inference has not been authorized")
    from homeassistant.util.dt import utcnow
    started = utcnow().isoformat()
    per_goal = int(per_goal or entry.options.get(CONF_TESTS_PER_GOAL, DEFAULT_TESTS_PER_GOAL))
    by_session = {a["recording_id"][:8]: a for a in analyses}
    planned = []
    for hypothesis in hypotheses:  # One the goals can't be read from is left out (a problem); the others go on.
        async with contained("test", _named(hypothesis), step="planning", setback=False) as one:
            planned.append((hypothesis, *targets(hypothesis, by_session)))
        if one.failed:
            planned.append((hypothesis, [], [f"Its goals couldn't be read: {one.error}"]))
    planned.sort(key=lambda p: -sum(f["friction"] for f in p[1]))
    results, used, tried, earlier = [], {}, {}, dict(earlier or {})
    for hypothesis, found, skipped in planned:
        tests = []
        async with span("test.hypothesis", hypothesis=hypothesis.get("title"), goals=len(found)):
            for goal in found:
                if goal["goal"] in skip_goals:  # An improvement (waiting, applied or decided), or no fix found.
                    tests.append({"goal": goal["goal"], "status": "not_tested",
                                  "why": NO_FIX_WHY if _no_fix(skip_goals, goal["goal"])
                                  else "This goal already has an improvement."})
                    continue
                if used.get(goal["goal"], 0) + earlier.get(goal["goal"], 0) >= per_goal:
                    tests.append({"goal": goal["goal"], "status": "not_tested",
                                  "why": f"This goal's {per_goal} tests are used."})
                    continue
                if why := aside("test", goal["goal"]):  # Its tests failed run after run: not tried again.
                    tests.append({"goal": goal["goal"], "status": "not_tested", "why": why})
                    continue
                if not await _reaches(runtime, hypothesis, goal):  # Nothing to replay it with: no test, no cost.
                    tests.append({"goal": goal["goal"], "status": "not_tested", "why": NOT_REACHED})
                    continue
                used[goal["goal"]] = used.get(goal["goal"], 0) + 1
                # Unexpected: this goal's test fails (logged, a problem of the run's) and the other tests go on; a
                # failure says nothing about the goal, so it isn't counted towards its tests.
                async with contained("test", goal["goal"]) as one:
                    try:
                        async with span("test.goal", goal=goal["goal"]) as section:
                            outcome = await _test_one(hass, entry, runtime, hypothesis, goal)
                            section.tag(status=outcome["status"])
                        tests.append({"goal": goal["goal"], "target": deepcopy(goal), **outcome})
                        if counts_as_tried(outcome):
                            tried[goal["goal"]] = tried.get(goal["goal"], 0) + 1
                    except CaseError as err:
                        tests.append({"goal": goal["goal"], "status": "unsupported", "why": str(err)})
                        tried[goal["goal"]] = tried.get(goal["goal"], 0) + 1
                if one.failed:
                    tests.append({"goal": goal["goal"], "status": "failed", "why": one.error[:300]})
        results.append({"title": hypothesis.get("title"), "tests": tests, "not_testable": skipped})
    tested = sum(used.values())
    report = {"results": results, "tests_per_goal": per_goal, "tested": tested, "paid_inference_performed": tested > 0,
              "tried": dict(tried)}
    if runtime.role_runs is not None:  # Kept, so a lost page or connection loses nothing.
        from uuid import uuid4
        # tried: each goal's tests, kept (trimmed) after the rest, so its tests-per-goal limit holds in later runs.
        await runtime.role_runs.record({"id": uuid4().hex, "kind": "hypothesis_tests", "status": "completed",
                                         "started": started, "finished": utcnow().isoformat(), "output": report,
                                         "tried": dict(tried), **ids()})
    return report


NO_FIX_WHY = "No fix was found for this goal."
NOT_REACHED = "It only changes the instructions of an assistant this conversation didn't use."


def _named(hypothesis):
    """A hypothesis as the run's problems name it: its title, cut short."""
    return str(hypothesis.get("title") or "untitled")[:60]


def _no_fix(skip_goals, goal):
    return isinstance(skip_goals, dict) and skip_goals.get(goal) == NO_FIX


async def _reaches(runtime, hypothesis, goal):
    """Whether the hypothesis changes anything for this goal's conversation: anything but edits of assistants'
    instructions does; those do only when the conversation used one of those assistants."""
    from .instructions import is_edit, ran_with
    changes = hypothesis.get("changes") or []
    if not changes or not all(is_edit(c) for c in changes):
        return True
    try:
        used = (ran_with(await runtime.recorder.export(goal["recording_id"])) or {}).get("target")
    except (CaseError, ValueError):
        return True  # Its test says why it can't be replayed.
    return bool(used) and any(c.get("target") == used for c in changes)


def _instructions_now(hass, hypothesis):
    """{target: template} of the assistants whose instructions the hypothesis edits, as Home Assistant has them while
    it's tested: applying it later finds a conflict if one changed since."""
    from .instructions import is_edit, live
    found = {}
    for change in hypothesis.get("changes") or []:
        if is_edit(change) and change.get("target") and (now := live(hass, change["target"])) is not None:
            found[change["target"]] = now["template"]
    return found


async def _test_one(hass, entry, runtime, hypothesis, found):
    case = await runtime.casebook.create(
        found["recording_id"], title=hypothesis.get("title") or found["summary"], first_turn=found["first_turn"],
        last_turn=found["last_turn"], intervention_turns=found["intervention_turns"], goal_unmet=found["unmet"],
        acceptable=found.get("acceptable", ()), unwanted=found.get("unwanted"), feedback=found.get("feedback"),
        destination=found["summary"],
        criteria=found["criteria"], evidence={"kind": "journey_analysis", "session": found["session"],
                                              "goal_turn": found["goal_turn"]})
    tag(case_id=case["id"])  # The goal's test, and everything in it from here on.
    async with span("baseline") as section:
        case = await run_baseline(runtime.baselines, case)
        section.tag(status=case["baseline"]["status"])
    if case["baseline"]["status"] != "reproduced":
        return {"status": "baseline_" + case["baseline"]["status"], "case_id": case["id"],
                "baseline": case["baseline"]}
    if case.get("follow_feedback"):  # The friction still happens: the replies without a fix don't follow it.
        case = await check_baseline_feedback(hass, entry, runtime.casebook, case)
        heard = case["baseline"]["feedback_check"]
        if heard["status"] != "completed":
            return {"status": "unsupported", "case_id": case["id"], "feedback_check": heard,
                    "why": "The replies without the change couldn't be checked against the household's feedback."}
        if heard["followed"] and not any(case.get(k) for k in ("intervention_turns", "goal_unmet", "unwanted")):
            return {"status": "unsupported", "case_id": case["id"], "feedback_check": heard,
                    "why": "Without the change the replies already follow the household's feedback, so a test can't "
                           "show the change makes them follow it."}
    recording = await runtime.recorder.export(found["recording_id"])
    first = recording["turns"][found["first_turn"] - 1]["source"]
    scripts = deepcopy(first["scripts"]) if "scripts" in first else live_definitions(hass)[0]
    plan = prepare_change_candidate(case, recording, hypothesis=hypothesis, scripts=scripts, definition_evidence={
        "provenance": "recorded" if "scripts" in first else "current, when tested", "source_sha256": digest(scripts)})
    candidates = runtime.candidates
    if (earlier := getattr(candidates, "items", {}).get(plan["id"])) and earlier.get("result"):
        # Already attempted under this plan: its result stands (no resampling).
        return {"status": earlier["status"], "case_id": case["id"], "plan_id": plan["id"], "reused": True,
                "assessment": earlier["result"].get("assessment"),
                "followup_check": earlier["result"].get("followup_check"),
                **({"feedback_check": earlier["result"]["feedback_check"]}
                   if earlier["result"].get("feedback_check") else {}), "turns": earlier["result"].get("turns")}
    item = {"plan": plan, "status": "running", "result": None,
            **({"instructions_at_test": now} if (now := _instructions_now(hass, hypothesis)) else {})}
    async with closing(candidates._save, item) as keep:  # Kept before any inference; closed however it ends.
        agent = conversation.async_get_agent(hass, plan["baseline_plan"]["agent_id"])
        if agent is None:
            raise CaseError("The assistant this conversation used is no longer set up, so it can't be replayed.")
        trial = CandidateTrial(hass, plan, case, recording, agent, evidence_kind="model_counterfactual")
        async with span("candidate", plan_id=plan["id"]) as section:
            item["result"] = await trial.run(allow_paid_inference=True)
            section.tag(status=item["result"]["status"])
        item["result"]["followup_check"] = await check_followups(hass, role_model(entry, "followup_check"),
                                                                 item["result"].get("followups"))
        if plan["judge"].get("feedback") and item["result"]["status"] == "awaiting_review":  # Its effects passed first.
            heard = item["result"]["feedback_check"] = await check_feedback(
                hass, role_model(entry, "followup_check") or role_model(entry, "editor"), plan["judge"]["feedback"],
                item["result"]["turns"])
            followed(item["result"], heard)
        item["status"] = item["result"]["status"]
        await keep(item)
    return {"status": item["status"], "case_id": case["id"], "plan_id": plan["id"],
            "assessment": item["result"]["assessment"], "followup_check": item["result"]["followup_check"],
            **({"feedback_check": item["result"]["feedback_check"]} if item["result"].get("feedback_check") else {}),
            "turns": item["result"]["turns"]}


def followed(result, heard):
    """A test's outcome once its replies were checked against the household's feedback: improved only if they follow
    it (a check that couldn't run judges nothing: the test can't say)."""
    assessment = result.get("assessment") or {}
    assessment["feedback_followed"] = heard["followed"] if heard["status"] == "completed" else None
    if result["status"] == "awaiting_review" and not assessment["feedback_followed"]:
        result["status"] = "not_improved" if assessment["feedback_followed"] is False else "unsupported"
        if assessment["feedback_followed"] is None:
            result["error"] = {"type": "FeedbackCheck", "message": "The replies couldn't be checked against the "
                                                                    "household's feedback."}
    return result
