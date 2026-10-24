"""Grounded journeys and immutable recording references; no model or HA actions."""

from copy import deepcopy
from hashlib import sha256
import json

import homeassistant.const as ha_const

from .const import TESTED_PROVIDERS


class CaseError(ValueError):
    """Evidence is incomplete, inconsistent, or outside the supported slice."""


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False).encode()).hexdigest()


def _content(recording, replay):
    """The conversation's messages: the final ChatLog, or for analysis, rebuilt from the recorded events when
    the final copy is stale (HA deletes a locally handled conversation before the recorder sees it again)."""
    content = recording.get("chat_log", {}).get("content", [])
    users = sum(1 for m in content if m["role"] == "user")
    if replay or users >= len(recording.get("turns", [])):
        return content
    rebuilt = []
    for event in recording.get("events", []):
        if event["type"] == "created":
            rebuilt = list((event.get("data") or {}).get("chat_log", {}).get("content", []))
        elif event["type"] == "content_added":
            rebuilt.append(event["data"]["content"])
    return rebuilt


def recording_turns(recording, replay=True):
    """Join final ChatLog to source snapshots without inferring user intent.

    replay=False is for analysis: it also accepts turns Home Assistant handled itself, without the AI model
    (no prepared context; marked handled_locally), which a replay could not reproduce.
    """
    if recording.get("format") != 1:
        raise CaseError("Unsupported recording format")
    if recording.get("status") not in {"session_expired", "interrupted", "capture_timed_out"}:
        raise CaseError("Recording is still active")
    exchanges = []
    for item in _content(recording, replay):
        if item["role"] == "user":
            exchanges.append({"messages": [deepcopy(item)]})
        elif exchanges:
            exchanges[-1]["messages"].append(deepcopy(item))
    snapshots = recording.get("turns", [])
    if replay and len(exchanges) < len(snapshots) and any(e["type"] == "deleted" for e in recording.get("events", [])):
        raise CaseError("Home Assistant handled this conversation itself, without the AI model; there is nothing to replay")
    if len(exchanges) != len(snapshots) or not exchanges:
        raise CaseError("User turns and source snapshots do not align")
    for number, (exchange, snapshot) in enumerate(zip(exchanges, snapshots, strict=True), 1):
        if exchange["messages"][0]["content"] != snapshot["user"]["content"]:
            raise CaseError("User text differs between recording views")
        preparations = snapshot.get("preparations", [])
        local = not replay and snapshot.get("source") and not preparations and not snapshot.get("prepared")
        if not local and (not preparations or not snapshot.get("prepared") or not snapshot.get("source")):
            raise CaseError(f"Turn {number} lacks prepared context or source state")
        history = [] if local else preparations[0].get("content", [])
        expected = [t["user"]["content"] for t in snapshots[:number]]
        if not local and ([m["content"] for m in history if m["role"] == "user"] != expected
                          or not history or history[-1]["role"] != "user"
                          or history[0]["role"] != "system"):
            raise CaseError(f"Turn {number} has incomplete or future-contaminated context")
        if any(m.get("attachments") for m in history):
            raise CaseError("Attachments are not supported by this replay slice")
        finals = [m for m in exchange["messages"] if m["role"] == "assistant"
                  and m.get("content") and not m.get("tool_calls")]
        if not finals:
            raise CaseError(f"Turn {number} has no complete final reply")
        calls = [c for m in exchange["messages"] for c in m.get("tool_calls", [])]
        results = [m for m in exchange["messages"] if m["role"] == "tool_result"]
        ids = [c["id"] for c in calls]
        result_ids = [r["tool_call_id"] for r in results]
        if len(ids) != len(set(ids)) or len(result_ids) != len(set(result_ids)) or set(ids) != set(result_ids):
            raise CaseError(f"Turn {number} has missing or duplicate tool results")
        exchange.update(number=number, source=deepcopy(snapshot["source"]), handled_locally=bool(local),
                        prepared=deepcopy(snapshot.get("prepared")), history=deepcopy(history),
                        calls=deepcopy(calls), results=deepcopy(results),
                        final_reply=finals[-1]["content"], ended=finals[-1]["created"])
    if replay and recording.get("practice"):
        # A practice turn's snapshot holds the real states; the assistant saw them with practice's earlier
        # simulated changes on top.
        for exchange in exchanges:
            states = exchange["source"]["states"]
            for change in recording["practice"]["simulated_changes"]:
                if change["at"] < exchange["source"]["at"] and change["entity_id"] in states and change.get("new"):
                    states[change["entity_id"]] = deepcopy(change["new"])
    return exchanges


def validate_criteria(criteria, source):
    """The pilot judges state/command evidence, never invents physical position."""
    kind = criteria.get("kind")
    expected_keys = {"kind", "states"} if kind == "state" else {"kind", "sequences"}
    if kind not in {"state", "transition_sequence"} or set(criteria) != expected_keys:
        raise CaseError("Use a state or transition_sequence command-level criterion")
    targets = criteria.get("states" if kind == "state" else "sequences")
    if not isinstance(targets, dict) or not targets:
        raise CaseError("Destination needs explicit recorded targets")
    for entity, value in targets.items():
        if entity not in source["states"]:
            raise CaseError(f"Destination target has no recorded state: {entity}")
        if kind == "state" and not isinstance(value, str):
            raise CaseError("Expected state must be a string")
        if kind == "transition_sequence" and (
                not isinstance(value, list) or not value or any(v not in {"on", "off"} for v in value)):
            raise CaseError("Expected transitions must be a nonempty on/off sequence")


def create_case(recording, *, title, first_turn, last_turn, destination=None,
                criteria=None, evidence=None, intervention_turns=None, goal_unmet=False, acceptable=()):
    """Explicit grounding input, not an automatic destination inference."""
    turns = recording_turns(recording)
    if (type(first_turn) is not int or type(last_turn) is not int
            or not 1 <= first_turn <= last_turn <= len(turns)):
        raise CaseError("Invalid journey turn range")
    selected = list(range(first_turn, last_turn + 1))
    interventions = intervention_turns or []
    if (any(type(n) is not int or n not in selected[1:] for n in interventions)
            or len(set(interventions)) != len(interventions)):
        raise CaseError("Interventions must refer to distinct subsequent journey turns")
    established = bool(destination and criteria and evidence)
    if any([destination, criteria, evidence]) and not established:
        raise CaseError("An established destination requires summary, criteria and evidence")
    if established:
        validate_criteria(criteria, turns[first_turn - 1]["source"])
    key = digest({"recording": digest(recording), "turns": selected})
    return {"format": 1, "id": key, "recording_id": recording["id"],
            "recording_digest": digest(recording), "title": title,
            "turns": selected, "original_user_turns": len(selected),
            "intervention_turns": sorted(interventions),
            # The friction may be the goal itself going unmet (settings guessed or missed), not extra turns.
            "goal_unmet": bool(goal_unmet),
            # Controls the user would accept being set alongside the goal (a car's climate when warming it up).
            "acceptable_devices": sorted(set(acceptable)),
            "destination": {"status": "established" if established else "needs_feedback",
                            "summary": destination, "criteria": deepcopy(criteria),
                            "evidence": deepcopy(evidence)},
            "feedback": [], "revision": 1, "audit": None,
            "baseline": {"status": "not_run", "attempts": 0},
            "question": None if established else "Help your assistant learn: what would you have liked to happen?"}


def verify_case(case, recording):
    if case.get("format") != 1 or case["recording_id"] != recording["id"] or case["recording_digest"] != digest(recording):
        raise CaseError("Case no longer matches its immutable recording")
    turns = recording_turns(recording)
    selected = case["turns"]
    if not selected or selected != list(range(selected[0], selected[-1] + 1)) or not 1 <= selected[0] <= selected[-1] <= len(turns):
        raise CaseError("Invalid case turn range")
    if case["destination"]["status"] != "established":
        raise CaseError("Destination needs feedback; no evaluation may begin")
    validate_criteria(case["destination"]["criteria"], turns[selected[0] - 1]["source"])
    return [turns[n - 1] for n in selected]


def retained_inputs(case, recording, turn_numbers):
    """Fetch exact original strings; no fabricated answers or reordered turns."""
    turns = verify_case(case, recording)
    allowed = case["turns"]
    if (not turn_numbers or any(type(n) is not int or n not in allowed for n in turn_numbers)
            or turn_numbers != sorted(set(turn_numbers))):
        raise CaseError("Inputs must be an ordered subsequence of original journey turns")
    if len(turn_numbers) >= len(allowed) and not case.get("goal_unmet"):
        # For an unmet goal, meeting it in as many turns is an improvement too.
        raise CaseError("Same or more user turns is not an improvement")
    by_number = {t["number"]: t for t in turns}
    return [by_number[n]["messages"][0]["content"] for n in turn_numbers]


def replay_readiness(case, recording):
    """Report gaps before spending; offline receipt playback does not fill them."""
    turns = verify_case(case, recording)
    gaps, notes = [], []
    running = ha_const.__version__
    if recording.get("core_version") != running:
        # The sandbox runs this HA's engine; the replay fidelity check shows whether the request still looks the same.
        notes.append(f"recorded_on_ha_{recording.get('core_version')}_replayed_on_ha_{running}")
    if turns[0]["number"] > 1 and any("provider_native_history_omitted" in x for x in recording.get("limitations", [])):
        gaps.append("provider_native_history_missing_at_cutoff")
    if any(not t["prepared"]["context"].get("context", {}).get("id") for t in turns):
        gaps.append("structured_request_identity_missing")
    configs = turns[0]["prepared"].get("provider_config", [])
    if len(configs) != 1:
        gaps.append("provider_configuration_missing_or_ambiguous")
    else:
        options = configs[0]["options"]
        if turns[0]["prepared"]["context"].get("platform") not in TESTED_PROVIDERS:
            notes.append("provider_not_yet_tested_for_replay")
        if any(t["prepared"].get("provider_config") != configs for t in turns):
            gaps.append("provider_configuration_changed_during_journey")
        if any(options.get(k) not in (None, False, "off") for k in ("code_execution", "web_fetch", "web_search")):
            notes.append("original_hosted_tools_preserved; provider_execution_is_not_intercepted_by_HA")
        if turns[0]["number"] == 1:
            notes.append("replay_from_start_carries_its_own_native_history; no_historical_native_backfill")
        # The product promises HA-level context, not an identical provider wire
        # request. Runtime model metadata is a fidelity note, not a new gate.
        if "provider_model_capabilities" not in turns[0]["prepared"]:
            notes.append("provider_runtime_capabilities_were_not_captured; exact_wire_identity_not_claimed")
        if any(options.get(k) not in (None, False, "off") for k in ("web_search", "web_fetch")) and any(
                key not in options for key in ("web_search_max_uses", "web_fetch_max_uses", "user_location")):
            notes.append("uncaptured_hosted_tool_limits_and_location_use_pinned_HA_defaults; old_capture_does_not_prove_their_original_values")
    if any(c.get("external") and c["tool_name"] != "tool_search_tool_bm25"
           for t in turns for c in t["calls"]):
        gaps.append("recorded_provider_tool_outside_catalogue_search_coverage")
    if any(c.get("external") for t in turns for c in t["calls"]):
        notes.append("tool_search_runs_in_provider_over_recorded_catalogue; receipts_are_not_replayed_as_functions")
    return {"model_baseline_ready": not gaps, "gaps": gaps, "fidelity_notes": notes,
            "paid_inference_performed": False}


def mark_baseline(case, status, *, evidence, transient=False):
    """Record a genuine trial result; deterministic failures cannot earn retries."""
    if case["destination"]["status"] != "established":
        raise CaseError("Destination is not established")
    previous = case["baseline"]
    if previous["status"] in {"reproduced", "not_reproducible", "unsupported"}:
        raise CaseError("Baseline is terminal; preserve the case for reference")
    if previous["status"] == "infrastructure_failure" and (not previous.get("transient") or previous["attempts"] >= 3):
        raise CaseError("Another baseline attempt is not useful or exceeds three attempts")
    if (status not in {"reproduced", "not_reproducible", "unsupported", "infrastructure_failure"}
            or not isinstance(evidence, dict) or evidence.get("kind") != "model_baseline"):
        raise CaseError("Baseline needs a supported status and explicit evidence")
    updated = deepcopy(case)
    updated["baseline"] = {"status": status, "attempts": previous["attempts"] + 1,
                           "evidence": deepcopy(evidence), "transient": bool(transient)}
    return updated
