"""A sandboxed Home Assistant instance: HA's own engine, acting on a simulated home.

Each sandbox is a separate HomeAssistant object in its own thread and event
loop, so the real instance stays the one HA's helpers find. It runs HA's own
integrations (intents, the Assist API, scripts, automations) over a copy of
the home; only the commands to devices are emulated (devices.py). It has no
network listener and no device integrations, so nothing in it can reach a real
device. It lives only as long as one replay.
"""

import asyncio
from contextlib import suppress
import tempfile
import threading
from types import SimpleNamespace

from homeassistant.core import callback

from . import devices

SANDBOX_KEY = "assist_improver_sandbox"
RUNNING = set()  # Sandboxes not yet stopped; stop_all() ends them when the improver unloads.
THREAD_NAME = "assist-improver-sandbox"
SANDBOX_LOG_LIMIT = 200
DEVICE_SERVICES = {
    "light": ("turn_on", "turn_off", "toggle"),
    "switch": ("turn_on", "turn_off", "toggle"),
    "fan": ("turn_on", "turn_off", "toggle", "set_percentage", "set_preset_mode", "increase_speed",
            "decrease_speed", "oscillate", "set_direction"),
    "media_player": ("turn_on", "turn_off", "toggle", "media_play", "media_pause", "media_stop",
                     "media_play_pause", "volume_set", "volume_up", "volume_down", "volume_mute", "select_source"),
    "climate": ("turn_on", "turn_off", "set_hvac_mode", "set_temperature", "set_fan_mode", "set_preset_mode",
                "set_humidity", "set_swing_mode"),
    "cover": ("open_cover", "close_cover", "stop_cover", "set_cover_position"),
    "valve": ("open_valve", "close_valve", "stop_valve", "set_valve_position"),
    "lock": ("lock", "unlock", "open"),
    "vacuum": ("start", "pause", "stop", "return_to_base", "clean_area", "clean_spot"),
    "select": ("select_option",), "input_select": ("select_option",),
    "number": ("set_value",), "input_number": ("set_value",),
    "input_boolean": ("turn_on", "turn_off", "toggle"),
    "humidifier": ("turn_on", "turn_off", "set_humidity", "set_mode"),
    "water_heater": ("turn_on", "turn_off", "set_temperature", "set_operation_mode"),
    "siren": ("turn_on", "turn_off"), "remote": ("turn_on", "turn_off"),
}


def _forget(hass):
    """Drop what the sandbox left in Python's process-wide caches, so nothing outlives it.

    HA memoises many helpers per instance (registries, translations, template helpers); any instance that ran
    leaves entries there. Found through Python's reference tracking, so HA's list of such helpers isn't
    hard-coded here.
    """
    import functools
    import gc
    from homeassistant.helpers.template import states
    for cache in (states.CACHED_TEMPLATE_LRU, states.CACHED_TEMPLATE_NO_COLLECT_LRU):
        for key in [k for k, v in cache.items() if getattr(v, "_hass", None) is hass]:
            del cache[key]
    keys = [r for r in gc.get_referrers(hass) if isinstance(r, tuple)]
    holders = [r for r in gc.get_referrers(*keys) if isinstance(r, dict)] if keys else []
    for owner in gc.get_referrers(*holders) if holders else []:
        if isinstance(owner, functools._lru_cache_wrapper):
            owner.cache_clear()
    gc.collect()


class _SandboxLogs:
    """A log filter: records from a sandbox's thread go to that sandbox, not to Home Assistant's own log."""

    def filter(self, record):
        if record.threadName != THREAD_NAME:
            return True
        for sandbox in list(RUNNING):
            if sandbox.thread is not None and sandbox.thread.ident == record.thread:
                if len(sandbox.log) < SANDBOX_LOG_LIMIT:
                    sandbox.log.append(f"{record.levelname} {record.name}: {record.getMessage()}"[:500])
                break
        return False


_LOG_FILTER = _SandboxLogs()


def _keep_logs_in_sandboxes():
    import logging
    for handler in logging.getLogger().handlers:
        if _LOG_FILTER not in handler.filters:
            handler.addFilter(_LOG_FILTER)


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
        self.commands, self.unsimulated, self.transitions = [], [], []
        self.observing = False  # True while recorded changes are applied, which aren't the replay's own.
        self.log = []  # What HA logged inside the sandbox; kept here, out of the real instance's log.
        self.device_ids = {}  # Recorded device ID -> its ID in the sandbox.
        self._dir = None  # Made and removed on the sandbox's own thread, never blocking HA's loop.

    @classmethod
    async def start(cls, time_zone):
        sandbox = cls(time_zone)
        ready = asyncio.get_running_loop().create_future()
        outer = asyncio.get_running_loop()

        def run():
            from homeassistant import runner
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

        _keep_logs_in_sandboxes()
        sandbox.thread = threading.Thread(target=run, name=THREAD_NAME, daemon=True)
        sandbox.thread.start()
        RUNNING.add(sandbox)
        try:
            await ready
        except BaseException:
            await sandbox.stop()
            raise
        return sandbox

    async def run(self, fn, *args):
        """Run fn(hass, *args) in the sandbox's loop and wait for its result."""
        return await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(fn(self.hass, *args), self.loop))

    async def stop(self):
        RUNNING.discard(self)
        if self.loop is None or self.loop.is_closed():
            return
        with suppress(Exception):
            await self.run(lambda hass: hass.async_stop(force=True))
        self.loop.call_soon_threadsafe(self.loop.stop)
        await asyncio.get_running_loop().run_in_executor(None, self.thread.join, 30)
        _forget(self.hass)
        self.hass = None

    async def _setup(self):
        from homeassistant import bootstrap, config_entries, loader
        from homeassistant.core import HomeAssistant
        from homeassistant.helpers import frame
        from homeassistant.setup import async_setup_component
        hass = self.hass = HomeAssistant(self._dir.name)  # This thread's instance only.
        hass.data[SANDBOX_KEY] = self
        await hass.config.async_set_time_zone(self.time_zone)
        loader.async_setup(hass)
        hass.config_entries = config_entries.ConfigEntries(hass, {})
        real = frame._hass.hass
        try:
            if not await bootstrap.async_load_base_functionality(hass):  # HA's own start: registries, triggers, ...
                raise RuntimeError("The sandbox could not load Home Assistant's base functionality")
        finally:
            frame._hass.hass = real  # HA's frame helper is process-wide; it stays with the real instance.
        # Intents register a web view; the sandbox has no web server, so their views go nowhere.
        hass.http = _NoWebServer()
        hass.config.components.add("http")
        for component in ("homeassistant", "intent", "llm"):
            if not await async_setup_component(hass, component, {}):
                raise RuntimeError(f"The sandbox could not set up {component}")
        self.emulate_devices()

    def emulate_devices(self):
        """Device commands go to the emulated drivers, also after an integration registers its own services."""
        for domain, services in DEVICE_SERVICES.items():
            for service in services:
                self.hass.services.async_register(domain, service, self._device_command)

    @callback
    def state_changed(self, event):
        old, new = event.data.get("old_state"), event.data.get("new_state")
        if self.observing or old is None or new is None or old.state == new.state:
            return
        if new.domain not in ("conversation", "ai_task", "tts", "stt", "wake_word", "assist_satellite"):
            self.transitions.append({"entity_id": new.entity_id, "old": old.state, "new": new.state})

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
            self.hass.states.async_set(entity_id, result[0], result[1], context=call.context)


INTEGRATION_TOOLS = {"intent", "homeassistant", "llm"}  # Tool prefixes HA's core sets up, not an integration.


def seed_options(turn):
    """What a recorded request's tools say the sandbox needs: their integrations, and a device that runs timers."""
    prepared = turn["prepared"]
    names = [t["name"] for t in prepared["tools"]]
    return {"integrations": sorted({n.split("__", 1)[0] for n in names if "__" in n} - INTEGRATION_TOOLS),
            "timer_device": prepared["context"].get("device_id") if any(n.endswith("StartTimer") for n in names)
            else None}


async def seed(hass, source, *, user_name=None, integrations=(), timer_device=None, scripts=None, automations=None,
               exposure_changes=None, alias_changes=None):
    """Make the sandbox the recorded home: its areas, devices, entities (names, aliases, areas), states and what
    Assist could see; the integrations whose Assist tools the request had; then its scripts and automations,
    which create their own entities. exposure_changes ({entity_id: exposed}) and alias_changes ({entity_id:
    [aliases]}) apply on top of the recorded setup. Runs in the sandbox's loop."""
    from homeassistant.components.homeassistant.exposed_entities import async_expose_entity
    from homeassistant.components.intent import async_register_timer_handler
    from homeassistant.core import CoreState
    from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er
    from homeassistant.setup import async_setup_component
    hass.config.location_name = source.get("home_name") or hass.config.location_name
    # HA's chat log names the signed-in person; the sandbox has no accounts, only the recorded person.
    hass.auth = SimpleNamespace(async_get_user=_user(user_name))
    for integration in integrations:  # Their Assist tools (lights, media, timers, ...), without their devices.
        if not await async_setup_component(hass, integration, {}):
            raise RuntimeError(f"The sandbox could not set up {integration}")
    hass.data[SANDBOX_KEY].emulate_devices()
    areas = {area_id: ar.async_get(hass).async_create(area["name"], aliases=set(area.get("aliases") or [])).id
             for area_id, area in (source.get("areas") or {}).items()}
    devices = hass.data[SANDBOX_KEY].device_ids = _seed_devices(hass, source.get("devices") or {}, areas)
    if timer_device in devices:  # The recorded device could run timers (its tools offered them).
        async_register_timer_handler(hass, devices[timer_device], lambda *args: None)
    # Scripts and automations create their own entities once loaded; everything else is seeded here.
    own = {domain for domain, loaded in (("script", scripts), ("automation", automations)) if loaded}
    registry = er.async_get(hass)
    for entity_id, entry in (source.get("entities") or {}).items():
        if entry.get("disabled_by") or entity_id not in source["states"] or entity_id.split(".", 1)[0] in own:
            continue
        domain, object_id = entity_id.split(".", 1)
        registry.async_get_or_create(domain, entry.get("platform") or "recorded", entity_id,
                                     suggested_object_id=object_id)
    for entity_id, state in source["states"].items():
        if entity_id.split(".", 1)[0] not in own:
            hass.states.async_set(entity_id, state["state"], state.get("attributes") or {})
    # Running, without firing HA's start events: automations are live, but none triggers on a start. They load
    # after the states are seeded, so seeding triggers none either.
    hass.set_state(CoreState.running)
    if scripts and not await async_setup_component(hass, "script", {"script": scripts}):
        raise RuntimeError("The sandbox could not load the scripts")
    if automations and not await async_setup_component(hass, "automation", {"automation": automations}):
        raise RuntimeError("The sandbox could not load the automations")
    await hass.async_block_till_done()
    for entity_id, entry in (source.get("entities") or {}).items():
        if registry.async_get(entity_id) is not None:
            registry.async_update_entity(
                entity_id, name=entry.get("name"), original_name=entry.get("original_name"), aliases=_aliases(entry),
                area_id=areas.get(entry.get("area_id")), device_id=devices.get(entry.get("device_id")),
                hidden_by=er.RegistryEntryHider(entry["hidden_by"]) if entry.get("hidden_by") else None)
    for entity_id, added in (alias_changes or {}).items():
        if (entry := registry.async_get(entity_id)) is None:
            raise RuntimeError(f"{entity_id} isn't in the sandbox")
        registry.async_update_entity(entity_id, aliases=[*entry.aliases, *added])
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


def _aliases(entry):
    """The names as resolved when recorded; 0.1.0 recordings kept HA's raw list, its computed name as a 0."""
    from homeassistant.helpers import entity_registry as er
    if "alias_entries" in entry:
        return list(entry.get("aliases") or [])
    return [a if isinstance(a, str) else er.COMPUTED_NAME for a in entry.get("aliases") or [0]]  # Its own name.


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
    async def get_user(user_id):
        return SimpleNamespace(id=user_id, name=name) if name else None
    return get_user


def sandbox_context(hass, llm_context):
    """The recorded request's context, with its device as the sandbox knows it."""
    from dataclasses import replace
    device_id = hass.data[SANDBOX_KEY].device_ids.get(llm_context.device_id, llm_context.device_id)
    return replace(llm_context, device_id=device_id)


async def system_prompt(hass, llm_context, api_id, prompt):
    """Assist's system prompt and tools, as HA builds them for this request in the sandbox."""
    from homeassistant.components.conversation.chat_log import ChatLog
    chat_log = ChatLog(hass, "assist-improver-sandbox-prompt")
    await chat_log.async_provide_llm_data(sandbox_context(hass, llm_context), user_llm_hass_api=api_id,
                                          user_llm_prompt=prompt)
    return chat_log.content[0].content, chat_log.llm_api


async def observe(hass, source, exclude=()):
    """Move entities the replay never changed to their next recorded values (others in the home moved them).
    exclude: entities the recorded assistant changed, whose next values are the original run's, not others'."""
    sandbox = hass.data[SANDBOX_KEY]
    touched = {c["entity_id"] for c in sandbox.commands} | {t["entity_id"] for t in sandbox.transitions} | set(exclude)
    sandbox.observing = True
    try:
        for entity_id, state in source["states"].items():
            current = hass.states.get(entity_id)
            if entity_id not in touched and current is not None and (
                    current.state != state["state"] or dict(current.attributes) != (state.get("attributes") or {})):
                hass.states.async_set(entity_id, state["state"], state.get("attributes") or {})
        await hass.async_block_till_done()
    finally:
        sandbox.observing = False


async def snapshot(hass):
    """The sandbox's home now: states, the replay's state changes and its device commands."""
    from copy import deepcopy
    sandbox = hass.data[SANDBOX_KEY]
    await hass.async_block_till_done()
    return {"states": {s.entity_id: s.state for s in hass.states.async_all()},
            "transitions": deepcopy(sandbox.transitions), "commands": deepcopy(sandbox.commands),
            "unsimulated": list(sandbox.unsimulated)}
