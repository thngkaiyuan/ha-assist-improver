"""Practice mode: your regular assistant, in a simulated home.

While practice mode is on for a person, their conversations with any AI
assistant run HA's own tools and intent logic against a copy of the home: HA
decides which devices a request targets and which command each gets, and only
the commands to devices (the drivers' job) are caught and emulated on the copy
(devices.py). The copy starts from the real states and lives as long as the
chat session. To-do lists and timers are copied into the session too, so HA's
own list and timer intents act on the copies; a broadcast is recorded, never
spoken. Commands without an emulation are reported as done and flagged in the
recording. Other people, and requests without a signed-in person (such as
voice satellites), are never affected.

The hook is the one place every assistant gets its tools, ChatLog's
async_provide_llm_data. It is installed while the improver is loaded and
passes straight through unless practice mode applies.
"""

from collections import ChainMap
from dataclasses import asdict, fields, replace
from datetime import timedelta

from homeassistant.components.conversation import ChatLog
from homeassistant.components.intent.const import TIMER_DATA
from homeassistant.components.intent.timers import TimerManager
from homeassistant.components.todo import TodoItemStatus
from homeassistant.components.todo.const import DATA_COMPONENT as TODO_LISTS
from homeassistant.components.todo.entity import api_items_factory
from homeassistant.core import State
from homeassistant.helpers import llm
from homeassistant.util.dt import utcnow
from homeassistant.util.ulid import ulid_now

from . import devices
from .reflective import ROLE_ACTIVE
from .snapshot import state_record
from .trace import journal

SESSION_TTL = timedelta(hours=2)
# Intents that act only through what practice simulates (Home Assistant's services and states, the to-do lists and
# the timers), so they can run against the copy. Any other tool that isn't read-only (the old shopping list intents,
# and anything Home Assistant adds later) never runs in practice: it's recorded as not simulated, so nothing real
# changes.
SIMULATED_INTENTS = {
    "HassTurnOn", "HassTurnOff", "HassToggle", "HassLightSet", "HassSetPosition", "HassStopMoving",
    "HassClimateSetTemperature", "HassFanSetSpeed", "HassMediaPause", "HassMediaUnpause", "HassMediaNext",
    "HassMediaPrevious", "HassSetVolume", "HassSetVolumeRelative", "HassMediaPlayerMute", "HassMediaPlayerUnmute",
    "HassVacuumStart", "HassVacuumReturnToBase", "HassVacuumCleanArea", "HassLawnMowerStartMowing",
    "HassLawnMowerDock", "HassHumidifierSetpoint", "HassHumidifierMode", "HassBroadcast",
    "HassListAddItem", "HassListCompleteItem", "HassListRemoveItem",
    "HassStartTimer", "HassCancelTimer", "HassCancelAllTimers", "HassIncreaseTimer", "HassDecreaseTimer",
    "HassPauseTimer", "HassUnpauseTimer", "HassTimerStatus"}


def simulated(tool):
    """Whether a tool can run against the copy: read-only, a script (run through hass.services, which practice
    catches), or an intent known to act only through services and states."""
    inner = getattr(tool, "tool", tool)  # HA's NamespacedTool wraps the real one.
    if any(getattr(getattr(t, "annotations", None), "read_only", False) for t in (tool, inner)):
        return True
    if type(inner).__name__ == "ActionTool":
        return True
    return getattr(inner, "intent_type", None) in SIMULATED_INTENTS
MODE_KEY = "assist_improver_practice_mode"
STORE_KEY = "assist_improver.practice_mode"


class SimulatedStates:
    """hass.states as practice sees it: the real states with the practice changes on top."""

    def __init__(self, real):
        self._real = real
        self.overrides = {}

    def get(self, entity_id):
        return self.overrides.get(entity_id) or self._real.get(entity_id)

    def async_all(self, domain_filter=None):
        if isinstance(domain_filter, str):
            domain_filter = {domain_filter}
        return [self.overrides.get(s.entity_id, s) for s in self._real.async_all(domain_filter)]

    def __getattr__(self, name):
        return getattr(self._real, name)


def _entity_ids(data, target):
    """The entities a service call targets; data loses its entity_id."""
    entity_ids = data.pop("entity_id", None) or (target or {}).get("entity_id") or []
    return [entity_ids] if isinstance(entity_ids, str) else list(entity_ids)


class SimulatedServices:
    """hass.services as practice sees it: device commands are emulated on the copy, never sent."""

    def __init__(self, real, session):
        self._real, self.session = real, session

    async def async_call(self, domain, service, service_data=None, blocking=False, context=None, target=None,
                         return_response=False):
        if return_response and devices.is_query(service) and domain != "script":  # A script named get_* still acts.
            copies = self.session.lists.lists if domain == "todo" and self.session.lists else {}
            data = dict(service_data or {})
            entity_ids = _entity_ids(data, target)
            if not any(e in copies for e in entity_ids):
                return await self._real.async_call(domain, service, service_data, blocking=blocking,
                                                   context=context, target=target, return_response=True)
            # A list this session changed answers from its copy; any other is read as it is.
            response = {e: copies[e].items(data.get("status")) for e in entity_ids if e in copies}
            if rest := [e for e in entity_ids if e not in copies]:
                response |= await self._real.async_call(domain, service, {**data, "entity_id": rest},
                                                        blocking=blocking, context=context, return_response=True)
            return response
        data = dict(service_data or {})
        entity_ids = _entity_ids(data, target)
        if not entity_ids and self.session.report:
            # A command to no particular device, such as running a script by name: nothing to emulate.
            self.session.report.unsimulated(f"{domain}.{service}", data)
        for entity_id in entity_ids:
            self.session.command(domain, service, entity_id, data, context)
        return {} if return_response else None

    def __getattr__(self, name):
        return getattr(self._real, name)


class SimulatedList:
    """A to-do list as practice sees it: a copy of the real items, changed only here. HA's own list intents act on
    it as they would on the real list."""

    def __init__(self, real, session):
        self.entity_id, self.session = real.entity_id, session
        self.todo_items = [replace(item) for item in real.todo_items or ()]

    async def async_create_todo_item(self, item):
        self.todo_items.append(replace(item, uid=item.uid or ulid_now()))
        self._changed()

    async def async_update_todo_item(self, item):
        changes = {f.name: getattr(item, f.name) for f in fields(item) if getattr(item, f.name) is not None}
        self.todo_items = [replace(i, **changes) if i.uid == item.uid else i for i in self.todo_items]
        self._changed()

    async def async_delete_todo_items(self, uids):
        self.todo_items = [i for i in self.todo_items if i.uid not in uids]
        self._changed()

    def items(self, statuses=None):
        """What todo.get_items answers for this list."""
        statuses = [statuses] if isinstance(statuses, str) else statuses
        return {"items": [asdict(i, dict_factory=api_items_factory) for i in self.todo_items
                          if not statuses or i.status in statuses]}

    def _changed(self):
        """A list's state is the count of items still to do, as HA reports it."""
        if (state := self.session.states.get(self.entity_id)) is not None:
            count = sum(i.status == TodoItemStatus.NEEDS_ACTION for i in self.todo_items)
            self.session.update(state, str(count), state.attributes)


class SimulatedLists:
    """HA's to-do lists as practice sees them: each list is copied when the session first uses it."""

    def __init__(self, real, session):
        self._real, self.session, self.lists = real, session, {}

    def get_entity(self, entity_id):
        if entity_id not in self.lists:
            if (real := self._real.get_entity(entity_id)) is None:
                return None
            self.lists[entity_id] = SimulatedList(real, self.session)
        return self.lists[entity_id]

    def __getattr__(self, name):
        return getattr(self._real, name)


class QuietDevices:
    """The real timer devices, so a timer starts for them as it would for real, but none of them hears of it."""

    def __init__(self, real):
        self._real = real

    def __contains__(self, device_id):
        return device_id in self._real

    def __getitem__(self, device_id):
        return lambda event, timer: None


class SimulatedTimers(TimerManager):
    """HA's timers as practice sees them: the running ones copied, then started, changed and cancelled only here.
    Nothing rings on a device, and a timer's delayed command never runs: it's flagged in the recording."""

    def __init__(self, hass, real, session):
        super().__init__(hass)
        self.session, self.handlers = session, QuietDevices(real.handlers)
        for timer_id, timer in real.timers.items():
            self.timers[timer_id] = copy = replace(timer)
            if copy.is_active:
                self.timer_tasks[timer_id] = hass.async_create_background_task(
                    self._wait_for_timer(timer_id, copy.seconds_left, copy.updated_at), name=f"Practice timer {timer_id}")

    def start_timer(self, device_id, *args, conversation_command=None, **kwargs):
        if conversation_command and self.session.report:
            self.session.report.unsimulated("delayed command", {"command": conversation_command})
        return super().start_timer(device_id, *args, conversation_command=conversation_command, **kwargs)

    async def _async_run_conversation_command(self, timer):
        """A delayed command runs only in real use; it was flagged when its timer started."""


class SimulatedHass:
    """A view of hass with the simulated states, device commands, lists and timers; everything else is the real
    hass. What HA code stores in hass.data here stays in the session."""

    def __init__(self, hass, states, services, data):
        self._hass = hass
        self.states, self.services, self.data = states, services, data

    def __getattr__(self, name):
        return getattr(self._hass, name)


class PracticeSession:
    def __init__(self, hass):
        self.states = SimulatedStates(hass.states)
        self.data = ChainMap({}, hass.data)
        self.view = SimulatedHass(hass, self.states, SimulatedServices(hass.services, self), self.data)
        self.lists = None
        if (lists := hass.data.get(TODO_LISTS)) is not None:
            self.data[TODO_LISTS] = self.lists = SimulatedLists(lists, self)
        if (timers := hass.data.get(TIMER_DATA)) is not None:
            self.data[TIMER_DATA] = SimulatedTimers(self.view, timers, self)
        self.report = None
        self.touched = utcnow()

    def command(self, domain, service, entity_id, data, context):
        """One device command: apply its emulated effect to the copy, or flag it."""
        state = self.states.get(entity_id)
        if domain == "homeassistant" and state is not None:
            domain = state.domain  # homeassistant.turn_on and friends act through the device's own domain.
        result = devices.effect(domain, service, state, data) if state is not None else None
        if result is None or result[0] is None:
            if self.report:
                self.report.unsimulated(f"{domain}.{service}", {"entity_id": entity_id, **data})
            return
        self.update(state, *result, context=context)

    def update(self, state, value, attributes, context=None):
        """A new state on the copy, recorded if anything changed."""
        new = State(state.entity_id, value, attributes, context=context)
        if new.state != state.state or new.attributes != state.attributes:
            self.states.overrides[state.entity_id] = new
            if self.report:
                self.report.changed(state, new)


class SimulatedAPIInstance(llm.APIInstance):
    """The assistant's own tools, prompt and context, with HA's tool logic running against the copy."""

    def __init__(self, real, session, report):
        super().__init__(api=real.api, api_prompt=real.api_prompt, llm_context=real.llm_context,
                         tools=real.tools, custom_serializer=real.custom_serializer)
        self.session = session
        session.report = report

    async def async_call_tool(self, tool_input):
        tool = next((t for t in self.tools if t.name == tool_input.tool_name), None)
        if tool is None:
            return await super().async_call_tool(tool_input)  # Raises HA's own "not found".
        own = getattr(getattr(tool, "tool", tool), "improver_own", False)  # Filing writes only the improver's record.
        if not own and (not type(tool).__module__.startswith("homeassistant.") or not simulated(tool)):
            # Only HA's own tools known to act through the hass they're given run in practice; the rest never do.
            self.session.report.unsimulated(tool_input.tool_name, tool_input.tool_args)
            return llm.ToolResult(data={"success": True})
        result = await tool.async_call(self.session.view, tool_input, self.llm_context)
        return result if isinstance(result, llm.ToolResult) else llm.ToolResult(data=result)


class PracticeReport:
    """Writes what practice did into the session's recording."""

    def __init__(self, recorder, conversation_id):
        self.recorder, self.conversation_id = recorder, conversation_id

    def _practice(self):
        return self.recorder.practice(self.conversation_id) if self.recorder else None

    def changed(self, old, new):
        if (practice := self._practice()) is not None:
            practice["simulated_changes"].append({"at": utcnow().isoformat(), "entity_id": new.entity_id,
                                                  "old": state_record(old), "new": state_record(new)})

    def unsimulated(self, tool_name, args):
        if (practice := self._practice()) is not None:
            practice["unsimulated_actions"].append({"at": utcnow().isoformat(), "tool": tool_name,
                                                    "args": dict(args)})


class PracticeSessions:
    def __init__(self, hass):
        self.hass, self.sessions = hass, {}

    def get(self, conversation_id, recording_id=None):
        now = utcnow()
        for key in [k for k, s in self.sessions.items() if now - s.touched >= SESSION_TTL]:
            journal("info", "Practice session ended: idle for 2 hours", conversation_id=key)
            del self.sessions[key]
        if conversation_id not in self.sessions:
            journal("info", "Practice session started: a simulated copy of the home", conversation_id=conversation_id,
                    recording_id=recording_id)
            self.sessions[conversation_id] = PracticeSession(self.hass)
        session = self.sessions[conversation_id]
        session.touched = now
        return session


class PracticeMode:
    """Who is practising, their simulated homes, and the switch's listeners.

    It stays on for a person until they turn it off, across restarts, so it never switches off by surprise.
    """

    def __init__(self, hass, recorder):
        from homeassistant.helpers.storage import Store
        self.hass, self.recorder = hass, recorder
        self.sessions = PracticeSessions(hass)
        self.users = {}  # user_id -> name, for whoever is practising.
        self.listeners = []
        self.store = Store(hass, 1, STORE_KEY, private=True, atomic_writes=True)

    async def async_load(self):
        self.users = ((await self.store.async_load()) or {}).get("users", {})

    def is_on(self, user_id):
        return user_id in self.users

    async def async_turn_on(self, user_id, name):
        self.users[user_id] = name
        journal("info", "Practice mode on", user_id=user_id)
        await self._save()

    async def async_turn_off(self, user_id):
        self.users.pop(user_id, None)
        journal("info", "Practice mode off", user_id=user_id)
        await self._save()

    async def _save(self):
        await self.store.async_save({"users": self.users})
        for listener in list(self.listeners):
            listener()

    def applies(self, chat_log, llm_context):
        if ROLE_ACTIVE.get() or chat_log.conversation_id.startswith("improver-replay-"):
            return False  # The improver's own AI roles and replays are never practice.
        context = getattr(llm_context, "context", None)
        return bool(context and context.user_id and self.is_on(context.user_id))

    async def provide_llm_data(self, original, chat_log, llm_context, *args, **kwargs):
        """One practice turn: the assistant's own prompt and tools, acting on the simulated home.

        Goals are never asked for: the learning loop works them out, as it does for real use.
        """
        if self.recorder:
            self.recorder.practice(chat_log.conversation_id)  # Marks the recording as practice.
        await original(chat_log, llm_context, *args, **kwargs)
        if chat_log.llm_api is not None:
            recording_id = getattr(self.recorder, "keys", {}).get(chat_log.conversation_id)
            chat_log.llm_api = SimulatedAPIInstance(chat_log.llm_api,
                                                    self.sessions.get(chat_log.conversation_id, recording_id),
                                                    PracticeReport(self.recorder, chat_log.conversation_id))


_ORIGINAL = None


def install():
    """Hook ChatLog.async_provide_llm_data; it passes through unless practice mode applies."""
    global _ORIGINAL
    if _ORIGINAL is not None:
        return
    _ORIGINAL = original = ChatLog.async_provide_llm_data

    async def provide_llm_data(chat_log, llm_context, *args, **kwargs):
        mode = chat_log.hass.data.get(MODE_KEY)
        if mode is None or not mode.applies(chat_log, llm_context):
            return await original(chat_log, llm_context, *args, **kwargs)
        return await mode.provide_llm_data(original, chat_log, llm_context, *args, **kwargs)

    ChatLog.async_provide_llm_data = provide_llm_data


def uninstall():
    global _ORIGINAL
    if _ORIGINAL is not None:
        ChatLog.async_provide_llm_data = _ORIGINAL
        _ORIGINAL = None
