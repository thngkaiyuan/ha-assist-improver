"""What a recording shows the assistant did, and whether that established a case's destination.

Replays themselves run in a sandboxed HA instance (sandbox.py, replay.py).
"""

from .cases import CaseError, replay_readiness, verify_case


# The conversation machinery's own entities change with every turn; they aren't the home.
MACHINERY = {"conversation", "ai_task", "tts", "stt", "wake_word", "assist_satellite"}


class UnsupportedSimulation(CaseError):
    pass


def recorded_transitions(recording, turn):
    """The state changes the assistant made during one recorded turn: practice's simulated changes, or the real
    changes HA attributes to the turn's request."""
    start, end = turn["source"]["at"], turn["ended"]
    practice = recording.get("practice")
    if practice:
        changes = practice["simulated_changes"]
    else:
        request = (turn.get("prepared") or {}).get("context", {}).get("context", {}).get("id")
        changes = [c for c in recording.get("state_changes", [])
                   if request and request in ((c.get("new") or {}).get("context") or {}).values()]
    return [{"entity_id": c["entity_id"], "old": c["old"]["state"], "new": c["new"]["state"]}
            for c in changes if start <= c["at"] <= end and c.get("old") and c.get("new")
            and c["old"]["state"] != c["new"]["state"] and c["entity_id"].split(".", 1)[0] not in MACHINERY]


def destination_met(criteria, states, transitions):
    """Whether a replay's home meets a case's destination: its states at the end, or each device's sequence."""
    if criteria["kind"] == "state":
        return all(states.get(e) == s for e, s in criteria["states"].items())
    return all([t["new"] for t in transitions if t["entity_id"] == e] == seq for e, seq in criteria["sequences"].items())


def per_entity(transitions):
    return {e: [(t["old"], t["new"]) for t in transitions if t["entity_id"] == e]
            for e in {t["entity_id"] for t in transitions}}


def audit_recorded_case(case, recording):
    """Check the recorded changes establish the destination; never claim model reproduction."""
    turns = verify_case(case, recording)
    criteria = case["destination"]["criteria"]
    targets = set(criteria.get("states", criteria.get("sequences", {})))
    observed = [t for turn in turns for t in recorded_transitions(recording, turn)]
    if criteria["kind"] == "state":
        final = {e: turns[0]["source"]["states"][e]["state"] for e in targets}
        final.update({t["entity_id"]: t["new"] for t in observed if t["entity_id"] in targets})
        met = all(final[e] == state for e, state in criteria["states"].items())
    else:
        met = all([t["new"] for t in observed if t["entity_id"] == e] == sequence
                  for e, sequence in criteria["sequences"].items())
    unintended = sorted({t["entity_id"] for t in observed} - targets)
    return {"status": "recorded_changes_checked", "evidence_kind": "recorded_change_audit",
            "recording_digest": case["recording_digest"], "case_revision": case["revision"],
            "practice": bool(recording.get("practice")),
            "transitions": observed, "command_criteria_met": met and not unintended,
            "unintended_targets": unintended, "user_turns": len(turns),
            "physical_outcome": "See separately attributed owner observation; relay states alone do not prove position",
            "model_behavior_reproduced": False, "intervention_reduction_proven": False,
            "readiness": replay_readiness(case, recording)}
