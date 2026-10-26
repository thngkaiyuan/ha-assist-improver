"""From the generator's hypotheses to tested candidates; code runs every step, models only where judgement is needed.

For each hypothesis and each goal it names (up to the tests-per-goal setting per goal,
over the goal's lifetime): build the goal's case from the analysis, replay the recorded
conversation as the baseline (reproduced when its effects on the home match the
recording), then replay with the hypothesis's changes, the user's recorded
messages in order, until the goal is met. A small model checks that each
replayed follow-up still fits the replies around it.
"""

from copy import deepcopy
import logging

from homeassistant.components import conversation
import probatio

from .analyzer import top_level
from .cases import CaseError, digest
from .candidates import CandidateTrial, prepare_change_candidate
from .const import CONF_TESTS_PER_GOAL, DEFAULT_TESTS_PER_GOAL, NO_FIX
from .hypotheses import friction_by_goal
from .reflective import counts_as_tried, run_role
from .settings import role_model
from .snapshot import live_definitions
from .trace import ids, span, tag

_LOGGER = logging.getLogger(__name__)

CHECK_ROLE = "followup_check"
CHECK_INSTRUCTIONS = """A conversation with a home assistant was replayed with a change to the assistant. The user's messages after the first were replayed exactly as the user originally sent them, so some may no longer fit. For each follow-up below you see the assistant's reply before it, the user's message, and the assistant's reply after it. Say whether the message still broadly makes sense after the reply before it (fits), with a short reason. A message that answers or reacts to something the assistant didn't say does not fit."""
CHECK_STRUCTURE = probatio.Schema({probatio.Required("verdicts"): [{
    probatio.Required("turn"): int, probatio.Required("fits"): bool, probatio.Required("why"): str}]})


def targets(hypothesis, analyses):
    """Each goal the hypothesis names that a replay can judge by its effects on the home: its session, turns and
    destination. Returns (testable goals, reasons the others can't be judged)."""
    found, skipped = [], []
    if not hypothesis.get("goals"):
        return found, ["The hypothesis names no goals."]
    for goal_ref in hypothesis["goals"]:
        name = f"{goal_ref['session'][:8]}:{goal_ref['goal_turn']}"
        analysis = analyses.get(goal_ref["session"][:8])
        if analysis is None:
            skipped.append(f"{name}: no analysis for this session.")
            continue
        turn = goal_ref["goal_turn"]
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
        if not extra and not unmet:
            skipped.append(f"{name}: no friction to fix (no extra turns, and the goal was met).")
            continue
        found.append({"recording_id": analysis["recording_id"], "session": goal_ref["session"][:8], "goal": name,
                      "goal_turn": turn, "first_turn": turn, "last_turn": max([turn, *extra]),
                      "intervention_turns": extra, "friction": len(extra) + unmet, "unmet": unmet,
                      "acceptable": acceptable,
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


async def test_hypotheses(hass, entry, runtime, *, hypotheses, analyses, per_goal=None, skip_goals=(), earlier=None,
                          allow_paid_inference=False):
    """Test each hypothesis on every goal instance it names that a replay can judge, most friction first. A goal
    instance is a goal as it came up in one conversation (session and turn): the same goal phrased differently must be
    fixed in each. No instance gets more than `per_goal` tests with a fix in its lifetime (the tests-per-goal setting
    by default): earlier, {goal: tests it had in earlier runs}, counts too. Only a test that says something about the
    goal counts there (reflective.TRIED): its fix judged, or it can't be replayed; a failure of the provider, the
    sandbox or the code doesn't. Its baseline runs once. skip_goals: {goal: why} of goals that are settled (an improvement, or no
    fix found)."""
    if not allow_paid_inference:
        raise CaseError("Paid inference has not been authorized")
    from homeassistant.util.dt import utcnow
    started = utcnow().isoformat()
    per_goal = int(per_goal or entry.options.get(CONF_TESTS_PER_GOAL, DEFAULT_TESTS_PER_GOAL))
    by_session = {a["recording_id"][:8]: a for a in analyses}
    planned = [(h, *targets(h, by_session)) for h in hypotheses]
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
                used[goal["goal"]] = used.get(goal["goal"], 0) + 1
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
                except Exception as err:  # Unexpected: this goal's test fails (logged); the other tests go on.
                    _LOGGER.exception("Assist Improver: testing %r on %s failed", hypothesis.get("title"),
                                      goal["goal"])
                    tests.append({"goal": goal["goal"], "status": "failed",
                                  "why": f"{type(err).__name__}: {err}"[:300]})
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


def _no_fix(skip_goals, goal):
    return isinstance(skip_goals, dict) and skip_goals.get(goal) == NO_FIX


async def _test_one(hass, entry, runtime, hypothesis, found):
    case = await runtime.casebook.create(
        found["recording_id"], title=hypothesis.get("title") or found["summary"], first_turn=found["first_turn"],
        last_turn=found["last_turn"], intervention_turns=found["intervention_turns"], goal_unmet=found["unmet"],
        acceptable=found.get("acceptable", ()),
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
                "followup_check": earlier["result"].get("followup_check"), "turns": earlier["result"].get("turns")}
    item = await candidates._save({"plan": plan, "status": "running", "result": None})  # Before any inference.
    agent = conversation.async_get_agent(hass, plan["baseline_plan"]["agent_id"])
    if agent is None:
        raise CaseError("The assistant this conversation used is no longer set up, so it can't be replayed.")
    trial = CandidateTrial(hass, plan, case, recording, agent, evidence_kind="model_counterfactual")
    async with span("candidate", plan_id=plan["id"]) as section:
        item["result"] = await trial.run(allow_paid_inference=True)
        section.tag(status=item["result"]["status"])
    item["result"]["followup_check"] = await check_followups(hass, role_model(entry, "followup_check"),
                                                             item["result"].get("followups"))
    item["status"] = item["result"]["status"]
    await candidates._save(item)
    return {"status": item["status"], "case_id": case["id"], "plan_id": plan["id"],
            "assessment": item["result"]["assessment"], "followup_check": item["result"]["followup_check"],
            "turns": item["result"]["turns"]}
