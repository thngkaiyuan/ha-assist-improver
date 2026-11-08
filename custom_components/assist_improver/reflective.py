"""Runtime for the improver's own AI roles: HA's public AI Task interface only.

Every call needs explicit paid-inference consent. The improver's own model
calls are marked so the recorder never captures them as household sessions.
Usage is what the provider reports through HA's conversation trace, if any.
"""

import asyncio
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from copy import deepcopy
import json
import logging
import re

import orjson
from homeassistant.components import ai_task
from homeassistant.components.conversation import ConversationTraceEventType
from homeassistant.helpers.json import json_bytes
from homeassistant.helpers.storage import Store
from homeassistant.util.dt import parse_datetime, utcnow

from . import touchpoints
from .cases import CaseError, digest
from .const import DOMAIN, STORAGE_VERSION
from .failures import closing
from .records import PipelineRun, RoleCall
from .retention import prune_runs
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


def own_trace(hass):
    """A conversation trace of the call's own, which holds what the provider reports for it (an AI Task's result
    carries no usage, and a replay's is its own); none, the usage unknown, while Home Assistant's has changed."""
    try:
        return touchpoints.get(hass, "conversation_trace")()
    except touchpoints.Broken:
        return nullcontext()


NO_TOOLS = re.compile(r"(does not|doesn't|do not|not) support (tools|tool use|tool calling|function calling)", re.I)
NO_TOOLS_MESSAGE = ("This AI model can't use tools, which this step needs. Choose a model that supports tool use, "
                    "such as Claude Opus 5.5.")


def explained(message):
    """A provider's error in the household's words where it's one they can act on."""
    return NO_TOOLS_MESSAGE if NO_TOOLS.search(message or "") else message


def task_text(instructions, payload):
    """What a role's AI Task is told: its instructions, then its input as JSON."""
    return f"{instructions}\n\nInput (JSON):\n{json.dumps(payload, ensure_ascii=False, sort_keys=True)}"


async def run_role(hass, *, role, entity_id, instructions, payload, structure, llm_api=None,
                   allow_paid_inference=False):
    """One AI Task call. Returns a run record; never retries or resamples."""
    if not allow_paid_inference:
        raise CaseError("Paid inference has not been authorized")
    if not entity_id:
        raise CaseError("No AI Task entity is configured for the improver; open its Configure screen")
    text = task_text(instructions, payload)
    record = {"role": role, "entity_id": entity_id, "started": utcnow().isoformat(),
              "instructions_sha256": digest(instructions), "input_sha256": digest(payload),
              "input_chars": len(text), "status": "running", "output": None, "error": None, **ids()}
    # Ended however the call ends (failures.closing); its caller keeps it, inside a record that closes the same way.
    async with closing(None, record):
        # One span per call; the tools it calls get theirs inside (trace.traced_tool).
        async with span(f"ai.{role}", role=role, model=entity_id, input_chars=len(text)) as section:
            with role_marker(role), own_trace(hass) as trace:
                try:
                    result = await ai_task.async_generate_data(
                        hass, task_name=f"Assist Improver: {role}", entity_id=entity_id,
                        instructions=text, structure=structure, llm_api=llm_api)
                    record.update(status="completed", output=plain(result.data),
                                  conversation_id=result.conversation_id)
                except Exception as err:
                    record.update(status="failed", error={"type": type(err).__name__,
                                                          "message": explained(str(err))[:500]})
                    section.fail(f"{type(err).__name__}: {err}")
            stats = []  # The usage the provider reported, if it can be read: never the call's outcome.
            with touchpoints.guarded(hass, "conversation_trace"):
                stats = [plain(e["data"]["stats"]) for e in (trace.as_dict()["events"] if trace else ())
                         if e["event_type"] == ConversationTraceEventType.AGENT_DETAIL
                         and isinstance(e["data"], dict) and "stats" in e["data"]]
            section.tag(status=record["status"], tokens=tokens(stats) or None)
        record["usage"] = {"status": "provider_reported_partial" if stats else "unavailable", "events": stats,
                           "billing_total": None,
                           "limits": "Provider-reported token counts where available; not an exact bill"}
        record["finished"] = utcnow().isoformat()
        record["seconds"] = round((parse_datetime(record["finished"])
                                   - parse_datetime(record["started"])).total_seconds(), 2)
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


RECENT = 50  # Analyses (and quick checks) the run confirmation's rough token estimate goes by.


def _analysis(run):
    return run.get("kind") == "journey_analyzer" and run.get("status") == "completed" and bool(run.get("analysis"))


def _tokens(usage):
    spent = tokens([e for e in (usage or {}).get("events") or [] if isinstance(e, dict)])  # Only what reads as counts.
    return spent.get("input_tokens", 0) + spent.get("output_tokens", 0)


def held_verdict(run):
    """Whether a run record is a quick check's verdict held for its analysis (analysis.kept_verdict reuses it)."""
    return (run.get("kind") == "journey_analyzer" and run.get("status") == "held"
            and (run.get("triage") or {}).get("status") == "completed")


def recent_spend(runs):
    """The provider-reported tokens (input plus output) the last RECENT analyses and quick checks in a run log used,
    each on average (None for none): a rough guide only. A verdict kept from an earlier check cost nothing this time."""
    spent = {"analyze": [], "triage": []}
    for run in reversed(runs):
        if len(spent["analyze"]) == len(spent["triage"]) == RECENT:
            break
        if run.get("kind") != "journey_analyzer":
            continue
        if (_analysis(run) and run.get("answer_status") != "triaged_smooth" and len(spent["analyze"]) < RECENT
                and (used := _tokens(run.get("usage")))):
            spent["analyze"].append(used)
        check = run.get("triage") or {}
        if ((_analysis(run) or held_verdict(run)) and check.get("status") == "completed" and not check.get("kept")
                and len(spent["triage"]) < RECENT and (used := _tokens(check.get("usage")))):
            spent["triage"].append(used)
    return {stage: sum(used) / len(used) if used else None for stage, used in spent.items()}


def _frozen(run):
    """A record's JSON, made once when it's kept: every later write of the log copies it as it is. One that won't
    serialize stays as it is, for the store to report as it always has."""
    try:
        return orjson.Fragment(json_bytes(run))
    except TypeError:
        return run


class RoleRuns:
    """The run log: a private record of every AI role call and pipeline run (records.RUN_LOG).

    A kept record changes only by being recorded again, never in place: so its JSON is made once, when it's kept, and
    a write of the log only joins those, off the event loop (the store serializes in the executor)."""
    def __init__(self, hass):
        self.store = Store(hass, STORAGE_VERSION, f"{DOMAIN}.role_runs", private=True, atomic_writes=True,
                           serialize_in_event_loop=False)
        self.runs: list[RoleCall | PipelineRun] = []  # By kind: records.RUN_LOG.
        self.lock = asyncio.Lock()
        self._json, self._json_for = [], None  # Each record's JSON, in order, and the list of runs it's for.

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
            if run["status"] == "running":  # A pipeline run's error is text (records.PipelineRun): none to add.
                run.update(status="interrupted", error=None if run.get("kind") == "pipeline" else
                           {"message": "Reload during the call; not repeated"})
        await self._save()

    async def record(self, run):
        """Keep a record, at the end of the log, replacing any with its id (an old log's may have several). One a
        pipeline run creates names the run and its trace."""
        kept = deepcopy(run)
        if kept.get("kind") != "pipeline":
            for key, value in ids().items():
                kept.setdefault(key, value)
        async with self.lock:
            others = [(r, f) for r, f in zip(self.runs, self._frozen_runs()) if r.get("id") != run["id"]]
            self.runs = self._json_for = [*(r for r, _ in others), kept]
            self._json = [*(f for _, f in others), _frozen(kept)]
            await self._save()
        return deepcopy(kept)

    async def drop(self, recording_ids):
        """Forget deleted conversations' records (their analyses, verdicts and attempts); how many went."""
        async with self.lock:
            kept = [(r, f) for r, f in zip(self.runs, self._frozen_runs()) if r.get("recording_id") not in recording_ids]
            dropped = len(self.runs) - len(kept)
            if dropped:
                self.runs = self._json_for = [r for r, _ in kept]
                self._json = [f for _, f in kept]
                await self._save()
        return dropped

    async def prune(self, recordings, now=None):
        """The daily tidy-up (retention.prune_runs); returns what it did."""
        async with self.lock:
            runs, done = prune_runs(self.runs, recordings, now)
            if done["deleted"]:
                before = {id(r): f for r, f in zip(self.runs, self._frozen_runs())}  # Kept records keep theirs.
                self._json = [before[id(r)] for r in runs]
                self.runs = self._json_for = runs
                await self._save()
        return done
