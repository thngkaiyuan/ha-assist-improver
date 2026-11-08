"""Replay sandboxes in worker processes of their own (P11.8.1).

A sandbox is Home Assistant's own engine seeded as the household's home (sandbox.py). Each replay, and each evidence probe
(evidence.py), gets a fresh worker process: it starts for that one job, seeds and serves it, and is killed once the job
is done or given up, or the improver unloads. So nothing a sandbox does can reach the real instance's process (its
memory, its logging, HA's process-wide helpers) or another sandbox, a sandbox that hangs or crashes takes only its own
job down, and nothing of it outlives the job.

Everything a replay runs, runs in its worker: seeding the home, building Assist's prompt and tools, the
household's assistant answering each turn (its model is called from there), the tool calls, and the evidence probes.
The real instance only drives which recorded turn is said (replay.py, simulate.py). To load the assistant, its
provider's config entry, API key included, goes to the worker over the pipe to its stdin; it stays in the worker's
memory and its own folder, which go with it. They talk over the worker's stdin and stdout, one JSON message per line:
{"id", "op", "args"}, answered by {"id", "result"} or {"id", "error"}. Only plain data crosses.

In tests the same messages go to the same server in the test's own process (INLINE), through the same JSON.
"""

import asyncio
from collections import deque
from contextlib import suppress
import contextvars
from datetime import datetime
from functools import partial
import itertools
import json
import logging
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
import traceback

import probatio
from homeassistant.core import async_get_hass
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util.hass_dict import HassKey

from . import guard, household, sandbox, touchpoints
from .cases import CaseError
from .const import DOMAIN
from .sandbox import (STATS as SANDBOX_STATS, THREAD_NAME, Sandbox, call_tool, carry, observe, seed, snapshot,
                      system_prompt)
from .simulation import UnsupportedSimulation
from .standin import write
from .trace import span

_LOGGER = logging.getLogger(__name__)

CALL_SECONDS = 600  # One call longer than this means the worker is stuck: it's killed, and the call fails.
LIMIT = 1 << 30  # The longest message line: a recording is a few MB.
LARGE = 1 << 16  # An answer line longer than this is decoded in Home Assistant's executor, off its loop.
INLINE = False  # Tests: serve in this process, through the same JSON.
STATS = dict.fromkeys(SANDBOX_STATS, 0)  # Sandbox statistics of the workers this process has run, summed.
IMPROVER = Path(__file__).resolve().parent  # The improver's code, which each worker runs.
# The worker's sandbox runs Home Assistant's own code and the improver's alone (whose setup does nothing there):
# custom_components is a folder of the worker's own holding only the improver and the sandbox's own integrations (the
# stand-in devices and the no-op web server, standin.write), so Home Assistant finds none of the household's custom
# integrations there (one a recorded tool names is never set up: its actions and tools are stood in), and sets the
# improver up as an integration, as household knowledge needs (household.serve). The one exception: a household
# assistant's own provider, when it's a custom integration (_folder). The sandbox's config folder is made in the
# worker's own folder too: the real instance makes that folder and removes it once the worker has ended, killed or
# not; the worker removes it itself, should the real instance end first. Before Home Assistant is imported, the worker
# installs its clock (clock.py), from the moment it's started at (seconds since the epoch; none: now), and its
# containment guard (guard.py): it writes only in its folder (no bytecode either). Both are loaded while the improver
# is a bare package (its __init__ imports Home Assistant); then the whole package loads, and its modules find them
# installed, in sys.modules.
BOOT = """import sys, tempfile, types
sys.dont_write_bytecode = True
folder, start = sys.argv[1], sys.argv[2]
tempfile.tempdir = folder
for name in ("custom_components", "custom_components.assist_improver"):
    package = sys.modules[name] = types.ModuleType(name)
    package.__path__ = [folder + "/" + name.replace(".", "/")]
from custom_components.assist_improver.clock import install as clock
clock(float(start) if start else None)
from custom_components.assist_improver.guard import install
install(folder)
del sys.modules["custom_components.assist_improver"]
from custom_components.assist_improver.worker import main
main(folder)"""


class WorkerError(Exception):
    """The worker couldn't answer: it stopped, got stuck, or failed in a way that isn't the replay's."""


class Refused(WorkerError):
    """The worker's guard refused something its sandbox tried (guard.py): its job stops, as the machinery's fault."""


# The real instance's side.

# hass.data, not runtime_data: its callers (the evidence probes) hold no entry.
KEY: HassKey[set["Worker"]] = HassKey(f"{DOMAIN}_workers")  # The workers running.
SLOTS: HassKey[asyncio.Semaphore] = HassKey(f"{DOMAIN}_job_slots")  # Evidence jobs' turns at a worker (job).


class Worker:
    """One job's worker process, as the real instance uses it: await Worker.start(hass), await call(op, **args),
    await stop()."""

    def __init__(self, hass):
        self.hass = hass  # The instance it serves.
        self.process = self.folder = None
        self.pending, self.ids = {}, itertools.count(1)
        self.stderr = deque(maxlen=50)  # The worker's last log lines, for when it stops unexpectedly.
        self.stats = {}  # Its sandbox statistics, as of its last answer (sandbox.STATS there).
        self._writing = asyncio.Lock()  # Messages to it are written in the order sent (_send).
        self._server = None  # INLINE: the server, in this process.

    @classmethod
    async def start(cls, hass, providers=None, start=None):
        """A fresh worker. providers: the household's assistants' providers that are custom integrations
        (replay.custom_providers), for its sandbox to load; start: the recorded moment (ISO) its clock starts at, never
        later than its sandboxes' (clock.py; none: now)."""
        worker = cls(hass)
        hass.data.setdefault(KEY, set()).add(worker)
        if INLINE:
            worker._server = Server()
            return worker
        try:
            worker.folder = await hass.async_add_executor_job(_folder, providers or {})
            # As Home Assistant starts itself (python -P), so no folder's modules shadow its imports (the guard's
            # among them), and in its own folder, not Home Assistant's. In a session of its own, so its process group
            # isn't Home Assistant's: a signal to that group, from code the guard doesn't see, reaches no further. It
            # still ends with Home Assistant, however that stops: its input closes (main).
            moment = str(datetime.fromisoformat(start).timestamp()) if start else ""
            worker.process = await asyncio.create_subprocess_exec(
                sys.executable, "-P", "-c", BOOT, worker.folder, moment, cwd=worker.folder,
                env=environment(worker.folder),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                limit=LIMIT, start_new_session=True)
        except BaseException:
            await worker.stop()
            raise
        hass.async_create_background_task(worker._read(worker.process), "assist_improver_worker_read")
        hass.async_create_background_task(worker._log(worker.process), "assist_improver_worker_log")
        return worker

    async def call(self, op, **args):
        """An operation in the worker."""
        message = {"id": next(self.ids), "op": op, "args": args}
        if self._server is not None:
            # In a context of its own, as in the worker's process: nothing of the caller's (its chat log) in it.
            answer = json.loads(json.dumps(await asyncio.create_task(
                self._server.answer(json.loads(json.dumps(message))), context=contextvars.Context())))
        else:
            answer = await self._send(message)
        self.stats = answer.get("stats") or self.stats
        if "error" in answer:
            err = raised(answer["error"])
            if isinstance(err, touchpoints.Broken):  # The worker's Home Assistant is this one: reported here.
                touchpoints.report(self.hass, err.name, err.reason)
            raise err
        return answer["result"]

    async def _send(self, message):
        future = asyncio.get_running_loop().create_future()
        self.pending[message["id"]] = future
        try:
            # In the order sent, however long each takes to encode: a message's tool calls, which HA runs at once,
            # reach the worker in the order HA started them.
            async with self._writing:
                line = await self.hass.async_add_executor_job(_line, message)  # MBs: off the loop.
                if (process := self.process) is None:  # Stopped, or stopped while this one waited its turn.
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
            if future.done() and not future.cancelled():
                future.exception()  # Seen: a worker that stopped may fail it before it's awaited.

    async def _read(self, process):
        while line := await process.stdout.readline():
            # As the worker wrote it (json.dumps), in order; a long one (a sandbox's states, MBs) off the loop.
            answer = await self.hass.async_add_executor_job(json.loads, line) if len(line) > LARGE else json.loads(line)
            if (future := self.pending.get(answer.get("id"))) is not None and not future.done():
                future.set_result(answer)
        await process.wait()
        self._fail(WorkerError(f"The replay worker stopped (exit {process.returncode})"
                               + (f": {self.stderr[-1]}" if self.stderr else "")))

    def _fail(self, why):
        """Every call waiting for an answer fails: the process asked won't answer."""
        for future in list(self.pending.values()):
            if not future.done():
                future.set_exception(why)

    async def _log(self, process):
        while line := await process.stderr.readline():
            text = line.decode(errors="replace").rstrip()
            self.stderr.append(text[:500])
            _LOGGER.debug("Replay worker: %s", text[:500])

    async def stop(self):
        """Its job is done or given up: the worker is killed (nothing in it is kept), and its folder goes."""
        self.hass.data.get(KEY, set()).discard(self)
        process, self.process = self.process, None
        self._fail(WorkerError("The replay worker stopped"))  # Nothing answers them now.
        for key, value in self.stats.items():  # Each worker counts from zero.
            STATS[key] = max(STATS.get(key, 0), value) if key.startswith("slowest_") else round(
                STATS.get(key, 0) + value, 3)
        self.stats = {}
        if self._server is not None:
            server, self._server = self._server, None
            await server.stop()
        if process is not None:
            with suppress(ProcessLookupError):
                process.kill()
            await process.wait()
        if self.folder is not None:
            folder, self.folder = self.folder, None
            await self.hass.async_add_executor_job(partial(shutil.rmtree, folder, ignore_errors=True))


PREFIX = "assist_improver_worker_"  # A worker's folder's name (_folder), in the temporary folder.
LEFTOVER_SECONDS = 60  # A worker's empty folder this old is a leftover (remove_leftovers), not one just made.


def remove_leftovers():
    """Worker folders an earlier run left behind, removed as the improver is set up. A worker whose Home Assistant ends
    first empties and removes its own folder as it exits (main); where Landlock holds it (guard.landlock), it can
    empty its folder but not remove it, so the folder stays, empty. Only empty ones go, and none just made: another
    Home Assistant's live workers may share the temporary folder."""
    for path in Path(tempfile.gettempdir()).glob(PREFIX + "*"):
        with suppress(OSError):
            if time.time() - path.stat().st_mtime > LEFTOVER_SECONDS:
                path.rmdir()  # Only an empty folder.


def _folder(providers):
    """A worker's own folder (BOOT): its custom_components holds the improver, the sandbox's own integrations
    (standin.write) and the household's assistants' providers that are custom integrations ({domain: its folder}), so
    its sandbox loads the household's assistant by its own integration (DETAILS P6.2.3). No other custom integration
    ever is. A provider gone from home isn't there: it can't load (can't replay)."""
    folder = tempfile.mkdtemp(prefix=PREFIX)
    own = Path(folder, "custom_components")
    own.mkdir()
    (own / DOMAIN).symlink_to(IMPROVER)
    write(own)
    for domain, path in providers.items():
        if not (own / domain).exists() and Path(path).is_dir():
            (own / domain).symlink_to(path)
    return folder


def environment(folder):
    """A worker's environment: the real instance's, without its Supervisor access (DETAILS P11.8.3), so nothing in a
    sandbox can reach the host through the Supervisor; and its temporary files in its folder, as Python's (BOOT), so
    libraries and child processes that read TMPDIR or SQLITE_TMPDIR put theirs there too (C's tmpfile() reads
    neither)."""
    return {**{k: v for k, v in os.environ.items() if not k.startswith(("SUPERVISOR", "HASSIO"))},
            **dict.fromkeys(("TMPDIR", "TEMP", "TMP", "SQLITE_TMPDIR"), folder)}


def _line(message):
    return (json.dumps(message, separators=(",", ":")) + "\n").encode()


async def job(name, **args):
    """One of evidence.py's sandbox jobs, run whole in a worker of its own. However many are asked for at once, at
    most evidence.CONCURRENT of their workers run per instance; the rest wait their turn. A job never waits on another
    while it holds its turn, so none waits forever. Replays' workers (RemoteSandbox) aren't counted."""
    from .evidence import CONCURRENT
    from .replay import speaker
    hass = async_get_hass()  # The probes hold no hass: HA's own accessor finds it.
    if args.get("recording") is not None and args.get("today") is not None:  # Its rights, as a replay has them.
        args["today"] = {**args["today"], "person": await speaker(hass, args["recording"])}
    # Its clock starts at the recording's first moment: its sandboxes start there or later.
    start = (((args.get("recording") or {}).get("turns") or [{}])[0].get("source") or {}).get("at")
    async with hass.data.setdefault(SLOTS, asyncio.Semaphore(CONCURRENT)):
        worker = await Worker.start(hass, start=start)
        try:
            return await worker.call("evidence", job=name, **args)
        finally:
            await worker.stop()


async def shutdown(hass):
    """When the improver unloads: no worker outlives it."""
    for worker in list(hass.data.get(KEY, ())):
        await worker.stop()


def raised(error):
    """The exception a worker's error stands for in the real instance. Home Assistant tells the model a failed tool
    call's error by its class name and text, so those are kept."""
    kind, name, message = error["kind"], error["name"], error["message"]
    if kind == "case":
        return CaseError(message)
    if kind == "unsupported":
        return UnsupportedSimulation(message)
    if kind == "touchpoint":  # The machinery's: never a verdict.
        return touchpoints.Broken(name, message)
    if kind == "refused":
        return Refused(message)
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
    """A replay's sandbox, in a worker of its own, as replay.py uses it: its operations by name."""

    def __init__(self, worker):
        self.worker = worker

    @classmethod
    @span("sandbox.start")
    async def start(cls, hass, time_zone, home=None, setup=None, providers=None):
        """A sandbox set up as today's home (sandbox.Sandbox.start), its clock from the home's moment. providers: the
        household's assistants' providers that are custom integrations (replay.custom_providers)."""
        worker = await Worker.start(hass, providers, (home or {}).get("at"))
        try:
            await worker.call("home.start", time_zone=time_zone, home=home, setup=setup)
        except BaseException:
            await worker.stop()
            raise
        return cls(worker)

    async def call(self, op, **args):
        return await self.worker.call(f"home.{op}", **args)

    async def stop(self):
        async with span("sandbox.stop"):
            await self.worker.stop()


# The worker's side.

class Server:
    """What a worker serves: its home, the evidence probes, and its statistics."""

    def __init__(self):
        self.sandbox = self.api = None

    async def answer(self, message):
        try:
            handler = getattr(self, "op_" + message["op"].replace(".", "_"))
            result = {"result": await handler(**message["args"])}
        except Exception as err:  # noqa: BLE001 - every failure goes back to the caller.
            result = {"error": _error(err)}
        if guard.REFUSED:  # Whatever caught it: once something was refused, the job stops.
            result = {"error": {"kind": "refused", "name": "Refused", "message": guard.REFUSED[0]}}
        return {"id": message["id"], **result, "stats": dict(sandbox.STATS)}

    async def stop(self):
        """INLINE: its sandbox stops, as its process would end."""
        if self.sandbox is not None:
            sandbox, self.sandbox = self.sandbox, None
            await sandbox.stop()

    async def op_home_start(self, time_zone, home=None, setup=None):
        self.sandbox = await Sandbox.start(time_zone, home, setup)

    async def op_home_seed(self, options, person=None, scripts=None, automations=None, exposure_changes=None,
                           moving=(), machinery=(), cut=(), recorded=None):
        await self.sandbox.run(lambda hass: seed(
            hass, person=person, **options, scripts=scripts, automations=automations,
            exposure_changes=exposure_changes, moving=moving, machinery=machinery, cut=cut, recorded=recorded))

    async def op_home_assistant(self, entry, agent):
        """The household's assistant, loaded by its provider's own integration (sandbox.load_assistant), which the
        guard lets reach its provider's own endpoints."""
        guard.allow(entry)
        return {"agent": await self.sandbox.run(lambda hass: sandbox.load_assistant(hass, entry, agent))}

    async def op_home_household(self, knowledge):
        """The household knowledge the sandbox's Assist reads."""
        await self.sandbox.run(lambda hass: household.serve(hass, knowledge))

    async def op_home_prompt(self, context, api_id, template):
        """Assist's system prompt and tools as HA builds them in the sandbox, each tool's parameters as the JSON
        schema its own serializer gives."""
        prompt, api = await self.sandbox.run(lambda hass: system_prompt(hass, llm_context(context), api_id, template))
        self.api = api
        if api is None:
            return {"prompt": prompt, "api_prompt": None, "tools": []}
        tools = [{"name": t.name, "description": t.description, "integration": getattr(t, "integration", None),
                  "parameters": probatio.to_openapi(t.parameters, custom_serializer=api.custom_serializer,
                                                    openapi_version="3.1.0")} for t in api.tools]
        return {"prompt": prompt, "api_prompt": api.api_prompt, "tools": tools}

    async def op_home_turn(self, text, conversation_id, context, language, agent_id, device_id=None, watch=(),
                           until=None):
        """One Assist turn, the household's assistant answering here (sandbox.run_turn), never past until."""
        return await self.sandbox.run(lambda hass: sandbox.run_turn(
            hass, text, conversation_id, context, language, agent_id, device_id, watch, until))

    async def op_home_carry(self, conversation_id, context, steps):
        """A test's turns before its branch, its baseline's, in the replay's chat log (sandbox.carry)."""
        await self.sandbox.run(lambda hass: carry(hass, conversation_id, llm_context(context), steps))

    async def op_home_tool(self, tool_name, tool_args, call_id=None, external=False):
        from homeassistant.helpers import llm
        if self.api is None:
            raise WorkerError("The sandbox has no Assist tools yet")
        tool_input = llm.ToolInput(tool_name=tool_name, tool_args=tool_args, external=external,
                                   **({"id": call_id} if call_id else {}))
        result = await self.sandbox.run(lambda hass: call_tool(hass, self.api, tool_input))
        return {"data": result.data, "error": result.error}

    async def op_home_observe(self, source, moves=()):
        await self.sandbox.run(lambda hass: observe(hass, source, moves))

    async def op_home_snapshot(self, attributes=(), until=None):
        return await self.sandbox.run(lambda hass: snapshot(hass, attributes, until))

    async def op_evidence(self, job, **args):
        """One of evidence.py's sandbox jobs, run here whole."""
        from . import evidence
        return await getattr(evidence, evidence.JOBS[job])(**args)


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
    if isinstance(err, touchpoints.Broken):
        return {"kind": "touchpoint", "name": err.name, "message": err.reason}
    kind = ("unsupported" if isinstance(err, UnsupportedSimulation) else "case" if isinstance(err, CaseError)
            else "ha" if isinstance(err, HomeAssistantError) else "invalid" if isinstance(err, probatio.Invalid)
            else "other")
    return {"kind": kind, "name": type(err).__name__, "message": str(err)[:2000]}


def main(folder):
    """The worker process: answer messages from stdin. The real instance kills it once its job is done; should the
    real instance end first, its input closes, and it exits at once, its folder removed."""
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
    # Its sandbox's lines go to the replay (sandbox._Log), not to Home Assistant's log: stderr is the worker's own.
    logging.getLogger().handlers[0].addFilter(lambda record: record.threadName != THREAD_NAME)
    code = 0
    try:
        asyncio.run(_serve())
    except BaseException:  # A crash: its traceback ends the stderr the real instance reports it with.
        traceback.print_exc()
        code = 1
    finally:
        shutil.rmtree(folder, ignore_errors=True)
        os._exit(code)  # Its sandbox's threads aren't waited for.


async def _serve():
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=LIMIT)
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    out, tasks, server = sys.stdout.buffer, set(), Server()

    async def one(message):
        out.write(await loop.run_in_executor(None, _line, await server.answer(message)))
        out.flush()
    while line := await reader.readline():
        task = loop.create_task(one(json.loads(line)))
        tasks.add(task)
        task.add_done_callback(tasks.discard)
