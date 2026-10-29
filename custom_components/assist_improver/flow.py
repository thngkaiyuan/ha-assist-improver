"""The model's engine (SPEC.md P0): one record of where each conversation is, a record per stage for each of its
rounds, what each run took in, and each trip back with what paid for it.

A run admits conversations, and only what it admits moves (P0.7). Each moves through the stages one at a time, whole
(P0.5), until its round ends: something waits at Your review, it ends in Your household, or it's held for a problem
(P0.6, P0.8). The stages' work is done by their workers (analysis, proposal, simulate, improvements); this decides
who moves, where, and when, and keeps the record every screen and stage reads (P0.2).
"""

import asyncio
from copy import deepcopy
import logging

from homeassistant.helpers.storage import Store
from homeassistant.util.dt import utcnow

from .cases import CaseError
from .const import CONF_AUTO_ACCEPT, DOMAIN
from .trace import span

_LOGGER = logging.getLogger(__name__)

STORE = f"{DOMAIN}.flow"
STAGES = ("Your Assist", "Triage", "Analyze", "Propose", "Write up", "Your review", "Your household")
KEYS = {"Triage": "triage", "Analyze": "analyze", "Propose": "propose", "Write up": "write"}  # Settings' stage names.
ENDS = ("Your review", "Your household")  # Where a round stops moving.
RETRIES = 3  # The tries a stage gives a conversation in one run before holding it ("a few", P0.8).
CLOSED = ("session_expired", "interrupted", "capture_timed_out")  # A conversation Assist has closed.


def deliberate(sent):
    """Whether a conversation was sent in with a clear sign it needs a closer look, so it goes straight to Analyze, past
    the quick check (sent: its mark, Flow.sent_in; DETAILS P9.3.1): a note written as it was sent in, filed from Assist,
    or sent in again after an earlier round looked at it. One picked, or sent in from the panel without a note, gets
    the quick check, as a full run's conversations do."""
    return bool(sent) and bool(sent.get("note") or sent.get("via") == "assist" or sent.get("again"))


def analysed_turns(analysis):
    """An analysis's turns: each one said (by number), then "note" for the note it was sent in with, if any."""
    said = int(analysis.get("turns") or max([g.get("turn") or 0 for g in analysis.get("goals") or []] or [0]))
    return list(range(1, said + 1)) + (["note"] if any(f.get("turn") is None for f in analysis.get("feedback") or [])
                                       else [])


class Failed(Exception):
    """A stage couldn't do its work on a conversation this time (the model erred, say)."""


class CutOff(Exception):
    """The run was cut off (cancelled, or Home Assistant stopping) while a stage worked on a conversation."""


class TakenOut(Exception):
    """The household took the conversation out (archived it, accepted or declined its improvement, or marked it a
    test) while a stage worked on it: what the stage found moves it nowhere."""


TAKEN_OUT = ("archived", "improved", "declined", "marked as a test")


class Flow:
    def __init__(self, hass, entry, runtime):
        self.hass, self.entry, self.runtime = hass, entry, runtime
        self.store = Store(hass, 1, STORE, private=True, atomic_writes=True)
        self.conversations = {}  # {recording id: its record}

    async def load(self):
        self.conversations = ((await self.store.async_load()) or {}).get("conversations", {})
        self.recover()

    def recover(self):
        """What a run left between stages when Home Assistant stopped mid-run is held there, cut off, for a manual
        full run or its stage's run to pick up (P0.8): no conversation is left in a round that nothing will move."""
        for conv in self.conversations.values():
            if self._in_round(conv) and conv["stage"] not in ENDS and not conv["held"]:
                self._hold(conv, "cut off", "The run was cut off before this was done.")

    async def save(self):
        await self.store.async_save({"conversations": self.conversations})

    # Reading.

    def record(self, rid):
        """A conversation's record, made at Your Assist the first time it's asked for."""
        if rid not in self.conversations:
            self.conversations[rid] = {"id": rid, "stage": "Your Assist", "round": 0, "held": None, "outcome": None,
                                       "archived": False, "covered_by": None, "sent": None, "rounds": {},
                                       "trips": [], "trace": []}
        return self.conversations[rid]

    def place(self, rid):
        """Where a conversation is: its stage, round, hold (cause and why), outcome, whether archived, and the
        improvement a duplicate points to."""
        conv = self.record(rid)
        return {k: deepcopy(conv[k]) for k in ("stage", "round", "held", "outcome", "archived", "covered_by")}

    def counts(self):
        """{stage: how many conversations are there}, by each one's record (DETAILS P14.1): every kept conversation,
        at Your Assist only those waiting for a run."""
        out = dict.fromkeys(STAGES, 0)
        for rid, summary in self.runtime.recorder.index.items():
            if not summary.get("user_turns") or summary.get("deleting"):
                continue
            stage = self.record(rid)["stage"]
            if stage != "Your Assist" or self.waiting(rid):
                out[stage] += 1
        return out

    def analysis(self, rid):
        """A conversation's latest analysis, the one every later stage reads (P0.2), or None while it has none."""
        try:
            return self._latest_analysis(self.record(rid))
        except Failed:
            return None

    def round_of(self, rid, number=None):
        conv = self.record(rid)
        return conv["rounds"].get(str(number if number is not None else conv["round"]))

    def question(self, rid):
        """The question for the household it waits at Your review with, or None."""
        conv = self.record(rid)
        current = conv["rounds"].get(str(conv["round"])) or {}
        return (current.get("records", {}).get("Your review") or {}).get("question") \
            if conv["stage"] == "Your review" else None

    def waiting(self, rid):
        """New and waiting for a run to admit it: at Your Assist, closed, with turns, not archived or being deleted."""
        conv, summary = self.record(rid), self.runtime.recorder.index.get(rid) or {}
        return (conv["stage"] == "Your Assist" and not conv["held"] and not conv["archived"]
                and summary.get("status") in CLOSED and summary.get("user_turns") != 0
                and not summary.get("deleting"))

    def _deleted(self, rid):
        """Deleted, or being deleted (Recorder.forget_later, during a run): it moves no further and isn't held; it goes
        when the run ends (DETAILS P5.1.3)."""
        summary = self.runtime.recorder.index.get(rid)
        return summary is None or bool(summary.get("deleting"))

    async def forget(self, rids):
        """Conversations deleted for good: their records go with them."""
        if [rid for rid in rids if self.conversations.pop(rid, None) is not None]:
            await self.save()

    # Who a run moves (P0.7).

    def admitted(self, run):
        """(admitted, released): the new conversations a run takes in, and the held ones it releases."""
        index = self.runtime.recorder.index
        new = [rid for rid in index if self.waiting(rid)]
        held = [rid for rid in index if self.record(rid)["held"] and not self._deleted(rid)]
        trigger, only, picked = run["trigger"], run.get("only"), run.get("picked")
        if trigger in ("revision", "conflict"):
            return [run["conversation"]], []
        if isinstance(picked, list):  # A picked run, and the fast lane (filed): those, new or held.
            return [rid for rid in picked if rid in new], [rid for rid in picked if rid in held]
        if only:  # A run of one stage releases what's held there.
            return [], [rid for rid in held if KEYS.get(self.record(rid)["stage"]) == only]
        if trigger == "scheduled":
            return new, []
        return new, held  # A manual full run.

    # The engine.

    async def run(self, run, on_move=None):
        """A run: admits its conversations and moves each until its round ends. Records what it took in (admitted,
        released, moved) on the run, and publishes its progress as it goes, kept by on_move after each move: the stage
        at work on the conversation it's on (run["stage"], a KEYS key), how far it is (progress: total, done,
        conversation), and each conversation's result once its round stops (results, with the tallies History reads:
        _result)."""
        admitted, released = self.admitted(run)
        run.update(admitted=admitted, released=released, moved=list(dict.fromkeys(admitted + released)), results={})
        progress = run.setdefault("progress", {})
        progress.update(total=len(run["moved"]), done=0, conversation=None, taken_in=len(admitted))

        async def moved():
            if on_move is not None:
                await on_move()
        person = run["trigger"] in ("manual", "revision", "conflict", "filed")
        for rid in released:  # Each release gives one more try, paid for by the person who started the run: a retry
            conv = self.record(rid)  # of a stage that failed is a way back (P0.13); picking up a cut-off round isn't.
            if person and (conv["held"] or {}).get("cause") == "failures":
                self._trip(conv, conv["stage"], conv["stage"], "person")
            conv["held"] = None
        for rid in admitted:
            conv = self.record(rid)
            if run["trigger"] in ("revision", "conflict"):
                continue  # Its new round, at Propose, was started by the decision.
            self._start_round(rid, conv)
        await self.save()
        pending = list(run["moved"])
        try:
            while pending:
                rid = progress["conversation"] = pending[0]
                if not self._deleted(rid):  # One deleted meanwhile has left the run.
                    async with span("conversation", recording_id=rid):  # Its work, in the run's trace.
                        await self._advance(rid, run, moved, released=rid in released)
                pending.pop(0)
                self._result(run, rid)
                progress["done"] += 1
                await self.save()
                await moved()
            progress["conversation"] = None
        except (asyncio.CancelledError, CutOff):
            for rid in pending:  # What it hadn't finished is held for a manual full run or its stage's run.
                if self._deleted(rid):
                    continue
                conv = self.record(rid)
                if conv["stage"] not in ENDS:
                    self._hold(conv, "cut off", "The run was cut off before this was done.")
                self._result(run, rid)
            await self.save()
            raise
        except Exception:  # The engine's own fault: nothing it hadn't finished is left between stages unheld.
            for rid in pending:
                if self._deleted(rid):
                    continue
                conv = self.record(rid)
                if conv["stage"] not in ENDS and not conv["held"]:
                    self._hold(conv, "failures", "The run failed before this was done.")
                self._result(run, rid)
            await self.save()
            raise

    def _result(self, run, rid):
        """A conversation's result in the run once its round stops (at Your review, in Your household, or held), and
        the run's tallies from its results and each one's round record: analysed, new improvements (written up in
        this run), duplicates, couldn't be read, and held, with the first one's why."""
        conv = self.record(rid)
        if self._deleted(rid) or (conv["stage"] not in ENDS and not conv["held"]):
            return
        held = conv["held"]
        run["results"][rid] = {"stage": conv["stage"], "outcome": conv["outcome"],
                               "held": {"cause": held["cause"], "why": held["why"]} if held else None}
        results = run["results"]

        def done(stage):  # The results whose stage finished its work on them in this run.
            return [r for r in results if any(a["run"] == run["id"] and a["status"] == "done" for a in
                                              ((self.round_of(r) or {}).get("records", {}).get(stage) or {})
                                              .get("attempts", []))]
        holds = [r["held"] for r in results.values() if r["held"]]
        run["progress"].update(
            analyzed=len(done("Analyze")),
            new_improvements=sum(len(self.round_of(r)["records"]["Write up"].get("improvements") or [])
                                 for r in done("Write up")),
            duplicates=sum(1 for r in results.values() if r["outcome"] == "duplicate"),
            unreadable=sum(1 for r in results.values() if r["outcome"] == "couldn't be read"),
            held=len(holds), why=holds[0]["why"] if holds else None)

    def _start_round(self, rid, conv, at="Your Assist", **record):
        conv["round"] += 1
        conv["held"], conv["outcome"], conv["covered_by"] = None, None, None
        conv["rounds"][str(conv["round"])] = {"path": [], "records": {}, "handoffs": []}
        summary = self.runtime.recorder.index.get(rid) or {}
        self._enter(conv, at, **({"ended": summary.get("updated"), "sent": conv.get("sent")}  # Closed: its last.
                                 if at == "Your Assist" else record))
        conv["sent"] = None

    def _enter(self, conv, stage, **record):
        """The conversation moves to a stage: what it hands that stage keeps its contract (P0.4), its record for this
        round starts, and its path and trace show it."""
        if self._taken_out(conv) and stage != "Your household":
            raise TakenOut()
        current = conv["rounds"][str(conv["round"])]
        if conv["stage"] != stage:  # A move: one stage hands it to the next (or, from Your review, to a new round).
            current["handoffs"].append(self._handoff(conv, conv["stage"], stage, record))
        conv["stage"] = stage
        current["path"].append(stage)
        current["records"].setdefault(stage, {"attempts": []}).update(record)
        conv["trace"].append({"at": utcnow().isoformat(), "round": conv["round"], "stage": stage,
                              "goals": list(self._goals(conv))})

    def _handoff(self, conv, start, end, data):
        """What a move hands the stage it enters, checked against its contract (records.HANDOFFS)."""
        from . import contracts, records
        base = {"conversation": conv["id"], "round": conv["round"]}
        if end == "Your household":
            kind, record = records.Ended, {**base, "outcome": conv["outcome"]}
        elif start == "Your Assist":
            kind, record = records.Admitted, base
        elif start == "Triage":
            kind, record = records.Checked, {**base, "smooth": False}
        elif start == "Analyze":
            kind, record = records.Analysed, {**base, "goals": self._goals(conv), "question": data.get("question")}
        elif start == "Propose":
            kind, record = records.Proposed, {**base, "question": data.get("question"), "improvements": list(
                data.get("improvements") or ([data["waiting"]] if data.get("waiting") else []))}
        elif start == "Write up":
            kind, record = records.Written, {**base, "question": data.get("question"),
                                             "improvements": self._improvements(conv["id"], pending=True)}
        elif start == "Your review" and end == "Propose":
            sent = data.get("revision") or data.get("conflict") or {}
            kind, record = records.SentBack, {**base, "improvement": sent.get("improvement"),
                                              "why": "revision" if "revision" in data else "conflict"}
        elif start == "Your household" and end == "Your review":
            kind, record = records.Undone, {**base, "improvement": data.get("improvement")}
        else:
            raise contracts.ContractError(f"No contract for a move from {start} to {end}.")
        return {"start": start, "end": end, "contract": kind.__name__,
                "record": contracts.check(record, kind, f"The handoff from {start} to {end}")}

    def _judged(self, conv):
        """The goals the conversation carries, as measures.goals_of gives them: its latest analysis's, and its revision
        notes' since; none while it has no analysis."""
        from .measures import goals_of
        analysis, revisions = None, []
        for number in sorted(conv["rounds"], key=int):
            records = conv["rounds"][number]["records"]
            if found := (records.get("Analyze") or {}).get("analysis"):
                analysis, revisions = found, []
            if revision := (records.get("Propose") or {}).get("revision"):
                revisions.append((number, revision))
        return goals_of(analysis, revisions) if analysis else []

    def _goals(self, conv):
        """The keys of the goals the conversation carries ("turn:index", "revision:index")."""
        return [g["key"] for g in self._judged(conv)]

    def _turns(self, conv):
        """The conversation's turns: what was said, the note it was sent in with, and each revision note since its
        latest analysis ("revision:<round>")."""
        turns = []
        for number in sorted(conv["rounds"], key=int):
            records = conv["rounds"][number]["records"]
            if analysis := (records.get("Analyze") or {}).get("analysis"):
                turns = analysed_turns(analysis)
            if (records.get("Propose") or {}).get("revision"):
                turns.append(f"revision:{number}")
        return turns

    @staticmethod
    def _taken_out(conv):
        return conv["stage"] == "Your household" and conv["outcome"] in TAKEN_OUT

    @staticmethod
    def _in_round(conv):
        """Whether it's in a round still going: one started, not yet ended in Your household, and still where its round
        took it (one sent in as the answer to its question waits at Your Assist for a run to start its next round)."""
        current = conv["rounds"].get(str(conv["round"])) or {}
        return bool(current.get("path")) and current["path"][-1] == conv["stage"] != "Your household"

    def _hold(self, conv, cause, why):
        # One being deleted isn't held: it goes when the run ends; nor is one taken out meanwhile: it's out of the
        # pipeline, in Your household (TakenOut).
        if not self._deleted(conv["id"]) and not self._taken_out(conv):
            conv["held"] = {"cause": cause, "why": why, "at": utcnow().isoformat()}

    def _end(self, conv, outcome, **extra):
        if self._taken_out(conv):
            raise TakenOut()
        conv["outcome"] = outcome
        conv.update(extra)
        self._enter(conv, "Your household", outcome=outcome)

    def _trip(self, conv, start, end, paid_by):
        conv["trips"].append({"start": start, "end": end, "round": conv["round"], "paid_by": paid_by,
                              "at": utcnow().isoformat()})

    async def _advance(self, rid, run, moved, released=False):
        """Moves the conversation on until its round stops; after each move, the run says the stage at work on it, and
        moved() keeps that."""
        from .settings import paused
        conv = self.record(rid)
        tries = 1 if released else RETRIES
        while conv["stage"] not in ENDS:
            if self._deleted(rid):  # Deleted during the run: it leaves it at once, unheld (DETAILS P5.1.3).
                return
            stage = conv["stage"]
            if stage == "Your Assist":
                self._record(conv, "Your Assist", started_from=await self._started_from(rid))
                self._enter(conv, "Triage")
                continue
            if stage == "Triage" and deliberate(self._sent(conv)):  # A clear sign: straight through, no quick check.
                self._enter(conv, "Analyze")
                continue
            key = KEYS[stage]
            if key in paused(self.entry):
                self._hold(conv, "blocked", f"{stage} is blocked.")
                return
            if run.get("only") and run["only"] != key:
                self._hold(conv, "scope", f"The run only ran {self._stage_named(run['only'])}.")
                return
            run["stage"] = key
            await moved()
            if not await self._work(rid, conv, run, stage, tries):
                return
            # Auto-accept, once written up: not Write up's work, so a run cut off meanwhile holds nothing at Your
            # review (P0.8); one deleted while it was written up has left the run (P5.1.3).
            if stage == "Write up" and self.entry.options.get(CONF_AUTO_ACCEPT) and not self._deleted(rid):
                await self._auto_accept(conv)
            tries = RETRIES  # A stage it reaches afterwards, in the same run, gives it its full tries.

    @staticmethod
    def _stage_named(key):
        return next(stage for stage, k in KEYS.items() if k == key)

    async def _work(self, rid, conv, run, stage, tries):
        """A stage's work on a conversation, tried up to `tries` times in this run; held for failures after that.
        Whether it moved on."""
        attempts = conv["rounds"][str(conv["round"])]["records"][stage]["attempts"]
        budget = {"budget": f"retries:{stage}:{run['id']}", "cap": tries - 1}
        for attempt in range(tries):
            if attempt and self._deleted(rid):  # Deleted while this stage worked on it: no more tries.
                return False
            if attempt:
                self._trip(conv, stage, stage, budget)
            try:
                await getattr(self, f"_{KEYS[stage]}")(rid, conv)
            except TakenOut:
                attempts.append({"run": run["id"], "at": utcnow().isoformat(), "status": "taken out"})
                return False
            except Failed as err:
                attempts.append({"run": run["id"], "at": utcnow().isoformat(), "status": "failed",
                                 "error": str(err)[:500]})
                continue
            except asyncio.CancelledError:
                attempts.append({"run": run["id"], "at": utcnow().isoformat(), "status": "cut off"})
                self._hold(conv, "cut off", "The run was cut off while this stage worked on it.")
                raise
            attempts.append({"run": run["id"], "at": utcnow().isoformat(), "status": "done"})
            return True
        self._hold(conv, "failures", f"{stage} failed {tries} time{'s' if tries > 1 else ''} in a row.")
        return False

    # The stages' work: each moves the conversation on, ends it, or raises Failed.

    async def _triage(self, rid, conv):
        from .settings import role_model
        if not role_model(self.entry, "triage"):  # No quick check set up: it passes straight through.
            self._enter(conv, "Analyze")
            return
        result = await self._analysis(rid, triage_only=True)
        if result is None:
            return
        if (check := result.get("triage") or {}).get("status") not in (None, "completed"):  # Retried, then held (P0.8).
            raise Failed(str((check.get("error") or {}).get("message") or "The quick check didn't finish."))
        if result.get("answer_status") == "triaged_smooth":
            self._record(conv, "Triage", verdict=result.get("output"))
            self._end(conv, "looked smooth")
        elif result.get("status") == "held":  # Not smooth: on to a full analysis.
            self._record(conv, "Triage", verdict=(result.get("triage") or {}).get("output"))
            self._enter(conv, "Analyze")
        else:
            raise Failed(str((result.get("error") or {}).get("message") or "The quick check didn't finish."))

    async def _analyze(self, rid, conv):
        from .hypotheses import friction_by_goal
        result = await self._analysis(rid)
        if result is None:
            return
        analysis = result.get("analysis")
        if not analysis:
            raise Failed(str((result.get("error") or {}).get("message") or "The analysis didn't finish."))
        if result.get("answer_status") == "best_rejected":  # No answer kept its checks: retried, then held (P0.3).
            raise Failed("The analysis didn't map every turn into a goal tree: "
                         + "; ".join((analysis.get("problems") or [])[:3]))
        self._record(conv, "Analyze", analysis=analysis,
                     given={"turns": analysed_turns(analysis), "goals": [], "conversations": [rid[:8]]})
        trouble = set(friction_by_goal(analysis))
        covered = {d.get("goal_turn"): d.get("improvement") for d in analysis.get("duplicates") or []}
        if not trouble and analysis.get("question_for_user"):  # It travels the line to wait at Your review (P0.4).
            self._enter(conv, "Propose", question=analysis["question_for_user"])
        elif not trouble:
            self._end(conv, "no trouble found")
        elif not trouble - set(covered):
            self._end(conv, "duplicate", covered_by=next(iter(covered.values())))
        else:
            self._enter(conv, "Propose")

    async def _analysis(self, rid, **kwargs):
        """The quick check or the analysis, by today's worker; None when no role can read the conversation (it ends
        as couldn't be read)."""
        from .analysis import analyze
        try:
            return await analyze(self.hass, self.entry, self.runtime, rid, sent=self._sent(self.record(rid)), **kwargs)
        except asyncio.CancelledError:
            raise
        except CaseError as err:
            self._end(self.record(rid), "couldn't be read", why=str(err)[:500])
            return None
        except Exception as err:
            _LOGGER.exception("Assist Improver couldn't analyse conversation %s", rid)
            raise Failed(f"{type(err).__name__}: {err}") from err

    def tests(self, rid, number=None):
        """The tests Simulate ran for a conversation's proposals in a round (the latest by default)."""
        found = self.round_of(rid, number) or {}
        return list((found.get("records", {}).get("Propose") or {}).get("tests") or [])

    def tests_cap(self):
        """The tests each round gets: the counterfactual setting (P0.9)."""
        from .const import CONF_TESTS_PER_GOAL, DEFAULT_TESTS_PER_GOAL
        return int(self.entry.options.get(CONF_TESTS_PER_GOAL, DEFAULT_TESTS_PER_GOAL))

    def tests_left(self, rid):
        """The tests the conversation's round has left: its cap, less each test it ran (P0.6: each round has its
        own). A test whose replay an earlier round ran is reused, never resampled, and still one of this round's."""
        return self.tests_cap() - len(self.tests(rid))

    async def test(self, rid, hypothesis, cap):
        """Simulate's stamp: one test of an idea on the conversation it's proposed for (simulate.test), judged on
        every goal the conversation carries, from its round's tests. The same changes are tested once a round (P6.3.4).
        When the proposer acts again after a test, that test sent it back to Propose, paid for by the round's tests.
        simulate.Untestable: why it can't be tested (a conversation no idea can be tested on is marked so for its
        round); Failed: the machinery cut it off, and it can run again."""
        from . import contracts, records, simulate
        from .cases import digest
        conv = self.record(rid)
        record = conv["rounds"][str(conv["round"])]["records"]["Propose"]
        changes = digest(hypothesis.get("changes"))
        if (same := [t for t in record.get("tests") or [] if t["changes"] == changes]):
            return same[-1]
        if untestable := record.get("untestable"):
            raise simulate.Untestable(untestable["why"], conversation=True)
        try:
            test = await simulate.test(self.hass, self.entry, self.runtime, rid, self._judged(conv), hypothesis)
        except simulate.Untestable as err:
            if err.conversation:
                record["untestable"] = {"why": str(err), "at": utcnow().isoformat()}
                await self.save()
            raise
        except simulate.Transient as err:
            raise Failed(str(err)) from err
        earlier = [t["after"]["plan"] for n, r in conv["rounds"].items() if n != str(conv["round"])
                   for t in (r["records"].get("Propose") or {}).get("tests") or []]
        if test["after"]["plan"] in earlier:  # Its replay with the change an earlier round ran, kept: reused, not paid.
            test["reused"] = True
        if record.get("tests"):  # A test after another: that one sent the proposer back (an idea untested isn't one).
            self._trip(conv, "Simulate", "Propose", {"budget": f"tests:{rid}:{conv['round']}", "cap": cap})
        record.setdefault("tests", []).append(contracts.check(test, records.Test, "The test"))
        await self.save()
        return test

    async def _propose(self, rid, conv):
        """The proposer, on this conversation, with the household in view: it collects the reviewer's approval
        (ask_reviewer) and Simulate's validation (test_idea) for each idea, spending the round's tests as it sees fit,
        and sends on one idea that has both (P0.4, P0.9): a round brings its conversation one improvement (P0.6,
        P0.10). With none, the round ends no fix found."""
        from . import contracts, records
        from .cases import digest
        from .hypotheses import REVIEWS_PER_ROUND
        from .proposal import propose
        from .improvements import PENDING
        from . import stamps
        if question := self._carried(conv, "Propose"):  # Nothing to propose: a question passes on.
            self._enter(conv, "Write up", question=question)
            return
        analysis = self._latest_analysis(conv)
        improvements, cap, asked = self.runtime.improvements, self.tests_cap(), {}
        current = conv["rounds"][str(conv["round"])]["records"].setdefault("Propose", {"attempts": []})
        # What the household sent back for this round, if it was: a revision (its note, with the goal it was given on
        # its way in, P0.3) or an accept's conflict, with the improvement in full.
        revision, conflict = current.get("revision") or {}, current.get("conflict") or {}
        back = revision or conflict
        sent_back = ({**deepcopy(back), "why": "revision" if revision else "conflict",
                      "improvement": improvements.described(improvements.items[back["improvement"]], detailed=True)}
                     if back.get("improvement") in improvements.items else None)
        # The round's reviews, as its tests are: each attempt's ask_reviewer starts from them, and each review is kept
        # here as it returns, however its attempt ends; one that asks for changes sends its idea back then, paid by the
        # round's review budget, which every review counts against (P0.2, P0.13).
        reviews, tests = current.setdefault("reviews", []), self.tests(rid)
        # A proposer picking up its round is given the tests and reviews it has so far, with what's left (P0.9); its
        # given names them by their place in the round's (P0.2).
        so_far = stamps.so_far(reviews, tests, self.tests_left(rid), REVIEWS_PER_ROUND - len(reviews))
        given = {"turns": self._turns(conv), "goals": self._goals(conv),
                 "records": [{"stage": "Analyze", "round": int(self._analysed_in(conv))}],
                 **({"tests": list(range(len(tests))), "reviews": list(range(len(reviews)))} if so_far else {})}
        self._record(conv, "Propose", given=given)

        async def keep(review):
            if review["verdict"] == "request_changes":
                self._trip(conv, "Review", "Propose", {"budget": f"reviews:{rid}:{conv['round']}",
                                                       "cap": REVIEWS_PER_ROUND})
            await self.save()

        def tester(ask):  # The proposer's stamps: the round's reviews, and its tests.
            ask.reviews, ask.keep = reviews, keep
            asked["ask"], asked["tester"] = ask, stamps.TestIdea(self, rid, ask, cap)
            return asked["tester"]
        try:
            generator = await propose(self.hass, self.entry, self.runtime, [analysis], tester=tester,
                                      feedback=improvements.feedback(), sent_back=sent_back, so_far=so_far,
                                      existing=improvements.catalog(asking=rid, detailed=True))
        except Exception as err:
            raise Failed(f"{type(err).__name__}: {err}") from err
        if back.get("improvement"):  # A decision on the version this round revises, under way as the proposer
            await improvements.settled(back["improvement"])  # returns, goes first, with its move (P0.11, decided).
        if self._taken_out(conv):  # Taken out while the proposer worked, or just now: nothing is built for it.
            raise TakenOut()
        if generator.get("status") == "failed" and not generator.get("output"):
            raise Failed(str((generator.get("error") or {}).get("message") or "The proposal couldn't finish."))
        self._record(conv, "Propose", given={**given, "conversations": list(generator.get("sessions") or [])})
        answer = generator.get("output") or {}
        if generator.get("answer_status") == "best_rejected" or not isinstance(answer, dict):
            # No answer kept its contract: nothing is handed on, and the stage tries again, then holds (P0.4, P0.8).
            raise Failed("The proposal broke its contract: " + "; ".join(map(str, (generator.get("problems") or [])[:3])))
        tests, revised = self.tests(rid), improvements.items.get(revision.get("improvement")) or {}

        def held_back(h):
            """Why an idea doesn't go on to Write up, or None: it needs both stamps on exactly its changes, its
            contract kept, and other changes than those the household asked to revise."""
            stamped = stamps.of(reviews, tests, h)
            if not stamped["Review"]:
                return "The reviewer's latest review of these changes didn't approve them."
            if not stamped["Simulate"]:
                return "No test of these changes validated."
            if revised and digest(h.get("changes")) == digest(revised.get("changes")):
                return "These are the changes the household asked to revise."
            return "; ".join(contracts.problems(h, records.Hypothesis, "An idea")) or None
        submitted, sent = {}, {}  # By its changes (P0.2): the idea sent on (submit_hypotheses takes one).
        for h in answer.get("hypotheses") or []:
            submitted.setdefault(digest(h.get("changes")), h)
            if held_back(h) is None:
                sent.setdefault(digest(h.get("changes")), h)
        ideas, judged = {}, [r["hypothesis"] for r in reviews] + [t["hypothesis"] for t in tests]
        for h in judged + (answer.get("hypotheses") or []):  # One per set of changes, with every title it went by.
            idea = ideas.setdefault(digest(h.get("changes")), {"titles": [], "hypothesis": h})
            if h.get("title") not in idea["titles"]:
                idea["titles"].append(h.get("title"))

        def had(changes):  # What the proposer had when it made the idea: the round's given, and each test back before
            first = next((n for n, t in enumerate(tests) if t["changes"] == changes), len(tests))  # its own first.
            return {**given, "tests": list(range(first))}
        self._record(conv, "Propose", ideas=[
            {**idea, "given": had(key), "stamps": stamps.of(reviews, tests, idea["hypothesis"]), "sent_on": key in sent,
             "why": None if key in sent else held_back(submitted.get(key, idea["hypothesis"]))
             or "The proposer didn't send it on."} for key, idea in ideas.items()])
        stopped = getattr(asked.get("ask"), "stopped", False)
        crashed = getattr(asked.get("tester"), "crashed", None)
        if not sent and (stopped or crashed):  # A stamp it couldn't get: a failure of Propose's (P0.4, P0.8).
            raise Failed("The reviewer stopped." if stopped else f"Simulate couldn't test: {crashed}")
        seen = list(generator.get("sessions") or [])  # The household it had in view, archived ones included.
        built = [await improvements.build_for(
            h, [t for t in tests if t["validated"] and t["changes"] == key][-1], rid, conv["round"], checked_on=seen,
            revises=back.get("improvement")) for key, h in sent.items()]
        earlier = improvements.items.get(back.get("improvement")) or {}
        if built:
            self._enter(conv, "Write up", improvements=built)
        elif earlier.get("status") in (*PENDING, "conflict"):  # No new version: the earlier one stays up for review,
            if earlier["status"] == "conflict":  # one that met a conflict back up for the household to decide (P0.6).
                await improvements.up_again(earlier["id"])
            self._enter(conv, "Write up", waiting=earlier["id"])
        elif (addressed := self._addressed(rid, analysis, answer)):  # Another conversation's improvement covers it.
            self._end(conv, "duplicate", covered_by=addressed)
        else:
            self._end(conv, "no fix found")

    async def _write(self, rid, conv):
        """The editor's write-up; a failed one is a failure of this stage like any other (P0.8): the improvement
        isn't up for review until it's written up."""
        from .pipeline import editor_model
        if question := self._carried(conv, "Write up"):  # Nothing to write up: a question passes on.
            self._enter(conv, "Your review", question=question)
            return
        improvements = self.runtime.improvements
        unwritten = [item["id"] for item in improvements.unwritten() if item["id"] in self._improvements(rid)]
        if unwritten and await improvements.write_copy(editor_model(self.entry), unwritten):
            raise Failed("The write-up didn't finish.")
        self._enter(conv, "Your review")

    async def _auto_accept(self, conv):
        """Auto-accept (DETAILS P12.9): each low-risk improvement this round wrote up is applied at once, and its
        conversation ends improved, as the household's accept would end it; one that isn't low-risk, can't apply or
        meets a conflict stays up for review."""
        improvements = self.runtime.improvements
        written = (conv["rounds"][str(conv["round"])]["records"].get("Write up") or {}).get("improvements") or []
        await improvements.auto_accept(written)
        for iid in written:
            if (item := improvements.items.get(iid)) and item["status"] == "applied":
                await self.decided(item, "accept")

    def _improvements(self, rid, pending=False):
        """The improvements of a conversation: those it came from (P0.10)."""
        from .improvements import PENDING
        return [item["id"] for item in self.runtime.improvements.items.values()
                if item.get("conversation") == rid and (not pending or item["status"] in PENDING)]

    async def _started_from(self, rid):
        """The home and time the conversation started from, as its recording keeps them: what every replay of it
        starts from too (P0.1)."""
        from .cases import started_from
        try:
            first = ((await self.runtime.recorder.export(rid)).get("turns") or [{}])[0].get("source") or {}
        except Exception:  # noqa: BLE001 - one that can't be read is ended by the stage that reads it, with why.
            return None
        return started_from(first)

    @staticmethod
    def _addressed(rid, analysis, answer):
        """The improvement the proposer says already covers all of this conversation's trouble (P0.12: a duplicate),
        or None."""
        from .hypotheses import friction_by_goal
        covered = {r.get("goal_turn"): r.get("improvement") for r in answer.get("already_addressed") or []
                   if str(r.get("session"))[:8] == rid[:8] and r.get("improvement")}
        trouble = set(friction_by_goal(analysis))
        return next(iter(covered.values())) if trouble and trouble <= set(covered) else None

    @staticmethod
    def _sent(conv):
        """How the conversation was sent in, as its round started at Your Assist (sent_in), or None."""
        return (conv["rounds"][str(conv["round"])]["records"].get("Your Assist") or {}).get("sent")

    def _carried(self, conv, stage):
        """The question for the household the conversation carries at this stage in its round, if any."""
        return (conv["rounds"][str(conv["round"])]["records"].get(stage) or {}).get("question")

    def _record(self, conv, stage, **data):
        conv["rounds"][str(conv["round"])]["records"].setdefault(stage, {"attempts": []}).update(deepcopy(data))

    def _latest_analysis(self, conv):
        return conv["rounds"][self._analysed_in(conv)]["records"]["Analyze"]["analysis"]

    def _analysed_in(self, conv):
        """The round whose Analyze record is the latest analysis: the one every later stage reads (P0.2)."""
        for number in sorted(conv["rounds"], key=int, reverse=True):
            if (conv["rounds"][number]["records"].get("Analyze") or {}).get("analysis"):
                return number
        raise Failed("No analysis to propose from.")

    # The household's own moves.

    def _covering(self, item, at=None):
        """The conversation an improvement belongs to (P0.10), when it stands at `at`; with none, while its round is
        still going (at Your review, or a revision's round held or worked on before it)."""
        rid = item.get("conversation")
        if not rid or rid not in self.runtime.recorder.index:
            return []
        conv = self.record(rid)
        return [rid] if (conv["stage"] == at if at else self._in_round(conv)) else []

    async def decided(self, item, decision, note=""):
        """What a decision at Your review does to the conversation it's about (P0.11, P0.7): accepted, it's improved;
        declined, declined and archived, nothing more; both from wherever its round has reached (an earlier version
        stays up for review while its revision's round runs, P0.6); a revision sends it back to Propose for a new
        round, paid for by the person; undone, it's back up for review. The conversations sent back to Propose, for a
        run of their own."""
        back = []
        if decision == "undo" and item["status"] != "applied":  # One that couldn't run, or rolled back, moves nothing.
            for rid in self._covering(item, "Your household"):
                conv = self.record(rid)
                conv["outcome"] = None
                self._enter(conv, "Your review", improvement=item["id"])
        accepted = decision == "accept" and item["status"] == "applied"
        for rid in self._covering(item, None if accepted or decision == "decline" else "Your review"):
            conv = self.record(rid)
            if accepted:  # A run working on it leaves it (TakenOut), as after a decline.
                conv["held"] = None
                self._end(conv, "improved")
            elif decision == "decline":
                conv["held"] = None
                self._end(conv, "declined", archived=True)
            elif decision == "revise":  # The note is a turn, with its goal given on its way in (P0.3).
                self._back_to_propose(rid, conv, revision={"improvement": item["id"], "note": note,
                                                           "goal": self._note_goal(conv, item, note)})
                back.append(rid)
        await self.save()
        return back

    def _note_goal(self, conv, item, note):
        """A revision note's goal: a sub-goal of the root goal its improvement is for (P0.3), unmet: it asks for what
        the conversation didn't get."""
        turns = sorted({int(goal.split(":")[1]) for goal in item["fixes"]})
        try:
            goals = self._latest_analysis(conv).get("goals") or []
        except Failed:
            goals = []
        from .analyzer import root_turn
        serves = root_turn(goals, turns[0]) if turns else None
        return {"summary": note, "kind": "feedback", "serves": serves, "reached": False}

    async def resolve(self, item):
        """The household agreed to send back an improvement whose accept met a conflict: its conversation goes back in
        at Propose, for a new round (P0.7). The conversations sent back, for a run of their own."""
        back = []
        for rid in self._covering(item, "Your review"):
            self._back_to_propose(rid, self.record(rid),
                                  conflict={"improvement": item["id"], "conflicts": deepcopy(item.get("conflicts"))})
            back.append(rid)
        await self.save()
        return back

    def _back_to_propose(self, rid, conv, **record):
        self._trip(conv, "Your review", "Propose", "person")
        self._start_round(rid, conv, at="Propose", **record)

    def waits(self, rid):
        """Whether an improvement of this conversation (P0.10) waits for the household: up for review (the earlier
        version too, while its revision's round runs), needing attention, or met a conflict. It isn't archived or
        marked as a test meanwhile: the household's decision is its way out (P0.11)."""
        from .improvements import PENDING
        return any(item.get("conversation") == rid and item["status"] in (*PENDING, "conflict") and item.get("copy")
                   for item in self.runtime.improvements.items.values())

    def revising(self, iid):
        """Whether the household's revision of this improvement is still being worked on: its conversation is in the
        round that revision started, not yet stopped. The improvement stays up for review meanwhile, to accept or
        decline (P0.6), not to revise again."""
        conv = self.conversations.get((self.runtime.improvements.items.get(iid) or {}).get("conversation"))
        if conv is None:
            return False
        revision = (conv["rounds"].get(str(conv["round"]), {}).get("records", {}).get("Propose") or {}).get("revision")
        return (revision or {}).get("improvement") == iid and self._in_round(conv) and conv["stage"] not in ENDS

    async def archive(self, *rids, archived=True):
        """Archived by hand: out of the pipeline, in Your household (P0.12); not one an improvement of which waits for
        the household (waits). Unarchived: one whose round had ended in Your household is back as that round ended
        (P0.6); one the archive took out mid-round goes back in the pipeline, new, for a new round (DETAILS P3.3.3).
        Either is kept in the conversations list while it stands so (listed; DETAILS P14.4.3), though a list would
        leave it out on its own. Saved once, however many; the ones archived or unarchived."""
        done = [rid for rid in rids if not (archived and self.waits(rid))]
        for rid in done:
            conv = self.record(rid)
            if archived and not conv["archived"]:
                conv.pop("listed", None)
                conv["before_archive"] = {k: conv[k] for k in ("stage", "outcome", "covered_by")}
                if conv["stage"] == "Your household":  # Its round already ended there.
                    conv.update(archived=True, outcome="archived", held=None)
                else:
                    if not self._in_round(conv):  # Waiting for a run: a round starts, to end it.
                        self._start_round(rid, conv)
                    conv["held"] = None
                    self._end(conv, "archived", archived=True)  # Its round ends here, from where it reached (P0.6).
            elif not archived:
                if conv["archived"]:
                    before = conv.pop("before_archive", {})
                    conv["archived"] = False
                    if before.get("stage", "Your household") == "Your household":
                        conv.update({k: before[k] for k in ("outcome", "covered_by") if k in before})
                    else:  # A way back a person takes (P0.13). It waits marked as the round the archive ended started
                        # (sent): the mark the screens show, and its next round starts from.
                        self._trip(conv, "Your household", "Your Assist", "person")
                        conv.update(stage="Your Assist", outcome=None, covered_by=None, sent=self.sent(rid))
                conv["listed"] = self._where(conv)
        await self.save()
        return done

    @staticmethod
    def _where(conv):
        return {k: conv[k] for k in ("round", "stage", "outcome")}

    def unarchived(self, rid):
        """Whether the household unarchived it where it stands now (archive): it stays in the conversations list until
        it stands otherwise."""
        conv = self.record(rid)
        return conv.get("listed") == self._where(conv)

    def review(self, rid):
        """The improvement of this conversation (P0.10) waiting for the household's review, written up, or None."""
        items = self.runtime.improvements.items
        return next((iid for iid in self._improvements(rid, pending=True) if items[iid].get("copy")), None)

    async def mark_test(self, rid):
        """The household marked it a test: it ends in Your household as one (P0.12), from wherever it stood; not while
        an improvement of it waits for the household (waits). Whether it was marked."""
        if self.waits(rid):
            return False
        conv = self.record(rid)
        if not self._in_round(conv):  # Not in a round: one starts at Your Assist, to end it (P0.6).
            conv.update(stage="Your Assist", outcome=None, archived=False)
            self._start_round(rid, conv)
        conv["held"] = None
        self._end(conv, "marked as a test")
        await self.save()
        return True

    def sent(self, rid):
        """How the conversation was last sent in ({at, via, note, again, by}), or None: its mark while it waits for a
        run, else the one its latest round from Your Assist started with."""
        conv = self.record(rid)
        if conv["sent"]:
            return deepcopy(conv["sent"])
        for number in sorted(conv["rounds"], key=int, reverse=True):
            if "Your Assist" in (records := conv["rounds"][number]["records"]):
                return deepcopy(records["Your Assist"].get("sent"))
        return None

    async def sent_in(self, rid, note="", via="panel", by=None):
        """Sent in: from the panel (Send this one in, or picked from Your household) or filed from Assist (via). New,
        it's marked as it waits at Your Assist; from Your household, or as the answer to its question, it goes back
        there for a run to take it in, for a new round (P0.12, P0.11). A note sent before is kept when it's sent in
        again without one (DETAILS P8.9.4); again: an earlier round looked at it (Triage or Analyze). Anywhere else
        it's in the pipeline already, and nothing changes."""
        conv = self.record(rid)
        if conv["stage"] == "Your household" or self.question(rid):
            self._trip(conv, conv["stage"], "Your Assist", "person")  # A way back a person takes (P0.13).
            conv.update(stage="Your Assist", held=None, archived=False)
            conv.pop("before_archive", None)
        elif conv["stage"] != "Your Assist" or conv["held"]:
            return
        note = (note or "").strip()[:500] or (self.sent(rid) or {}).get("note")
        again = any(stage in r["path"] for r in conv["rounds"].values() for stage in ("Triage", "Analyze"))
        conv["sent"] = {"at": utcnow().isoformat(), "via": via, **({"note": note} if note else {}), "again": again,
                        **({"by": by} if by else {})}
        await self.save()
