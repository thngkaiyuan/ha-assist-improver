"""One run of the pipeline: analyze, propose, test, then write up improvements for review.

Started from the panel (Run now, after the user confirms what it will spend) or on
the schedule (off by default). Each stage works on its backlog; code decides what
that is, and the AI roles are the same ones the admin actions run:

1. Analyze: each finished conversation not analyzed yet (each gets the quick
   check first), except conversations the household marked as tests.
2. Propose: the hypothesis generator and its reviewer, over every analysis, with the
   household's decisions on earlier improvements; it runs when there are new
   analyses with friction, or revisions asked for, since it last ran.
3. Test: every hypothesis on every goal it names, up to the tests-per-goal setting.
4. Write up: the editor's headline and what-you-get; then improvements go up for
   review in the panel and the notification.

Conversations wait at the start (Your Assist) until a run takes them in; then each
stage works on what reached it and passes its output on: triage (each conversation
gets the quick check; all pass through when no triage model is set), then
analysis, and so on. A stage the household blocked (a gate) is skipped: what reached it
waits there, and later stages still work on theirs. One run at a time; its progress is
kept, so the panel shows it, and a run cut off by a restart is marked interrupted,
never restarted on its own.

Each run has a trace (trace.py) under the run's id: a span for the run, each stage, each conversation, the
proposal, each test and each write-up, with the AI calls and their tools inside.

On-demand improvements (a setting, off by default): only the conversations the user filed (Assist's
file_improvement tool, llm.py) are taken in, each as soon as it ends: a run starts then, or right after the one in
progress. A filed conversation skips the quick check: the user already said it needs a closer look.
"""

import asyncio
from copy import deepcopy
import logging
import time
from uuid import uuid4

from homeassistant.core import callback
from homeassistant.helpers.storage import Store
from homeassistant.util.dt import utcnow

from .cases import CaseError
from .const import CONF_AUTO_ACCEPT, CONF_ON_DEMAND, CONF_TESTS_PER_GOAL, DEFAULT_TESTS_PER_GOAL, DOMAIN
from .records import PipelineRun
from .reflective import index_of
from .settings import GATES, blocked_stages, listed, paused, role_model
from .trace import for_run, ids, journal, span, tag

_LOGGER = logging.getLogger(__name__)
MAX_ATTEMPTS = 3  # A conversation that keeps failing for an unexpected reason is set aside after this many runs.

STAGES = ("analyze", "propose", "test", "write")
STORE = f"{DOMAIN}.pipeline"  # Where each taken-in conversation waits: {"at": {recording id: "triage" | "analyze"}}.


def editor_model(entry):
    """The editor's model: its own setting, else the quick-check model, else the follow-up check's, else the main one."""
    return role_model(entry, "editor")
CLOSED = ("session_expired", "interrupted", "capture_timed_out")


class Pipeline:
    def __init__(self, hass, entry, runtime):
        self.hass, self.entry, self.runtime, self.recorder = hass, entry, runtime, runtime.recorder
        self.current: PipelineRun | None = None  # The run in progress, or the last one.
        self._task = None
        self._again = False  # A filed conversation ended during a run: run again right after it.
        self.store = Store(hass, 1, STORE, private=True, atomic_writes=True)
        self.waiting: dict[str, str] = {}  # {recording id: the stage it waits at}, as last kept (_at() leaves out what's done).

    async def load(self):
        self.waiting = ((await self.store.async_load()) or {}).get("at", {})

    # On-demand improvements.

    @property
    def on_demand(self):
        return bool(self.entry.options.get(CONF_ON_DEMAND))

    def filed(self, rid):
        return bool(self.recorder.index.get(rid, {}).get("filed"))

    @callback
    def request(self, _recording_id=None):
        """A filed conversation ended (on-demand improvements): run now, or again right after the run in progress."""
        if not self.on_demand:
            return
        if self.running:
            self._again = True
            return
        try:
            self.start(trigger="filed")
        except CaseError:  # A step's model isn't available: recorded as failed, with why.
            pass

    @callback
    def _ran(self, task):
        if self._again and not task.cancelled():
            self._again = False
            self.request()

    # What a run would do, for the confirmation before it starts.

    def _runs(self):
        """The run log's index (reflective.RunIndex): what the stages read from the log, without scanning it."""
        return index_of(self.runtime.role_runs)

    def _analyzed(self):
        """{recording id: its latest accepted analysis}."""
        return dict(self._runs().analyzed)

    def _tests(self):
        """Recordings the household said were tests (ignored when deciding an improvement)."""
        out = set()
        for item in self.runtime.improvements.items.values():
            if any(d["decision"] == "was_test" for d in item.get("decisions", [])):
                out |= {t["case_id"] for t in item.get("tests", [])}
        recordings = set()
        for case_id in out:
            try:
                recordings.add(self.runtime.casebook.get(case_id)["recording_id"])
            except CaseError:
                continue
        return recordings

    def _unreadable(self):
        """Recordings no role can read (a run found them malformed): skipped for good, not retried."""
        return set(self._runs().unreadable)

    def _readable(self, rid, runs=None):
        """A conversation with something to analyse: still recorded (the tidy-up deletes empty ones), not one Home
        Assistant handled itself, with no turns recorded (user_turns 0), and not one a run found malformed."""
        runs = self._runs() if runs is None else runs
        summary = self.recorder.index.get(rid)
        return summary is not None and summary.get("user_turns") != 0 and rid not in runs.unreadable

    def to_analyze(self, tests=None, picked=False):
        """Finished conversations not analysed yet; only those sent in (filed) with on-demand improvements on, or
        for a run on picked conversations."""
        runs, tests = self._runs(), self._tests() if tests is None else tests
        filed_only = picked or self.on_demand
        return sorted(rid for rid, summary in self.recorder.index.items()
                      if summary.get("status") in CLOSED and rid not in runs.analyzed and rid not in tests
                      and self._readable(rid, runs) and (not filed_only or summary.get("filed")))

    def _at(self):
        """{recording id: the stage it waits at} for conversations a run took in that aren't analysed yet."""
        runs = self._runs()
        return {rid: stage for rid, stage in self.waiting.items() if rid not in runs.analyzed and self._readable(rid, runs)}

    async def _save_at(self, at):
        self.waiting = dict(at)
        await self.store.async_save({"at": self.waiting})

    def _new(self):
        """Finished conversations no run has taken in yet: they wait at the start."""
        at = self._at()
        return [rid for rid in self.to_analyze(picked=bool((self.current or {}).get("picked"))) if rid not in at]

    def _last_generator(self):
        return self._runs().generator

    def _untested(self, copy=True):
        """The hypotheses the last proposal left untested (its tests were paused or cut off), with its run: a copy, as
        a kept record changes only by being recorded again (copy=False: only to count them)."""
        _, run = self._last_generator()
        if run is None or run.get("tested") is not False:
            return [], None
        run = deepcopy(run) if copy else run
        return (run.get("output") or {}).get("hypotheses") or [], run

    def _to_propose(self, tests=None):
        """What the proposer has to work on: analyses with friction since it last ran, and revisions asked for."""
        from .hypotheses import friction_by_goal, short
        tests = self._tests() if tests is None else tests
        count, settled = 0, set(self.runtime.improvements.settled_goals())
        for rid, analysis in self._runs().since:
            if rid not in tests:
                try:  # Goals an existing improvement already covers (marked duplicates too) go no further.
                    count += any(f"{short(rid)}:{turn}" not in settled for turn in friction_by_goal(analysis))
                except (KeyError, TypeError):
                    count += bool(analysis.get("goals"))
        return count + self._revisions()

    def _revisions(self):
        """Improvements the household asked to revise: they wait at Propose."""
        return sum(1 for i in self.runtime.improvements.items.values() if i["status"] == "revision_requested")

    def backlog(self, todo=None, tests=None):
        """What waits at each stage: {stage: count}, as the settings view shows it, and how many of those at Propose
        are revisions. todo, tests: to_analyze() and _tests(), if known."""
        tests = self._tests() if tests is None else tests
        at = self._at()
        waiting = list(at.values())
        sent = sum(1 for rid, stage in at.items() if stage == "triage" and self.filed(rid))  # They skip the quick check.
        return {"requests": sum(1 for rid in (self.to_analyze(tests) if todo is None else todo) if rid not in at),
                "triage": waiting.count("triage") - sent, "analyze": waiting.count("analyze") + sent,
                "propose": self._to_propose(tests), "revisions": self._revisions(),
                "test": len(self._untested(copy=False)[0]),
                "write": len(self.runtime.improvements.unwritten()),
                "review": len(self.runtime.improvements.pending()),
                "household": sum(1 for i in self.runtime.improvements.items.values() if i["status"] == "applied")}

    def preview(self):
        """What Run now will do, in numbers the confirmation shows (no per-call cost accounting: coarse controls)."""
        options = self.entry.options
        tests = self._tests()
        todo = self.to_analyze(tests)
        return {"conversations_to_analyze": len(todo), "backlog": self.backlog(todo, tests),
                "paused": sorted(paused(self.entry)),
                "conversations_recorded": sum(1 for s in self.recorder.index.values() if s.get("status") in CLOSED),
                "conversations_analyzed": len(self._runs().analyzed),
                "quick_check": bool(role_model(self.entry, "triage")),
                "tests_per_goal": int(options.get(CONF_TESTS_PER_GOAL, DEFAULT_TESTS_PER_GOAL)),
                "blocked_models": {stage: titles for stage in GATES  # Each stage, so Run one step can tell too.
                                   for titles in [blocked_stages(self.hass, self.entry, stage).get(stage)] if titles},
                "running": self.running}

    @property
    def running(self):
        return self._task is not None and not self._task.done()

    # Running.

    def start(self, *, trigger, force=False, only=None, picked=False):
        """force: propose again over every analysis even with nothing new (Run anyway). only: run just that stage
        (the others hold their backlog, as if paused, for this run). picked: a run on the conversations the user
        picked: it takes in only those sent in, whatever on-demand improvements are set to."""
        if self.running:
            raise CaseError("A run is already in progress")
        if blocked := blocked_stages(self.hass, self.entry, only):
            steps = list(dict.fromkeys(t for titles in blocked.values() for t in titles))
            reason = (f"{listed(steps)} can't run: {'its AI model isn' if len(steps) == 1 else 'their AI models aren'}"
                      "'t available. Choose another in the settings.")
            if trigger in ("scheduled", "filed"):  # Nobody is watching: kept for the history, not failed midway.
                now = utcnow().isoformat()
                self.current = {"id": uuid4().hex, "kind": "pipeline", "trigger": trigger, "status": "failed",
                                "started": now, "finished": now, "stage": None, "progress": {}, "error": reason,
                                "force": False, "only": None}
                journal("warning", f"{trigger.capitalize()} run not started: {reason}", run_id=self.current["id"])
                self.hass.async_create_task(self.runtime.role_runs.record(self.current))
            raise CaseError(reason)
        run_id = uuid4().hex
        self.current = {"id": run_id, "kind": "pipeline", "trigger": trigger, "status": "running",
                        "started": utcnow().isoformat(), "stage": STAGES[0], "progress": {}, "error": None,
                        "force": force, "only": only, "trace_id": run_id, **({"picked": True} if picked else {})}
        self._task = self.hass.async_create_background_task(self._run(), "assist_improver_pipeline", eager_start=False)
        self._task.add_done_callback(self._ran)
        return deepcopy(self.current)

    async def cancel(self):
        if self.running:
            self._task.cancel()

    async def stop(self):
        """Unloading: a run in progress is cancelled and waited for, so nothing writes after the stores close."""
        if self.running:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def status(self):
        return {"run": deepcopy(self.current), "preview": self.preview()}

    async def _stage(self, stage, **progress):
        self.current["stage"] = stage
        self.current["progress"].update(progress)
        await self.runtime.role_runs.record(self.current)  # Kept, so a restart loses nothing.

    async def _guarded(self, stage, work):
        """One stage's work. An unexpected failure stays with that stage: kept with why (and logged with its
        traceback, for a bug report), and the run goes on to the stages that don't need it. False if it failed."""
        began = time.monotonic()
        try:
            async with span(f"stage.{stage}"):
                await work()
            return True
        except asyncio.CancelledError:
            raise
        except Exception as err:
            _LOGGER.exception("Assist Improver: the %s stage failed", stage)
            self.current["progress"].setdefault("problems", []).append(
                {"stage": stage, "error": f"{type(err).__name__}: {err}"[:500]})
            return False
        finally:  # Where a run's time goes, per stage (diagnostics and the debug log).
            seconds = round(time.monotonic() - began, 1)
            self.current["progress"].setdefault("timings", {})[stage] = seconds
            _LOGGER.debug("Assist Improver: %s took %.1f s", stage, seconds)

    async def _run(self):
        """The run, in its own trace (its id is the run's)."""
        run = self.current
        async with for_run(run["id"]), span("run", trigger=run["trigger"], only=run.get("only"),
                                            force=run.get("force")):
            journal("info", f"Run started: {run['trigger']}" + (f", {run['only']} only" if run.get("only") else ""))
            await self._stages()

    async def _stages(self):
        from .proposal import propose as run_proposal
        from .validation import test_hypotheses
        run = self.current
        only = run.get("only")  # Read at each stage: a pause applies right away.
        gate = (lambda stage: stage == only) if only else (lambda stage: stage not in paused(self.entry))
        state = {"hypotheses": [], "generator": None, "analyses": [], "settled": {}}

        async def analyze():
            await self._stage("analyze")
            at = self._at()
            if not only or only == "triage":  # A run takes in what waits at the start.
                new = self._new()
                # One the user filed skips the quick check: they already said it needs a closer look.
                at.update({rid: "analyze" if self.filed(rid) else "triage" for rid in new})
                run["progress"]["taken_in"] = len(new)
                await self._save_at(at)
            if sent := [rid for rid, stage in at.items() if stage == "triage" and self.filed(rid)]:
                at.update(dict.fromkeys(sent, "analyze"))  # Sent in while it waited for the quick check: skips it too.
                await self._save_at(at)
            todo = [rid for rid, stage in at.items() if gate(stage)]
            run["progress"].update(analyze_total=len(todo), analyzed=0, skipped_as_smooth=0, analyze_failed=0)
            checker, tried = role_model(self.entry, "triage"), set()
            for recording_id in todo:  # Triage first: a quick check, or pass through without a triage model.
                if at.get(recording_id) != "triage":
                    continue
                if not checker or self.filed(recording_id):  # Sent in since the run began: no quick check either.
                    at[recording_id] = "analyze"
                    continue
                result = await self._analyze(recording_id, triage_only=not gate("analyze"))
                tried.add(recording_id)  # Analysed too (analysis is open): a failure waits for the next run.
                self._count(result, at, recording_id)
                await self._save_at(at); await self._stage("analyze")
            if gate("analyze"):
                for recording_id in [rid for rid, stage in at.items() if stage == "analyze" and rid not in tried]:
                    result = await self._analyze(recording_id, full=True)  # It passed triage already.
                    self._count(result, at, recording_id)
                    await self._save_at(at); await self._stage("analyze")
            await self._save_at(at)
            run["progress"]["held"] = sum(1 for stage in at.values() if not gate(stage))

        async def propose():
            tests = self._tests()
            state["analyses"] = analyses = [a for rid, a in self._analyzed().items() if rid not in tests]
            # Goals with an improvement already: left out.
            state["settled"] = settled = self.runtime.improvements.settled_goals()
            state["hypotheses"], state["generator"] = self._untested()
            if not (analyses and gate("propose") and (self._to_propose(tests) or run.get("force")
                                                      or self._last_generator()[1] is None)):
                return
            await self._stage("propose", sessions=len(analyses))
            async with span("propose", sessions=len(analyses)) as section:
                generator = await run_proposal(self.hass, self.entry, self.runtime, "generate_hypotheses",
                                              analyses=analyses, feedback=self.runtime.improvements.feedback(),
                                              settled=settled,
                                              existing=self.runtime.improvements.catalog(detailed=True),
                                              allow_paid_inference=True)
                section.tag(status=generator.get("status"), generator_run=generator.get("id"))
            hypotheses = (generator.get("output") or {}).get("hypotheses") or []
            if generator.get("status") == "completed" and (found := await self.runtime.improvements.note_addressed(
                    (generator.get("output") or {}).get("already_addressed"))):  # Kept with what covers them.
                run["progress"]["already_addressed"] = run["progress"].get("already_addressed", 0) + found
            run["progress"].update(hypotheses=len(hypotheses), generator_run=generator.get("id"),
                                   generator_status=generator.get("status"))
            if generator.get("status") == "completed" and hypotheses:
                generator["tested"] = False  # Until its tests ran: a paused or cut-off test stage keeps them.
                await self.runtime.role_runs.record(generator)
            state["hypotheses"], state["generator"] = hypotheses, generator

        async def simulate():
            hypotheses, generator = state["hypotheses"], state["generator"]
            if not (hypotheses and gate("test")):
                return
            await self._stage("test", hypotheses=len(hypotheses))
            try:
                report = await test_hypotheses(self.hass, self.entry, self.runtime, hypotheses=hypotheses,
                                               analyses=state["analyses"], skip_goals=set(state["settled"]),
                                               allow_paid_inference=True)
            except Exception:
                # Tried again next run, but not forever: after MAX_ATTEMPTS failures these ideas are set aside.
                generator["test_attempts"] = generator.get("test_attempts", 0) + 1
                generator["tested"] = generator["test_attempts"] >= MAX_ATTEMPTS
                await self.runtime.role_runs.record(generator)
                raise
            generator["tested"] = True
            await self.runtime.role_runs.record(generator)
            run["progress"].update(tested=report.get("tested", 0))
            before = {i["id"] for i in self.runtime.improvements.items.values()}
            await self.runtime.improvements.refresh()
            run["progress"].update(new_improvements=sum(1 for i in self.runtime.improvements.items.values()
                                                        if i["id"] not in before))

        async def write():
            if not (gate("write") and (waiting := self.runtime.improvements.unwritten())):
                return
            # What the household reads first: the headline and what they get, by the editor; then up for review.
            await self._stage("write", writing=len(waiting))
            written = await self.runtime.improvements.write_copy(editor_model(self.entry))
            if self.entry.options.get(CONF_AUTO_ACCEPT):  # The household chose to accept without reviewing.
                run["progress"]["auto_accepted"] = await self.runtime.improvements.auto_accept(list(written))

        try:
            await self._guarded("analyze", analyze)
            if await self._guarded("propose", propose):  # Simulate tests what Propose gave it (or left untested).
                await self._guarded("test", simulate)
            await self._guarded("write", write)  # Earlier improvements still get written up.
            await self._finish("done")
        except asyncio.CancelledError:
            await self._finish("cancelled")
            raise
        except Exception as err:  # A run never takes the integration down; its error is kept for the panel.
            _LOGGER.exception("Assist Improver: the run failed")
            await self._finish("failed", error=f"{type(err).__name__}: {err}"[:500])

    def _strikes(self, recording_id):
        """How many runs failed on this conversation for an unexpected reason."""
        return self._runs().strikes[recording_id]

    async def _analyze(self, recording_id, **kwargs):
        """Triage and analysis of one conversation, as a span: its steps, and how it ended."""
        steps = "triage" if kwargs.get("triage_only") else "analysis" if kwargs.get("full") else "triage+analysis"
        async with span("conversation", recording_id=recording_id, steps=steps) as section:
            result = await self._attempt(recording_id, **kwargs)
            section.tag(status=result.get("status"), answer_status=result.get("answer_status"))
            return result

    async def _attempt(self, recording_id, **kwargs):
        """One no role can read is skipped for good, with why. One that fails for an unexpected reason is logged
        and tried again next run, up to MAX_ATTEMPTS. The run goes on."""
        from .analysis import analyze
        record = {"id": uuid4().hex, "kind": "journey_analyzer", "recording_id": recording_id, "status": "failed",
                  "finished": utcnow().isoformat(), **ids()}
        try:
            return await analyze(self.hass, self.entry, self.runtime, recording_id, allow_paid_inference=True,
                                  **kwargs)
        except asyncio.CancelledError:
            raise
        except CaseError as err:
            return await self.runtime.role_runs.record({**record, "id": f"unreadable-{recording_id}",
                                                         "answer_status": "unreadable", "error": {"message": str(err)}})
        except Exception as err:
            _LOGGER.exception("Assist Improver couldn't analyse conversation %s", recording_id)
            message = f"{type(err).__name__}: {err}"[:500]
            if self._strikes(recording_id) + 1 >= MAX_ATTEMPTS:
                return await self.runtime.role_runs.record({
                    **record, "id": f"unreadable-{recording_id}", "answer_status": "unreadable",
                    "error": {"message": f"Set aside after {MAX_ATTEMPTS} failed runs: {message}"}})
            return await self.runtime.role_runs.record({**record, "answer_status": "error",
                                                         "error": {"message": message}})

    def _count(self, result, at, recording_id):
        """Where a conversation goes after triage or analysis, and the run's counts."""
        progress = self.current["progress"]
        progress["analyzed"] += 1
        if result.get("analysis"):  # Analysed (or triaged as smooth): done with the early stages.
            at.pop(recording_id, None)
            progress["skipped_as_smooth"] += result.get("answer_status") == "triaged_smooth"
            if marked := len(result["analysis"].get("duplicates") or []):  # Already fixed: they stop here.
                progress["already_addressed"] = progress.get("already_addressed", 0) + marked
        elif result.get("answer_status") == "unreadable":
            at.pop(recording_id, None)  # Nothing any role can read: not retried.
            progress["unreadable"] = progress.get("unreadable", 0) + 1
        elif result.get("status") == "held":
            at[recording_id] = "analyze"  # Triage's verdict is kept; analysis is blocked.
        else:
            at[recording_id] = "analyze"  # Failed: tried again next run.
            progress["analyze_failed"] += 1
            if not progress.get("problem") and (why := (result.get("error") or {}).get("message")):
                progress["problem"] = why  # The first failure's reason, for the history.

    async def _finish(self, status, *, note=None, error=None):
        self.current.update(status=status, finished=utcnow().isoformat(), note=note, error=error)
        tag(outcome=status)
        journal("error" if status == "failed" else "info", f"Run {status}" + (f": {error}" if error else ""),
                timings=self.current["progress"].get("timings"), problems=self.current["progress"].get("problems"))
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
