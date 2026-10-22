"""Deterministic recorded-action checks in a private state dictionary.

This module cannot access hass, services, credentials, a network or a model.
It validates recorded HA on/off and context calls; it does not rediscover the
assistant's decisions, execute scripts, or infer physical garage positions.
"""

from copy import deepcopy

from .cases import CaseError, digest, replay_readiness, verify_case


class UnsupportedSimulation(CaseError):
    pass


class RecordedWorld:
    def __init__(self, source):
        self.source = deepcopy(source)
        self.states = {k: v["state"] for k, v in source["states"].items()}
        self.transitions = []
        self.calls = []

    def apply_recorded(self, call, receipt, source):
        """A recorded result is reusable only with checked applicability."""
        if call.get("external"):
            raise UnsupportedSimulation("External provider tools are receipts, not simulated functions")
        if call["id"] != receipt["tool_call_id"] or receipt["tool_name"] != call["tool_name"]:
            raise CaseError("Mismatched call and result")
        name, args = call["tool_name"], call["tool_args"]
        result = deepcopy(receipt["result"])
        if name == "homeassistant__GetLiveContext":
            matched = self._context_scope(args, source)
            for entity in matched:
                if self.states.get(entity) != source["states"][entity]["state"]:
                    raise UnsupportedSimulation("Recorded context result is stale in this simulated world")
            if not matched and not result["error"]:
                raise CaseError("Successful context receipt has no matching exposed state")
            if matched and result["error"]:
                raise UnsupportedSimulation("Context lookup error is not modeled for matching entities")
        elif name in {"intent__HassTurnOn", "intent__HassTurnOff"}:
            if set(args) != {"name", "domain"} or not isinstance(args["name"], str):
                raise UnsupportedSimulation("Only recorded named on/off targets are supported")
            data = result.get("data", {})
            if result.get("error") or data.get("response_type") != "action_done":
                raise UnsupportedSimulation("Failed control actions require an explicit failure model")
            successes = data.get("data", {}).get("success", [])
            if len(successes) != 1 or data["data"].get("failed"):
                raise UnsupportedSimulation("Only one successful named target per recorded call is supported")
            target = successes[0]
            entity = target.get("id")
            if (target.get("type") != "entity" or target.get("name") != args["name"]
                    or entity not in self.states or entity not in source["states"]
                    or source["exposure"].get(entity) is not True
                    or entity.split(".")[0] not in {"fan", "switch", "light"}
                    or args["domain"] != [entity.split(".")[0]]):
                raise UnsupportedSimulation("Target is unsupported, unexposed, or absent from recorded state")
            registry = source["entities"].get(entity, {})
            names = [source["states"][entity].get("attributes", {}).get("friendly_name"),
                     registry.get("name"), registry.get("original_name"), *registry.get("aliases", [])]
            if args["name"].casefold() not in [v.casefold() for v in names if isinstance(v, str)]:
                raise UnsupportedSimulation("Target name cannot be verified from the captured inventory")
            if self.states[entity] not in {"on", "off"}:
                raise UnsupportedSimulation("On/off effect is unknown from this source state")
            state = "on" if name.endswith("TurnOn") else "off"
            before = self.states[entity]
            self.calls.append({"tool": name, "args": deepcopy(args), "entity_id": entity, "state": state})
            if before != state:
                self.transitions.append({"entity_id": entity, "old": before, "new": state})
            self.states[entity] = state
        else:
            raise UnsupportedSimulation(f"No deterministic simulator for {name}")
        return result

    @staticmethod
    def _context_scope(args, source):
        if set(args) - {"domain", "area", "name"}:
            raise UnsupportedSimulation("Unsupported context query fields")
        domains = args.get("domain")
        if isinstance(domains, str):
            domains = [domains]
        matched = []
        for entity, state in source["states"].items():
            if source["exposure"].get(entity) is not True:
                continue
            if domains and entity.split(".")[0] not in domains:
                continue
            registry = source["entities"].get(entity, {})
            device = source.get("devices", {}).get(registry.get("device_id"), {})
            area = source.get("areas", {}).get(registry.get("area_id") or device.get("area_id"), {})
            if args.get("area") and args["area"].casefold() not in [str(x).casefold() for x in [area.get("name", ""), *area.get("aliases", [])]]:
                continue
            names = [state.get("attributes", {}).get("friendly_name"), registry.get("name"),
                     registry.get("original_name"), *registry.get("aliases", [])]
            if args.get("name") and args["name"].casefold() not in [x.casefold() for x in names if isinstance(x, str)]:
                continue
            matched.append(entity)
        return matched


def audit_recorded_case(case, recording):
    """Check tool receipts and state trajectories; never claim model reproduction."""
    turns = verify_case(case, recording)
    world = RecordedWorld(turns[0]["source"])
    criteria = case["destination"]["criteria"]
    targets = set(criteria.get("states", criteria.get("sequences", {})))
    external_receipts = []
    local_calls = 0
    for turn in turns:
        for entity in targets:
            if world.states.get(entity) != turn["source"]["states"][entity]["state"]:
                raise CaseError("Simulated trajectory disagrees with the next recorded source state")
        receipts = {r["tool_call_id"]: r for r in turn["results"]}
        for call in turn["calls"]:
            receipt = receipts[call["id"]]
            if call.get("external"):
                external_receipts.append({"tool_name": call["tool_name"],
                                          "result_digest": digest(receipt["result"]),
                                          "provenance": "recorded external receipt; not simulated"})
                continue
            result = world.apply_recorded(call, receipt, turn["source"])
            if result != receipt["result"]:
                raise CaseError("Simulated tool result differs from recorded receipt")
            local_calls += 1
    observed = {entity: [] for entity in targets}
    start, end = turns[0]["source"]["at"], turns[-1]["ended"]
    for change in recording.get("state_changes", []):
        entity = change["entity_id"]
        if (entity in targets and start <= change["at"] <= end and change.get("old") and change.get("new")
                and change["old"]["state"] != change["new"]["state"]):
            observed[entity].append((change["old"]["state"], change["new"]["state"]))
    simulated = {entity: [(t["old"], t["new"]) for t in world.transitions if t["entity_id"] == entity]
                 for entity in targets}
    if observed != simulated:
        raise CaseError("Simulated target transitions differ from recorded observations")
    if criteria["kind"] == "state":
        met = all(world.states[e] == state for e, state in criteria["states"].items())
    else:
        met = all([t["new"] for t in world.transitions if t["entity_id"] == e] == sequence
                  for e, sequence in criteria["sequences"].items())
    unintended = sorted({t["entity_id"] for t in world.transitions} - targets)
    return {"status": "recorded_actions_verified", "evidence_kind": "deterministic_receipt_audit",
            "recording_digest": case["recording_digest"], "case_revision": case["revision"],
            "local_tool_calls_checked": local_calls, "external_receipts": external_receipts,
            "transitions": world.transitions, "command_criteria_met": met and not unintended,
            "unintended_targets": unintended, "user_turns": len(turns),
            "physical_outcome": "See separately attributed owner observation; relay states alone do not prove position",
            "model_behavior_reproduced": False, "intervention_reduction_proven": False,
            "readiness": replay_readiness(case, recording)}
