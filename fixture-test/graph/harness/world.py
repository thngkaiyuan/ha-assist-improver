"""The household's world: an offline Home Assistant with the improver installed, Assist's real tools and scripts, its
AI roles (on Home Assistant's real AI Task path: ha_task.py) and its assistant played by stand-ins (roles.py), on an
event loop of its own, so graph tests stay plain functions. Built as kit.py's improver fixture is; the suite's conftest
closes each world after its test, and a stand-in's call off its tool's schema fails it then."""

import asyncio
from datetime import timedelta
import shutil
import tempfile

from homeassistant.util.dt import utcnow
from pytest import MonkeyPatch

OPEN = []  # Worlds not closed yet.
DEADLINE = 120  # Seconds a run may take to finish before the harness calls it stalled.


def close_all():
    slips = []
    while OPEN:
        world = OPEN[-1]
        world.close()
        slips += world.slips
    assert not slips, f"The stand-ins called tools off their schemas: {slips}"


class World:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.patch = MonkeyPatch()
        self.dir = tempfile.mkdtemp(prefix="household-")
        self.conversations = {}  # {first request: Conversation}
        self.hass = self.entry = self.runtime = None
        self.slips = []  # The stand-ins' calls off their tools' schemas (ha_task).
        self.timers = []  # [((hour, minute, second), action)]: what Home Assistant was asked to call at a time of day.
        OPEN.append(self)
        try:
            self.call(self._setup())
        except BaseException:
            self.close()
            raise

    def call(self, coro):
        """Runs a coroutine on the world's loop to the end, and whatever it started that the product waits on."""
        return self.loop.run_until_complete(coro)

    async def _setup(self):
        from homeassistant import config as conf_util
        from homeassistant.core import HomeAssistant, ServiceRegistry
        from homeassistant.setup import async_setup_component

        from conftest import assist_home
        import ha_task
        from recordings import bind_agent, prepared_recording
        from test_changeset import _original_async_call
        from test_milestone1 import fake_entry
        from custom_components.assist_improver import async_setup_entry
        from custom_components.assist_improver.const import CONF_AI_TASK, CONF_SCHEDULE, CONF_TRIAGE_AI_TASK
        from homeassistant.helpers import event

        from .corpus import ABLE, ENTITIES
        from .roles import Assistant, Roles

        self.hass = hass = HomeAssistant(self.dir)
        guarded, original = ServiceRegistry.async_call, _original_async_call()

        async def allow_script_reload(registry, domain, service, *args, **kwargs):
            # Reloading scripts is configuration, not a device action: accepting a changeset does it.
            if (domain, service) == ("script", "reload"):
                return await original(registry, domain, service, *args, **kwargs)
            return await guarded(registry, domain, service, *args, **kwargs)
        self.patch.setattr(ServiceRegistry, "async_call", allow_script_reload)
        await assist_home(hass, ENTITIES)  # The household's devices, as the kinds of conversation need them.
        for entity_id, (state, name) in ENTITIES.items():  # And what each can do.
            if able := ABLE.get(entity_id.split(".")[0]):
                hass.states.async_set(entity_id, state, {"friendly_name": name, **able})
        open(hass.config.path("configuration.yaml"), "w").write("script: !include scripts.yaml\n")
        open(hass.config.path("scripts.yaml"), "w").write("{}\n")
        assert await async_setup_component(hass, "script", await conf_util.async_hass_config_yaml(hass))

        for entity_id in ha_task.ENTITIES:
            hass.states.async_set(entity_id, "unknown")
        self.roles = Roles(self.conversations, current_run=lambda: (self.runtime.pipeline.current or {}).get("id"))
        self.slips = ha_task.serve(hass, self.roles)
        assistant, client = await bind_agent(hass, prepared_recording())  # The household's own assistant.
        client.reply = Assistant(client, self.conversations, self.roles, hass).answer
        self.client = client

        def at_time_of_day(_hass, action, hour=None, minute=None, second=None):
            # Home Assistant's clock is the world's: a time of day comes only when a test says so (at).
            timer = ((hour, minute, second), action)
            self.timers.append(timer)
            return lambda: timer in self.timers and self.timers.remove(timer)
        self.patch.setattr(event, "async_track_time_change", at_time_of_day)
        # As a new install sets it up, with the household's daily schedule.
        self.entry = fake_entry({CONF_AI_TASK: "ai_task.opus", CONF_TRIAGE_AI_TASK: "ai_task.haiku",
                                 CONF_SCHEDULE: "daily"})
        await async_setup_entry(hass, self.entry)
        self.runtime = self.entry.runtime_data

    async def arrives(self, talk, going_on=False):
        """A conversation, as the recorder keeps it: each turn with the scripts and automations the household had then
        (recordings.as_kept). Finished, it ended now; going on, it's the recorder's live recording of it, until its
        chat session ends (ends)."""
        from recordings import as_kept
        self.conversations[talk.said] = talk
        record, recorder = as_kept(self.hass, talk.record), self.runtime.recorder
        now = utcnow().isoformat()
        if going_on:  # As Recorder.capture keeps a conversation it records, under its chat session's id.
            record.update(conversation_id=f"conversation-{record['id']}", status="active", started=now,
                          last_activity=now, events=[])
            recorder.keys[record["conversation_id"]], recorder.sessions[record["id"]] = record["id"], record
        else:
            await recorder.store(record["id"]).async_save(record)
        recorder.index[record["id"]] = {
            "id": record["id"], "started": now, "updated": now, "status": record["status"],
            "user_turns": len(record["turns"])}
        if talk.kind.note:  # Filed from Assist with a note as it went on: sent in, as Recorder.file sends one in.
            await recorder.on_filed(record["id"], talk.kind.note, None)

    async def files(self, talk):
        """The household asks Assist to file the conversation as it goes on: Assist calls the improver's tool for it,
        from the conversation's chat log (llm.FileImprovement)."""
        from homeassistant.components.conversation.chat_log import ChatLog, current_chat_log
        from homeassistant.core import Context
        from homeassistant.helpers import llm
        from custom_components.assist_improver.llm import FileImprovement
        token = current_chat_log.set(ChatLog(self.hass, talk.record["conversation_id"]))
        try:
            await FileImprovement().async_call(self.hass, llm.ToolInput(FileImprovement.name, {}), llm.LLMContext(
                platform="anthropic", context=Context(user_id="alex"), language="en", assistant="conversation",
                device_id=None))
        finally:
            current_chat_log.reset(token)

    async def ends(self, *talks):
        """Conversations going on end together: Home Assistant's cleanup expires their chat sessions in one go, and
        tells the chat logs' subscribers (the recorder) each one's log is deleted."""
        from homeassistant.components.conversation.chat_log import _async_notify_subscribers
        from homeassistant.components.conversation.const import ChatLogEventType
        for talk in talks:
            _async_notify_subscribers(self.hass, talk.record["conversation_id"], ChatLogEventType.DELETED, {})

    async def at(self, hour, minute):
        """That time of day comes: Home Assistant calls what it was asked to call then, with the local time."""
        from homeassistant.util import dt as dt_util
        now = dt_util.now().replace(hour=hour, minute=minute, second=0, microsecond=0)
        for when, action in list(self.timers):
            if when == (hour, minute, 0) and asyncio.iscoroutine(result := action(now)):
                await result

    async def settle(self):
        """Waits for a run in progress, any its end starts right after it, and what Home Assistant was doing, to finish:
        a run that doesn't within the deadline fails the test, rather than hanging the suite. Then Home Assistant cleans
        up the chat sessions the runs' AI Tasks opened, as it does minutes later. At rest, no kept conversation is left
        between stages unheld: each is at Your Assist, Your review or Your household, or held with a cause and a why."""
        from homeassistant.helpers.chat_session import DATA_CHAT_SESSION_CLEANUP

        def running():  # A run in progress, or one queued for right after it (Pipeline._ran starts it once the run
            pipeline = self.runtime.pipeline  # before has ended, a loop turn or more later).
            return pipeline.running or bool(pipeline._filed or pipeline._sent_back)
        try:
            async with asyncio.timeout(DEADLINE):
                await self.hass.async_block_till_done()
                while running():  # Rechecked once HA has run what each run's end started.
                    await asyncio.sleep(0.01)
                    await self.hass.async_block_till_done()
                if cleanup := self.hass.data.get(DATA_CHAT_SESSION_CLEANUP):
                    cleanup._cleanup(utcnow() + timedelta(minutes=6))
                    await self.hass.async_block_till_done()
            index = self.runtime.recorder.index
            stranded = [conv for rid, conv in self.runtime.flow.conversations.items()  # Not flow.record: it makes one.
                        if rid in index and not index[rid].get("deleting")
                        and conv["stage"] not in ("Your Assist", "Your review", "Your household")
                        and not all((conv["held"] or {}).get(key) for key in ("cause", "why"))]
            assert not stranded, "Left between stages, unheld (SPEC P0.6, P0.8): " + "; ".join(
                f"{conv['id'][:8]} at {conv['stage']}, round {conv['round']}, held {conv['held']}" for conv in stranded)
        except TimeoutError:
            raise AssertionError(f"The run never finished (waited {DEADLINE} s)") from None

    async def ws(self, handler, **msg):
        """A panel command, as the household's admin user sends it from the open panel, which saves the scripts a
        change writes through HA's script editor: its result, or (code, message) for an error."""
        from editors import Connection, Editors, command, open_panel
        connection = Connection()
        await open_panel(self.hass, connection, Editors(self.hass))
        try:
            reply = await command(self.hass, connection, handler, **msg)
        finally:
            connection.close()
        return reply if reply[0] == "ok" else ("error", reply[1:])

    def close(self):
        if self in OPEN:
            OPEN.remove(self)
        try:
            if self.hass is not None:
                self.call(self._teardown())
        finally:
            self.patch.undo()
            self.loop.close()
            shutil.rmtree(self.dir, ignore_errors=True)

    async def _teardown(self):
        from custom_components.assist_improver import async_unload_entry
        from custom_components.assist_improver.worker import shutdown
        if self.runtime is not None:
            await async_unload_entry(self.hass, self.entry)
        await self.hass.async_block_till_done()
        await shutdown(self.hass)
        await self.hass.async_stop()
