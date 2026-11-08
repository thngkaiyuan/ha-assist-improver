"""Immutable recording references: the turns as a replay reads them, and what stops a replay; no model or HA
actions."""

from copy import deepcopy
from hashlib import sha256
import json
import re


CLOSED = ("session_expired", "interrupted", "capture_timed_out")  # A conversation Assist has closed.


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


def _result(message):
    """Home Assistant's result in a tool_result message, {data, ...}: before 2026.10 only its data was written, as
    tool_result."""
    return message["result"] if "result" in message else {"data": message.get("tool_result")}


def _failed(item):
    """Whether a message is a step Home Assistant answered with an error: its result's data names one."""
    return (item["role"] == "tool_result" and isinstance(result := _result(item), dict)
            and isinstance(result.get("data"), dict) and "error" in result["data"])


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
    args, error = call.get("tool_args") or {}, _result(result)["data"]
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
        elif turns and _failed(item):
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
    return sum(1 for item in _content(recording, False) if _failed(item))


def recording_turns(recording, replay=True):
    """Join final ChatLog to source snapshots without inferring user intent.

    replay=False is for analysis: it also accepts turns Home Assistant handled itself, without the AI model
    (no prepared context; marked handled_locally), which a replay could not reproduce.

    Each turn's source is the recording's own home snapshot, not a copy (megabytes, and this runs on the event loop),
    and its actions the recording's own call log (None where it kept none, P8.2.10): readers only read them, from a
    recording that is already their own copy (Recorder.export).
    """
    if recording.get("format") != 1:
        raise CaseError("Unsupported recording format")
    if recording.get("status") not in CLOSED:
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
        exchange.update(number=number, source=snapshot["source"], handled_locally=bool(local),
                        prepared=deepcopy(snapshot.get("prepared")), history=deepcopy(history),
                        calls=deepcopy(calls), results=deepcopy(results), actions=snapshot.get("actions"),
                        final_reply=finals[-1]["content"], ended=finals[-1]["created"])
    return exchanges


def calls_in(messages):
    """Assist's calls in one turn's messages (as recorded, or a replay step's output), each with Home Assistant's
    result: [{tool, args, result}]."""
    asked = {c.get("id"): c for m in messages if m.get("role") == "assistant" for c in m.get("tool_calls") or []}
    return [{"tool": m.get("tool_name"), "args": (asked.get(m.get("tool_call_id")) or {}).get("tool_args") or {},
             "result": _result(m)} for m in messages if m.get("role") == "tool_result"]


def replayable_turns(recording):
    """The recording's turns as a replay reads them, or [] when it can't be replayed (a turn Home Assistant handled
    itself, without the AI model): there's nothing to run, so no facts from it."""
    try:
        return recording_turns(recording)
    except CaseError:
        return []


def replay_readiness(recording):
    """What stops a replay of the whole conversation, reported before anything is spent (gaps), and what limits it
    (notes). A replay always starts at the first turn and makes its own history from there, on today's setup."""
    turns = recording_turns(recording)
    gaps, notes = [], ["replay_from_start_carries_its_own_native_history; no_historical_native_backfill"]
    if any(not t["prepared"]["context"].get("context", {}).get("id") for t in turns):
        gaps.append("structured_request_identity_missing")
    if any(c.get("external") for t in turns for c in t["calls"]):
        notes.append("hosted_tools_run_at_the_provider; their_receipts_are_outside_receipts")
    return {"model_baseline_ready": not gaps, "gaps": gaps, "notes": notes, "paid_inference_performed": False}


def started_from(source):
    """The home and time a conversation started from (its first turn's source): the moment every replay of it starts
    from too, on today's setup (P0.1)."""
    return {"at": source.get("at"), "home": digest(source.get("states") or {})}
