"""Passive ChatLog recorder. No inference client or device-action path."""

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

from .const import DOMAIN, SAVE_DELAY, STORAGE_VERSION, VERSION
from .reflective import ROLE_ACTIVE
from .serialization import plain
from .snapshot import provider_config, snapshot, state_record

_LOGGER = logging.getLogger(__name__)


class Recorder:
    def __init__(self, hass):
        self.hass = hass
        self.index_store = Store(hass, STORAGE_VERSION, f"{DOMAIN}.index", private=True, atomic_writes=True)
        self.index = {}
        self.sessions = {}
        self.keys = {}
        self.logs = {}
        self.dirty = set()
        self.unsubscribers = []
        self.timer = None
        self.lock = asyncio.Lock()
        self.last_error = None
        self.capture_error = None
        self.running = False

    def store(self, key):
        return Store(self.hass, STORAGE_VERSION, f"{DOMAIN}.session_{key}", private=True, atomic_writes=True)

    async def start(self):
        saved = await self.index_store.async_load()
        self.index = saved.get("sessions", {}) if saved else {}
        for key, summary in self.index.items():
            if summary["status"] == "active":
                # A clean shutdown or interrupted process can split a journey.
                summary["status"] = "interrupted"
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
    def schedule(self, key):
        self.dirty.add(key)
        if self.timer is None:
            self.timer = self.hass.loop.call_later(SAVE_DELAY, self.flush_later)

    @callback
    def flush_later(self):
        self.timer = None
        self.hass.async_create_task(self.flush(), "assist_improver_save")

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
                self.dirty.add(key)

    async def flush(self):
        async with self.lock:
            self.observe_logs()
            keys, self.dirty = self.dirty, set()
            try:
                for key in keys:
                    await self.store(key).async_save(deepcopy(self.sessions[key]))
                if keys:
                    await self.index_store.async_save({"sessions": deepcopy(self.index)})
                self.last_error = self.capture_error
                for key in keys:
                    if key not in self.dirty and self.sessions[key]["status"] != "active":
                        self.sessions.pop(key)
                        self.logs.pop(key, None)
            except Exception as err:
                self.dirty |= keys
                self.last_error = type(err).__name__
                _LOGGER.error("Recording persistence failed (%s); evidence remains pending", type(err).__name__)
                # Retry on next event or shutdown; do not silently discard.
            finally:
                if self.running and self.logs and self.timer is None:
                    self.timer = self.hass.loop.call_later(SAVE_DELAY, self.flush_later)

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
        session = self.sessions[key]
        session["last_activity"] = now
        session["events"].append({"at": now, "type": kind, "data": plain(data)})
        content = data.get("content", {})
        if kind == "content_added" and content.get("role") == "user":
            session["turns"].append({"user": plain(content), "source": snapshot(self.hass)})
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
                        "provider_config": provider_config(self.hass, api.llm_context.platform)}
            for tool in api.tools:
                metadata["tools"].append({"name": tool.name, "description": tool.description,
                    "integration": tool.integration,
                    "parameters": plain(probatio.to_openapi(tool.parameters,
                        custom_serializer=api.custom_serializer, openapi_version="3.1.0"))})
            if session["turns"]:
                session["turns"][-1]["prepared"] = metadata
        if "chat_log" in data:
            session["chat_log"] = plain(data["chat_log"])
        if kind == "deleted":
            session["status"] = "session_expired"
        self.index[key] = {"id": key, "started": session["started"], "updated": now,
                           "status": session["status"], "user_turns": len(session["turns"])}
        self.schedule(key)

    @callback
    def on_state(self, event):
        if not self.running:
            return
        change = {"at": plain(event.time_fired), "entity_id": event.data["entity_id"],
                  "old": state_record(event.data.get("old_state")),
                  "new": state_record(event.data.get("new_state"))}
        for key, session in self.sessions.items():
            if session["status"] == "active":
                session["state_changes"].append(change)
                self.schedule(key)

    async def export(self, key):
        if key not in self.index:
            raise ValueError("Unknown recording ID")
        await self.flush()
        data = self.sessions.get(key) or await self.store(key).async_load()
        if data is None:
            raise ValueError("Recording is unavailable")
        return deepcopy(data)

    def status(self):
        return {"recording": self.running, "sessions": len(self.index),
                "pending_writes": len(self.dirty), "last_error": self.last_error,
                "coverage": "common ChatLog only; native intents/provider-native history are not replay-ready"}
