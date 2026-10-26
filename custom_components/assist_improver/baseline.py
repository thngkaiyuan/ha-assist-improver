"""The baseline: a recorded conversation replayed through the household's own provider in a sandbox, turn by turn,
to check it reproduces what happened before a fix is tried (validation.run_baseline drives it). Never a new client;
preparation is read-only, a run needs paid-call consent, and nothing samples again to get a convenient baseline.
"""

import asyncio
from collections import Counter
from copy import copy, deepcopy
from dataclasses import is_dataclass, replace
from types import MappingProxyType

from homeassistant.components import conversation
from homeassistant.const import __version__ as CORE_VERSION

from .cases import CaseError, create_case, digest, recording_turns, replay_readiness, verify_case
from .const import REPLAY_ENGINE, TESTED_PROVIDERS
from .failures import closing
from .replay import FrozenTurnLog
from .records import Baseline
from .serialization import plain
from .simulation import audit_recorded_case, destination_met, per_entity, recorded_transitions
from .trace import span


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
    # These fields stay as they are, review_between_user_inputs included: the plan's ID is their digest, and a new ID
    # would run every baseline already paid for again.
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
    if case.get("unwanted"):  # Side effects to remove reproduce too, so the friction still happens (only then: IDs).
        plan["unwanted"] = sorted(case["unwanted"])
    plan["id"] = digest(plan)
    return plan


def clone_provider(agent, plan):
    """Copy per-run options onto the user's own assistant; keep HA's provider code and client.

    Anthropic and OpenRouter are tested. Other providers get the same copy and run labelled as not yet tested
    (plan["provider_tested"]); a provider that caches options outside its subentry may not see a candidate's change.
    """
    provider = plan["provider"]
    if agent is None:  # Removed, or its provider isn't loaded.
        raise CaseError("The assistant this conversation used is no longer set up, so it can't be replayed.")
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
    """One replay of a case's turns under its plan; it runs once, and its sandbox closes however it ends."""
    def __init__(self, hass, case, recording, agent):
        self.plan = prepare_plan(case, recording)
        self.turns = verify_case(case, recording)
        self.recording, self._rerun = recording, None
        criteria = self.criteria = case["destination"]["criteria"]
        # The goal's devices, and the side effects it left: both get their recorded effects, or it doesn't reproduce.
        self.unwanted = set(case.get("unwanted") or {})
        self.goal_devices = set(criteria.get("states", criteria.get("sequences", {}))) | self.unwanted
        self.destination_met = None  # Whether the replay without a fix met the goal, once it's reproduced.
        # Where the goal's own turns end: a replay on past them (to a later side effect) doesn't judge the goal.
        self.goal_last = case["turns"][0] + case.get("original_user_turns", len(case["turns"])) - 1
        self.agent = clone_provider(agent, self.plan)
        self.log = FrozenTurnLog(hass, case, recording)
        self.status = "prepared"
        self.steps = []
        self.error = None

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

    def export(self) -> Baseline:
        """The baseline record its case keeps."""
        return {"status": self.status, "plan": deepcopy(self.plan), "steps": deepcopy(self.steps),
                "error": deepcopy(self.error), "destination_met": self.destination_met,
                "usage": self.log.usage_report()}

    async def run(self, *, allow_paid_inference=False, progress=None) -> Baseline:
        """Replay every turn in order, moving on while the goal's devices get the recorded effects; stop at the first
        turn that doesn't (not_reproducible), can't be replayed (unsupported) or fails (infrastructure_failure).
        Reproduced once every turn has, with whether the goal was met. progress(record) is given the record so far
        before each later turn is paid for."""
        if not allow_paid_inference:
            raise CaseError("Paid inference has not been authorized")
        if self.status != "prepared":
            raise CaseError("Baseline already attempted; no automatic resampling")
        self.status = "running"
        try:
            at_goal = None  # Whether the goal was met where its own turns end.
            for index, turn in enumerate(self.turns):
                if index:
                    if progress is not None:
                        await progress(self.export())
                    try:
                        self.log.advance(turn, check_sources=self.plan.get("expected_effects") != "recorded_calls_rerun")
                    except CaseError as err:
                        self.status = "unsupported"
                        self.error = {"type": type(err).__name__, "message": str(err)}
                        break
                if not await self._turn(turn):
                    break
                if turn["number"] == self.goal_last:
                    world = self.log.replay_api.world
                    at_goal = destination_met(self.criteria, world.states, world.transitions)
            else:
                world = self.log.replay_api.world
                # Each side effect to remove happened in the replay too, or a fix can't be judged to leave it alone
                # (a change outside the turns replayed matches as no change at all).
                changed = {t["entity_id"] for step in self.steps for t in step.get("transitions", [])}
                if missing := sorted(self.unwanted - changed):
                    self.status = "unsupported"
                    self.error = {"type": "CaseError", "message": "The replay without a change didn't change "
                                  f"{', '.join(missing)}, so a fix can't be judged to leave it alone"}
                else:
                    self.status = "reproduced"
                    self.destination_met = at_goal if at_goal is not None else destination_met(
                        self.criteria, world.states, world.transitions)
        except asyncio.CancelledError:  # A cancelled run still closes its sandbox.
            self.status = "cancelled"
            raise
        finally:
            await self.log.close()
        return self.export()

    async def _turn(self, turn):
        """Replay one turn and keep it as a step; True when the goal's devices got the recorded effects."""
        start = len(self.log.content)
        transition_start = len(self.log.replay_api.world.transitions)
        try:
            async with span("replay.turn", replay="baseline", turn=turn["number"]):
                await self.agent._async_handle_message(self.log.input, self.log)
            new_content = self.log.content[start:]
            if (not new_content or self.log.unresponded_tool_results
                    or new_content[-1].role != "assistant" or not new_content[-1].content
                    or new_content[-1].tool_calls):
                raise CaseError("Provider did not finish this turn")
            actual = plain([c.as_dict() for c in new_content])
            calls = [c for m in actual for c in m.get("tool_calls", [])]
            # The friction still happens: the goal's devices (and side effects to remove) get the same effects as
            # recorded, however the model phrased its calls. The model may vary elsewhere (another device, a question
            # instead of an action); those differences are kept as notes, not a reason to stop. The replies are kept
            # for reading.
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
                               "transitions": deepcopy(actual_transitions), "recorded_transitions": expected})
            if not matches:
                self.status = "not_reproducible"
        except Exception as err:
            self.status = "unsupported" if isinstance(err, CaseError) else "infrastructure_failure"
            self.error = {"type": type(err).__name__}
            if isinstance(err, CaseError):
                self.error["message"] = str(err)
            self.steps.append({"turn": turn["number"], "input": self.log.input.text,
                               "output": plain([c.as_dict() for c in self.log.content[start:]]),
                               "external_calls": deepcopy(self.log.external_calls),
                               "incomplete": True})
        self.steps[-1]["prompt_matches_recording"] = self.log.prompt_matches_recording
        return self.status == "running"


class BaselineManager:
    """Runs a case's baseline, once per plan, and keeps it with the case. A trial (its provider history and sandbox)
    lives only while it runs: a reload in the middle leaves the attempt interrupted, never repeated."""
    def __init__(self, hass, casebook):
        self.hass = hass
        self.book = casebook

    async def prepare(self, case_id):
        case = self.book.get(case_id)
        recording = await self.book.recorder.export(case["recording_id"])
        plan = prepare_plan(case, recording)
        clone_provider(conversation.async_get_agent(self.hass, plan["agent_id"]), plan)
        return {"plan": plan, "paid_inference_performed": False}

    async def run(self, case_id, *, plan_id, allow_paid_inference=False):
        """Replay the case's conversation under the prepared plan, kept before any inference and after each turn;
        returns the case with its settled baseline. A baseline attempted under this plan never runs again; one
        under an older plan moves to baseline_history."""
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
            agent = conversation.async_get_agent(self.hass, plan["agent_id"])
            trial = BaselineTrial(self.hass, case, recording, agent)

            async def save(record):
                case["baseline"] = record
                return await self.book._save(case)
            # Kept before any inference and after each turn; closed however it ends.
            async with closing(save, {**trial.export(), "status": "running"}) as keep:
                return await keep(await trial.run(allow_paid_inference=True, progress=keep))


async def replay_recording(hass, recorder, recording_id, last_turn=1):
    """A recording's first turns replayed as a baseline would (the assistant it used, a sandbox in the replay
    worker), judged by whether the assistant changed the same devices the same way: for checking the replay engine
    end to end (a developer service). Paid: the assistant's own model. Nothing is stored."""
    from homeassistant.components import conversation
    from .simulation import recorded_transitions
    recording = await recorder.export(recording_id)
    case = create_case(recording, title="Developer replay", first_turn=1, last_turn=last_turn)
    states = {t["entity_id"]: t["new"] for turn in recording_turns(recording) if turn["number"] in case["turns"]
              for t in recorded_transitions(recording, turn)}
    if not states:
        raise CaseError("The recorded assistant changed no device in those turns, so there's nothing to compare with")
    case["destination"] = {"status": "established", "summary": "What the recorded assistant did",
                           "criteria": {"kind": "state", "states": states}, "evidence": {"kind": "developer_replay"}}
    trial = BaselineTrial(hass, case, recording, conversation.async_get_agent(hass, prepare_plan(case, recording)["agent_id"]))
    record = await trial.run(allow_paid_inference=True)
    return {"agent_id": trial.plan["agent_id"], "model": (trial.plan["provider"].get("options") or {}).get("chat_model"),
            **{k: v for k, v in record.items() if k != "plan"}}
