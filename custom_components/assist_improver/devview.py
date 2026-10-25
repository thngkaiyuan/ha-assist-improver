"""The developer view's data (/assist-improver?view=developer): every conversation and where it stands, any ID's
lifecycle with its log, runs and where their time went (trace.py), the journal, and the improver's footprint.

Admins only, and only while developer mode is on (the hidden developer_services option). Every stage and status is
derived by the same code the pipeline decides with, never worked out again here. Health reports sizes and counts,
never conversation text.
"""

import asyncio
from collections import Counter, defaultdict
import json
import os
import sys
import time

import probatio
from homeassistant.components import websocket_api

from . import trace
from .const import CONF_DEVELOPER, DOMAIN, VERSION
from .runtime import loaded

MIN_PREFIX = 4  # Characters an ID prefix needs before lookup tries it.
JOURNAL_SHOWN = 200  # Journal entries a lookup shows; the Logs tab pages through the rest.


def _runtime(hass, connection, msg):
    """The loaded improver, or None after sending why not (not loaded, or developer mode off)."""
    runtime = loaded(hass)
    pipeline = runtime.pipeline if runtime else None
    if pipeline is None:
        connection.send_error(msg["id"], "not_loaded", "Assist Improver isn't loaded.")
        return None
    if not pipeline.entry.options.get(CONF_DEVELOPER):
        connection.send_error(msg["id"], "developer_mode_off", "Developer mode is off.")
        return None
    return runtime


def _short(recording_id):
    from .hypotheses import short
    return short(recording_id)


def _recording_sizes(hass):
    """{recording ID: bytes on disk}."""
    path, prefix = hass.config.path(".storage"), f"{DOMAIN}.session_"
    out = {}
    for name in os.listdir(path) if os.path.isdir(path) else []:
        if name.startswith(prefix):
            try:
                out[name[len(prefix):]] = os.path.getsize(os.path.join(path, name))
            except FileNotFoundError:
                continue
    return out


class _Facts:
    """What the pipeline knows about every recording, gathered in one pass over the run log, the improvements and
    the cases."""

    def __init__(self, runtime):
        from .hypotheses import friction_by_goal
        pipeline, improvements = runtime.pipeline, runtime.improvements
        self.analysed, self.unreadable, self.tests = pipeline._analyzed(), pipeline._unreadable(), pipeline._tests()
        self.waiting_at = pipeline._at()
        self.settled = improvements.settled_goals()
        self.attempts, self.proposals = defaultdict(list), defaultdict(list)
        for run in getattr(runtime.role_runs, "runs", []):
            if run.get("kind") == "journey_analyzer" and run.get("recording_id"):
                self.attempts[run["recording_id"]].append(run)
            elif run.get("kind") == "hypothesis_generator":
                for session in run.get("sessions") or []:
                    self.proposals[session].append(run)
        self.covering = defaultdict(list)
        for item in improvements.items.values():
            for goal in dict.fromkeys(g.split(":")[0] for g in improvements._covers(item)):
                self.covering[goal].append(item["id"])
        self.cases = defaultdict(list)
        for case in runtime.casebook.cases.values():
            self.cases[case.get("recording_id")].append(case["id"])
        self.friction_by_goal = friction_by_goal

    def stage(self, rid, summary):
        """Where a recording stands, in the pipeline's own terms."""
        if summary.get("status") == "active":
            return "active"
        if summary.get("user_turns") == 0:
            return "empty"
        if rid in self.tests:
            return "test"
        if rid in self.unreadable:
            return "set_aside"
        analysis = self.analysed.get(rid)
        if analysis is None:
            attempts = self.attempts.get(rid, [])
            if attempts and attempts[-1].get("status") == "held":
                return "held"
            # Its last attempt failed (a provider's error or a bug): the next run tries again.
            return "failing" if attempts and attempts[-1].get("status") == "failed" else "waiting"
        if analysis.get("triaged"):  # Sent in since: it waits for the full analysis.
            return "waiting" if summary.get("filed") else "smooth"
        goals = [f"{_short(rid)}:{turn}" for turn in self.friction(analysis)]
        if goals and all(g in self.settled for g in goals):
            return "duplicate" if analysis.get("duplicates") and not self.covering.get(_short(rid)) else "improvement"
        return "improvement" if self.covering.get(_short(rid)) else "analysed"

    def strikes(self, rid):
        return sum(1 for r in self.attempts.get(rid, []) if r.get("answer_status") == "error")

    def friction(self, analysis):
        try:
            return self.friction_by_goal(analysis)
        except (KeyError, TypeError):
            return {}


def conversation_rows(runtime, sizes=None):
    """One row per recording, newest first: {id, short, started, ended, user_turns, status, stored, bytes, practice,
    stage, waiting_at, strikes, goals, friction, improvements, cases, runs}. stage is one of active, empty, waiting,
    held, failing (its last attempt failed; strikes counts the unexpected ones, which set it aside at three),
    set_aside, test, smooth, analysed, duplicate, improvement."""
    facts, sizes = _Facts(runtime), sizes or {}
    rows = []
    for rid, summary in runtime.recorder.index.items():
        analysis = facts.analysed.get(rid) or {}
        short = _short(rid)
        runs = {r.get("run_id") for r in facts.attempts.get(rid, []) if r.get("run_id")}
        runs |= {r.get("run_id") for r in facts.proposals.get(short, []) if r.get("run_id")}
        rows.append({"id": rid, "short": short, "started": summary.get("started"), "ended": summary.get("updated"),
                     "user_turns": summary.get("user_turns"), "status": summary.get("status"),
                     "stored": summary.get("stored", "full"), "bytes": sizes.get(rid),
                     "practice": bool((runtime.recorder.sessions.get(rid) or {}).get("practice")) or None,
                     "filed": (summary.get("filed") or {}).get("at"),  # On-demand: when the user filed it.
                     "stage": facts.stage(rid, summary), "waiting_at": facts.waiting_at.get(rid),
                     "strikes": facts.strikes(rid), "goals": len(analysis.get("goals") or []),
                     "friction": len(facts.friction(analysis)) if analysis else 0,
                     "improvements": facts.covering.get(short, []), "cases": facts.cases.get(rid, []),
                     "runs": sorted(runs)})
    rows.sort(key=lambda r: r["started"] or "", reverse=True)
    return rows


# Lookup: any ID, or the start of one.

def _candidates(runtime, trace_ids):
    """(kind, id, title) for everything an ID can name."""
    for rid, summary in runtime.recorder.index.items():
        yield "recording", rid, f"Conversation from {summary.get('started', '?')}"
    for conversation_id, rid in runtime.recorder.keys.items():
        yield "conversation_id", conversation_id, f"Home Assistant conversation of recording {_short(rid)}"
    for run in getattr(runtime.role_runs, "runs", []):
        if run.get("id"):
            kind = "run" if run.get("kind") == "pipeline" else "role_call"
            yield kind, run["id"], f"{run.get('kind')} {run.get('status')} {run.get('started') or ''}".strip()
    for item in runtime.improvements.items.values():
        yield "improvement", item["id"], item.get("title", "")
    for case in runtime.casebook.cases.values():
        yield "case", case["id"], case.get("title", "")
    for trace_id in trace_ids:
        if trace_id.startswith("day_"):
            yield "trace", trace_id, f"The day's trace {trace_id[4:]}"


def resolve(runtime, query, trace_ids=()):
    """[{kind, id, title}] that the query names: an exact ID (or a recording's 8-character short ID) first; else
    every ID it starts, once it has MIN_PREFIX characters."""
    query = (query or "").strip()
    found = list(_candidates(runtime, trace_ids))
    exact = [c for c in found if c[1] == query]
    if not exact and len(query) == 8:
        exact = [c for c in found if c[0] == "recording" and c[1][:8] == query]
    if exact:
        chosen = exact
    elif len(query) >= MIN_PREFIX:
        chosen = [c for c in found if c[1].startswith(query)]
    else:
        chosen = []
    unique = {(kind, cid): title for kind, cid, title in chosen}
    return [{"kind": kind, "id": cid, "title": title} for (kind, cid), title in unique.items()]


def _event(at, kind, what, **ids):
    return {"at": at, "kind": kind, "what": what, "ids": {k: v for k, v in ids.items() if v}}


def _role_call(run):
    """A run-log record without its bulky parts (raw replies, analyses), for timelines."""
    keep = ("id", "kind", "role", "status", "answer_status", "recording_id", "started", "finished", "seconds",
            "entity_id", "run_id", "trace_id", "error")
    out = {k: run.get(k) for k in keep if run.get(k) is not None}
    tokens = _tokens(run)
    if tokens:
        out["tokens"] = tokens
    return out


def _tokens(run):
    """Provider-reported token counts of a run-log record (and its triage), summed by name."""
    totals = Counter()

    def add(value, key=""):
        if isinstance(value, dict):
            for k, v in value.items():
                add(v, k)
        elif isinstance(value, list):
            for v in value:
                add(v, key)
        elif isinstance(value, (int, float)) and not isinstance(value, bool) and "token" in key:
            totals[key] += value
    add((run.get("usage") or {}).get("events"))
    add(((run.get("triage") or {}).get("usage") or {}).get("events"))
    return dict(totals)


def recording_timeline(runtime, rid):
    facts, summary = _Facts(runtime), runtime.recorder.index.get(rid, {})
    short = _short(rid)
    events = [_event(summary.get("started"), "recorded", f"Recording started ({summary.get('user_turns', 0)} user "
                     f"turns so far)", recording=rid)]
    if summary.get("status") != "active":
        events.append(_event(summary.get("updated"), "closed", f"Closed: {summary.get('status')}; stored "
                             f"{summary.get('stored', 'full')}", recording=rid))
    for run in facts.attempts.get(rid, []):
        triage = run.get("triage") or {}
        verdict = (triage.get("output") or {}).get("smooth") if isinstance(triage.get("output"), dict) else None
        what = f"{run.get('status')}" + (f" ({run['answer_status']})" if run.get("answer_status") else "")
        if verdict is not None:
            what += f"; triage: {'smooth' if verdict else 'needs analysis'}"
        if run.get("analysis"):
            what += f"; {len(run['analysis'].get('goals') or [])} goals, {len(facts.friction(run['analysis']))} with friction"
        if (run.get("error") or {}).get("message"):
            what += f"; {run['error']['message']}"
        events.append(_event(run.get("finished") or run.get("started"), "analysis", what, role_call=run.get("id"),
                             run=run.get("run_id")))
    for run in facts.proposals.get(short, []):
        events.append(_event(run.get("finished") or run.get("started"), "proposal",
                             f"Read by the proposer ({run.get('status')})", role_call=run.get("id"),
                             run=run.get("run_id")))
    for case_id in facts.cases.get(rid, []):
        case = runtime.casebook.cases[case_id]
        events.append(_event(case.get("created"), "case", f"Case: {case.get('title', '')}", case=case_id))
    for improvement_id in facts.covering.get(short, []):
        item = runtime.improvements.items[improvement_id]
        goals = [g for g in runtime.improvements._covers(item) if g.split(":")[0] == short]
        events.append(_event(item.get("created"), "improvement", f"Improvement covers goal(s) {', '.join(goals)}: "
                             f"{item.get('title', '')} ({item.get('status')})", improvement=improvement_id))
    return events


def run_timeline(runtime, run_id):
    run = next(r for r in runtime.role_runs.runs if r.get("id") == run_id)
    progress = run.get("progress") or {}
    events = [_event(run.get("started"), "run", f"Run started ({run.get('trigger')}"
                     + (f", only {run['only']}" if run.get("only") else "") + ")", run=run_id)]
    began, ended = run.get("started") or "", run.get("finished") or "9999"
    for call in runtime.role_runs.runs:
        if call is run or call.get("kind") == "pipeline":
            continue
        mine = call.get("run_id") == run_id if call.get("run_id") else (
            began <= (call.get("started") or call.get("finished") or "") <= ended)
        if mine:
            what = f"{call.get('role') or call.get('kind')}: {call.get('status')}"
            if call.get("answer_status"):
                what += f" ({call['answer_status']})"
            if call.get("seconds") is not None:
                what += f" in {call['seconds']} s"
            events.append(_event(call.get("started") or call.get("finished"), "role_call", what,
                                 role_call=call.get("id"), recording=call.get("recording_id")))
    for problem in progress.get("problems") or []:
        events.append(_event(run.get("finished"), "problem", f"{problem.get('stage')}: {problem.get('error')}",
                             run=run_id))
    if run.get("finished"):
        events.append(_event(run["finished"], "finished", f"Run {run.get('status')}"
                             + (f": {run['error']}" if run.get("error") else ""), run=run_id))
    return run, events


def improvement_timeline(runtime, improvement_id):
    item = runtime.improvements.items[improvement_id]
    events = [_event(item.get("created"), "created", f"Proposed: {item.get('title', '')}", improvement=improvement_id)]
    if item.get("copy"):
        events.append(_event(item["copy"].get("at") or item.get("created"), "written",
                             f"Written up: {item['copy'].get('headline') or item['copy'].get('status')}",
                             improvement=improvement_id))
    for test in item.get("tests", []):
        events.append(_event(test.get("at") or item.get("created"), "test",
                             f"Tested on goal {test.get('goal')}: {test.get('result') or test.get('status')}",
                             case=test.get("case_id"), recording=None))
    for decision in item.get("decisions", []):
        events.append(_event(decision.get("at"), "decision", f"{decision.get('decision')}: {decision.get('outcome')}"
                             + (" (automatically)" if decision.get("auto") else ""), improvement=improvement_id))
    for happened in item.get("history", []):
        events.append(_event(happened.get("at"), "history", f"{happened.get('event')}: {happened.get('status', '')}",
                             improvement=improvement_id))
    return events


async def lookup(hass, runtime, query):
    """The lookup command's reply: several matches to pick from, or one with its timeline, journal and trace."""
    retained = [t["id"] for t in await trace.traces(hass)]
    matches = resolve(runtime, query, retained)
    if len(matches) != 1:
        return {"matches": matches}
    match = matches[0]
    kind, mid = match["kind"], match["id"]
    reply = {"match": match, "timeline": [], "journal": [], "trace_id": None, "record": None}
    if kind == "conversation_id":
        kind, mid = "recording", runtime.recorder.keys[mid]
        reply["match"] = {**match, "recording": mid}
    if kind == "recording":
        reply["timeline"] = recording_timeline(runtime, mid)
        reply["record"] = {**runtime.recorder.index.get(mid, {})}
        reply["journal"] = await trace.find_entries(hass, limit=JOURNAL_SHOWN, recording_id=mid)
    elif kind == "run":
        run, reply["timeline"] = run_timeline(runtime, mid)
        reply["record"] = {k: run.get(k) for k in ("id", "status", "trigger", "only", "started", "finished",
                                                   "stage", "progress", "error", "trace_id")}
        reply["trace_id"] = run.get("trace_id") or (mid if mid in retained else None)
        reply["journal"] = await trace.find_entries(hass, limit=JOURNAL_SHOWN, run_id=mid)
    elif kind == "role_call":
        call = next(r for r in runtime.role_runs.runs if r.get("id") == mid)
        reply["record"] = _role_call(call)
        reply["trace_id"] = call.get("trace_id")
        if call.get("run_id"):
            reply["timeline"] = run_timeline(runtime, call["run_id"])[1]
    elif kind == "improvement":
        reply["timeline"] = improvement_timeline(runtime, mid)
        item = runtime.improvements.items[mid]
        reply["record"] = {k: item.get(k) for k in ("id", "title", "status", "created")}
        reply["journal"] = await trace.find_entries(hass, limit=JOURNAL_SHOWN, improvement_id=mid)
    elif kind == "case":
        case = runtime.casebook.cases[mid]
        reply["record"] = {k: case.get(k) for k in ("id", "title", "recording_id", "turns", "revision")}
        reply["timeline"] = [_event(case.get("created"), "case", f"Case from recording {_short(case.get('recording_id') or '')}",
                                    recording=case.get("recording_id"), case=mid)]
        reply["journal"] = await trace.find_entries(hass, limit=JOURNAL_SHOWN, case_id=mid)
    elif kind == "trace":
        reply["trace_id"] = mid
        reply["journal"] = await trace.find_entries(hass, limit=JOURNAL_SHOWN, trace_ids={mid})
    reply["timeline"].sort(key=lambda e: e.get("at") or "")
    return reply


# Health: where space and time go.

def _measured(value):
    """(bytes as JSON, milliseconds the measurement took): an estimate of what a structure holds."""
    began = time.perf_counter()
    try:
        size = len(json.dumps(value, default=str))
    except (TypeError, ValueError, RecursionError):
        size = None
    return size, round((time.perf_counter() - began) * 1000, 1)


def _memory(hass, runtime):
    from .practice import MODE_KEY
    parts = {"live recordings": runtime.recorder.sessions, "recordings index": runtime.recorder.index,
             "run log": getattr(runtime.role_runs, "runs", []), "improvements": runtime.improvements.items,
             "cases": runtime.casebook.cases, "candidates": getattr(runtime.candidates, "items", None),
             "hot spots": trace.HOT}
    out = {}
    for name, value in parts.items():
        if value is None:
            continue
        size, ms = _measured(value)
        out[name] = {"items": len(value) if hasattr(value, "__len__") else None, "bytes": size, "ms": ms}
    mode = hass.data.get(MODE_KEY)
    sessions = getattr(getattr(mode, "sessions", None), "sessions", {}) or {}
    out["practice sessions"] = {
        "items": len(sessions), "simulated states": sum(len(s.states.overrides) for s in sessions.values()),
        "list copies": sum(len(s.lists.lists) for s in sessions.values() if s.lists),
        "timers": sum(len(t.timers) for s in sessions.values()
                      if (t := s.data.maps[0].get(_timer_key())) is not None)}
    out["active traces"] = trace.active_summary()
    return out


def _timer_key():
    from homeassistant.components.intent.const import TIMER_DATA
    return TIMER_DATA


def _tasks():
    """Our asyncio tasks now, by name."""
    mine = []
    for task in asyncio.all_tasks():
        coro = task.get_coro()
        module = getattr(getattr(coro, "cr_code", None), "co_filename", "") or ""
        name = task.get_name()
        if "assist_improver" in name or "Practice timer" in name or f"{os.sep}assist_improver{os.sep}" in module:
            mine.append(name if not name.startswith("Task-") else f"{name} ({getattr(coro, '__qualname__', '?')})")
    return sorted(mine)


def _ai_calls(runtime):
    by_role = defaultdict(lambda: {"calls": 0, "failed": 0, "seconds": 0.0, "slowest": 0.0, "tokens": Counter()})
    for run in getattr(runtime.role_runs, "runs", []):
        role = run.get("role")
        if not role or not run.get("started"):
            continue
        row = by_role[role]
        row["calls"] += 1
        row["failed"] += run.get("status") == "failed"
        row["seconds"] = round(row["seconds"] + (run.get("seconds") or 0), 1)
        row["slowest"] = max(row["slowest"], run.get("seconds") or 0)
        row["tokens"].update(_tokens(run))
    return {role: {**row, "tokens": dict(row["tokens"])} for role, row in by_role.items()}


def _max_rss():
    """Home Assistant's whole process: its peak resident memory, in bytes."""
    import resource
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak if sys.platform == "darwin" else peak * 1024


async def health(hass, runtime):
    from .diagnostics import _storage
    from .worker import STATS
    disk = await hass.async_add_executor_job(_storage, hass.config.path(".storage"))
    pipeline = runtime.pipeline
    current = pipeline.current or {}
    return {
        "version": VERSION,
        "disk": {**disk, "recordings_by_storage": dict(Counter(s.get("stored", "full") for s in runtime.recorder.index.values()))},
        "memory": _memory(hass, runtime),
        "now": {"run": {"running": current.get("status") == "running", "stage": current.get("stage"),
                        "id": current.get("id")},
                "tasks": _tasks(), "sandboxes": {**STATS, "running": STATS.get("started", 0) - STATS.get("stopped", 0)},
                "pending_writes": runtime.recorder.status().get("pending_writes")},
        "hot": sorted(({"name": name, **row} for name, row in trace.HOT.items()), key=lambda r: -r["total_ms"]),
        "ai_calls": _ai_calls(runtime),
        "retention": runtime.retention,
        "process": {"max_rss_bytes": _max_rss(), "note": "The whole Home Assistant process. For CPU and memory "
                    "profiles, use Home Assistant's Profiler integration."},
    }


# Websocket commands.

@websocket_api.websocket_command({probatio.Required("type"): f"{DOMAIN}/dev/conversations"})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_conversations(hass, connection, msg):
    """{"rows": [...]}: every recording, newest first (conversation_rows)."""
    if (runtime := _runtime(hass, connection, msg)) is None:
        return
    sizes = await hass.async_add_executor_job(_recording_sizes, hass)
    connection.send_result(msg["id"], {"rows": conversation_rows(runtime, sizes)})


@websocket_api.websocket_command({probatio.Required("type"): f"{DOMAIN}/dev/lookup",
                                  probatio.Required("query"): str})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_lookup(hass, connection, msg):
    """{"matches": [{kind, id, title}]} when the query names none or several; else {"match", "record", "timeline":
    [{at, kind, what, ids}], "journal": [entries], "trace_id"} for the one it names."""
    if (runtime := _runtime(hass, connection, msg)) is None:
        return
    connection.send_result(msg["id"], await lookup(hass, runtime, msg["query"]))


@websocket_api.websocket_command({probatio.Required("type"): f"{DOMAIN}/dev/runs"})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_runs(hass, connection, msg):
    """{"runs": [{id, started, finished, status, trigger, only, timings, counts, problems, error, trace_id,
    trace_available}]}, newest first."""
    if (runtime := _runtime(hass, connection, msg)) is None:
        return
    retained = {t["id"] for t in await trace.traces(hass)}
    runs = []
    for run in getattr(runtime.role_runs, "runs", []):
        if run.get("kind") != "pipeline":
            continue
        progress = dict(run.get("progress") or {})
        trace_id = run.get("trace_id") or run.get("id")
        runs.append({"id": run.get("id"), "started": run.get("started"), "finished": run.get("finished"),
                     "status": run.get("status"), "trigger": run.get("trigger"), "only": run.get("only"),
                     "stage": run.get("stage"), "timings": progress.pop("timings", {}),
                     "problems": progress.pop("problems", []), "counts": progress, "error": run.get("error"),
                     "trace_id": trace_id, "trace_available": trace_id in retained})
    runs.sort(key=lambda r: r["started"] or "", reverse=True)
    connection.send_result(msg["id"], {"runs": runs})


@websocket_api.websocket_command({probatio.Required("type"): f"{DOMAIN}/dev/trace",
                                  probatio.Required("trace_id"): str})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_trace(hass, connection, msg):
    """{"trace": {id, kind, started, updated, finished, spans (by start), journal (newest first), open, dropped}}."""
    if _runtime(hass, connection, msg) is None:
        return
    loaded = await trace.load(hass, msg["trace_id"])
    if loaded is None:
        connection.send_error(msg["id"], "not_found", "No such trace: it may have been deleted after a week.")
        return
    connection.send_result(msg["id"], {"trace": {**loaded, "spans": trace.spans(loaded),
                                                 "journal": list(trace.entries(loaded))}})


@websocket_api.websocket_command({probatio.Required("type"): f"{DOMAIN}/dev/journal",
                                  probatio.Optional("level"): probatio.In(list(trace.LEVELS)),
                                  probatio.Optional("text"): str, probatio.Optional("before"): str,
                                  probatio.Optional("run_id"): str, probatio.Optional("recording_id"): str,
                                  probatio.Optional("improvement_id"): str,
                                  probatio.Optional("limit", default=100): probatio.All(int, probatio.Range(1, 500))})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_journal(hass, connection, msg):
    """{"entries": [entry + trace_id], "more": bool}, newest first; page with before = the last entry's at."""
    if _runtime(hass, connection, msg) is None:
        return
    filters = {k: msg[k] for k in ("level", "text", "before", "run_id", "recording_id", "improvement_id") if msg.get(k)}
    found = await trace.find_entries(hass, limit=msg["limit"] + 1, **filters)
    connection.send_result(msg["id"], {"entries": found[:msg["limit"]], "more": len(found) > msg["limit"]})


@websocket_api.websocket_command({probatio.Required("type"): f"{DOMAIN}/dev/health"})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_health(hass, connection, msg):
    """{"version", "disk", "memory", "now", "hot", "ai_calls", "retention", "process"} (health)."""
    if (runtime := _runtime(hass, connection, msg)) is None:
        return
    connection.send_result(msg["id"], await health(hass, runtime))


COMMANDS = (ws_conversations, ws_lookup, ws_runs, ws_trace, ws_journal, ws_health)
