"""A sandbox's devices: the stand-in integration (DETAILS P11.4), and the sandbox's no-op web server.

Both are integrations of the sandbox's own, written into its worker's own custom_components (write: no manifest of
theirs ships in the improver's package): `http`, the documented override of a built-in, which sets up no web server,
and `assist_improver_standin`, which stands in for every integration with entities in the home. A sandbox sets one
stand-in config entry up through its config flow per recorded config entry, titled the same (sandbox.seed). Its setup
moves that entry's devices (loaded with the registries, sandbox.Sandbox.start) to it, children following their
parent, so devices keep their ids, areas and names, then forwards to one platform per entity domain Home Assistant
has (homeassistant.const.Platform). Each platform adds its entities as instances of that domain's own entity class,
seeded from the recorded state and registry entry, so Home Assistant's entity services, intents and validation run
on them as on the household's devices. A command a stand-in takes reports back what was asked (_table: fields to
attributes, commands to states, in Home Assistant's own enums); one it can't take is reported as not simulated.
Stated gap: their entities' platform is the stand-in's, so a template asking by integration
(integration_entities('<domain>')) finds none; asking by an entry's title finds its stand-in's.
"""

import asyncio
from contextvars import ContextVar
import enum
import functools
import importlib
import inspect
import json
from pathlib import Path

from homeassistant.components.cover import CoverState
from homeassistant.components.lock import LockState
from homeassistant.components.valve import ValveState
from homeassistant.const import (ATTR_AREA_ID, ATTR_DEVICE_ID, ATTR_ENTITY_ID, ATTR_FLOOR_ID, ATTR_LABEL_ID,
                                 STATE_OFF, STATE_ON, STATE_UNAVAILABLE, Platform)
from homeassistant.exceptions import HomeAssistantError

from .happened import tts_message
from .serialization import plain

STANDIN = "assist_improver_standin"
TARGETS = (ATTR_ENTITY_ID, ATTR_DEVICE_ID, ATTR_AREA_ID, ATTR_FLOOR_ID, ATTR_LABEL_ID)  # A call's target, not its data.
# What a device that reports its movement shows on its way to each end, as Home Assistant's covers, valves and locks
# report it (DETAILS P6.2.7).
UNDERWAY = {CoverState.OPEN: CoverState.OPENING, CoverState.CLOSED: CoverState.CLOSING,
            ValveState.OPEN: ValveState.OPENING, ValveState.CLOSED: ValveState.CLOSING,
            LockState.LOCKED: LockState.LOCKING, LockState.UNLOCKED: LockState.UNLOCKING}
MOVERS = (Platform.COVER, Platform.VALVE, Platform.LOCK)
# The class standing in where a domain has several (an emitter, not a receiver; a scene, not its base).
CLASSES = {"infrared": "InfraredEmitterEntity", "radio_frequency": "RadioFrequencyTransmitterEntity", "scene": "Scene"}
# What a domain's class leaves to its integration without declaring it abstract (it raises until given): a calendar's
# next event (none known), a speech engine's languages (the home's), a light's colour modes (as it shows them, or as
# its registry entry has them; on and off alone for one with neither).
GIVEN = {"calendar": {"event": lambda self: None},
         "tts": {"default_language": lambda self: self.hass.config.language,
                 "supported_languages": lambda self: [self.hass.config.language]},
         "light": {"supported_color_modes": lambda self: self._attributes.get("supported_color_modes") or (
             self._capabilities or {}).get("supported_color_modes") or ["onoff"]}}
# Entity properties a stand-in sets from its registry entry, never from the state's attributes.
OWN = {"name", "icon", "device_class", "unit_of_measurement", "supported_features", "entity_category",
       "translation_key", "has_entity_name", "unique_id", "available", "state", "device_info", "entity_id",
       "capability_attributes", "state_attributes", "extra_state_attributes", "should_poll", "friendly_name"}
# The service call the task is making, as Home Assistant told of it in the task (sandbox.Sandbox.calling): (its
# sandbox, its index in the sandbox's calls).
CALLING = ContextVar("assist_improver_standin_calling", default=(None, None))
_IN_CALL = ContextVar("assist_improver_standin_in_call", default=None)  # The stand-in a service call is running on.


def passed_on():
    """A call made while a stand-in takes one: Home Assistant's own part of it (a speech engine speaking on its media
    players), which does it, so the stand-in's call is simulated (sandbox.Sandbox.calling)."""
    if (taker := _IN_CALL.get()) is not None:
        taker._handled = True


def write(folder):
    """The sandbox's own integrations, written into a custom_components folder (a worker's own)."""
    files = {
        "http/manifest.json": {"domain": "http", "name": "No web server", "version": "1"},
        "http/__init__.py": "from custom_components.assist_improver.standin import async_setup_http as async_setup\n",
        f"{STANDIN}/manifest.json": {"domain": STANDIN, "name": "Stand-in devices", "version": "1",
                                     "config_flow": True},
        f"{STANDIN}/__init__.py": "from custom_components.assist_improver.standin import async_setup_entry\n",
        f"{STANDIN}/config_flow.py": "from custom_components.assist_improver.standin import flow\n"
                                     "StandInFlow = flow()\n",
        **{f"{STANDIN}/{p.value}.py": "from custom_components.assist_improver.standin import platform\n"
                                      f"async_setup_entry = platform({p.value!r})\n" for p in Platform}}
    for name, content in files.items():
        path = Path(folder, name)
        path.parent.mkdir(exist_ok=True)
        path.write_text(content if isinstance(content, str) else json.dumps(content))


class NoWebServer:
    """hass.http for an instance with no web server: integrations' views and static paths go nowhere."""

    def __getattr__(self, name):
        async def ignored_async(*args, **kwargs):
            return None
        return ignored_async if name.startswith("async_") else (lambda *args, **kwargs: None)


async def async_setup_http(hass, config):
    hass.http = NoWebServer()
    return True


def flow():
    """The stand-in integration's config flow, made only where it's set up (a sandbox): an entry per recorded one."""
    from homeassistant.config_entries import ConfigFlow

    class StandInFlow(ConfigFlow, domain=STANDIN):
        async def async_step_import(self, data):
            return self.async_create_entry(title=data["title"], data=data)
    return StandInFlow


async def async_setup_entry(hass, entry):
    """A recorded config entry's stand-in: its devices move to it, then its platforms set its entities up."""
    from homeassistant.helpers import device_registry as dr
    from .sandbox import SANDBOX_KEY
    group = hass.data[SANDBOX_KEY].groups[entry.data["group"]]
    registry = dr.async_get(hass)
    for device in dr.async_entries_for_config_entry(registry, entry.data["group"]):
        registry.async_update_device(device.id, new_config_entry_id=entry.entry_id, new_config_subentry_id=None)
    await hass.config_entries.async_forward_entry_setups(entry, sorted(group["entities"]))
    # None, as Home Assistant asks: a boolean is reported through its one instance per process, another sandbox's
    # when tests serve them in one.


def platform(domain):
    """A domain's platform: the stand-ins of its entities in the entry's group (sandbox.seed)."""
    async def async_setup_entry(hass, entry, async_add_entities):
        from homeassistant.helpers import entity_registry as er
        from .sandbox import SANDBOX_KEY
        sandbox, registry = hass.data[SANDBOX_KEY], er.async_get(hass)
        async_add_entities([stand_in(hass, sandbox, domain, entity_id, registry.async_get(entity_id))
                            for entity_id in sandbox.groups[entry.data["group"]]["entities"][domain]])
    return async_setup_entry


def stand_in(hass, sandbox, domain, entity_id, entry):
    return _class(domain)(hass, sandbox, domain, entity_id, entry, sandbox.home["states"][entity_id])


@functools.cache
def standable(domain):
    """Whether a domain's integration sets its entities up from config entries, as the stand-in's are (an image
    processor's doesn't)."""
    return hasattr(importlib.import_module(f"homeassistant.components.{domain}"), "async_setup_entry")


@functools.cache
def entity_class(domain):
    """A domain's own entity class: the one its integration's package defines that every other derives from."""
    from homeassistant.helpers.entity import Entity
    package = f"homeassistant.components.{domain}"
    module = importlib.import_module(package)
    if domain in CLASSES:
        return getattr(module, CLASSES[domain])
    found = {c for c in _defined(module) if isinstance(c, type) and issubclass(c, Entity)
             and (c.__module__ == package or c.__module__.startswith(package + "."))}
    [base] = [c for c in found if not any(o is not c and o in found for o in c.__mro__)]
    return base


def _defined(module):
    """What a module defines, as it stands: another thread's import can add a submodule to it meanwhile."""
    return vars(module).copy().values()


@functools.cache
def _features(domain):
    """A domain's supported-features flags (LightEntityFeature), which its services check by flag; None for none."""
    module = importlib.import_module(f"homeassistant.components.{domain}")
    return next((c for c in _defined(module) if isinstance(c, type) and issubclass(c, enum.IntFlag)
                 and c.__name__.endswith("EntityFeature")), None)


@functools.cache
def _class(domain):
    base = entity_class(domain)
    space = {}
    for name in getattr(base, "__abstractmethods__", ()):  # What the household's integration would have given.
        if isinstance(getattr(base, name), property):
            space[name] = property(lambda self, n=name: self._attributes.get(n))
        else:
            space[name] = _unsimulated
    space |= {name: property(given) for name, given in GIVEN.get(domain, {}).items()}
    for command in _table().get(domain, {}):
        if (method := getattr(base, f"async_{command}", None)) is not None:
            space[f"async_{command}"] = _command_method(command, inspect.signature(method))
    return type(f"{base.__name__}StandIn", (_EXTRA.get(domain, StandIn), base), space)


async def _unsimulated(self, *args, **kwargs):
    raise NotImplementedError


def _command_method(command, signature):
    async def method(self, *args, **kwargs):
        bound = signature.bind(self, *args, **kwargs)
        fields = {}
        for name, value in list(bound.arguments.items())[1:]:
            kind = signature.parameters[name].kind
            fields.update(value if kind is inspect.Parameter.VAR_KEYWORD else {name: value})
        await self._command(command, fields)
    return method


@functools.cache
def _table():
    """What each command reports back: {domain: {command: (its new state, or a function of its fields; None: the
    state stays, and the attributes it sets, or a function of its fields and the attributes held; _GONE: it shows it
    no more)}}, in Home Assistant's own enums; its fields go to the attributes of the same name (FIELDS renames)."""
    from homeassistant.components.alarm_control_panel import AlarmControlPanelState as Alarm
    from homeassistant.components.lawn_mower import LawnMowerActivity as Mower
    from homeassistant.components.light import LightEntityStateAttribute
    from homeassistant.components.media_player import MediaPlayerEntityStateAttribute, MediaPlayerState as Media
    from homeassistant.components.vacuum import VacuumActivity as Vacuum
    on_off = {"turn_on": (STATE_ON, {}), "turn_off": (STATE_OFF, {})}
    kept = (None, {})

    def light_on(fields, held):  # Its colour mode: the colour asked for's, else the one it had, else its first.
        mode = next((m for f, m in colour_modes().items() if f in fields), None) or held.get("color_mode")
        return {"color_mode": mode or next(iter(held.get("supported_color_modes") or []), None)}
    return {
        "light": {"turn_on": (STATE_ON, light_on),  # Off, it shows none of a light's state attributes.
                  "turn_off": (STATE_OFF, dict.fromkeys(LightEntityStateAttribute))},
        "switch": on_off, "siren": on_off,
        "remote": {**on_off, "send_command": kept},
        "humidifier": {**on_off, "set_humidity": kept, "set_mode": kept},
        "fan": {**on_off, "set_percentage": (lambda f: STATE_OFF if f.get("percentage") == 0 else STATE_ON, {}),
                "set_preset_mode": (STATE_ON, {}), "oscillate": kept, "set_direction": kept},
        "climate": {"set_hvac_mode": (lambda f: f["hvac_mode"], {}),
                    "set_temperature": (lambda f: f.get("hvac_mode"), {}), "set_fan_mode": kept,
                    "set_preset_mode": kept, "set_humidity": kept, "set_swing_mode": kept,
                    "set_swing_horizontal_mode": kept},
        "water_heater": {"set_operation_mode": (lambda f: f["operation_mode"], {}),
                         "set_temperature": (lambda f: f.get("operation_mode"), {}), "turn_off": (STATE_OFF, {}),
                         "turn_away_mode_on": (None, {"away_mode": STATE_ON}),
                         "turn_away_mode_off": (None, {"away_mode": STATE_OFF})},
        "cover": {"open_cover": (CoverState.OPEN, {"current_position": 100}),
                  "close_cover": (CoverState.CLOSED, {"current_position": 0}),
                  "set_cover_position": (lambda f: CoverState.CLOSED if f["position"] == 0 else CoverState.OPEN, {}),
                  "open_cover_tilt": (None, {"current_tilt_position": 100}),
                  "close_cover_tilt": (None, {"current_tilt_position": 0}), "set_cover_tilt_position": kept},
        "valve": {"open_valve": (ValveState.OPEN, {"current_position": 100}),
                  "close_valve": (ValveState.CLOSED, {"current_position": 0}),
                  "set_valve_position": (lambda f: ValveState.CLOSED if f["position"] == 0 else ValveState.OPEN, {})},
        "lock": {"lock": (LockState.LOCKED, {}), "unlock": (LockState.UNLOCKED, {}), "open": (LockState.OPEN, {})},
        "media_player": {"turn_on": (STATE_ON, {}), "media_play": (Media.PLAYING, {}),
                         # Off, it shows none of what it plays (its group, if any, stays).
                         "turn_off": (Media.OFF, dict.fromkeys(set(MediaPlayerEntityStateAttribute) - {
                             MediaPlayerEntityStateAttribute.GROUP_MEMBERS}, _GONE)),
                         "media_pause": (Media.PAUSED, {}), "media_stop": (Media.IDLE, {}),
                         "set_volume_level": kept, "mute_volume": kept, "select_source": kept,
                         "select_sound_mode": kept, "set_shuffle": kept, "set_repeat": kept, "media_seek": kept},
        "vacuum": {"start": (Vacuum.CLEANING, {}), "pause": (Vacuum.PAUSED, {}), "stop": (Vacuum.IDLE, {}),
                   "return_to_base": (Vacuum.RETURNING, {}), "clean_spot": (Vacuum.CLEANING, {}),
                   "clean_segments": (Vacuum.CLEANING, {}), "set_fan_speed": kept},
        "lawn_mower": {"start_mowing": (Mower.MOWING, {}), "pause": (Mower.PAUSED, {}), "dock": (Mower.RETURNING, {})},
        "alarm_control_panel": {"alarm_disarm": (Alarm.DISARMED, {}), "alarm_trigger": (Alarm.TRIGGERED, {}),
                                **{f"alarm_arm_{m}": (getattr(Alarm, f"ARMED_{m.upper()}"), {})
                                   for m in ("away", "home", "night", "vacation", "custom_bypass")}},
        "select": {"select_option": (lambda f: f["option"], {})},
        "number": {"set_native_value": (lambda f: str(float(f["value"])), {})},
        "text": {"set_value": (lambda f: f["value"], {})},
        **{d: {"set_value": (lambda f: f["value"].isoformat(), {})} for d in ("date", "time", "datetime")},
        # Home Assistant sets their state itself (when last pressed, activated or sent) before the command.
        **{d: {c: (_NOW, {})} for d, c in (("button", "press"), ("scene", "activate"), ("notify", "send_message"))},
    }


@functools.cache
def colour_modes():
    """A light's colour fields (each the attribute it shows that colour in) by the colour mode each sets."""
    from homeassistant.components.light import ColorMode
    return {"color_temp_kelvin": ColorMode.COLOR_TEMP, "hs_color": ColorMode.HS, "rgb_color": ColorMode.RGB,
            "rgbw_color": ColorMode.RGBW, "rgbww_color": ColorMode.RGBWW, "xy_color": ColorMode.XY,
            "white": ColorMode.WHITE}


def attribute_of(domain, field):
    """The attribute a command's field sets: FIELDS's name for it (a cover's position: current_position), else its own."""
    name = FIELDS.get(field, field)
    return name.get(domain, field) if isinstance(name, dict) else name


@functools.cache
def trigger_only(domain):
    """Whether a domain's entities hold no state of their own to reach, only when they were last used or ran: a
    button's, a scene's or a notifier's (the classes whose state Home Assistant sets itself: _NOW), an event entity's,
    or a script, on only while it runs."""
    try:
        return domain == "script" or issubclass(entity_class(domain), _used())
    except (ImportError, ValueError):  # No one class its entities derive from.
        return False


@functools.cache
def _used():
    """The entity classes whose state says only when they were last used (trigger_only)."""
    from homeassistant.components.event import EventEntity
    return (*(entity_class(d) for d, commands in _table().items() if any(s is _NOW for s, _ in commands.values())),
            EventEntity)


@functools.cache
def reports(domain):
    """The attributes a domain's entity class reports, its state's and its capabilities', as Home Assistant's own enums
    of them name them (FanEntityStateAttribute: a fan's direction, which its class reads from current_direction)."""
    package = f"homeassistant.components.{domain}"
    return frozenset(member.value for c in _defined(importlib.import_module(package))
                     if isinstance(c, type) and issubclass(c, enum.StrEnum) and c.__module__.startswith(package)
                     and c.__name__.endswith(("StateAttribute", "CapabilityAttribute")) for member in c)


def held(domain, attribute):
    """Whether a domain's entities can hold an attribute a field sets: one its entity class reports (reports), not one
    following from the state (FROM_STATE, by the property it's read from: PROPERTIES; a water heater's operation_mode
    is its state)."""
    return attribute in reports(domain) and PROPERTIES.get(domain, {}).get(attribute, attribute) not in FROM_STATE


_NOW = object()  # A state Home Assistant's entity class sets itself: the time it was last used.
_GONE = object()  # An attribute a command leaves its device not showing.
# A command's fields by the attribute they set where its name differs ({field: attribute, or {domain: attribute}}):
# what an integration does with a field, which Home Assistant's classes don't say.
FIELDS = {"position": {"cover": "current_position", "valve": "current_position", "media_player": "media_position"},
          "tilt_position": "current_tilt_position", "volume": "volume_level", "mute": "is_volume_muted",
          "media_type": "media_content_type", "media_id": "media_content_id",
          "activity": {"remote": "current_activity"}}
# An attribute by the entity property Home Assistant's class reads it from where its name differs.
PROPERTIES = {"cover": {"current_position": "current_cover_position",
                        "current_tilt_position": "current_cover_tilt_position"},
              "valve": {"current_position": "current_valve_position"},
              "climate": {"temperature": "target_temperature", "target_temp_high": "target_temperature_high",
                          "target_temp_low": "target_temperature_low", "humidity": "target_humidity",
                          "target_temp_step": "target_temperature_step"},
              "water_heater": {"temperature": "target_temperature", "operation_mode": "current_operation",
                               "target_temp_high": "target_temperature_high",
                               "target_temp_low": "target_temperature_low"},
              "humidifier": {"humidity": "target_humidity"},
              "number": {"min": "native_min_value", "max": "native_max_value", "step": "native_step"},
              "text": {"min": "native_min", "max": "native_max"}}
# Entity properties Home Assistant's own logic reads that follow from the state (a toggle reads is_on; a number's value
# is its state).
FROM_STATE = {"value": lambda s: s, "is_on": lambda s: s == STATE_ON, "is_closed": lambda s: s == CoverState.CLOSED,
              "is_opening": lambda s: s == CoverState.OPENING, "is_closing": lambda s: s == CoverState.CLOSING,
              "is_locked": lambda s: s == LockState.LOCKED, "is_locking": lambda s: s == LockState.LOCKING,
              "is_unlocking": lambda s: s == LockState.UNLOCKING, "is_jammed": lambda s: s == LockState.JAMMED,
              "is_open": lambda s: s == LockState.OPEN, "hvac_mode": lambda s: s, "current_option": lambda s: s,
              "current_operation": lambda s: s, "alarm_state": lambda s: s, "activity": lambda s: s}


class StandIn:
    """A device's stand-in: its domain's own entity class (_class), seeded from its recorded state (its state and
    attributes written as recorded) and registry entry (what its platform writes into it: name, icon, class, unit,
    features, capabilities, category, translation key, device), reporting back each command it takes."""

    _attr_should_poll = False

    def __init__(self, hass, sandbox, domain, entity_id, entry, state):
        if "hass" in inspect.signature(super().__init__).parameters:
            super().__init__(hass)
        else:
            super().__init__()
        self._sandbox, self._domain, self.entity_id, self._handled = sandbox, domain, entity_id, False
        self._taken = set()  # The calls it took (_call), by their index in the sandbox's.
        self._failed = None  # The error the command it's taking failed with at home (async_request_call).
        attributes = state.get("attributes") or {}
        if entry is not None:
            from homeassistant.helpers import device_registry as dr
            self._attr_unique_id, self._attr_has_entity_name = entry.unique_id, entry.has_entity_name
            self._attr_name, self._attr_translation_key = entry.original_name, entry.translation_key
            self._attr_entity_category, self._attr_device_class = entry.entity_category, entry.original_device_class
            self._attr_icon, features = entry.original_icon, entry.supported_features
            self._unit, self._capabilities = entry.unit_of_measurement, entry.capabilities
            self.device_entry = dr.async_get(hass).async_get(entry.device_id) if entry.device_id else None
            if self._has("suggested_display_precision"):  # A sensor's, which its class writes back to the entry.
                self._attr_suggested_display_precision = (entry.options.get(domain) or {}).get(
                    "suggested_display_precision")
        else:  # One with no registry entry: as its state shows it.
            self._attr_name, self._attr_device_class = attributes.get("friendly_name"), attributes.get("device_class")
            self._attr_icon, features = attributes.get("icon"), attributes.get("supported_features") or 0
            self._unit, self._capabilities = attributes.get("unit_of_measurement"), None
        self._attr_supported_features = flags(features) if (flags := _features(domain)) else features
        self._attr_entity_picture = attributes.get("entity_picture")
        self._attr_attribution = attributes.get("attribution")
        self._attr_assumed_state = bool(attributes.get("assumed_state"))
        if domain in ("climate", "water_heater"):  # As the home shows its temperatures.
            self._attr_temperature_unit = hass.config.units.temperature_unit
        self._state, self._attributes = state["state"], dict(attributes)
        self._mirror()
        sandbox.standins[entity_id] = self

    def _mirror(self):
        """Home Assistant's class reads what it validates and decides from its properties: they follow the held
        state and attributes."""
        renamed = PROPERTIES.get(self._domain, {})
        for key, value in self._attributes.items():
            name = renamed.get(key, key)
            if name not in OWN and self._has(name):
                setattr(self, f"_attr_{name}", value)
        for name, derived in FROM_STATE.items():
            if hasattr(type(self), name) and name not in renamed.values():
                setattr(self, f"_attr_{name}", derived(self._state))
        self._attr_available = self._state != STATE_UNAVAILABLE

    def _has(self, name):
        """Whether Home Assistant's class has this property."""
        return hasattr(type(self), name) and not callable(getattr(type(self), name))

    @property
    def state(self):
        return self._state

    @property
    def capability_attributes(self):
        return self._capabilities

    @property
    def state_attributes(self):
        return None

    @property
    def extra_state_attributes(self):
        return self._attributes

    @property
    def unit_of_measurement(self):
        return self._unit

    async def async_request_call(self, coro):
        """A service call on this stand-in, as Home Assistant's entity services make it: logged as a command (the
        call's data, its target aside); refused as at home when the recording shows it failed there in the turn the
        sandbox is at (DETAILS P11.4); reported as not simulated when the stand-in can't take it."""
        domain, service, data = await self._call()
        name = f"{domain}.{service}"
        self._sandbox.commands.append({"entity_id": self.entity_id, "service": name,
                                       "data": plain({k: v for k, v in data.items() if k not in TARGETS})})
        failed = self._sandbox.failures.get((self.entity_id, self._sandbox.at))
        token, self._handled, self._failed = _IN_CALL.set(self), False, failed
        try:
            result = await coro
        except NotImplementedError:
            result = None
        finally:
            _IN_CALL.reset(token)
        if failed is not None:  # Refused before it changed anything (_refuse), or after Home Assistant's own part.
            raise HomeAssistantError(failed)
        if result is None and not self._handled:
            self._sandbox.unsimulated.append(f"{name} {self.entity_id}")
        return result

    async def _call(self):
        """The service call this stand-in is taking: the one its task made (CALLING), unless that's not one on it, or
        one it took already (a call made while Home Assistant tells of another is told of once that's done, in the
        task that told of the other: an automation's first action, set off by a change); then the last one on it it
        hasn't taken, once Home Assistant has told of it."""
        calls, (sandbox, index) = self._sandbox.calls, CALLING.get()
        if sandbox is not self._sandbox or index in self._taken or not self._on(calls[index]):
            await asyncio.sleep(0)
            index = next((i for i in reversed(range(len(calls))) if i not in self._taken and self._on(calls[i])), None)
        if index is None:
            return self._domain, "unknown", {}
        self._taken.add(index)
        return calls[index]

    def _on(self, call):
        """Whether a call is on this stand-in: one of its domain that names it, or names no entity."""
        domain, _, data = call
        named = data.get(ATTR_ENTITY_ID) or []
        named = [e.strip() for e in named.split(",")] if isinstance(named, str) else named
        return domain == self._domain and (not named or "all" in named or self.entity_id in named)

    async def _command(self, command, fields):
        """What a command reports back (_table): its new state, and the attributes its fields and the command set."""
        from homeassistant.helpers import config_validation as cv
        from homeassistant.util import dt as dt_util
        # Its own fields: no target (a thermostat's set_temperature passes it), as Home Assistant's own helper strips.
        fields = {k: v for k, v in fields.items() if k not in cv.ENTITY_SERVICE_FIELDS}
        if _IN_CALL.get() is not self:  # Called on the entity itself (a relative volume, by Home Assistant's intent).
            self._sandbox.commands.append({"entity_id": self.entity_id, "service": f"{self._domain}.{command}",
                                           "data": plain(fields)})
        state, sets = _table()[self._domain][command]
        state = state(fields) if callable(state) else state
        sets = sets(fields, self._attributes) if callable(sets) else sets
        attributes = {}
        for field, value in fields.items():
            name = attribute_of(self._domain, field)
            if name in self._attributes or held(self._domain, name):  # One the state shows (a mode) is no attribute.
                attributes[name] = plain(value)
        attributes |= {k: v for k, v in sets.items() if k in self._attributes}
        if state is _NOW:
            state = dt_util.utcnow().isoformat()
        if state is None and not attributes:
            return  # Nothing it takes: not simulated.
        self._handled = True
        self.hold(state if state is not None else self._state, {k: v for k, v in attributes.items() if v is not _GONE},
                  [k for k, v in attributes.items() if v is _GONE], moving=True)

    def _refuse(self):
        """Refuse the command it's taking, as at home, when the recording shows it failed there (async_request_call)."""
        if _IN_CALL.get() is self and self._failed is not None:
            raise HomeAssistantError(self._failed)

    def hold(self, state, attributes=None, removed=(), moving=False):
        """Hold a new state and attributes, and report them; others' changes between turns come here too
        (sandbox.observe). A device that reports its movement (moving, sandbox.moving) shows it on its way first, then
        gets there (its travel time isn't simulated)."""
        self._refuse()
        before, was = self._state, self._attributes.get("current_position")
        self._attributes = {k: v for k, v in self._attributes.items() if k not in removed} | (attributes or {})
        between, now = None, self._attributes.get("current_position")
        if moving and self.entity_id in self._sandbox.moving and self._domain in MOVERS:
            if state != before:
                between = UNDERWAY.get(state)
            elif self._domain != Platform.LOCK and None not in (was, now) and was != now:  # Open, to elsewhere.
                between = CoverState.OPENING if now > was else CoverState.CLOSING
        self._state = between or state
        self._mirror()
        self.async_write_ha_state()
        if between is not None:
            self.hass.async_create_task(self._arrive(between, state), "assist_improver_standin_arrive",
                                        eager_start=False)

    async def _arrive(self, between, state):
        if self._state == between:  # Not sent elsewhere since.
            self._state = state
            self._mirror()
            self.async_write_ha_state()


class MediaPlayerStandIn(StandIn):
    """A media player: its volume steps as asked (Home Assistant's own volume step), a spoken message or an
    announcement it plays is taken as done, leaving what it played (DETAILS P9.10.5: the call is in the turn's call
    log), and a search or a browse answers with what the recording's search found on it, else is not simulated."""

    async def async_volume_up(self):
        await self._step(1)

    async def async_volume_down(self):
        await self._step(-1)

    async def _step(self, direction):
        if _IN_CALL.get() is not self:  # Home Assistant's relative volume intent steps it on the entity itself.
            self._sandbox.commands.append({"entity_id": self.entity_id, "service":
                                           f"media_player.volume_{'up' if direction > 0 else 'down'}", "data": {}})
        level = self._attributes.get("volume_level") or 0
        self._handled = True
        self.hold(self._state, {"volume_level": round(max(0.0, min(1.0, level + direction * self.volume_step)), 2)})

    async def async_play_media(self, media_type, media_id, **kwargs):
        self._refuse()
        self._handled = True
        if tts_message(media_id) is None and not kwargs.get("announce"):  # Spoken words, or an announcement, pass.
            self.hold("playing", {"media_content_type": media_type, "media_content_id": media_id})

    async def async_search_media(self, query):
        from homeassistant.components.media_player import SearchMedia
        if not (found := self._found()):
            raise NotImplementedError
        return SearchMedia(result=found)

    async def async_browse_media(self, media_content_type=None, media_content_id=None):
        if (found := next((m for m in self._found() if m.media_content_id == media_content_id), None)) is None:
            raise NotImplementedError
        return found

    def _found(self):
        """The media the recording's searches found and played on this player (simulation.recorded)."""
        from homeassistant.components.media_player import BrowseMedia
        names = inspect.signature(BrowseMedia).parameters
        return [BrowseMedia(**{k: v for k, v in m["media"].items() if k in names})
                for m in self._sandbox.recorded.get("media") or [] if self.entity_id in m["entity_ids"]]


class AssistSatelliteStandIn(StandIn):
    """A voice satellite: an announcement it makes or a conversation it starts is taken as done (DETAILS P9.10.5: the
    call, with its words or media, is in the turn's call log), refused as at home when the recording shows it failed
    there. Home Assistant checks first that it can (its features)."""

    async def async_internal_announce(self, *args, **kwargs):
        self._refuse()
        self._handled = True

    async def async_internal_start_conversation(self, *args, **kwargs):
        self._refuse()
        self._handled = True


class TodoStandIn(StandIn):
    """A to-do list: its items, as recorded, changed as asked."""

    def __init__(self, hass, sandbox, domain, entity_id, entry, state):
        super().__init__(hass, sandbox, domain, entity_id, entry, state)
        from homeassistant.components.todo import TodoItem, TodoItemStatus
        from homeassistant.util import dt as dt_util
        names = set(inspect.signature(TodoItem).parameters)

        def item(found):
            due = found.get("due")
            due = (dt_util.parse_datetime(due) if "T" in due else dt_util.parse_date(due)) if due else None
            return TodoItem(**{k: v for k, v in found.items() if k in names and k not in ("status", "due")},
                            status=TodoItemStatus(found["status"]) if found.get("status") else None, due=due)
        recorded = ((sandbox.home.get("todo") or {}).get(entity_id) or {}).get("items") or []
        self._attr_todo_items = [item(i) for i in recorded]
        self._made = 0

    def _changed(self):
        from homeassistant.components.todo import TodoItemStatus
        self._handled = True
        self.hold(str(sum(i.status == TodoItemStatus.NEEDS_ACTION for i in self._attr_todo_items)))

    async def async_create_todo_item(self, item):
        from dataclasses import replace
        self._made += 1
        self._attr_todo_items = [*self._attr_todo_items, replace(item, uid=item.uid or f"stand-in-{self._made}")]
        self._changed()

    async def async_update_todo_item(self, item):
        self._attr_todo_items = [item if i.uid == item.uid else i for i in self._attr_todo_items]
        self._changed()

    async def async_delete_todo_items(self, uids):
        self._attr_todo_items = [i for i in self._attr_todo_items if i.uid not in uids]
        self._changed()

    async def async_move_todo_item(self, uid, previous_uid=None):
        items = list(self._attr_todo_items)
        moved = next(i for i in items if i.uid == uid)
        items.remove(moved)
        at = next((n + 1 for n, i in enumerate(items) if i.uid == previous_uid), 0)
        self._attr_todo_items = [*items[:at], moved, *items[at:]]
        self._changed()


_EXTRA = {"media_player": MediaPlayerStandIn, "assist_satellite": AssistSatelliteStandIn, "todo": TodoStandIn}
# The integrations the table and trigger_only read, imported as the improver loads (Home Assistant imports it off its
# event loop), never on the loop at a first use.
_used()
