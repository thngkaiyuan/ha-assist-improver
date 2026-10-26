"""Storage that doesn't grow without end, while every conversation stays in the regression set.

A recording keeps a full snapshot of the home for each turn (about a megabyte in a large home). Once a recording
closes it's packed, losslessly: later turns keep only what changed since the first turn's snapshot (rebuilt on
read), and chat-log copies nothing reads are dropped. Every day, older recordings nothing needs in full any more
are compacted: they keep the conversation, its tool calls, and the devices it involved (their states, names, areas
and exposure), not the rest of the home. A compacted recording is still read by every AI role, but no longer
replayed. Kept in full: recordings an improvement or a case was built from, recent ones (KEEP_FULL_DAYS), and the
newest KEEP_NEWEST, which seed the sandbox. Recordings with no turns (Home Assistant handled them itself) are
deleted.

The log of runs and AI role calls (diagnostics and profiling) is tidied the same way: what the pipeline still reads
stays (trimmed to those fields after KEEP_FULL_DAYS), runs with their stage timings and the test stages' tests per
goal (what holds each goal to its tests-per-goal limit; trimmed to that after KEEP_FULL_DAYS) stay for
KEEP_HISTORY_DAYS, and every other record is deleted after KEEP_FULL_DAYS.

The developer view's traces (trace.py) are kept TRACE_DAYS after they were last written, and no more than
TRACE_MAX_BYTES of them, the oldest deleted first; a trace still being written is never deleted.
"""

from copy import deepcopy
from datetime import timedelta

from homeassistant.util.dt import parse_datetime, utcnow

from .trace import journal

KEEP_FULL_DAYS = 30
KEEP_NEWEST = 20
KEEP_HISTORY_DAYS = 365
TRACE_DAYS = 7
TRACE_MAX_BYTES = 20 * 1024 * 1024
# What the pipeline reads from a record it still needs; the rest (raw replies, triage detail, token counts) is
# diagnostics, trimmed after KEEP_FULL_DAYS. A held verdict keeps its triage: the next analysis reuses it.
NEEDED_FIELDS = ("id", "kind", "role", "recording_id", "status", "answer_status", "analysis", "digest_sha256",
                 "entity_id", "started", "finished", "seconds", "error", "tested", "test_attempts", "run_id",
                 "trace_id")
# What a test stage's record keeps after KEEP_FULL_DAYS, for a year: each goal's tests, against its tests-per-goal limit.
TEST_FIELDS = ("id", "kind", "status", "started", "finished", "tried", "run_id", "trace_id")
READ_EVENTS = ("created", "content_added", "deleted")  # The events cases._content reads; the rest are copies.
CLOSED = ("session_expired", "interrupted", "capture_timed_out")


def _diff(base, source):
    """What changed in a snapshot since the first turn's: {key: {changed..., "__removed__": [...]}} per mapping."""
    out = {}
    for key, value in source.items():
        old = base.get(key)
        if isinstance(value, dict) and isinstance(old, dict):
            changed = {k: v for k, v in value.items() if k not in old or old[k] != v}  # New keys too, None or not.
            removed = [k for k in old if k not in value]
            if changed or removed:
                out[key] = {"changed": changed, **({"removed": removed} if removed else {})}
        elif old != value:
            out[key] = {"value": value}
    for key in base:
        if key not in source:
            out[key] = {"gone": True}
    return out


def _patch(base, changes):
    source = deepcopy(base)
    for key, change in changes.items():
        if change.get("gone"):
            source.pop(key, None)
        elif "value" in change:
            source[key] = deepcopy(change["value"])
        else:
            merged = dict(source.get(key) or {})
            merged.update(deepcopy(change["changed"]))
            for k in change.get("removed", []):
                merged.pop(k, None)
            source[key] = merged
    return source


def pack(recording):
    """A closed recording as stored: later turns' snapshots as changes from the first, unread copies dropped."""
    if recording.get("packed") or recording.get("status") not in CLOSED:
        return recording
    packed = deepcopy(recording)
    turns = packed.get("turns", [])
    if turns and "source" in turns[0]:
        base = turns[0]["source"]
        for turn in turns[1:]:
            if "source" in turn:
                turn["source_changes"] = _diff(base, turn.pop("source"))
    packed["events"] = [e if e.get("type") in READ_EVENTS else {"at": e.get("at"), "type": e.get("type")}
                        for e in packed.get("events", [])]
    packed["packed"] = 1
    return packed


def unpack(recording):
    """A recording as every reader expects it: each turn with its full snapshot."""
    if not recording or not recording.get("packed"):
        return recording
    turns = recording.get("turns", [])
    if turns and "source" in turns[0]:
        base = turns[0]["source"]
        for turn in turns[1:]:
            if "source_changes" in turn:
                turn["source"] = _patch(base, turn.pop("source_changes"))
    recording.pop("packed", None)
    return recording


def involved(recording):
    """The devices a conversation involved: those Assist acted on, and those a person or Assist changed while it
    went on (a change carrying a user or a parent context; sensors reporting by themselves don't count)."""
    windows = _windows(recording)
    found = {c["entity_id"] for c in recording.get("state_changes", [])
             if _caused(c) and any(_during(c.get("at"), w) for w in windows)}
    for message in (recording.get("chat_log") or {}).get("content", []):
        # HA's chat log writes the tool's result, and a copy of the bare intent response as tool_result.
        for result in (message.get("result"), message.get("tool_result")):
            found |= _targets(result)
    return found


def _targets(result):
    """The devices an intent's response says it acted on or failed on, at whichever depth it carries them."""
    if not isinstance(result, dict):
        return set()
    data = result.get("data")
    for layer in (data.get("data") if isinstance(data, dict) else None, data, result):
        if isinstance(layer, dict) and any(isinstance(layer.get(k), list) for k in ("success", "failed")):
            return {t.get("id") for k in ("success", "failed") for t in layer.get(k) or []
                    if isinstance(t, dict) and t.get("id")}
    return set()


def _caused(change):
    context = ((change.get("new") or {}).get("context") or {})
    return bool(context.get("user_id") or context.get("parent_id"))


def _windows(recording):
    """(start, end) of each turn: from the user's words until the next turn (or a minute after the last)."""
    starts = [parse_datetime(t["user"].get("created") or "") for t in recording.get("turns", [])
              if (t.get("user") or {}).get("created")]
    starts = [s for s in starts if s]
    return [(s, starts[i + 1] if i + 1 < len(starts) else s + timedelta(minutes=1)) for i, s in enumerate(starts)]


def _during(at, window):
    moment = parse_datetime(at or "")
    return bool(moment) and window[0] <= moment <= window[1]


def compact(recording, also=()):
    """An older recording kept as a digest: the conversation, its tool calls, and the devices it involved (plus
    `also`: those its analysis named), with their areas and devices; the rest of the home goes. Read by every role;
    never replayed."""
    recording = unpack(deepcopy(recording))
    keep = involved(recording) | set(also)
    for turn in recording.get("turns", []):
        source = turn.get("source")
        if not source:
            continue
        devices = {e.get("device_id") for k, e in (source.get("entities") or {}).items() if k in keep}
        for key in ("states", "entities", "exposure"):
            if isinstance(source.get(key), dict):
                source[key] = {k: v for k, v in source[key].items() if k in keep}
        if isinstance(source.get("devices"), dict):
            source["devices"] = {k: v for k, v in source["devices"].items() if k in devices}
        for key in ("scripts", "automations"):
            source.pop(key, None)
    recording["state_changes"] = [c for c in recording.get("state_changes", []) if c["entity_id"] in keep]
    recording["compact"] = True
    return pack(recording)


async def prune(recorder, referenced, now=None, named=None):
    """The daily tidy-up: delete recordings with no turns, pack closed ones not packed yet, and compact older ones
    nothing needs in full. referenced: recording IDs (or their first 8 characters) an improvement or a case uses;
    named: {recording ID: devices its analysis named}, kept when compacting. Returns what it did, for diagnostics."""
    now = now or utcnow()
    done = {"deleted": 0, "packed": 0, "compacted": 0}
    closed = sorted((k for k, s in recorder.index.items() if s.get("status") in CLOSED),
                    key=lambda k: recorder.index[k].get("started") or "", reverse=True)
    newest = {k for k in closed if recorder.index[k].get("user_turns")}
    newest = set(sorted(newest, key=lambda k: recorder.index[k].get("started") or "", reverse=True)[:KEEP_NEWEST])
    for key in closed:
        summary = recorder.index[key]
        if summary.get("user_turns") == 0:
            await recorder.delete(key)
            done["deleted"] += 1
            journal("info", "Recording deleted: no turns (Home Assistant handled it itself)", recording_id=key)
            continue
        started = parse_datetime(summary.get("started") or "")
        old = started is not None and now - started > timedelta(days=KEEP_FULL_DAYS)
        due = old and not (key in referenced or key[:8] in referenced or key in newest)
        if summary.get("stored") == "compact" or (summary.get("stored") == "packed" and not due):
            continue  # The index says how it's stored: nothing to load.
        if (data := await recorder.store(key).async_load()) is None:
            continue
        if due:
            await recorder.store(key).async_save(compact(data, (named or {}).get(key, ())))
            summary["stored"] = "compact"
            done["compacted"] += 1
            journal("info", f"Recording compacted: older than {KEEP_FULL_DAYS} days and nothing needs it in full",
                    recording_id=key)
        else:
            if not data.get("packed"):
                await recorder.store(key).async_save(pack(data))
                done["packed"] += 1
                journal("info", "Recording packed", recording_id=key)
            summary["stored"] = "packed"
    await recorder.save_index()
    return done


def needed_runs(runs, recordings):
    """Positions of the records the pipeline still reads, each with the fields it reads (None: the whole record).
    Anything still running, each existing conversation's latest analysis, unreadable marker, failed
    attempts (while it's not analysed) and held verdict, and the latest proposal with its hypotheses and the friction
    and sessions it was given (Pipeline.seen, unseen)."""
    needed, latest, analysed, settled = {}, {}, set(), set()
    for n, run in enumerate(runs):
        kind, status, rid = run.get("kind"), run.get("status"), run.get("recording_id")
        if status == "running":
            needed[n] = None
        elif kind == "hypothesis_generator" and status == "completed":
            latest["generator"] = n
        elif kind == "journey_analyzer" and rid in recordings:
            if status == "completed" and run.get("analysis"):
                latest[("analysis", rid)] = n
                analysed.add(rid)
            elif run.get("answer_status") == "unreadable":
                latest[("unreadable", rid)] = n
                settled.add(rid)
            elif status == "held":
                latest[("held", rid)] = n
    for key, n in latest.items():
        if key == "generator":
            needed[n] = NEEDED_FIELDS + ("output", "friction", "sessions")
        elif key[0] != "held" or key[1] not in analysed:
            needed[n] = None if key[0] == "held" else NEEDED_FIELDS
    for n, run in enumerate(runs):  # Failed attempts count towards setting a conversation aside.
        rid = run.get("recording_id")
        if run.get("answer_status") == "error" and rid in recordings and rid not in analysed | settled:
            needed[n] = NEEDED_FIELDS
    return needed


def prune_runs(runs, recordings, now=None):
    """The daily tidy-up of the run log: (the records kept, in order, {"trimmed": n, "deleted": n})."""
    now = now or utcnow()
    needed, kept, done = needed_runs(runs, recordings), [], {"trimmed": 0, "deleted": 0}
    for n, run in enumerate(runs):
        moment = parse_datetime(run.get("finished") or run.get("started") or "")
        age = now - moment if moment else None  # No time: a record from before every record had one.
        if n in needed:
            fields = needed[n]
            if fields and (age is None or age > timedelta(days=KEEP_FULL_DAYS)) and set(run) - set(fields):
                run = {k: v for k, v in run.items() if k in fields}
                done["trimmed"] += 1
            kept.append(run)
        elif run.get("kind") == "hypothesis_tests" and age is not None and age <= timedelta(days=KEEP_HISTORY_DAYS):
            if age > timedelta(days=KEEP_FULL_DAYS) and set(run) - set(TEST_FIELDS):
                from .reflective import tried
                run = {**{k: v for k, v in run.items() if k in TEST_FIELDS}, "tried": tried(run)}
                done["trimmed"] += 1
            kept.append(run)
        elif age is not None and age <= timedelta(
                days=KEEP_HISTORY_DAYS if run.get("kind") == "pipeline" else KEEP_FULL_DAYS):
            kept.append(run)
        else:
            done["deleted"] += 1
    return kept, done


async def prune_traces(hass, active=(), now=None):
    """The daily tidy-up of the traces: those not written for TRACE_DAYS are deleted, then the oldest until the rest
    fit in TRACE_MAX_BYTES; an active one (`active`: their ids) never. Returns {"deleted": n, "bytes": total left}."""
    from .trace import remove, stored
    now = (now or utcnow()).timestamp()
    files = sorted(await hass.async_add_executor_job(stored, hass.config.path(".storage")), key=lambda f: f["mtime"])
    total, deleted = sum(f["bytes"] for f in files), 0
    for found in files:  # Oldest first.
        old = now - found["mtime"] > TRACE_DAYS * 86400
        if found["id"] in active or not (old or total > TRACE_MAX_BYTES):
            continue
        await remove(hass, found["id"])
        total -= found["bytes"]
        deleted += 1
    return {"deleted": deleted, "bytes": total}
