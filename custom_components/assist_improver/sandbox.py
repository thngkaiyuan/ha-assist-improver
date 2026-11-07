"""A sandboxed Home Assistant instance: HA's own engine, acting on a simulated home.

A sandbox runs only in a replay worker, a short-lived process of its own (worker.py), never in Home Assistant's own
process; in tests, in the test's process (worker.INLINE). It is a separate HomeAssistant object in its own thread and
event loop, with the home's registries loaded whole from today's setup (start). It runs HA's own integrations (intents,
the Assist API, scripts, automations, scenes, the household's storage and YAML helpers, and the household's assistant
by its provider's integration, which calls the model from here) over a copy of the home; only the commands to devices
(the stand-in integration, standin.py) and calls beyond the home (outside) are stood in. It has no network listener and
no device integrations, so nothing in it can reach a real device. Its config folder is a throwaway one, gone with its
worker. The worker's process is the sandbox's, so its time zone is the home's, as the household's Home Assistant has
it.

The sandbox runs on one clock (clock.py, DETAILS P6.2.5): seeded as the home at a recorded turn's moment, it starts
then, and moves on to each later turn's recorded time before that turn (advance), timers due on the way firing at
their times. Inside a turn the clock runs at real speed, but while the turn's tool calls run and its result settles
(windows, snapshot), waits pass at once: a script's delay, a timed automation due before the next recorded turn.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager, suppress
from datetime import datetime
import functools
import json
import logging
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace

from homeassistant.const import EVENT_CALL_SERVICE, Platform
from homeassistant.core import SupportsResponse, callback
from homeassistant.exceptions import HomeAssistantError, ServiceNotSupported
from homeassistant.util import dt as dt_util
from homeassistant.util.hass_dict import HassKey

from . import clock, standin
from .const import DOMAIN
from .digest import kept
from .touchpoints import relied_on, resolve

_LOGGER = logging.getLogger(__name__)

SANDBOX_KEY: HassKey["Sandbox"] = HassKey(f"{DOMAIN}_sandbox")  # In the sandbox's own instance.
THREAD_NAME = "assist-improver-sandbox"
EXECUTOR_WORKERS = 64  # Threads for Home Assistant's blocking jobs, as its own loop has.
TEARDOWN_TIMEOUT = 5  # Seconds the tasks left when a sandbox ends get to finish cancelling, as Home Assistant gives them.
SANDBOX_LOG_LIMIT = 200
# Real seconds the gap before a recorded turn may take (advance): well inside a worker call's limit, and far more than
# a gap's waits need while they pass at once (an automation's whole week of runs every 30 s takes about 9).
GAP_SECONDS = 30
# Integrations a sandbox sets up for real (DETAILS P11.8), besides homeassistant, intent and llm (start), the storage
# and YAML helpers the home has (setup_now's), the domains of the stand-ins' entities, and the improver for its
# Assist tools (seed's filing).
CORE = ("conversation", "assist_pipeline", "sun", "script", "automation", "scene")
# Never set up in a sandbox, whatever the home or Assist's tools name: the Supervisor's integration, whose services act
# on the host, the cloud, the network and discovery, and the recorder (DETAILS P11.8.3).
NEVER = {"hassio", "cloud", "network", "recorder", "dhcp", "ssdp", "zeroconf", "usb", "bluetooth"}
# What of hass.config a sandbox takes from today's (Config.async_update's names): never its folders, allowlists or
# components.
CONFIG = ("latitude", "longitude", "elevation", "radius", "location_name", "currency", "country", "language")
MODELS = "ai_task"  # Each of its actions is a model call (_outside).


class _Log(logging.Handler):
    """What Home Assistant logs in a sandbox's thread, kept with the sandbox for the replay to report."""

    def __init__(self, sandbox):
        super().__init__()
        self.sandbox = sandbox

    def emit(self, record):
        if record.thread == self.sandbox.thread.ident and len(self.sandbox.log) < SANDBOX_LOG_LIMIT:
            self.sandbox.log.append(f"{record.levelname} {record.name}: {record.getMessage()}"[:500])


def _run_at(hass, source):
    """In a sandbox's loop: the recorded turn it's now at is the one of this snapshot (its at): a command that failed
    at home then fails here (standin.StandIn.async_request_call)."""
    if source.get("at"):
        hass.data[SANDBOX_KEY].at = _moment(source["at"])


def _moment(at):
    return datetime.fromisoformat(at).astimezone(dt_util.UTC) if at else None


def _epoch(at):
    """A recorded time (ISO), in seconds since the epoch; None for none."""
    return datetime.fromisoformat(at).timestamp() if at else None


@contextmanager
def _held(hass):
    """The replay's conversations held while its clock may move on between turns (advance, snapshot): they went on
    at home through the gap, so their chat sessions last (Home Assistant ends one idle for five minutes), and their
    chat logs carry on."""
    from homeassistant.helpers import chat_session
    with ExitStack() as held:
        for conversation_id in hass.data[SANDBOX_KEY].conversations.values():
            held.enter_context(chat_session.async_get_chat_session(hass, conversation_id, context=None))
        yield


async def advance(hass, at):
    """In a sandbox's loop: its clock on to a recorded time (ISO) if that's later, each timer due on the way firing at
    its time (clock.Window), what it sets off with it, the replay's conversations held (_held). Once the gap has taken
    GAP_SECONDS (its waits couldn't pass at once), the clock moves there anyway, and what's still due fires then."""
    if (moment := _epoch(at)) is not None and moment > hass.loop.clock.now():
        with _held(hass):
            await hass.loop.settle(moment, deadline=clock.REAL_MONOTONIC() + GAP_SECONDS)
            hass.loop.clock.move_to(moment)


@contextmanager
def _tool_windows(hass, conversation_id, cap):
    """In a sandbox's loop, while a turn runs: a window (clock.Window, never past cap) from each assistant message
    with tool calls Home Assistant runs being added to the conversation's chat log (its stream has ended) until the
    last of their results is (the next model call starts there)."""
    from .serialization import plain
    subscribe, events = resolve("chat_log_subscribe"), resolve("chat_log_events")
    open_ = {}  # "window" and the "calls" whose results it waits for, while one is open.

    def close():
        if open_:
            open_.pop("window").__exit__()
            open_.clear()

    def heard(chat, kind, data):
        if chat != conversation_id or kind != events.CONTENT_ADDED:
            return
        content = plain(data.get("content") or {})
        if content.get("role") == "assistant":
            if calls := {c["id"] for c in content.get("tool_calls") or [] if not c.get("external")}:
                close()
                open_.update(window=clock.Window(hass.loop, cap).__enter__(), calls=calls)
        elif content.get("role") == "tool_result" and open_:
            open_["calls"].discard(content.get("tool_call_id"))
            if not open_["calls"]:
                close()
    with relied_on("chat_log_subscribe"):
        unsubscribe = subscribe(hass, heard)
    try:
        yield
    finally:
        unsubscribe()
        close()


STATS = {"started": 0, "start_seconds": 0.0, "slowest_start": 0.0, "assistants": 0, "assistant_seconds": 0.0,
         "slowest_assistant": 0.0}  # Diagnostics: where time goes.


def _timed(kind, seconds):
    STATS[f"{kind}_seconds"] = round(STATS[f"{kind}_seconds"] + seconds, 3)
    STATS[f"slowest_{kind}"] = round(max(STATS[f"slowest_{kind}"], seconds), 3)


class Sandbox:
    """Start with await Sandbox.start(...); run coroutines in it with await sandbox.run(fn, *args)."""

    def __init__(self, time_zone, home=None, setup=None):
        self.time_zone = time_zone
        self.home = home or {}  # Today's home at the recorded moment (snapshot.at_moment), as seeded.
        self.setup = setup or {}  # Today's setup beyond the home (replay.setup_now): config entries, helpers, services.
        self.hass = self.loop = self.thread = None
        self.commands, self.unsimulated, self.transitions = [], [], []
        self.changes = []  # Each change of a state or its attributes: {entity_id, old, new, attributes_changed}.
        self.spoken = []  # Each message spoken on devices, by whatever called it (_speak).
        self.outside = []  # Each call beyond the home, as answered (outside): an outside receipt (DETAILS P11.7).
        # Each conversation's id here, by the id it has outside: Home Assistant starts a conversation it doesn't know
        # under an id of its own, as at home (run_turn).
        self.conversations = {}
        self.moving = set()  # Devices the recording shows reporting their movement (simulation.reports_movement).
        self.machinery = set()  # The voice assistant's own entities, as today's pipeline has them (digest.machinery).
        self.cut = set()  # The improver's own AI Tasks (settings.own_models).
        self.recorded = {}  # What the recording answered and failed (replay.recorded): answers to the same calls.
        self.failures = {}  # (entity_id, turn's at) -> the error its command failed with then (recorded's failed).
        self.calls = []  # Each service call, as Home Assistant tells of it (calling): (domain, service, data).
        self.groups = {}  # The stand-in entries' groups (_groups), by recorded config entry.
        self.standins = {}  # entity_id -> its stand-in (standin.StandIn).
        self.unheld = set()  # The states no integration here holds, seeded as they were (seed).
        self.observing = False  # True while recorded changes are applied, which aren't the replay's own.
        self.at = None  # The time of the recorded turn it's at (_run_at).
        self.clock = None  # Its clock (clock.py), from the home's moment.
        self.log = []  # What HA logged inside the sandbox (_Log).
        self.moment = {}  # The home as seeded (home_now), which each turn's home is told against (run_turn).
        self._dir = None  # Made and removed on the sandbox's own thread, never blocking the main loop.

    @classmethod
    async def start(cls, time_zone, home=None, setup=None):
        """A sandbox set up as today's home (home: snapshot.at_moment's; setup: replay.setup_now's), its registries
        loaded whole, disabled entries included, as Home Assistant loads its own; none: an empty home."""
        began = clock.REAL_MONOTONIC()
        sandbox = cls(time_zone, home, setup)
        sandbox.clock = clock.current(_epoch((home or {}).get("at")))
        outer = asyncio.get_running_loop()
        ready = outer.create_future()
        log = _Log(sandbox)

        def run():
            try:
                clock.join(sandbox.clock)
                sandbox._dir = tempfile.TemporaryDirectory(prefix="assist-improver-sandbox-")
                loop = sandbox.loop = clock.Loop(sandbox.clock)
                loop.set_default_executor(ThreadPoolExecutor(EXECUTOR_WORKERS, initializer=clock.join,
                                                             initargs=(sandbox.clock,)))
                asyncio.set_event_loop(loop)
                try:
                    try:
                        loop.run_until_complete(sandbox._setup())
                    except BaseException as err:  # noqa: BLE001 - reported to the caller, which stops the replay.
                        outer.call_soon_threadsafe(ready.set_exception, err)
                        return
                    outer.call_soon_threadsafe(ready.set_result, None)
                    loop.run_forever()
                finally:  # As HA ends its loop: cancel what's left, then end its threads.
                    try:
                        if tasks := asyncio.all_tasks(loop):
                            for task in tasks:
                                task.cancel()
                            loop.run_until_complete(asyncio.wait(tasks, timeout=TEARDOWN_TIMEOUT))
                        loop.run_until_complete(loop.shutdown_asyncgens())
                        loop.run_until_complete(loop.shutdown_default_executor(TEARDOWN_TIMEOUT))
                    finally:
                        asyncio.set_event_loop(None)
                        loop.close()
                        sandbox._dir.cleanup()
            finally:
                logging.getLogger().removeHandler(log)

        sandbox.thread = threading.Thread(target=run, name=THREAD_NAME, daemon=True)
        logging.getLogger().addHandler(log)
        sandbox.thread.start()
        try:
            await ready
        except BaseException:
            if ready.cancelled():  # The caller gave up while it started.
                await sandbox.stop()
            else:  # It failed to start: its thread ends its loop by itself.
                await outer.run_in_executor(None, sandbox.thread.join, 30)
            raise
        STATS["started"] += 1
        _timed("start", clock.REAL_MONOTONIC() - began)
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
        if self.loop is None or self.loop.is_closed():
            return
        with suppress(Exception):
            await self.run(lambda hass: hass.async_stop(force=True))
        self.loop.call_soon_threadsafe(self.loop.stop)
        await asyncio.get_running_loop().run_in_executor(None, self.thread.join, 30)
        self.hass = None

    async def _setup(self):
        """Home Assistant's own start, as far as the sandbox needs it: no public API starts an instance in-process."""
        from homeassistant.core import HomeAssistant
        from homeassistant.setup import async_setup_component
        hass = self.hass = HomeAssistant(self._dir.name)  # This thread's instance only.
        hass.data[SANDBOX_KEY] = self
        if self.home:
            self.groups = await hass.async_add_executor_job(_groups, self.home, self.setup)
            await hass.async_add_executor_job(_store, Path(self._dir.name), self.home, self.setup, self.groups)
        resolve("skip_pip")
        hass.config.skip_pip = True  # Nothing is installed from here (DETAILS P11.8.3): the worker's Python is HA's.
        await hass.config.async_set_time_zone(self.time_zone)  # The process's too: templates' now() reads it.
        resolve("loader")(hass)
        hass.config_entries = resolve("config_entries")(hass, {})
        if not await resolve("base_functionality")(hass):
            raise RuntimeError("The sandbox could not load Home Assistant's base functionality")
        if config := self.home.get("config"):
            await hass.config.async_update(**{k: config[k] for k in CONFIG if config.get(k) is not None},
                                           **_units(config))
        hass.bus.async_listen(EVENT_CALL_SERVICE, self.calling)
        for component in ("homeassistant", "intent", "llm"):  # The web server they ask for is standin's no-op one.
            if not await async_setup_component(hass, component, {}):
                raise RuntimeError(f"The sandbox could not set up {component}")

    @callback
    def calling(self, event):
        """A service call, as Home Assistant tells of it (in the calling task, unless it tells of another then):
        what a stand-in's command is a call of (standin.StandIn._call)."""
        self.calls.append((event.data["domain"], event.data["service"], event.data.get("service_data") or {}))
        standin.CALLING.set((self, len(self.calls) - 1))

    @callback
    def state_changed(self, event):
        old, new = event.data.get("old_state"), event.data.get("new_state")
        if self.observing or old is None or new is None:
            return
        if kept({"entity_id": new.entity_id, "new": {"state": new.state}}, machinery=self.machinery,
                cut=self.cut) in (None, "machinery"):  # Neither the home's nor a request's change.
            return
        if old.state != new.state:
            self.transitions.append({"entity_id": new.entity_id, "old": old.state, "new": new.state})
        moved = {k: [old.attributes.get(k), new.attributes.get(k)] for k in {*old.attributes, *new.attributes}
                 if old.attributes.get(k) != new.attributes.get(k) and k != "friendly_name"}
        if old.state != new.state or moved:  # Its settings too, as a recording keeps them (exploration.SessionView).
            from .serialization import plain
            self.changes.append({"entity_id": new.entity_id, "old": old.state, "new": new.state,
                                 **({"attributes_changed": plain(dict(sorted(moved.items())))} if moved else {})})


def _units(config):
    """Today's unit system, by the name Config.async_update takes."""
    from homeassistant.util.unit_system import METRIC_SYSTEM, US_CUSTOMARY_SYSTEM
    for name, system in (("metric", METRIC_SYSTEM), ("us_customary", US_CUSTOMARY_SYSTEM)):
        if config.get("unit_system") == system.as_dict():
            return {"unit_system": name}
    return {}


def setup_of(today):
    """What a sandbox sets up of today's setup (replay.setup_now's, with replay.today's assistant) beyond the home:
    its config entries, storage and YAML helpers and actions, and the assistant's own entry (load_assistant's)."""
    return {**{k: today.get(k) or {} for k in ("entries", "helpers", "services")},
            "assistant": ((today.get("assistant") or {}).get("entry") or {}).get("entry_id")}


def _real(setup):
    """The integrations a sandbox sets up for real, by domain, and the config entries it loads as they are."""
    domains = {*CORE, *(setup.get("helpers") or {}), "homeassistant", DOMAIN}
    entries = {i for i, e in (setup.get("entries") or {}).items() if e["domain"] in domains}
    return domains, entries | ({setup["assistant"]} if setup.get("assistant") else set())


def _groups(home, setup):
    """The stand-ins of a home's devices, one group per config entry they came from (its entry's title), or per
    integration for those with none: {group: {title, registered: [entity_id], entities: {domain: [entity_id]}}}: the
    entities of an entity domain (homeassistant.const.Platform, standin.standable) whose integration isn't set up for
    real; those disabled, or with no state, aren't added (entities), as at home."""
    domains, real = _real(setup)
    entries, groups = setup.get("entries") or {}, {}
    platforms = {p.value for p in Platform if standin.standable(p.value)}
    for device in (home.get("devices") or {}).values():
        if (entry := entries.get(device.get("config_entry_id"))) and device["config_entry_id"] not in real \
                and not entry.get("disabled_by"):
            groups.setdefault(device["config_entry_id"], {"title": entry["title"], "registered": [], "entities": {}})
    for entity_id, entity in (home.get("entities") or {}).items():
        domain, source = entity_id.split(".", 1)[0], entity.get("config_entry_id")
        if domain not in platforms or entity["platform"] in domains or source in real:
            continue
        if (entry := entries.get(source)) is not None and entry.get("disabled_by"):
            continue
        group = groups.setdefault(source if entry else f"platform:{entity['platform']}",
                                  {"title": entry["title"] if entry else entity["platform"], "registered": [],
                                   "entities": {}})
        group["registered"].append(entity_id)
        if entity_id in (home.get("states") or {}) and not entity.get("disabled_by"):
            group["entities"].setdefault(domain, []).append(entity_id)
    return groups


def _store(folder, home, setup, groups):
    """Today's registries in Home Assistant's own storage form, for it to load whole (a stand-in's entity under the
    stand-in integration, its unique ID its integration's and its own: two integrations may share one; no entry until
    its stand-in's adds it, so moving its device to the stand-in doesn't remove it); the states
    Home Assistant's own integrations restore (a helper's value, an automation on or off, when a script last ran); and
    the helpers' own storage, as today's."""
    import attr
    from homeassistant.core import State
    from homeassistant.helpers import (area_registry as ar, device_registry as dr, entity_registry as er,
                                       floor_registry as fr, label_registry as lr, restore_state)
    from homeassistant.helpers.json import json_dumps
    resolve("entity_storage")
    resolve("device_storage")
    stored = resolve("restore_storage")
    stood_in = {e for group in groups.values() for e in group["registered"]}
    entity_fields = {f.name for f in attr.fields(er.RegistryEntry) if f.init}
    devices = {f.name for f in attr.fields(dr.DeviceEntry) if f.init and not f.name.startswith("_")}
    children = {f.name for f in attr.fields(dr.ChildDeviceEntry) if f.init}

    def entity(record):
        found = {k: v for k, v in record.items() if k in entity_fields}
        found["aliases"] = [er.COMPUTED_NAME if a is None else a for a in found.get("aliases") or []]
        if record["entity_id"] in stood_in:  # Its entry the stand-in's, once its platform adds it.
            found |= {"platform": standin.STANDIN, "unique_id": f"{record['platform']}:{record['unique_id']}",
                      "previous_unique_id": None, "config_entry_id": None, "config_subentry_id": None}
        return er.RegistryEntry(**found).as_storage_fragment

    def device(record):
        kind, names = (dr.ChildDeviceEntry, children) if record.get("parent_device_id") else (dr.DeviceEntry, devices)
        found = {k: v for k, v in record.items() if k in names}
        for key in ("identifiers", "connections"):
            if key in found:
                found[key] = {tuple(i) for i in found[key]}
        found["labels"] = set(found.get("labels") or ())
        return kind(**found)
    made = [device(d) for d in (home.get("devices") or {}).values()]
    domains = {*CORE, *(setup.get("helpers") or {})}
    stores = {
        er: {"entities": [entity(e) for e in (home.get("entities") or {}).values()], "deleted_entities": [],
             "settings": {"entity_id_parts": None}},  # How new entity IDs are made: Home Assistant's default.
        dr: {"devices": [d.as_storage_fragment for d in made if not isinstance(d, dr.ChildDeviceEntry)],
             "child_devices": [d.as_storage_fragment for d in made if isinstance(d, dr.ChildDeviceEntry)],
             "deleted_devices": []},
        ar: {"areas": list((home.get("areas") or {}).values())},
        fr: {"floors": list((home.get("floors") or {}).values())},
        lr: {"labels": list((home.get("labels") or {}).values())}}
    storage = folder / resolve("storage_folder")
    storage.mkdir()
    for registry, data in stores.items():
        (storage / registry.STORAGE_KEY).write_text(json_dumps({
            "version": registry.STORAGE_VERSION_MAJOR, "minor_version": registry.STORAGE_VERSION_MINOR,
            "key": registry.STORAGE_KEY, "data": data}))
    now = dt_util.utcnow()
    with relied_on("restore_storage"):
        restored = [stored(State(e, s["state"], s.get("attributes") or {}), None, now).as_dict()
                    for e, s in (home.get("states") or {}).items() if e.split(".", 1)[0] in domains]
        (storage / restore_state.STORAGE_KEY).write_text(json_dumps({
            "version": restore_state.STORAGE_VERSION, "key": restore_state.STORAGE_KEY, "data": restored}))
    for domain, helper in (setup.get("helpers") or {}).items():
        if helper.get("storage") is not None:
            (storage / domain).write_text(json.dumps(helper["storage"]))


def seed_options(today, turn):
    """What today's setup (replay.setup_now's) says the sandbox needs for a recorded request: the entity domains
    Assist's tools come from (a light's intents), the request's device when it runs timers, and whether Assist can file
    the conversation."""
    from .llm import FileImprovement
    names, device = today["tools"], turn["prepared"]["context"].get("device_id")
    return {"integrations": sorted({n.split("__", 1)[0] for n in names if "__" in n} & {p.value for p in Platform}),
            "timer_device": device if device and device in today["timers"] else None,
            "filing": FileImprovement.name in names}


async def seed(hass, *, person=None, integrations=(), timer_device=None, filing=False, scripts=None,
               automations=None, exposure_changes=None, moving=(), machinery=(), cut=(), recorded=None):
    """Make the sandbox the home it was started as (Sandbox.start: snapshot.at_moment's, today's setup at a recorded
    moment): Home Assistant's own integrations set up for real (CORE, the home's storage and YAML helpers, the entity
    domains Assist's tools come from), a stand-in entry per config entry with devices (standin.py), every other state as
    it was; calls beyond the home stood in (outside); then its scripts, automations and scenes, which create their own
    entities. A change comes as the home it makes (changes.changed); exposure_changes ({entity_id: exposed}), what it
    shows or hides, must name what Home Assistant loads. moving: the devices that report their movement
    (simulation.reports_movement); machinery: the voice assistant's own entities (digest.machinery), and cut: the
    improver's own AI Tasks, neither ever the home's changes nor its home (digest.kept); recorded: what the recording
    answered and failed (replay.recorded). Runs in the sandbox's loop. filing: Assist had the improver's filing tool,
    which answers here as filing does and files nothing (llm.ReplayedFiling)."""
    from homeassistant.components.intent import async_register_timer_handler
    from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntryState
    from homeassistant.core import CoreState
    from homeassistant.setup import async_setup_component
    expose = resolve("sandbox_exposure")  # No public API exposes an entity, nor runs HA without starting it.
    resolve("running_state")
    sandbox = hass.data[SANDBOX_KEY]
    source, setup = sandbox.home, sandbox.setup
    _run_at(hass, source)
    # The sandbox has no accounts, only the person speaking, with their rights (replay._person).
    resolve("auth_user")
    with relied_on("auth_user"):
        hass.auth = SimpleNamespace(async_get_user=_user(person))
    sandbox.moving, sandbox.machinery, sandbox.cut = set(moving), set(machinery), set(cut)
    sandbox.recorded = recorded or {}
    sandbox.failures = {(e, _moment(f["at"])): f["error"] for f in sandbox.recorded.get("failed") or []
                        for e in f["entity_ids"]}
    if filing:
        from .llm import REPLAYED_FILING
        hass.data[REPLAYED_FILING] = True
        if not await async_setup_component(hass, DOMAIN, {}):  # Assist asks the platforms of set-up integrations.
            raise RuntimeError("The sandbox could not set up the improver")
    domains, real = _real(setup)
    for entry_id, entry in (setup.get("entries") or {}).items():  # A core integration's own entry (the sun's).
        if entry["domain"] in CORE and not entry.get("disabled_by"):
            await hass.config_entries.async_add(_entry(entry_id, entry))
    platforms = {p.value for p in Platform}
    wanted = [d for d in ("conversation", "assist_pipeline", "sun", *(setup.get("helpers") or {}), *integrations)
              if d not in hass.config.components]
    for domain in wanted:
        conf = ((setup.get("helpers") or {}).get(domain) or {}).get("yaml") or {}
        if not await async_setup_component(hass, domain, conf):
            raise RuntimeError(f"The sandbox could not set up {domain}")
    unregistered = [e for e, s in (source.get("states") or {}).items() if e.split(".", 1)[0] in platforms
                    and e not in (source.get("entities") or {}) and hass.states.get(e) is None
                    and e.split(".", 1)[0] not in ("script", "automation", "scene")]
    if unregistered:
        found = sandbox.groups.setdefault("", {"title": "Home Assistant", "registered": [],
                                               "entities": {}})["entities"]
        for entity_id in unregistered:
            found.setdefault(entity_id.split(".", 1)[0], []).append(entity_id)
    for group, found in sandbox.groups.items():
        made = await hass.config_entries.flow.async_init(standin.STANDIN, context={"source": SOURCE_IMPORT},
                                                         data={"group": group, "title": found["title"]})
        if made["result"].state is not ConfigEntryState.LOADED:
            raise RuntimeError(f"The sandbox could not stand in for {found['title']}")
    own = {domain for domain, loaded in (("script", scripts), ("automation", automations),
                                         ("scene", source.get("scenes"))) if loaded}
    assistant = {e for e, x in (source.get("entities") or {}).items()  # Its provider sets them up (load_assistant).
                 if setup.get("assistant") and x.get("config_entry_id") == setup["assistant"]}
    for entity_id, state in (source.get("states") or {}).items():  # What no integration here holds, as it was.
        if hass.states.get(entity_id) is None and entity_id.split(".", 1)[0] not in own and entity_id not in assistant:
            hass.states.async_set(entity_id, state["state"], state.get("attributes") or {})
            sandbox.unheld.add(entity_id)
    if timer_device:  # The request's device runs timers, in the registry or not.
        async_register_timer_handler(hass, timer_device, lambda *args: None)
    _hold_delayed_commands(hass)
    _emulate_speaking(hass)
    _outside(hass, setup.get("services") or {})
    await _closed(hass, domains, setup)
    # Running, without firing HA's start events: automations are live, but none triggers on a start. They load
    # after the states are seeded, so seeding triggers none either; each restores whether it's on as the home had it.
    hass.set_state(CoreState.running)
    for domain, conf in (("script", scripts), ("automation", automations), ("scene", source.get("scenes"))):
        if conf and not await async_setup_component(hass, domain, {domain: conf}):
            raise RuntimeError(f"The sandbox could not load the {domain}s")
    await hass.async_block_till_done()
    for entity_id in exposure_changes or {}:  # A change to something HA couldn't load can't be tested.
        if (state := hass.states.get(entity_id)) is None or state.state == "unavailable":
            raise RuntimeError(f"{entity_id} could not be loaded")
    exposure = {**(source.get("exposure") or {}), **(exposure_changes or {})}
    for entity_id, exposed in exposure.items():
        if exposed is not None and hass.states.get(entity_id) is not None:
            expose(hass, "conversation", entity_id, bool(exposed))
    hass.bus.async_listen("state_changed", sandbox.state_changed)
    await hass.async_block_till_done()
    sandbox.moment = home_now(hass)


def _entry(entry_id, entry):
    """A config entry of today's as Home Assistant builds one from its storage, its own id and title (its data and
    options are never read)."""
    from types import MappingProxyType
    with relied_on("config_entry"):
        return resolve("config_entry")(
            domain=entry["domain"], entry_id=entry_id, title=entry["title"], data={}, options={}, source="user",
            version=1, minor_version=1, unique_id=None, subentries_data=None, discovery_keys=MappingProxyType({}))


async def _closed(hass, domains, setup):
    """No integration that reaches devices or services is set up in a sandbox: each loaded one is set up for real
    here, an entity domain, the stand-in or its web server, or one with no devices or services to reach (no iot_class,
    or calculated), and never one of NEVER (DETAILS P11.8)."""
    from homeassistant.loader import async_get_integration
    allowed = {*domains, *(p.value for p in Platform), standin.STANDIN, "http", "intent", "llm"}
    for domain in sorted(c for c in hass.config.components if "." not in c):
        if domain in NEVER or (domain not in allowed and (await async_get_integration(hass, domain)).iot_class
                               not in (None, "calculated")):
            raise RuntimeError(f"The sandbox set up {domain}, which reaches devices or services")


async def load_assistant(hass, entry, agent):
    """The household's assistant, as Home Assistant loads it at home: its provider's config entry (entry: HA's storage
    form, ConfigEntry.as_dict(), with the conversation subentries it uses) set up by the provider's own integration,
    and its conversation agents at the home's entity ids (agent: one registry entry's entity_id, unique_id, platform
    and config_subentry_id). Nothing is installed: the provider's package is the one Home Assistant has. The entry is
    saved only in the sandbox's throwaway folder. The agent's entity id."""
    from datetime import datetime
    from types import MappingProxyType
    from homeassistant.components import conversation
    from homeassistant.config_entries import ConfigEntryDisabler
    from homeassistant.helpers import entity_registry as er
    from homeassistant.util.enum import try_parse_enum
    began = clock.REAL_MONOTONIC()
    ConfigEntry = resolve("config_entry")  # Built as HA builds one from its storage: no public API does.
    registry, entity_id = er.async_get(hass), agent["entity_id"]
    created, modified = datetime.fromisoformat(entry["created_at"]), datetime.fromisoformat(entry["modified_at"])
    with relied_on("config_entry"):
        loaded = ConfigEntry(  # As Home Assistant builds an entry from its storage.
            created_at=created, data=entry["data"],
            disabled_by=try_parse_enum(ConfigEntryDisabler, entry["disabled_by"]),
            discovery_keys=MappingProxyType({}), domain=entry["domain"], entry_id=entry["entry_id"],
            minor_version=entry["minor_version"], modified_at=modified, options=entry["options"],
            pref_disable_new_entities=False,  # The agent isn't new here: it's the home's own.
            pref_disable_polling=entry["pref_disable_polling"], source=entry["source"],
            subentries_data=entry["subentries"], title=entry["title"], unique_id=entry["unique_id"],
            version=entry["version"])
    with relied_on("config_entries"):
        hass.config_entries._entries[loaded.entry_id] = loaded  # As HA's own test helpers add one.
    _stand_in_apis(hass, entry["subentries"], hass.data[SANDBOX_KEY].recorded)
    made = registry.async_get_or_create("conversation", agent["platform"], agent["unique_id"],
                                        suggested_object_id=entity_id.split(".", 1)[1], config_entry=loaded,
                                        config_subentry_id=agent["config_subentry_id"])
    if made.entity_id != entity_id:
        raise RuntimeError(f"The sandbox gave the assistant {made.entity_id}, not {entity_id}")
    before = _actions(hass)
    set_up = await hass.config_entries.async_setup(loaded.entry_id)
    await hass.async_block_till_done()
    # What the provider brought with it reaches beyond the home but for the turn's own call: its own actions, and
    # those of what it set up (AI Tasks', text-to-speech's), stood in as every such call is (_outside).
    _stand_in(hass, {k: hass.services.supports_response(*k) for k, found in _actions(hass).items()
                     if before.get(k) is not found})
    if not set_up or conversation.async_get_agent(hass, entity_id) is None:
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
    _timed("assistant", clock.REAL_MONOTONIC() - began)
    return entity_id


def _stand_in_apis(hass, subentries, recorded):
    """The LLM APIs an assistant uses beside Assist that the sandbox doesn't have (another integration's, a remote
    server's): they stand in with the tools the recording shows beyond Assist's (recorded's tools, as specified there),
    each answering with the recorded result of the same call, else an empty one marked not simulated: an outside
    receipt (DETAILS P11.7)."""
    import probatio
    from homeassistant.helpers import llm

    class Specified:
        """A tool's parameters as recorded: any arguments pass, and they read as the recorded JSON schema."""

        def __init__(self, schema):
            self.schema = schema

        def __call__(self, value):
            return value

    class Recorded(llm.Tool):
        def __init__(self, spec):
            self.name, self.description = spec["name"], spec.get("description")
            self.integration = spec.get("integration") or DOMAIN
            self.parameters = probatio.Schema(Specified(spec.get("parameters") or {}))

        async def async_call(self, hass, tool_input, llm_context):
            from .serialization import plain
            sandbox, args = hass.data[SANDBOX_KEY], plain(tool_input.tool_args)
            answer = next((r["result"] for r in sandbox.recorded.get("results") or []
                           if r["tool_name"] == self.name and r["tool_args"] == args), None)
            sandbox.outside.append({"action": f"tool {self.name}", "data": args,
                                    "answer": "recorded" if answer is not None else "not simulated"})
            if answer is None:
                sandbox.unsimulated.append(f"tool {self.name}")
            found = answer or {}
            return llm.ToolResult(data=found.get("data", found), error=bool(found.get("error")))

    def serializer(value):
        return value.schema if isinstance(value, Specified) else probatio.UNSUPPORTED
    tools = [Recorded(spec) for spec in recorded.get("tools") or []]

    class StandIn(llm.API):
        async def async_get_api_instance(self, llm_context):
            return llm.APIInstance(api=self, api_prompt="", llm_context=llm_context, tools=tools,
                                   custom_serializer=serializer)
    have = {api.id for api in llm.async_get_apis(hass)}
    for subentry in subentries:
        named = subentry["data"].get("llm_hass_api") or []
        for api_id in [named] if isinstance(named, str) else named:
            if api_id not in have:
                llm.async_register_api(hass, StandIn(hass=hass, id=api_id, name=api_id))
                have.add(api_id)
                tools = []  # Each recorded tool once.


def _hold_delayed_commands(hass):
    """A timer's command for when it ends isn't simulated (HA would hand it to the conversation agent that set it): the
    timer runs without it, and the command is logged as an outside receipt."""
    timers = resolve("timers")  # No public API holds a timer's command back.
    resolve("timer_start")
    resolve("timer_device")
    if (manager := hass.data.get(timers)) is None:
        return
    start = manager.start_timer

    def start_timer(device_id, *args, conversation_command=None, conversation_agent_id=None, **kwargs):
        if conversation_command:
            hass.data[SANDBOX_KEY].outside.append({"action": "timer command", "answer": "not simulated",
                                                   "data": {"command": conversation_command}})
            if device_id is None or not manager.is_timer_device(device_id):
                return None
        return start(device_id, *args, **kwargs)
    with relied_on("timer_start"):
        manager.start_timer = start_timer


# An announcement on devices (DETAILS P9.10.5) leaves no state to simulate: each speaking service (analyzer.speaking)
# is taken as done, and what it said or played where is logged, whatever called it (a broadcast, a script), for a
# replay to judge an action by. A media player logs what it plays itself (standin.MediaPlayerStandIn).
def _emulate_speaking(hass):
    """Registered after the integrations' own, before the household's scripts load (a legacy engine's say, configured
    in YAML, never loads here: outside takes it)."""
    from .analyzer import SPEAKING
    for name in SPEAKING:
        if not name.startswith("media_player."):
            hass.services.async_register(*name.split("."), functools.partial(_speak, hass))


def _outside(hass, services):
    """Calls beyond the home: every action today's Home Assistant has that the sandbox doesn't (an integration's own,
    a notification; a legacy engine's say speaks), and the model calls a sandbox never makes but the turn's own
    (conversation.process on any agent but Home Assistant's built-in one, every AI Task action), each stood in by one
    that logs it (_stand_in). services: today's, {domain: {service: whether it answers}}. What the assistant's provider
    brings with it is stood in once it's loaded (load_assistant)."""
    have = hass.services.async_services()
    _stand_in(hass, {
        **{(domain, service): SupportsResponse(answers) for domain, found in services.items()
           for service, answers in found.items() if domain == MODELS or service not in have.get(domain, {})},
        **{(MODELS, service): found.supports_response for service, found in have.get(MODELS, {}).items()}})
    hass.services.async_register("conversation", "process", functools.partial(_process, hass),
                                 supports_response=SupportsResponse.OPTIONAL)


def _stand_in(hass, actions):
    """Each action ({(domain, service): whether it answers}) stood in by one that logs it: what it spoke (_speak), or
    a call beyond the home (_answer)."""
    from .analyzer import speaking
    for (domain, service), answers in actions.items():
        spoken = speaking(f"{domain}.{service}")
        hass.services.async_register(domain, service, functools.partial(_speak if spoken else _answer, hass),
                                     supports_response=SupportsResponse.NONE if spoken else answers)


def _actions(hass):
    """Each action the sandbox has, by (domain, service): its handler."""
    return {(domain, service): found for domain, services in hass.services.async_services().items()
            for service, found in services.items()}


async def _answer(hass, call):
    """A call beyond the home, logged as an outside receipt: answered empty, as not simulated (no action's answer is
    recorded)."""
    from .serialization import plain
    sandbox, name = hass.data[SANDBOX_KEY], f"{call.domain}.{call.service}"
    sandbox.outside.append({"action": name, "data": plain(dict(call.data)), "answer": "not simulated"})
    sandbox.unsimulated.append(name)
    return {} if call.return_response else None


async def _process(hass, call):
    """conversation.process: Home Assistant's built-in agent answers here as at home; any other is a model call
    beyond the turn's own (answer)."""
    from homeassistant.components import conversation
    if call.data.get("agent_id") not in (None, conversation.HOME_ASSISTANT_AGENT):
        return await _answer(hass, call)
    result = await conversation.async_converse(
        hass, call.data["text"], call.data.get("conversation_id"), call.context, call.data.get("language"),
        agent_id=call.data.get("agent_id"))
    return result.as_dict() if call.return_response else None


async def _speak(hass, call):
    """Log what a speaking call said or played: {service, message, targets}, with media for an announcement's audio
    (a chime); tts.speak speaks with its engine on its media players, a legacy engine's say on the media players it
    targets, the others on the entities they target. As Home Assistant's entity services skip an entity that's missing
    or unavailable, nothing is spoken there, and a satellite named that can't announce (or start a conversation)
    refuses the call."""
    from homeassistant.helpers import config_validation as cv
    from homeassistant.helpers.service import async_extract_entity_ids
    sandbox, data, service = hass.data[SANDBOX_KEY], dict(call.data), f"{call.domain}.{call.service}"
    here = lambda e: (state := hass.states.get(e)) is not None and state.state != "unavailable"  # noqa: E731
    found = sorted(e for e in await async_extract_entity_ids(call) if here(e))
    targeted, media = [e for e in found if e.split(".", 1)[0] == call.domain], None
    if service == "tts.speak":
        if not targeted:  # No engine to speak with.
            return
        players = data.get("media_player_entity_id") or []
        targets = sorted(p.strip() for p in (players.split(",") if isinstance(players, str) else players) if here(
            p.strip()))
        message = data.get("message")
    elif call.domain == "tts":  # A legacy engine's say, on its media players (named, as its schema reads them, or all).
        named = data.get("entity_id") or []
        players = hass.states.async_entity_ids("media_player") if named == "all" else cv.entity_ids(named)
        targets, message = sorted(e for e in players if e.startswith("media_player.") and here(e)), data.get("message")
    else:  # A satellite's announcement, or a conversation it starts: its words, or its audio.
        start, feature = ("start_", 2) if call.service == "start_conversation" else ("", 1)  # Its feature's bit.
        able = lambda e: int(hass.states.get(e).attributes.get("supported_features") or 0) & feature  # noqa: E731
        named = data.get("entity_id") or []
        if refused := [e for e in targeted if e in cv.entity_ids(named) and not able(e)]:
            raise ServiceNotSupported(call.domain, call.service, refused[0])
        targets, media = [e for e in targeted if able(e)], data.get(f"{start}media_id")
        media = media.get("media_content_id") if isinstance(media, dict) else media  # As the media selector gives it.
        message = data.get(f"{start}message") or _tts_message(media)
    sandbox.spoken.append({"service": service, "message": str(message or ""), "targets": targets,
                           **({"media": str(media)} if media and _tts_message(media) is None else {})})
    data = {k: v for k, v in data.items() if k not in standin.TARGETS}
    sandbox.commands += [{"entity_id": e, "service": service, "data": data} for e in targets]  # A command to each.


def _tts_message(media):
    """The message a text-to-speech media source speaks (media-source://tts/<engine>?message=...), else None."""
    from yarl import URL
    if not isinstance(media, str) or not media.startswith("media-source://tts"):
        return None
    return URL(media).query.get("message")


# What a sandbox sets up of a home (a snapshot) besides the devices' states (seeded): its registries, and the rest.
REGISTRIES = ("entities", "devices", "areas", "floors", "labels")
SETUP = ("time_zone", "exposure", "scripts", "automations", "scenes", "household", "household_on")
STAMPS = ("created_at", "modified_at")  # When a registry entry was made and last changed, which nothing reads here.


def seeded(home):
    """What a sandbox sets up of a home besides the devices' states, which is what a replay depends on of a setup
    (simulate.setup_digest): its registries whole but their time stamps, what Assist can see, its scripts,
    automations and scenes, the household knowledge, and the config it takes (CONFIG)."""
    return {**{k: {i: {f: v for f, v in x.items() if f not in STAMPS} for i, x in (home.get(k) or {}).items()}
               for k in REGISTRIES},
            **{k: home.get(k) for k in SETUP},
            "config": {k: (home.get("config") or {}).get(k) for k in (*CONFIG, "unit_system")}}


def _user(person):
    """Home Assistant's lookup of the person speaking, as their user record has them today (replay._person): HA names
    them, and checks what they may use and control."""
    from homeassistant.auth.permissions.const import POLICY_CONTROL

    async def get_user(user_id):
        if person is None:
            return None
        refused = set(person["refused"])
        return SimpleNamespace(
            id=user_id, name=person["name"], is_admin=person["is_admin"],
            groups=[SimpleNamespace(id=group) for group in person["group_ids"]],
            permissions=SimpleNamespace(check_entity=lambda entity_id, key: key != POLICY_CONTROL or
                                        entity_id not in refused))
    return get_user


async def system_prompt(hass, llm_context, api_id, prompt):
    """Assist's system prompt and tools, as HA builds them for this request in the sandbox."""
    from homeassistant.components.conversation import ChatLog
    chat_log = ChatLog(hass, "assist-improver-sandbox-prompt")
    await chat_log.async_provide_llm_data(llm_context, user_llm_hass_api=api_id, user_llm_prompt=prompt)
    return chat_log.content[0].content, chat_log.llm_api


MARKED = ("transitions", "changes", "commands", "spoken", "outside", "unsimulated")  # A turn's own lists (run_turn).


async def run_turn(hass, text, conversation_id, context, language, agent_id, device_id=None, watch=(), until=None):
    """One Assist turn as Home Assistant runs it at home, on the household's assistant loaded here (load_assistant),
    at the recorded turn's time: HA opens the chat session and log as its pipeline does, the assistant renders its
    prompt, builds its tools, asks its model and runs each tool call here, at once, as it comes, waits inside them
    passing at once (_tool_windows), never past until (the next recorded turn's time, ISO; None: none). Its answer:
    what the turn added to the chat log after the person's words (each as HA gives it), what the assistant was given
    (its system prompt, its tools' names), the provider's usage as the assistant traces it, the watched entities'
    attributes before and after, and the home once everything the turn set off has run or until came (snapshot): its
    changes, commands, speech and calls beyond the home (outside), and what of it wasn't simulated (unemulated), the
    turn's own."""
    from homeassistant.components import conversation
    from homeassistant.core import Context
    from homeassistant.helpers import chat_session
    from .serialization import plain
    sandbox, usage = hass.data[SANDBOX_KEY], []
    before = {e: dict(state.attributes) for e in watch if (state := hass.states.get(e)) is not None}
    marks = {k: len(getattr(sandbox, k)) for k in MARKED}
    with chat_session.async_get_chat_session(  # Its owner: whoever says the turn (async_converse).
            hass, sandbox.conversations.get(conversation_id, conversation_id), context=None) as session, \
            conversation.async_get_chat_log(hass, session) as chat_log, \
            _tool_windows(hass, session.conversation_id, _epoch(until)):
        sandbox.conversations[conversation_id] = session.conversation_id
        start = len(chat_log.content)
        resolve("chat_log_trace")  # What a provider reports of a turn reaches only the chat log's trace.
        traced = chat_log.async_trace

        def trace(agent_details):  # As the assistant reports it, provider-neutral (absent: unknown, never zero).
            if isinstance(agent_details.get("stats"), dict):
                usage.append(plain(agent_details["stats"]))
            traced(agent_details)
        with relied_on("chat_log_trace"):
            chat_log.async_trace = trace
        result = await conversation.async_converse(
            hass, text, session.conversation_id, Context(**context), language=language, agent_id=agent_id,
            device_id=device_id)
        added = chat_log.content[start:]
        if added and added[0].role == "user":
            added = added[1:]
        api = chat_log.llm_api
        given = {"prompt": chat_log.content[0].content if chat_log.content else None,
                 "tools": [t.name for t in api.tools] if api else []}
    found = await snapshot(hass, watch, until)
    return {"content": plain([c.as_dict() for c in added]), "reply": plain(result.response.as_dict()),
            "given": given, "usage": usage, "before": plain(before), "attributes": plain(found["attributes"]),
            **{k: plain(found[k][marks[k]:]) for k in marks if k != "unsimulated"},
            "unemulated": found["unsimulated"][marks["unsimulated"]:], "home": moved(sandbox.moment, home_now(hass))}


def home_now(hass):
    """The home's states now, each {state, attributes} in plain form: the voice assistant's own entities and the
    improver's own AI Tasks aren't the home (digest.kept)."""
    from .serialization import plain
    gears = hass.data[SANDBOX_KEY].machinery | hass.data[SANDBOX_KEY].cut
    return {s.entity_id: {"state": s.state, "attributes": plain(dict(s.attributes))} for s in hass.states.async_all()
            if s.entity_id not in gears}


def moved(moment, now):
    """How the home differs now from the moment it was seeded as: {entity_id: {state, the attributes changed or
    added, removed: those gone}}, None for an entity gone. A test that branches from a baseline after this turn starts
    from it (replay.SandboxHome's start)."""
    out = {e: None for e in moment if e not in now}
    for entity_id, found in now.items():
        was = moment.get(entity_id) or {"attributes": {}}
        attributes = {k: v for k, v in found["attributes"].items() if k not in was["attributes"]
                      or was["attributes"][k] != v}
        removed = sorted(k for k in was["attributes"] if k not in found["attributes"])
        if found["state"] != was.get("state") or attributes or removed:
            out[entity_id] = {"state": found["state"], "attributes": attributes, "removed": removed}
    return out


async def carry(hass, conversation_id, llm_context, steps):
    """A test's turns before its branch, its baseline's (DETAILS P6.2), in Home Assistant's chat log for the replay's
    conversation, as Home Assistant keeps a conversation's history, through its public conversation API: each turn's
    words, then the baseline's replies, tool calls and their results, which no tool runs again. steps: the
    baseline's, each {input, output: what Assist added, as run_turn gives it}. The assistant builds its own prompt and
    tools at the branch turn, as at a conversation's first."""
    from homeassistant.components import conversation
    from homeassistant.helpers import chat_session, llm
    sandbox, context = hass.data[SANDBOX_KEY], llm_context
    with chat_session.async_get_chat_session(hass, conversation_id, context=None) as session, \
            conversation.async_get_chat_log(hass, session) as chat_log:
        sandbox.conversations[conversation_id] = session.conversation_id
        # A chat log takes tool calls only with an API to answer them: these are answered with the baseline's results.
        await chat_log.async_provide_llm_data(context, user_llm_hass_api=llm.LLM_API_ASSIST)
        for step in steps:
            chat_log.async_add_user_content(conversation.UserContent(content=step["input"]))
            results = {i["tool_call_id"]: _tool_result(i) for i in step["output"] if i.get("role") == "tool_result"}
            answered = set()
            for item in step["output"]:
                if item.get("role") == "assistant":
                    calls = [llm.ToolInput(tool_name=c["tool_name"], tool_args=c["tool_args"], id=c["id"],
                                           external=bool(c.get("external"))) for c in item.get("tool_calls") or []]
                    content = conversation.AssistantContent(
                        agent_id=item["agent_id"], content=item.get("content"),
                        thinking_content=item.get("thinking_content"), tool_calls=calls or None)
                    given = {c.id: _done(results[c.id]) for c in calls if not c.external}
                    if not given:
                        chat_log.async_add_assistant_content_without_tools(content)
                        continue
                    answered |= given.keys()
                    async for _ in chat_log.async_add_assistant_content(content, tool_call_tasks=given):
                        pass
                elif item.get("role") == "tool_result" and item["tool_call_id"] not in answered:  # An external call's.
                    chat_log.async_add_assistant_content_without_tools(conversation.ToolResultContent(
                        agent_id=item["agent_id"], tool_call_id=item["tool_call_id"], tool_name=item["tool_name"],
                        result=results[item["tool_call_id"]]))
        await chat_log.async_provide_llm_data(context)  # No API: the assistant's own comes at the branch turn.


def _tool_result(item):
    """A tool result as HA's chat log gives it (tool_result: before 2026.10), as Home Assistant's ToolResult."""
    from homeassistant.helpers import llm
    found = item["result"] if "result" in item else {"data": item.get("tool_result")}
    return llm.ToolResult(data=found.get("data"), error=bool(found.get("error")))


def _done(result):
    """A tool call's task, done: its result."""
    task = asyncio.get_running_loop().create_future()
    task.set_result(result)
    return task


async def call_tool(hass, api, tool_input):
    """One of Assist's tools, called in the sandbox."""
    return await api.async_call_tool(tool_input)


async def observe(hass, source, moves=()):
    """Move on to the next recorded turn: its time (advance), and what others in the home moved since, on top of what
    the replay did itself. moves: each skipped turn's then this one's, in order (simulation.moved_by_others). What the
    moves set off is waited for, as others' doing too. A device's stand-in takes its move itself
    (standin.StandIn.hold), what another integration here holds takes it through Home Assistant (_reproduce), and a
    state nothing holds is set as it moved."""
    from homeassistant.core import State
    await advance(hass, source.get("at"))
    _run_at(hass, source)
    sandbox = hass.data[SANDBOX_KEY]
    sandbox.observing = True
    try:
        for moved in moves:
            held = []
            for entity_id, move in moved.items():
                if (current := hass.states.get(entity_id)) is None:
                    continue
                state = move.get("state", current.state)
                attributes = {k: v for k, v in current.attributes.items() if k not in move["removed"]}
                if (device := sandbox.standins.get(entity_id)) is not None:
                    device.hold(state, move["attributes"], move["removed"])
                elif entity_id in sandbox.unheld:
                    hass.states.async_set(entity_id, state, attributes | move["attributes"])
                else:
                    held.append(State(entity_id, state, attributes | move["attributes"]))
            await _reproduce(hass, held)
        await hass.async_block_till_done()
    finally:
        sandbox.observing = False


async def _reproduce(hass, states):
    """Others' moves on what an integration here holds (a helper's value, an automation on or off), made as a scene
    sets a state: by Home Assistant's reproduce state, which each integration maps to its own actions, and HA checks.
    One it can't set so (the sun, a script running) follows its own; one today's setup refuses (a helper's range
    changed since) is logged."""
    from homeassistant.helpers.state import async_reproduce_state
    from homeassistant.loader import async_get_integration
    for state in states:
        if not (await async_get_integration(hass, state.domain)).platforms_exists(("reproduce_state",)):
            continue
        try:
            await async_reproduce_state(hass, state)
        except (HomeAssistantError, ValueError) as err:
            _LOGGER.warning("Others' move on %s couldn't be made here: %s", state.entity_id, err)


async def snapshot(hass, attributes=(), until=None):
    """The sandbox's home once what's running has finished, its waits (a script's delay) passing at once, or once
    nothing more can happen before until (a recorded time, ISO; None: none), which the clock never passes, the
    replay's conversations held meanwhile (_held): states, the replay's state changes, its device commands, what it
    spoke and its calls beyond the home; the attributes of the entities named (a light's brightness a goal asks
    for)."""
    from copy import deepcopy
    from .serialization import plain
    sandbox = hass.data[SANDBOX_KEY]
    with _held(hass):
        await hass.loop.settle(_epoch(until), until=hass.async_block_till_done())
    return {"states": {s.entity_id: s.state for s in hass.states.async_all()},
            "attributes": plain({e: dict(state.attributes) for e in attributes
                                 if (state := hass.states.get(e)) is not None}),
            "transitions": deepcopy(sandbox.transitions), "changes": deepcopy(sandbox.changes),
            "commands": deepcopy(sandbox.commands),
            "unsimulated": list(sandbox.unsimulated), "spoken": deepcopy(sandbox.spoken),
            "outside": deepcopy(sandbox.outside)}
