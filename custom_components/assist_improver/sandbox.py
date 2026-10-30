"""A sandboxed Home Assistant instance: HA's own engine, acting on a simulated home.

Each sandbox is a separate HomeAssistant object in its own thread and event
loop, so the real instance stays the one HA's helpers find. It runs HA's own
integrations (intents, the Assist API, scripts, automations) over a copy of
the home; only the commands to devices are emulated (devices.py). It has no
network listener and no device integrations, so nothing in it can reach a real
device. It lives only as long as one replay.

What it shares with the real instance is the process, and it leaves nothing of itself there: HA's frame helper goes
back to the real instance before the sandbox's start first awaits; its log records are kept out of HA's log by a
filter that comes off with the last sandbox; its entries in HA's memo caches go when it stops (a single-entry memo
it took over is recomputed for the real instance on its next call); and the time zone is its own config's only. In
the replay worker's own process (OWN_PROCESS) nothing else shares it, so the process's default time zone is the
home's there too, as the real instance has it.

What Assist reads and sets off is as of the conversation: seeded as the home at a recorded turn, and moved on to each
later one, the sandbox renders Assist's instructions (their now()) and runs Assist's tool calls at that turn's time
(HA's date and time tool, and the scripts and automations a call sets off, their time conditions included), as HA's
own tests freeze time where they need to. Only there: while sandboxes run, HA's dt_util.now reads the recorded time
inside those, and Home Assistant's own time everywhere else. HA's scheduler reads utcnow and the system clock, which
stay real (timed automations aren't all due at once).
"""

import asyncio
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from datetime import datetime
import functools
import gc
import logging
import sys
import tempfile
import threading
import time
from types import ModuleType, SimpleNamespace

import probatio
from homeassistant.core import SupportsResponse, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from . import devices
from .trace import span

_LOGGER = logging.getLogger(__name__)

SANDBOX_KEY = "assist_improver_sandbox"
RUNNING = set()  # Sandboxes not yet stopped; stop_all() ends them when the improver unloads.
THREAD_NAME = "assist-improver-sandbox"
SANDBOX_LOG_LIMIT = 200
# True in the replay worker's own process (worker.main), where only sandboxes run: there the process's default time
# zone is the home's, which templates' now() reads (an assistant's instructions, HA's date and time line).
OWN_PROCESS = False
DEVICE_SERVICES = {
    "light": ("turn_on", "turn_off", "toggle"),
    "switch": ("turn_on", "turn_off", "toggle"),
    "fan": ("turn_on", "turn_off", "toggle", "set_percentage", "set_preset_mode", "increase_speed",
            "decrease_speed", "oscillate", "set_direction"),
    "media_player": ("turn_on", "turn_off", "toggle", "media_play", "media_pause", "media_stop",
                     "media_play_pause", "volume_set", "volume_up", "volume_down", "volume_mute", "select_source"),
    "climate": ("turn_on", "turn_off", "toggle", "set_hvac_mode", "set_temperature", "set_fan_mode",
                "set_preset_mode", "set_humidity", "set_swing_mode"),
    "cover": ("open_cover", "close_cover", "stop_cover", "set_cover_position"),
    "valve": ("open_valve", "close_valve", "stop_valve", "set_valve_position"),
    "lock": ("lock", "unlock", "open"),
    "vacuum": ("start", "pause", "stop", "return_to_base", "clean_area", "clean_spot"),
    "select": ("select_option",), "input_select": ("select_option",),
    "number": ("set_value",), "input_number": ("set_value",),
    "input_boolean": ("turn_on", "turn_off", "toggle"),
    "humidifier": ("turn_on", "turn_off", "toggle", "set_humidity", "set_mode"),
    "water_heater": ("turn_on", "turn_off", "set_temperature", "set_operation_mode"),
    "siren": ("turn_on", "turn_off", "toggle"), "remote": ("turn_on", "turn_off", "toggle"),
}


def _forget(hass):
    """Drop the sandbox's entries from Home Assistant's process-wide caches, so nothing outlives it; the real
    instance's entries stay.

    HA memoises helpers per instance (registries, translations, template helpers), and any instance that ran leaves
    entries there. Only those caches are looked at, so this takes as long as the sandbox's own entries, not as long as
    the process is big. It runs on the sandbox's thread once its loop has closed, never on HA's loop. The instance's
    own reference cycles go with Python's garbage collector in its normal course: a forced collection would walk
    the whole process.
    """
    from homeassistant.helpers.template import states
    for cache in (states.CACHED_TEMPLATE_LRU, states.CACHED_TEMPLATE_NO_COLLECT_LRU):  # Keyed by state, per instance.
        for key, value in cache.items():
            if getattr(value, "_hass", None) is hass:
                cache.pop(key, None)
    for cache in _instance_caches():
        _drop(cache, hass)


_INSTANCE_CACHES = {}  # Module name -> (its namespace's size when looked at, its lru_caches that take an instance).
_INSTANCE_CACHES_LOCK = threading.Lock()


def _instance_caches():
    """HA's memoised helpers keyed by an instance: module-level lru_caches whose function takes `hass` (HA's
    singletons, template helpers, ...). Found in the loaded modules, so HA's list of them isn't hard-coded here.
    A module is looked at again only when its namespace changed size (imported since, or still importing then)."""
    with _INSTANCE_CACHES_LOCK:
        for name, module in list(sys.modules.items()):
            if not isinstance(module, ModuleType):
                continue
            namespace = module.__dict__
            if (seen := _INSTANCE_CACHES.get(name)) is None or seen[0] != len(namespace):
                _INSTANCE_CACHES[name] = (len(namespace), [
                    value for value in list(namespace.values())
                    if isinstance(value, functools._lru_cache_wrapper) and _takes_hass(value)])
        return list({id(cache): cache for _, caches in _INSTANCE_CACHES.values() for cache in caches}.values())


def _takes_hass(cache):
    function = cache
    for _ in range(5):  # The wrapped functions, as functools.wraps chains them.
        if (function := getattr(function, "__wrapped__", None)) is None:
            return False
        code = getattr(function, "__code__", None)
        if code is not None and "hass" in code.co_varnames[:code.co_argcount + code.co_kwonlyargcount]:
            return True
    return False


def _drop(cache, hass):
    """Remove an instance's entries from one lru_cache. An unbounded one (functools.cache) loses just those
    entries; a bounded one (HA's singletons keep a single entry) is cleared only when it holds the instance."""
    if not cache.cache_info().currsize:
        return

    def mine(key):  # The cache's key: its call's arguments.
        return type(key) is tuple and any(part is hass for part in key)
    held = gc.get_referents(cache)  # The cache's own parts: its table and, when bounded, its keys and results.
    if cache.cache_parameters()["maxsize"] is None:
        tables = [part for part in held if type(part) is dict and part is not cache.__dict__]
        if len(tables) == 1:
            for key in [key for key in list(tables[0]) if mine(key)]:
                tables[0].pop(key, None)
            return
        held = [key for table in tables for key in list(table)]
    if any(mine(key) for key in held):
        cache.cache_clear()


class _SandboxLogs:
    """A log filter: records from a sandbox's thread go to that sandbox, not to Home Assistant's own log."""

    def filter(self, record):
        if record.threadName != THREAD_NAME:
            return True
        for sandbox in list(_LOGGING):
            if sandbox.thread is not None and sandbox.thread.ident == record.thread:
                if len(sandbox.log) < SANDBOX_LOG_LIMIT:
                    sandbox.log.append(f"{record.levelname} {record.name}: {record.getMessage()}"[:500])
                break
        return False


_LOG_FILTER = _SandboxLogs()
_LOGGING = set()  # Sandboxes whose thread hasn't ended: what they log is kept out of HA's log.
_FILTERED = []  # Root handlers the filter was put on; it comes off them when the last sandbox thread ends.
_LOGGING_LOCK = threading.Lock()  # Sandboxes start on HA's loop and end on their own threads.


def _sandbox_started(sandbox):
    """On HA's loop, as a sandbox starts: its thread's records are kept from HA's log handlers, and dt_util.now can
    read a recorded time."""
    with _LOGGING_LOCK:
        _LOGGING.add(sandbox)
        for handler in logging.getLogger().handlers:
            if _LOG_FILTER not in handler.filters:
                handler.addFilter(_LOG_FILTER)
                _FILTERED.append(handler)
        _recorded_clocks(True)


def _thread_ended(sandbox):
    """Last on a sandbox's own thread: after the last one, nothing of the sandboxes stays on HA's log handlers or
    clock. Never left to HA's loop, which may be gone or never run again by then (stopping)."""
    with _LOGGING_LOCK:
        _LOGGING.discard(sandbox)
        if not _LOGGING:
            for handler in _FILTERED:
                handler.removeFilter(_LOG_FILTER)
            _FILTERED.clear()
            _recorded_clocks(False)


_AS_RECORDED = ContextVar("assist_improver_as_recorded", default=None)  # The recorded time, where Assist reads it.
_NOW = dt_util.now  # Home Assistant's own.


def _now(time_zone=None):
    """dt_util.now while sandboxes run: the recorded turn's time where Assist reads it in a sandbox (_as_recorded),
    Home Assistant's own everywhere else."""
    at = _AS_RECORDED.get()
    return _NOW(time_zone) if at is None else at.astimezone(time_zone or dt_util.get_default_time_zone())


@contextmanager
def _as_recorded(hass):
    """In a sandbox's loop: what runs inside reads the time of the recorded turn the sandbox is at."""
    token = _AS_RECORDED.set(hass.data[SANDBOX_KEY].at)
    try:
        yield
    finally:
        _AS_RECORDED.reset(token)


def _run_at(hass, source):
    """In a sandbox's loop: the recorded turn it's now at is the one of this snapshot (its at)."""
    if source.get("at"):
        hass.data[SANDBOX_KEY].at = datetime.fromisoformat(source["at"]).astimezone(dt_util.UTC)


def _recorded_clocks(running):
    """While sandboxes run, dt_util.now can read a recorded time (only inside _as_recorded); after the last, it's HA's
    own again."""
    dt_util.now = _now if running else _NOW


STATS = {"started": 0, "start_seconds": 0.0, "slowest_start": 0.0, "stopped": 0, "stop_seconds": 0.0,
         "slowest_stop": 0.0, "teardown_seconds": 0.0, "slowest_teardown": 0.0, "assistants": 0,
         "assistant_seconds": 0.0, "slowest_assistant": 0.0}  # Diagnostics: where time goes.


def _timed(kind, seconds):
    STATS[f"{kind}_seconds"] = round(STATS[f"{kind}_seconds"] + seconds, 3)
    STATS[f"slowest_{kind}"] = round(max(STATS[f"slowest_{kind}"], seconds), 3)


async def stop_all():
    for sandbox in list(RUNNING):
        await sandbox.stop()


class _NoWebServer:
    """hass.http for an instance with no web server: integrations' views and static paths go nowhere."""

    def __getattr__(self, name):
        async def ignored_async(*args, **kwargs):
            return None
        return ignored_async if name.startswith("async_") else (lambda *args, **kwargs: None)


class Sandbox:
    """Start with await Sandbox.start(...); run coroutines in it with await sandbox.run(fn, *args)."""

    def __init__(self, time_zone):
        self.time_zone = time_zone
        self.hass = self.loop = self.thread = None
        self.commands, self.unsimulated, self.transitions, self.changes = [], [], [], []
        self.flags = []  # Each command with no emulation, with what it asked: as sandbox mode flags it.
        # Asks the real home a read (a future of its answer): sandbox mode's copies only (seed's reads), never a replay.
        self.reader = None
        self.spoken = []  # Each message spoken on devices, by whatever called it (_speak).
        self.forecasts = {}  # Sandbox mode: the home's weather forecasts, as it gave them (_hold_forecasts).
        # Each conversation's id here, by the id it has outside: Home Assistant starts a conversation it doesn't know
        # under an id of its own, as at home (run_turn).
        self.conversations = {}
        self.moving = set()  # Devices the recording shows reporting their movement (simulation.reports_movement).
        self.observing = False  # True while recorded changes are applied, which aren't the replay's own.
        self.at = None  # The time of the recorded turn it's at, which Assist reads (_as_recorded).
        self.log = []  # What HA logged inside the sandbox; kept here, out of the real instance's log.
        self.device_ids = {}  # Recorded device ID -> its ID in the sandbox.
        self._dir = None  # Made and removed on the sandbox's own thread, never blocking HA's loop.
        self.teardown_seconds = 0.0  # Dropping its entries from HA's process-wide caches, on its own thread.
        self.frame_real = None  # The instance HA's frame helper had before this sandbox started: the real one.

    @classmethod
    @span("sandbox.start")
    async def start(cls, time_zone):
        began = time.monotonic()
        sandbox = cls(time_zone)
        ready = asyncio.get_running_loop().create_future()
        outer = asyncio.get_running_loop()

        def run():
            from homeassistant import runner
            try:
                sandbox._dir = tempfile.TemporaryDirectory(prefix="assist-improver-sandbox-")
                loop = sandbox.loop = runner.create_event_loop()  # HA's own loop, with its executor.
                asyncio.set_event_loop(loop)
                try:
                    try:
                        loop.run_until_complete(sandbox._setup())
                    except BaseException as err:  # noqa: BLE001 - reported to the caller, which stops the replay.
                        outer.call_soon_threadsafe(ready.set_exception, err)
                        return
                    outer.call_soon_threadsafe(ready.set_result, None)
                    loop.run_forever()
                finally:  # As HA's runner ends a loop: cancel tasks, then shut its threads down.
                    try:
                        runner._cancel_all_tasks_with_timeout(loop, runner.TASK_CANCELATION_TIMEOUT)
                        loop.run_until_complete(loop.shutdown_asyncgens())
                        loop.run_until_complete(loop.shutdown_default_executor())
                    finally:
                        asyncio.set_event_loop(None)
                        loop.close()
                        sandbox._dir.cleanup()
            finally:
                sandbox._end()

        _sandbox_started(sandbox)
        sandbox.thread = threading.Thread(target=run, name=THREAD_NAME, daemon=True)
        sandbox.thread.start()
        RUNNING.add(sandbox)
        try:
            await ready
        except BaseException:
            if ready.cancelled():  # The caller gave up while it started.
                await sandbox.stop()
            else:  # It failed to start: its thread ends its loop and tears down by itself.
                RUNNING.discard(sandbox)
                await asyncio.get_running_loop().run_in_executor(None, sandbox.thread.join, 30)
            raise
        STATS["started"] += 1
        _timed("start", time.monotonic() - began)
        _LOGGER.debug("Replay sandbox started in %.2f s", time.monotonic() - began)
        return sandbox

    async def run(self, fn, *args):
        """Run fn(hass, *args) in the sandbox's loop and wait for its result. An HA error gets its message there: HA
        translates it from its own loop and keeps it, so it reads anywhere after."""
        async def inside():
            try:
                return await fn(self.hass, *args)
            except HomeAssistantError as err:
                with suppress(Exception):
                    str(err)
                raise
        return await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(inside(), self.loop))

    async def stop(self):
        RUNNING.discard(self)
        if self.loop is None or self.loop.is_closed():
            return
        async with span("sandbox.stop"):
            with suppress(Exception):
                await self.run(lambda hass: hass.async_stop(force=True))
            began = time.monotonic()
            self.loop.call_soon_threadsafe(self.loop.stop)
            # Its thread ends its loop, then drops its cache entries (_end); HA's loop only waits.
            async with span("sandbox.teardown"):
                await asyncio.get_running_loop().run_in_executor(None, self.thread.join, 30)
            _timed("teardown", self.teardown_seconds)
            self.hass = None
            STATS["stopped"] += 1
            _timed("stop", time.monotonic() - began)
        _LOGGER.debug("Replay sandbox stopped in %.2f s (dropping its cache entries %.3f s, on its own thread)",
                      time.monotonic() - began, self.teardown_seconds)

    def _end(self):
        """Last on the sandbox's thread, its loop closed (also after a failed start): drop its entries from HA's
        process-wide caches, then the thread is done (_thread_ended)."""
        from homeassistant.helpers import frame
        try:
            if self.hass is not None:
                began = time.monotonic()
                if frame._hass.hass is self.hass:  # Ended inside HA's start, before giving the frame helper back.
                    frame._hass.hass = self.frame_real
                _forget(self.hass)
                self.teardown_seconds = time.monotonic() - began
        finally:
            _thread_ended(self)

    async def _setup(self):
        from homeassistant import bootstrap, config_entries, loader
        from homeassistant.core import HomeAssistant
        from homeassistant.helpers import frame
        from homeassistant.setup import async_setup_component
        from homeassistant.util.async_ import create_eager_task
        hass = self.hass = HomeAssistant(self._dir.name)  # This thread's instance only.
        hass.data[SANDBOX_KEY] = self
        # The sandbox's own config only: HA's async_set_time_zone also sets the process-wide default time zone, which
        # would carry over to the real instance. The recording is from the same home, so the default is its zone.
        # In the worker's own process the default is set too, as the real instance has it (every sandbox there is the
        # same home's): templates read it.
        if (zone := await dt_util.async_get_time_zone(self.time_zone)) is None:
            raise ValueError(f"Received invalid time zone {self.time_zone}")
        hass.config.time_zone = self.time_zone
        if OWN_PROCESS:
            dt_util.set_default_time_zone(zone)
        loader.async_setup(hass)
        hass.config_entries = config_entries.ConfigEntries(hass, {})
        # Its entries are held, never saved: an assistant's provider entry holds the household's key (load_assistant).
        hass.config_entries._async_schedule_save = lambda: None
        real = frame._hass.hass
        while real is not None and SANDBOX_KEY in real.data:  # Another sandbox is starting: the one it gives back.
            real = real.data[SANDBOX_KEY].frame_real
        self.frame_real = real
        # HA's own start (registries, triggers, ...) first points the process-wide frame helper at this instance, in
        # its synchronous part. Started eagerly, that part has run when the task is made: the helper goes back to the
        # real instance then, before the start's first await, instead of for the whole start.
        try:
            load = create_eager_task(bootstrap.async_load_base_functionality(hass))
        finally:
            frame._hass.hass = real
        if not await load:
            raise RuntimeError("The sandbox could not load Home Assistant's base functionality")
        # Intents register a web view; the sandbox has no web server, so their views go nowhere.
        hass.http = _NoWebServer()
        hass.config.components.add("http")
        for component in ("homeassistant", "intent", "llm"):
            if not await async_setup_component(hass, component, {}):
                raise RuntimeError(f"The sandbox could not set up {component}")
        self.emulate_devices()

    def emulate_devices(self, domains=()):
        """Device commands go to the emulated drivers, also after an integration registers its own services; so does
        every other command an integration has for the devices of these domains (one that answers nothing), which has
        no emulation and is flagged, rather than acting on no device."""
        registered = self.hass.services.async_services_internal()
        for domain in {*DEVICE_SERVICES, *domains}:
            others = {s for s, handler in registered.get(domain, {}).items()
                      if handler.supports_response is SupportsResponse.NONE} if domain in domains else set()
            for service in {*DEVICE_SERVICES.get(domain, ()), *others}:
                self.hass.services.async_register(domain, service, self._device_command)

    @callback
    def state_changed(self, event):
        from .serialization import plain
        old, new = event.data.get("old_state"), event.data.get("new_state")
        if self.observing or old is None or new is None or new.domain in (
                "conversation", "ai_task", "tts", "stt", "wake_word", "assist_satellite"):
            return
        if old.state != new.state:
            self.transitions.append({"entity_id": new.entity_id, "old": old.state, "new": new.state})
        if old.state != new.state or old.attributes != new.attributes:  # With its attributes, as sandbox mode keeps it.
            self.changes.append({"entity_id": new.entity_id, "old": {"state": old.state,
                                                                     "attributes": plain(dict(old.attributes))},
                                 "new": {"state": new.state, "attributes": plain(dict(new.attributes))}})

    def stand_in_missing(self, scripts):
        """The actions the household's scripts name that the sandbox has no service for (a phone's notification, a
        scene it doesn't load): flagged when a script reaches one, and the script runs on, as it would at home."""
        for name in _actions(scripts or {}):
            domain, _, service = name.partition(".")
            if service and not self.hass.services.has_service(domain, service):
                self.hass.services.async_register(domain, service, self._no_service)

    async def _no_service(self, call):
        from .serialization import plain
        self.unsimulated.append(f"{call.domain}.{call.service}")
        self.flags.append({"service": f"{call.domain}.{call.service}", "args": plain(dict(call.data))})

    async def _device_command(self, call):
        """A device's driver, emulated: apply the command's effect to the sandbox's state."""
        from homeassistant.helpers.service import async_extract_entity_ids
        from .serialization import plain
        data = {k: v for k, v in call.data.items() if k not in ("entity_id", "device_id", "area_id", "floor_id",
                                                                "label_id")}
        for entity_id in sorted(await async_extract_entity_ids(call)):
            if entity_id.split(".", 1)[0] != call.domain:
                continue
            state = self.hass.states.get(entity_id)
            self.commands.append({"entity_id": entity_id, "service": f"{call.domain}.{call.service}", "data": dict(data)})
            result = devices.effect(call.domain, call.service, state, data) if state is not None else None
            if result is None or result[0] is None:
                self.unsimulated.append(f"{call.domain}.{call.service} {entity_id}")
                self.flags.append({"service": f"{call.domain}.{call.service}",
                                   "args": plain({"entity_id": entity_id, **data})})
                continue
            # A device that reports its movement reports it on its way, as it did when recorded, then gets there
            # right after (its travel time isn't emulated), both under the command, as HA's own device reports.
            between = devices.underway(call.domain, call.service, state, *result) if entity_id in self.moving else None
            self.hass.states.async_set(entity_id, between or result[0], result[1], context=call.context)
            if between is not None:
                self.hass.async_create_task(self._arrive(entity_id, between, result[0], call.context),
                                            "assist_improver_sandbox_arrive", eager_start=False)

    async def _arrive(self, entity_id, between, end, context):
        """A device on its way gets there, unless it's been sent elsewhere since."""
        if (state := self.hass.states.get(entity_id)) is not None and state.state == between:
            self.hass.states.async_set(entity_id, end, dict(state.attributes), context=context)


# Tool prefixes that aren't an integration to set up: HA's core sets these up, and the improver's own (its
# file_improvement tool) is no part of the home: a sandbox serves the improver's llm platform itself, for the
# household's lines (household.serve) and a filing tool that files nothing (seed's filing), and the worker never runs
# the integration's setup.
INTEGRATION_TOOLS = {"intent", "homeassistant", "llm", "assist_improver"}


def seed_options(turn):
    """What a recorded request's tools say the sandbox needs: their integrations, a device that runs timers, and
    whether Assist could file the conversation."""
    from .llm import FileImprovement
    prepared = turn["prepared"]
    names = [t["name"] for t in prepared["tools"]]
    return {"integrations": sorted({n.split("__", 1)[0] for n in names if "__" in n} - INTEGRATION_TOOLS),
            "timer_device": prepared["context"].get("device_id") if any(n.endswith("StartTimer") for n in names)
            else None, "filing": FileImprovement.name in names}


async def seed(hass, source, *, user_name=None, integrations=(), timer_device=None, filing=False, scripts=None,
               automations=None, exposure_changes=None, alias_changes=None, alias_removals=None, moving=(), lists=None,
               timers=(), reads=False, calendars=None, forecasts=None):
    """Make the sandbox the recorded home: its areas, devices, entities (names, aliases, areas, a sensor's display
    precision), states and what Assist could see; the integrations whose Assist tools the request had; then its
    scripts and automations, which create their own entities. exposure_changes ({entity_id: exposed}) and
    alias_changes ({entity_id: [aliases added]}, alias_removals {entity_id: [aliases removed]}) apply on top of the
    recorded setup. moving: the devices that report their movement (simulation.reports_movement). lists: to-do lists'
    items ({entity_id: [item]}, as todo.get_items gives them), each held in the sandbox as a list of its own that
    Home Assistant's list intents and reads act on (a recording keeps none). timers: the home's running timers
    ({device_id, name, seconds_left, language, is_active}), running here too and ringing nowhere. Runs in the
    sandbox's loop. filing: Assist had the improver's filing tool, which answers here as filing does and files
    nothing (llm.ReplayedFiling). Sandbox mode's copy: reads, the lookups it can't hold are the real home's
    (_read_through); calendars ({entity_id: events}) and forecasts ({entity_id: {kind: forecast}}), what it holds of
    the home's calendars and weather (_seed_calendars, _hold_forecasts)."""
    from homeassistant.components.homeassistant.exposed_entities import async_expose_entity
    from homeassistant.components.intent import async_register_timer_handler
    from homeassistant.core import CoreState
    from homeassistant.helpers import area_registry as ar, entity_registry as er
    from homeassistant.setup import async_setup_component
    _run_at(hass, source)
    hass.config.location_name = source.get("home_name") or hass.config.location_name
    # HA's chat log names the signed-in person; the sandbox has no accounts, only the recorded person.
    hass.auth = SimpleNamespace(async_get_user=_user(user_name))
    # Scripts and automations create their own entities once loaded; everything else is seeded here.
    own = {domain for domain, loaded in (("script", scripts), ("automation", automations)) if loaded}
    hass.data[SANDBOX_KEY].moving = set(moving)
    if filing:
        from .const import DOMAIN
        from .llm import REPLAYED_FILING
        hass.data[REPLAYED_FILING] = True
        hass.config.components.add(DOMAIN)  # Home Assistant's Assist asks the platforms of loaded integrations.
    for integration in integrations:  # Their Assist tools (lights, media, timers, ...), without their devices.
        if integration in own:
            continue  # Set up below with the household's own; set up empty first, it would never load them.
        if not await async_setup_component(hass, integration, {}):
            raise RuntimeError(f"The sandbox could not set up {integration}")
    # The devices seeded as states below: their integrations' commands reach the emulated drivers. Lists, held
    # calendars, scripts and automations are entities of their own here.
    hass.data[SANDBOX_KEY].emulate_devices({e.split(".", 1)[0] for e in source.get("states") or {}} - own - {"todo"}
                                           - ({"calendar"} if calendars is not None else set()))
    if reads:  # Sandbox mode: what the copy can't hold, the real home answers.
        _read_through(hass, {e.split(".", 1)[0] for e in source.get("states") or {}} - own - {"todo"})
    if "script" not in own and any(e.startswith("script.") for e in source.get("states") or {}):
        # Scripts the sandbox wasn't given (their configuration unread) never run: a command to one is one the
        # emulated home has no model for, so it's flagged like any other.
        for service in ("turn_on", "turn_off", "toggle"):
            hass.services.async_register("script", service, hass.data[SANDBOX_KEY]._device_command)
    areas = {area_id: ar.async_get(hass).async_create(area["name"], aliases=set(area.get("aliases") or [])).id
             for area_id, area in (source.get("areas") or {}).items()}
    devices = hass.data[SANDBOX_KEY].device_ids = _seed_devices(hass, source.get("devices") or {}, areas)
    if timer_device:  # The recorded device could run timers (its tools offered them), in the registry or not.
        async_register_timer_handler(hass, devices.get(timer_device, timer_device), lambda *args: None)
    _hold_delayed_commands(hass)
    _copy_timers(hass, timers, devices)
    registry = er.async_get(hass)
    lists, calendars = lists or {}, calendars or {}
    # Entities loaded here already by their own integration (the household's assistants, a spare's): as they are.
    held = {e for e in source.get("entities") or {} if getattr(registry.async_get(e), "config_entry_id", None)}
    for entity_id, entry in (source.get("entities") or {}).items():
        if entry.get("disabled_by") or entity_id not in source["states"] or entity_id.split(".", 1)[0] in own or (
                entity_id in held):
            continue
        domain, object_id = entity_id.split(".", 1)
        # A list or calendar the sandbox holds is its integration's own here (_seed_lists, _seed_calendars).
        registry.async_get_or_create(domain, "todo" if entity_id in lists else "calendar" if entity_id in calendars
                                     else entry.get("platform") or "recorded",
                                     entity_id, suggested_object_id=object_id, supported_features=(
                                         source["states"][entity_id].get("attributes") or {}).get(
                                         "supported_features") or 0)  # As the device reports them.
    for entity_id in [*lists, *calendars]:  # Its id here too where the home's registry has no entry for it.
        registry.async_get_or_create(entity_id.split(".", 1)[0], entity_id.split(".", 1)[0], entity_id,
                                     suggested_object_id=entity_id.split(".", 1)[1])
    for entity_id, state in source["states"].items():
        if entity_id.split(".", 1)[0] not in own and entity_id not in {*lists, *calendars, *held}:
            hass.states.async_set(entity_id, state["state"], state.get("attributes") or {})
    if lists:
        await _seed_lists(hass, source, lists)
    if calendars:
        await _seed_calendars(hass, source, calendars)
    if forecasts is not None:
        _hold_forecasts(hass, forecasts)
    _stand_in_speakers(hass)
    _emulate_speaking(hass)
    # Running, without firing HA's start events: automations are live, but none triggers on a start. They load
    # after the states are seeded, so seeding triggers none either.
    hass.set_state(CoreState.running)
    if scripts and not await async_setup_component(hass, "script", {"script": scripts}):
        raise RuntimeError("The sandbox could not load the scripts")
    hass.data[SANDBOX_KEY].stand_in_missing(scripts)
    if automations and not await async_setup_component(hass, "automation", {"automation": automations}):
        raise RuntimeError("The sandbox could not load the automations")
    await hass.async_block_till_done()
    for entity_id, entry in (source.get("entities") or {}).items():
        if registry.async_get(entity_id) is not None:
            registry.async_update_entity(
                entity_id, name=entry.get("name"), original_name=entry.get("original_name"), aliases=_aliases(entry),
                area_id=areas.get(entry.get("area_id")), device_id=devices.get(entry.get("device_id")),
                hidden_by=er.RegistryEntryHider(entry["hidden_by"]) if entry.get("hidden_by") else None)
            if entry.get("sensor_options"):  # The display precision HA's live context rounds a sensor's state to.
                registry.async_update_entity_options(entity_id, "sensor", entry["sensor_options"])
    for entity_id in {*(alias_changes or {}), *(alias_removals or {})}:
        if (entry := registry.async_get(entity_id)) is None:
            raise RuntimeError(f"{entity_id} isn't in the sandbox")
        removed = set((alias_removals or {}).get(entity_id, []))
        registry.async_update_entity(entity_id, aliases=[*(a for a in entry.aliases if a not in removed),
                                                         *(alias_changes or {}).get(entity_id, [])])
    for entity_id in exposure_changes or {}:  # A change to something HA couldn't load can't be tested.
        if (state := hass.states.get(entity_id)) is None or state.state == "unavailable":
            raise RuntimeError(f"{entity_id} could not be loaded")
    exposure = {**(source.get("exposure") or {}), **(exposure_changes or {})}
    for entity_id, exposed in exposure.items():
        if exposed is not None and hass.states.get(entity_id) is not None:
            async_expose_entity(hass, "conversation", entity_id, bool(exposed))
    sandbox = hass.data[SANDBOX_KEY]
    hass.bus.async_listen("state_changed", sandbox.state_changed)
    await hass.async_block_till_done()


def _actions(config):
    """The actions (domain.service) a script or automation configuration names, its nested sequences' too; one named
    by a template can't be known."""
    if isinstance(config, dict):
        for key, value in config.items():
            if key in ("action", "service") and isinstance(value, str) and "{" not in value:
                yield value
            else:
                yield from _actions(value)
    elif isinstance(config, list):
        for value in config:
            yield from _actions(value)


async def load_assistant(hass, entry, agent):
    """The household's assistant, as Home Assistant loads it at home: its provider's config entry (entry: HA's storage
    form, ConfigEntry.as_dict(), with the conversation subentries it uses) set up by the provider's own integration,
    and its conversation agents at the home's entity ids (agent: one registry entry's entity_id, unique_id, platform
    and config_subentry_id, or a list of them). Nothing is installed: the provider's package is the real instance's.
    The entry is held in memory, never saved (Sandbox._setup). An entry loaded already stays as it is if the home
    still has it so (a spare's, loaded before the conversation); changed since (a setting, an assistant added), it's
    loaded afresh, as Home Assistant reloads an entry. The (first) agent's entity id."""
    from datetime import datetime
    from types import MappingProxyType
    from homeassistant.config_entries import ConfigEntry, ConfigEntryDisabler
    from homeassistant.helpers import entity_registry as er
    from homeassistant.helpers.entity_component import DATA_INSTANCES
    from homeassistant.util.enum import try_parse_enum
    began = time.monotonic()
    hass.config.skip_pip = True
    agents_given = agent if isinstance(agent, list) else [agent]
    registry, entity_id = er.async_get(hass), agents_given[0]["entity_id"]
    if (current := hass.config_entries.async_get_entry(entry["entry_id"])) is not None:
        from homeassistant.config_entries import ConfigEntryState
        have = {s.subentry_id: dict(s.data) for s in current.subentries.values()}
        agents = hass.data.get(DATA_INSTANCES, {}).get("conversation")
        if current.state is ConfigEntryState.LOADED and agents is not None and all(
                agents.get_entity(a["entity_id"]) is not None for a in agents_given) and (
                dict(current.data), dict(current.options)) == (entry["data"], entry["options"]) and all(
                have.get(s["subentry_id"]) == s["data"] for s in entry["subentries"]):
            return entity_id
        await hass.config_entries.async_remove(current.entry_id)
    for agent in agents_given:
        # The home as recorded holds the agent too, seeded as a state of its own: it gives way to the agent itself.
        if registry.async_get(agent["entity_id"]) is not None:
            registry.async_remove(agent["entity_id"])
        hass.states.async_remove(agent["entity_id"])
    loaded = ConfigEntry(  # As Home Assistant builds an entry from its storage.
        created_at=datetime.fromisoformat(entry["created_at"]), data=entry["data"],
        disabled_by=try_parse_enum(ConfigEntryDisabler, entry["disabled_by"]), discovery_keys=MappingProxyType({}),
        domain=entry["domain"], entry_id=entry["entry_id"], minor_version=entry["minor_version"],
        modified_at=datetime.fromisoformat(entry["modified_at"]), options=entry["options"],
        pref_disable_new_entities=False,  # The agent isn't new here: it's the home's own.
        pref_disable_polling=entry["pref_disable_polling"], source=entry["source"],
        subentries_data=entry["subentries"], title=entry["title"], unique_id=entry["unique_id"],
        version=entry["version"])
    hass.config_entries._entries[loaded.entry_id] = loaded  # As HA's own test helpers add one.
    _stand_in_apis(hass, entry["subentries"])
    for agent in agents_given:
        made = registry.async_get_or_create("conversation", agent["platform"], agent["unique_id"],
                                            suggested_object_id=agent["entity_id"].split(".", 1)[1],
                                            config_entry=loaded, config_subentry_id=agent["config_subentry_id"])
        if made.entity_id != agent["entity_id"]:
            raise RuntimeError(f"The sandbox gave the assistant {made.entity_id}, not {agent['entity_id']}")
    set_up = await hass.config_entries.async_setup(loaded.entry_id)
    await hass.async_block_till_done()
    agents = hass.data.get(DATA_INSTANCES, {}).get("conversation")
    if not set_up or agents is None or any(agents.get_entity(a["entity_id"]) is None for a in agents_given):
        from homeassistant.config_entries import ConfigEntryState
        from homeassistant.loader import IntegrationNotFound, async_get_integration
        from .cases import CaseError
        state = loaded.state
        await hass.config_entries.async_remove(loaded.entry_id)  # No retries against the provider meanwhile.
        why = " | ".join([f"The sandbox could not set up the assistant's {loaded.domain} entry ({state.value})",
                          *hass.data[SANDBOX_KEY].log[-3:]])
        try:
            built_in = (await async_get_integration(hass, loaded.domain)).is_built_in
        except IntegrationNotFound:
            built_in = False
        if not built_in and state is not ConfigEntryState.SETUP_RETRY:  # A custom provider that can't load here.
            raise CaseError(why + " | so it can't be replayed")
        raise RuntimeError(why)
    STATS["assistants"] += 1
    _timed("assistant", time.monotonic() - began)
    return entity_id


# The lookups sandbox mode's copy asks the real home (_read_through): each a read of a device that the copy can't
# hold (a media library; calendars and forecasts it holds: _seed_calendars, _hold_forecasts), on Home Assistant's
# query path (SupportsResponse.ONLY). A service marked so that acts (a satellite asking a question, an AI task) is
# never one.
READS = frozenset({("media_player", "search_media"), ("media_player", "browse_media")})


def _read_through(hass, domains):
    """Sandbox mode's copy: a lookup its devices' integrations answer only by reading the device (READS: a media
    library's search), which the copy can't hold, is asked of the real home (Sandbox.reader): its own handler answers
    there, announced nowhere."""
    sandbox = hass.data[SANDBOX_KEY]
    registered = hass.services.async_services_internal()
    for domain in domains:
        for service, handler in list(registered.get(domain, {}).items()):
            if (domain, service) not in READS or handler.supports_response is not SupportsResponse.ONLY:
                continue

            async def read(call, _domain=domain, _service=service):
                from .serialization import plain
                if sandbox.reader is None:
                    raise HomeAssistantError("Sandbox mode can't read the real home here")
                return _as_answered(_domain, _service, await asyncio.wrap_future(sandbox.reader({
                    "domain": _domain, "service": _service, "data": plain(dict(call.data))})))
            hass.services.async_register(domain, service, read, handler.schema,
                                         supports_response=SupportsResponse.ONLY)


def _as_answered(domain, service, response):
    """A read's answer, as the real home's handler gave it: what crossed as dicts, Home Assistant's own media classes
    again where its own tools read them (a media search, a browse)."""
    from homeassistant.components.media_player import BrowseMedia, SearchMedia

    def media(item):
        return BrowseMedia(**{k: v for k, v in item.items() if k != "children"},
                           children=[media(child) for child in item.get("children") or ()] or None)
    if (domain, service) == ("media_player", "search_media"):
        return {e: SearchMedia(result=[media(i) for i in found["result"]]) for e, found in response.items()}
    if (domain, service) == ("media_player", "browse_media"):
        return {e: media(found) for e, found in response.items()}
    return response


def _stand_in_apis(hass, subentries):
    """The LLM APIs an assistant uses beside Assist that the sandbox doesn't have (another integration's, a remote
    server's): each stands in, with no tools until sandbox mode gives it the real one's (outside_tools), so the
    assistant runs with Assist's; a call to one of the others gets Home Assistant's own not-found (DETAILS P11.17)."""
    from homeassistant.helpers import llm
    have = {api.id for api in llm.async_get_apis(hass)}
    for subentry in subentries:
        named = subentry["data"].get("llm_hass_api") or []
        for api_id in [named] if isinstance(named, str) else named:
            if api_id not in have and api_id != "assist_improver":  # The household API is served on its own.
                llm.async_register_api(hass, _outside_api()[0](hass=hass, id=api_id, name=api_id))
                have.add(api_id)


async def outside_tools(hass, apis):
    """Sandbox mode: each stand-in API is named as the real one and offers its tools as the assistant has them at home
    (apis: {API id: {name, tools: [{name, description, integration, parameters: JSON schema}]}}), their parameters
    built as Home Assistant builds a remote server's (probatio.from_openapi). A call to one is reported done and
    flagged (_flagging), never run (DETAILS P11.15, P11.17)."""
    from homeassistant.helpers import llm
    for api in llm.async_get_apis(hass):
        if isinstance(api, _outside_api()[0]) and api.id in apis:
            api.name, api.definitions = apis[api.id]["name"], apis[api.id]["tools"]


@functools.cache
def _outside_api():
    """The stand-in API and its tools' class (Home Assistant's llm helpers load with the sandbox, not on import)."""
    from homeassistant.helpers import llm

    class Outside(llm.API):
        definitions = ()

        async def async_get_api_instance(self, llm_context):
            return llm.APIInstance(api=self, api_prompt="", llm_context=llm_context,
                                   tools=[StandIn(d) for d in self.definitions])

    class StandIn(llm.Tool):
        def __init__(self, definition):
            self.name, self.description = definition["name"], definition["description"]
            self.parameters = probatio.from_openapi(definition["parameters"])
            # The improver's stand-in, tagged as such: a tool Home Assistant can't place by its integration is
            # reported through its frame helper, which another sandbox starting in this process may be holding.
            self.integration = definition.get("integration") or "assist_improver"

        async def async_call(self, hass, tool_input, llm_context):
            return llm.ToolResult(data={"success": True})
    return Outside, StandIn


def _hold_delayed_commands(hass):
    """A timer's command for when it ends has no emulation (HA would hand it to the conversation agent that set it):
    flagged, and the timer runs without it."""
    from homeassistant.components.intent.const import TIMER_DATA
    if (manager := hass.data.get(TIMER_DATA)) is None:
        return
    start, sandbox = manager.start_timer, hass.data[SANDBOX_KEY]

    def start_timer(device_id, *args, conversation_command=None, conversation_agent_id=None, **kwargs):
        if conversation_command:
            sandbox.flags.append({"service": "delayed command", "args": {"command": conversation_command}})
            if device_id is None or not manager.is_timer_device(device_id):
                return None
        return start(device_id, *args, **kwargs)
    manager.start_timer = start_timer


def _copy_timers(hass, timers, devices):
    """The home's running timers, on its devices here, where they ring nowhere."""
    from homeassistant.components.intent import async_register_timer_handler
    from homeassistant.components.intent.const import TIMER_DATA
    if (manager := hass.data.get(TIMER_DATA)) is None:
        return
    for timer in timers or ():
        if (device := devices.get(timer["device_id"])) is None:
            continue
        if not manager.is_timer_device(device):
            async_register_timer_handler(hass, device, lambda *args: None)
        copied = manager.start_timer(device, None, None, timer["seconds_left"], language=timer["language"],
                                     name=timer["name"])
        if not timer["is_active"]:
            manager.pause_timer(copied)


def _stand_in_speakers(hass):
    """Home Assistant's relative volume intent turns a media player's volume through the device itself, not a command:
    each one seeded here has a stand-in its component hands out, whose volume goes to the emulated driver as the
    command it stands for."""
    from homeassistant.components.media_player.const import DATA_COMPONENT

    class Speaker:
        available = False  # Never an entity action's: those reach the emulated driver, or none (a media search).

        def __init__(self, entity_id):
            self.entity_id = entity_id

        @property
        def volume_level(self):
            return hass.states.get(self.entity_id).attributes.get("volume_level")

        async def _command(self, service, **data):
            await hass.services.async_call("media_player", service, {"entity_id": self.entity_id, **data},
                                           blocking=True)

        async def async_volume_up(self):
            await self._command("volume_up")

        async def async_volume_down(self):
            await self._command("volume_down")

        async def async_set_volume_level(self, volume):
            await self._command("volume_set", volume_level=volume)
    if (component := hass.data.get(DATA_COMPONENT)) is not None:
        for entity_id in hass.states.async_entity_ids("media_player"):
            component._entities.setdefault(entity_id, Speaker(entity_id))  # noqa: SLF001 - as its platform adds one.


# A message spoken on devices (DETAILS P9.10.5) leaves no state to emulate: each speaking service (analyzer.SPEAKING)
# is taken as done, and what it spoke where is logged, whatever called it (a broadcast, a script), for a replay to judge
# an action by. Nothing is heard on the copy, so sandbox mode flags each, as it flags a command with no emulation.
def _emulate_speaking(hass):
    """Registered after the integrations' own and the emulated drivers, before the household's scripts load."""
    from .analyzer import SPEAKING
    for name in SPEAKING:
        hass.services.async_register(*name.split("."), functools.partial(_speak, hass))


async def _speak(hass, call):
    """Log what a speaking call spoke: {service, message, targets}; tts.speak speaks with its engine on its media
    players, the others on the entities they target. As Home Assistant's entity services skip an entity that's missing
    or unavailable, nothing is spoken there. A media player's play_media that isn't a spoken message has no
    emulation."""
    from homeassistant.helpers.service import async_extract_entity_ids
    from .serialization import plain
    sandbox, data, service = hass.data[SANDBOX_KEY], dict(call.data), f"{call.domain}.{call.service}"
    here = lambda e: (state := hass.states.get(e)) is not None and state.state != "unavailable"  # noqa: E731
    targeted = sorted(e for e in await async_extract_entity_ids(call) if e.split(".", 1)[0] == call.domain and here(e))
    if call.domain == "tts":
        if not targeted:  # No engine to speak with.
            return
        players = data.get("media_player_entity_id") or []
        targets = sorted(p.strip() for p in (players.split(",") if isinstance(players, str) else players) if here(
            p.strip()))
        message = data.get("message")
    else:
        targets = targeted
        media = data.get("media_content_id") or (data.get("media") or {}).get("media_content_id") or data.get(
            "media_id")
        message = data.get("message") if call.service == "announce" else None
        message = message or _tts_message(media)
    if message is None:
        return await sandbox._device_command(call)  # noqa: SLF001 - the driver's, as for any other command.
    sandbox.spoken.append({"service": service, "message": str(message), "targets": targets})
    if call.domain == "tts":
        sandbox.flags.append({"service": service, "args": plain(data)})
    data = {k: v for k, v in data.items() if k not in ("entity_id", "device_id", "area_id", "floor_id", "label_id")}
    if call.domain != "tts":
        sandbox.flags += [{"service": service, "args": plain({"entity_id": e, **data})} for e in targets]
    sandbox.commands += [{"entity_id": e, "service": service, "data": data} for e in targets]  # A command to each.


def _tts_message(media):
    """The message a text-to-speech media source speaks (media-source://tts/<engine>?message=...), else None."""
    from yarl import URL
    if not isinstance(media, str) or not media.startswith("media-source://tts"):
        return None
    return URL(media).query.get("message")


async def _seed_lists(hass, source, lists):
    """Each to-do list, its items held here: Home Assistant's own to-do integration runs it as any list, under the
    registry entry seed made for it (so its names, aliases and area are the home's)."""
    from dataclasses import asdict, replace
    from itertools import count
    from homeassistant.components.todo import TodoItem, TodoListEntity
    from homeassistant.components.todo.entity import api_items_factory
    from homeassistant.components.todo.const import DATA_COMPONENT
    from homeassistant.setup import async_setup_component
    if not await async_setup_component(hass, "todo", {}):
        raise RuntimeError("The sandbox could not set up todo")
    made = count(1)  # An added item's uid, as a list gives one: in order, so a replay stays the same.

    class SandboxList(TodoListEntity):
        def __init__(self, entity_id, state, items):
            attributes = state.get("attributes") or {}
            self._attr_unique_id, self._attr_name = entity_id, attributes.get("friendly_name")
            self._attr_supported_features = attributes.get("supported_features", 7)
            fields = {"summary", "uid", "status", "due", "description", "completed"}
            self._attr_todo_items = [TodoItem(**{k: v for k, v in item.items() if k in fields}) for item in items]

        async def async_create_todo_item(self, item):
            self._attr_todo_items = [*self.todo_items, replace(item, uid=item.uid or f"sandbox-{next(made)}")]
            self.async_write_ha_state()

        async def async_update_todo_item(self, item):
            self._attr_todo_items = [item if item.uid and i.uid == item.uid else i for i in self.todo_items]
            self.async_write_ha_state()

        async def async_delete_todo_items(self, uids):
            self._attr_todo_items = [i for i in self.todo_items if i.uid not in uids]
            self.async_write_ha_state()

        def held(self):
            return [asdict(i, dict_factory=api_items_factory) for i in self.todo_items]

        def hold(self, items):
            fields = {"summary", "uid", "status", "due", "description", "completed"}
            self._attr_todo_items = [TodoItem(**{k: v for k, v in item.items() if k in fields}) for item in items]
            self.async_write_ha_state()
    states = source.get("states") or {}
    await hass.data[DATA_COMPONENT].async_add_entities([SandboxList(entity_id, states.get(entity_id) or {}, items)
                                                        for entity_id, items in lists.items()])


async def _seed_calendars(hass, source, calendars):
    """Each calendar, its events of the coming week held here as the home's calendar gave them (calendar.get_events):
    Home Assistant's own calendar integration runs it as any calendar, so its tools and services read it, and an event
    created in sandbox mode is created here. One written here stays as written (hold_stores)."""
    from homeassistant.components.calendar import CalendarEntity, CalendarEvent
    from homeassistant.components.calendar.const import DATA_COMPONENT, CalendarEventStatus
    from homeassistant.setup import async_setup_component
    if not await async_setup_component(hass, "calendar", {}):
        raise RuntimeError("The sandbox could not set up calendar")

    def event(given):
        start, end = (dt_util.parse_datetime(given[k]) if "T" in given[k] else datetime.fromisoformat(given[k]).date()
                      for k in ("start", "end"))
        return CalendarEvent(start=start, end=end, summary=given["summary"], description=given.get("description"),
                             location=given.get("location"),
                             status=CalendarEventStatus(given["status"]) if given.get("status") else None)

    class SandboxCalendar(CalendarEntity):
        # Its state moves only as the home's does (hold, each turn) or as the copy writes it: never by a poll or an
        # alarm of its own, which would read the turn's time and count time passing as the copy's doing.
        _attr_should_poll = False

        def __init__(self, entity_id, state, events):
            attributes = state.get("attributes") or {}
            self._attr_unique_id, self._attr_name = entity_id, attributes.get("friendly_name")
            self._attr_supported_features = attributes.get("supported_features") or 0
            self.written, self.events = False, sorted(map(event, events), key=lambda e: e.start_datetime_local)

        @property
        def event(self):  # The one going on, or the next.
            return next((e for e in self.events if e.end_datetime_local > dt_util.now()), None)

        async def async_get_events(self, hass, start_date, end_date):
            return [e for e in self.events if e.start_datetime_local < end_date and e.end_datetime_local > start_date]

        async def async_create_event(self, **kwargs):
            created = CalendarEvent(start=kwargs["dtstart"], end=kwargs["dtend"], summary=kwargs["summary"],
                                    description=kwargs.get("description"), location=kwargs.get("location"),
                                    rrule=kwargs.get("rrule"))
            self.written, self.events = True, sorted([*self.events, created], key=lambda e: e.start_datetime_local)
            self.async_write_ha_state()

        def hold(self, events):
            self.events = sorted(map(event, events), key=lambda e: e.start_datetime_local)
            self.async_write_ha_state()

        @callback
        def _async_write_ha_state(self):
            super()._async_write_ha_state()
            for unsub in self._alarm_unsubs or ():
                unsub()
            self._alarm_unsubs = []
    states = source.get("states") or {}
    await hass.data[DATA_COMPONENT].async_add_entities([SandboxCalendar(entity_id, states.get(entity_id) or {}, events)
                                                        for entity_id, events in calendars.items()])


def _hold_forecasts(hass, forecasts):
    """The weather's forecasts, each kind it gives, as the home's weather gave them (weather.get_forecasts): the copy
    holds its weather as states, so Home Assistant's forecast query answers with these here, as given (hold_stores
    renews them each turn); a kind the weather doesn't give is refused, as Home Assistant refuses it."""
    from homeassistant.const import ENTITY_MATCH_ALL
    from homeassistant.helpers import config_validation as cv
    from homeassistant.helpers.service import async_extract_entity_ids
    sandbox = hass.data[SANDBOX_KEY]
    sandbox.forecasts = dict(forecasts)

    async def get_forecasts(call):
        answer = {}
        named = set(sandbox.forecasts) if call.data.get("entity_id") == ENTITY_MATCH_ALL else {
            e for e in await async_extract_entity_ids(call) if e in sandbox.forecasts}
        for entity_id in sorted(named):
            if (forecast := sandbox.forecasts[entity_id].get(call.data["type"])) is None:
                raise HomeAssistantError(f"Weather entity '{entity_id}' does not support '{call.data['type']}' "
                                         "forecast")
            answer[entity_id] = {"forecast": forecast}
        return answer
    hass.services.async_register("weather", "get_forecasts", get_forecasts, cv.make_entity_service_schema(
        {probatio.Required("type"): probatio.In(("daily", "hourly", "twice_daily"))}),
                                 supports_response=SupportsResponse.ONLY)


async def hold_stores(hass, source, calendars, forecasts):
    """The home's calendars and forecasts as read now, each turn (sandbox mode), but a calendar the copy wrote: that
    one stays its own. A calendar the copy doesn't hold yet (unreadable before, or one Home Assistant loaded since) is
    taken over as seed takes one (source: the home's calendars' states, entities and areas, and its exposure); one
    not read now stays as it was."""
    from homeassistant.components.calendar.const import DATA_COMPONENT
    from homeassistant.components.homeassistant.exposed_entities import async_expose_entity
    sandbox, component, new = hass.data[SANDBOX_KEY], hass.data.get(DATA_COMPONENT), {}
    sandbox.observing = True  # The home's own changes, not the copy's.
    try:
        for entity_id, events in calendars.items():
            if (held := component.get_entity(entity_id) if component else None) is None:
                new[entity_id] = events
            elif not held.written:
                held.hold(events)
        if new:
            _take_over(hass, source, new, "calendar")
            await _seed_calendars(hass, source, new)
            for entity_id in new:
                async_expose_entity(hass, "conversation", entity_id, bool((source.get("exposure") or {}).get(
                    entity_id)))
        sandbox.forecasts |= forecasts
    finally:
        sandbox.observing = False


async def hold_lists(hass, source, lists, given, settle=True):
    """Each to-do list as the home has it now (lists: {entity_id: items}), but one the sandbox changed since it was
    last given it (given: the items it was given): that one stays its own. A list it doesn't hold yet (one Home
    Assistant loaded since) is added as seed adds one. settle: as observe's. What it now holds as given:
    [entity_id]."""
    from homeassistant.components.homeassistant.exposed_entities import async_expose_entity
    from homeassistant.components.todo.const import DATA_COMPONENT
    component, sandbox, taken, new = hass.data.get(DATA_COMPONENT), hass.data[SANDBOX_KEY], [], {}
    sandbox.observing = True  # The home's own changes, not the sandbox's.
    try:
        for entity_id, items in lists.items():
            if (held := component.get_entity(entity_id) if component else None) is None:
                new[entity_id] = items
            elif held.held() == given.get(entity_id):
                held.hold(items)
                taken.append(entity_id)
        if new:
            _take_over(hass, source, new, "todo")
            await _seed_lists(hass, source, new)
            for entity_id in new:
                async_expose_entity(hass, "conversation", entity_id, True)
            taken += list(new)
        if settle:
            await hass.async_block_till_done()
    finally:
        sandbox.observing = False
    return taken


def _take_over(hass, source, entity_ids, platform):
    """Entities seeded as states (or not at all) that the sandbox holds from now on, by an integration of its own here
    (platform): their ids, names, aliases and areas as the home has them."""
    from homeassistant.helpers import area_registry as ar, entity_registry as er
    registry, areas = er.async_get(hass), ar.async_get(hass)
    for entity_id in entity_ids:
        entry = (source.get("entities") or {}).get(entity_id) or {}
        area = ((source.get("areas") or {}).get(entry.get("area_id")) or {}).get("name")
        if (seeded := registry.async_get(entity_id)) is not None and seeded.platform != platform:
            registry.async_remove(entity_id)  # Seeded as a state: the held entity's id is its.
        hass.states.async_remove(entity_id)
        registry.async_get_or_create(entity_id.split(".", 1)[0], platform, entity_id,
                                     suggested_object_id=entity_id.split(".", 1)[1])
        if entry:  # One the home's registry has none for keeps its own name, as seeded.
            registry.async_update_entity(entity_id, name=entry.get("name"), aliases=_aliases(entry),
                                         area_id=getattr(areas.async_get_area_by_name(area or ""), "id", None))


def _aliases(entry):
    """The names as resolved when recorded."""
    return list(entry.get("aliases") or [])


def _seed_devices(hass, recorded, areas):
    """The recorded devices, under one recorded config entry (devices need one). {recorded id: sandbox id}"""
    from types import MappingProxyType
    from homeassistant.config_entries import ConfigEntry, ConfigEntryState
    from homeassistant.helpers import device_registry as dr
    entry = ConfigEntry(domain="assist_improver_recorded", title="Recorded home", data={}, source="user",
                        version=1, minor_version=1, options={}, unique_id=None, subentries_data=None,
                        discovery_keys=MappingProxyType({}), state=ConfigEntryState.LOADED)
    hass.config_entries._entries[entry.entry_id] = entry  # As HA's own test helpers add an entry; never set up.
    registry, out = dr.async_get(hass), {}
    for device_id, device in recorded.items():
        created = registry.async_get_or_create(config_entry_id=entry.entry_id, identifiers={("recorded", device_id)},
                                               name=device.get("name"), manufacturer=device.get("manufacturer"),
                                               model=device.get("model"))
        registry.async_update_device(created.id, name_by_user=device.get("name_by_user"),
                                     area_id=areas.get(device.get("area_id")))
        out[device_id] = created.id
    return out


def _user(name):
    async def get_user(user_id):  # The copy has nothing of the person's to keep from them.
        return SimpleNamespace(id=user_id, name=name, is_admin=True) if name else None
    return get_user


def sandbox_context(hass, llm_context):
    """The recorded request's context, with its device as the sandbox knows it."""
    from dataclasses import replace
    device_id = hass.data[SANDBOX_KEY].device_ids.get(llm_context.device_id, llm_context.device_id)
    return replace(llm_context, device_id=device_id)


async def system_prompt(hass, llm_context, api_id, prompt):
    """Assist's system prompt and tools, as HA builds them for this request in the sandbox, at the recorded turn's
    time."""
    from homeassistant.components.conversation.chat_log import ChatLog
    chat_log = ChatLog(hass, "assist-improver-sandbox-prompt")
    with _as_recorded(hass):
        await chat_log.async_provide_llm_data(sandbox_context(hass, llm_context), user_llm_hass_api=api_id,
                                              user_llm_prompt=prompt)
    return chat_log.content[0].content, chat_log.llm_api


MARKED = ("transitions", "commands", "changes", "flags", "spoken", "unsimulated")  # A turn's own lists (run_turn).


async def run_turn(hass, text, conversation_id, context, language, agent_id, device_id=None, watch=(), emit=None,
                   settle=True, since=None, extra_system_prompt=None):
    """One Assist turn as Home Assistant runs it at home, on the household's assistant loaded here (load_assistant),
    at the recorded turn's time: HA opens the chat session and log as its pipeline does, the assistant renders its
    prompt, builds its tools, asks its model and runs each tool call here, at once, as it comes. Its answer: what the
    turn added to the chat log after the person's words (each as HA gives it), what the assistant was given (its
    system prompt, its tools' names), the provider's usage as the assistant traces it, the watched entities'
    attributes before and after, and the home once everything the turn set off has run (snapshot): its changes,
    commands, flags and speech, and its commands with no emulation (unemulated), the turn's own, with where each
    list ends (marks). emit: given each delta of the reply as Home Assistant's chat log gets it (sandbox mode relays
    them to the person's own), and before them, once the assistant has it, what it was given ({given}: its prompt,
    API, instructions and tools' definitions, as the recorder keeps them). settle: seconds to wait at most for what
    the turn set off (sandbox mode: a script's delay doesn't hold the reply, as at home), True to wait for all of it;
    since: the marks the turn's own lists start from (sandbox mode: where the last turn's ended, so what ran on after
    it is this one's)."""
    from homeassistant.components import conversation
    from homeassistant.core import Context
    from homeassistant.helpers import chat_session
    from .serialization import plain
    sandbox, usage = hass.data[SANDBOX_KEY], []
    before = {e: dict(state.attributes) for e in watch if (state := hass.states.get(e)) is not None}
    marks, streamed = since or {k: len(getattr(sandbox, k)) for k in MARKED}, []

    def relayed(_chat_log, delta):  # In the chat log's own shape, its tool calls plain.
        streamed.append(delta.get("role"))
        emit(plain({k: [{"tool_name": c.tool_name, "tool_args": c.tool_args, "id": c.id, "external": c.external}
                        for c in v] if k == "tool_calls" else v for k, v in delta.items() if v is not None}))
    with _as_recorded(hass), chat_session.async_get_chat_session(
            hass, sandbox.conversations.get(conversation_id, conversation_id)) as session, \
            conversation.async_get_chat_log(hass, session, chat_log_delta_listener=relayed if emit else None) as \
            chat_log:
        sandbox.conversations[conversation_id] = session.conversation_id
        start = len(chat_log.content)
        traced, provide = chat_log.async_trace, chat_log.async_provide_llm_data

        async def provided(*args, **kwargs):  # Each tool call the assistant makes, flagged as _flagging says.
            await provide(*args, **kwargs)
            if (api := chat_log.llm_api) is not None:
                api.async_call_tool = functools.partial(_flagging, hass, api, api.async_call_tool)
            if emit:
                emit({"given": plain({
                    "prompt": chat_log.content[0].content, "extra_system_prompt": chat_log.extra_system_prompt,
                    "api_id": api.api.id if api else None, "api_prompt": api.api_prompt if api else None,
                    "tools": [{"name": t.name, "description": t.description, "integration": t.integration,
                               "parameters": probatio.to_openapi(t.parameters, custom_serializer=api.custom_serializer,
                                                                 openapi_version="3.1.0")} for t in api.tools]
                    if api else []})})
        chat_log.async_provide_llm_data = provided
        stream = chat_log.async_add_delta_content_stream

        def streaming(agent_id, deltas):  # Each of its streams a message of its own there too, as here.
            relayed(chat_log, {"role": "assistant"})
            return stream(agent_id, deltas)
        if emit:
            chat_log.async_add_delta_content_stream = streaming

        def trace(agent_details):  # As the assistant reports it, provider-neutral (absent: unknown, never zero).
            if isinstance(agent_details.get("stats"), dict):
                usage.append(plain(agent_details["stats"]))
            traced(agent_details)
        chat_log.async_trace = trace
        result = await conversation.async_converse(
            hass, text, session.conversation_id, Context(**context), language=language, agent_id=agent_id,
            extra_system_prompt=extra_system_prompt,
            device_id=sandbox.device_ids.get(device_id, device_id) if device_id else None)
        added = chat_log.content[start:]
        if added and added[0].role == "user":
            added = added[1:]
        if emit and not streamed:  # An assistant that adds its reply whole: relayed as its stream would have been.
            for content in added:
                relayed(chat_log, content.as_dict() if content.role == "tool_result" else {
                    "role": content.role, "content": content.content, "tool_calls": content.tool_calls,
                    "thinking_content": getattr(content, "thinking_content", None)})
        api = chat_log.llm_api
        given = {"prompt": chat_log.content[0].content if chat_log.content else None,
                 "tools": [t.name for t in api.tools] if api else []}
    if settle is not True:
        with suppress(TimeoutError):
            async with asyncio.timeout(settle):
                await hass.async_block_till_done()
    found = await snapshot(hass, watch, settle=settle is True)
    return {"content": plain([c.as_dict() for c in added]), "reply": plain(result.response.as_dict()),
            "given": given, "usage": usage, "before": plain(before), "attributes": plain(found["attributes"]),
            **{k: plain(found[k][marks[k]:]) for k in marks if k != "unsimulated"},
            "unemulated": found["unsimulated"][marks["unsimulated"]:], "marks": {k: len(found[k]) for k in MARKED}}


async def call_tool(hass, api, tool_input):
    """One of Assist's tools, called in the sandbox at the recorded turn's time: HA's date and time tool answers with
    it, and what the call sets off (a script, the automations its changes trigger) reads it; flagged as _flagging
    says."""
    with _as_recorded(hass):
        return await _flagging(hass, api, api.async_call_tool, tool_input)


async def _flagging(hass, api, call, tool_input):
    """A tool call (call: the API instance's own), with what has no emulation flagged: a stand-in for another API's
    tool (outside_tools), and an action Home Assistant reports done on a device the sandbox holds only as a state, with
    no command to it and nothing changed (an intent that acts on the device itself, such as a relative volume
    change). Each under the name the model called."""
    from homeassistant.helpers import llm
    from homeassistant.helpers.entity_component import DATA_INSTANCES
    sandbox = hass.data[SANDBOX_KEY]
    before = len(sandbox.commands), len(sandbox.changes)
    result = await call(tool_input)
    tool = next((t for t in api.tools if t.name == tool_input.tool_name), None)
    while isinstance(tool, llm.NamespacedTool):
        tool = tool.tool
    if isinstance(tool, _outside_api()[1]):
        sandbox.flags.append({"service": tool_input.tool_name, "args": dict(tool_input.tool_args)})
    done = result.data if isinstance(result.data, dict) and result.data.get("response_type") == "action_done" else {}
    if done and (len(sandbox.commands), len(sandbox.changes)) == before:
        components = hass.data.get(DATA_INSTANCES, {})
        for target in (done.get("data") or {}).get("success") or []:
            entity_id = target.get("id") if target.get("type") == "entity" else None
            component = components.get(entity_id.split(".", 1)[0]) if entity_id else None
            if entity_id and hass.states.get(entity_id) is not None and (
                    component is None or component.get_entity(entity_id) is None):
                sandbox.flags.append({"service": tool_input.tool_name,
                                      "args": {"entity_id": entity_id, **tool_input.tool_args}})
    return result


async def observe(hass, source, moves=(), settle=True):
    """Move on to the next recorded turn: its time, and what others in the home moved since, on top of what the replay
    did itself. moves: each skipped turn's then this one's, in order (simulation.moved_by_others). settle: wait for
    what the moves set off, as others' doing too; sandbox mode doesn't (what its copy is running, a script's delay,
    runs on as its own)."""
    _run_at(hass, source)
    sandbox = hass.data[SANDBOX_KEY]
    sandbox.observing = True
    try:
        for moved in moves:
            for entity_id, move in moved.items():
                if (current := hass.states.get(entity_id)) is None:
                    continue
                attributes = {k: v for k, v in current.attributes.items() if k not in move["removed"]}
                hass.states.async_set(entity_id, move.get("state", current.state), attributes | move["attributes"])
        if settle:
            await hass.async_block_till_done()
    finally:
        sandbox.observing = False


async def snapshot(hass, attributes=(), settle=True):
    """The sandbox's home now: states, the replay's state changes, its device commands and what it spoke; the
    attributes of the entities named (a light's brightness a goal asks for). settle: once what's running has finished
    (a script's delay too); otherwise as it is."""
    from copy import deepcopy
    sandbox = hass.data[SANDBOX_KEY]
    if settle:
        await hass.async_block_till_done()
    return {"states": {s.entity_id: s.state for s in hass.states.async_all()},
            "attributes": {e: dict(state.attributes) for e in attributes if (state := hass.states.get(e)) is not None},
            "transitions": deepcopy(sandbox.transitions), "commands": deepcopy(sandbox.commands),
            "unsimulated": list(sandbox.unsimulated), "changes": deepcopy(sandbox.changes),
            "flags": deepcopy(sandbox.flags), "spoken": deepcopy(sandbox.spoken)}
