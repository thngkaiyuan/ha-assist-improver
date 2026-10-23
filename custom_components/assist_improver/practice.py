"""Practice mode: your regular assistant, in a simulated home.

While practice mode is on for a person, their conversations with any AI
assistant run HA's own tools and intent logic against a copy of the home: HA
decides which devices a request targets and which command each gets, and only
the commands to devices (the drivers' job) are caught and emulated on the copy
(devices.py). The copy starts from the real states and lives as long as the
chat session. Commands without an emulation are reported as done and flagged
in the recording. Other people, and requests without a signed-in person (such
as voice satellites), are never affected.

The hook is the one place every assistant gets its tools, ChatLog's
async_provide_llm_data. It is installed while the improver is loaded and
passes straight through unless practice mode applies.
"""

from datetime import timedelta

from homeassistant.components.conversation import ChatLog
from homeassistant.core import State
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import intent, llm
from homeassistant.util.dt import utcnow

from . import devices
from .reflective import ROLE_ACTIVE
from .snapshot import state_record

SESSION_TTL = timedelta(hours=2)
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


class SimulatedServices:
    """hass.services as practice sees it: device commands are emulated on the copy, never sent."""

    def __init__(self, real, session):
        self._real, self.session = real, session

    async def async_call(self, domain, service, service_data=None, blocking=False, context=None, target=None,
                         return_response=False):
        if return_response and devices.is_query(service):
            return await self._real.async_call(domain, service, service_data, blocking=blocking, context=context,
                                               target=target, return_response=True)
        data = dict(service_data or {})
        entity_ids = data.pop("entity_id", None) or (target or {}).get("entity_id") or []
        entity_ids = [entity_ids] if isinstance(entity_ids, str) else entity_ids
        if not entity_ids and self.session.report:
            # A command to no particular device, such as running a script by name: nothing to emulate.
            self.session.report.unsimulated(f"{domain}.{service}", data)
        for entity_id in entity_ids:
            self.session.command(domain, service, entity_id, data, context)
        return {} if return_response else None

    def __getattr__(self, name):
        return getattr(self._real, name)


class SimulatedHass:
    """A view of hass with the simulated states and emulated device commands; everything else is the real hass."""

    def __init__(self, hass, states, services):
        self._hass = hass
        self.states, self.services = states, services

    def __getattr__(self, name):
        return getattr(self._hass, name)


class PracticeSession:
    def __init__(self, hass):
        self.states = SimulatedStates(hass.states)
        self.view = SimulatedHass(hass, self.states, SimulatedServices(hass.services, self))
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
        new = State(entity_id, result[0], result[1], context=context)
        if new.state != state.state or new.attributes != state.attributes:
            self.states.overrides[entity_id] = new
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
        if not type(tool).__module__.startswith("homeassistant."):
            # Only HA's own tools are known to act through the hass they're given; others never run in practice.
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

    def get(self, conversation_id):
        now = utcnow()
        self.sessions = {k: s for k, s in self.sessions.items() if now - s.touched < SESSION_TTL}
        session = self.sessions.setdefault(conversation_id, PracticeSession(self.hass))
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
        await self._save()

    async def async_turn_off(self, user_id):
        self.users.pop(user_id, None)
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
            chat_log.llm_api = SimulatedAPIInstance(chat_log.llm_api, self.sessions.get(chat_log.conversation_id),
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
