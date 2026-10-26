"""One run of the pipeline: analyze, propose, test, then write up improvements for review.

Started from the panel (Run now, after the user confirms what it will spend) or on
the schedule (off by default). Each stage works on its backlog; code decides what
that is, and the AI roles are the same ones the admin actions run:

1. Analyze: each finished conversation not analyzed yet (each gets the quick
   check first), except conversations the household marked as tests; and each one
   sent in again after its analysis (resent), whatever it found, for one fresh look.
2. Propose: the hypothesis generator and its reviewer, over every analysis, with the
   household's decisions on earlier improvements; it runs when there are new
   analyses with friction, friction no proposal has had yet, or revisions asked for,
   since it last ran.
3. Test: every hypothesis on every goal it names, up to the tests-per-goal setting over the goal's lifetime. A goal
   with no fix found (the proposer said why, or its tests ran out without one passing) is settled: no later run
   spends anything on it, unless the household sends its conversation in again (a fresh budget).
4. Write up: the editor's headline and what-you-get; then improvements go up for
   review in the panel and the notification.

Conversations wait at the start (Your Assist) until a run takes them in; then each
stage works on what reached it and passes its output on: triage (each conversation
gets the quick check; all pass through when no triage model is set), then
analysis, and so on. A stage the household blocked (a gate) is skipped: what reached it
waits there, and later stages still work on theirs. One run at a time; its progress is
kept, so the panel shows it, and a run cut off by a restart is marked interrupted,
never restarted on its own. What goes wrong stays where it happened (failures.py): one item's failure (a
conversation, a hypothesis, a goal's test, a write-up) with that item, kept in the run's problems, while the stage
goes on with the others; a stage's own failure with that stage, while the stages that don't need it go on.

Each run has a trace (trace.py) under the run's id: a span for the run, each stage, each conversation, the
proposal, each test and each write-up, with the AI calls and their tools inside.

What a run takes in depends on how it started. A full run (Run in the settings, the home page's Run with nothing
picked, or the schedule) takes in every finished conversation not analysed yet. A run on picked conversations (the
home page's Run with some picked: they're filed as it starts) takes in and works on only those sent in; others an
earlier run took in wait for the next full run. On-demand improvements (a setting, on for a new install) add a fast lane: a conversation the user filed (Assist's
file_improvement tool, llm.py) or sent in from the panel gets a run of its own as soon as it ends, or right after the
one in progress; that run (trigger "filed") takes in and works on only conversations sent in, so it stays cheap.
Whichever run takes it in, a conversation sent in gets the quick check first, as a full run's do, unless the user gave
a clear sign it needs a closer look (deliberate): a note written as they sent it in, filing it from Assist, or sending
in again one already analysed (the quick check found it smooth, or no fix was found for it). Those go straight to
analysis, and the analyzer hears the user sent them in.
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
from . import failures
from .failures import MAX_ATTEMPTS  # A conversation that keeps failing for an unexpected reason is set aside then.
from .records import PipelineRun
from .reflective import index_of
from .settings import GATES, blocked_stages, listed, paused, role_model
from .trace import for_run, ids, journal, span, tag

_LOGGER = logging.getLogger(__name__)

STAGES = ("analyze", "propose", "test", "write")
# Where each taken-in conversation waits, {"at": {recording id: "triage" | "analyze"}}, and the items that failed in
# runs in a row, {"setbacks": {...}} (failures.py).
STORE = f"{DOMAIN}.pipeline"


def editor_model(entry):
    """The editor's model: its own setting, else the quick-check model, else the follow-up check's, else the main one."""
    return role_model(entry, "editor")
CLOSED = ("session_expired", "interrupted", "capture_timed_out")


def deliberate(filed):
    """Whether a conversation was sent in with a clear sign it needs a closer look, so it goes straight to analysis,
    past the quick check (filed: its marker, Recorder.file and file_kept): a note written as it was sent in, filed from
    Assist, or sent in again after an analysis (the quick check found it smooth, or no fix was found for it). A marker
    from before filings said where they came from reads as one from Assist, so nothing sent in then changes course.
    One picked, or sent in from the panel without a note, gets the quick check, as a full run's conversations do."""
    return bool(filed) and bool(filed.get("note") or filed.get("via") != "panel" or filed.get("again"))


class Pipeline:
    def __init__(self, hass, entry, runtime):
        self.hass, self.entry, self.runtime, self.recorder = hass, entry, runtime, runtime.recorder
        self.current: PipelineRun | None = None  # The run in progress, or the last one.
        self._task = None
        self._again = False  # A filed conversation ended during a run: run again right after it.
        self.store = Store(hass, 1, STORE, private=True, atomic_writes=True)
        self.waiting: dict[str, str] = {}  # {recording id: the stage it waits at}, as last kept (_at() leaves out what's done).
        self.setbacks = {}  # {item: its failed runs in a row} (failures.Collector).

    async def load(self):
        saved = (await self.store.async_load()) or {}
        self.waiting, self.setbacks = saved.get("at", {}), failures.kept_setbacks(saved.get("setbacks"))

    # On-demand improvements.

    @property
    def on_demand(self):
        return bool(self.entry.options.get(CONF_ON_DEMAND))

    def filed(self, rid):
        """Sent in (filed): what a run on picked conversations and the fast lane take in and work on."""
        return bool(self.recorder.index.get(rid, {}).get("filed"))

    def skips_check(self, rid):
        """Sent in with a clear sign it needs a closer look (deliberate): straight to analysis, past the quick check."""
        return deliberate(self.recorder.index.get(rid, {}).get("filed"))

    def sent_again(self, rid):
        """It has an analysis already, the quick check's smooth verdict or a full one (no fix was found for it): sent
        in now, it's sent in again (file_kept's again), a clear sign (deliberate)."""
        return rid in self._runs().analyzed

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
        # Conversations deleted during the run go now; none of them is taken in again meanwhile (_readable).
        self.hass.async_create_task(self.recorder.forget_queued(), "assist_improver_forget_queued")
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
        Assistant handled itself, with no turns recorded (user_turns 0), not one a run found malformed, and not one
        waiting to be deleted (Recorder.forget_later)."""
        runs = self._runs() if runs is None else runs
        summary = self.recorder.index.get(rid)
        return (summary is not None and summary.get("user_turns") != 0 and rid not in runs.unreadable
                and not summary.get("deleting"))

    def _unanalysed(self, rid, runs):
        """No analysis yet, or sent in again since its latest one (resent). One sent in plainly that the quick check
        then found smooth is done with: it stands smooth, as a full run's would, until it's sent in again."""
        return runs.analyzed.get(rid) is None or self.resent(rid, runs)

    def resent(self, rid, runs=None):
        """Sent in after its latest analysis, whatever that found: smooth at the quick check, no trouble found, or no
        fix found. It waits for one fresh full analysis, which reads the user's note as feedback and takes the old
        one's place for every stage after it. A run takes it past the quick check: the user said it needs a closer
        look (a signal for skipping the check)."""
        runs = self._runs() if runs is None else runs
        filed = (self.recorder.index.get(rid) or {}).get("filed")
        return bool(filed and rid in runs.analyzed and (filed.get("at") or "") > runs.analyzed_at.get(rid, ""))

    def seen(self):
        """The goals with friction ("session:turn") the last proposal was given, or None before the first: from its
        record, or (one kept before it listed them) the goals its answer placed, since it had to place every one."""
        _, run = self._last_generator()
        if run is None:
            return None
        if isinstance(run.get("friction"), list):
            return set(run["friction"])
        answer = run.get("output") or {}
        refs = [g for h in answer.get("hypotheses") or [] for g in h.get("goals") or []] + [
            r for key in ("not_fixed", "already_addressed") for r in answer.get(key) or []]
        return {f"{str(r.get('session'))[:8]}:{r.get('goal_turn')}" for r in refs}

    def unseen(self, tests=None, settled=None):
        """{recording id: its goals with friction that no proposal has had}, among analyses before the last proposal:
        what an upgrade counts as friction now (side effects left, feedback). Not given to the last proposal, though it
        could read their conversation (its sessions: one it couldn't read waits until it can), and no improvement ever
        covered them (a revision waits at Propose on its own). They wait at Propose like a new analysis."""
        from .hypotheses import friction_by_goal, short
        from .improvements import Improvements
        seen = self.seen()
        if seen is None:
            return {}
        runs, tests, improvements = self._runs(), self._tests() if tests is None else tests, self.runtime.improvements
        settled = improvements.settled_goals() if settled is None else settled
        known = seen | set(settled)
        for item in improvements.items.values():
            try:
                known |= set(Improvements._covers(item))
            except (KeyError, TypeError):  # One in an older shape covers what settled_goals says it does.
                continue
        since, out = {rid for rid, _ in runs.since}, {}
        given = self._last_generator()[1].get("sessions")
        for rid, analysis in runs.analyzed.items():
            if rid in since or rid in tests or (isinstance(given, list) and short(rid) not in given):
                continue
            try:
                goals = {f"{short(rid)}:{turn}" for turn in friction_by_goal(analysis)} - known
            except (KeyError, TypeError):  # An analysis in an older shape: nothing new in it.
                continue
            if goals:
                out[rid] = goals
        return out

    def to_analyze(self, tests=None, filed_only=False):
        """Finished conversations not analysed yet: every one, as a full run takes them in; only those sent in (filed)
        with filed_only, as a run on picked conversations and the fast lane do."""
        runs, tests = self._runs(), self._tests() if tests is None else tests
        return sorted(rid for rid, summary in self.recorder.index.items()
                      if summary.get("status") in CLOSED and self._unanalysed(rid, runs) and rid not in tests
                      and self._readable(rid, runs) and (not filed_only or summary.get("filed")))

    def _at(self):
        """{recording id: the stage it waits at} for conversations a run took in that aren't analysed yet."""
        runs = self._runs()
        return {rid: stage for rid, stage in self.waiting.items()
                if self._unanalysed(rid, runs) and self._readable(rid, runs)}

    async def _save_at(self, at):
        self.waiting = dict(at)
        await self._save()

    async def _save(self):
        await self.store.async_save({"at": self.waiting, "setbacks": self.setbacks})

    def _new(self, filed_only=False):
        """Finished conversations no run has taken in yet: they wait at the start (filed_only: as to_analyze)."""
        at = self._at()
        return [rid for rid in self.to_analyze(filed_only=filed_only) if rid not in at]

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

    async def upgrade_side_effects(self):
        """Analyses an earlier version kept before a side effect had to be Assist's own change (with its turn):
        recomputed by code from the recording, once, so a device's own follow-on (a fan's climate turning off with it)
        no longer counts as trouble. No model; one whose recording can't be read stays as it was. How many changed."""
        from .analyzer import side_effects_left
        from .exploration import SessionView
        changed = {}
        for run in self.runtime.role_runs.runs:
            analysis = run.get("analysis") if run.get("kind") == "journey_analyzer" else None
            effects = (analysis or {}).get("side_effects_left") or []
            if not effects or all("turn" in e for e in effects):
                continue
            try:
                view = SessionView(await self.recorder.export(run["recording_id"]))
            except (CaseError, ValueError, KeyError, TypeError):
                continue
            changed[run["id"]] = {**run, "analysis": {**analysis, "side_effects_left": side_effects_left(
                view, analysis.get("goals") or [])}}
        await self.runtime.role_runs.amend(changed)
        return len(changed)

    def _to_propose(self, tests=None):
        """What the proposer has to work on: analyses with friction since it last ran, earlier ones with friction it
        wasn't given (unseen), and revisions asked for."""
        from .hypotheses import friction_by_goal, short
        tests = self._tests() if tests is None else tests
        count, settled = 0, set(self.runtime.improvements.settled_goals())
        for rid, analysis in self._runs().since:
            if rid not in tests:
                try:  # Goals an existing improvement already covers (marked duplicates too) go no further.
                    count += any(f"{short(rid)}:{turn}" not in settled for turn in friction_by_goal(analysis))
                except (KeyError, TypeError):
                    count += bool(analysis.get("goals"))
        return count + len(self.unseen(tests, settled)) + self._revisions()

    def _per_goal(self):
        return int(self.entry.options.get(CONF_TESTS_PER_GOAL, DEFAULT_TESTS_PER_GOAL))

    def _revisions(self):
        """Improvements the household asked to revise: they wait at Propose (until no fix is found for them)."""
        return len(self.runtime.improvements.revising())

    def backlog(self, todo=None, tests=None):
        """What waits at each stage: {stage: count}, as the settings view shows it, and how many of those at Propose
        are revisions. At the start (requests): what a full run takes in; straight: those of them that go past the
        quick check (deliberate). sent: those sent in that a run on picked conversations works on besides the picked
        ones (at the start or at a step; the panel adds the picks); sent_straight: those of them past the quick check
        (deliberate, or waiting at Analyze already). The rough estimate gives the rest the quick check. todo, tests:
        to_analyze() and _tests(), if known."""
        tests = self._tests() if tests is None else tests
        at, todo = self._at(), self.to_analyze(tests) if todo is None else todo
        waiting, sent = list(at.values()), [rid for rid in todo if self.filed(rid)]
        past = sum(1 for rid, stage in at.items() if stage == "triage" and self.skips_check(rid))  # Counted at Analyze.
        return {"requests": sum(1 for rid in todo if rid not in at),
                "straight": sum(1 for rid in todo if rid not in at and self.skips_check(rid)), "sent": len(sent),
                "sent_straight": sum(1 for rid in sent if at.get(rid) == "analyze" or self.skips_check(rid)),
                "triage": waiting.count("triage") - past, "analyze": waiting.count("analyze") + past,
                "propose": self._to_propose(tests), "revisions": self._revisions(),
                "test": len(self._untested(copy=False)[0]),
                "write": len(self.runtime.improvements.unwritten()),
                "review": len(self.runtime.improvements.pending()),
                "household": sum(1 for i in self.runtime.improvements.items.values() if i["status"] == "applied")}

    def preview(self):
        """What a full run (Run now) will do, in numbers the confirmation shows (no per-call cost accounting: coarse
        controls)."""
        options = self.entry.options
        tests = self._tests()
        todo = self.to_analyze(tests)
        return {"conversations_to_analyze": len(todo), "backlog": self.backlog(todo, tests), "which": self._which(todo),
                "paused": sorted(paused(self.entry)),
                "conversations_recorded": sum(1 for s in self.recorder.index.values() if s.get("status") in CLOSED),
                "conversations_analyzed": len(self._runs().analyzed),
                "quick_check": bool(role_model(self.entry, "triage")),
                "tests_per_goal": int(options.get(CONF_TESTS_PER_GOAL, DEFAULT_TESTS_PER_GOAL)),
                "blocked_models": {stage: titles for stage in GATES  # Each stage, so Run one step can tell too.
                                   for titles in [blocked_stages(self.hass, self.entry, stage).get(stage)] if titles},
                "spend": self._spend(), "running": self.running}

    def _which(self, todo, names=3):
        """What was said first in a few of the new conversations a full run takes in, for its confirmation: those sent
        in first, then the newest, leaving out hidden ones. Read from the conversations view's kept words, so nothing
        is opened; none until that view has read them."""
        kept = getattr(getattr(self.runtime, "transcripts", None), "kept", None) or {}
        index, at = self.recorder.index, self._at()
        new = sorted((rid for rid in todo if rid not in at and not index[rid].get("hidden")),
                     key=lambda rid: index[rid].get("started") or "", reverse=True)
        new.sort(key=lambda rid: not index[rid].get("filed"))  # Stable: the newest first within each.
        said = (((kept.get(rid) or {}).get("turns") or [{}])[0].get("you") for rid in new)
        return [words for words in said if words][:names]

    def _spend(self):
        """Recent analyses' and quick checks' tokens each, on average, with the model each of those steps runs on now
        (as Settings names it): what the confirmation's rough estimate goes by. No prices, no spend tracking."""
        from .settings import model_name
        runs, out = self._runs(), {}
        for stage, role in (("analyze", "analyzer"), ("triage", "triage")):
            if (per := runs.spent[stage].average()) and (entity_id := role_model(self.entry, role)):
                out[stage] = {"tokens": round(per), "model": model_name(self.hass, entity_id)}
        return out

    @property
    def running(self):
        return self._task is not None and not self._task.done()

    # Running.

    def start(self, *, trigger, force=False, only=None, picked=False):
        """trigger: manual or scheduled (a full run, unless picked), or filed (the fast lane). force: propose again
        over every analysis even with nothing new (Run anyway). only: run just that stage (the others hold their
        backlog, as if paused, for this run). picked: a run on the conversations the user picked: it takes in only
        those sent in (and works only on those), whatever on-demand improvements are set to."""
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
        traceback, for a bug report), and the run goes on to the stages that don't need it. One item's failure stays
        with that item (failures.contained); the stage failed too when every one of its items did. False if it
        failed."""
        began = time.monotonic()
        try:
            async with span(f"stage.{stage}"):
                await work()
            if failed := failures.nothing_done(stage):
                self.current["progress"].setdefault("problems", []).append(
                    {"stage": stage, "error": "Nothing done: its 1 item failed" if failed == 1
                     else f"Nothing done: all {failed} of its items failed"})
                return False
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
        """The run, in its own trace (its id is the run's), with its failures collected (failures.py)."""
        run = self.current
        collector = failures.Collector(run["id"], run["progress"], self.setbacks, self._save)
        try:
            async with for_run(run["id"]), span("run", trigger=run["trigger"], only=run.get("only"),
                                                force=run.get("force")):
                with failures.collecting(collector):
                    journal("info", f"Run started: {run['trigger']}"
                            + (f", {run['only']} only" if run.get("only") else ""))
                    await self._stages()
        except Exception as err:  # Outside any stage (its trace, say): the run's record still closes.
            if run["status"] != "running":
                raise
            _LOGGER.exception("Assist Improver: the run failed")
            await self._finish("failed", error=f"{type(err).__name__}: {err}"[:500])

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
            # The fast lane, and a run on picked conversations (filed as it starts), work only on those sent in:
            # others a full run took in wait for the next one.
            sent_only = run["trigger"] == "filed" or bool(run.get("picked"))
            mine = self.filed if sent_only else (lambda _rid: True)
            if not only or only == "triage":  # A run takes in what waits at the start.
                new = self._new(filed_only=sent_only)
                # One sent in with a clear sign skips the quick check: the user already said it needs a closer look.
                at.update({rid: "analyze" if self.skips_check(rid) else "triage" for rid in new})
                run["progress"]["taken_in"] = len(new)
                await self._save_at(at)
            if sent := [rid for rid, stage in at.items() if stage == "triage" and self.skips_check(rid)]:
                at.update(dict.fromkeys(sent, "analyze"))  # Sent in with a clear sign while it waited for the check.
                await self._save_at(at)
            todo = [rid for rid, stage in at.items() if gate(stage) and mine(rid)]
            run["progress"].update(analyze_total=len(todo), analyzed=0, skipped_as_smooth=0, analyze_failed=0)
            checker, tried = role_model(self.entry, "triage"), set()
            # Which half of the stage is at work, for the panel: the quick check (each one it passes is analysed in
            # the same call), then analysis alone.
            if checker and any(at.get(rid) == "triage" and not self.skips_check(rid) for rid in todo):
                run["progress"]["phase"] = "triage"
            for recording_id in todo:  # Triage first: a quick check, or pass through without a triage model.
                if at.get(recording_id) != "triage":
                    continue
                if not checker or self.skips_check(recording_id):  # Or sent in with a clear sign since it began.
                    at[recording_id] = "analyze"
                    continue
                result = await self._analyze(recording_id, triage_only=not gate("analyze"))
                tried.add(recording_id)  # Analysed too (analysis is open): a failure waits for the next run.
                self._count(result, at, recording_id)
                await self._save_at(at); await self._stage("analyze")
            if gate("analyze"):
                run["progress"]["phase"] = "analyze"
                await self._stage("analyze")
                for recording_id in [rid for rid, stage in at.items() if stage == "analyze" and rid not in tried
                                     and mine(rid)]:
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
            if generator.get("status") == "completed" and (unfixed := await self.runtime.improvements.no_fix(
                    not_fixed(generator.get("output") or {}), by="proposer")):  # Outside the assistant's control.
                run["progress"]["no_fix_found"] = run["progress"].get("no_fix_found", 0) + unfixed
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
            improvements, per_goal = self.runtime.improvements, self._per_goal()
            earlier = improvements.tries(self._runs().tried)  # Each goal's tests in earlier runs count too.
            try:
                report = await test_hypotheses(self.hass, self.entry, self.runtime, hypotheses=hypotheses,
                                               analyses=state["analyses"], skip_goals=dict(state["settled"]),
                                               earlier=earlier, per_goal=per_goal, allow_paid_inference=True)
            except Exception:
                # Tried again next run, but not forever: after MAX_ATTEMPTS failures these ideas are set aside.
                generator["test_attempts"] = generator.get("test_attempts", 0) + 1
                generator["tested"] = generator["test_attempts"] >= MAX_ATTEMPTS
                await self.runtime.role_runs.record(generator)
                raise
            generator["tested"] = True
            await self.runtime.role_runs.record(generator)
            run["progress"].update(tested=report.get("tested", 0))
            before = {i["id"] for i in improvements.items.values()}
            await improvements.refresh()
            run["progress"].update(new_improvements=sum(1 for i in improvements.items.values()
                                                        if i["id"] not in before))
            # A goal whose tests ran out without an improvement for it, in a conversation still kept: no fix found.
            from .sessions import short
            kept, used = {short(rid) for rid in self.recorder.index}, improvements.tries(self._runs().tried)
            if out := await improvements.no_fix({goal: f"Its {per_goal} tests found no fix." for goal, n in used.items()
                                                 if n >= per_goal and goal.split(":")[0] in kept}, by="limit"):
                run["progress"]["no_fix_found"] = run["progress"].get("no_fix_found", 0) + out

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
                message = f"Set aside after {MAX_ATTEMPTS} failed runs: {message}"
                failures.problem("analyze", recording_id[:8], message)
                return await self.runtime.role_runs.record({
                    **record, "id": f"unreadable-{recording_id}", "answer_status": "unreadable",
                    "error": {"message": message}})
            failures.problem("analyze", recording_id[:8], message)  # Counted by the run log (_strikes).
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
        await self._save()  # The setbacks too, as code that doesn't await left them (failures.contained).
        self.current.update(status=status, finished=utcnow().isoformat(), note=note, error=error)
        tag(outcome=status)
        journal("error" if status == "failed" else "info", f"Run {status}" + (f": {error}" if error else ""),
                timings=self.current["progress"].get("timings"), problems=self.current["progress"].get("problems"))
        await self.runtime.role_runs.record(self.current)


def not_fixed(answer):
    """{goal: why} of the goals a proposal's answer listed in not_fixed with a reason, and no hypothesis of it fixes."""
    fixing = {f"{g['session']}:{g['goal_turn']}" for h in answer.get("hypotheses") or [] for g in h.get("goals") or []}
    return {f"{r['session']}:{r['goal_turn']}": r["why"] for r in answer.get("not_fixed") or []
            if (r.get("why") or "").strip() and f"{r['session']}:{r['goal_turn']}" not in fixing}


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
