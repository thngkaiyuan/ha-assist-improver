"""Replay sandboxes in a worker process of their own (N6).

A sandbox is Home Assistant's own engine seeded as a recorded home (sandbox.py). It runs in a separate Python
process, so nothing it does can reach the real instance's process: not its memory, its logging or HA's
process-wide helpers, and a sandbox that hangs or crashes takes only the worker down. The worker starts when a
replay first needs it, serves every sandbox after that, and stops once idle or when the improver unloads.

The model stays in the real instance: a replay's chat log and the household's assistant run there (replay.py), and
only Home Assistant's side runs in the worker: seeding the recorded home, building Assist's prompt and tools, the
tool calls, and the evidence probes (evidence.py). They talk over the worker's stdin and stdout, one JSON message
per line: {"id", "op", "args"}, answered by {"id", "result"} or {"id", "error"}. Only plain data crosses.

In tests the same messages go to the same server in the test's own process (INLINE), through the same JSON.
"""

import asyncio
from collections import deque
from contextlib import suppress
import contextvars
import itertools
import json
import logging
from pathlib import Path
import sys
from uuid import uuid4

import probatio
from homeassistant.exceptions import HomeAssistantError

from .cases import CaseError
from .sandbox import STATS as SANDBOX_STATS
from .simulation import UnsupportedSimulation
from .trace import span

_LOGGER = logging.getLogger(__name__)

IDLE_SECONDS = 120  # Stopped after this long with nothing to do: no call, and no sandbox open.
CALL_SECONDS = 600  # One call longer than this means the worker is stuck: it's stopped, and the call fails.
LIMIT = 1 << 30  # The longest message line: a recording is a few MB.
INLINE = False  # Tests: serve in this process, through the same JSON.
STATS = dict(SANDBOX_STATS)  # Sandbox statistics across every worker this process started (sandbox.STATS there).
# The worker loads only the modules sandboxes use: the integration's own setup (its __init__) never runs there. Its
# sandboxes run Home Assistant's own code and the improver's alone: custom_components is a folder of the worker's own
# holding only the improver, so Home Assistant finds none of the household's custom integrations there (one a recorded
# tool names is never set up, nor its tools run), and finds the improver as an integration, as household knowledge
# needs (household.serve). The one exception: a household assistant's own provider, when it's a custom integration
# (link_providers).
BOOT = """import atexit, os, shutil, sys, tempfile, types
improver = sys.argv[1] + "/custom_components/assist_improver"
own = tempfile.mkdtemp(prefix="assist_improver_worker_")
atexit.register(shutil.rmtree, own, True)
os.symlink(improver, own + "/assist_improver")
for name, path in (("custom_components", own), ("custom_components.assist_improver", improver)):
    package = types.ModuleType(name)
    package.__path__ = [path]
    sys.modules[name] = package
from custom_components.assist_improver.worker import main
main()"""


class WorkerError(Exception):
    """The worker couldn't answer: it stopped, got stuck, or failed in a way that isn't the replay's."""


# The real instance's side.

class Worker:
    """The replay worker, as the real instance uses it: await call(op, **args)."""

    def __init__(self, root):
        self.root = root  # The folder custom_components is in.
        self.process = None
        self.pending, self.ids = {}, itertools.count(1)
        self.stderr = deque(maxlen=50)  # The worker's last log lines, for when it stops unexpectedly.
        self.busy = 0
        self.open = set()  # The sandboxes started and not yet stopped: a worker with one in use isn't idle.
        self._idle = self._reading = None
        self._before = dict.fromkeys(SANDBOX_STATS, 0)  # Statistics of the workers that have stopped, summed.
        self._now = dict(self._before)  # The running worker's own, as of its last answer.
        self._starting = asyncio.Lock()
        self._writing = None  # The running process's lock: messages to it are written in the order sent (_send).
        self._server = None  # INLINE: the server, in this process.
        self.generation = 0  # Counts the workers started: a sandbox lives only as long as the one it was started in.
        self.listeners = {}  # {call's message id: what its deltas go to, as they come}: a turn streaming its reply.
        self.reader = None  # Answers a sandbox's read of the real home (practice.py); none: every read is refused.

    async def call(self, op, listener=None, **args):
        """An operation in the worker; listener: given each delta it streams before its answer (home.turn)."""
        message = {"id": next(self.ids), "op": op, "args": args}
        if listener is not None:
            self.listeners[message["id"]] = listener
        self.busy += 1
        if self._idle is not None:
            self._idle.cancel()
            self._idle = None
        try:
            if INLINE:
                if self._server is None:
                    loop = asyncio.get_running_loop()
                    self._server = Server(  # Called from a sandbox's thread, as the worker process's are.
                        lambda asked, delta: loop.call_soon_threadsafe(
                            self._delta, asked, json.loads(json.dumps(delta))),
                        lambda request: asyncio.run_coroutine_threadsafe(self._read_for(request), loop))
                    self.generation += 1
                # In a context of its own, as in the worker's process: nothing of the caller's (its chat log) in it.
                answer = json.loads(json.dumps(await asyncio.create_task(
                    self._server.answer(json.loads(json.dumps(message))), context=contextvars.Context())))
            else:
                answer = await self._send(message)
            if op == "home.start" and "result" in answer:
                self.open.add(answer["result"])
        finally:
            self.listeners.pop(message["id"], None)
            if op == "home.stop":
                self.open.discard(args.get("session"))
            self.busy -= 1
            if not self.busy and not self.open and not INLINE:
                self._idle = asyncio.get_running_loop().call_later(IDLE_SECONDS, self._stop_soon)
        self._count(answer.get("stats"))
        if "error" in answer:
            raise raised(answer["error"])
        return answer["result"]

    def _delta(self, asked, delta):
        if (listener := self.listeners.get(asked)) is not None:
            listener(delta)

    async def _read_for(self, request):
        """A sandbox's read of the real home ({domain, service, data}), answered by the reader."""
        if self.reader is None:
            raise WorkerError("This sandbox can't read the real home")
        return json.loads(json.dumps(await self.reader(request)))

    async def _answer_read(self, process, read_id, request):
        try:
            reply = {"result": await self._read_for(request)}
        except Exception as err:  # noqa: BLE001 - the sandbox's read fails as it would at home.
            reply = {"error": _error(err)}
        line = _line({"id": read_id, "op": "read_answer", "args": {"read_id": read_id, **reply}})
        async with self._writing:
            if self.process is process:
                process.stdin.write(line)
                await process.stdin.drain()

    def _count(self, stats):
        """STATS: the stopped workers' statistics and the running one's (each worker counts from zero)."""
        if not stats:
            return
        self._now = stats
        for key, value in stats.items():
            before = self._before.get(key, 0)
            STATS[key] = max(before, value) if key.startswith("slowest_") else round(before + value, 3)

    def _restarted(self):
        for key, value in self._now.items():
            before = self._before.get(key, 0)
            self._before[key] = max(before, value) if key.startswith("slowest_") else round(before + value, 3)
        self._now = dict.fromkeys(self._now, 0)

    async def _send(self, message):
        await self._start()
        future = asyncio.get_running_loop().create_future()
        self.pending[message["id"]] = future
        try:
            # In the order sent, however long each takes to encode: a message's tool calls, which HA runs at once,
            # reach the worker in the order HA started them.
            process = self.process
            async with self._writing:
                line = await asyncio.get_running_loop().run_in_executor(None, _line, message)  # MBs: off the loop.
                if self.process is not process:  # Stopped while this one waited its turn.
                    raise WorkerError("The replay worker stopped")
                process.stdin.write(line)
                await process.stdin.drain()
            return await asyncio.wait_for(future, CALL_SECONDS)
        except TimeoutError:
            await self.stop()
            raise WorkerError(f"The replay worker didn't answer {message['op']} in {CALL_SECONDS} s") from None
        except (ConnectionError, BrokenPipeError) as err:
            raise WorkerError(f"The replay worker stopped: {err}") from err
        finally:
            self.pending.pop(message["id"], None)

    async def _start(self):
        async with self._starting:
            if self.process is not None and self.process.returncode is None:
                return
            self._restarted()
            self.open.clear()  # A worker that stopped stopped its sandboxes too.
            self.generation += 1
            self.process = await asyncio.create_subprocess_exec(
                sys.executable, "-c", BOOT, str(self.root), stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, limit=LIMIT)
            self._writing = asyncio.Lock()  # Made with each process, on the loop that writes to it.
            self._reading = (asyncio.create_task(self._read(self.process), name="assist_improver_worker_read"),
                             asyncio.create_task(self._log(self.process), name="assist_improver_worker_log"))
            _LOGGER.debug("Replay worker started (pid %s)", self.process.pid)

    async def _read(self, process):
        while line := await process.stdout.readline():
            answer = json.loads(line)
            if "delta" in answer:  # A turn streaming its reply, before its answer.
                self._delta(answer["id"], answer["delta"])
                continue
            if "read" in answer:  # A sandbox asking the real home.
                asyncio.get_running_loop().create_task(self._answer_read(process, answer["read_id"], answer["read"]),
                                                       name="assist_improver_worker_read")
                continue
            if (future := self.pending.get(answer.get("id"))) is not None and not future.done():
                future.set_result(answer)
        await process.wait()
        why = WorkerError(f"The replay worker stopped (exit {process.returncode})"
                          + (f": {self.stderr[-1]}" if self.stderr else ""))
        for future in list(self.pending.values()):
            if not future.done():
                future.set_exception(why)

    async def _log(self, process):
        while line := await process.stderr.readline():
            text = line.decode(errors="replace").rstrip()
            self.stderr.append(text[:500])
            _LOGGER.debug("Replay worker: %s", text[:500])

    def _stop_soon(self):
        self._idle = None
        if not self.busy and not self.open:
            asyncio.get_running_loop().create_task(self.stop(), name="assist_improver_worker_stop")

    async def stop(self):
        """End the worker: it stops its sandboxes and exits once its input closes; it's killed if it doesn't."""
        if self._idle is not None:
            self._idle.cancel()
            self._idle = None
        if self._server is not None:
            server, self._server = self._server, None
            await server.stop()
        process, self.process = self.process, None
        self.open.clear()
        if process is None or process.returncode is not None:
            return
        with suppress(Exception):
            process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), 30)
        except TimeoutError:
            process.kill()
            await process.wait()
        _LOGGER.debug("Replay worker stopped")


def _line(message):
    return (json.dumps(message, separators=(",", ":")) + "\n").encode()


_WORKER = None
_ASKING = contextvars.ContextVar("asking", default=None)  # The message a worker operation answers.


def worker():
    """The replay worker (one per Home Assistant process)."""
    global _WORKER
    if _WORKER is None:
        _WORKER = Worker(Path(__file__).resolve().parents[2])
    return _WORKER


async def shutdown():
    """When the improver unloads: no worker outlives it."""
    if _WORKER is not None:
        await _WORKER.stop()


def raised(error):
    """The exception a worker's error stands for in the real instance. Home Assistant tells the model a failed tool
    call's error by its class name and text, so those are kept."""
    kind, name, message = error["kind"], error["name"], error["message"]
    if kind == "case":
        return CaseError(message)
    if kind == "unsupported":
        return UnsupportedSimulation(message)
    base = {"ha": HomeAssistantError, "invalid": probatio.Invalid}.get(kind)
    if base is None:
        return WorkerError(f"{name}: {message}")
    return _named(kind, name, base)(message)


_NAMED = {}


def _named(kind, name, base):
    if (kind, name) not in _NAMED:
        _NAMED[kind, name] = type(name, (base,), {"__str__": lambda self: str(self.args[0]) if self.args else ""})
    return _NAMED[kind, name]


class RemoteSandbox:
    """A sandbox in the worker, as replay.py uses it: its operations by name, and what it reported."""

    def __init__(self, session, generation=None):
        self.session, self.generation = session, generation
        self.unsimulated = []  # Device commands the sandbox has no emulation for, as of its last answer.
        self.log = []  # What HA logged inside it, as of its last answer.

    @classmethod
    @span("sandbox.start")
    async def start(cls, time_zone, providers=None):
        """providers: the household's assistants' providers that are custom integrations (replay.custom_providers)."""
        session = await worker().call("home.start", time_zone=time_zone, providers=providers or {})
        return cls(session, worker().generation)

    @property
    def live(self):
        """Whether the worker it was started in still runs (one started since has sandboxes of its own)."""
        return self.generation == worker().generation

    async def call(self, op, **args):
        found = await worker().call(f"home.{op}", session=self.session, **args)
        if isinstance(found, dict) and "unsimulated" in found:
            self.unsimulated, self.log = found["unsimulated"], found.get("log", self.log)
        return found

    async def stop(self):
        async with span("sandbox.stop"):
            with suppress(WorkerError):  # A worker that stopped has stopped its sandboxes too.
                await worker().call("home.stop", session=self.session)


# The worker's side.

def link_providers(providers):
    """The household's assistants' providers that are custom integrations ({domain: its folder}), linked into the
    worker's own custom_components beside the improver, so its sandboxes load the household's assistant by its own
    integration (DETAILS P6.2.3). No other custom integration ever is (BOOT)."""
    import os
    import custom_components
    own = Path(custom_components.__path__[0])
    for domain, folder in providers.items():
        link = own / domain
        if domain == "assist_improver" or not Path(folder).is_dir() or (
                link.is_symlink() and os.readlink(link) == folder):
            continue  # The improver's own, a copy gone from home (it can't load: can't replay), or linked already.
        if link.is_symlink():
            link.unlink()  # The household's copy has moved: linked where it is now.
        os.symlink(folder, link)


class Server:
    """The operations a worker serves: recorded homes (sessions), the evidence probes, and its statistics.

    on_delta(asked, delta): a streaming operation's delta, for the message that asked; on_read(request): a sandbox's
    read of the real home, a future of its answer. Both are called from a sandbox's thread."""

    def __init__(self, on_delta=None, on_read=None):
        self.sessions, self.on_delta, self.on_read = {}, on_delta, on_read
        self.reads = {}  # {read id: its future}: reads of the real home awaiting their answer (op_read_answer).

    async def answer(self, message):
        from . import sandbox
        if message["op"] == "read_answer":  # The real home answering a read: no answer of its own.
            self.op_read_answer(**message["args"])
            return None
        _ASKING.set(message["id"])
        try:
            handler = getattr(self, "op_" + message["op"].replace(".", "_"))
            result = {"result": await handler(**message["args"])}
        except Exception as err:  # noqa: BLE001 - every failure goes back to the caller.
            result = {"error": _error(err)}
        return {"id": message["id"], **result, "stats": dict(sandbox.STATS)}

    async def stop(self):
        for session in list(self.sessions):
            await self.op_home_stop(session)

    def op_read_answer(self, read_id, result=None, error=None):
        if (future := self.reads.pop(read_id, None)) is not None and not future.done():
            if error is not None:
                future.set_exception(raised(error))
            else:
                future.set_result(result)

    def _home(self, session):
        if session not in self.sessions:
            raise WorkerError("That sandbox has stopped")
        return self.sessions[session]

    @staticmethod
    def _reported(home, extra=None):
        return {**(extra or {}), "unsimulated": list(home.sandbox.unsimulated), "log": list(home.sandbox.log)}

    async def op_home_start(self, time_zone, providers=None):
        from . import sandbox
        from .sandbox import Sandbox
        if providers and sandbox.OWN_PROCESS and not INLINE:  # Before the sandbox first looks for custom ones.
            link_providers(providers)
        session = uuid4().hex  # Never one another worker gave out: a sandbox from before a restart finds none.
        self.sessions[session] = _Home(await Sandbox.start(time_zone))
        self.sessions[session].sandbox.reader = self.on_read
        return session

    async def op_home_seed(self, session, source, options, user_name=None, scripts=None, automations=None,
                           exposure_changes=None, alias_changes=None, alias_removals=None, moving=(), lists=None,
                           timers=(), reads=False, calendars=None, forecasts=None):
        from .sandbox import seed
        home = self._home(session)
        await home.sandbox.run(lambda hass: seed(
            hass, source, user_name=user_name, **options, scripts=scripts, automations=automations,
            exposure_changes=exposure_changes, alias_changes=alias_changes, alias_removals=alias_removals,
            moving=moving, lists=lists, timers=timers, reads=reads, calendars=calendars, forecasts=forecasts))
        return self._reported(home)

    async def op_home_assistant(self, session, entry, agent):
        """The household's assistant, loaded by its provider's own integration (sandbox.load_assistant)."""
        from .sandbox import load_assistant
        home = self._home(session)
        return self._reported(home, {"agent": await home.sandbox.run(lambda hass: load_assistant(hass, entry, agent))})

    async def op_home_household(self, session, knowledge, legacy=False):
        """The household knowledge the sandbox's Assist reads; legacy: through the household API (up to 0.37)."""
        from .household import LegacyAPI, serve
        from .replay import register
        home = self._home(session)

        async def read(hass):
            if legacy:
                await register(hass, LegacyAPI(hass, knowledge))
            else:
                serve(hass, knowledge)
        await home.sandbox.run(read)
        return self._reported(home)

    async def op_home_prompt(self, session, context, api_id, template):
        """Assist's system prompt and tools as HA builds them in the sandbox, each tool's parameters as the JSON
        schema its own serializer gives."""
        from .sandbox import system_prompt
        home = self._home(session)
        prompt, api = await home.sandbox.run(lambda hass: system_prompt(hass, llm_context(context), api_id, template))
        home.api = api
        if api is None:
            return self._reported(home, {"prompt": prompt, "api_prompt": None, "tools": []})
        tools = [{"name": t.name, "description": t.description, "integration": getattr(t, "integration", None),
                  "parameters": probatio.to_openapi(t.parameters, custom_serializer=api.custom_serializer,
                                                    openapi_version="3.1.0")} for t in api.tools]
        return self._reported(home, {"prompt": prompt, "api_prompt": api.api_prompt, "tools": tools})

    async def op_home_turn(self, session, text, conversation_id, context, language, agent_id, device_id=None,
                           watch=(), stream=False, settle=True, since=None, extra_system_prompt=None):
        """One Assist turn, the household's assistant answering here (sandbox.run_turn); stream: each delta of its
        reply, as it comes, ahead of the answer."""
        from functools import partial
        from .sandbox import run_turn
        home = self._home(session)
        emit = partial(self.on_delta, _ASKING.get()) if stream and self.on_delta else None
        return self._reported(home, await home.sandbox.run(lambda hass: run_turn(
            hass, text, conversation_id, context, language, agent_id, device_id, watch, emit, settle, since,
            extra_system_prompt)))

    async def op_home_tool(self, session, tool_name, tool_args, call_id=None, external=False):
        from homeassistant.helpers import llm
        from .sandbox import call_tool
        home = self._home(session)
        if home.api is None:
            raise WorkerError("The sandbox has no Assist tools yet")
        tool_input = llm.ToolInput(tool_name=tool_name, tool_args=tool_args, external=external,
                                   **({"id": call_id} if call_id else {}))
        result = await home.sandbox.run(lambda hass: call_tool(hass, home.api, tool_input))
        return self._reported(home, {"data": result.data, "error": result.error})

    async def op_home_outside(self, session, apis):
        """The tools of the household assistant's other LLM APIs, as it has them at home (sandbox.outside_tools)."""
        from .sandbox import outside_tools
        home = self._home(session)
        await home.sandbox.run(lambda hass: outside_tools(hass, apis))

    async def op_home_observe(self, session, source, moves=(), settle=True):
        from .sandbox import observe
        home = self._home(session)
        await home.sandbox.run(lambda hass: observe(hass, source, moves, settle))
        return self._reported(home)

    async def op_home_stores(self, session, source, calendars, forecasts):
        """The home's calendars and forecasts now, but a calendar the copy wrote (sandbox.hold_stores)."""
        from .sandbox import hold_stores
        home = self._home(session)
        await home.sandbox.run(lambda hass: hold_stores(hass, source, calendars, forecasts))

    async def op_home_lists(self, session, source, lists, given, settle=True):
        """The home's to-do lists now, but those the sandbox changed (sandbox.hold_lists)."""
        from .sandbox import hold_lists
        home = self._home(session)
        return {"held": await home.sandbox.run(lambda hass: hold_lists(hass, source, lists, given, settle))}

    async def op_home_snapshot(self, session, attributes=(), settle=True):
        from .sandbox import snapshot
        home = self._home(session)
        return self._reported(home, await home.sandbox.run(lambda hass: snapshot(hass, attributes, settle)))

    async def op_home_stop(self, session):
        home = self.sessions.pop(session, None)
        if home is not None:
            await home.sandbox.stop()

    async def op_evidence(self, job, **args):
        """One of evidence.py's sandbox jobs, run here whole."""
        from . import evidence
        return await getattr(evidence, evidence.JOBS[job])(**args)


class _Home:
    def __init__(self, sandbox):
        self.sandbox, self.api = sandbox, None


def llm_context(found):
    """An LLMContext from its plain form (context_data)."""
    from homeassistant.core import Context
    from homeassistant.helpers import llm
    context = found.get("context")
    return llm.LLMContext(platform=found["platform"], context=Context(**context) if context else None,
                          language=found.get("language"), assistant=found["assistant"],
                          device_id=found.get("device_id"))


def context_data(llm_context):
    """An LLMContext's plain form, to cross to the worker."""
    context = llm_context.context
    return {"platform": llm_context.platform, "language": llm_context.language, "assistant": llm_context.assistant,
            "device_id": llm_context.device_id,
            "context": {"id": context.id, "user_id": context.user_id, "parent_id": context.parent_id}
            if context is not None else None}


def _error(err):
    kind = ("unsupported" if isinstance(err, UnsupportedSimulation) else "case" if isinstance(err, CaseError)
            else "ha" if isinstance(err, HomeAssistantError) else "invalid" if isinstance(err, probatio.Invalid)
            else "other")
    return {"kind": kind, "name": type(err).__name__, "message": str(err)[:2000]}


def main():
    """The worker process: answer messages from stdin until it closes, then stop every sandbox and exit."""
    from . import sandbox
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
    sandbox.OWN_PROCESS = True  # Only sandboxes run here: the process's time zone is the home's.
    asyncio.run(_serve())


async def _serve():
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=LIMIT)
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    out = sys.stdout.buffer
    asked_ids, tasks = itertools.count(1), set()

    def write(message):
        out.write(_line(message))
        out.flush()

    def read(request):
        async def ask():
            read_id = next(asked_ids)
            future = server.reads[read_id] = loop.create_future()
            write({"read": request, "read_id": read_id})
            return await future
        return asyncio.run_coroutine_threadsafe(ask(), loop)
    server = Server(lambda asked, delta: loop.call_soon_threadsafe(write, {"id": asked, "delta": delta}), read)

    async def one(message):
        answer = await server.answer(message)
        if answer is not None:
            out.write(await loop.run_in_executor(None, _line, answer))
            out.flush()
    try:
        while line := await reader.readline():
            task = loop.create_task(one(json.loads(line)))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
        if tasks:
            await asyncio.wait(tasks, timeout=30)
    finally:
        await server.stop()
