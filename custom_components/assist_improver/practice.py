"""Practice mode: your regular assistant, in a copy of the home.

While practice mode is on for a person, each of their Assist conversations runs on a copy of the home in a Home
Assistant of its own: a sandbox in the replay worker (worker.py, sandbox.py), seeded as the home is when the
conversation begins, which keeps seeing the real changes it hasn't made. The person's assistant runs there too,
loaded by its provider's own integration from its own config entry (sandbox.load_assistant): it renders its prompt,
builds its tools and asks its model there, and each tool call runs there, at once, as at home. Its devices are
emulated (devices.py), its to-do lists hold the real lists' items and its calendars the real calendars' coming week,
its weather the real forecasts (each read before every turn, but one the copy changed: copy on write), the
household's scripts run there, its timers run there and ring nowhere. Nothing of the turn runs here, so nothing it
does can reach the real home. Only a lookup the copy can't hold (a media library's search) is asked of the real home
as the turn runs, through the query's own handler, never announced on the real bus (read). Other people, and requests
without a signed-in person (such as voice satellites), are never affected.

The turn's reply streams back into the person's own chat log as it comes, through Home Assistant's own delta stream,
each tool call answered with what the copy's call answered (_Relay): the person sees their assistant answer as at
home, and the recorder keeps the turn as it keeps any, with what the assistant was given in the copy. Filing the
conversation writes only the improver's own record, so the person's is filed here, as any day.

The hook is Home Assistant's own way into an assistant, ConversationEntity.internal_async_process: it passes through
unless the person speaking is practising. Any other way an assistant reaches the real home's tools for a practising
person (one that isn't a conversation entity) is refused at ChatLog's async_provide_llm_data, nothing run; that hook
also tells the recorder who is speaking.

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
import json
from dataclasses import asdict, replace
from datetime import timedelta
from functools import partial
from types import SimpleNamespace

from homeassistant.components import conversation, persistent_notification
from homeassistant.components.conversation import (AssistantContent, ChatLog, ConversationEntity, ConversationResult,
                                                   SystemContent)
from homeassistant.components.conversation.chat_log import _async_notify_subscribers
from homeassistant.components.conversation.const import ChatLogEventType
from homeassistant.components.conversation.default_agent import DefaultAgent
from homeassistant.components.todo.const import DATA_COMPONENT as TODO_LISTS
from homeassistant.components.todo.entity import api_items_factory
from homeassistant.core import ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import chat_session, entity_registry as er, intent, llm
from homeassistant.helpers.json import json_dumps
from homeassistant.util import dt as dt_util
from homeassistant.util.dt import utcnow

from .const import DOMAIN
from .llm import FileImprovement
from .reflective import ROLE_ACTIVE
from .serialization import plain
from .trace import journal

SESSION_TTL = timedelta(hours=2)
SETTLE = 2  # Seconds a turn's reply waits on the copy for what it set off; a script's delay runs on, as at home.
READ_SECONDS = 5  # Each read of a calendar or forecast the copy holds, before a turn (LiveHome._stores).
MODE_KEY = "assist_improver_practice_mode"
STORE_KEY = "assist_improver.practice_mode"
NO_ASSISTANT = "Sandbox mode needs an AI assistant, so I didn't change anything."
NOT_HERE = "Sandbox mode runs your assistant on a copy of the home; this one can't run there, so nothing was done."
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


async def read(hass, request):
    """A practising copy's read of the real home (sandbox._read_through): one of the lookups it may ask (sandbox.READS)
    is answered as query answers. Anything else is refused."""
    from .sandbox import READS
    domain, service = request["domain"].lower(), request["service"].lower()
    if (domain, service) not in READS:
        raise HomeAssistantError(f"Sandbox mode only reads the real home ({domain}.{service})")
    return await query(hass, domain, service, request.get("data") or {})


async def query(hass, domain, service, data):
    """A query of the real home on Home Assistant's query path (SupportsResponse.ONLY), answered by its own handler, as
    the registry runs it, but never announced on the real bus, where an automation would hear it and the recorder keep
    it; anything else is refused. Its answer as Home Assistant sends a response on: its own types as their dicts."""
    registry = hass.services
    if not registry.has_service(domain, service) or registry.supports_response(domain, service) is not \
            SupportsResponse.ONLY:
        raise HomeAssistantError(f"Sandbox mode only reads the real home ({domain}.{service})")
    handler = registry.async_services_internal()[domain][service]
    call = ServiceCall(hass, domain, service, handler.schema(data) if handler.schema else data, None,
                       return_response=True)
    response = await registry._execute_service(handler, call)  # noqa: SLF001 - the registry's own run, unannounced.
    return json.loads(json_dumps(response))


class _Relay(llm.APIInstance):
    """The person's chat log's tools for a practice turn, as their assistant has them on the copy (provided): each
    call answered with what the copy's call answered (results: {call id: its future}), never run here; once the
    copy's turn is over (end), a call it never answered fails."""

    def __init__(self, hass, llm_context):
        super().__init__(api=SimpleNamespace(hass=hass, id=f"{DOMAIN}_sandbox", name="Sandbox mode"), api_prompt="",
                         llm_context=llm_context, tools=[], custom_serializer=_as_given)
        self.results, self.over = {}, False

    def provided(self, chat_log, given):
        """What the copy's assistant was given, in the person's chat log as Home Assistant's async_provide_llm_data
        leaves it there (its prompt, its API, the update it tells the chat log's subscribers), so the recorder keeps
        the turn as at home."""
        self.api = SimpleNamespace(hass=self.api.hass, id=given["api_id"], name="Sandbox mode")
        self.api_prompt, self.tools = given["api_prompt"], [_Shown(tool) for tool in given["tools"]]
        chat_log.llm_input_provided_index = len(chat_log.content)
        chat_log.llm_api, chat_log.extra_system_prompt = self, given["extra_system_prompt"]
        chat_log.content[0] = SystemContent(content=given["prompt"])
        _async_notify_subscribers(self.api.hass, chat_log.conversation_id, ChatLogEventType.UPDATED,
                                  {"chat_log": chat_log.as_dict()})

    async def async_call_tool(self, tool_input):
        if (future := self.results.get(tool_input.id)) is None:
            future = self.results[tool_input.id] = asyncio.get_running_loop().create_future()
            if self.over:
                self.end()
        result = await future
        self.results.pop(tool_input.id, None)  # Answered: a later round's call by the same id waits for its own.
        if tool_input.tool_name.split("__")[-2:] == FileImprovement.name.split("__"):
            # Filing writes only the improver's own record: the person's conversation is filed, as any day.
            await FileImprovement().async_call(self.api.hass, replace(tool_input, tool_name=FileImprovement.name),
                                               self.llm_context)
        return result

    def end(self):
        self.over = True
        for future in self.results.values():
            if not future.done():
                future.set_exception(HomeAssistantError("Sandbox mode's copy of the home didn't answer"))


class _Shown(llm.Tool):
    """A tool the person's assistant was given on the copy, as it was given: its parameters' JSON schema, read back
    unchanged (_as_given). Never run here (_Relay)."""

    def __init__(self, tool):
        self.name, self.description, self.integration = tool["name"], tool["description"], tool["integration"]
        self.parameters = SimpleNamespace(given=tool["parameters"])


def _as_given(value):
    return value.given if isinstance(value, SimpleNamespace) else llm.selector_serializer(value)


async def _relayed(deltas, relay, chat_log):
    """The copy's reply as the person's chat log takes it: what the assistant was given (relay.provided), then its
    deltas, each tool call as Home Assistant's own; a tool call's result answers the call (_Relay), a provider's own
    tool's comes as the provider gave it."""
    loop, external = asyncio.get_running_loop(), set()
    while (delta := await deltas.get()) is not None:
        if "end" in delta:  # The copy's turn is over: a call it never answered fails (_Relay.end).
            relay.end()
            continue
        if "given" in delta:
            relay.provided(chat_log, delta["given"])
            continue
        if delta.get("role") == "tool_result":
            result = llm.ToolResult(**delta["result"])
            if delta["tool_call_id"] in external:
                yield {"role": "tool_result", "tool_call_id": delta["tool_call_id"], "tool_name": delta["tool_name"],
                       "result": result}
            elif not (future := relay.results.setdefault(delta["tool_call_id"], loop.create_future())).done():
                future.set_result(result)
            continue
        if delta.get("tool_calls"):
            calls = [llm.ToolInput(tool_name=c["tool_name"], tool_args=c["tool_args"], id=c["id"],
                                   external=c["external"]) for c in delta["tool_calls"]]
            external |= {c.id for c in calls if c.external}
            delta = {**delta, "tool_calls": calls}
        yield delta


class LiveHome:
    """One practising conversation's copy of the home: a sandbox in the replay worker, seeded from the home as it is
    when the conversation begins, the person's assistant loaded there; before each turn, the real changes it hasn't
    made since reach it (as a replay's others' moves do)."""

    def __init__(self, hass, conversation_id, start):
        self.hass, self.conversation_id, self.report = hass, conversation_id, PracticeReport(None, None)
        self.start = start  # A started sandbox, a spare one if there is one (PracticeMode.take).
        self.sandbox, self.synced, self.mine, self.lists, self.agents = None, {}, set(), {}, set()
        self.marks = None  # Where the copy's lists stood when the last turn's report ended (sandbox.run_turn).
        self.closed = False  # Stopped: its copy is gone, and none is started for it again.
        self.touched = utcnow()

    async def prepare(self, agent_id, llm_context):
        """The copy as the home is now, with the person's assistant loaded."""
        from .replay import assistant_of
        source = self._now()
        if self.sandbox is not None and not self.sandbox.live:  # The worker stopped, and its copies with it.
            journal("warning", "Practice session's copy of the home ended with the replay worker: a new copy",
                    conversation_id=self.conversation_id)
            self.sandbox, self.synced, self.mine, self.lists, self.agents = None, {}, set(), {}, set()
            self.marks = None
        if self.sandbox is None:
            await self._seed(source, llm_context)
        else:
            await self._mirror(source)
            if (lists := self._lists(source)) and any(lists[e] != self.lists.get(e) for e in lists):
                held = await self.sandbox.call("lists", source=source, lists=lists, given=self.lists, settle=False)
                self.lists |= {e: lists[e] for e in held["held"]}
            # The calendars' own part of the home, for one the copy takes over now (sandbox.hold_stores).
            ids = {e for e in source["states"] if e.startswith("calendar.")}
            await self.sandbox.call("stores", source={
                "states": {e: source["states"][e] for e in ids}, "areas": source.get("areas") or {},
                "entities": {e: v for e, v in (source.get("entities") or {}).items() if e in ids},
                "exposure": {e: v for e, v in (source.get("exposure") or {}).items() if e in ids}},
                **await self._stores(source))
        if source.get("household_on"):
            await self.sandbox.call("household", knowledge=source.get("household"))
        if agent_id not in self.agents:
            if (found := assistant_of(self.hass, agent_id)) is None:
                raise HomeAssistantError(NOT_HERE)
            await self.sandbox.call("assistant", **found)
            self.agents.add(agent_id)
        if outside := await self._outside(agent_id, llm_context):
            await self.sandbox.call("outside", apis=outside)

    async def _outside(self, agent_id, llm_context):
        """The tools the person's assistant has at home from LLM APIs beside Assist (another integration's, a remote
        server's), as the model is shown them: the copy offers each in their place, never runs one (sandbox
        outside_tools)."""
        import probatio
        from .household import API_ID, api_ids
        from .snapshot import subentry_of
        entry = self.hass.config_entries.async_get_entry(getattr(
            er.async_get(self.hass).async_get(agent_id), "config_entry_id", None) or "")
        subentry = entry.subentries.get(subentry_of(self.hass, agent_id)) if entry else None
        apis = api_ids(subentry.data.get("llm_hass_api") if subentry else None)
        outside = {}
        for api_id in apis:
            if api_id in (llm.LLM_API_ASSIST, API_ID):
                continue
            with suppress(HomeAssistantError):  # Gone since: the copy's stand-in has none, as Home Assistant has none.
                api = await llm.async_get_api(self.hass, api_id, llm_context)
                outside[api_id] = {"name": api.api.name, "tools": [{
                    "name": t.name, "description": t.description, "integration": getattr(t, "integration", None),
                    "parameters": plain(probatio.to_openapi(t.parameters, custom_serializer=api.custom_serializer,
                                                            openapi_version="3.1.0"))} for t in api.tools]}
        return outside

    async def turn(self, user_input, listener):
        """The person's words to their assistant on the copy, its reply's deltas to listener as they come; what the
        copy changed, and what it couldn't emulate, into the recording: by the reply, and what ran on after the last
        one (a script's delay)."""
        context = user_input.context
        found = await self.sandbox.call(
            "turn", listener=listener, stream=True, text=user_input.text, conversation_id=self.conversation_id,
            context={"id": context.id, "user_id": context.user_id, "parent_id": context.parent_id},
            language=user_input.language, agent_id=user_input.agent_id, device_id=user_input.device_id,
            extra_system_prompt=user_input.extra_system_prompt, settle=SETTLE, since=self.marks)
        self.marks = found["marks"]
        for change in found["changes"]:
            self.mine.add(change["entity_id"])
            self.report.changed(change["entity_id"], change["old"], change["new"])
        for flag in found["flags"]:
            self.report.unsimulated(flag["service"], flag["args"])
        return found

    async def _seed(self, source, llm_context):
        from .sandbox import seed_options
        from .simulation import reports_movement
        self.sandbox = await self.start()
        if self.closed:  # Sandbox mode ended while it started.
            await self.stop()
            raise HomeAssistantError("Sandbox mode ended")
        # The integrations of Home Assistant's own Assist tools are set up in the copy, the household's scripts with
        # them; its intent scripts never are (the copy answers with Home Assistant's own intents). The improver's
        # filing tool, where Assist has it, answers there as filing does (the person's own is filed here: _Relay).
        real = await llm.async_get_api(self.hass, llm.LLM_API_ASSIST, llm_context)
        tools = {"tools": [{"name": t.name} for t in real.tools if isinstance(t, FileImprovement) or (
            type(t).__module__.startswith("homeassistant.") and t.name.split("__", 1)[0] != HOUSEHOLD_INTENTS)],
                 "context": {"device_id": llm_context.device_id}}
        user = await self.hass.auth.async_get_user(llm_context.context.user_id) if llm_context.context else None
        try:
            self.lists = self._lists(source)
            await self.sandbox.call("seed", source=source, user_name=getattr(user, "name", None),
                                    options=seed_options({"prepared": tools}), scripts=source.get("scripts") or {},
                                    lists=self.lists, timers=self._timers(), reads=True, **await self._stores(source),
                                    moving=reports_movement({"turns": [{"source": source}], "state_changes": []}))
        except Exception:
            await self.stop()
            raise
        self.synced = source["states"]

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

    def _timers(self):
        """The home's running timers, each on its device."""
        from homeassistant.components.intent.const import TIMER_DATA
        return [{"device_id": t.device_id, "name": t.name, "seconds_left": t.seconds_left, "language": t.language,
                 "is_active": t.is_active} for t in getattr(self.hass.data.get(TIMER_DATA), "timers", {}).values()
                if t.device_id]

    async def _stores(self, source):
        """What the copy holds of the home's calendars and weather, read now: each calendar's events from the start of
        today to the end of the coming week (as far as Home Assistant's calendar tool looks), and each kind of
        forecast each weather gives. Each by its integration's own query of the entity, as Home Assistant's query
        service runs it but without the device refresh it asks of a polling device after (nothing is announced on the
        real bus); side by side, each within READ_SECONDS. One that can't be read now isn't renewed."""
        from homeassistant.components.calendar.const import DATA_COMPONENT as CALENDARS
        from homeassistant.components.calendar.services import async_get_events_service
        from homeassistant.components.weather.const import DATA_COMPONENT as WEATHER
        from homeassistant.components.weather.services import _async_get_forecasts_service
        start = dt_util.start_of_local_day()
        week = {"start_date_time": start, "end_date_time": start + timedelta(days=7)}

        async def read(component, query, domain, service, entity_id, data):
            if (entity := self.hass.data[component].get_entity(entity_id) if component in self.hass.data else None) \
                    is None or not entity.available:
                raise LookupError(entity_id)
            async with asyncio.timeout(READ_SECONDS):
                return json.loads(json_dumps(await query(entity, ServiceCall(self.hass, domain, service, data, None,
                                                                             return_response=True))))
        reads = {}
        for entity_id, state in source["states"].items():
            if entity_id.startswith("calendar."):
                reads[entity_id, None] = read(CALENDARS, async_get_events_service, "calendar", "get_events",
                                              entity_id, week)
            elif entity_id.startswith("weather."):
                features = (state.get("attributes") or {}).get("supported_features") or 0
                for kind, flag in (("daily", 1), ("hourly", 2), ("twice_daily", 4)):
                    if features & flag:
                        reads[entity_id, kind] = read(WEATHER, _async_get_forecasts_service, "weather",
                                                      "get_forecasts", entity_id, {"type": kind})
        calendars, forecasts = {}, {}
        for (entity_id, kind), found in zip(reads, await asyncio.gather(*reads.values(), return_exceptions=True)):
            if isinstance(found, Exception):  # Unavailable, too slow, or its integration failing.
                continue
            if kind is None:
                calendars[entity_id] = found["events"]
            else:
                forecasts.setdefault(entity_id, {})[kind] = found["forecast"]
        return {"calendars": calendars, "forecasts": forecasts}

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
        await self.sandbox.call("observe", source={"at": source["at"]}, moves=[moves], settle=False)
        self.synced = source["states"]

    async def stop(self):
        self.closed = True
        if self.sandbox is not None:
            sandbox, self.sandbox = self.sandbox, None
            await sandbox.stop()


def household_assistants(hass):
    """The household's assistants, each provider entry with its agents among them (replay.assistant_of): as a spare
    sandbox loads them, so a practice conversation's first turn finds its assistant there."""
    from homeassistant.helpers import entity_registry as er
    from .cases import CaseError
    from .replay import assistant_of
    grouped, registry = {}, hass.data.get(er.DATA_REGISTRY)
    for entity in getattr(registry, "entities", {}).values():
        if entity.domain != "conversation" or entity.config_subentry_id is None:
            continue
        with suppress(CaseError):
            if (found := assistant_of(hass, entity.entity_id)) is None:
                continue
            group = grouped.setdefault(found["entry"]["entry_id"], {"entry": found["entry"], "agent": []})
            if group["entry"] is not found["entry"]:
                group["entry"]["subentries"] += found["entry"]["subentries"]
            group["agent"].append(found["agent"])
    return list(grouped.values())


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
        from .worker import worker
        self.hass, self.recorder = hass, recorder  # No recorder while the improver's entry isn't loaded.
        worker().reader = partial(read, hass)  # What a practising copy can't hold, the real home answers.
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
        """While anyone is in sandbox mode, a sandbox is started and waiting (it keeps the replay worker up), the
        household's assistants loaded in it, so a practice conversation's first turn only seeds it, never waiting for
        the worker to start (about 1.3 s) or an assistant to load."""
        if self.users and self.spare is None:
            self.spare = self.hass.async_create_task(self._started(preload=True), "assist_improver_practice_spare")

    async def _started(self, preload=False):
        from .replay import RemoteSandbox, custom_providers
        sandbox = await RemoteSandbox.start(self.hass.config.time_zone, custom_providers(self.hass))
        if preload:
            for assistant in household_assistants(self.hass):
                with suppress(Exception):  # Loaded at the conversation's first turn instead, which says why not.
                    await sandbox.call("assistant", **assistant)
        return sandbox

    async def take(self):
        """A started sandbox for a new conversation: the spare, then another one warmed for the next."""
        spare, self.spare = self.spare, None
        self.warm()
        if spare is not None:
            with suppress(Exception):  # A worker that stopped since: start one now.
                if (sandbox := await spare).live:
                    return sandbox
        return await self._started()

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

    def practising(self, context):
        """Whether a request made in this context is the signed-in person's practice."""
        if ROLE_ACTIVE.get():
            return False  # The improver's own AI roles are never practice (a replay's turns run in its sandbox).
        return bool(context and context.user_id and self.is_on(context.user_id))

    async def turn(self, agent, user_input):
        """One practice turn: the person's assistant answers on their copy of the home, its reply streaming into their
        own chat log as it comes, each tool call answered with what the copy's call answered.

        Goals are never asked for: the learning loop works them out, as it does for real use.
        """
        hass = self.hass
        with chat_session.async_get_chat_session(hass, user_input.conversation_id) as session, \
                conversation.async_get_chat_log(hass, session, user_input) as chat_log:
            conversation_id = chat_log.conversation_id
            if self.recorder is not None:
                self.recorder.heard(conversation_id, user_input.context.user_id)
                self.recorder.practice(conversation_id)  # Marks the recording as practice.
            home = self.sessions.get(conversation_id, getattr(self.recorder, "keys", {}).get(conversation_id))
            home.report = PracticeReport(self.recorder, conversation_id)
            # As the assistant's own integration makes it, by its platform (the domain that conversing).
            context = user_input.as_llm_context(getattr(er.async_get(hass).async_get(agent.entity_id), "platform",
                                                        None) or getattr(agent.platform, "platform_name", None))
            await home.prepare(agent.entity_id, context)
            relay, deltas = _Relay(hass, context), asyncio.Queue()
            answered = hass.async_create_task(home.turn(replace(
                user_input, conversation_id=conversation_id, agent_id=agent.entity_id), deltas.put_nowait),
                "assist_improver_practice_turn")

            def ended(_task):  # Whatever it ends with, the stream ends after what the copy sent.
                deltas.put_nowait({"end": True})
                deltas.put_nowait(None)
            answered.add_done_callback(ended)
            chat_log.llm_api = relay
            async for _content in chat_log.async_add_delta_content_stream(agent.entity_id,
                                                                         _relayed(deltas, relay, chat_log)):
                pass
            try:
                found = await answered
            except HomeAssistantError:
                raise
            except Exception as err:  # The worker stopped: the assistant failed, as Home Assistant says one did.
                raise HomeAssistantError("Sandbox mode's copy of the home didn't answer") from err
            for usage in found["usage"]:  # The provider's usage, in the conversation's own trace.
                chat_log.async_trace({"stats": usage})
            if not isinstance(chat_log.content[-1], AssistantContent):  # No reply: the assistant's own error, if any.
                failed = found["reply"].get("response_type") == "error"
                speech = (found["reply"].get("speech") or {}).get("plain", {}).get("speech") if failed else None
                raise HomeAssistantError(speech or "Sandbox mode's copy of the home didn't answer")
            return conversation.async_get_result_from_chat_log(user_input, chat_log)

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
            if mode.practising(user_input.context):
                return None if name in BEFORE_ASSISTANT else mode.without_assistant(user_input, chat_log)
        return await original(agent, user_input, chat_log, *args, **kwargs)
    return answer


def _hook():
    """Hook Home Assistant's way into an assistant (ConversationEntity.internal_async_process), ChatLog's
    async_provide_llm_data and the default agent's own answers, once, when the improver's code loads, and for as long
    as Home Assistant runs: they pass through unless the person speaking is practising (the recorder hears who's
    speaking either way). Sandbox mode itself stays while the improver's entry reloads or fails to set up, so a person
    in it is never handed the real home; only the recording waits for the improver. While Home Assistant is starting,
    until the improver has set up, nothing of it holds sandbox mode yet."""
    process = ConversationEntity.internal_async_process
    provide = ChatLog.async_provide_llm_data

    async def internal_async_process(agent, user_input):
        mode = agent.hass.data.get(MODE_KEY)
        if mode is None or isinstance(agent, DefaultAgent) or not mode.practising(user_input.context):
            return await process(agent, user_input)
        return await mode.turn(agent, user_input)

    async def provide_llm_data(chat_log, llm_context, *args, **kwargs):
        mode = chat_log.hass.data.get(MODE_KEY)
        context = getattr(llm_context, "context", None)
        if mode is not None and mode.recorder is not None:  # Who's speaking, with or without an Assist API.
            mode.recorder.heard(chat_log.conversation_id, getattr(context, "user_id", None))
        if mode is not None and mode.practising(context):  # Not Home Assistant's way into an assistant: refused.
            raise HomeAssistantError(NOT_HERE)
        return await provide(chat_log, llm_context, *args, **kwargs)

    ConversationEntity.internal_async_process = internal_async_process
    ChatLog.async_provide_llm_data = provide_llm_data
    for name in ITSELF:
        setattr(DefaultAgent, name, _answers_itself(name, getattr(DefaultAgent, name)))


_hook()
