"""Immutable recording references: the turns as a replay reads them, and what stops a replay; no model or HA
actions."""

from copy import deepcopy
from hashlib import sha256
import json
import re

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


# Assist's steps (its tool calls) in plain words; others are named from their own name ("HassLawnMowerDock": "Lawn
# mower dock").
STEPS = {"HassTurnOn": "Turn on", "HassTurnOff": "Turn off", "HassToggle": "Toggle", "HassLightSet": "Set light",
         "HassSetPosition": "Set position", "HassStopMoving": "Stop", "HassClimateSetTemperature": "Set temperature",
         "HassFanSetSpeed": "Set fan speed", "HassMediaPause": "Pause", "HassMediaUnpause": "Resume",
         "HassMediaNext": "Next track", "HassMediaPrevious": "Previous track", "HassSetVolume": "Set volume",
         "HassVacuumStart": "Start vacuum", "HassVacuumReturnToBase": "Send vacuum home",
         "HassVacuumCleanArea": "Vacuum an area", "HassBroadcast": "Broadcast", "HassGetState": "Check",
         "GetLiveContext": "Look up devices", "HassListAddItem": "Add to list", "HassStartTimer": "Start timer"}


def step(call, result):
    """A failed step in plain words: what Assist tried, on what (name, area, floor, domain, as it asked), and Home
    Assistant's error."""
    space, _, name = (call.get("tool_name") or result.get("tool_name") or "").rpartition("__")
    what = (f"Run script {name.replace('_', ' ')}" if space == "script" else STEPS.get(name)
            or " ".join(re.findall(r"[A-Z][a-z]*|[a-z]+|\d+", name.removeprefix("Hass")) or [name]).capitalize())
    args, error = call.get("tool_args") or {}, result["tool_result"]
    on = [", ".join(map(str, v)) if isinstance(v, list) else str(v)
          for v in (args.get(k) for k in ("name", "area", "floor")) if v]
    on += [", ".join(d.replace("_", " ") for d in (domain if isinstance(domain, list) else [domain]))
           for domain in [args.get("domain")] if domain]  # "media_player": media player.
    return {"step": " · ".join([what, *on]), "error": str(error.get("error_text") or error.get("error"))[:300]}


def transcript(recording):
    """What was said, turn by turn: the user's words, Assist's last reply to them ("" when there was none), and the
    steps that failed in between (failed, where any did). Any recording can be read this way, also one no replay
    could use."""
    turns, calls = [], {}
    for item in _content(recording, False):
        if item["role"] == "user":
            turns.append({"you": (item.get("content") or "").strip(), "assist": ""})
        elif turns and item["role"] == "assistant" and item.get("content") and not item.get("tool_calls"):
            turns[-1]["assist"] = item["content"].strip()
        elif item["role"] == "assistant":
            calls.update({call.get("id"): call for call in item.get("tool_calls") or []})
        elif (turns and item["role"] == "tool_result" and isinstance(item.get("tool_result"), dict)
              and "error" in item["tool_result"]):
            turns[-1].setdefault("failed", []).append(step(calls.get(item.get("tool_call_id")) or {}, item))
    return turns or [{"you": ((t.get("user") or {}).get("content") or "").strip(), "assist": ""}
                     for t in recording.get("turns", [])]


def replayed(turns):
    """A replay's steps (each turn's input and what Assist added to the conversation for it), read as transcript
    reads a recording."""
    return transcript({"chat_log": {"content": [item for t in turns for item in
                                                [{"role": "user", "content": t["input"]}, *(t.get("output") or [])]]}})


def failed_calls(recording):
    """How many of Assist's tool calls failed (Home Assistant answered with an error): a sign the user didn't get
    what they asked for."""
    return sum(1 for item in _content(recording, False)
               if item["role"] == "tool_result" and isinstance(item.get("tool_result"), dict) and "error" in item["tool_result"])


def recording_turns(recording, replay=True):
    """Join final ChatLog to source snapshots without inferring user intent.

    replay=False is for analysis: it also accepts turns Home Assistant handled itself, without the AI model
    (no prepared context; marked handled_locally), which a replay could not reproduce.
    """
    if recording.get("format") != 1:
        raise CaseError("Unsupported recording format")
    if recording.get("status") not in {"session_expired", "interrupted", "capture_timed_out"}:
        raise CaseError("Recording is still active")
    if replay and recording.get("compact"):  # Its home snapshot keeps only the devices it involved (retention.py).
        raise CaseError("This conversation was compacted to save space, so it can't be replayed")
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
        for exchange in exchanges:
            exchange["source"]["states"] = as_seen(recording, exchange["source"])
    return exchanges


def calls_in(messages):
    """Assist's calls in one turn's messages (as recorded, or a replay step's output), each with Home Assistant's
    result: [{tool, args, result}]."""
    asked = {c.get("id"): c for m in messages if m.get("role") == "assistant" for c in m.get("tool_calls") or []}
    return [{"tool": m.get("tool_name"), "args": (asked.get(m.get("tool_call_id")) or {}).get("tool_args") or {},
             "result": m["result"] if "result" in m else {"data": m.get("tool_result")}}  # tool_result: before 2026.10.
            for m in messages if m.get("role") == "tool_result"]


def as_seen(recording, source):
    """A turn's snapshot states as the assistant saw them: a practice turn's snapshot holds the real states, which it
    saw with practice's earlier simulated changes on top."""
    states = deepcopy(source.get("states") or {})
    for change in (recording.get("practice") or {}).get("simulated_changes") or []:
        if change["at"] < source["at"] and change["entity_id"] in states and change.get("new"):
            states[change["entity_id"]] = deepcopy(change["new"])
    return states


def replayable_turns(recording):
    """The recording's turns as a replay reads them, or [] when it can't be replayed (a turn Home Assistant handled
    itself, without the AI model): there's nothing to run, so no facts from it."""
    try:
        return recording_turns(recording)
    except CaseError:
        return []


def replay_readiness(recording):
    """What stops a replay of the whole conversation, reported before anything is spent (gaps), and what limits how
    faithful it is (fidelity_notes); offline receipt playback does not fill them. A replay always starts at the first
    turn and makes its own history from there."""
    turns = recording_turns(recording)
    gaps, notes = [], []
    running = ha_const.__version__
    if recording.get("core_version") != running:
        # The sandbox runs this HA's engine; the replay fidelity check shows whether the request still looks the same.
        notes.append(f"recorded_on_ha_{recording.get('core_version')}_replayed_on_ha_{running}")
    if any(not t["prepared"]["context"].get("context", {}).get("id") for t in turns):
        gaps.append("structured_request_identity_missing")
    if not {"scripts", "automations"} <= turns[0]["source"].keys():
        # Replayed on today's scripts, it wouldn't be the conversation as it was (P0.1).
        gaps.append("recorded_before_definitions_were_kept")
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


def started_from(source):
    """The home and time a conversation started from (its first turn's source): what every replay of it starts from
    too (P0.1)."""
    return {"at": source.get("at"), "home": digest(source.get("states") or {})}
