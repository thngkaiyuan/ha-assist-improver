"""A sandboxed Home Assistant instance: HA's own engine, acting on a simulated home.

A sandbox runs only in a replay worker, a short-lived process of its own (worker.py), never in Home Assistant's own
process; in tests, in the test's process (worker.INLINE). It is a separate HomeAssistant object in its own thread and
event loop. It runs HA's own integrations (intents, the Assist API, scripts, automations, and the household's
assistant by its provider's integration, which calls the model from here) over a copy of the home; only the commands
to devices are emulated (devices.py). It has no network listener and no device integrations, so nothing in it can
reach a real device. Its config folder is a throwaway one, gone with its worker. The worker's process is the
sandbox's, so its time zone is the home's, as the household's Home Assistant has it.

What Assist reads and sets off is as of the conversation: seeded as the home at a recorded turn's moment, and moved on
to each later one, the sandbox renders Assist's instructions (their now()) and runs Assist's tool calls at that turn's
time (HA's date and time tool, and the scripts and automations a call sets off, their time conditions included), as
HA's own tests freeze time where they need to. Only there: HA's dt_util.now reads the recorded time inside those, and
Home Assistant's own time everywhere else. HA's scheduler reads utcnow and the system clock, which stay real (timed
automations aren't all due at once).
"""

import asyncio
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from datetime import datetime
import functools
import logging
import tempfile
import threading
import time
from types import SimpleNamespace

from homeassistant.core import SupportsResponse, callback
from homeassistant.exceptions import HomeAssistantError, ServiceNotSupported
from homeassistant.util import dt as dt_util
from homeassistant.util.hass_dict import HassKey

from . import devices
from .const import DOMAIN
from .touchpoints import relied_on, resolve

_LOGGER = logging.getLogger(__name__)

SANDBOX_KEY: HassKey["Sandbox"] = HassKey(f"{DOMAIN}_sandbox")  # In the sandbox's own instance.
THREAD_NAME = "assist-improver-sandbox"
SANDBOX_LOG_LIMIT = 200
NOT_THE_HOME = ("conversation", "ai_task", "tts", "stt", "wake_word", "assist_satellite")  # Assist's own, not devices.
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


class _Log(logging.Handler):
    """What Home Assistant logs in a sandbox's thread, kept with the sandbox for the replay to report."""

    def __init__(self, sandbox):
        super().__init__()
        self.sandbox = sandbox

    def emit(self, record):
        if record.thread == self.sandbox.thread.ident and len(self.sandbox.log) < SANDBOX_LOG_LIMIT:
            self.sandbox.log.append(f"{record.levelname} {record.name}: {record.getMessage()}"[:500])


_AS_RECORDED = ContextVar("assist_improver_as_recorded", default=None)  # The recorded time, where Assist reads it.
_NOW = dt_util.now  # Home Assistant's own.


def _now(time_zone=None):
    """dt_util.now in the worker's process: the recorded turn's time where Assist reads it (_as_recorded), Home
    Assistant's own everywhere else."""
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


STATS = {"started": 0, "start_seconds": 0.0, "slowest_start": 0.0, "assistants": 0, "assistant_seconds": 0.0,
         "slowest_assistant": 0.0}  # Diagnostics: where time goes.


def _timed(kind, seconds):
    STATS[f"{kind}_seconds"] = round(STATS[f"{kind}_seconds"] + seconds, 3)
    STATS[f"slowest_{kind}"] = round(max(STATS[f"slowest_{kind}"], seconds), 3)


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
        self.commands, self.unsimulated, self.transitions = [], [], []
        self.spoken = []  # Each message spoken on devices, by whatever called it (_speak).
        # Each conversation's id here, by the id it has outside: Home Assistant starts a conversation it doesn't know
        # under an id of its own, as at home (run_turn).
        self.conversations = {}
        self.moving = set()  # Devices the recording shows reporting their movement (simulation.reports_movement).
        self.observing = False  # True while recorded changes are applied, which aren't the replay's own.
        self.at = None  # The time of the recorded turn it's at, which Assist reads (_as_recorded).
        self.log = []  # What HA logged inside the sandbox (_Log).
        self.device_ids = {}  # Recorded device ID -> its ID in the sandbox.
        self.moment = {}  # The home as seeded (home_now), which each turn's home is told against (run_turn).
        self._dir = None  # Made and removed on the sandbox's own thread, never blocking the main loop.

    @classmethod
    async def start(cls, time_zone):
        began = time.monotonic()
        sandbox = cls(time_zone)
        outer = asyncio.get_running_loop()
        ready = outer.create_future()
        log = _Log(sandbox)
        # HA's own loop, with its executor, ended as its runner ends one.
        create_loop, cancel_tasks, cancel_timeout = map(resolve, ("event_loop", "cancel_tasks", "cancel_timeout"))

        def run():
            try:
                sandbox._dir = tempfile.TemporaryDirectory(prefix="assist-improver-sandbox-")
                loop = sandbox.loop = create_loop()
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
                        cancel_tasks(loop, cancel_timeout)
                        loop.run_until_complete(loop.shutdown_asyncgens())
                        loop.run_until_complete(loop.shutdown_default_executor())
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
        _timed("start", time.monotonic() - began)
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
        resolve("skip_pip")
        hass.config.skip_pip = True  # Nothing is installed from here (DETAILS P11.8.3): the worker's Python is HA's.
        await hass.config.async_set_time_zone(self.time_zone)  # The process's too: templates' now() reads it.
        dt_util.now = _now  # The process's clock: the recorded turn's time where Assist reads it.
        resolve("loader")(hass)
        hass.config_entries = resolve("config_entries")(hass, {})
        if not await resolve("base_functionality")(hass):
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
        no emulation (unsimulated), rather than acting on no device."""
        for domain in {*DEVICE_SERVICES, *domains}:
            others = {s for s, handler in self.hass.services.async_services_for_domain(domain).items()
                      if handler.supports_response is SupportsResponse.NONE} if domain in domains else set()
            for service in {*DEVICE_SERVICES.get(domain, ()), *others}:
                self.hass.services.async_register(domain, service, self._device_command)

    @callback
    def state_changed(self, event):
        old, new = event.data.get("old_state"), event.data.get("new_state")
        if self.observing or old is None or new is None or new.domain in NOT_THE_HOME:
            return
        if old.state != new.state:
            self.transitions.append({"entity_id": new.entity_id, "old": old.state, "new": new.state})

    def stand_in_missing(self, scripts):
        """The actions the household's scripts name that the sandbox has no service for (a phone's notification, a
        scene it doesn't load): unsimulated when a script reaches one, and the script runs on, as it would at home."""
        from .analyzer import speaking
        for name in _actions(scripts or {}):
            domain, _, service = name.partition(".")
            if service and not self.hass.services.has_service(domain, service):  # A legacy engine's say: spoken.
                self.hass.services.async_register(domain, service, functools.partial(_speak, self.hass)
                                                  if speaking(name) else self._no_service)

    async def _no_service(self, call):
        self.unsimulated.append(f"{call.domain}.{call.service}")

    async def _device_command(self, call):
        """A device's driver, emulated: apply the command's effect to the sandbox's state."""
        from homeassistant.helpers.service import async_extract_entity_ids
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
# Never set up in a sandbox, whatever Assist's tools name (DETAILS P11.8.3): the Supervisor's integration, whose
# services act on the host (reboot, shut down, restore a backup).
NEVER = {"hassio"}


def seed_options(today, turn):
    """What today's setup (replay.setup_now's) says the sandbox needs for a recorded request: the integrations Assist's
    tools come from, the request's device when it runs timers, and whether Assist can file the conversation."""
    from .llm import FileImprovement
    names, device = today["tools"], turn["prepared"]["context"].get("device_id")
    return {"integrations": sorted({n.split("__", 1)[0] for n in names if "__" in n} - INTEGRATION_TOOLS),
            "timer_device": device if device and device in today["timers"] else None,
            "filing": FileImprovement.name in names}


async def seed(hass, source, *, user_name=None, integrations=(), timer_device=None, filing=False, scripts=None,
               automations=None, exposure_changes=None, alias_changes=None, alias_removals=None, moving=()):
    """Make the sandbox the home a snapshot holds (snapshot.at_moment's: today's setup at a recorded moment): its areas,
    devices, entities (names, aliases, areas, a sensor's display precision), states and what Assist can see; the
    integrations Assist's tools come from; then its scripts and automations, which create their own entities, each
    automation on or off as the snapshot has it. exposure_changes ({entity_id: exposed}) and alias_changes
    ({entity_id: [aliases added]}, alias_removals {entity_id: [aliases removed]}) apply on top. moving: the devices
    that report their movement (simulation.reports_movement). Runs in the sandbox's loop. filing: Assist had the
    improver's filing tool, which answers here as filing does and files nothing (llm.ReplayedFiling)."""
    from homeassistant.components.intent import async_register_timer_handler
    from homeassistant.core import CoreState
    from homeassistant.helpers import area_registry as ar, entity_registry as er
    from homeassistant.setup import async_setup_component
    expose = resolve("sandbox_exposure")  # No public API exposes an entity, nor runs HA without starting it.
    resolve("running_state")
    _run_at(hass, source)
    hass.config.location_name = source.get("home_name") or hass.config.location_name
    # HA's chat log names the signed-in person; the sandbox has no accounts, only the recorded person.
    hass.auth = SimpleNamespace(async_get_user=_user(user_name))
    # Scripts and automations create their own entities once loaded; everything else is seeded here.
    own = {domain for domain, loaded in (("script", scripts), ("automation", automations)) if loaded}
    hass.data[SANDBOX_KEY].moving = set(moving)
    if filing:
        from .llm import REPLAYED_FILING
        hass.data[REPLAYED_FILING] = True
        hass.config.components.add(DOMAIN)  # Home Assistant's Assist asks the platforms of loaded integrations.
    for integration in integrations:  # Their Assist tools (lights, media, timers, ...), without their devices.
        if integration in own or integration in NEVER:
            continue  # Set up below with the household's own (set up empty first, it would never load them), or never.
        if not await async_setup_component(hass, integration, {}):
            raise RuntimeError(f"The sandbox could not set up {integration}")
    # The devices seeded as states below: their integrations' commands reach the emulated drivers. Scripts and
    # automations are entities of their own here, and to-do lists aren't devices.
    hass.data[SANDBOX_KEY].emulate_devices({e.split(".", 1)[0] for e in source.get("states") or {}} - own - {"todo"})
    if "script" not in own and any(e.startswith("script.") for e in source.get("states") or {}):
        # Scripts the sandbox wasn't given (their configuration unread) never run: a command to one is one the
        # emulated home has no model for, so it's flagged like any other.
        for service in ("turn_on", "turn_off", "toggle"):
            hass.services.async_register("script", service, hass.data[SANDBOX_KEY]._device_command)
    areas = {area_id: ar.async_get(hass).async_create(area["name"], aliases=set(area.get("aliases") or [])).id
             for area_id, area in (source.get("areas") or {}).items()}
    devices = hass.data[SANDBOX_KEY].device_ids = _seed_devices(hass, source.get("devices") or {}, areas)
    if timer_device:  # The request's device runs timers, in the registry or not.
        async_register_timer_handler(hass, devices.get(timer_device, timer_device), lambda *args: None)
    _hold_delayed_commands(hass)
    registry = er.async_get(hass)
    for entity_id, entry in (source.get("entities") or {}).items():
        if entry.get("disabled_by") or entity_id not in source["states"] or entity_id.split(".", 1)[0] in own:
            continue
        domain, object_id = entity_id.split(".", 1)
        registry.async_get_or_create(domain, entry.get("platform") or "recorded", entity_id,
                                     suggested_object_id=object_id, supported_features=(
                                         source["states"][entity_id].get("attributes") or {}).get(
                                         "supported_features") or 0)  # As the device reports them.
    for entity_id, state in source["states"].items():
        if entity_id.split(".", 1)[0] not in own:
            hass.states.async_set(entity_id, state["state"], state.get("attributes") or {})
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
    for wanted in ("on", "off") if automations else ():  # Each on or off as the home has it, not as configured.
        if ids := [e for e, state in (source.get("states") or {}).items() if e.startswith("automation.")
                   and state["state"] == wanted and getattr(hass.states.get(e), "state", wanted) != wanted]:
            await hass.services.async_call("automation", f"turn_{wanted}", {"entity_id": ids}, blocking=True)
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
            expose(hass, "conversation", entity_id, bool(exposed))
    sandbox = hass.data[SANDBOX_KEY]
    hass.bus.async_listen("state_changed", sandbox.state_changed)
    await hass.async_block_till_done()
    sandbox.moment = home_now(hass)


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
    and config_subentry_id). Nothing is installed: the provider's package is the one Home Assistant has. The entry is
    saved only in the sandbox's throwaway folder. The agent's entity id."""
    from datetime import datetime
    from types import MappingProxyType
    from homeassistant.components import conversation
    from homeassistant.config_entries import ConfigEntryDisabler
    from homeassistant.helpers import entity_registry as er
    from homeassistant.util.enum import try_parse_enum
    began = time.monotonic()
    ConfigEntry = resolve("config_entry")  # Built as HA builds one from its storage: no public API does.
    registry, entity_id = er.async_get(hass), agent["entity_id"]
    # The seeded home holds the agent too, as a state of its own: it gives way to the agent itself.
    if registry.async_get(entity_id) is not None:
        registry.async_remove(entity_id)
    hass.states.async_remove(entity_id)
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
    _stand_in_apis(hass, entry["subentries"])
    made = registry.async_get_or_create("conversation", agent["platform"], agent["unique_id"],
                                        suggested_object_id=entity_id.split(".", 1)[1], config_entry=loaded,
                                        config_subentry_id=agent["config_subentry_id"])
    if made.entity_id != entity_id:
        raise RuntimeError(f"The sandbox gave the assistant {made.entity_id}, not {entity_id}")
    set_up = await hass.config_entries.async_setup(loaded.entry_id)
    await hass.async_block_till_done()
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
    _timed("assistant", time.monotonic() - began)
    return entity_id


def _stand_in_apis(hass, subentries):
    """The LLM APIs an assistant uses beside Assist that the sandbox doesn't have (another integration's, a remote
    server's): each stands in with no tools, so the assistant runs with Assist's; a call to one of the others gets
    Home Assistant's own not-found."""
    from homeassistant.helpers import llm

    class StandIn(llm.API):
        async def async_get_api_instance(self, llm_context):
            return llm.APIInstance(api=self, api_prompt="", llm_context=llm_context, tools=[])
    have = {api.id for api in llm.async_get_apis(hass)}
    for subentry in subentries:
        named = subentry["data"].get("llm_hass_api") or []
        for api_id in [named] if isinstance(named, str) else named:
            if api_id not in have:
                llm.async_register_api(hass, StandIn(hass=hass, id=api_id, name=api_id))
                have.add(api_id)


def _hold_delayed_commands(hass):
    """A timer's command for when it ends has no emulation (HA would hand it to the conversation agent that set it):
    the timer runs without it."""
    timers = resolve("timers")  # No public API holds a timer's command back.
    resolve("timer_start")
    resolve("timer_device")
    if (manager := hass.data.get(timers)) is None:
        return
    start = manager.start_timer

    def start_timer(device_id, *args, conversation_command=None, conversation_agent_id=None, **kwargs):
        if conversation_command and (device_id is None or not manager.is_timer_device(device_id)):
            return None
        return start(device_id, *args, **kwargs)
    manager.start_timer = start_timer


def _stand_in_speakers(hass):
    """Home Assistant's relative volume intent turns a media player's volume through the device itself, not a command:
    each one seeded here has a stand-in its component hands out, whose volume goes to the emulated driver as the
    command it stands for."""
    players = resolve("media_players")  # No public API hands out an entity of one's own.
    resolve("media_player_entities")

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
    if (component := hass.data.get(players)) is not None:
        for entity_id in hass.states.async_entity_ids("media_player"):
            with relied_on("media_player_entities"):
                component._entities.setdefault(entity_id, Speaker(entity_id))  # noqa: SLF001 - as a platform adds one.


# An announcement on devices (DETAILS P9.10.5) leaves no state to emulate: each speaking service (analyzer.speaking)
# is taken as done, and what it said or played where is logged, whatever called it (a broadcast, a script), for a
# replay to judge an action by.
def _emulate_speaking(hass):
    """Registered after the integrations' own and the emulated drivers, before the household's scripts load (a legacy
    engine's say, configured in YAML, never loads here: stand_in_missing takes it)."""
    from .analyzer import SPEAKING
    for name in SPEAKING:
        hass.services.async_register(*name.split("."), functools.partial(_speak, hass))


async def _speak(hass, call):
    """Log what a speaking call said or played: {service, message, targets}, with media for an announcement's audio
    (a chime); tts.speak speaks with its engine on its media players, a legacy engine's say on the media players it
    targets, the others on the entities they target. As Home Assistant's entity services skip an entity that's missing
    or unavailable, nothing is spoken there, and a satellite named that can't announce (or start a conversation)
    refuses the call. A media player's play_media that's neither a spoken message nor an announcement has no
    emulation."""
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
    elif call.domain == "media_player":
        targets = targeted
        media = data.get("media_content_id") or (data.get("media") or {}).get("media_content_id")
        message = _tts_message(media)
        if message is None and not cv.boolean(data.get("announce") or False):
            return await sandbox._device_command(call)  # noqa: SLF001 - the driver's, as for any other command.
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
    data = {k: v for k, v in data.items() if k not in ("entity_id", "device_id", "area_id", "floor_id", "label_id")}
    sandbox.commands += [{"entity_id": e, "service": service, "data": data} for e in targets]  # A command to each.


def _tts_message(media):
    """The message a text-to-speech media source speaks (media-source://tts/<engine>?message=...), else None."""
    from yarl import URL
    if not isinstance(media, str) or not media.startswith("media-source://tts"):
        return None
    return URL(media).query.get("message")


def _aliases(entry):
    """The names as resolved when the snapshot was read."""
    return list(entry.get("aliases") or [])


def _seed_devices(hass, recorded, areas):
    """The recorded devices, under one recorded config entry (devices need one). {recorded id: sandbox id}"""
    from types import MappingProxyType
    from homeassistant.config_entries import ConfigEntryState
    from homeassistant.helpers import device_registry as dr
    with relied_on("config_entry"):
        entry = resolve("config_entry")(
            domain="assist_improver_recorded", title="Recorded home", data={}, source="user", version=1,
            minor_version=1, options={}, unique_id=None, subentries_data=None, discovery_keys=MappingProxyType({}),
            state=ConfigEntryState.LOADED)
    with relied_on("config_entries"):
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
    from homeassistant.components.conversation import ChatLog
    chat_log = ChatLog(hass, "assist-improver-sandbox-prompt")
    with _as_recorded(hass):
        await chat_log.async_provide_llm_data(sandbox_context(hass, llm_context), user_llm_hass_api=api_id,
                                              user_llm_prompt=prompt)
    return chat_log.content[0].content, chat_log.llm_api


MARKED = ("transitions", "commands", "spoken", "unsimulated")  # A turn's own lists (run_turn).


async def run_turn(hass, text, conversation_id, context, language, agent_id, device_id=None, watch=()):
    """One Assist turn as Home Assistant runs it at home, on the household's assistant loaded here (load_assistant),
    at the recorded turn's time: HA opens the chat session and log as its pipeline does, the assistant renders its
    prompt, builds its tools, asks its model and runs each tool call here, at once, as it comes. Its answer: what the
    turn added to the chat log after the person's words (each as HA gives it), what the assistant was given (its
    system prompt, its tools' names), the provider's usage as the assistant traces it, the watched entities'
    attributes before and after, and the home once everything the turn set off has run (snapshot): its changes,
    commands and speech, and its commands with no emulation (unemulated), the turn's own."""
    from homeassistant.components import conversation
    from homeassistant.core import Context
    from homeassistant.helpers import chat_session
    from .serialization import plain
    sandbox, usage = hass.data[SANDBOX_KEY], []
    before = {e: dict(state.attributes) for e in watch if (state := hass.states.get(e)) is not None}
    marks = {k: len(getattr(sandbox, k)) for k in MARKED}
    with _as_recorded(hass), chat_session.async_get_chat_session(  # Its owner: whoever says the turn (async_converse).
            hass, sandbox.conversations.get(conversation_id, conversation_id), context=None) as session, \
            conversation.async_get_chat_log(hass, session) as chat_log:
        sandbox.conversations[conversation_id] = session.conversation_id
        start = len(chat_log.content)
        resolve("chat_log_trace")  # What a provider reports of a turn reaches only the chat log's trace.
        traced = chat_log.async_trace

        def trace(agent_details):  # As the assistant reports it, provider-neutral (absent: unknown, never zero).
            if isinstance(agent_details.get("stats"), dict):
                usage.append(plain(agent_details["stats"]))
            traced(agent_details)
        chat_log.async_trace = trace
        result = await conversation.async_converse(
            hass, text, session.conversation_id, Context(**context), language=language, agent_id=agent_id,
            device_id=sandbox.device_ids.get(device_id, device_id) if device_id else None)
        added = chat_log.content[start:]
        if added and added[0].role == "user":
            added = added[1:]
        api = chat_log.llm_api
        given = {"prompt": chat_log.content[0].content if chat_log.content else None,
                 "tools": [t.name for t in api.tools] if api else []}
    found = await snapshot(hass, watch)
    return {"content": plain([c.as_dict() for c in added]), "reply": plain(result.response.as_dict()),
            "given": given, "usage": usage, "before": plain(before), "attributes": plain(found["attributes"]),
            **{k: plain(found[k][marks[k]:]) for k in marks if k != "unsimulated"},
            "unemulated": found["unsimulated"][marks["unsimulated"]:], "home": moved(sandbox.moment, home_now(hass))}


def home_now(hass):
    """The home's states now, each {state, attributes} in plain form: Assist's own entities aren't the home."""
    from .serialization import plain
    return {s.entity_id: {"state": s.state, "attributes": plain(dict(s.attributes))} for s in hass.states.async_all()
            if s.domain not in NOT_THE_HOME}


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
    sandbox, context = hass.data[SANDBOX_KEY], sandbox_context(hass, llm_context)
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
    """One of Assist's tools, called in the sandbox at the recorded turn's time: HA's date and time tool answers with
    it, and what the call sets off (a script, the automations its changes trigger) reads it."""
    with _as_recorded(hass):
        return await api.async_call_tool(tool_input)


async def observe(hass, source, moves=()):
    """Move on to the next recorded turn: its time, and what others in the home moved since, on top of what the replay
    did itself. moves: each skipped turn's then this one's, in order (simulation.moved_by_others). What the moves set
    off is waited for, as others' doing too."""
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
        await hass.async_block_till_done()
    finally:
        sandbox.observing = False


async def snapshot(hass, attributes=()):
    """The sandbox's home once what's running has finished (a script's delay too): states, the replay's state changes,
    its device commands and what it spoke; the attributes of the entities named (a light's brightness a goal asks
    for)."""
    from copy import deepcopy
    sandbox = hass.data[SANDBOX_KEY]
    await hass.async_block_till_done()
    return {"states": {s.entity_id: s.state for s in hass.states.async_all()},
            "attributes": {e: dict(state.attributes) for e in attributes if (state := hass.states.get(e)) is not None},
            "transitions": deepcopy(sandbox.transitions), "commands": deepcopy(sandbox.commands),
            "unsimulated": list(sandbox.unsimulated), "spoken": deepcopy(sandbox.spoken)}
