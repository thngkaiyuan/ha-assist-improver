"""Bounded baseline trials through the existing HA provider, never a new client.

Preparation is read-only. Every model step requires explicit paid-call consent.
The pilot pauses for a semantic review between original user inputs; it never
invents an answer or samples again to obtain a convenient baseline.
"""

from collections import Counter
from copy import copy, deepcopy
from dataclasses import is_dataclass, replace
from types import MappingProxyType

from homeassistant.components import conversation
from homeassistant.const import __version__ as CORE_VERSION

from .cases import CaseError, digest, replay_readiness, verify_case
from .const import REPLAY_ENGINE, TESTED_PROVIDERS
from .replay import FrozenTurnLog
from .serialization import plain
from .simulation import audit_recorded_case, destination_met, per_entity, recorded_transitions


def prepare_plan(case, recording):
    turns = verify_case(case, recording)
    readiness = replay_readiness(case, recording)
    if not readiness["model_baseline_ready"]:
        raise CaseError("Replay is unsupported: " + ", ".join(readiness["gaps"]))
    # Whether the recording itself reached the destination is information, not part of the plan: a baseline
    # reproduces the friction, reached or not, and a candidate must reach it.
    audit_recorded_case(case, recording)
    provider = turns[0]["prepared"]["provider_config"][0]
    agent_ids = {m["agent_id"] for t in turns for m in t["messages"] if m["role"] == "assistant"}
    if len(agent_ids) != 1:
        raise CaseError("Conversation provider identity is missing or ambiguous")
    platform = turns[0]["prepared"]["context"].get("platform")
    # What a turn's effects are compared with: the real changes HA recorded; for a practice session, what today's
    # engine does with the recorded calls (the practice engine of the recording day may have emulated less).
    expected_effects = "recorded_calls_rerun" if recording.get("practice") else "recorded_changes"
    plan = {"case_id": case["id"], "case_revision": case["revision"],
            "recording_digest": case["recording_digest"], "core_version": CORE_VERSION,
            "agent_id": agent_ids.pop(), "provider": deepcopy(provider), "platform": platform,
            "expected_effects": expected_effects, "replay_engine": REPLAY_ENGINE,
            "provider_tested": platform in TESTED_PROVIDERS,
            "turns": case["turns"], "user_inputs": [t["messages"][0]["content"] for t in turns],
            "prompt_digests": [digest(t["history"][0]["content"]) for t in turns],
            "tool_digests": [digest(t["prepared"]["tools"]) for t in turns],
            "provider_option_changes": {}, "candidate_trials": 0,
            "review_between_user_inputs": True, "automatic_baseline_retries": 0,
            "provider_request_round_limit_per_turn": 10,
            "transport_retries": "The provider integration's own SDK retry defaults; no added retry loop",
            "notes": readiness["fidelity_notes"],
            "effect_boundary": "HA's own tools act on a simulated copy of the recorded states; provider tools retain original configuration",
            "external_limit": "Non-search server tools may execute remotely before being reported; if used, this evaluator stops as unsupported"}
    plan["id"] = digest(plan)
    return plan


def clone_provider(agent, plan):
    """Copy per-run options onto the user's own assistant; keep HA's provider code and client.

    Anthropic and OpenRouter are tested. Other providers get the same copy and run labelled as not yet tested
    (plan["provider_tested"]); a provider that caches options outside its subentry may not see a candidate's change.
    """
    provider = plan["provider"]
    if (agent.entry.entry_id != provider["config_entry_id"]
            or agent.subentry.subentry_id != provider["subentry_id"]):
        raise CaseError("Selected HA agent does not match the recorded provider entry")
    options = deepcopy(provider["options"])
    clone = copy(agent)
    if is_dataclass(agent.subentry):
        clone.subentry = replace(agent.subentry, data=MappingProxyType(options))
    else:  # In-process fixture subentry; production uses HA's frozen dataclass.
        clone.subentry = copy(agent.subentry)
        clone.subentry.data = MappingProxyType(options)
    module = type(agent).__module__
    if module == "homeassistant.components.anthropic.conversation":
        model = options.get("chat_model")
        if not model:
            raise CaseError("Recorded model is missing")
        model_info, found = agent.entry.runtime_data.get_model_info(model)
        if not found:
            raise CaseError("Recorded model has no cached provider metadata; do not silently substitute a model")
        clone.model_info = model_info
    elif module == "homeassistant.components.open_router.conversation":
        if not options.get("model"):
            raise CaseError("Recorded model is missing")
        clone.model = options["model"]  # OpenRouter reads its model at setup, not from the subentry per message.
    return clone


def _calls_key(calls):
    return Counter(digest({"name": c["tool_name"], "args": c["tool_args"]}) for c in calls if not c.get("external"))


class BaselineTrial:
    def __init__(self, hass, case, recording, agent):
        self.plan = prepare_plan(case, recording)
        self.turns = verify_case(case, recording)
        self.recording, self._rerun = recording, None
        criteria = self.criteria = case["destination"]["criteria"]
        self.goal_devices = set(criteria.get("states", criteria.get("sequences", {})))
        self.destination_met = None  # Whether the replay without a fix met the goal, once it's reproduced.
        self.agent = clone_provider(agent, self.plan)
        self.log = FrozenTurnLog(hass, case, recording)
        self.index = 0
        self.status = "prepared"
        self.steps = []
        self.error = None
        self.reviews = []

    async def _expected(self, turn):
        if self.plan.get("expected_effects") != "recorded_calls_rerun":
            return recorded_transitions(self.recording, turn)
        if self._rerun is None:
            from .evidence import recorded_call_effects
            from .snapshot import live_definitions
            source = self.turns[0]["source"]
            scripts, automations = ((source["scripts"], source.get("automations", []))
                                    if "scripts" in source else live_definitions(self.log.hass))
            self._rerun = await recorded_call_effects(self.recording,
                                                      {"scripts": scripts, "automations": automations}) or {}
        return self._rerun.get(turn["number"], [])

    def export(self):
        return {"status": self.status, "run_id": self.log.conversation_id,
                "plan": deepcopy(self.plan), "next_turn": self.turns[self.index]["number"],
                "steps": deepcopy(self.steps), "reviews": deepcopy(self.reviews),
                "error": deepcopy(self.error), "attempts": 1, "destination_met": self.destination_met,
                "usage": self.log.usage_report(),
                "effect_boundary": self.plan["effect_boundary"]}

    async def step(self, *, allow_paid_inference=False, expected_turn):
        if not allow_paid_inference:
            raise CaseError("Paid inference has not been authorized")
        turn = self.turns[self.index]
        if expected_turn != turn["number"] or self.status not in {"prepared", "ready_for_next_turn"}:
            raise CaseError("Trial is not ready for this turn; refresh its status")
        self.status = "running"
        start = len(self.log.content)
        transition_start = len(self.log.replay_api.world.transitions)
        try:
            await self.agent._async_handle_message(self.log.input, self.log)
            new_content = self.log.content[start:]
            if (not new_content or self.log.unresponded_tool_results
                    or new_content[-1].role != "assistant" or not new_content[-1].content
                    or new_content[-1].tool_calls):
                raise CaseError("Provider did not finish this turn")
            actual = plain([c.as_dict() for c in new_content])
            calls = [c for m in actual for c in m.get("tool_calls", [])]
            # The friction still happens: the goal's devices get the same effects as recorded, however the model
            # phrased its calls. The model may vary elsewhere (another device, a question instead of an action);
            # those differences are kept as notes, not a reason to stop.
            actual_transitions = self.log.replay_api.world.transitions[transition_start:]
            expected = await self._expected(turn)
            actual_by, expected_by = per_entity(actual_transitions), per_entity(expected)
            matches = all(actual_by.get(e, []) == expected_by.get(e, []) for e in self.goal_devices)
            elsewhere = {e: {"replay": [list(c) for c in actual_by.get(e, [])],
                             "recorded": [list(c) for c in expected_by.get(e, [])]}
                         for e in sorted((set(actual_by) | set(expected_by)) - self.goal_devices)
                         if actual_by.get(e, []) != expected_by.get(e, [])}
            self.steps.append({"turn": turn["number"], "input": self.log.input.text,
                               "output": actual, "original_reply": turn["final_reply"],
                               "ha_effects_match": matches, "same_tool_calls": _calls_key(calls) == _calls_key(turn["calls"]),
                               "differences_elsewhere": elsewhere,
                               "transitions": deepcopy(actual_transitions), "recorded_transitions": expected,
                               "semantic_review": "pending"})
            self.status = "awaiting_review" if matches else "not_reproducible"
        except Exception as err:
            self.status = "unsupported" if isinstance(err, CaseError) else "infrastructure_failure"
            self.error = {"type": type(err).__name__}
            if isinstance(err, CaseError):
                self.error["message"] = str(err)
            self.steps.append({"turn": turn["number"], "input": self.log.input.text,
                               "output": plain([c.as_dict() for c in self.log.content[start:]]),
                               "external_calls": deepcopy(self.log.external_calls),
                               "incomplete": True})
        if self.steps[-1].get("turn") == turn["number"]:
            self.steps[-1]["prompt_matches_recording"] = self.log.prompt_matches_recording
        if self.status not in ("awaiting_review", "ready_for_next_turn"):
            await self.log.close()
        return self.export()

    def review(self, *, expected_turn, reproduced, feedback):
        if self.status != "awaiting_review" or expected_turn != self.turns[self.index]["number"]:
            raise CaseError("No matching turn is awaiting review")
        if not feedback.strip():
            raise CaseError("Record why the response and next original input are or are not compatible")
        self.reviews.append({"turn": expected_turn, "reproduced": reproduced, "feedback": feedback})
        self.steps[-1]["semantic_review"] = "accepted" if reproduced else "rejected"
        if not reproduced:
            self.status = "not_reproducible"
        elif self.index == len(self.turns) - 1:
            self.status = "reproduced"
            world = self.log.replay_api.world
            self.destination_met = destination_met(self.criteria, world.states, world.transitions)
        if self.status in ("not_reproducible", "reproduced"):
            self.log.hass.async_create_task(self.log.close(), "assist_improver_close_replay_sandbox")
        else:
            try:
                self.log.advance(self.turns[self.index + 1],
                                 check_sources=self.plan.get("expected_effects") != "recorded_calls_rerun")
            except CaseError as err:
                self.status = "unsupported"
                self.error = {"type": type(err).__name__, "message": str(err)}
                self.log.hass.async_create_task(self.log.close(), "assist_improver_close_replay_sandbox")
            else:
                self.index += 1
                self.status = "ready_for_next_turn"
        return self.export()


class BaselineManager:
    """Native admin-action orchestration; review never causes an inference call."""
    def __init__(self, hass, casebook):
        self.hass = hass
        self.book = casebook
        self.trials = {}

    async def prepare(self, case_id):
        case = self.book.get(case_id)
        recording = await self.book.recorder.export(case["recording_id"])
        plan = prepare_plan(case, recording)
        clone_provider(conversation.async_get_agent(self.hass, plan["agent_id"]), plan)
        return {"plan": plan, "paid_inference_performed": False}

    async def start(self, case_id, *, plan_id, allow_paid_inference=False):
        if not allow_paid_inference:
            raise CaseError("Paid inference has not been authorized")
        async with self.book.lock:
            case = self.book.get(case_id)
            recording = await self.book.recorder.export(case["recording_id"])
            plan = prepare_plan(case, recording)
            if plan["id"] != plan_id:
                raise CaseError("Prepared plan changed; inspect the new plan before spending")
            if case["baseline"]["status"] != "not_run":
                if case["baseline"].get("plan", {}).get("id") == plan["id"]:
                    raise CaseError("Baseline already attempted; no automatic resampling")
                # A different plan is a different test (how it's judged changed), not a resample: keep the old one.
                case["baseline_history"] = [*case.get("baseline_history", []), case["baseline"]]
                case["baseline"] = {"status": "not_run", "attempts": 0}
            agent = conversation.async_get_agent(self.hass, plan["agent_id"])
            trial = BaselineTrial(self.hass, case, recording, agent)
            case["baseline"] = {**trial.export(), "status": "running"}
            await self.book._save(case)  # Persist attempt BEFORE any inference.
            self.trials[case_id] = trial
            case["baseline"] = await trial.step(allow_paid_inference=True, expected_turn=trial.turns[0]["number"])
            return await self.book._save(case)

    async def continue_trial(self, case_id, *, expected_turn, allow_paid_inference=False):
        if not allow_paid_inference:
            raise CaseError("Paid inference has not been authorized")
        async with self.book.lock:
            case, trial = self._active(case_id)
            if trial.status != "ready_for_next_turn" or expected_turn != trial.turns[trial.index]["number"]:
                raise CaseError("No reviewed continuation is ready")
            case["baseline"] = {**trial.export(), "status": "running"}
            await self.book._save(case)
            case["baseline"] = await trial.step(allow_paid_inference=True, expected_turn=expected_turn)
            return await self.book._save(case)

    async def review(self, case_id, **review):
        async with self.book.lock:
            case, trial = self._active(case_id)
            # Review may advance the in-memory log. If storage fails, a retry
            # must see the same pending turn, not a half-accepted continuation.
            checkpoint = (trial.index, trial.status, deepcopy(trial.steps),
                deepcopy(trial.error), deepcopy(trial.reviews), trial.log)
            # No live HA objects/native provider history need a deep copy.
            trial.log = copy(trial.log)
            trial.log.content = list(trial.log.content)
            trial.log.replay_api = copy(trial.log.replay_api)
            trial.log.replay_api.world = deepcopy(trial.log.replay_api.world)
            try:
                case["baseline"] = trial.review(**review)
                return await self.book._save(case)
            except Exception:
                (trial.index, trial.status, trial.steps, trial.error,
                    trial.reviews, trial.log) = checkpoint
                raise

    def _active(self, case_id):
        case = self.book.get(case_id)
        if case_id not in self.trials:
            raise CaseError("Trial history is not in memory; preserve this attempt rather than silently restart it")
        trial = self.trials[case_id]
        if case["revision"] != trial.plan["case_revision"]:
            raise CaseError("Case feedback changed during this trial; do not continue the old plan")
        return case, trial
