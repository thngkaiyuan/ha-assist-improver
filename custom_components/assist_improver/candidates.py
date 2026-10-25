"""Testing a fix: a hypothesis's changes as a candidate plan (script definitions, exposure, aliases, instruction
lines), checked against its case and recording, then the conversation replayed in a sandbox with the household's own
assistant and the change, the user's messages in order, until the goal is met (validation.py drives it). Each plan
runs once: an identical plan reuses its result, never samples again.
"""

from copy import deepcopy

from homeassistant.helpers.storage import Store

from .baseline import clone_provider, prepare_plan
from .cases import CaseError, digest, retained_inputs, verify_case
from .const import DOMAIN, STORAGE_VERSION
from .replay import FrozenTurnLog, check_fidelity
from .changes import setup_from_changes
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
            or matched != case["turns"]):
        raise CaseError("Representative baseline must reproduce with current grounding before a candidate trial")
    # This slice supports just the first original input. Later slices can
    # retain longer ordered subsequences, with compatible recorded answers.
    retained = [case["turns"][0]]
    inputs = retained_inputs(case, recording, retained)
    return turns, baseline_plan, baseline, retained, inputs


def prepare_change_candidate(case, recording, *, hypothesis, scripts, definition_evidence):
    """A generated hypothesis's changes, tested on one case (the goal as it came up in one recording). scripts: the
    household's script definitions ({object id: config}) the changes build on."""
    if not isinstance(hypothesis, dict) or not hypothesis.get("changes"):
        raise CaseError("Candidate needs a hypothesis with changes")
    if not isinstance(hypothesis.get("title"), str) or not hypothesis["title"].strip():
        raise CaseError("Candidate needs one coherent hypothesis")
    turns, baseline_plan, baseline, retained, inputs = _grounding(case, recording)
    source = turns[0]["source"]
    options = turns[0]["prepared"]["provider_config"][0]["options"]
    user_id = (turns[0]["prepared"].get("context") or {}).get("context", {}).get("user_id")
    setup = setup_from_changes(hypothesis["changes"], source=source, prompt=options.get("prompt"), scripts=scripts,
                               user_id=user_id)
    edits = [{"kind": "assist_exposure", "entity_id": e, "before": bool(source["exposure"].get(e)), "after": v}
             for e, v in sorted(setup["exposure"].items())]
    touched = {c["entity_id"].split(".", 1)[1] for c in hypothesis["changes"]
               if c["kind"] in ("script", "exposure") and (c.get("entity_id") or "").startswith("script.")}
    definitions = {f"script.{oid}": deepcopy(scripts[oid]) for oid in sorted(touched) if oid in scripts}
    plan = {"format": 1, "kind": "hypothesis_changes", "hypothesis": deepcopy(hypothesis),
            "evaluation_case_id": case["id"], "baseline_digest": digest(baseline),
            "baseline_plan": baseline_plan, "retained_turns": retained, "user_inputs": inputs,
            "judge": {"destination": deepcopy(case["destination"]), "original_user_turns": len(turns),
                      # Whether the conversation without the fix met the goal (its baseline): a fix that meets an
                      # unmet goal improves it even in as many turns.
                      "baseline_met": baseline.get("destination_met"),
                      "acceptable_devices": case.get("acceptable_devices", [])},
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
                       *(["Instruction lines are tested through Assist with household knowledge; applying "
                          "switches the tested assistant to it"] if "household" in setup else [])]}
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
    conversation changed without the fix too (its baseline), which aren't unintended."""
    criteria = plan["judge"]["destination"]["criteria"]
    targets = set(criteria.get("states", criteria.get("sequences", {})))
    destination_met = met(criteria, world.states, world.transitions)
    # A command to an unintended target counts even if its state did not change. A script's own run is not a
    # device command; its steps are.
    unintended = sorted({c["entity_id"] for c in world.calls if not c["service"].startswith("script.")}
                        - targets - set(also_expected) - set(plan["judge"].get("acceptable_devices", [])))
    original = plan["judge"]["original_user_turns"]
    reduced = 0 < used_user_turns < original
    # Goals met first, then fewer turns: meeting a goal the baseline didn't meet counts in as many turns.
    newly_met = destination_met and plan["judge"].get("baseline_met") is False and 0 < used_user_turns <= original
    return {"command_destination_met": destination_met, "unintended_targets": unintended,
            "strictly_fewer_user_turns": reduced, "goal_newly_met": newly_met,
            "baseline_met": plan["judge"].get("baseline_met"),
            "original_user_turns": original, "candidate_user_turns": used_user_turns,
            "command_check_passed": destination_met and (reduced or newly_met) and not unintended,
            "physical_outcome": "not_verified_by_simulation"}


class CandidateTrial:
    """One representative, one exact original input; no retries or live writes."""
    def __init__(self, hass, plan, case, recording, agent, *, evidence_kind):
        if evidence_kind not in {"scripted_transport_fixture", "model_counterfactual"}:
            raise CaseError("Distinguish scripted transport tests from actual model decisions")
        first = verify_case(case, recording)[0]
        scripts = deepcopy(first["source"]["scripts"]) if "scripts" in first["source"] else live_definitions(hass)[0]
        scripts.update({entity.split(".", 1)[1]: deepcopy(d) for entity, d in plan["definitions"].items()})
        self.plan = deepcopy(verify_candidate(plan, case, recording, scripts=scripts))
        self.evidence_kind, self.recording, self.first_turn = evidence_kind, recording, first["number"]
        self.turns, self.turns_run = verify_case(case, recording), []
        # What the conversation changed without the fix (its baseline): commanding those isn't unintended.
        self.baseline_entities = sorted({t["entity_id"] for step in case["baseline"].get("steps", [])
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
            # has, until the destination is met.
            for index, turn in enumerate(self.turns):
                if index:
                    self.log.continue_with(turn)
                start = len(self.log.content)
                async with span("replay.turn", replay="candidate", turn=turn["number"]):
                    await self.agent._async_handle_message(self.log.input, self.log)
                output = self.log.content[start:]
                if (not output or self.log.unresponded_tool_results or output[-1].role != "assistant"
                        or not output[-1].content or output[-1].tool_calls):
                    raise UnsupportedSimulation("Candidate did not finish the user's turn")
                self.turns_run.append({"turn": turn["number"], "input": self.log.input.text,
                                       "reply": output[-1].content, "output": plain([c.as_dict() for c in output])})
                self.assessment = judge_effects(self.plan, self.log.replay_api.world, used_user_turns=index + 1,
                                                also_expected=self.baseline_entities)
                if self.assessment["command_destination_met"]:
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

    def export(self) -> CandidateResult:
        return {"plan_id": self.plan["id"], "evaluation_case_id": self.plan["evaluation_case_id"],
                "status": self.status, "evidence_kind": self.evidence_kind, "run_id": self.log.conversation_id,
                "input": self.turns_run[0]["input"] if self.turns_run else self.log.input.text,
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

    def get(self, plan_id):
        if plan_id not in self.items:
            raise CaseError("Unknown candidate plan ID")
        return deepcopy(self.items[plan_id])
