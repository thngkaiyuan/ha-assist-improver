"""Passive ChatLog recorder. No inference client or device-action path.

Each recording's start, close and storage changes go in today's trace (trace.py); snapshots, packing and saves are
timed there too.

A recording keeps the state changes some reader uses (digest.worth_keeping). An active one is saved when a turn is
added, when it closes, and otherwise at most every SAVE_EVERY seconds, so a crash loses at most those seconds; saves,
and each turn's snapshot of the home, serialize off the event loop."""

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta
from hashlib import sha256
import logging
import re
from uuid import uuid4

import probatio
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, EVENT_STATE_CHANGED, __version__ as CORE_VERSION
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store
from homeassistant.util.dt import utcnow

from . import touchpoints
from .retention import CLOSED, pack, unpack
from .const import DOMAIN, SAVE_DELAY, STORAGE_VERSION, VERSION
from .digest import flap, worth_keeping
from .records import Recording, RecordingSummary
from .reflective import ROLE_ACTIVE
from .serialization import plain
from .snapshot import answering, home, provider_config, state_record, subentry_of, taken
from .trace import journal, span, timed

# Its error logs name only the exception's type: a message can carry what the household said or has.
_LOGGER = logging.getLogger(__name__)
SAVE_EVERY = 15  # Seconds an active recording's changes may wait to be saved, unless a turn was added or it closed.
# A voice device's Assistant select (assist_pipeline's AssistPipelineSelect), by its unique ID: one per wake word.
PIPELINE_SELECT = re.compile(r".+-pipeline(_\d+)?")
# What each chat log event carries that the recorder reads, as Home Assistant's subscription gives it.
CARRIES = {"created": "chat_log", "updated": "chat_log", "content_added": "content"}


def _frozen(session):
    """The recording as it is now, for a save that serializes in another thread: a copy of each list and dict the
    recorder adds to or sets keys in later; their entries are shared, since nothing changes an entry once recorded."""
    def copied(mapping):
        return {k: list(v) if isinstance(v, list) else v for k, v in mapping.items()}
    out = copied(session)
    out["turns"] = [copied(turn) for turn in session.get("turns", [])]
    return out


def _copied(recording):
    """A recording as readers get it: their own copy, unpacked (retention.unpack)."""
    return unpack(deepcopy(recording))


def _requests(session):
    """The context IDs of the conversation's requests so far, which the changes Assist makes for them carry."""
    found = {(((t.get("prepared") or {}).get("context") or {}).get("context") or {}).get("id")
             for t in session["turns"]}
    found.discard(None)
    return found


def _pipeline(hass, device_id):
    """The voice assistant (Assist pipeline) a request ran through, as the voice device it came from chooses it: its
    Assistant select, read as the device reads it ("preferred": Home Assistant's preferred assistant). None for a
    request from anywhere else (the app, a browser, an action) or a device with several assistants set (_pipeline_ran
    may tell)."""
    if not device_id or "assist_pipeline" not in hass.config.components:  # Loaded: importing it reads no files.
        return None
    from homeassistant.components.assist_pipeline import (OPTION_PREFERRED, PipelineNotFound, async_get_pipeline,
                                                          async_get_pipelines)
    chosen = {state.state for entry in er.async_entries_for_device(er.async_get(hass), device_id)
              if entry.domain == "select" and PIPELINE_SELECT.fullmatch(entry.unique_id)
              and (state := hass.states.get(entry.entity_id)) is not None}
    if len(chosen) != 1:
        return None
    name = chosen.pop()
    try:
        if name == OPTION_PREFERRED:
            return async_get_pipeline(hass).id
        return next((p.id for p in async_get_pipelines(hass) if p.name == name), None)
    except (KeyError, PipelineNotFound):  # assist_pipeline not set up after all, or no preferred one: not known.
        return None


def _pipeline_ran(hass, conversation_id):
    """The voice assistant a request ran through where its device can't say (_pipeline: the app, a browser, a device
    with several assistants set, a turn Home Assistant answered itself), by Home Assistant's log of its last runs: the
    run whose intent step names this conversation. None for one that didn't run through one (an action), or once that
    log can't be read: the household's view then names the assistant by the AI that answered
    (household.voice_assistant)."""
    if "assist_pipeline" not in hass.config.components:  # Not set up: not known.
        return None
    # No public API names a conversation's pipeline run: assist_pipeline's own debug log of runs, read only here.
    with touchpoints.guarded(hass, "pipeline_debug"):
        touchpoints.get(hass, "pipeline_runs")
        for pipeline_id, runs in list(hass.data[touchpoints.get(hass, "pipeline_debug")].pipeline_debug.items()):
            for run in reversed(list(runs.values())):
                if any(e.type == "intent-start" and (e.data or {}).get("conversation_id") == conversation_id
                       for e in run.events):
                    return pipeline_id
    return None


def _events(kind, data):
    """A chat log event as the recorder takes it in. A new chat log may come with the user's first words already in
    it, and no content_added for them (Home Assistant 2026.12): taken as the created event before them and the
    content_added of them that earlier versions send, so the recording is the same on both."""
    log = data.get("chat_log") if kind == "created" else None
    content = log.get("content") if isinstance(log, dict) else None
    if not content or not isinstance(content[-1], dict) or content[-1].get("role") != "user":
        return [(kind, data)]
    return [(kind, {**data, "chat_log": {**log, "content": content[:-1]}}), ("content_added", {"content": content[-1]})]


def _agent(content, chat_log):
    """The conversation agent answering, where an event shows it: its content's agent_id (a reply's or a tool
    result's), else the last reply's in the chat log it carries."""
    if isinstance(content, dict) and content.get("agent_id"):
        return content["agent_id"]
    replies = [c for c in (chat_log or {}).get("content") or [] if c.get("role") == "assistant" and c.get("agent_id")]
    return replies[-1]["agent_id"] if replies else None


def _given_this_turn(chat_log):
    """The AI was given the chat log up to the latest user words (not just an earlier turn's)."""
    users = [i for i, c in enumerate(chat_log.content) if c.role == "user"]
    return bool(users) and chat_log.llm_input_provided_index == users[-1] + 1


class Recorder:
    def __init__(self, hass):
        self.hass = hass
        self.index_store = Store(hass, STORAGE_VERSION, f"{DOMAIN}.index", private=True, atomic_writes=True)
        self.index: dict[str, RecordingSummary] = {}
        self.sessions: dict[str, Recording] = {}  # The recordings still going, or not yet written.
        self.keys = {}  # Conversation ID -> recording ID, while Home Assistant keeps the conversation or the recording.
        self.logs = {}
        self.sampled = {}  # Recording ID -> its chat log as observe_logs last read it: (log, length, last item).
        self.sourcing = set()  # Turns' homes being serialized off the event loop (_serialized).
        self.dirty = set()
        self.urgent = set()  # Unsaved recordings with a turn added: saved at the next tick, not within SAVE_EVERY.
        self.saved = {}  # Loop time of each live recording's last save.
        self.unsubscribers = []
        self.timer = None
        self.lock = asyncio.Lock()
        self.last_error = None
        self.capture_error = None
        self.running = False
        self.on_filed = None  # Awaited with (recording ID, note, user ID) as Assist files one (flow.sent_in).
        self.on_filed_closed = None  # Called with each recording's ID once it ends: one sent in gets its run.
        self.answering = {}  # Recording ID -> the subentry of the assistant answering it, once it has.
        self.brief_cache = None  # Each recording's brief (sessions.BriefCache), loaded when a proposal first needs it.
        self.on_forgotten = None  # Awaited with a forgotten recording's ID: what was derived from it goes too.
        self.ignored = set()  # Conversations not kept (not Assist's, or deleted while going): never recorded again.

    def store(self, key):
        # Serializes in an executor: flush hands it a copy the event loop doesn't change (_frozen).
        return Store(self.hass, STORAGE_VERSION, f"{DOMAIN}.session_{key}", private=True, atomic_writes=True,
                     serialize_in_event_loop=False)

    async def start(self):
        saved = await self.index_store.async_load()
        self.index = saved.get("sessions", {}) if saved else {}
        for key, summary in self.index.items():
            if summary["status"] == "active":
                # A clean shutdown or interrupted process can split a journey.
                summary["status"] = "interrupted"
                journal("info", "Recording closed: interrupted by a restart", recording_id=key,
                        user_turns=summary.get("user_turns"))
                data = await self.store(key).async_load()
                if data:
                    data["status"] = "interrupted"
                    data["limitations"].append("capture_interrupted_by_restart")
                    await self.store(key).async_save(data)
        await self.index_store.async_save({"sessions": self.index})
        self.running = True
        self.unsubscribers = [self.hass.bus.async_listen(EVENT_STATE_CHANGED, self.on_state)]
        try:  # No public API hears Assist's conversations: its chat logs, their events and what capture reads.
            subscribe = touchpoints.get(self.hass, "chat_log_subscribe")
            touchpoints.get(self.hass, "chat_log_events")
            touchpoints.get(self.hass, "chat_log")
            self.unsubscribers.append(subscribe(self.hass, self.on_chat))
        except touchpoints.Broken:  # Reported: nothing is recorded until the improver is updated.
            pass
        # A once-listener removes itself when it fires; removing it again logs an error.
        self.stop_listener = self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self.on_stop)

    @callback
    def on_stop(self, event):
        self.stop_listener = None
        self.hass.async_create_task(self.stop(), "assist_improver_stop")

    @callback
    def schedule(self, key, soon=False):
        """Mark a recording unsaved; soon (a turn was added) saves it at the next tick."""
        self.dirty.add(key)
        if soon:
            self.urgent.add(key)
        self.arm()

    def due(self, key, now):
        """Whether an unsaved recording is saved at this tick: a turn was added, it closed, it was never saved, or its
        last save is SAVE_EVERY seconds old."""
        return (key in self.urgent or key not in self.saved or self.sessions[key]["status"] != "active"
                or now - self.saved[key] >= SAVE_EVERY)

    @callback
    def arm(self):
        """Wake for the next save due (SAVE_DELAY after a change at the soonest), and every SAVE_DELAY while a chat
        log is watched (observe_logs)."""
        now = self.hass.loop.time()
        times = [now if key not in self.sessions or self.due(key, now) else self.saved[key] + SAVE_EVERY
                 for key in self.dirty] + ([now] if self.logs else [])
        if not times:
            return
        when = max(min(times), now + SAVE_DELAY)
        if self.timer is not None:
            if self.timer.when() <= when:
                return
            self.timer.cancel()
        self.timer = self.hass.loop.call_at(when, self.flush_later)

    @callback
    def flush_later(self):
        self.timer = None
        self.hass.async_create_task(self.flush(due_only=True), "assist_improver_save")

    def observe_logs(self):
        # Retain only active logs and sample their serialized state; never attach/replace a listener.
        for key, log in list(self.logs.items()):
            session = self.sessions[key]
            # Read again only once it grew: HA adds to a conversation's chat log, request after request.
            last = log.content[-1] if log.content else None
            before = self.sampled.get(key)
            if before is None or before[0] is not log or before[1] != len(log.content) or before[2] is not last:
                self.sampled[key] = (log, len(log.content), last)
                self._sample(key, session, log)
            # An exception in an upstream conversation can omit cleanup events.
            # Close only the capture, retain it, and make the gap explicit.
            if (session["status"] == "active" and
                utcnow() - datetime.fromisoformat(session["last_activity"]) > timedelta(minutes=15)):
                session["status"] = "capture_timed_out"
                session["limitations"].append("No conversation activity for 15 minutes; cleanup not observed")
                self.index[key]["status"] = session["status"]
                journal("info", "Recording closed: no activity for 15 minutes", recording_id=key,
                        user_turns=len(session["turns"]))
                self.dirty.add(key)
                self._closed(key)

    def _sample(self, key, session, log):
        current = plain(log.as_dict())
        self._pin(key, _agent(None, current))
        if any(getattr(c, "native", None) is not None for c in log.content):
            limitation = "provider_native_history_omitted; replay adapter must check compatibility"
            if limitation not in session["limitations"]:
                session["limitations"].append(limitation)
                self.dirty.add(key)
        if current != session.get("chat_log"):
            session["chat_log"] = current
            session["last_activity"] = utcnow().isoformat()
            session["events"].append({"at": utcnow().isoformat(),
                                      "type": "log_observed", "data": current})
            self.dirty.add(key)

    async def _sourced(self):
        """Once every turn's home being serialized is attached to its turn (_serialized): a recording is copied or
        saved only then."""
        while self.sourcing:
            await asyncio.wait(set(self.sourcing))

    async def flush(self, due_only=False):
        """Save the unsaved recordings: all of them, or (due_only: the timer's ticks) those due a save."""
        async with self.lock:
            self.observe_logs()
            now = self.hass.loop.time()
            self.dirty &= self.sessions.keys()
            keys = {key for key in self.dirty if not due_only or self.due(key, now)}
            urgent = keys & self.urgent
            self.dirty -= keys
            self.urgent -= keys
            try:
                if keys:
                    async with span("recorder.flush"):
                        await self.save(keys, now)
                self.last_error = self.capture_error
            except Exception as err:
                self.dirty |= keys
                self.urgent |= urgent
                self.last_error = type(err).__name__
                _LOGGER.error("Recording persistence failed (%s); evidence remains pending", type(err).__name__)
                # Retry on next event or shutdown; do not silently discard.
            finally:
                if self.running:
                    self.arm()

    async def save(self, keys, now):
        for key in keys:  # A closed recording is stored packed (retention.py); readers get it unpacked.
            await self._sourced()
            async with span("recorder.pack", recording_id=key):
                stored = _frozen(self.sessions[key])
                if stored.get("status") in CLOSED:  # Packing copies the whole recording: done off the event loop.
                    stored = await self.hass.async_add_executor_job(pack, stored)
            async with timed("store.recordings"):
                await self.store(key).async_save(stored)
            self.saved[key] = now
            if stored.get("packed") and key in self.index:
                if self.index[key].get("stored") != "packed":
                    journal("info", "Recording packed", recording_id=key)
                self.index[key]["stored"] = "packed"
        async with timed("store.recordings_index"):
            await self.index_store.async_save({"sessions": deepcopy(self.index)})
        for key in keys:
            if key not in self.dirty and self.sessions[key]["status"] != "active":
                self.sessions.pop(key)
                for kept in (self.logs, self.sampled, self.saved, self.answering):
                    kept.pop(key, None)

    async def stop(self):
        self.running = False
        for unsubscribe in self.unsubscribers:
            unsubscribe()
        self.unsubscribers.clear()
        if getattr(self, "stop_listener", None):
            self.stop_listener()
            self.stop_listener = None
        if self.timer:
            self.timer.cancel()
            self.timer = None
        await self.flush()

    @callback
    def on_chat(self, conversation_id, kind, data):
        if not self.running:  # A replay's chat log is its sandbox's: none reaches here.
            return
        if kind == "deleted":  # HA's cleanup, minutes later, in whatever context set its timer (an AI role's, say).
            if self.keys.get(conversation_id) not in self.sessions or conversation_id in self.ignored:
                self._ended(conversation_id)  # It ended unrecorded (an AI Task's chat log, say): nothing to keep.
                return
        elif ROLE_ACTIVE.get():  # The improver's own AI-role calls are never household sessions.
            return
        if conversation_id in self.ignored:
            return
        try:
            for kind, data in _events(str(kind), data):
                self.capture(conversation_id, kind, data)
        except Exception as err:  # A recorder error must never break an ordinary assistant request.
            self._failed(self.keys.get(conversation_id), err)

    def _failed(self, key, err):
        self.last_error = self.capture_error = type(err).__name__
        _LOGGER.error("Conversation capture incomplete (%s)", type(err).__name__)
        if key in self.sessions:
            self.sessions[key]["limitations"].append(f"capture_error:{type(err).__name__}")
            self.schedule(key)

    def _ended(self, conversation_id):
        """Home Assistant ended a conversation the recorder isn't following, for good (an ID used again starts a new
        one): it isn't ignored any more, and its recording's ID isn't kept unless its recording is (an ignored one's is
        going)."""
        if self.keys.get(conversation_id) not in self.index or conversation_id in self.ignored:
            self.keys.pop(conversation_id, None)
        self.ignored.discard(conversation_id)

    def capture(self, conversation_id, kind, data):
        if (carries := CARRIES.get(kind)) and not isinstance(data.get(carries), dict):  # Reported until an update.
            touchpoints.report(self.hass, "chat_log_subscribe", f"a {kind} event came without its {carries}")
        key = self.keys.get(conversation_id)
        now = utcnow().isoformat()
        if key not in self.sessions:
            key = sha256(f"{uuid4()}:{conversation_id}".encode()).hexdigest()
            self.keys[conversation_id] = key
            self.sessions[key] = {
                "format": 1, "id": key, "conversation_id": conversation_id, "started": now,
                "core_version": CORE_VERSION, "integration_version": VERSION, "status": "active",
                "last_activity": now,
                "events": [], "turns": [], "state_changes": [], "limitations": [],
            }
            if kind != "created":
                self.sessions[key]["limitations"].append("recording_started_mid_session")
            journal("info", "Recording started", recording_id=key, conversation_id=conversation_id,
                    mid_session=kind != "created")
        session = self.sessions[key]
        if kind == "updated" and not any(c.get("role") == "user" for c in (data.get("chat_log") or {}).get("content", [])):
            self._not_assist(key)
            return
        session["last_activity"] = now
        session["events"].append({"at": now, "type": kind, "data": plain(data)})
        content = data.get("content", {})
        turn_added = kind == "content_added" and content.get("role") == "user"
        self._pin(key, _agent(content, data.get("chat_log")))
        if turn_added:  # The home as the user's words came: read now, serialized off the event loop.
            with span("recorder.snapshot", recording_id=key):  # On the event loop: watched.
                read = taken(self.hass)
            session["turns"].append(turn := {"user": plain(content)})
            task = self.hass.async_create_task(self._serialized(key, turn, read), "assist_improver_snapshot")
            self.sourcing.add(task)
            task.add_done_callback(self.sourcing.discard)
        try:  # The chat log of the request being answered, which says what its AI is given.
            active = touchpoints.get(self.hass, "current_chat_log").get()
        except touchpoints.Broken:
            active = None
        if not active or active.conversation_id != conversation_id:
            active = None
        given = data.get("chat_log") if active and kind == "updated" else None  # The chat log as the AI was given it.
        # Home Assistant gives a turn like the last one the last turn's prompt and tools, without an update: read at
        # its first answer.
        kept = (given is None and active and active.llm_api and content.get("role") == "assistant" and session["turns"]
                and "prepared" not in session["turns"][-1] and _given_this_turn(active))
        if kept:
            given = {**active.as_dict(),
                     "content": [c.as_dict() for c in active.content[:active.llm_input_provided_index]]}
        if given is not None:
            self.logs[key] = active
            if session["turns"]:
                session["turns"][-1].setdefault("preparations", []).append(plain(given))
                if active.llm_api is None:
                    session["turns"][-1]["context_limitations"] = [
                        "No selected LLM API: identity, device and tool metadata unavailable from ChatLog"
                    ]
        if given is not None and active.llm_api:
            api = active.llm_api
            metadata = {"context": plain(api.llm_context), "api_id": api.api.id,
                        "api_prompt": api.api_prompt, "tools": [],
                        "provider_config": answering(provider_config(self.hass, api.llm_context.platform),
                                                     self.answering.get(key))}
            for tool in api.tools:
                metadata["tools"].append({"name": tool.name, "description": tool.description,
                    "integration": tool.integration,
                    "parameters": plain(probatio.to_openapi(tool.parameters,
                        custom_serializer=api.custom_serializer, openapi_version="3.1.0"))})
            if session["turns"]:
                session["turns"][-1]["prepared"] = metadata
        if (kind == "content_added" and content.get("role") == "assistant" and content.get("content")
                and not content.get("tool_calls") and session["turns"]):  # A turn's answer.
            self._check_prepared(session, active, content)
        if "pipeline_id" not in session:  # Its voice assistant, whose settings say what Assist may do.
            found = None
            if given is not None:  # An AI preparing.
                device = active.llm_api.llm_context.device_id if active.llm_api else None
                found = _pipeline(self.hass, device) or _pipeline_ran(self.hass, conversation_id)
            elif kind == "content_added" and content.get("role") == "assistant":  # Home Assistant's own reply.
                found = _pipeline_ran(self.hass, conversation_id)
            if found:
                session["pipeline_id"] = found
        if "chat_log" in data:
            session["chat_log"] = plain(data["chat_log"])
        if kind == "deleted":
            session["status"] = "session_expired"
            journal("info", "Recording closed: the conversation ended", recording_id=key,
                    user_turns=len(session["turns"]))
        self.index[key] = {"id": key, "started": session["started"], "updated": now,
                           "status": session["status"], "user_turns": len(session["turns"]),
                           **({"deleting": True} if (self.index.get(key) or {}).get("deleting") else {})}
        self.schedule(key, soon=turn_added)
        if kind == "deleted":
            self._closed(key)

    def _check_prepared(self, session, active, content):
        """A turn the AI with Assist's tools answered keeps what it was given, which a replay needs. One answered
        without it means Home Assistant changed how its chat log tells it: reported, never kept silently incomplete.
        A reply something else gave (a sentence trigger, Home Assistant's own intents) isn't the AI's: not checked."""
        if "prepared" in session["turns"][-1]:
            touchpoints.ok(self.hass, "chat_log_events")
        elif (active is not None and active.llm_api is not None
              and content.get("agent_id") == active.llm_api.llm_context.agent_id):
            touchpoints.report(self.hass, "chat_log_events",
                               f"turn {len(session['turns'])} was recorded without what its AI was given")

    async def _serialized(self, key, turn, now):
        """A turn's home, as snapshot.taken read it on the event loop, serialized in the executor and attached to the
        turn; a recording is copied or saved once it is (_sourced)."""
        try:
            async with span("recorder.serialize", recording_id=key):
                turn["source"] = await self.hass.async_add_executor_job(home, now)
        except Exception as err:  # noqa: BLE001 - a recorder error must never break an ordinary assistant request.
            self._failed(key, err)
        finally:  # A turn without its home isn't kept (it can't be read or replayed), as when reading it failed.
            if "source" not in turn and (session := self.sessions.get(key)) is not None:
                session["turns"] = [t for t in session["turns"] if t is not turn]
                if key in self.index:
                    self.index[key]["user_turns"] = len(session["turns"])

    def _pin(self, key, agent_id):
        """Now it's known which of the provider's assistants answers: its configuration is the one kept (P8.2.3)."""
        if key in self.answering or not agent_id or (subentry := subentry_of(self.hass, agent_id)) is None:
            return
        self.answering[key] = subentry
        for turn in self.sessions[key]["turns"]:
            if "prepared" in turn:
                turn["prepared"]["provider_config"] = answering(turn["prepared"]["provider_config"], subentry)
        self.dirty.add(key)

    def _not_assist(self, key):
        """A chat log prepared with no words in it: Home Assistant adds the user's words as it opens the log for an
        Assist request, while an AI Task (another integration's, say) prepares its prompt first. It isn't one of the
        household's Assist conversations (P8.1), so it isn't kept, and the conversation isn't recorded again."""
        self.ignored.add(self.sessions[key]["conversation_id"])
        journal("info", "Recording dropped: not an Assist conversation", recording_id=key)
        self.hass.async_create_task(self.delete(key), "assist_improver_drop")

    async def file(self, conversation_id, note=None, user_id=None):
        """Assist's file_improvement tool, when the user asks: the conversation it's in is sent in (on_filed), and the
        pipeline takes it in once it ends. Its recording ID, or None if it isn't being recorded."""
        key = self.keys.get(conversation_id)
        session = self.sessions.get(key)
        if session is None:
            return None
        await self.on_filed(key, note, user_id)
        journal("info", "Conversation filed for improvement", recording_id=key, note=note)
        if session["status"] != "active":
            self._closed(key)
        return key

    def _closed(self, key):
        """A recording ended: one sent in goes to the pipeline (on_filed_closed)."""
        if self.on_filed_closed is not None:
            self.on_filed_closed(key)

    @callback
    def on_state(self, event):
        if not self.running:
            return
        watching = [s for s in self.sessions.values() if s["status"] == "active"]
        if not watching:
            return
        change = {"at": plain(event.time_fired), "entity_id": event.data["entity_id"],
                  "old": state_record(event.data.get("old_state")),
                  "new": state_record(event.data.get("new_state"))}
        for session in watching:
            if worth_keeping(change, _requests(session)):
                session["state_changes"].append(change)
            elif flap(change):  # The digest says how many there were.
                session["flaps_not_kept"] = session.get("flaps_not_kept", 0) + 1
            else:
                continue
            self.schedule(session["id"])

    async def export(self, key):
        if key not in self.index:
            raise ValueError("Unknown recording ID")
        await self.flush(due_only=True)  # Samples the chat logs; a live recording is read from memory.
        await self._sourced()
        if key in self.sessions:  # Still changing on the event loop: the copy starts from it as it is now.
            data = _frozen(self.sessions[key])
        elif (data := await self.store(key).async_load()) is None:
            raise ValueError("Recording is unavailable")
        # Copying and unpacking the whole recording: done off the event loop.
        return await self.hass.async_add_executor_job(_copied, data)

    async def save_index(self):
        await self.index_store.async_save({"sessions": deepcopy(self.index)})

    async def delete(self, key):
        """Remove a recording and its index entry for good."""
        async with self.lock:  # After a save in progress, and with nothing left to save, so none lands after this.
            self._drop(key)
            await self.store(key).async_remove()
            self.index.pop(key, None)
            await self.index_store.async_save({"sessions": deepcopy(self.index)})

    def _drop(self, key):
        """What's kept in memory of a recording going for good, its conversation's ID among it."""
        for kept in (self.sessions, self.logs, self.sampled, self.saved, self.answering):
            kept.pop(key, None)
        self.dirty.discard(key)
        self.urgent.discard(key)
        for conversation_id in [c for c, k in self.keys.items() if k == key]:
            del self.keys[conversation_id]

    def _going(self, keys):
        """The Assist conversations of these recordings Home Assistant may still be having (not those it ended)."""
        return {cid for cid, key in self.keys.items()
                if key in keys and (self.index.get(key) or {}).get("status") != "session_expired"}

    async def forget_later(self, *keys):
        """Delete conversations once the run in progress ends (Pipeline._ran: forget_queued), or at the next start:
        out of view at once, and not taken in or recorded any further meanwhile."""
        self.ignored |= self._going(set(keys))
        for key in keys:
            self.index[key]["deleting"] = True
        await self.save_index()
        journal("info", f"{len(keys)} conversation(s) queued for deletion: a run is in progress", recording_ids=list(keys))

    async def forget_queued(self):
        """Delete the conversations queued for deletion (forget_later), together; if that fails, one by one, so one
        that can't be is put back where it was, and the household told. The IDs deleted."""
        from homeassistant.components import persistent_notification
        queued = [k for k, s in self.index.items() if s.get("deleting")]
        if not queued:
            return []
        try:
            await self.forget(*queued)
            return queued
        except Exception as err:  # noqa: BLE001 - then one by one: each that fails is put back; the others still go.
            _LOGGER.warning("Deleting %d queued conversations together failed (%s); one by one", len(queued),
                            type(err).__name__)
        done, failed = [], []
        for key in [k for k in queued if k in self.index]:
            try:
                await self.forget(key)
                done.append(key)
            except Exception as err:  # noqa: BLE001
                _LOGGER.error("A conversation queued for deletion couldn't be deleted (%s)", type(err).__name__)
                journal("error", f"Conversation not deleted: {type(err).__name__}", recording_id=key)
                if key in self.index:
                    self.index[key].pop("deleting", None)
                    failed.append(key)
        if failed:
            await self.save_index()
            persistent_notification.async_create(
                self.hass, "A conversation couldn't be deleted. Try deleting it again.",
                title="Assist Improver", notification_id=f"{DOMAIN}_not_deleted")
        return done

    async def forget(self, *keys):
        """Conversations the household doesn't want kept: their recordings, and what the improver derived from them
        (their run-log records, their briefs, and what on_forgotten drops), go for good, each store written once
        however many go. Improvements already built from them stay. One still going isn't recorded again."""
        gone = set(keys)
        self.ignored |= self._going(gone)
        async with self.lock:  # After a save in progress, and with nothing left to save, so none lands after this.
            for key in gone:
                self._drop(key)
                await self.store(key).async_remove()
                self.index.pop(key, None)
            await self.index_store.async_save({"sessions": deepcopy(self.index)})
        if self.on_forgotten is not None:
            await self.on_forgotten(gone)
        if self.brief_cache is None:  # No proposal has read the briefs since Home Assistant started.
            from .sessions import BriefCache
            self.brief_cache = BriefCache(self.hass)
        await self.brief_cache.drop(self, *gone)
        journal("info", f"{len(gone)} recording(s) deleted at the household's request", recording_ids=sorted(gone))

    def status(self):
        return {"recording": self.running, "sessions": len(self.index),
                "pending_writes": len(self.dirty), "last_error": self.last_error}
