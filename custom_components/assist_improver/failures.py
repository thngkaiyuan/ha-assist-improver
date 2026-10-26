"""When something goes wrong in a run: one item's failure stays with that item, and an AI step's record always closes.

contained(stage, item): one item's work in a stage that works over several (conversations, hypotheses, goals,
improvements). An unexpected failure is logged with its traceback, kept in the run's problems as {stage, item, error}
(Settings > History lists them under Details), and the stage goes on with the other items. Errors the caller expects
(expected=) are the item's own answer, not a problem: contained without a word. An item that fails again run after run
is set aside after MAX_ATTEMPTS runs in a row, with why (aside() says so before it's tried again), so a deterministic
failure doesn't recur, or cost, every run; a success clears its count, and a new version of the integration tries
everything again. A stage fails as a whole only when an error isn't tied to one item, or every one of its items failed
(nothing_done).

closing(save, record): an AI step's record, kept "running" before any inference, then closed whatever ends the step:
failed with the error on an exception, interrupted on cancellation, both raised again. No record stays "running" after
its step ended (a restart marks the ones it cut off).

Outside a pipeline run (an admin action, start-up) contained still contains and logs; there are no problems to keep
and nothing is set aside.
"""

import asyncio
from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
import logging

from homeassistant.util.dt import utcnow

from .const import VERSION

_LOGGER = logging.getLogger(__name__)

MAX_ATTEMPTS = 3  # Runs in a row an item may fail in before it's set aside.
CANCELLED = "Cancelled before it finished; not repeated"


class Collector:
    """A pipeline run's failures: its problems (in its progress, as "problems"), each item's failed runs in a row (its
    setbacks, {key: {runs, run, error, at, version, aside}}, kept between runs by save(): right away from an async
    block, at the end of the run from a sync one), and how each stage's own items went."""

    def __init__(self, run_id, progress, setbacks, save=None):
        self.run_id, self.progress, self.setbacks, self.save = run_id, progress, setbacks, save
        self.ok, self.failed = Counter(), Counter()  # {stage: its own items that went fine, or failed}

    def keep(self, problem):
        problems = self.progress.setdefault("problems", [])
        if problem not in problems:
            problems.append(problem)

    def went_fine(self, stage, key, counted):
        """Its count starts again. Whether the setbacks changed."""
        self.ok[stage] += counted
        return self.setbacks.pop(key, None) is not None

    def went_wrong(self, stage, item, key, error, counted, setback):
        """Kept with why; the run it fails in counts once. (The error as kept, whether this failure set it aside.)"""
        self.failed[stage] += counted
        set_aside = False
        if setback:
            entry = self.setbacks.setdefault(key, {"runs": 0, "version": VERSION})
            if entry.get("run") != self.run_id:
                entry.update(runs=entry["runs"] + 1, run=self.run_id)
            entry.update(error=error, at=utcnow().isoformat())
            if set_aside := entry["runs"] >= MAX_ATTEMPTS:
                entry["aside"] = error = f"Set aside after {MAX_ATTEMPTS} failed runs: {error}"
        self.keep({"stage": stage, "item": item, "error": error})
        return error, set_aside


_RUN: ContextVar[Collector | None] = ContextVar("assist_improver_failures", default=None)


@contextmanager
def collecting(collector):
    """A run's collector, current for everything inside (tasks started inside too)."""
    token = _RUN.set(collector)
    try:
        yield collector
    finally:
        _RUN.reset(token)


def kept_setbacks(saved):
    """The setbacks a store kept, less those from another version of the integration: a new version tries again."""
    return {key: entry for key, entry in (saved or {}).items()
            if isinstance(entry, dict) and entry.get("version") == VERSION}


def _key(stage, item, step):
    return f"{stage}:{step}:{item}" if step else f"{stage}:{item}"


def aside(stage, item, step=None):
    """Why this item is set aside in this stage (it failed MAX_ATTEMPTS runs in a row), or None: it's tried."""
    run = _RUN.get()
    entry = run.setbacks.get(_key(stage, str(item), step)) if run is not None else None
    return entry.get("aside") if entry else None


def problem(stage, item, error):
    """A failure the stage counted its own way (analysis keeps its own), kept in the run's problems too."""
    if (run := _RUN.get()) is not None:
        run.keep({"stage": stage, "item": str(item), "error": error})


def nothing_done(stage):
    """How many of the stage's own items failed, when every one did (it failed as a whole); 0 otherwise."""
    run = _RUN.get()
    return run.failed[stage] if run is not None and run.failed[stage] and not run.ok[stage] else 0


class contained:
    """One item's work: `async with contained(stage, item) as one:` (or `with`, in code that doesn't await), then
    one.failed, one.error (as kept), one.expected (it was one of expected) and one.set_aside (this failure set the
    item aside). step names a part of the stage's work on the item (a capability probe, say): counted on its own, and
    named in the error; only the stage's own items (no step) decide whether nothing could be done. setback=False:
    tried again whenever asked (work inside one AI call, not run after run)."""

    def __init__(self, stage, item, *, expected=(), step=None, setback=True):
        self.stage, self.item, self.step, self.expected, self.setback = stage, str(item), step, expected, setback
        self.failed = self.set_aside = False
        self.error = None

    def __enter__(self):
        return self

    def __exit__(self, kind, err, tb):
        return self._ended(kind, err, tb)[0]

    async def __aenter__(self):
        return self

    async def __aexit__(self, kind, err, tb):
        held, changed = self._ended(kind, err, tb)
        if changed and (run := _RUN.get()) is not None and run.save is not None:
            await run.save()
        return held

    def _ended(self, kind, err, tb):
        """(Whether the exception is held here, whether the setbacks changed.)"""
        run, key, counted = _RUN.get(), _key(self.stage, self.item, self.step), self.step is None
        if kind is None:
            return False, run is not None and run.went_fine(self.stage, key, counted)
        if not issubclass(kind, Exception):  # Cancelled, or Home Assistant stopping: never held here.
            return False, False
        self.failed, self.expected = True, isinstance(err, self.expected)
        if self.expected:
            self.error = str(err)
            return True, False
        self.error = (f"{self.step}: " if self.step else "") + f"{kind.__name__}: {err}"[:500]
        _LOGGER.error("Assist Improver: %s failed on %s", self.stage, self.item, exc_info=(kind, err, tb))
        if run is None:
            return True, False
        self.error, self.set_aside = run.went_wrong(self.stage, self.item, key, self.error, counted, self.setback)
        return True, self.setback


class closing:
    """An AI step's record for the step's lifetime: `async with closing(save, record) as keep:` saves it (running)
    first; keep(record) saves a later state (the last one kept is the one closed); an exception closes it as failed
    with the error, a cancellation as interrupted, each saved and raised again. save: the store's own (the run log's
    record, a candidate's or a case's baseline), or None for a record its caller keeps."""

    def __init__(self, save, record):
        self.save, self.record = save, record

    async def keep(self, record=None):
        if record is not None:
            self.record = record
        return await self.save(self.record) if self.save is not None else self.record

    async def __aenter__(self):
        await self.keep()  # Persist the attempt BEFORE any inference.
        return self.keep

    async def __aexit__(self, kind, err, tb):
        if kind is None or not issubclass(kind, Exception | asyncio.CancelledError):
            return False
        if issubclass(kind, asyncio.CancelledError):
            self.record.update(status="interrupted", error={"message": CANCELLED})
        else:
            self.record.update(status="failed", error={"type": kind.__name__,
                                                       "message": f"{kind.__name__}: {err}"[:500]})
        await self.keep()
        return False
