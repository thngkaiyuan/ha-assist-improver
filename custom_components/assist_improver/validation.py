"""From the generator's hypotheses to tested candidates; code runs every step, models only where judgement is needed.

For each hypothesis and each goal it names (up to the tests-per-goal setting per goal
in a cycle): build the goal's case from the analysis, replay the recorded
conversation as the baseline (reproduced when its effects on the home match the
recording), then replay with the hypothesis's changes, the user's recorded
messages in order, until the goal is met. A small model checks that each
replayed follow-up still fits the replies around it.
"""

from copy import deepcopy

from homeassistant.components import conversation
import probatio

from .analyzer import top_level
from .cases import CaseError, digest
from .candidates import CandidateTrial, prepare_change_candidate
from .const import CONF_TESTS_PER_GOAL, DEFAULT_TESTS_PER_GOAL
from .hypotheses import friction_by_goal
from .reflective import run_role
from .settings import role_model
from .snapshot import live_definitions
from .themes import create_theme

BASELINE_REVIEW = "Effects on the home match the recording; the replies are kept for reading."
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


def target(hypothesis, analyses):
    """The testable goal with the most friction, or None with why."""
    found, skipped = targets(hypothesis, analyses)
    return (found[0], None) if found else (None, skipped[0] if skipped else "No goal can be judged.")


async def run_baseline(manager, case):
    """Replay the recorded conversation; reproduced when every turn's effects match the recording. A baseline
    settled under the current plan is reused; one settled under an older plan is run again under the new one."""
    plan = (await manager.prepare(case["id"]))["plan"]
    if case["baseline"].get("plan", {}).get("id") == plan["id"] and case["baseline"]["status"] != "not_run":
        return case
    case = await manager.start(case["id"], plan_id=plan["id"], allow_paid_inference=True)
    while case["baseline"]["status"] == "awaiting_review":
        turn = case["baseline"]["next_turn"]
        case = await manager.review(case["id"], expected_turn=turn, reproduced=True, feedback=BASELINE_REVIEW)
        if case["baseline"]["status"] == "ready_for_next_turn":
            case = await manager.continue_trial(case["id"], expected_turn=case["baseline"]["next_turn"],
                                                allow_paid_inference=True)
    return case


async def check_followups(hass, entity_id, followups):
    """The small model's verdict on each replayed follow-up; None when no check model is set."""
    if not followups or not entity_id:
        return None
    record = await run_role(hass, role=CHECK_ROLE, entity_id=entity_id, instructions=CHECK_INSTRUCTIONS,
                            payload={"followups": followups}, structure=CHECK_STRUCTURE, allow_paid_inference=True)
    verdicts = (record.get("output") or {}).get("verdicts", [])
    return {"status": record["status"], "verdicts": verdicts, "error": record.get("error"),
            "all_fit": record["status"] == "completed" and all(v["fits"] for v in verdicts), "usage": record["usage"]}


async def test_hypotheses(hass, entry, recorder, *, hypotheses, analyses, per_goal=None, skip_goals=(),
                          allow_paid_inference=False):
    """Test each hypothesis on every goal instance it names that a replay can judge, most friction first. A goal
    instance is a goal as it came up in one conversation (session and turn): the same goal phrased differently must be
    fixed in each. No instance gets more than `per_goal` replays with a fix in this cycle (the tests-per-goal setting
    by default); its baseline runs once."""
    if not allow_paid_inference:
        raise CaseError("Paid inference has not been authorized")
    from homeassistant.util.dt import utcnow
    started = utcnow().isoformat()
    per_goal = int(per_goal or entry.options.get(CONF_TESTS_PER_GOAL, entry.options.get(
        "tests_per_cycle", DEFAULT_TESTS_PER_GOAL)))  # tests_per_cycle: the setting's name before 0.17.4.
    by_session = {a["recording_id"][:8]: a for a in analyses}
    planned = [(h, *targets(h, by_session)) for h in hypotheses]
    planned.sort(key=lambda p: -sum(f["friction"] for f in p[1]))
    results, used = [], {}
    for hypothesis, found, skipped in planned:
        tests = []
        for goal in found:
            if goal["goal"] in skip_goals:  # Already has an improvement (waiting, applied or decided).
                tests.append({"goal": goal["goal"], "status": "not_tested", "why": "This goal already has an improvement."})
                continue
            if used.get(goal["goal"], 0) >= per_goal:
                tests.append({"goal": goal["goal"], "status": "not_tested",
                              "why": f"This goal's {per_goal} tests are used this cycle."})
                continue
            used[goal["goal"]] = used.get(goal["goal"], 0) + 1
            try:
                tests.append({"goal": goal["goal"], "target": deepcopy(goal),
                              **await _test_one(hass, entry, recorder, hypothesis, goal)})
            except CaseError as err:
                tests.append({"goal": goal["goal"], "status": "unsupported", "why": str(err)})
        results.append({"title": hypothesis.get("title"), "tests": tests, "not_testable": skipped})
    tested = sum(used.values())
    report = {"results": results, "tests_per_goal": per_goal, "tested": tested, "paid_inference_performed": tested > 0}
    if getattr(recorder, "role_runs", None) is not None:  # Kept, so a lost page or connection loses nothing.
        from uuid import uuid4
        await recorder.role_runs.record({"id": uuid4().hex, "kind": "hypothesis_tests", "status": "completed",
                                         "started": started, "finished": utcnow().isoformat(), "output": report})
    return report


async def _test_one(hass, entry, recorder, hypothesis, found):
    case = await recorder.casebook.create(
        found["recording_id"], title=hypothesis.get("title") or found["summary"], first_turn=found["first_turn"],
        last_turn=found["last_turn"], intervention_turns=found["intervention_turns"], goal_unmet=found["unmet"],
        acceptable=found.get("acceptable", ()),
        destination=found["summary"],
        criteria=found["criteria"], evidence={"kind": "journey_analysis", "session": found["session"],
                                              "goal_turn": found["goal_turn"]})
    case = await run_baseline(recorder.baselines, case)
    if case["baseline"]["status"] != "reproduced":
        return {"status": "baseline_" + case["baseline"]["status"], "case_id": case["id"],
                "baseline": case["baseline"]}
    recording = await recorder.export(found["recording_id"])
    cases = {case["id"]: case}
    theme = create_theme(cases, title=hypothesis.get("title") or found["summary"],
                         friction=hypothesis.get("cause") or found["summary"],
                         observations=[{"case_id": case["id"], "role": "friction",
                                        "rationale": f"Goal at turn {found['goal_turn']}: "
                                                     f"{len(found['intervention_turns'])} extra turn(s)"
                                                     + (", and the goal went unmet" if found["unmet"] else "")}],
                         provenance="Code-built from the generator's hypothesis and the journey analysis")
    first = recording["turns"][found["first_turn"] - 1]["source"]
    scripts = deepcopy(first["scripts"]) if "scripts" in first else live_definitions(hass)[0]
    plan = prepare_change_candidate(theme, cases, recording, evaluation_case_id=case["id"], hypothesis=hypothesis,
                                    scripts=scripts, definition_evidence={
                                        "provenance": "recorded" if "scripts" in first else "current, when tested",
                                        "source_sha256": digest(scripts)})
    candidates = recorder.candidates
    if (earlier := getattr(candidates, "items", {}).get(plan["id"])) and earlier.get("result"):
        # Already attempted under this plan: its result stands (no resampling).
        return {"status": earlier["status"], "case_id": case["id"], "plan_id": plan["id"], "reused": True,
                "assessment": earlier["result"].get("assessment"),
                "followup_check": earlier["result"].get("followup_check"), "turns": earlier["result"].get("turns")}
    item = await candidates._save({"plan": plan, "status": "running", "evidence": theme["journeys"],
                                   "result": None, "reviews": [], "application": None})  # Before any inference.
    agent = conversation.async_get_agent(hass, plan["baseline_plan"]["agent_id"])
    trial = CandidateTrial(hass, plan, cases, recording, agent, evidence_kind="model_counterfactual")
    item["result"] = await trial.run(allow_paid_inference=True)
    item["result"]["followup_check"] = await check_followups(hass, role_model(entry, "followup_check"),
                                                             item["result"].get("followups"))
    item["status"] = item["result"]["status"]
    await candidates._save(item)
    return {"status": item["status"], "case_id": case["id"], "plan_id": plan["id"],
            "assessment": item["result"]["assessment"], "followup_check": item["result"]["followup_check"],
            "turns": item["result"]["turns"]}
