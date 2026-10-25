"""Runtime for the improver's own AI roles: HA's public AI Task interface only.

Every call needs explicit paid-inference consent. The improver's own model
calls are marked so the recorder never captures them as household sessions.
Usage is what the provider reports through HA's conversation trace, if any.
"""

import asyncio
from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
import json
import logging
import re

import orjson
from homeassistant.components import ai_task
from homeassistant.components.conversation.trace import (
    ConversationTraceEventType, async_conversation_trace,
)
from homeassistant.helpers.json import json_bytes
from homeassistant.helpers.storage import Store
from homeassistant.util.dt import parse_datetime, utcnow

from .cases import CaseError, digest
from .const import DOMAIN, STORAGE_VERSION
from .records import PipelineRun, RoleCall, TestReport
from .serialization import plain
from .trace import ids, span, timed

_LOGGER = logging.getLogger(__name__)

ROLE_ACTIVE: ContextVar[str | None] = ContextVar("assist_improver_role", default=None)


@contextmanager
def role_marker(role):
    token = ROLE_ACTIVE.set(role)
    try:
        yield
    finally:
        ROLE_ACTIVE.reset(token)


NO_TOOLS = re.compile(r"(does not|doesn't|do not|not) support (tools|tool use|tool calling|function calling)", re.I)
NO_TOOLS_MESSAGE = ("This AI model can't use tools, which this step needs. Choose a model that supports tool use, "
                    "such as Claude Opus 5.5.")


def explained(message):
    """A provider's error in the household's words where it's one they can act on."""
    return NO_TOOLS_MESSAGE if NO_TOOLS.search(message or "") else message


async def run_role(hass, *, role, entity_id, instructions, payload, structure, llm_api=None,
                   allow_paid_inference=False):
    """One AI Task call. Returns a run record; never retries or resamples."""
    if not allow_paid_inference:
        raise CaseError("Paid inference has not been authorized")
    if not entity_id:
        raise CaseError("No AI Task entity is configured for the improver; open its Configure screen")
    text = f"{instructions}\n\nInput (JSON):\n{json.dumps(payload, ensure_ascii=False, sort_keys=True)}"
    record = {"role": role, "entity_id": entity_id, "started": utcnow().isoformat(),
              "instructions_sha256": digest(instructions), "input_sha256": digest(payload),
              "input_chars": len(text), "status": "running", "output": None, "error": None, **ids()}
    # One span per call; the tools it calls get theirs inside (trace.traced_tool).
    async with span(f"ai.{role}", role=role, model=entity_id, input_chars=len(text)) as section:
        with role_marker(role), async_conversation_trace() as trace:
            try:
                result = await ai_task.async_generate_data(
                    hass, task_name=f"Assist Improver: {role}", entity_id=entity_id,
                    instructions=text, structure=structure, llm_api=llm_api)
                record.update(status="completed", output=plain(result.data), conversation_id=result.conversation_id)
            except Exception as err:
                record.update(status="failed", error={"type": type(err).__name__,
                                                      "message": explained(str(err))[:500]})
                section.fail(f"{type(err).__name__}: {err}")
        stats = [plain(e["data"]["stats"]) for e in trace.as_dict()["events"]
                 if e["event_type"] == ConversationTraceEventType.AGENT_DETAIL
                 and isinstance(e["data"], dict) and "stats" in e["data"]]
        section.tag(status=record["status"], tokens=tokens(stats) or None)
    record["usage"] = {"status": "provider_reported_partial" if stats else "unavailable", "events": stats,
                       "billing_total": None,
                       "limits": "Provider-reported token counts where available; not an exact bill"}
    record["finished"] = utcnow().isoformat()
    record["seconds"] = round((parse_datetime(record["finished"]) - parse_datetime(record["started"])).total_seconds(), 2)
    _LOGGER.debug("%s on %s: %s in %.1f s", role, entity_id, record["status"], record["seconds"])
    return record


def tokens(stats):
    """The provider-reported token counts of a call's usage events, added up by kind ({} if none were reported)."""
    total = {}
    for event in stats:
        for key, value in (event or {}).items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                total[key] = total.get(key, 0) + value
    return total


class RunIndex:
    """What the pipeline reads from the run log, gathered in one pass: each recording's latest accepted analysis, the
    recordings no role can read, unexpected failures, each recording's latest held quick-check verdict, records by id
    (the pipeline's state), and the last completed proposal with the analyses since. Kept current as records are
    added; taking out one an answer rests on (rare: a record replaced by one with its id) means building it again."""

    def __init__(self, runs=()):
        self.analyzed = {}  # {recording id: its latest accepted analysis}, in the order each was first analysed.
        self.unreadable = Counter()  # {recording id: records saying no role can read it}
        self.strikes = Counter()  # {recording id: runs that failed on it for an unexpected reason}
        self.held = {}  # {recording id: its latest held record with a completed quick check}
        self.ids = {}  # {record id: the first record with it}
        self.repeated = set()  # Ids more than one record has (only an old log's): replacing those rebuilds.
        self.generator = (-1, None)  # The last completed proposal, with its position in the log.
        self.since = []  # [(recording id, analysis)]: accepted analyses after it, in order.
        self.size = 0
        for run in runs:
            self.add(run)

    @staticmethod
    def _analysis(run):
        return run.get("kind") == "journey_analyzer" and run.get("status") == "completed" and bool(run.get("analysis"))

    @staticmethod
    def _verdict(run):
        return (run.get("kind") == "journey_analyzer" and run.get("status") == "held"
                and (run.get("triage") or {}).get("status") == "completed")

    def add(self, run):
        """A record appended to the log."""
        position, self.size = self.size, self.size + 1
        if (key := run.get("id")) in self.ids:
            self.repeated.add(key)
        else:
            self.ids[key] = run
        rid = run.get("recording_id")
        if run.get("kind") == "hypothesis_generator" and run.get("status") == "completed":
            self.generator, self.since = (position, run), []
        elif run.get("kind") == "journey_analyzer":
            if self._analysis(run):
                self.analyzed[rid] = run["analysis"]
                self.since.append((rid, run["analysis"]))
            elif self._verdict(run):
                self.held[rid] = run
            if run.get("answer_status") == "unreadable":
                self.unreadable[rid] += 1
            elif run.get("answer_status") == "error":
                self.strikes[rid] += 1

    def remove(self, run, position):
        """A record taken out of the log at this position. False when an answer rested on it: build again."""
        key = run.get("id")
        if (key in self.repeated or self.ids.get(key) is not run or self._analysis(run) or self._verdict(run)
                or position == self.generator[0]):
            return False
        del self.ids[key]
        self.size -= 1
        if position < self.generator[0]:
            self.generator = (self.generator[0] - 1, self.generator[1])
        if run.get("kind") == "journey_analyzer":
            counter = {"unreadable": self.unreadable, "error": self.strikes}.get(run.get("answer_status"))
            if counter is not None:
                counter[run.get("recording_id")] -= 1
                if counter[run.get("recording_id")] <= 0:
                    del counter[run.get("recording_id")]
        return True


def index_of(role_runs):
    """The index of a run log: RoleRuns keeps its own current; anything else holding runs (a test's stand-in) gets one
    built from them."""
    return role_runs.run_index if isinstance(role_runs, RoleRuns) else RunIndex(getattr(role_runs, "runs", []))


def _frozen(run):
    """A record's JSON, made once when it's kept: every later write of the log copies it as it is. One that won't
    serialize stays as it is, for the store to report as it always has."""
    try:
        return orjson.Fragment(json_bytes(run))
    except TypeError:
        return run


def _position(runs, run):
    """Where a record is in the log (searched from the end: the one replaced is usually recent)."""
    for n in range(len(runs) - 1, -1, -1):
        if runs[n] is run:
            return n
    return None


class RoleRuns:
    """Private record of every AI role call, kept for scoring and as offline fixtures.

    A kept record changes only by being recorded again, never in place: so its JSON is made once, when it's kept, and
    a write of the log only joins those, off the event loop (the store serializes in the executor)."""
    def __init__(self, hass):
        self.store = Store(hass, STORAGE_VERSION, f"{DOMAIN}.role_runs", private=True, atomic_writes=True,
                           serialize_in_event_loop=False)
        self.runs: list[RoleCall | PipelineRun | TestReport] = []  # By kind: records.RUN_LOG.
        self.lock = asyncio.Lock()
        self._index = self._indexed = None  # The index, and the list of runs it was built for.
        self._json, self._json_for = [], None  # Each record's JSON, in order, and the list of runs it's for.

    @property
    def run_index(self):
        """The index (RunIndex), built again if the log was replaced or changed other than by record() and prune()."""
        if self._indexed is not self.runs or self._index.size != len(self.runs):
            self._index, self._indexed = RunIndex(self.runs), self.runs
        return self._index

    def _frozen_runs(self):
        if self._json_for is not self.runs or len(self._json) != len(self.runs):
            self._json, self._json_for = [_frozen(r) for r in self.runs], self.runs
        return self._json

    async def _save(self):
        async with timed("store.role_runs"):
            await self.store.async_save({"runs": self._frozen_runs()})

    async def start(self):
        saved = await self.store.async_load()
        self.runs = saved.get("runs", []) if saved else []
        for run in self.runs:
            if run["status"] == "running":
                run.update(status="interrupted", error={"message": "Reload during the call; not repeated"})
        self._index, self._indexed = RunIndex(self.runs), self.runs
        await self._save()

    async def record(self, run):
        """Keep a record (replacing the one with its id). One a pipeline run creates names the run and its trace."""
        kept = deepcopy(run)
        if kept.get("kind") != "pipeline":
            for key, value in ids().items():
                kept.setdefault(key, value)
        async with self.lock:
            runs, frozen, index = self.runs, self._frozen_runs(), self.run_index
            old = index.ids.get(run["id"])
            n = _position(runs, old) if old is not None and run["id"] not in index.repeated else None
            if n is not None:  # It replaces the one with its id: out of its place, and in at the end.
                runs, frozen = runs[:n] + runs[n + 1:], frozen[:n] + frozen[n + 1:]
                if not index.remove(old, n):
                    index = None
            elif old is not None:  # Several records with its id (an old log's): it replaces them all.
                kept_pairs = [(r, f) for r, f in zip(runs, frozen) if r.get("id") != run["id"]]
                runs, frozen, index = [r for r, _ in kept_pairs], [f for _, f in kept_pairs], None
            self.runs, self._json = [*runs, kept], [*frozen, _frozen(kept)]
            self._json_for = self.runs
            if index is None:
                self._index = RunIndex(self.runs)
            else:
                index.add(kept)
            self._indexed = self.runs
            await self._save()
        return deepcopy(kept)

    async def prune(self, recordings, now=None):
        """The daily tidy-up (retention.prune_runs); returns what it did."""
        from .retention import prune_runs
        async with self.lock:
            runs, done = prune_runs(self.runs, recordings, now)
            if done["trimmed"] or done["deleted"]:
                before = {id(r): f for r, f in zip(self.runs, self._frozen_runs())}  # Untouched records keep theirs.
                self._json = [before[id(r)] if id(r) in before else _frozen(r) for r in runs]
                self.runs = self._json_for = runs
                await self._save()
        return done
