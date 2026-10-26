"""Passive ChatLog recorder. No inference client or device-action path.

Each recording's start, close and storage changes go in today's trace (trace.py); snapshots, packing and saves are
timed there too.

A recording keeps the state changes some reader uses (digest.worth_keeping). An active one is saved when a turn is
added, when it closes, and otherwise at most every SAVE_EVERY seconds, so a crash loses at most those seconds; saves
serialize off the event loop."""

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta
from hashlib import sha256
import logging
from uuid import uuid4

import probatio
from homeassistant.components.conversation.chat_log import async_subscribe_chat_logs, current_chat_log
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, EVENT_STATE_CHANGED, __version__ as CORE_VERSION
from homeassistant.core import callback
from homeassistant.helpers.storage import Store
from homeassistant.util.dt import utcnow

from .retention import CLOSED, pack, unpack
from .const import DOMAIN, SAVE_DELAY, STORAGE_VERSION, VERSION
from .digest import flap, worth_keeping
from .records import Recording, RecordingSummary
from .reflective import ROLE_ACTIVE
from .serialization import plain
from .snapshot import answering, provider_config, snapshot, state_record, subentry_of
from .trace import journal, span, timed

_LOGGER = logging.getLogger(__name__)
SAVE_EVERY = 15  # Seconds an active recording's changes may wait to be saved, unless a turn was added or it closed.


def _frozen(session):
    """The recording as it is now, for a save that serializes in another thread: a copy of each list and dict the
    recorder adds to or sets keys in later; their entries are shared, since nothing changes an entry once recorded."""
    def copied(mapping):
        return {k: list(v) if isinstance(v, list) else v for k, v in mapping.items()}
    out = copied(session)
    out["turns"] = [copied(turn) for turn in session.get("turns", [])]
    if isinstance(session.get("practice"), dict):
        out["practice"] = copied(session["practice"])
    return out


def _requests(session):
    """The context IDs of the conversation's requests so far, which the changes Assist makes for them carry."""
    found = {(((t.get("prepared") or {}).get("context") or {}).get("context") or {}).get("id")
             for t in session["turns"]}
    found.discard(None)
    return found


def _agent(content, chat_log):
    """The conversation agent answering, where an event shows it: a tool result's or a reply's agent_id (HA sends
    no event for a reply itself; the chat log the recorder observes has them)."""
    if isinstance(content, dict) and content.get("agent_id"):
        return content["agent_id"]
    replies = [c for c in (chat_log or {}).get("content") or [] if c.get("role") == "assistant" and c.get("agent_id")]
    return replies[-1]["agent_id"] if replies else None


class Recorder:
    def __init__(self, hass):
        self.hass = hass
        self.index_store = Store(hass, STORAGE_VERSION, f"{DOMAIN}.index", private=True, atomic_writes=True)
        self.index: dict[str, RecordingSummary] = {}
        self.sessions: dict[str, Recording] = {}  # The recordings still going, or not yet written.
        self.keys = {}
        self.logs = {}
        self.dirty = set()
        self.urgent = set()  # Unsaved recordings with a turn added: saved at the next tick, not within SAVE_EVERY.
        self.saved = {}  # Loop time of each live recording's last save.
        self.unsubscribers = []
        self.timer = None
        self.lock = asyncio.Lock()
        self.last_error = None
        self.capture_error = None
        self.running = False
        self.on_filed_closed = None  # Called with a filed recording's ID once it ends (on-demand: the pipeline runs).
        self.answering = {}  # Recording ID -> the subentry of the assistant answering it, once it has.
        self.brief_cache = None  # Each recording's brief (sessions.BriefCache), loaded when a proposal first needs it.
        self.on_forgotten = None  # Awaited with a forgotten recording's ID: what was derived from it goes too.
        # Privacy controls (settings): whether new conversations are kept, and whether a person's switch is off.
        self.recording_on = lambda: True
        self.opted_out = lambda user_id: False
        self.ignored = set()  # Conversations not kept (a person whose switch is off): never recorded again.

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
        self.unsubscribers = [
            async_subscribe_chat_logs(self.hass, self.on_chat),
            self.hass.bus.async_listen(EVENT_STATE_CHANGED, self.on_state),
        ]
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
        # HA omits some final assistant notifications. Retain only active logs
        # and sample their serialized state; never attach/replace a listener.
        for key, log in list(self.logs.items()):
            current = plain(log.as_dict())
            session = self.sessions[key]
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
                self.logs.pop(key, None)
                self.saved.pop(key, None)

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
        # The improver's own replays and AI-role calls are never household sessions.
        if not self.running or conversation_id.startswith("improver-replay-") or ROLE_ACTIVE.get():
            return
        if conversation_id in self.ignored or (conversation_id not in self.keys and not self.recording_on()):
            return  # Recording is off (new conversations aren't kept), or the speaker's switch is.
        try:
            self.capture(conversation_id, str(kind), data)
        except Exception as err:
            # A recorder error must never break an ordinary assistant request.
            self.last_error = type(err).__name__
            self.capture_error = self.last_error
            _LOGGER.error("Conversation capture incomplete (%s)", type(err).__name__)
            key = self.keys.get(conversation_id)
            if key in self.sessions:
                self.sessions[key]["limitations"].append(f"capture_error:{type(err).__name__}")
                self.schedule(key)

    def capture(self, conversation_id, kind, data):
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
        session["last_activity"] = now
        session["events"].append({"at": now, "type": kind, "data": plain(data)})
        content = data.get("content", {})
        turn_added = kind == "content_added" and content.get("role") == "user"
        if key not in self.answering and (agent_id := _agent(content, data.get("chat_log"))):
            # Now it's known which of the provider's assistants answers: its configuration is the one kept.
            if (subentry := subentry_of(self.hass, agent_id)) is not None:
                self.answering[key] = subentry
                for turn in session["turns"]:
                    if "prepared" in turn:
                        turn["prepared"]["provider_config"] = answering(turn["prepared"]["provider_config"], subentry)
        if turn_added:
            with span("recorder.snapshot", recording_id=key):  # The whole home, on the event loop: watched.
                source = snapshot(self.hass)
            session["turns"].append({"user": plain(content), "source": source})
        active = current_chat_log.get()
        if active and active.conversation_id == conversation_id and kind == "updated":
            self.logs[key] = active
            if session["turns"]:
                session["turns"][-1].setdefault("preparations", []).append(plain(data.get("chat_log")))
                if active.llm_api is None:
                    session["turns"][-1]["context_limitations"] = [
                        "No selected LLM API: identity, device and tool metadata unavailable from ChatLog"
                    ]
        if active and active.conversation_id == conversation_id and active.llm_api and kind == "updated":
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
            user_id = getattr(getattr(api.llm_context, "context", None), "user_id", None)
            if user_id and self.opted_out(user_id):  # Known once Assist prepares its first answer: forgotten now.
                self.ignored.add(conversation_id)
                self.hass.async_create_task(self.forget(key), "assist_improver_forget")
                return
        if "chat_log" in data:
            session["chat_log"] = plain(data["chat_log"])
        if kind == "deleted":
            session["status"] = "session_expired"
            journal("info", "Recording closed: the conversation ended", recording_id=key,
                    user_turns=len(session["turns"]))
        self.index[key] = {"id": key, "started": session["started"], "updated": now,
                           "status": session["status"], "user_turns": len(session["turns"]),
                           **({"filed": session["filed"]} if session.get("filed") else {}),
                           **{flag: value for flag in ("hidden", "unarchived", "deleting")
                              if (value := (self.index.get(key) or {}).get(flag))}}
        self.schedule(key, soon=turn_added)
        if kind == "deleted":
            self._closed(key)

    def file(self, conversation_id, note=None, user_id=None):
        """Mark a conversation for improvement (on-demand improvements: Assist's file_improvement tool, when the user
        asks): the pipeline takes it in once it ends. Its recording ID, or None if it isn't being recorded."""
        key = self.keys.get(conversation_id)
        session = self.sessions.get(key)
        if session is None:
            return None
        session["filed"] = {"at": utcnow().isoformat(), "via": "assist",
                            **({"note": note.strip()[:500]} if note and note.strip() else {}),
                            **({"by": user_id} if user_id else {})}
        if key in self.index:
            self.index[key]["filed"] = session["filed"]
        self.schedule(key, soon=True)
        journal("info", "Conversation filed for improvement", recording_id=key, note=session["filed"].get("note"))
        if session["status"] != "active":
            self._closed(key)
        return key

    async def file_kept(self, keys, note=None, user_id=None, notify=True, again=()):
        """Mark kept conversations for improvement (the conversations view: the user sends them in), as file() does
        for one going on now; on disk too for those no longer in memory. notify: tell the pipeline of each that has
        ended (on-demand improvements run then); off when a run follows anyway. again: those among them analysed
        before (the quick check found them smooth, or no fix was found), sent in again (pipeline.deliberate). The
        recording IDs marked."""
        filed = {"at": utcnow().isoformat(), "via": "panel",
                 **({"note": note.strip()[:500]} if note and note.strip() else {}),
                 **({"by": user_id} if user_id else {})}
        marked, again = [key for key in dict.fromkeys(keys) if key in self.index], set(again)
        for key in marked:
            mark = {**filed, **({"again": True} if key in again else {})}
            self.index[key]["filed"] = dict(mark)
            self.index[key].pop("unarchived", None)  # In again: once it settles again, it leaves the list on its own.
            if (session := self.sessions.get(key)) is not None:
                session["filed"] = dict(mark)
                self.schedule(key, soon=True)
            else:
                async with self.lock:
                    if (stored := await self.store(key).async_load()) is not None:
                        await self.store(key).async_save({**stored, "filed": dict(mark)})
        if marked:
            await self.save_index()
            journal("info", f"{len(marked)} conversation(s) sent in for improvement", recording_ids=marked,
                    note=filed.get("note"))
        for key in marked if notify else ():
            if self.index[key].get("status") != "active":
                self._closed(key)
        return marked

    def _closed(self, key):
        """A recording ended: a filed one goes to the pipeline (on_filed_closed)."""
        if (self.index.get(key) or {}).get("filed") and self.on_filed_closed is not None:
            self.on_filed_closed(key)

    def practice(self, conversation_id):
        """The practice part of a session's recording, created on first use; None if not recorded."""
        session = self.sessions.get(self.keys.get(conversation_id))
        if session is None:
            return None
        self.schedule(session["id"])
        return session.setdefault("practice", {"simulated_changes": [], "unsimulated_actions": []})

    @callback
    def on_state(self, event):
        if not self.running:
            return
        # A practice recording's readers read its simulated changes; its real ones are someone else's.
        watching = [s for s in self.sessions.values() if s["status"] == "active" and "practice" not in s]
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
        data = self.sessions.get(key) or await self.store(key).async_load()
        if data is None:
            raise ValueError("Recording is unavailable")
        return unpack(deepcopy(data))

    async def save_index(self):
        await self.index_store.async_save({"sessions": deepcopy(self.index)})

    async def delete(self, key):
        """Remove a recording and its index entry for good."""
        async with self.lock:  # After a save in progress, and with nothing left to save, so none lands after this.
            self.sessions.pop(key, None)
            self.logs.pop(key, None)
            self.saved.pop(key, None)
            self.dirty.discard(key)
            self.urgent.discard(key)
            await self.store(key).async_remove()
            self.index.pop(key, None)
            await self.index_store.async_save({"sessions": deepcopy(self.index)})

    def _conversations(self, keys):
        """The Assist conversation IDs of these recordings, while this run of Home Assistant knows them."""
        return {cid for cid, key in self.keys.items() if key in keys}

    async def hide(self, *keys, hidden=True, settled=None):
        """Take kept conversations out of the conversations list (Archive), or put them back (Unarchive); the index is
        saved once. settled: {key: standing} of those out of it on their own (conversations.SETTLED), kept in it once
        put back while they stand so (unarchived). Nothing else changes: they're kept, analysed, tested and replayed
        as before."""
        for key in keys:
            summary = self.index[key]
            summary.pop("hidden", None)
            summary.pop("unarchived", None)
            if hidden:
                summary["hidden"] = True
            elif key in (settled or {}):
                summary["unarchived"] = settled[key]
        await self.save_index()
        journal("info", f"{len(keys)} conversation(s) {'hidden' if hidden else 'unhidden'}", recording_ids=list(keys))

    async def forget_later(self, *keys):
        """Delete conversations once the run in progress ends (Pipeline._ran: forget_queued), or at the next start:
        out of view at once, and not taken in or recorded any further meanwhile."""
        self.ignored |= self._conversations(set(keys))
        for key in keys:
            self.index[key]["deleting"] = True
        await self.save_index()
        journal("info", f"{len(keys)} conversation(s) queued for deletion: a run is in progress", recording_ids=list(keys))

    async def forget_queued(self):
        """Delete the conversations queued for deletion (forget_later), together; if that fails, one by one, so one
        that can't be is put back where it was (in the list, or among the archived when settled), and the household told. The IDs deleted."""
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
        self.ignored |= self._conversations(gone)
        async with self.lock:  # After a save in progress, and with nothing left to save, so none lands after this.
            for key in gone:
                for kept in (self.sessions, self.logs, self.saved, self.answering):
                    kept.pop(key, None)
                self.dirty.discard(key)
                self.urgent.discard(key)
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
