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
import itertools
import json
import logging
from pathlib import Path
import sys

import probatio
from homeassistant.exceptions import HomeAssistantError

from .cases import CaseError
from .sandbox import STATS as SANDBOX_STATS
from .simulation import UnsupportedSimulation
from .trace import span

_LOGGER = logging.getLogger(__name__)

IDLE_SECONDS = 120  # Stopped after this long with nothing to do.
CALL_SECONDS = 600  # One call longer than this means the worker is stuck: it's stopped, and the call fails.
LIMIT = 1 << 30  # The longest message line: a recording is a few MB.
INLINE = False  # Tests: serve in this process, through the same JSON.
STATS = dict(SANDBOX_STATS)  # Sandbox statistics across every worker this process started (sandbox.STATS there).
# The worker loads only the modules sandboxes use: the integration's own setup (its __init__) never runs there.
BOOT = """import sys, types
for name, path in (("custom_components", "custom_components"),
                   ("custom_components.assist_improver", "custom_components/assist_improver")):
    package = types.ModuleType(name)
    package.__path__ = [sys.argv[1] + "/" + path]
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
        self._idle = self._reading = None
        self._before = dict.fromkeys(SANDBOX_STATS, 0)  # Statistics of the workers that have stopped, summed.
        self._now = dict(self._before)  # The running worker's own, as of its last answer.
        self._starting = asyncio.Lock()
        self._server = None  # INLINE: the server, in this process.

    async def call(self, op, **args):
        message = {"id": next(self.ids), "op": op, "args": args}
        self.busy += 1
        if self._idle is not None:
            self._idle.cancel()
            self._idle = None
        try:
            if INLINE:
                self._server = self._server or Server()
                answer = json.loads(json.dumps(await self._server.answer(json.loads(json.dumps(message)))))
            else:
                answer = await self._send(message)
        finally:
            self.busy -= 1
            if not self.busy and not INLINE:
                self._idle = asyncio.get_running_loop().call_later(IDLE_SECONDS, self._stop_soon)
        self._count(answer.get("stats"))
        if "error" in answer:
            raise raised(answer["error"])
        return answer["result"]

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
            line = await asyncio.get_running_loop().run_in_executor(None, _line, message)  # MBs: off the loop.
            self.process.stdin.write(line)
            await self.process.stdin.drain()
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
            self.process = await asyncio.create_subprocess_exec(
                sys.executable, "-c", BOOT, str(self.root), stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, limit=LIMIT)
            self._reading = (asyncio.create_task(self._read(self.process), name="assist_improver_worker_read"),
                             asyncio.create_task(self._log(self.process), name="assist_improver_worker_log"))
            _LOGGER.debug("Replay worker started (pid %s)", self.process.pid)

    async def _read(self, process):
        while line := await process.stdout.readline():
            answer = json.loads(line)
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
        if not self.busy:
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

    def __init__(self, session):
        self.session = session
        self.unsimulated = []  # Device commands the sandbox has no emulation for, as of its last answer.
        self.log = []  # What HA logged inside it, as of its last answer.

    @classmethod
    @span("sandbox.start")
    async def start(cls, time_zone):
        return cls(await worker().call("home.start", time_zone=time_zone))

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

class Server:
    """The operations a worker serves: recorded homes (sessions), the evidence probes, and its statistics."""

    def __init__(self):
        self.sessions, self.ids = {}, itertools.count(1)

    async def answer(self, message):
        from . import sandbox
        try:
            handler = getattr(self, "op_" + message["op"].replace(".", "_"))
            result = {"result": await handler(**message["args"])}
        except Exception as err:  # noqa: BLE001 - every failure goes back to the caller.
            result = {"error": _error(err)}
        return {"id": message["id"], **result, "stats": dict(sandbox.STATS)}

    async def stop(self):
        for session in list(self.sessions):
            await self.op_home_stop(session)

    def _home(self, session):
        if session not in self.sessions:
            raise WorkerError("That sandbox has stopped")
        return self.sessions[session]

    @staticmethod
    def _reported(home, extra=None):
        return {**(extra or {}), "unsimulated": list(home.sandbox.unsimulated), "log": list(home.sandbox.log)}

    async def op_home_start(self, time_zone):
        from .sandbox import Sandbox
        session = next(self.ids)
        self.sessions[session] = _Home(await Sandbox.start(time_zone))
        return session

    async def op_home_seed(self, session, source, options, user_name=None, scripts=None, automations=None,
                           exposure_changes=None, alias_changes=None, alias_removals=None):
        from .sandbox import seed
        home = self._home(session)
        await home.sandbox.run(lambda hass: seed(
            hass, source, user_name=user_name, **options, scripts=scripts, automations=automations,
            exposure_changes=exposure_changes, alias_changes=alias_changes, alias_removals=alias_removals))
        return self._reported(home)

    async def op_home_household(self, session, knowledge):
        from .household import HouseholdAPI
        from .replay import register
        home = self._home(session)
        await home.sandbox.run(lambda hass: register(hass, HouseholdAPI(hass, knowledge)))
        return self._reported(home)

    async def op_home_prompt(self, session, context, api_id, template):
        """Assist's system prompt and tools as HA builds them in the sandbox; each tool's parameters as the JSON
        schema its own serializer gives, which providers read back unchanged (replay.Precomputed)."""
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

    async def op_home_tool(self, session, tool_name, tool_args, call_id=None, external=False):
        from homeassistant.helpers import llm
        home = self._home(session)
        if home.api is None:
            raise WorkerError("The sandbox has no Assist tools yet")
        tool_input = llm.ToolInput(tool_name=tool_name, tool_args=tool_args, external=external,
                                   **({"id": call_id} if call_id else {}))
        result = await home.sandbox.run(lambda hass: home.api.async_call_tool(tool_input))
        return self._reported(home, {"data": result.data, "error": result.error})

    async def op_home_observe(self, session, source, exclude=()):
        from .sandbox import observe
        home = self._home(session)
        await home.sandbox.run(lambda hass: observe(hass, source, exclude))
        return self._reported(home)

    async def op_home_snapshot(self, session):
        from .sandbox import snapshot
        home = self._home(session)
        return self._reported(home, await home.sandbox.run(snapshot))

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
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
    asyncio.run(_serve())


async def _serve():
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=LIMIT)
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    out = sys.stdout.buffer
    server, tasks = Server(), set()

    async def one(message):
        answer = await server.answer(message)
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
