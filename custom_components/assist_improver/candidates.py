"""Testing a fix: a hypothesis's changes as a candidate plan (script definitions, exposure, aliases, instruction
lines), checked against its case and recording, then the conversation replayed in a sandbox with the household's own
assistant and the change, the user's messages in order, until the goal is met (validation.py drives it). Each plan
runs once: an identical plan reuses its result, never samples again.
"""

from copy import deepcopy
from types import SimpleNamespace

from homeassistant.helpers.storage import Store

from .baseline import clone_provider, prepare_plan
from .cases import CaseError, digest, replayed_turns, retained_inputs, verify_case
from .const import DOMAIN, STORAGE_VERSION
from .replay import FrozenTurnLog, check_fidelity
from .changes import setup_from_changes
from .instructions import target
from .snapshot import live_definitions
from .records import Candidate, CandidateResult
from .serialization import plain
from .simulation import UnsupportedSimulation, destination_met as met
from .trace import span


def _grounding(case, recording):
    """The case's turns, its baseline and the plan it ran under, and the inputs a candidate replays. The baseline
    must be reproduced under the current plan: every turn of the case kept as a step whose effects matched."""
    turns = verify_case(case, recording)
    baseline_plan = prepare_plan(case, recording)
    baseline = case["baseline"]
    matched = [s.get("turn") for s in baseline.get("steps", []) if s.get("ha_effects_match")]
    if (baseline["status"] != "reproduced" or baseline.get("plan", {}).get("id") != baseline_plan["id"]
            or matched != (case.get("lead_in") or []) + case["turns"]):
        raise CaseError("Representative baseline must reproduce with current grounding before a candidate trial")
    # This slice supports just the first original input. Later slices can
    # retain longer ordered subsequences, with compatible recorded answers.
    retained = [case["turns"][0]]
    inputs = retained_inputs(case, recording, retained)
    return turns, baseline_plan, baseline, retained, inputs


def _feedback_counts(case, baseline):
    """Whether a fix can be judged on the household's feedback: the replies without it (the baseline's, checked once
    by validation.check_baseline_feedback) didn't follow it. Feedback no check has seen yet stops the plan."""
    if not case.get("follow_feedback"):
        return False
    heard = baseline.get("feedback_check") or {}
    if heard.get("status") != "completed" or heard.get("feedback") != case["follow_feedback"]:
        raise CaseError("The replies without the change haven't been checked against the household's feedback")
    return heard["followed"] is False


def prepare_change_candidate(case, recording, *, hypothesis, scripts, definition_evidence):
    """A generated hypothesis's changes, tested on one case (the goal as it came up in one recording). scripts: the
    household's script definitions ({object id: config}) the changes build on."""
    if not isinstance(hypothesis, dict) or not hypothesis.get("changes"):
        raise CaseError("Candidate needs a hypothesis with changes")
    if not isinstance(hypothesis.get("title"), str) or not hypothesis["title"].strip():
        raise CaseError("Candidate needs one coherent hypothesis")
    turns, baseline_plan, baseline, retained, inputs = _grounding(case, recording)
    source = turns[0]["source"]
    config = turns[0]["prepared"]["provider_config"][0]
    user_id = (turns[0]["prepared"].get("context") or {}).get("context", {}).get("user_id")
    # The instructions it ran with, edited where the hypothesis edits this conversation's assistant's.
    setup = setup_from_changes(hypothesis["changes"], source=source, prompt=config["options"].get("prompt"),
                               scripts=scripts, user_id=user_id, assistant=target(config))
    edits = [{"kind": "assist_exposure", "entity_id": e, "before": bool(source["exposure"].get(e)), "after": v}
             for e, v in sorted(setup["exposure"].items())]
    touched = {c["entity_id"].split(".", 1)[1] for c in hypothesis["changes"]
               if c["kind"] in ("script", "exposure") and (c.get("entity_id") or "").startswith("script.")}
    definitions = {f"script.{oid}": deepcopy(scripts[oid]) for oid in sorted(touched) if oid in scripts}
    plan = {"format": 1, "kind": "hypothesis_changes", "hypothesis": deepcopy(hypothesis),
            "evaluation_case_id": case["id"], "baseline_digest": digest(baseline),
            "baseline_plan": baseline_plan, "retained_turns": retained, "user_inputs": inputs,
            "judge": {"destination": deepcopy(case["destination"]),
                      "original_user_turns": case.get("original_user_turns", len(turns)),
                      # Whether the conversation without the fix met the goal (its baseline): a fix that meets an
                      # unmet goal improves it even in as many turns.
                      "baseline_met": baseline.get("destination_met"),
                      "acceptable_devices": case.get("acceptable_devices", []),
                      # Side effects the original left, which the fix must not repeat (replayed through the last
                      # turn that made one), and the household's feedback, which its replies must follow
                      # (validation.check_feedback) where the replies without it didn't; only when there are (plan IDs).
                      **({"unwanted": deepcopy(case["unwanted"])} if case.get("unwanted") else {}),
                      **({"replay_through": case["replay_through"]} if case.get("replay_through") else {}),
                      **({"feedback": list(case["follow_feedback"])} if _feedback_counts(case, baseline) else {}),
                      # Feedback its only friction (no extra turns, nothing unmet, no side effect): judged on that
                      # alone, so turns it replays to reach the goal aren't credited as saved.
                      **({"feedback_only": True} if case.get("follow_feedback") and not any(
                          case.get(k) for k in ("intervention_turns", "goal_unmet", "unwanted")) else {})},
            "edits": edits, "definitions": deepcopy(definitions), "setup": deepcopy(setup),
            "definition_evidence": deepcopy(definition_evidence),
            # What this test covers: its case, replayed with the change. Each other goal the hypothesis names gets
            # its own test.
            "coverage": [{"case_id": case["id"], "role": "friction", "evaluation": "selected_counterfactual"}],
            "counterfactual_allowance_used_by_this_plan": 1,
            "provider_option_changes": {}, "system_prompt_change": setup["prompt_changed"],
            "limits": ["A representative pass does not prove gains for every affected journey",
                       "Script definitions may be later observations; their provenance is retained",
                       "HA's own engine runs the scripts in a sandbox; devices are emulated, physical position is not",
                       "Other scripts and automations use their current definitions unless the recording has them",
                       "Unmodelled device commands stop evaluation",
                       *(["Instruction lines are tested as household knowledge, which every assistant on Assist "
                          "reads"] if "household" in setup else [])]}
    plan["id"] = digest(plan)
    return plan


def verify_candidate(plan, case, recording, scripts=None):
    """The plan as its case, recording and baseline make it now; CaseError if anything it was built from changed."""
    expected = prepare_change_candidate(
        case, recording, hypothesis=plan["hypothesis"],
        scripts=scripts if scripts is not None else {e.split(".", 1)[1]: d for e, d in plan["definitions"].items()},
        definition_evidence=plan["definition_evidence"])
    if plan != expected:
        raise CaseError("Candidate, grounding or baseline changed; prepare and review a new plan")
    return expected


def judge_effects(plan, world, *, used_user_turns, also_expected=()):
    """Fixed command-level judge, independent of the candidate's tool responses. also_expected: devices the
    conversation changed without the fix too (its baseline), which aren't unintended.

    A fix improves the goal when it reaches it in no more turns, commands nothing unintended, changes none of the
    side effects the original left (unwanted), and removes some of its friction: fewer turns, the goal newly met, a
    side effect gone, or (judged after, by a small model: validation.followed) the household's feedback followed."""
    judge = plan["judge"]
    criteria = judge["destination"]["criteria"]
    targets = set(criteria.get("states", criteria.get("sequences", {})))
    destination_met = met(criteria, world.states, world.transitions)
    # A command to an unintended target counts even if its state did not change. A script's own run is not a
    # device command; its steps are.
    unintended = sorted({c["entity_id"] for c in world.calls if not c["service"].startswith("script.")}
                        - targets - set(also_expected) - set(judge.get("acceptable_devices", [])))
    original = judge["original_user_turns"]
    no_more = 0 < used_user_turns <= original
    # Judged on its feedback alone, turns and the goal aren't its friction to remove.
    reduced = 0 < used_user_turns < original and not judge.get("feedback_only")
    # Goals met first, then fewer turns: meeting a goal the baseline didn't meet counts in as many turns.
    newly_met = destination_met and judge.get("baseline_met") is False and no_more and not judge.get("feedback_only")
    # A side effect is gone when the replay didn't change that device at all (off and on again is still a change).
    unwanted = judge.get("unwanted") or {}
    left = sorted({t["entity_id"] for t in world.transitions} & set(unwanted))
    gone = sorted(set(unwanted) - set(left)) if not left else []
    out = {"command_destination_met": destination_met, "unintended_targets": unintended,
           "strictly_fewer_user_turns": reduced, "goal_newly_met": newly_met,
           "baseline_met": judge.get("baseline_met"),
           "original_user_turns": original, "candidate_user_turns": used_user_turns,
           "command_check_passed": destination_met and not unintended and not left and no_more and (
               reduced or newly_met or bool(gone) or bool(judge.get("feedback"))),
           "physical_outcome": "not_verified_by_simulation"}
    if unwanted:
        out.update(side_effects_left=left, side_effects_gone=gone)
    if judge.get("feedback"):
        out["feedback_followed"] = None  # Until the small model judged the replies (validation.followed).
    return out


def past_the_goal(at_goal, now, judge):
    """The assessment of a replay gone on past the turn its goal was met (to a later turn that made a side effect):
    the goal, and the turns it took, as judged there; what the fix commanded and left, through the last turn."""
    out = {**now, **{key: at_goal[key] for key in ("command_destination_met", "strictly_fewer_user_turns",
                                                   "goal_newly_met", "candidate_user_turns")}}
    no_more = 0 < at_goal["candidate_user_turns"] <= at_goal["original_user_turns"]
    out["command_check_passed"] = (at_goal["command_destination_met"] and not now["unintended_targets"]
                                   and not now.get("side_effects_left") and no_more
                                   and (at_goal["strictly_fewer_user_turns"] or at_goal["goal_newly_met"]
                                        or bool(now.get("side_effects_gone")) or bool(judge.get("feedback"))))
    return out


class CandidateTrial:
    """One representative, one exact original input; no retries or live writes."""
    def __init__(self, hass, plan, case, recording, agent, *, evidence_kind):
        if evidence_kind not in {"scripted_transport_fixture", "model_counterfactual"}:
            raise CaseError("Distinguish scripted transport tests from actual model decisions")
        self.turns, self.turns_run = replayed_turns(case, recording), []
        first = self.turns[0]  # Where the replay starts: its lead-in's first turn when it has one (cases.lead_in).
        scripts = deepcopy(first["source"]["scripts"]) if "scripts" in first["source"] else live_definitions(hass)[0]
        scripts.update({entity.split(".", 1)[1]: deepcopy(d) for entity, d in plan["definitions"].items()})
        self.plan = deepcopy(verify_candidate(plan, case, recording, scripts=scripts))
        self.evidence_kind, self.recording, self.first_turn = evidence_kind, recording, first["number"]
        # Replayed with the change, with the user's recorded words, and not judged: the goal is, from its first turn.
        self.lead_in = set(case.get("lead_in") or [])
        # What the conversation changed without the fix (its baseline): commanding those isn't unintended.
        self.baseline_entities = sorted({t["entity_id"] for step in case["baseline"].get("steps", [])
                                         if not step.get("lead_in")  # The goal's own turns, as it's judged.
                                         for t in step.get("transitions", []) + step.get("recorded_transitions", [])})
        setup = self.plan["setup"]
        self.log = FrozenTurnLog(hass, case, recording, scripts={**scripts, **setup["scripts"]},
                                 exposure_changes=setup["exposure"], alias_changes=setup["aliases"],
                                 alias_removals=setup.get("alias_removals"),
                                 prompt_template=setup["prompt"], use_sandbox_prompt=setup["prompt_changed"],
                                 household=setup.get("household"))
        self.agent = clone_provider(agent, plan["baseline_plan"])
        self.start = len(self.log.content)
        self.status, self.error, self.assessment = "prepared", None, None

    async def run(self, *, allow_paid_inference=False) -> CandidateResult:
        if not allow_paid_inference:
            raise CaseError("Paid inference has not been authorized")
        if self.status != "prepared":
            raise CaseError("Candidate was already attempted; no automatic resampling")
        self.status = "running"
        try:
            if self.plan["setup"]["prompt_changed"]:
                # The change shows in Assist's instructions, so the model sees the prompt HA builds in the sandbox.
                # That is faithful only if, unchanged, the sandbox builds exactly the recorded prompt.
                report = await check_fidelity(self.log.hass, self.recording, turn_number=self.first_turn)
                if not report["prompt_matches"]:
                    raise UnsupportedSimulation("The sandbox doesn't rebuild this recording's instructions exactly: "
                                                + " | ".join(report["prompt_diff"][2:8]))
            # The user's own messages, in order (a scripted user): the first, then each follow-up the recording
            # has, until the destination is met and past each turn that made a side effect it must not repeat
            # (replay_through: the household said those either way; turns past the goal don't count against it).
            through, met_at, world = self.plan["judge"].get("replay_through") or 0, None, self.log.replay_api.world
            goal = 0  # The goal's turns replayed so far.
            for index, turn in enumerate(self.turns):
                if index:
                    self.log.continue_with(turn)
                lead_in = turn["number"] in self.lead_in
                if not lead_in and not goal:  # The goal is judged on what its own turns do,
                    if self.lead_in:  # from where the recording had its devices when it began.
                        await self._check_start(turn, world)
                    since = len(world.transitions), len(world.calls)
                start = len(self.log.content)
                async with span("replay.turn", replay="candidate", turn=turn["number"]):
                    await self.agent._async_handle_message(self.log.input, self.log)
                output = self.log.content[start:]
                if (not output or self.log.unresponded_tool_results or output[-1].role != "assistant"
                        or not output[-1].content or output[-1].tool_calls):
                    raise UnsupportedSimulation("Candidate did not finish the user's turn")
                self.turns_run.append({"turn": turn["number"], **({"lead_in": True} if lead_in else {}),
                                       "input": self.log.input.text, "reply": output[-1].content,
                                       "output": plain([c.as_dict() for c in output])})
                if lead_in:
                    continue
                goal += 1
                own = SimpleNamespace(states=world.states, transitions=world.transitions[since[0]:],
                                      calls=world.calls[since[1]:])
                now = judge_effects(self.plan, own, used_user_turns=met_at or goal, also_expected=self.baseline_entities)
                if met_at is None:
                    self.assessment = now
                    if now["command_destination_met"]:
                        met_at, at_goal = goal, now
                else:  # Past the goal: the goal stands as judged where it was met; the side effects run on.
                    self.assessment = past_the_goal(at_goal, now, self.plan["judge"])
                if met_at and turn["number"] >= through:
                    break
            self.status = "awaiting_review" if self.assessment["command_check_passed"] else "not_improved"
        except Exception as err:
            self.status = "unsupported" if isinstance(err, CaseError) else "infrastructure_failure"
            self.error = {"type": type(err).__name__}
            if isinstance(err, CaseError):
                self.error["message"] = str(err)
        finally:
            await self.log.close()
        return self.export()

    async def _check_start(self, turn, world):
        """After a lead-in replayed with the change: the goal's devices, and the side effects it must not leave, as the
        recording had them when the goal began (once what others moved in the home meanwhile is applied, as the turn
        would apply it), or the goal can't be judged from there (unsupported)."""
        if self.log.pending_source is not None:
            await self.log.replay_api.observe(self.log.pending_source, self.log.assistant_entities)
            self.log.pending_source = None
        judge, recorded = self.plan["judge"], turn["source"]["states"]
        criteria = judge["destination"]["criteria"]
        devices = set(criteria.get("states", criteria.get("sequences", {}))) | set(judge.get("unwanted") or {})
        moved = sorted(e for e in devices if e in recorded and world.states.get(e) != recorded[e]["state"])
        if moved:
            raise UnsupportedSimulation("With the change, the turns before the goal left "
                                        f"{', '.join(moved)} unlike the recording, so the goal can't be judged")

    def export(self) -> CandidateResult:
        return {"plan_id": self.plan["id"], "evaluation_case_id": self.plan["evaluation_case_id"],
                "status": self.status, "evidence_kind": self.evidence_kind, "run_id": self.log.conversation_id,
                "input": next((t["input"] for t in self.turns_run if not t.get("lead_in")), self.log.input.text),
                "output": plain([c.as_dict() for c in self.log.content[self.start:]]),
                "turns": deepcopy(self.turns_run),
                # Each scripted follow-up between the replies around it, for a check that they still fit together.
                "followups": [{"turn": now["turn"], "reply_before": before["reply"], "message": now["input"],
                               "reply_after": now["reply"]} for before, now in zip(self.turns_run, self.turns_run[1:])],
                "simulated_calls": deepcopy(self.log.replay_api.world.calls),
                "transitions": deepcopy(self.log.replay_api.world.transitions),
                "external_calls": deepcopy(self.log.external_calls),
                "usage": self.log.usage_report(), "assessment": deepcopy(self.assessment),
                "error": deepcopy(self.error), "coverage": deepcopy(self.plan["coverage"]), "real_device_actions": 0}


def live_script_definitions(hass, entities):
    """Current UI/YAML script configs; later observations, never historical snapshots."""
    component = hass.data.get("script")
    definitions = {}
    for entity in entities:
        script = component.get_entity(entity) if component else None
        if script is None or not getattr(script, "raw_config", None):
            raise CaseError(f"Script {entity} has no readable configuration")
        definitions[entity] = deepcopy(dict(script.raw_config))
    return definitions


class CandidateManager:
    """The candidates store: each plan's test, kept from before its first inference, so no plan runs twice."""
    def __init__(self, hass, casebook):
        self.hass, self.book = hass, casebook
        self.store = Store(hass, STORAGE_VERSION, f"{DOMAIN}.candidates", private=True, atomic_writes=True)
        self.items: dict[str, Candidate] = {}

    async def start(self):
        saved = await self.store.async_load()
        self.items = saved.get("candidates", {}) if saved else {}
        for item in list(self.items.values()):
            if item["status"] == "running":
                # A reload lost the run mid-flight; keep the attempt, never rerun it.
                await self._save({**item, "status": "interrupted"})

    async def _save(self, item):
        updated = {**self.items, item["plan"]["id"]: deepcopy(item)}
        await self.store.async_save({"candidates": updated})
        self.items = updated
        return deepcopy(item)

    async def drop(self, plan_ids):
        """Forget changes' tests (those on a deleted conversation's cases)."""
        updated = {k: v for k, v in self.items.items() if k not in plan_ids}
        if len(updated) < len(self.items):
            await self.store.async_save({"candidates": updated})
            self.items = updated

    def get(self, plan_id):
        if plan_id not in self.items:
            raise CaseError("Unknown candidate plan ID")
        return deepcopy(self.items[plan_id])
