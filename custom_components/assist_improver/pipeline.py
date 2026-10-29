"""Run control: when a run starts, one at a time, and how it ends. What a run admits and how each conversation
moves is the engine's (flow.py, SPEC.md P0); this starts it, keeps its record and stops it.

How runs start (trigger):
- manual: Run in the panel, after the household confirms what it will spend (preview, read from the flow record). A
  full run, or one on the conversations picked (picked); Run one step runs one stage (only).
- scheduled: a full run at 03:00, daily or weekly (schedule; off by default).
- filed: the fast lane (on-demand improvements, a setting): a conversation sent in gets a run of its own once it ends,
  or right after the run in progress; after a restart, so does each one sent in and still waiting.
- revision or conflict: a conversation the household sent back to Propose gets a run of its own, now or right after
  the run in progress (P0.7).

A run that needs a stage whose AI model isn't available doesn't start (refusal); one nobody is watching is kept as
failed, with why. A run's record is kept from the start and after each move, so a restart that cuts it off shows it
interrupted, never restarted on its own; Cancel ends it cancelled. Its problems are kept in its progress (failures.py)
and its trace under its id (trace.py).
"""

import asyncio
from copy import deepcopy
import logging
from uuid import uuid4

from homeassistant.core import callback
from homeassistant.util.dt import utcnow

from .cases import CaseError
from .const import CONF_ON_DEMAND
from . import failures
from .flow import KEYS, deliberate
from .records import PipelineRun
from .reflective import index_of
from .settings import GATES, blocked_stages, listed, paused, role_model
from .trace import for_run, journal, span, tag

_LOGGER = logging.getLogger(__name__)


def editor_model(entry):
    """The editor's model: its own setting, else the quick-check model, else the feedback check's, else the main one."""
    return role_model(entry, "editor")


class Pipeline:
    def __init__(self, hass, entry, runtime):
        self.hass, self.entry, self.runtime, self.recorder = hass, entry, runtime, runtime.recorder
        self.current: PipelineRun | None = None  # The run in progress, or the last one.
        self._task = None
        self._filed, self._sent_back = [], []  # What waits for a run of its own, in turn (request, send_back).
        self._cancelled = False  # The household cancelled the run in progress (else a stop cut it off: interrupted).
        self._stopping = False  # Unloading: nothing new starts.

    # On-demand improvements.

    @property
    def on_demand(self):
        return bool(self.entry.options.get(CONF_ON_DEMAND))

    def filed(self, rid):
        """Sent in, and waiting for a run (its mark on the flow record, flow.Flow.sent_in): the fast lane's."""
        return bool(self.runtime.flow.record(rid)["sent"])

    @callback
    def request(self, *recording_ids):
        """Filed conversations ended, or were waiting at a restart (on-demand improvements): each gets a run of its own
        (the fast lane, SPEC.md P0.7), one after another, now or right after the run in progress."""
        if self.on_demand:
            self._filed += [rid for rid in recording_ids if rid not in self._filed]
            self._next_filed()

    def _next_filed(self):
        while self._filed and not self.running:
            try:
                self.start(trigger="filed", picked=[self._filed.pop(0)])
            except CaseError:  # A step's model isn't available: recorded as failed, with why, once; the others stay
                self._filed.clear()  # sent in, waiting for a run.

    @callback
    def send_back(self, recording_id, trigger):
        """A conversation sent back to Propose (trigger: revision, or conflict) gets a run of its own: now, or right
        after the run in progress (SPEC.md P0.7)."""
        self._sent_back.append((recording_id, trigger))
        self._next_sent_back()

    def _next_sent_back(self):
        while self._sent_back and not self.running:
            recording_id, trigger = self._sent_back.pop(0)
            try:
                self.start(trigger=trigger, conversation=recording_id)
            except CaseError:  # A step's model isn't available: recorded as failed, with why.
                pass

    @callback
    def _ran(self, task):
        # Conversations deleted during the run go now; none of them is taken in again meanwhile (flow.Flow.waiting).
        self.hass.async_create_task(self.recorder.forget_queued(), "assist_improver_forget_queued")
        # What was sent back or filed meanwhile gets its own run now, in turn, also after the household cancelled this
        # one; not while stopping.
        if not self._stopping and not self.hass.is_stopping:
            self._next_sent_back()
            self._next_filed()

    # What a run would do, for the confirmation before it starts.

    def preview(self):
        """What the panel shows before a run, all read from each conversation's flow record (DETAILS P14.1): each
        stage's count (stages); what's held at each stage, by cause (held: Settings' badges, and what Run one step
        releases there: it takes in nothing new, P0.7); what a manual full run would move (flow.Flow.admitted), less
        what would stop at a blocked stage: the new ones it admits (admits, and which: a few of them by their first
        words) and the held ones it releases, by stage (releases); how many quick checks and analyses that takes, for
        the cost line (quick: new ones without a clear sign, flow.deliberate, and those released at Triage; analyses:
        all but those released past Analyze); and the rest the panel reads (no per-call cost accounting: coarse
        controls)."""
        flow, blocked = self.runtime.flow, paused(self.entry)
        new, released = flow.admitted({"trigger": "manual"})

        def start(rid):  # The stage a new one starts at.
            return "analyze" if deliberate(flow.sent(rid)) else "triage"
        admits = [rid for rid in new if start(rid) not in blocked]
        held, releases = {}, {}
        for rid in released:
            place = flow.place(rid)
            if key := KEYS.get(place["stage"]):  # One cut off as it was admitted waits at Your Assist: no stage's.
                causes = held.setdefault(key, {})
                causes[place["held"]["cause"]] = causes.get(place["held"]["cause"], 0) + 1
            if (key := key or start(rid)) not in blocked:
                releases[key] = releases.get(key, 0) + 1
        checking = [rid for rid in admits if start(rid) == "triage"]
        return {"stages": flow.counts(), "held": held, "admits": len(admits), "releases": releases,
                "which": self._which(admits),
                "quick": len(checking) + releases.get("triage", 0) if role_model(self.entry, "triage") else 0,
                "analyses": 0 if "analyze" in blocked else len(admits) + releases.get("triage", 0)
                + releases.get("analyze", 0),
                "paused": sorted(blocked),
                "blocked_models": {stage: titles for stage in GATES  # Each stage, so Run one step can tell too.
                                   for titles in [blocked_stages(self.hass, self.entry, stage).get(stage)] if titles},
                "spend": self._spend(), "running": self.running,
                "applied": sum(1 for i in self.runtime.improvements.items.values() if i["status"] == "applied")}

    def _which(self, admits, names=3):
        """What was said first in a few of the new conversations a run admits, for its confirmation: those sent in
        first, then the newest. Read from the conversations view's kept words, so nothing is opened; none until that
        view has read them."""
        kept = getattr(getattr(self.runtime, "transcripts", None), "kept", None) or {}
        index = self.recorder.index
        new = sorted(admits, key=lambda rid: index[rid].get("started") or "", reverse=True)
        new.sort(key=lambda rid: not self.filed(rid))  # Stable: the newest first within each.
        said = (((kept.get(rid) or {}).get("turns") or [{}])[0].get("you") for rid in new)
        return [words for words in said if words][:names]

    def _spend(self):
        """Recent analyses' and quick checks' tokens each, on average, with the model each of those steps runs on now
        (as Settings names it): what the confirmation's rough estimate goes by. No prices, no spend tracking."""
        from .settings import model_name
        runs, out = index_of(self.runtime.role_runs), {}  # The run log's index (reflective.RunIndex).
        for stage, role in (("analyze", "analyzer"), ("triage", "triage")):
            if (per := runs.spent[stage].average()) and (entity_id := role_model(self.entry, role)):
                out[stage] = {"tokens": round(per), "model": model_name(self.hass, entity_id)}
        return out

    @property
    def running(self):
        return self._task is not None and not self._task.done()

    # Running.

    def refusal(self, only=None):
        """Why a run (of one step: only) can't start now: a step it needs has no AI model available; else None."""
        if blocked := blocked_stages(self.hass, self.entry, only):
            steps = list(dict.fromkeys(t for titles in blocked.values() for t in titles))
            return (f"{listed(steps)} can't run: {'its AI model isn' if len(steps) == 1 else 'their AI models aren'}"
                    "'t available. Choose another in the settings.")
        return None

    def start(self, *, trigger, only=None, picked=None, conversation=None):
        """trigger: manual or scheduled (a full run, unless picked), filed (the fast lane), or revision or conflict
        (the one conversation sent back to Propose: conversation). only: run just that stage. picked: the recording
        ids of the conversations the user picked (or filed, for the fast lane): it admits only those
        (flow.Flow.admitted)."""
        if self.running:
            raise CaseError("A run is already in progress")
        if reason := self.refusal(only):
            if trigger in ("scheduled", "filed"):  # Nobody is watching: kept for the history, not failed midway.
                now = utcnow().isoformat()
                self.current = {"id": uuid4().hex, "kind": "pipeline", "trigger": trigger, "status": "failed",
                                "started": now, "finished": now, "stage": None, "progress": {}, "error": reason,
                                "only": None}
                journal("warning", f"{trigger.capitalize()} run not started: {reason}", run_id=self.current["id"])
                self.hass.async_create_task(self.runtime.role_runs.record(self.current))
            raise CaseError(reason)
        run_id = uuid4().hex
        self._cancelled = False
        self.current = {"id": run_id, "kind": "pipeline", "trigger": trigger, "status": "running",
                        "started": utcnow().isoformat(), "stage": None, "progress": {}, "error": None,
                        "only": only, "trace_id": run_id, **({"picked": list(picked)} if picked is not None else {}),
                        **({"conversation": conversation} if conversation else {})}
        self._task = self.hass.async_create_background_task(self._run(), "assist_improver_pipeline", eager_start=False)
        self._task.add_done_callback(self._ran)
        return deepcopy(self.current)

    async def cancel(self):
        """The household's Cancel: the run ends as cancelled."""
        if self.running:
            self._cancelled = True
            self._task.cancel()

    async def stop(self):
        """Unloading: a run in progress is cut off (interrupted) and waited for, so nothing writes after the stores
        close."""
        self._stopping = True
        if self.running:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def status(self):
        return {"run": deepcopy(self.current), "preview": self.preview()}

    async def _run(self):
        """The run, in its own trace (its id is the run's), with its problems kept in its progress (failures.py)."""
        run = self.current
        try:
            async with for_run(run["id"]), span("run", trigger=run["trigger"], only=run.get("only")):
                with failures.collecting(run["progress"].setdefault("problems", [])):
                    journal("info", f"Run started: {run['trigger']}"
                            + (f", {run['only']} only" if run.get("only") else ""))
                    await self._flowing()
        except Exception as err:  # Outside any stage (its trace, say): the run's record still closes.
            if run["status"] != "running":
                raise
            _LOGGER.exception("Assist Improver: the run failed")
            await self._finish("failed", error=f"{type(err).__name__}: {err}"[:500])

    async def _flowing(self):
        """The run, by the model's engine (flow.py): what it admits moves, one conversation at a time. Its record is
        kept from the start, so a restart that cuts it off shows it interrupted (RoleRuns.start; DETAILS P5.10), and
        again after each move, with the progress the engine publishes on it."""
        try:
            await self.runtime.role_runs.record(self.current)
            await self.runtime.flow.run(self.current, on_move=lambda: self.runtime.role_runs.record(self.current))
            await self._finish("done")
        except asyncio.CancelledError:  # The household's Cancel, or Home Assistant stopping (interrupted).
            await self._finish("cancelled" if self._cancelled else "interrupted")
            raise
        except Exception as err:  # A run never takes the integration down; its error is kept for the panel.
            _LOGGER.exception("Assist Improver: the run failed")
            await self._finish("failed", error=f"{type(err).__name__}: {err}"[:500])

    async def _finish(self, status, *, note=None, error=None):
        self.current.update(status=status, finished=utcnow().isoformat(), note=note, error=error)
        tag(outcome=status)
        journal("error" if status == "failed" else "info", f"Run {status}" + (f": {error}" if error else ""),
                problems=self.current["progress"].get("problems"))
        await self.runtime.role_runs.record(self.current)


SCHEDULES = {"off": None, "daily": None, "weekly": 6}  # weekly: Sundays (weekday 6), at 03:00.


def schedule(hass, entry, pipeline, choice):
    """A scheduled run at 03:00 (daily, or Sundays for weekly); returns the unsubscribe, or None when off."""
    from homeassistant.helpers.event import async_track_time_change
    if choice not in ("daily", "weekly"):
        return None

    @callback
    def fire(now):
        if choice == "weekly" and now.weekday() != SCHEDULES["weekly"]:
            return
        if not pipeline.running:
            try:
                pipeline.start(trigger="scheduled")
            except CaseError:  # A step's model isn't available: the run is recorded as failed, with why.
                pass

    return async_track_time_change(hass, fire, hour=3, minute=0, second=0)
