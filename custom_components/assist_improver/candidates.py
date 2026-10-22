"""First thematic candidate: expose existing scripts, replay one representative.

Admin actions prepare a plan, run it once with explicit paid-call consent, record
a review, and on acceptance change Assist exposure only. Hypotheses are seeded by
an administrator; nothing generates them with a model yet.
"""

from copy import deepcopy

import probatio
from homeassistant.components import conversation
from homeassistant.components.homeassistant.exposed_entities import async_expose_entity, async_should_expose
from homeassistant.helpers import llm
from homeassistant.helpers.storage import Store

from .baseline import clone_provider, prepare_plan
from .cases import CaseError, digest, retained_inputs, verify_case
from .const import DOMAIN, STORAGE_VERSION
from .replay import FrozenTurnLog, RecordedParameters
from .script_simulation import compile_script, execute_compiled, script_tool_definition
from .serialization import plain
from .simulation import UnsupportedSimulation
from .themes import create_theme, verify_theme


def prepare_exposure_candidate(theme, cases, recording, *, evaluation_case_id,
                               hypothesis, entities, definitions, definition_evidence):
    """A shared solution is scoped by its theme; one case is a bounded test sample."""
    verify_theme(theme, cases)
    links = {link["case_id"]: link for link in theme["journeys"]}
    if evaluation_case_id not in links or links[evaluation_case_id]["role"] != "friction":
        raise CaseError("Choose an intervention journey linked to this shared theme")
    if not isinstance(hypothesis, str) or not hypothesis.strip():
        raise CaseError("Candidate needs one coherent hypothesis")
    case = cases[evaluation_case_id]
    turns = verify_case(case, recording)
    baseline_plan = prepare_plan(case, recording)
    baseline = case["baseline"]
    if (baseline["status"] != "reproduced" or baseline.get("plan", {}).get("id") != baseline_plan["id"]
            or [r.get("turn") for r in baseline.get("reviews", []) if r.get("reproduced")] != case["turns"]):
        raise CaseError("Representative baseline must reproduce with current grounding before a candidate trial")
    # This slice supports just the first original input. Later slices can
    # retain longer ordered subsequences, with compatible recorded answers.
    retained = [case["turns"][0]]
    inputs = retained_inputs(case, recording, retained)
    if not entities or len(entities) != len(set(entities)):
        raise CaseError("Choose distinct existing scripts to expose")
    if (not isinstance(definition_evidence, dict) or not definition_evidence.get("provenance")
            or not definition_evidence.get("source_sha256")):
        raise CaseError("Script definitions need attributed evidence, including when read")
    source = turns[0]["source"]
    programs, added_tools, edits = {}, [], []
    for entity in sorted(entities):
        if source["exposure"].get(entity) is not False:
            raise CaseError("Exposure-only pilot requires an existing, recorded unexposed script")
        programs[entity] = compile_script(entity, definitions, source)
        added_tools.append(script_tool_definition(entity, definitions[entity], source))
        edits.append({"kind": "assist_exposure", "entity_id": entity, "before": False, "after": True})
    existing_names = {t["name"] for t in turns[0]["prepared"]["tools"]}
    if existing_names & {t["name"] for t in added_tools}:
        raise CaseError("Proposed script tool is already in the recorded catalogue")
    plan = {"format": 1, "kind": "expose_existing_scripts", "theme": deepcopy(theme),
            "hypothesis": hypothesis, "proposal_origin": "manual_pilot_seed",
            "evaluation_case_id": evaluation_case_id, "baseline_digest": digest(baseline),
            "baseline_plan": baseline_plan, "retained_turns": retained, "user_inputs": inputs,
            "judge": {"destination": deepcopy(case["destination"]), "original_user_turns": len(turns)},
            "edits": edits, "definitions": deepcopy(definitions),
            "definition_evidence": deepcopy(definition_evidence),
            "compiled_scripts": programs, "added_tools": added_tools,
            "coverage": [{"case_id": link["case_id"], "role": link["role"],
                          "evaluation": "selected_counterfactual" if link["case_id"] == evaluation_case_id
                          else "related_evidence_not_counterfactually_tested"} for link in theme["journeys"]],
            "counterfactual_allowance_used_by_this_plan": 1,
            "provider_option_changes": {}, "system_prompt_change": False,
            "semantic_review": "pending", "regression_review": "pending", "application": "not_implemented",
            "limits": ["A representative pass does not prove gains for every affected journey",
                       "Script definitions may be later observations; their provenance is retained",
                       "Static switch sequences only; no physical position or timing simulation",
                       "Native HA excludes scripts from static and GetLiveContext inventories; only tools are added",
                       "Remaining original tools are receipt-bound; unmodeled calls stop evaluation"]}
    plan["id"] = digest(plan)
    return plan


def verify_candidate(plan, cases, recording):
    expected = prepare_exposure_candidate(plan["theme"], cases, recording,
        evaluation_case_id=plan["evaluation_case_id"], hypothesis=plan["hypothesis"],
        entities=[e["entity_id"] for e in plan["edits"]], definitions=plan["definitions"],
        definition_evidence=plan["definition_evidence"])
    if plan != expected:
        raise CaseError("Candidate, grounding or baseline changed; prepare and review a new plan")
    return expected


def judge_effects(plan, world, *, used_user_turns):
    """Fixed command-level judge, independent of the candidate's tool responses."""
    criteria = plan["judge"]["destination"]["criteria"]
    targets = set(criteria.get("states", criteria.get("sequences", {})))
    if criteria["kind"] == "state":
        destination_met = all(world.states.get(e) == s for e, s in criteria["states"].items())
    else:
        destination_met = all([t["new"] for t in world.transitions if t["entity_id"] == e] == seq
                              for e, seq in criteria["sequences"].items())
    # A command to an unintended target counts even if its state did not change.
    unintended = sorted({c["entity_id"] for c in world.calls} - targets)
    reduced = 0 < used_user_turns < plan["judge"]["original_user_turns"]
    return {"command_destination_met": destination_met, "unintended_targets": unintended,
            "strictly_fewer_user_turns": reduced,
            "original_user_turns": plan["judge"]["original_user_turns"], "candidate_user_turns": used_user_turns,
            "command_check_passed": destination_met and reduced and not unintended,
            "physical_outcome": "not_verified_by_simulation", "semantic_review": "pending",
            "regression_review": "pending", "validated_improvement": False}


class SimulatedScriptTool(llm.Tool):
    def __init__(self, entity_id, definition, steps, api):
        self.name = definition["name"]
        self.description = definition["description"]
        self.integration = "script"
        self.parameters = probatio.Schema(RecordedParameters(definition["parameters"]))
        self.entity_id, self.steps, self.api = entity_id, deepcopy(steps), api

    async def async_call(self, hass, tool_input, llm_context):
        try:
            if tool_input.tool_args != {}:
                raise UnsupportedSimulation("This script has no declared arguments")
            result = execute_compiled(self.api.world, self.entity_id, self.steps)
            self.api.dispatched.append({"name": self.name, "args": {}, "provenance": "declarative_simulation"})
            return llm.ToolResult(**result)
        except CaseError as err:
            self.api.unsupported.append(str(err))
            raise


class CandidateTrial:
    """One representative, one exact original input; no retries or live writes."""
    def __init__(self, hass, plan, cases, recording, agent, *, evidence_kind):
        if evidence_kind not in {"scripted_transport_fixture", "model_counterfactual"}:
            raise CaseError("Distinguish scripted transport tests from actual model decisions")
        self.plan = deepcopy(verify_candidate(plan, cases, recording))
        self.evidence_kind = evidence_kind
        case = cases[plan["evaluation_case_id"]]
        self.log = FrozenTurnLog(hass, case, recording)
        self.agent = clone_provider(agent, plan["baseline_plan"])
        for entity, definition in zip(sorted(plan["compiled_scripts"]), plan["added_tools"], strict=True):
            self.log.replay_api.tools.append(SimulatedScriptTool(entity, definition,
                plan["compiled_scripts"][entity], self.log.replay_api))
        self.start = len(self.log.content)
        self.status, self.error, self.assessment = "prepared", None, None

    async def run(self, *, allow_paid_inference=False):
        if not allow_paid_inference:
            raise CaseError("Paid inference has not been authorized")
        if self.status != "prepared":
            raise CaseError("Candidate was already attempted; no automatic resampling")
        self.status = "running"
        try:
            await self.agent._async_handle_message(self.log.input, self.log)
            output = self.log.content[self.start:]
            if (not output or self.log.unresponded_tool_results or output[-1].role != "assistant"
                    or not output[-1].content or output[-1].tool_calls):
                raise UnsupportedSimulation("Candidate did not finish the original user turn")
            self.assessment = judge_effects(self.plan, self.log.replay_api.world, used_user_turns=1)
            self.status = "awaiting_review" if self.assessment["command_check_passed"] else "not_improved"
        except Exception as err:
            self.status = "unsupported" if isinstance(err, CaseError) else "infrastructure_failure"
            self.error = {"type": type(err).__name__}
            if isinstance(err, CaseError):
                self.error["message"] = str(err)
        return self.export()

    def export(self):
        return {"plan_id": self.plan["id"], "theme_id": self.plan["theme"]["id"],
                "evaluation_case_id": self.plan["evaluation_case_id"], "status": self.status,
                "evidence_kind": self.evidence_kind, "run_id": self.log.conversation_id,
                "input": self.log.input.text,
                "output": plain([c.as_dict() for c in self.log.content[self.start:]]),
                "simulated_calls": deepcopy(self.log.replay_api.world.calls),
                "transitions": deepcopy(self.log.replay_api.world.transitions),
                "external_calls": deepcopy(self.log.external_calls),
                "usage": self.log.usage_report(), "assessment": deepcopy(self.assessment),
                "error": deepcopy(self.error), "validated_improvement": False,
                "coverage": deepcopy(self.plan["coverage"]),
                "application": "not_implemented", "real_device_actions": 0}


def live_script_definitions(hass, entities):
    """Current UI/YAML script configs; later observations, never historical snapshots."""
    component = hass.data.get("script")
    definitions = {}
    for entity in entities:
        script = component.get_entity(entity) if component else None
        if script is None or not getattr(script, "raw_config", None):
            raise CaseError(f"Script {entity} has no readable configuration")
        definitions[entity] = deepcopy(dict(script.raw_config))
    return definitions, {"provenance": "Live script configuration read when the candidate was prepared",
                         "source_sha256": digest(definitions)}


class CandidateManager:
    """Prepare, run once, review, and accept by exposure; scripts are never executed."""
    def __init__(self, hass, casebook):
        self.hass, self.book = hass, casebook
        self.store = Store(hass, STORAGE_VERSION, f"{DOMAIN}.candidates", private=True, atomic_writes=True)
        self.items = {}

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

    async def _context(self, theme_evidence, evaluation_case_id):
        cases = {e["case_id"]: self.book.get(e["case_id"]) for e in theme_evidence}
        recording = await self.book.recorder.export(cases[evaluation_case_id]["recording_id"])
        return cases, recording

    async def prepare(self, *, title, friction, evidence, evaluation_case_id, hypothesis, scripts):
        async with self.book.lock:
            cases, recording = await self._context(evidence, evaluation_case_id)
            theme = create_theme(cases, title=title, friction=friction, observations=evidence,
                                 provenance="Administrator-grounded theme, not automated discovery")
            definitions, definition_evidence = live_script_definitions(self.hass, scripts)
            plan = prepare_exposure_candidate(theme, cases, recording, evaluation_case_id=evaluation_case_id,
                hypothesis=hypothesis, entities=scripts, definitions=definitions,
                definition_evidence=definition_evidence)
            if plan["id"] in self.items:
                return {**self.get(plan["id"]), "paid_inference_performed": False}
            item = await self._save({"plan": plan, "status": "prepared", "evidence": deepcopy(evidence),
                                     "result": None, "reviews": [], "application": None})
            return {**item, "paid_inference_performed": False}

    async def run(self, plan_id, *, allow_paid_inference=False):
        if not allow_paid_inference:
            raise CaseError("Paid inference has not been authorized")
        async with self.book.lock:
            item = self.get(plan_id)
            if item["status"] != "prepared":
                raise CaseError("Candidate was already attempted; no automatic resampling")
            plan = item["plan"]
            cases, recording = await self._context(item["evidence"], plan["evaluation_case_id"])
            definitions, _ = live_script_definitions(self.hass, [e["entity_id"] for e in plan["edits"]])
            if definitions != plan["definitions"]:
                raise CaseError("Scripts changed since preparation; prepare and review a new plan")
            agent = conversation.async_get_agent(self.hass, plan["baseline_plan"]["agent_id"])
            trial = CandidateTrial(self.hass, plan, cases, recording, agent, evidence_kind="model_counterfactual")
            await self._save({**item, "status": "running"})  # Persist the attempt BEFORE inference.
            item["result"] = await trial.run(allow_paid_inference=True)
            item["status"] = item["result"]["status"]
            return await self._save(item)

    async def review(self, plan_id, *, destination_reached, no_regressions_expected, feedback):
        async with self.book.lock:
            item = self.get(plan_id)
            if item["status"] != "awaiting_review":
                raise CaseError("No candidate result is awaiting review")
            if not feedback.strip():
                raise CaseError("Record what the reviewer checked")
            item["reviews"].append({"destination_reached": destination_reached,
                                    "no_regressions_expected": no_regressions_expected, "feedback": feedback})
            recommended = destination_reached and no_regressions_expected
            item["status"] = "recommended" if recommended else "not_recommended"
            return await self._save(item)

    async def apply(self, plan_id, *, accepted_by):
        """Three-way exposure merge: proposal base, proposal, current HA setting."""
        async with self.book.lock:
            item = self.get(plan_id)
            if item["status"] != "recommended":
                raise CaseError("Only a reviewed, recommended candidate can be accepted")
            outcomes = []
            for edit in item["plan"]["edits"]:
                current = async_should_expose(self.hass, "conversation", edit["entity_id"])
                if current == edit["after"]:
                    outcome = "already_present"
                elif current == edit["before"]:
                    async_expose_entity(self.hass, "conversation", edit["entity_id"], edit["after"])
                    applied = async_should_expose(self.hass, "conversation", edit["entity_id"])
                    outcome = "applied" if applied == edit["after"] else "failed"
                else:
                    outcome = "conflict"
                outcomes.append({**edit, "outcome": outcome})
            complete = all(o["outcome"] in {"applied", "already_present"} for o in outcomes)
            item["application"] = {"accepted_by": accepted_by, "edits": outcomes,
                                   "status": "applied" if complete else "partial_or_conflicted",
                                   "scripts_executed": 0}
            item["status"] = "applied" if complete else "application_incomplete"
            return await self._save(item)
