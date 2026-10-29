"""Traces: what the improver did and where its time went, for debugging without AI help (the developer view).

A trace is a pipeline run's (its id is the run's id) or a day's ("day_YYYYMMDD", the local date): recordings
starting, closing and changing storage, the daily tidy-up, decisions, apply and undo, notifications, practice
sessions and settings changes. Each trace has its own private store, saved about every SAVE_DELAY seconds while
active, and for good at the end of a run, on unload and when Home Assistant stops. Only active traces stay in
memory; readers load the rest. The daily tidy-up deletes them (retention.prune_traces); the run log keeps each run's
record for a year, so nothing long-term needs a trace.

Data shapes (stable: devview.py and the panel read them; treat what the readers return as read-only):

    trace = {"id": str, "kind": "run" | "day", "started": iso, "updated": iso, "finished": iso | None,
             "spans": [span], "journal": [entry], "open": [open span], "dropped": {"spans": int, "journal": int}}
    span = {"id": str, "parent": str | None, "name": str, "tags": {...}, "at": iso (when it started),
            "ms": float (how long it took), "status": "ok" | "error" | "cancelled", "error": str | None}
    open span = {"id", "parent", "name", "tags", "at"}: still going in an active trace; in any other, cut off
                (Home Assistant stopped before it closed)
    entry = {"at": iso, "level": "debug" | "info" | "warning" | "error" | "critical", "message": str,
             "tags": {...}, "span": str | None (the span it happened in)}
            + "logger": str and, with a traceback, "exception": str, for records copied from the log
    HOT = {name: {"count": int, "total_ms": float, "max_ms": float, "last_at": iso}}: every span and timed
          section since Home Assistant started, by name (in memory only)

Times are UTC ISO strings. A span is added when it closes, so it comes after its children: order by "at" to draw
a waterfall. Tags merge the context's (CONTEXT_TAGS: run_id, recording_id, improvement_id, plan_id, role, goal),
which everything opened inside inherits, tasks started inside included, with the span's or entry's own. Span names
are few and fixed (ids go in tags), as HOT adds up by name. Beyond MAX_SPANS spans or MAX_JOURNAL entries a trace
counts what it drops instead.

The spans, nested as they run (own tags in brackets):

    run [trigger, only, outcome] > the spans below, as the flow moves each conversation (flow.py)
    conversation [recording_id, steps, triage, status, answer_status] > ai.triage, ai.journey_analyzer
    propose [sessions, status, generator_run] > evidence.load, ai.hypothesis_generator, ai.hypothesis_reviewer
    replay [kind: "before" | "after", plan_id, status] > replay.turn [replay: "before" | "after", turn] >
        replay.tool [tool]; sandbox.start, sandbox.seed, sandbox.stop > sandbox.teardown; ai.feedback_check
    write [improvement_id, status] > ai.editor
    ai.<role> [role, model, input_chars, status, tokens] > tool.<name> [accepted, or answer: "error"]
        (tool.ask_reviewer > evidence.check)
    tidy, in today's trace; recorder.flush, recorder.pack, recorder.snapshot [recording_id] wherever a trace is
    current. HOT only: store.role_runs, store.improvements, store.recordings, store.recordings_index.

A run's records in the run log (reflective.RoleRuns) carry its "run_id" and "trace_id"; the run's own record carries
"trace_id" (its id). Older records have neither.

Writing: span(name, **tags) around a section, `with` for code that doesn't await and `async with` for code that
does (a `with` section holding the event loop over BLOCK_MS is journaled as a warning); timed(name) the same, into
HOT only, also as a decorator; tag(**tags) adds to the current span; journal(level, message, **tags), and
journal_detail(...) kept only in developer mode. Records from the integration's loggers that pass Home Assistant's
configured level are copied into the journal too; logger levels are never changed.

Reading: active_ids() and active_summary() (what's in memory), traces(hass) (the retained ones), load(hass,
trace_id), spans(trace, **tags) and entries(trace, ...) for one, find_entries(hass, ...) across all of them.
"""

import asyncio
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
import functools
import heapq
import inspect
from itertools import islice
import json
import logging
import os
import re
from secrets import token_hex
import threading
import time
import traceback

from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import callback
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import CONF_DEVELOPER, DOMAIN

_LOGGER = logging.getLogger(__name__)
PACKAGE_LOGGER = __name__.rpartition(".")[0]  # The integration's loggers, whose records the journal copies.

MAX_SPANS, MAX_JOURNAL = 20000, 5000  # Per trace.
SAVE_DELAY = 15  # Seconds between saves of an active trace.
BLOCK_MS = 100  # A synchronous section holding the event loop longer than this is journaled.
MAX_TEXT = 4000  # A journal message or traceback is cut to this many characters.
CONTEXT_TAGS = ("run_id", "recording_id", "improvement_id", "plan_id", "role", "goal")
LEVELS = {"debug": 10, "info": 20, "warning": 30, "error": 40, "critical": 50}
STORE_PREFIX = f"{DOMAIN}.trace_"
_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_MARK = "assist_improver_journal"  # On the journal's own debug lines, so the log handler doesn't copy them back.

HOT = {}


def _now():
    return dt_util.utcnow().isoformat()


def _time(iso):
    moment = dt_util.parse_datetime(iso or "")
    return moment.timestamp() if moment else 0.0


def _text(value, limit=MAX_TEXT, tail=False):
    value = str(value)
    return value if len(value) <= limit else (value[-limit:] if tail else value[:limit])


def _clean(tags):
    """Tags as stored: None left out, tuples and sets as lists, anything else not JSON-like as text."""
    return {k: _plain(v) for k, v in tags.items() if v is not None}


def _plain(value):
    if isinstance(value, (str, int, float, bool, list, dict)):
        return value
    if isinstance(value, tuple):
        return list(value)
    return sorted(value, key=str) if isinstance(value, (set, frozenset)) else str(value)


def _kind(trace_id):
    return "day" if trace_id.startswith("day_") else "run"


# The context: which trace, which span, and which tags; asyncio tasks started inside get a copy.

@dataclass(frozen=True)
class _Context:
    trace: str | None = None
    span: "_Section | None" = None  # The innermost recorded span, the parent of new ones.
    tags: dict = field(default_factory=dict)


_CURRENT: ContextVar[_Context] = ContextVar("assist_improver_trace", default=_Context())


class _Trace:
    """An active trace, in memory."""

    def __init__(self, store, trace_id, kind, saved=None):
        saved = saved or {}
        self.store, self.id, self.kind = store, trace_id, kind
        self.started = saved.get("started") or _now()
        self.finished = None
        self.spans, self.journal = list(saved.get("spans") or []), list(saved.get("journal") or [])
        self.dropped = {"spans": 0, "journal": 0, **(saved.get("dropped") or {})}
        self.open = {}
        self.pending = False  # A save is scheduled.
        for cut in saved.get("open") or []:  # Spans a restart cut off: kept, closed at the last save.
            ms = (_time(saved.get("updated")) - _time(cut.get("at"))) * 1000 if saved.get("updated") else 0.0
            self.spans.append({**cut, "ms": round(max(ms, 0.0), 3), "status": "cancelled",
                               "error": "Cut off: Home Assistant stopped before it finished"})

    def snapshot(self, saving=True):
        """The trace as stored; shallow copies, so it can be serialized while the trace grows."""
        if saving:
            self.pending = False  # First: an entry added from here on schedules the next save.
        return {"id": self.id, "kind": self.kind, "started": self.started, "updated": _now(),
                "finished": self.finished, "spans": list(self.spans), "journal": list(self.journal),
                "open": list(self.open.values()), "dropped": dict(self.dropped)}


class Tracer:
    """The loaded improver's traces: the active ones (today's, and a running pipeline's), their stores and the log
    handler."""

    def __init__(self, hass, entry):
        self.hass, self.entry = hass, entry
        self.active = {}  # Trace id -> _Trace.
        self.stores = {}  # Trace id -> its Store, kept so a load sees a write still pending.
        self.today = None
        self.handler = _JournalHandler()
        self.stop_listener = None

    @property
    def developer(self):
        return bool(self.entry is not None and self.entry.options.get(CONF_DEVELOPER))

    def store(self, trace_id):
        if trace_id not in self.stores:
            # Serialized in the executor: a trace can be megabytes, and its lists are only ever appended to.
            self.stores[trace_id] = Store(self.hass, 1, STORE_PREFIX + trace_id, private=True, atomic_writes=True,
                                          serialize_in_event_loop=False)
        return self.stores[trace_id]

    def open(self, trace_id, kind, saved=None):
        trace = self.active[trace_id] = _Trace(self.store(trace_id), trace_id, kind, saved)
        return trace

    def on_loop(self):
        return threading.get_ident() == self.hass.loop_thread_id

    def day(self):
        """Today's trace, started on first use each day; yesterday's is saved for good and let go."""
        trace_id = f"day_{dt_util.now():%Y%m%d}"
        if self.today is not None and self.today.id == trace_id or not self.on_loop():
            return self.today
        if self.today is not None:
            self.today.finished = _now()
            self.hass.async_create_task(self.finish(self.today), "assist_improver_trace_save")
        self.today = self.open(trace_id, "day")
        return self.today

    def touch(self, trace):
        """Something was added: save it within SAVE_DELAY seconds."""
        if trace.pending:
            return
        trace.pending = True
        if self.on_loop():
            trace.store.async_delay_save(trace.snapshot, SAVE_DELAY)
        else:
            self.hass.loop.call_soon_threadsafe(trace.store.async_delay_save, trace.snapshot, SAVE_DELAY)

    def add_span(self, trace, record):
        if len(trace.spans) >= MAX_SPANS:
            trace.dropped["spans"] += 1
        else:
            trace.spans.append(record)
        self.touch(trace)

    def add_entry(self, trace, entry):
        if len(trace.journal) >= MAX_JOURNAL:
            trace.dropped["journal"] += 1
        else:
            trace.journal.append(entry)
        self.touch(trace)

    async def finish(self, trace):
        """Save a trace for good and let it go from memory."""
        try:
            await trace.store.async_save(trace.snapshot())
        finally:
            self.active.pop(trace.id, None)

    async def flush(self):
        for trace in list(self.active.values()):
            await trace.store.async_save(trace.snapshot())

    @callback
    def on_stop(self, _event):
        self.stop_listener = None
        self.hass.async_create_task(self.flush(), "assist_improver_trace_save")


_TRACER = None


def _tracer():
    tracer = _TRACER
    return tracer if tracer is not None and not tracer.hass.loop.is_closed() else None


async def async_setup(hass, entry):
    """Tracing for the loaded improver: today's trace (it continues across restarts), the log handler, and a save
    when Home Assistant stops."""
    global _TRACER
    if _TRACER is not None:  # An earlier instance that was never unloaded.
        logging.getLogger(PACKAGE_LOGGER).removeHandler(_TRACER.handler)
    tracer = Tracer(hass, entry)
    trace_id = f"day_{dt_util.now():%Y%m%d}"
    saved = await tracer.store(trace_id).async_load()
    tracer.today = tracer.open(trace_id, "day", saved if isinstance(saved, dict) else None)
    logging.getLogger(PACKAGE_LOGGER).addHandler(tracer.handler)
    # A once-listener removes itself when it fires; removing it again logs an error.
    tracer.stop_listener = hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, tracer.on_stop)
    _TRACER = tracer


async def async_unload():
    """The improver is unloading: every active trace is saved, and tracing stops."""
    global _TRACER
    tracer, _TRACER = _TRACER, None
    if tracer is None:
        return
    logging.getLogger(PACKAGE_LOGGER).removeHandler(tracer.handler)
    if tracer.stop_listener:
        tracer.stop_listener()
        tracer.stop_listener = None
    await tracer.flush()


# Writing.

class _Section:
    """A timed section: into HOT always, and recorded as a span when it is one and a trace is current. One use per
    instance; as a decorator, each call gets its own."""

    def __init__(self, name, tags, record):
        self.name, self.tags, self.record = name, tags, record
        self.id = self.trace = self.token = None
        self.status, self.error, self.closed = "ok", None, False

    def __enter__(self):
        return self._enter(awaits=False)

    async def __aenter__(self):
        return self._enter(awaits=True)

    def __exit__(self, kind, err, tb):
        self._exit(err)
        return False

    async def __aexit__(self, kind, err, tb):
        self._exit(err)
        return False

    def __call__(self, fn):
        name, tags, record = self.name, self.tags, self.record
        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def timed_call(*args, **kwargs):
                async with _Section(name, dict(tags), record):
                    return await fn(*args, **kwargs)
        else:
            @functools.wraps(fn)
            def timed_call(*args, **kwargs):
                with _Section(name, dict(tags), record):
                    return fn(*args, **kwargs)
        return timed_call

    def _enter(self, awaits):
        self.awaits, self.at = awaits, _now()
        if self.record:
            context, tracer = _CURRENT.get(), _tracer()
            self.trace = tracer.active.get(context.trace) if tracer is not None and context.trace else None
            nested = {k: v for k, v in self.tags.items() if k in CONTEXT_TAGS}
            self.context_tags = context.tags
            if self.trace is not None:
                self.id = token_hex(6)
                self.parent = context.span.id if context.span is not None and context.span.trace is self.trace \
                    else None
                self.trace.open[self.id] = {"id": self.id, "parent": self.parent, "name": self.name,
                                            "tags": {**context.tags, **self.tags}, "at": self.at}
            self.token = _CURRENT.set(_Context(context.trace, self if self.trace is not None else context.span,
                                               {**context.tags, **nested}))
        self.started = time.perf_counter()
        return self

    def _exit(self, err):
        ms = (time.perf_counter() - self.started) * 1000
        self.closed = True
        if self.token is not None:
            try:
                _CURRENT.reset(self.token)
            except ValueError:  # Closed in another context than it opened in; nothing to restore there.
                pass
        if isinstance(err, asyncio.CancelledError):
            self.status = "cancelled"
        elif err is not None:
            self.status, self.error = "error", _text(f"{type(err).__name__}: {err}", 500)
        hot = HOT.setdefault(self.name, {"count": 0, "total_ms": 0.0, "max_ms": 0.0, "last_at": None})
        hot["count"] += 1
        hot["total_ms"] = round(hot["total_ms"] + ms, 3)
        hot["max_ms"] = round(max(hot["max_ms"], ms), 3)
        hot["last_at"] = self.at
        if self.trace is not None:
            self.trace.open.pop(self.id, None)
            # Only into a trace still active: one saved for good (or from before a reload) is never written again.
            if (tracer := _tracer()) is not None and tracer.active.get(self.trace.id) is self.trace:
                tracer.add_span(self.trace, {"id": self.id, "parent": self.parent, "name": self.name,
                                             "tags": {**self.context_tags, **self.tags}, "at": self.at,
                                             "ms": round(ms, 3), "status": self.status, "error": self.error})
        if not self.awaits and ms > BLOCK_MS and _on_loop():
            _journal("warning", f"{self.name} blocked the event loop for {ms:.0f} ms",
                     {**self.tags, "section": self.name})

    def tag(self, **tags):
        """Add tags to this span; context tags also reach everything opened inside it from now on."""
        self.tags.update(_clean(tags))
        if self.trace is not None and not self.closed:
            self.trace.open.get(self.id, {}).get("tags", {}).update(_clean(tags))
        nested = {k: v for k, v in _clean(tags).items() if k in CONTEXT_TAGS}
        if nested and self.record and not self.closed:
            current = _CURRENT.get()
            _CURRENT.set(replace(current, tags={**current.tags, **nested}))
        return self

    def fail(self, error):
        """Mark this span as failed without raising (a failure the code handles, such as a provider's error)."""
        self.status, self.error = "error", _text(error, 500)
        return self


def span(name, **tags):
    """A span around a section: `with span(...)` for code that doesn't await, `async with` for code that does.
    Recorded in the current trace (outside one, it only counts in HOT); its context tags reach what's inside."""
    return _Section(name, _clean(tags), record=True)


def timed(name):
    """A section counted in HOT only (no span): `with`, `async with`, or as a decorator."""
    return _Section(name, {}, record=False)


def tag(**tags):
    """Add tags to the current span (and, for context tags, to everything opened inside it from now on)."""
    if (current := _CURRENT.get().span) is not None:
        current.tag(**tags)


def _on_loop():
    tracer = _tracer()
    if tracer is not None:
        return tracer.on_loop()
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def journal(level, message, **tags):
    """A structured entry in the current trace (a run's, else today's), mirrored to the debug log."""
    _journal(level, message, tags)


def journal_detail(level, message, **tags):
    """As journal, kept only in developer mode; Home Assistant's debug log shows it either way."""
    tracer = _tracer()
    _journal(level, message, tags, keep=tracer is not None and tracer.developer)


def _journal(level, message, tags, keep=True):
    _LOGGER.debug("%s%s", message, f" {tags}" if tags else "", extra={_MARK: True})
    if keep:
        _write(level, message, tags)


def _write(level, message, tags, **extra):
    if (tracer := _tracer()) is None:
        return
    context = _CURRENT.get()
    trace = (tracer.active.get(context.trace) if context.trace else None) or tracer.day()
    if trace is None:
        return
    spanned = context.span is not None and context.span.trace is trace
    tracer.add_entry(trace, {"at": _now(), "level": level, "message": _text(message),
                             "tags": {**context.tags, **_clean(tags)}, "span": context.span.id if spanned else None,
                             **extra})


class _JournalHandler(logging.Handler):
    """Copies the integration's log records into the journal: those that pass Home Assistant's configured level,
    as the handler sits on the integration's logger and changes no level."""

    def emit(self, record):
        if getattr(record, _MARK, False):
            return
        from .sandbox import THREAD_NAME
        if record.threadName == THREAD_NAME:  # A sandbox's own log stays with that sandbox.
            return
        try:
            extra = {"logger": record.name}
            if record.exc_info:
                extra["exception"] = _text("".join(traceback.format_exception(*record.exc_info)), tail=True)
            _write(record.levelname.lower(), record.getMessage(), {}, **extra)
        except Exception:  # noqa: BLE001 - logging must never fail the caller.
            self.handleError(record)


@asynccontextmanager
async def for_run(run_id):
    """A pipeline run's trace (its id is the run's), current for everything inside, tasks started inside too;
    saved for good at the end."""
    tracer = _tracer()
    trace = tracer.open(run_id, "run") if tracer is not None else None
    token = _CURRENT.set(_Context(run_id, None, {"run_id": run_id}))
    try:
        yield
    finally:
        _CURRENT.reset(token)
        if trace is not None:
            trace.finished = _now()
            await tracer.finish(trace)


@contextmanager
def today():
    """Today's trace as the current one, for work outside runs that has spans (the daily tidy-up)."""
    tracer = _tracer()
    trace = tracer.day() if tracer is not None else None
    token = _CURRENT.set(_Context(trace.id if trace else None, None, {}))
    try:
        yield
    finally:
        _CURRENT.reset(token)


@contextmanager
def tagged(**tags):
    """Context tags for everything inside, without a span."""
    current = _CURRENT.get()
    token = _CURRENT.set(replace(current, tags={**current.tags, **_clean(tags)}))
    try:
        yield
    finally:
        _CURRENT.reset(token)


def ids():
    """{"run_id", "trace_id"} while a pipeline run is current, else {}: for the records a run creates."""
    context = _CURRENT.get()
    run_id = context.tags.get("run_id")
    return {"run_id": run_id, "trace_id": context.trace} if run_id and context.trace else {}


def traced_tool(tool, prefix="tool"):
    """An AI role's tool whose every call gets a span (prefix.name, and how it answered), and in developer mode a
    journal entry with its input; the tool is otherwise unchanged, whoever calls it."""
    if getattr(tool, "_traced", False):
        return tool
    call = tool.async_call

    async def async_call(hass, tool_input, llm_context):
        async with span(f"{prefix}.{tool.name}") as section:
            args = getattr(tool_input, "tool_args", None)
            journal_detail("debug", f"{tool.name} called", args=_text(json.dumps(args, ensure_ascii=False,
                                                                                default=str), 1000))
            result = await call(hass, tool_input, llm_context)
            data = getattr(result, "data", result)
            if isinstance(data, dict) and "accepted" in data:
                section.tag(accepted=bool(data["accepted"]))
            elif isinstance(data, dict) and "error" in data:
                section.tag(answer="error")
            return result
    tool.async_call, tool._traced = async_call, True
    return tool


# Reading.

def active_ids():
    """The traces in memory now: today's, and a running pipeline's."""
    tracer = _tracer()
    return set(tracer.active) if tracer is not None else set()


def active_summary():
    """What the traces in memory hold, for the health view: [{"id", "kind", "started", "spans", "journal", "open",
    "dropped"}] (counts)."""
    tracer = _tracer()
    return [{"id": t.id, "kind": t.kind, "started": t.started, "spans": len(t.spans), "journal": len(t.journal),
             "open": len(t.open), "dropped": dict(t.dropped)} for t in (tracer.active.values() if tracer else ())]


def stored(path):
    """The trace stores in a .storage folder: [{"id", "bytes", "mtime"}]. Blocking: run it in the executor."""
    out = []
    for name in os.listdir(path) if os.path.isdir(path) else []:
        if name.startswith(STORE_PREFIX) and _ID.fullmatch(trace_id := name[len(STORE_PREFIX):]):
            try:
                stat = os.stat(os.path.join(path, name))
            except FileNotFoundError:  # Deleted since the listing.
                continue
            out.append({"id": trace_id, "bytes": stat.st_size, "mtime": stat.st_mtime})
    return out


async def traces(hass):
    """The retained traces, active ones first, then the most recently written: [{"id", "kind", "bytes", "updated",
    "active"}]. bytes and updated are the store's (None for an active trace not saved yet)."""
    files = await hass.async_add_executor_job(stored, hass.config.path(".storage"))
    active = active_ids()
    out = {f["id"]: {"id": f["id"], "kind": _kind(f["id"]), "bytes": f["bytes"],
                     "updated": datetime.fromtimestamp(f["mtime"], UTC).isoformat(), "active": f["id"] in active}
           for f in files}
    for trace_id in active - set(out):
        out[trace_id] = {"id": trace_id, "kind": _kind(trace_id), "bytes": None, "updated": None, "active": True}
    return sorted(out.values(), key=lambda t: (t["active"], t["updated"] or ""), reverse=True)


async def load(hass, trace_id):
    """One trace by id, or None: an active one as it is now (its lists shared, not copied), else from its store."""
    if not isinstance(trace_id, str) or not _ID.fullmatch(trace_id):
        return None
    tracer = _tracer()
    if tracer is not None and (trace := tracer.active.get(trace_id)) is not None:
        return trace.snapshot(saving=False)
    store = (tracer.stores.get(trace_id) if tracer is not None else None) or Store(
        hass, 1, STORE_PREFIX + trace_id, private=True)
    saved = await store.async_load()
    return saved if isinstance(saved, dict) else None


async def remove(hass, trace_id):
    """Delete a trace's store (retention)."""
    tracer = _tracer()
    store = tracer.stores.pop(trace_id, None) if tracer is not None else None
    await (store or Store(hass, 1, STORE_PREFIX + trace_id, private=True)).async_remove()


def spans(trace, **tags):
    """A trace's closed spans with every given tag equal, in the order they started."""
    return sorted((s for s in trace.get("spans") or []
                   if all((s.get("tags") or {}).get(k) == v for k, v in tags.items())), key=lambda s: s.get("at", ""))


def entries(trace, *, level=None, text=None, before=None, **tags):
    """A trace's journal entries, newest first: at least `level`, with `text` in the message or traceback (any
    case), older than `before` (an ISO time, for paging), and every given tag equal."""
    floor = LEVELS.get(level, 0) if level else 0
    needle = text.casefold() if text else None
    for entry in reversed(trace.get("journal") or []):
        if before and entry.get("at", "") >= before:
            continue
        if floor and LEVELS.get(entry.get("level"), LEVELS["info"]) < floor:
            continue
        if needle and needle not in f"{entry.get('message', '')}\n{entry.get('exception', '')}".casefold():
            continue
        if any((entry.get("tags") or {}).get(k) != v for k, v in tags.items()):
            continue
        yield entry


async def find_entries(hass, *, limit=100, trace_ids=None, **filters):
    """Journal entries across the retained traces (or the given ones), newest first, at most `limit`, each with its
    "trace_id"; filters as entries(). With a run_id, only that run's trace and the day traces are read. Loads one
    trace at a time, so memory stays bounded by the largest."""
    run_id = filters.get("run_id")
    found = []
    for info in await traces(hass):
        if trace_ids is not None and info["id"] not in trace_ids:
            continue
        if run_id and info["kind"] == "run" and info["id"] != run_id:
            continue
        if (trace := await load(hass, info["id"])) is None:
            continue
        page = [{**e, "trace_id": info["id"]} for e in islice(entries(trace, **filters), limit)]
        found = heapq.nlargest(limit, found + page, key=lambda e: e.get("at", ""))
    return found
