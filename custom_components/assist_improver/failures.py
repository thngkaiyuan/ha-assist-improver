"""When something goes wrong in a run: one item's failure stays with that item, and an AI step's record always closes.

contained(stage, item): one item's work in a stage that works over several (conversations, sessions, facts,
improvements). An unexpected failure is logged with its traceback, kept in the run's problems as {stage, item, error}
(Settings > History lists them under Details), and the stage goes on with the other items. Errors the caller expects
(expected=) are the item's own answer, not a problem: contained without a word. What happens to a conversation whose
stage fails as a whole (tried again, then held with why) is the flow's (flow.py, SPEC.md P0.8).

closing(save, record): an AI step's record, kept "running" before any inference, then closed whatever ends the step:
failed with the error on an exception, interrupted on cancellation, both raised again. No record stays "running" after
its step ended (a restart marks the ones it cut off).

Outside a run (an admin action, start-up) contained still contains and logs; there are no problems to keep.
"""

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
import logging

_LOGGER = logging.getLogger(__name__)

CANCELLED = "Cancelled before it finished; not repeated"

_PROBLEMS: ContextVar[list | None] = ContextVar("assist_improver_problems", default=None)


@contextmanager
def collecting(problems):
    """A run's problems (a list, kept in its progress), current for everything inside (tasks started inside too)."""
    token = _PROBLEMS.set(problems)
    try:
        yield problems
    finally:
        _PROBLEMS.reset(token)


class contained:
    """One item's work: `async with contained(stage, item) as one:` (or `with`, in code that doesn't await), then
    one.failed, one.error (as kept) and one.expected (it was one of expected). step names a part of the stage's work
    on the item (a capability probe, say), named in the error."""

    def __init__(self, stage, item, *, expected=(), step=None):
        self.stage, self.item, self.step, self.expected = stage, str(item), step, expected
        self.failed = False
        self.error = None

    def __enter__(self):
        return self

    def __exit__(self, kind, err, tb):
        return self._ended(kind, err, tb)

    async def __aenter__(self):
        return self

    async def __aexit__(self, kind, err, tb):
        return self._ended(kind, err, tb)

    def _ended(self, kind, err, tb):
        """Whether the exception is held here."""
        if kind is None or not issubclass(kind, Exception):  # Cancelled, or Home Assistant stopping: never held here.
            return False
        self.failed, self.expected = True, isinstance(err, self.expected)
        if self.expected:
            self.error = str(err)
            return True
        self.error = (f"{self.step}: " if self.step else "") + f"{kind.__name__}: {err}"[:500]
        _LOGGER.error("Assist Improver: %s failed on %s", self.stage, self.item, exc_info=(kind, err, tb))
        problem = {"stage": self.stage, "item": self.item, "error": self.error}
        if (problems := _PROBLEMS.get()) is not None and problem not in problems:
            problems.append(problem)
        return True


class closing:
    """An AI step's record for the step's lifetime: `async with closing(save, record) as keep:` saves it (running)
    first; keep(record) saves a later state (the last one kept is the one closed); an exception closes it as failed
    with the error, a cancellation as interrupted, each saved and raised again. save: the store's own (the run log's
    record, a replay's), or None for a record its caller keeps."""

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
