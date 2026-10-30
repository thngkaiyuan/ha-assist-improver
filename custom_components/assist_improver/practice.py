"""Practice mode: your regular assistant, in a copy of the home.

While practice mode is on for a person, their Assist conversations, with any AI assistant, act on a copy of the
home in a Home Assistant of its own: a sandbox in the replay worker (worker.py, sandbox.py), seeded as the home is
when the conversation begins, which keeps seeing the real changes it hasn't made. Assist's prompt and tools are
built there, and Home Assistant's own tools and intent logic run there: its devices are emulated (devices.py), its
to-do lists hold the real lists' items, the household's scripts run there, its timers run there and ring nowhere.
Nothing a tool does there can reach the real home. Only the reads the copy can't hold (a calendar's events, a media
library) are answered by the real home, through the query's own handler, never announced on the real bus. Tools the
copy doesn't have (an integration's own, an intent script) are reported as done and flagged in the recording, never
run. Other people, and requests without
a signed-in person (such as voice satellites), are never affected.

The hook is the one place every assistant gets its tools in Assist, ChatLog's async_provide_llm_data. For a
practising person, Home Assistant still builds the turn's system prompt from their assistant's own template, with
the copy's Assist API in place of the real one.

Home Assistant also answers some requests itself, with no assistant preparing
tools: the household's sentence triggers and, when Assist prefers handling
commands locally, its own intents, both before the assistant is asked; or its
own conversation agent, as Assist's assistant. A second hook, on its default
agent, tells the recorder who is speaking there (a person whose recording
switch is off isn't kept), and leaves a practising person's request to their
assistant rather than let Home Assistant act on the real home before it. With
no assistant to leave it to (its own agent is Assist's), it changes nothing and
says that sandbox mode needs an AI assistant.
"""

import asyncio
from contextlib import suppress
from dataclasses import asdict
from datetime import timedelta

from homeassistant.components import persistent_notification
from homeassistant.components.conversation import AssistantContent, ChatLog, ConversationResult
from homeassistant.components.conversation.default_agent import DefaultAgent
from homeassistant.components.todo.const import DATA_COMPONENT as TODO_LISTS
from homeassistant.components.todo.entity import api_items_factory
from homeassistant.core import ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import intent, llm
from homeassistant.util.dt import utcnow

from .reflective import ROLE_ACTIVE
from .trace import journal

SESSION_TTL = timedelta(hours=2)
MODE_KEY = "assist_improver_practice_mode"
STORE_KEY = "assist_improver.practice_mode"
NO_ASSISTANT = "Sandbox mode needs an AI assistant, so I didn't change anything."
# The reads a copy holds: its states (the live context), its clock, its to-do lists. Any other read of Home
# Assistant's own tools (a calendar's events, a media library) only the real home has.
COPY_READS = {"homeassistant", "llm", "todo"}
HOUSEHOLD_INTENTS = "intent_script"  # The household's own intents: the copy answers with Home Assistant's.


def reminder_id(user_id):
    """The notification reminding a person that sandbox mode is on for them (switch.py)."""
    return f"assist_improver_practice_{user_id}"


class PracticeReport:
    """Writes what practice did into the session's recording."""

    def __init__(self, recorder, conversation_id):
        self.recorder, self.conversation_id = recorder, conversation_id

    def _practice(self):
        return self.recorder.practice(self.conversation_id) if self.recorder else None

    def changed(self, entity_id, old, new, user_id=None):
        """A change on the copy: old and new as {state, attributes}, as the sandbox reports them."""
        if (practice := self._practice()) is not None:
            context = {"id": None, "parent_id": None, "user_id": user_id}
            practice["simulated_changes"].append({"at": utcnow().isoformat(), "entity_id": entity_id,
                                                  "old": {"entity_id": entity_id, **old},
                                                  "new": {"entity_id": entity_id, **new, "context": context}})

    def unsimulated(self, tool_name, args):
        if (practice := self._practice()) is not None:
            practice["unsimulated_actions"].append({"at": utcnow().isoformat(), "tool": tool_name,
                                                    "args": dict(args)})


class _Reads:
    """The real home as a read of Home Assistant's own sees it: a query (Home Assistant's mark, a service that only
    answers) is answered by its own handler, as the registry runs it, but never announced on the real bus, where an
    automation would hear it and the recorder keep it; nothing else is reached."""

    def __init__(self, hass):
        self._hass, self.states, self.data, self.config = hass, hass.states, hass.data, hass.config
        self.services = self

    def __getattr__(self, name):
        return getattr(self._hass, name)

    async def async_call(self, domain, service, service_data=None, blocking=False, context=None, target=None,
                         return_response=False):
        registry = self._hass.services
        domain, service = domain.lower(), service.lower()
        if not registry.has_service(domain, service) or registry.supports_response(domain, service) is not \
                SupportsResponse.ONLY:
            raise HomeAssistantError(f"Sandbox mode only reads the real home ({domain}.{service})")
        handler = registry.async_services_internal()[domain][service]
        data = {**(service_data or {}), **(target or {})}
        call = ServiceCall(self._hass, domain, service, handler.schema(data) if handler.schema else data, context,
                           return_response=True)
        return await registry._execute_service(handler, call)  # noqa: SLF001 - the registry's own run, unannounced.

    def has_service(self, domain, service):
        return self._hass.services.has_service(domain, service)

    def supports_response(self, domain, service):
        return self._hass.services.supports_response(domain, service)


class _CopyTool(llm.Tool):
    """One of Assist's tools as Home Assistant built it in the copy, offered by the name the person's assistant knows
    it by: a call runs there."""

    def __init__(self, tool, home, name):
        import probatio
        from .replay import Precomputed
        self.copy_name, self.name, self.description = tool["name"], name, tool["description"]
        self.parameters = probatio.Schema(Precomputed(tool["parameters"]))
        self.integration = tool.get("integration")
        self.home = home

    async def async_call(self, hass, tool_input, llm_context):
        async with self.home.acting:
            if (sandbox := self.home.sandbox) is None:  # Sandbox mode ended mid-turn: nothing runs.
                raise HomeAssistantError("Sandbox mode's copy of the home has ended")
            try:
                found = await sandbox.call("tool", tool_name=self.copy_name, tool_args=tool_input.tool_args,
                                           call_id=tool_input.id, external=tool_input.external)
            except Exception:
                with suppress(Exception):  # What it changed before it failed (a script's steps so far).
                    await self.home.report_since(sandbox)
                raise
            await self.home.report_since(sandbox)
        return llm.ToolResult(data=found["data"], error=found["error"])


class _RealRead(llm.Tool):
    """A read of Home Assistant's own the copy can't hold, answered by the real home (_Reads)."""

    def __init__(self, tool):
        self.tool, self.name, self.description = tool, tool.name, tool.description
        self.parameters, self.integration = tool.parameters, getattr(tool, "integration", None)
        self.annotations = type(tool).annotations

    async def async_call(self, hass, tool_input, llm_context):
        return await self.tool.async_call(_Reads(hass), tool_input, llm_context)


class _Flagged(llm.Tool):
    """A tool the copy doesn't have (a script, an integration's own): reported as done and flagged, never run."""

    def __init__(self, tool, home):
        self.home, self.name, self.description = home, tool.name, tool.description
        self.parameters, self.integration = tool.parameters, getattr(tool, "integration", None)

    async def async_call(self, hass, tool_input, llm_context):
        self.home.report.unsimulated(tool_input.tool_name, tool_input.tool_args)
        return llm.ToolResult(data={"success": True})


def _inner(tool):
    """The tool itself, out of Home Assistant's NamespacedTool wrappers (as Home Assistant unwraps a merged API's)."""
    while type(tool) is llm.NamespacedTool:
        tool = tool.tool
    return tool


def _assist_name(tool, apis):
    """A tool's name in Assist's own API, as the copy builds it; None for another API's tool. Beside another API,
    Home Assistant offers Assist's tools under its name (MergedAPI)."""
    if type(tool) is llm.NamespacedTool:
        return tool.tool.name if tool.namespace == llm.LLM_API_ASSIST else None
    return tool.name if apis == [llm.LLM_API_ASSIST] else None


def _improvers_own(tool):
    """The improver's own tool (filing the conversation): it writes only the improver's record, so it runs as at home.
    Its class says so (llm.py), never an instance."""
    kind = type(_inner(tool))
    return kind.__module__.startswith(f"{__package__}.") and getattr(kind, "improver_own", False)


def _reads_real(tool):
    """Whether a tool is a read of Home Assistant's own that only the real home can answer (COPY_READS): its class is
    Home Assistant's and declares it only reads (a claim set on an instance, as a remote server's, doesn't count)."""
    tool = _inner(tool)
    kind = type(tool)
    return kind.__module__.startswith("homeassistant.") and kind.annotations.read_only and \
        getattr(tool, "integration", None) not in COPY_READS


class LiveHome(llm.API):
    """One practising conversation's copy of the home: a sandbox in the replay worker, seeded from the home as it is
    when the conversation begins; before each turn, the real changes it hasn't made since reach it (as a replay's
    others' moves do). Its Assist API is what the person's assistant gets in place of the real one."""

    def __init__(self, hass, conversation_id, start):
        super().__init__(hass=hass, id=f"assist_improver_sandbox_{conversation_id}", name="Sandbox mode")
        self.conversation_id, self.apis, self.report = conversation_id, "assist", PracticeReport(None, None)
        self.start = start  # A started sandbox, a spare one if there is one (PracticeMode.take).
        self.sandbox, self.synced, self.mine, self.reported, self.flagged = None, {}, set(), 0, 0
        self.lists = {}  # Each to-do list's items as the copy was last given them.
        # One tool call on the copy at a time, with what it changed: a message's calls, which Home Assistant runs at
        # once, each get their own changes and flags.
        self.acting = asyncio.Lock()
        self.closed = False  # Stopped: its copy is gone, and none is started for it again.
        self.touched = utcnow()

    async def async_get_api_instance(self, llm_context):
        from .replay import serializer
        from .worker import context_data
        real = await llm.async_get_api(self.hass, self.apis, llm_context)  # The person's own tools, as at home.
        source = self._now()
        if self.sandbox is not None and not self.sandbox.live:  # The worker stopped, and its copies with it.
            journal("warning", "Practice session's copy of the home ended with the replay worker: a new copy",
                    conversation_id=self.conversation_id)
            self.sandbox, self.synced, self.mine, self.reported, self.flagged, self.lists = None, {}, set(), 0, 0, {}
        if self.sandbox is None:
            await self._seed(source, llm_context, real)
        else:
            await self._mirror(source)
            if (lists := self._lists(source)) and any(lists[e] != self.lists.get(e) for e in lists):
                held = await self.sandbox.call("lists", source=source, lists=lists, given=self.lists)
                self.lists |= {e: lists[e] for e in held["held"]}
        if source.get("household_on"):
            await self.sandbox.call("household", knowledge=source.get("household"))
        built = await self.sandbox.call("prompt", context=context_data(llm_context), api_id="assist", template="")
        copy, apis = {t["name"]: t for t in built["tools"]}, [self.apis] if isinstance(self.apis, str) else self.apis
        # Each tool the person's assistant has at home: the improver's own, a real read, Assist's own on the copy, or
        # flagged.
        tools = [t if _improvers_own(t) else _RealRead(t) if _reads_real(t) else _CopyTool(copy[name], self, t.name)
                 if (name := _assist_name(t, apis)) in copy else _Flagged(t, self) for t in real.tools]
        prompt = built["api_prompt"] if apis == [llm.LLM_API_ASSIST] else real.api_prompt
        return llm.APIInstance(api=self, api_prompt=prompt, llm_context=llm_context, tools=tools,
                               custom_serializer=serializer)

    def _now(self):
        """The real home now, as a recording keeps it (snapshot.snapshot), each state with all its attributes: the
        copy isn't kept, so it has every one Home Assistant's intents read (a humidifier's modes)."""
        from .serialization import plain
        from .snapshot import snapshot
        source = snapshot(self.hass)
        source["states"] = {s.entity_id: {**source["states"].get(s.entity_id, {}), "state": s.state,
                                          "attributes": plain(dict(s.attributes))} for s in self.hass.states.async_all()
                            if s.entity_id in source["states"]}
        return source

    async def _seed(self, source, llm_context, real):
        from .sandbox import seed_options
        from .simulation import reports_movement
        self.sandbox = await self.start()
        if self.closed:  # Sandbox mode ended while it started.
            await self.stop()
            raise HomeAssistantError("Sandbox mode ended")
        inner = [_inner(t) for t in real.tools]
        # The integrations of Home Assistant's own tools are set up in the copy, the household's scripts with them;
        # its intent scripts and other integrations' never are: their tools are flagged.
        tools = {"tools": [{"name": t.name} for t in inner if type(t).__module__.startswith("homeassistant.")
                           and t.name.split("__", 1)[0] != HOUSEHOLD_INTENTS],
                 "context": {"device_id": llm_context.device_id}}
        user = await self.hass.auth.async_get_user(llm_context.context.user_id) if llm_context.context else None
        try:
            self.lists = self._lists(source)
            await self.sandbox.call("seed", source=source, user_name=getattr(user, "name", None),
                                    options=seed_options({"prepared": tools}), scripts=source.get("scripts") or {},
                                    lists=self.lists, timers=self._timers(),
                                    moving=reports_movement({"turns": [{"source": source}], "state_changes": []}))
        except Exception:
            await self.stop()
            raise
        self.synced = source["states"]

    def _timers(self):
        """The home's running timers, each on its device."""
        from homeassistant.components.intent.const import TIMER_DATA
        return [{"device_id": t.device_id, "name": t.name, "seconds_left": t.seconds_left, "language": t.language,
                 "is_active": t.is_active} for t in getattr(self.hass.data.get(TIMER_DATA), "timers", {}).values()
                if t.device_id]

    def _lists(self, source):
        """Each exposed to-do list's items, as Home Assistant's own get_items gives them."""
        lists, component = {}, self.hass.data.get(TODO_LISTS)
        for entity_id, exposed in (source.get("exposure") or {}).items():
            if exposed and entity_id.startswith("todo.") and component is not None and \
                    (entity := component.get_entity(entity_id)) is not None:
                lists[entity_id] = [asdict(i, dict_factory=api_items_factory) for i in entity.todo_items or ()]
        return lists

    async def _mirror(self, source):
        """The real changes since the last turn that the copy hasn't made itself: on the copy, on top of its own (a
        to-do list's are its items, below)."""
        moves = {}
        for entity_id, now in source["states"].items():
            was = self.synced.get(entity_id)
            if entity_id in self.mine or was is None or entity_id.startswith("todo.") or (
                    now["state"], now["attributes"]) == (was["state"], was["attributes"]):
                continue
            moves[entity_id] = {"state": now["state"], "attributes": now["attributes"],
                                "removed": sorted(set(was["attributes"]) - set(now["attributes"]))}
        await self.sandbox.call("observe", source={"at": source["at"]}, moves=[moves])
        self.synced = source["states"]

    async def report_since(self, sandbox):
        """What the copy changed, and what it couldn't emulate, since the last call: into the recording. As it is now,
        not waiting on what's still running there (a script's delay), as Home Assistant answers at home: that's
        reported after the next call."""
        found = await sandbox.call("snapshot", settle=False)
        for change in found["changes"][self.reported:]:
            self.mine.add(change["entity_id"])
            self.report.changed(change["entity_id"], change["old"], change["new"])
        self.reported = len(found["changes"])
        for flag in found["flags"][self.flagged:]:
            self.report.unsimulated(flag["service"], flag["args"])
        self.flagged = len(found["flags"])

    async def stop(self):
        self.closed = True
        if self.sandbox is not None:
            sandbox, self.sandbox = self.sandbox, None
            await sandbox.stop()


class PracticeSessions:
    def __init__(self, hass, start):
        self.hass, self.start, self.sessions = hass, start, {}

    def get(self, conversation_id, recording_id=None):
        now = utcnow()
        for key in [k for k, s in self.sessions.items() if now - s.touched >= SESSION_TTL]:
            journal("info", "Practice session ended: idle for 2 hours", conversation_id=key)
            self.hass.async_create_task(self.sessions.pop(key).stop(), "assist_improver_practice_stop")
        if conversation_id not in self.sessions:
            journal("info", "Practice session started: a copy of the home", conversation_id=conversation_id,
                    recording_id=recording_id)
            self.sessions[conversation_id] = LiveHome(self.hass, conversation_id, self.start)
        session = self.sessions[conversation_id]
        session.touched = now
        return session


class PracticeMode:
    """Who is practising, their copies of the home, and the switch's listeners.

    It stays on for a person until they turn it off, across restarts, so it never switches off by surprise.
    """

    def __init__(self, hass, recorder=None):
        from homeassistant.helpers.storage import Store
        self.hass, self.recorder = hass, recorder  # No recorder while the improver's entry isn't loaded.
        self.sessions = PracticeSessions(hass, self.take)
        self.spare = None  # A sandbox starting or started, for the next conversation (warm).
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

    def warm(self):
        """While anyone is in sandbox mode, a sandbox is started and waiting (it keeps the replay worker up), so a
        practice conversation's first turn only seeds it, never waiting for the worker to start (about 1.3 s)."""
        from .replay import RemoteSandbox
        if self.users and self.spare is None:
            self.spare = self.hass.async_create_task(RemoteSandbox.start(self.hass.config.time_zone),
                                                     "assist_improver_practice_spare")

    async def take(self):
        """A started sandbox for a new conversation: the spare, then another one warmed for the next."""
        from .replay import RemoteSandbox
        spare, self.spare = self.spare, None
        self.warm()
        if spare is not None:
            with suppress(Exception):  # A worker that stopped since: start one now.
                if (sandbox := await spare).live:
                    return sandbox
        return await RemoteSandbox.start(self.hass.config.time_zone)

    async def cool(self):
        """No one in sandbox mode any more, or the worker's going: the spare goes, and so do the copies."""
        spare, self.spare = self.spare, None
        if spare is not None:
            with suppress(Exception):  # A worker that stopped has stopped it too.
                await (await spare).stop()
        for session in list(self.sessions.sessions.values()):
            await session.stop()
        self.sessions.sessions.clear()

    async def async_end(self):
        """Sandbox mode off for everyone, each reminder gone with it: Assist Improver disabled or removed (DETAILS
        P11.11)."""
        for user_id in self.users:
            persistent_notification.async_dismiss(self.hass, reminder_id(user_id))
        self.users.clear()
        journal("info", "Practice mode off for everyone: Assist Improver disabled or removed")
        await self._save()

    async def _save(self):
        await self.store.async_save({"users": self.users})
        if self.users:
            self.warm()
        else:
            await self.cool()
        for listener in list(self.listeners):
            listener()

    def applies(self, chat_log, llm_context):
        return self.practising(chat_log.conversation_id, getattr(llm_context, "context", None))

    def practising(self, conversation_id, context):
        """Whether a request in this conversation, made in this context, is the signed-in person's practice."""
        if ROLE_ACTIVE.get() or conversation_id.startswith("improver-replay-"):
            return False  # The improver's own AI roles and replays are never practice.
        return bool(context and context.user_id and self.is_on(context.user_id))

    async def provide_llm_data(self, original, chat_log, llm_context, user_llm_hass_api=None, *args, **kwargs):
        """One practice turn: the assistant's own prompt, with the copy's Assist API in place of the real one.

        Goals are never asked for: the learning loop works them out, as it does for real use.
        """
        if self.recorder:
            self.recorder.practice(chat_log.conversation_id)  # Marks the recording as practice.
        if user_llm_hass_api:
            recording_id = getattr(self.recorder, "keys", {}).get(chat_log.conversation_id)
            home = self.sessions.get(chat_log.conversation_id, recording_id)
            home.apis, home.report = user_llm_hass_api, PracticeReport(self.recorder, chat_log.conversation_id)
            user_llm_hass_api = home
        await original(chat_log, llm_context, user_llm_hass_api, *args, **kwargs)

    def without_assistant(self, user_input, chat_log):
        """A practice turn with Home Assistant's own agent as Assist's assistant: no assistant can answer on a copy of
        the home, so nothing acts (no sentence trigger, no intent), and the reply, added as the agent adds its own,
        says so."""
        if self.recorder:
            self.recorder.practice(chat_log.conversation_id)  # Marks the recording as practice.
        response = intent.IntentResponse(language=user_input.language or self.hass.config.language)
        response.async_set_speech(NO_ASSISTANT)
        chat_log.async_add_assistant_content_without_tools(
            AssistantContent(agent_id=user_input.agent_id, content=NO_ASSISTANT))
        return ConversationResult(response=response, conversation_id=chat_log.conversation_id)


# Where Home Assistant's default agent answers a request itself: before the assistant (the household's sentence
# triggers; its own intents, when Assist prefers handling commands locally), and as Assist's own assistant.
BEFORE_ASSISTANT = ("async_handle_sentence_triggers", "async_handle_intents")
ITSELF = (*BEFORE_ASSISTANT, "_async_handle_message")


def _answers_itself(name, original):
    """The default agent answering a request itself: the recorder hears who's speaking; before the assistant, a
    practising person's request isn't answered here (None, as for no match), so their assistant answers it on their
    copy of the home; as Assist's assistant, it changes nothing for them."""
    async def answer(agent, user_input, chat_log, *args, **kwargs):
        if (mode := agent.hass.data.get(MODE_KEY)) is not None:
            if mode.recorder is not None:
                mode.recorder.heard(chat_log.conversation_id, user_input.context.user_id)
            if mode.practising(chat_log.conversation_id, user_input.context):
                return None if name in BEFORE_ASSISTANT else mode.without_assistant(user_input, chat_log)
        return await original(agent, user_input, chat_log, *args, **kwargs)
    return answer


def _hook():
    """Hook ChatLog.async_provide_llm_data and the default agent's own answers, once, when the improver's code loads,
    and for as long as Home Assistant runs: they pass through unless sandbox mode applies (the recorder hears who's
    speaking either way). Sandbox mode itself stays while the improver's entry reloads or fails to set up, so a
    person in it is never handed the real home; only the recording waits for the improver. While Home Assistant is
    starting, until the improver has set up, nothing of it holds sandbox mode yet."""
    original = ChatLog.async_provide_llm_data

    async def provide_llm_data(chat_log, llm_context, *args, **kwargs):
        mode = chat_log.hass.data.get(MODE_KEY)
        if mode is not None and mode.recorder is not None:  # Who's speaking, with or without an Assist API.
            mode.recorder.heard(chat_log.conversation_id, getattr(getattr(llm_context, "context", None), "user_id", None))
        if mode is None or not mode.applies(chat_log, llm_context):
            return await original(chat_log, llm_context, *args, **kwargs)
        return await mode.provide_llm_data(original, chat_log, llm_context, *args, **kwargs)

    ChatLog.async_provide_llm_data = provide_llm_data
    for name in ITSELF:
        setattr(DefaultAgent, name, _answers_itself(name, getattr(DefaultAgent, name)))


_hook()
