"""One clock for a replay worker's process (DETAILS P6.2.5, P11.2).

worker.BOOT installs it before Home Assistant is imported (install), freezegun-style as Home Assistant's own tests set
the time: time.time, time.time_ns, time.monotonic, time.monotonic_ns and datetime's now, utcnow and today read the real
clock moved on by the worker's clock, which starts at the moment BOOT is given (a conversation's, or a test's branch
turn's) and only ever moves forward, so the sandbox's Home Assistant, its scheduler and the sun all read one time.
Python's thread waits keep the real clock (threading and queue are imported first), and so does the worker for its own
timings and limits (REAL_MONOTONIC). Limits: IDs made in C (Home Assistant's ULIDs) and time.localtime() with no
argument read the real clock.

The sandbox's event loop (Loop) moves the clock on by itself only inside a window (Window): while nothing is ready, no
executor job is pending and no file but its own wake-up pipe is watched, the clock moves to the next timer, never past
the window's cap; outside windows it runs at real speed.

In the tests' own process (worker.INLINE), which is also the real instance's, patch() installs the same reads, and
each sandbox's threads read a clock of their own (join); every other thread reads the real clock.
"""

import asyncio
import datetime as _datetime
import queue  # noqa: F401 - imported before the clock is installed: Python's thread waits keep real time.
import selectors
import threading
import time

REAL_TIME, REAL_TIME_NS = time.time, time.time_ns
REAL_MONOTONIC, REAL_MONOTONIC_NS = time.monotonic, time.monotonic_ns
REAL_DATETIME = _datetime.datetime
PROCESS = None  # A worker's clock, for every thread of its process (install).
_THREAD = threading.local()  # In the tests' own process: a sandbox's thread's clock (join).
SMALLEST = 1e-6  # Seconds: a timer due sooner is waited for.


class Clock:
    """The real clock moved on: wall-clock reads start at a moment, and the clock only moves forward from there, wall
    clock and monotonic alike."""

    def __init__(self, start=None):
        self.offset = 0 if start is None else round(start * 1e9) - REAL_TIME_NS()  # Wall clock, ns.
        self.moved = 0  # How far it was moved on since it started, ns: what monotonic reads on top of the real one.

    def now(self):
        """Its time, in seconds since the epoch."""
        return (REAL_TIME_NS() + self.offset) / 1e9

    def forward(self, seconds):
        step = max(0, round(seconds * 1e9))
        self.offset += step
        self.moved += step

    def move_to(self, moment):
        """On to a moment (seconds since the epoch), if it's later than now."""
        self.forward(moment - self.now())


def _clock():
    return PROCESS or getattr(_THREAD, "clock", None)


def _time():
    clock = _clock()
    return REAL_TIME() if clock is None else REAL_TIME() + clock.offset / 1e9


def _time_ns():
    clock = _clock()
    return REAL_TIME_NS() if clock is None else REAL_TIME_NS() + clock.offset


def _monotonic():
    clock = _clock()
    return REAL_MONOTONIC() if clock is None else REAL_MONOTONIC() + clock.moved / 1e9


def _monotonic_ns():
    clock = _clock()
    return REAL_MONOTONIC_NS() if clock is None else REAL_MONOTONIC_NS() + clock.moved


class _AsReal(type):
    """Every datetime the stand-in class makes is the real class's (its __new__), so it answers isinstance as the real
    class does."""

    def __instancecheck__(cls, obj):
        return type.__instancecheck__(cls, obj) or (cls is datetime and isinstance(obj, REAL_DATETIME))

    def __subclasscheck__(cls, subclass):
        return type.__subclasscheck__(cls, subclass) or (cls is datetime and issubclass(subclass, REAL_DATETIME))


class datetime(REAL_DATETIME, metaclass=_AsReal):  # noqa: N801 - datetime.datetime, as imported after install.
    """datetime.datetime with now, utcnow and today on the clock."""

    def __new__(cls, *args, **kwargs):
        return REAL_DATETIME(*args, **kwargs) if cls is datetime else super().__new__(cls, *args, **kwargs)

    @classmethod
    def now(cls, tz=None):
        return cls.fromtimestamp(time.time(), tz)

    @classmethod
    def utcnow(cls):
        return cls.fromtimestamp(time.time(), _datetime.UTC).replace(tzinfo=None)

    @classmethod
    def today(cls):
        return cls.fromtimestamp(time.time())


datetime.__module__ = datetime.__qualname__ = "datetime"


def patch():
    """Python's clock reads, on the clock of the thread reading (install, join); once, before Home Assistant is
    imported. date.today reads time.time."""
    if time.time is not _time:
        time.time, time.time_ns, time.monotonic, time.monotonic_ns = _time, _time_ns, _monotonic, _monotonic_ns
        _datetime.datetime = datetime


def install(start=None):
    """In a worker (BOOT): one clock for its whole process, from start (seconds since the epoch; None: now)."""
    global PROCESS
    patch()
    PROCESS = Clock(start)


def join(clock):
    """The calling thread reads this clock (a sandbox's, in the tests' own process); a worker's threads read its own."""
    if PROCESS is None:
        _THREAD.clock = clock


def current(start=None):
    """The clock a sandbox runs on: its worker's, moved on to start if that's later; in the tests' own process, a new
    one from start."""
    if PROCESS is None:
        return Clock(start)
    if start is not None:
        PROCESS.move_to(start)
    return PROCESS


class Window:
    """While open on a loop (Loop.window), the clock moves on to each next timer whenever the loop is quiet, never past
    cap (seconds since the epoch; None: no cap), and never once the real clock passes deadline (REAL_MONOTONIC; None:
    none). settled: done once the loop is quiet with nothing due before the cap (True), or the deadline passed (False);
    the clock moves no further in this window."""

    def __init__(self, loop, cap=None, deadline=None):
        self.loop, self.cap, self.deadline = loop, cap, deadline
        self.settled = loop.create_future()

    def __enter__(self):
        self.loop.window = self
        return self

    def __exit__(self, *exc):
        if self.loop.window is self:
            self.loop.window = None


class _Selector(selectors.BaseSelector):
    """The loop's selector, which moves the clock on in a window instead of waiting (Loop)."""

    def __init__(self, loop):
        self.inner, self.loop, self.own = selectors.DefaultSelector(), loop, frozenset()

    def register(self, fileobj, events, data=None):
        return self.inner.register(fileobj, events, data)

    def unregister(self, fileobj):
        return self.inner.unregister(fileobj)

    def modify(self, fileobj, events, data=None):
        return self.inner.modify(fileobj, events, data)

    def get_map(self):
        return self.inner.get_map()

    def close(self):
        self.inner.close()

    def select(self, timeout=None):
        """timeout: 0 when a callback is ready, else until the next timer (None: none)."""
        loop, window = self.loop, self.loop.window
        if window is None or window.settled.done():
            return self.inner.select(timeout)
        if window.deadline is not None and REAL_MONOTONIC() >= window.deadline:
            window.settled.set_result(False)
            return []
        if timeout == 0 or loop.jobs or not self.inner.get_map().keys() <= self.own:
            return self.inner.select(timeout)
        room = None if window.cap is None else window.cap - loop.clock.now()
        step = timeout if room is None else room if timeout is None else min(timeout, room)
        if step is None or step <= 0:  # Nothing more can happen before the cap.
            window.settled.set_result(True)
            return []  # Its waiter runs now.
        if step < SMALLEST:
            return self.inner.select(step)
        loop.clock.forward(step)
        return []  # The loop runs what's due now.


class Loop(asyncio.SelectorEventLoop):
    """A sandbox's event loop, on its clock: it counts the executor jobs pending, and moves the clock on in a window
    (Window, _Selector)."""

    def __init__(self, clock):
        self.clock, self.jobs, self.window = clock, 0, None
        selector = _Selector(self)
        super().__init__(selector)
        selector.own = frozenset(selector.get_map())  # Its own wake-up pipe, made with it.

    def run_in_executor(self, executor, func, *args):
        future = super().run_in_executor(executor, func, *args)
        self.jobs += 1
        future.add_done_callback(self._job_done)
        return future

    def _job_done(self, _future):
        self.jobs -= 1

    async def settle(self, cap=None, deadline=None, until=None):
        """Wait in a window (Window) for until (an awaitable: None, until it's settled): what it gave, else None once
        it settled first."""
        with Window(self, cap, deadline) as window:
            if until is None:
                return await window.settled
            task = asyncio.ensure_future(until)
            await asyncio.wait((task, window.settled), return_when=asyncio.FIRST_COMPLETED)
            if task.done():
                return task.result()
            task.cancel()
            return None
