"""The panel's websocket commands and the integration's services, as testable specs.

The integration is set up in the offline Home Assistant and the panel talks to it as the browser does: through HA's
own websocket connection (its message checks, its admin check and its replies). Services go through HA's own service
registry. Only the AI model (HA's AI Task) is scripted, and an AI provider's add-AI-Task flow where one is needed.
"""

import asyncio
import json
import logging
import shutil
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import probatio
import pytest
import pytest_asyncio
from homeassistant.auth.const import GROUP_ID_ADMIN, GROUP_ID_USER
from homeassistant.auth.models import Group, User
from homeassistant.components import ai_task, websocket_api
from homeassistant.components.websocket_api.connection import ActiveConnection
from homeassistant.core import Context, HomeAssistant, ServiceRegistry
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError, Unauthorized
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

import custom_components.assist_improver as integration
from conftest import load_registries, loaded_for, translated
from custom_components.assist_improver import DEVELOPER_SERVICES, SERVICES, async_setup_entry, async_unload_entry, panel
from custom_components.assist_improver.const import CONF_AI_TASK, CONF_TESTS_PER_GOAL, DOMAIN, VERSION
from recordings import make_recording
from test_changeset import _original_async_call, tested
from test_milestone1 import recording_hass  # noqa: F401  (fixture)

ALEX, JORDAN, VISITOR = "user-alex", "user-jordan", "user-visitor"
COMMANDS = {  # Each of the panel's commands (household ones aside), with a valid message.
    "improvements": {}, "improvement": {"improvement_id": "imp-a"},
    "decide": {"improvement_id": "imp-a", "decision": "decline", "note": ""},
    "run": {}, "run/start": {"confirmed": True}, "run/cancel": {},
    "settings": {}, "settings/set": {"tests_per_goal": 5}, "settings/models": {"entry_id": "provider"},
    "settings/create": {"entry_id": "provider", "model": "claude-haiku-5-5", "role": "triage"},
    "settings/recommended": {}, "log": {},
}


def person(name, user_id, *, admin=True):
    """A Home Assistant user: an administrator, or an ordinary user."""
    group = Group(name="Administrators" if admin else "Users", policy={}, id=GROUP_ID_ADMIN if admin else GROUP_ID_USER)
    return User(name=name, perm_lookup=None, id=user_id, is_active=True, groups=[group])


class Client:
    """A websocket connection as the panel's: HA's own message handling, schema checks and admin check run, and the
    replies come back as the browser receives them (JSON)."""

    def __init__(self, hass, user):
        self.next_id, self.waiting = 0, {}
        hass.data.setdefault(websocket_api.DOMAIN, {})
        self.connection = ActiveConnection(logging.getLogger(__name__), hass, self._receive, user, None, None)

    def _receive(self, message):
        if isinstance(message, (bytes, str)):
            message = json.loads(message)
        if (future := self.waiting.get(message["id"])) is not None and not future.done():
            future.set_result(message)

    async def send(self, command, *, domain=DOMAIN, **fields):
        """{success, result} or {success: False, error: {code, message}}; another domain's command for domain."""
        self.next_id += 1
        future = self.waiting[self.next_id] = asyncio.get_running_loop().create_future()
        self.connection.async_handle({"id": self.next_id, "type": f"{domain}/{command}", **fields})
        return await asyncio.wait_for(future, 10)

    async def result(self, command, **fields):
        reply = await self.send(command, **fields)
        assert reply["success"], reply
        return reply["result"]

    async def error(self, command, **fields):
        reply = await self.send(command, **fields)
        assert not reply["success"], reply
        return reply["error"]["code"], reply["error"]["message"]


class Entry:
    """The parts of the improver's config entry that setup, the settings commands and unloading use, behaving as
    HA's own: options changes reach the update listeners, unloading runs what setup asked to run on unload, and the
    tasks tied to it are waited for (background ones cancelled first)."""

    def __init__(self, options=None):
        self.entry_id, self.domain, self.title, self.data = "fixture-improver", DOMAIN, "Assist Improver", {}
        self.options = dict(options or {})
        self.on_unload, self.update_listeners, self.tasks, self.background = [], [], set(), set()

    def async_on_unload(self, func):
        self.on_unload.append(func)

    def async_create_task(self, hass, target, name=None, eager_start=True):
        return self._tied(self.tasks, hass.async_create_task(target, name, eager_start))

    def async_create_background_task(self, hass, target, name, eager_start=True):
        return self._tied(self.background, hass.async_create_background_task(target, name, eager_start))

    @staticmethod
    def _tied(tasks, task):
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return task

    async def unloaded(self):
        """As HA ends an entry, after its own unload or a setup that failed: what it asked to run on unload, then its
        tasks, background ones cancelled."""
        while self.on_unload:
            if (job := self.on_unload.pop()()) is not None:
                self._tied(self.tasks, asyncio.ensure_future(job))
        for task in self.background:
            task.cancel()
        if pending := self.tasks | self.background:
            await asyncio.wait(pending, timeout=10)

    def add_update_listener(self, listener):
        self.update_listeners.append(listener)
        return lambda: self.update_listeners.remove(listener)


class Model:
    """HA's AI Task, scripted: answers (the editor through its tool, as a model would), or holds each call until
    released (a run in progress)."""

    def __init__(self):
        self.calls, self.held = [], False
        self.entered, self.release = asyncio.Event(), asyncio.Event()

    async def __call__(self, hass, *, task_name, entity_id, instructions, structure=None, llm_api=None, **_):
        role = task_name.split(": ", 1)[1]
        self.calls.append((role, entity_id))
        if self.held:
            self.entered.set()
            await self.release.wait()
            raise RuntimeError("The model is overloaded")
        if role == "editor":
            tools = {t.name: t for t in (await llm_api.async_get_api_instance(None)).tools}
            await tools["submit_copy"].async_call(hass, SimpleNamespace(tool_args={
                "headline": "Call the front light lamp", "benefit": "Say “lamp” and the front light turns on."}),
                None)
            return SimpleNamespace(data="Written.", conversation_id="scripted")
        return SimpleNamespace(data={"smooth": True, "sure": True, "why": "Every request was done as asked."},
                               conversation_id="scripted")


class Home:
    """The offline Home Assistant with the improver set up, and what the tests do to it."""

    def __init__(self, hass, entry, model, people):
        self.hass, self.entry, self.model, self.people = hass, entry, model, people
        self.loaded, self.reloads, self.clients = False, [], {}

    @property
    def runtime(self):
        return self.entry.runtime_data

    @property
    def recorder(self):
        return self.entry.runtime_data.recorder

    async def setup(self):
        # None, as Home Assistant asks of setup and unload: it reports a boolean.
        assert await async_setup_entry(self.hass, self.entry) is None
        self.loaded = True

    async def unload(self):
        """As HA unloads an entry: the integration's own unload, then what setup asked to run on unload."""
        assert await async_unload_entry(self.hass, self.entry) is None
        await self.entry.unloaded()
        self.loaded = False

    async def reload(self, entry_id):
        self.reloads.append(entry_id)
        await self.unload()
        await self.setup()

    def client(self, user_id=ALEX):
        if user_id not in self.clients:
            self.clients[user_id] = Client(self.hass, self.people[user_id])
        return self.clients[user_id]

    async def arrives(self, record):
        """A conversation finishing, as the recorder keeps it."""
        await self.recorder.store(record["id"]).async_save(record)
        self.recorder.index[record["id"]] = {"status": record["status"], "user_turns": len(record["turns"])}

    def post(self, *items):
        """Improvements in the list (as the pipeline leaves them), and the notification and sidebar following."""
        for item in items:
            self.runtime.improvements.items[item["id"]] = item
        self.runtime.improvements._sync()

    async def run_finished(self):
        """Waits for the run in progress to end."""
        for _ in range(1000):
            if not self.runtime.pipeline.running:
                return
            await asyncio.sleep(0.01)
        raise AssertionError("The run never finished")


def lamp_test(**fields):
    """Its test: the lamp turned on in one turn with the change, where the conversation took three."""
    return tested("Turn on the lamp", **{"said": ("Turn on the lamp.", "The front light.", "Yes, that one."),
                                         "replies": ("", "", ""), "after": ("",), **fields})


def improvement(improvement_id, *, headline=None, status="awaiting_review", created=None, changes=None, test=None,
                written=True, **extra):
    """An improvement as the pipeline leaves it: a new name for the front light, tested, and written up."""
    changes = changes or [{"kind": "registry", "entity_id": "light.front", "aliases": [f"lamp {improvement_id}"]}]
    return {"id": improvement_id, "title": f"Idea {improvement_id}", "cause": "Assist didn't know the lamp's name",
            "changes": changes, "provider": {"title": "Kitchen"},
            "setup": {"aliases": {c["entity_id"]: c["aliases"] for c in changes if "aliases" in c}},
            "tested": {"scripts": {}, "exposure": {}}, "user_ids": [ALEX], "conversation": "rec1-recording",
            "round": 1, "test": test or lamp_test(), "fixes": ["rec1-rec:1"], "devices": [], "status": status,
            "created": created or (dt_util.utcnow() - timedelta(days=1)).isoformat(),
            "decisions": [], "application": None, "history": [],
            "copy": {"status": "written", "headline": headline or f"Call the front light lamp {improvement_id}",
                     "benefit": "Say “lamp” and the front light turns on."} if written else None, **extra}


def aliases(home):
    """The front light's names in HA's entity registry (its own name among them)."""
    return list(er.async_get(home.hass).async_get("light.front").aliases)


def conversation(recording_id="rec-one"):
    """A finished conversation with friction (the fixture relay pressed by hand over four turns)."""
    record = make_recording()
    record.update(id=recording_id, status="session_expired")
    return record


def provider(home, *, refuses=None, creates=True):
    """An AI provider (Anthropic) with its own add-AI-Task flow: a name, then the model. It refuses with an error,
    stops (aborts), or creates the AI Task, whose entity then appears."""
    from homeassistant.helpers.selector import SelectSelector, SelectSelectorConfig
    from test_ai_roles import anthropic_entry
    entry = anthropic_entry({"recommended": True})
    home.entries[entry.entry_id] = entry
    models = SelectSelector(SelectSelectorConfig(options=[{"value": "claude-opus-5-5", "label": "Claude Opus 5.5"},
                                                          {"value": "claude-haiku-5-5", "label": "Claude Haiku 5.5"}]))
    flow = {"steps": None, "answers": [], "aborted": []}

    def steps():
        return iter([probatio.Schema({probatio.Required("name"): str, probatio.Required("recommended"): bool}),
                     probatio.Schema({probatio.Optional("chat_model", default="claude-opus-5-5"): models})])

    async def init(handler, context):
        if refuses:
            raise RuntimeError(refuses)
        if not creates:
            return {"type": "abort", "flow_id": "f", "reason": "cannot_connect"}
        flow["steps"] = steps()
        return {"type": "form", "flow_id": "f", "data_schema": next(flow["steps"]), "errors": None}

    async def configure(flow_id, answer):
        flow["answers"].append(answer)
        try:
            return {"type": "form", "flow_id": "f", "data_schema": next(flow["steps"]), "errors": None}
        except StopIteration:
            entry.subentries = {**entry.subentries, "sub-2": SimpleNamespace(
                subentry_id="sub-2", subentry_type="ai_task_data", title=flow["answers"][0].get("name"),
                data={"recommended": False, **answer})}
            er.async_get(home.hass).async_get_or_create("ai_task", "anthropic", "uid-sub-2", config_entry=entry,
                                                        config_subentry_id="sub-2", suggested_object_id="haiku_new")
            home.hass.states.async_set("ai_task.haiku_new", "unknown")
            return {"type": "create_entry"}

    home.hass.config_entries.subentries = SimpleNamespace(async_init=init, async_configure=configure,
                                                          async_abort=flow["aborted"].append)
    return entry, flow


@pytest_asyncio.fixture
async def home(recording_hass, effect_guards, monkeypatch):
    """The improver set up in the offline HA, with two administrators (Alex and Jordan) and an ordinary user."""
    hass = recording_hass
    people = {ALEX: person("Alex", ALEX), JORDAN: person("Jordan", JORDAN), VISITOR: person("Visitor", VISITOR, admin=False)}
    hass.auth = SimpleNamespace(async_get_user=AsyncMock(side_effect=people.get))
    for entity_id in ("ai_task.opus", "ai_task.haiku"):
        hass.states.async_set(entity_id, "unknown")
    assert er.async_get(hass).async_get_or_create("light", "fixture", "front", suggested_object_id="front").entity_id \
        == "light.front"  # Registered first: its aliases are kept there.
    hass.states.async_set("light.front", "off", {"friendly_name": "Front light"})
    model = Model()
    monkeypatch.setattr(ai_task, "async_generate_data", model)
    entry = Entry({CONF_AI_TASK: "ai_task.opus"})
    improver = Home(hass, entry, model, people)

    def update_entry(entry, *, options=None, **_):  # As HA's: listeners hear of a change, scheduled.
        if options is None or dict(options) == dict(entry.options):
            return False
        entry.options = dict(options)
        for listener in list(entry.update_listeners):
            hass.async_create_task(listener(hass, entry))
        return True
    hass.config_entries.async_update_entry = update_entry
    hass.config_entries.async_reload = improver.reload
    improver.entries = {entry.entry_id: entry}  # Config entries HA knows of (an AI provider's, where a test adds one).
    hass.config_entries.async_get_entry = improver.entries.get
    await improver.setup()
    try:
        yield improver
    finally:
        model.release.set()
        if improver.loaded:
            await improver.unload()
        assert effect_guards == []


# Admin only, and input checked first.


@pytest.mark.asyncio
async def test_every_panel_command_is_for_administrators_only(home):
    home.post(improvement("imp-a"))
    visitor = home.client(VISITOR)
    for command, fields in COMMANDS.items():
        assert await visitor.error(command, **fields) == ("unauthorized", "Unauthorized"), command
    # Nothing changed: no run, no decision, the settings as they were.
    assert home.runtime.pipeline.current is None and home.model.calls == []
    assert home.runtime.improvements.get("imp-a")["decisions"] == []
    assert home.entry.options == {CONF_AI_TASK: "ai_task.opus"}


@pytest.mark.asyncio
async def test_the_panel_sits_in_the_sidebar_for_administrators_with_the_count_waiting(home):
    from homeassistant.components.frontend import DATA_PANELS
    hass = home.hass
    await home.unload()
    hass.config.components.add("frontend")  # Where HA's frontend runs (its web server serves the panel's script).
    hass.http = SimpleNamespace(async_register_static_paths=AsyncMock())
    await home.setup()
    shown = hass.data[DATA_PANELS][panel.URL_PATH]
    assert shown.require_admin is True and shown.sidebar_title == "Assist Improver"
    # Versioned, so the browser loads a new version's script.
    assert shown.config["_panel_custom"]["module_url"] == f"/{DOMAIN}_static/panel.js?v={VERSION}"
    assert shown.config["_panel_custom"]["handle_safe_area"] is True  # The panel keeps clear of a phone's insets itself.
    home.post(improvement("imp-a"), improvement("imp-b"))
    assert hass.data[DATA_PANELS][panel.URL_PATH].sidebar_title == "Assist Improver (2)"
    await home.client().result("decide", improvement_id="imp-a", decision="decline")
    assert hass.data[DATA_PANELS][panel.URL_PATH].sidebar_title == "Assist Improver (1)"
    await home.unload()
    assert panel.URL_PATH not in hass.data[DATA_PANELS]  # Unloading takes it out of the sidebar.
    await home.setup()  # Loaded again (a reload): back, without registering its script twice.
    assert panel.URL_PATH in hass.data[DATA_PANELS] and hass.http.async_register_static_paths.await_count == 1


@pytest.mark.asyncio
async def test_commands_refuse_malformed_input_before_acting(home):
    home.post(improvement("imp-a"))
    client = home.client()
    for command, fields in (
            ("improvement", {}), ("improvement", {"improvement_id": 7}),
            ("decide", {"improvement_id": "imp-a"}), ("decide", {"improvement_id": "imp-a", "decision": None}),
            ("decide", {"improvement_id": "imp-a", "decision": "decline", "reason": 7}),
            ("run/start", {}), ("run/start", {"confirmed": False}),
            ("run/start", {"confirmed": True, "only": "review"}),
            ("run/start", {"confirmed": True, "force": True}), ("run/start", {"confirmed": True, "picked": True}),
            ("run/start", {"confirmed": True, "only": "test"}),  # Simulate is Propose's stamp, not a step to run.
            ("settings/set", {"tests_per_goal": "three"}), ("settings/set", {"pause": "yes"}),
            ("settings/set", {"colour": "blue"}), ("settings/models", {}), ("settings/create", {"entry_id": "p"}),
            ("log", {"since": "yesterday"})):
        code, _ = await client.error(command, **fields)
        assert code == "invalid_format", (command, fields)
    assert home.runtime.pipeline.current is None and home.model.calls == []  # A run starts only once confirmed.
    assert home.runtime.improvements.get("imp-a")["decisions"] == []
    assert home.entry.options == {CONF_AI_TASK: "ai_task.opus"}


# Empty states.


@pytest.mark.asyncio
async def test_with_nothing_recorded_every_view_answers_empty(home):
    client = home.client()
    assert await client.result("improvements") == {"improvements": [], "questions": []}
    assert await client.result("log") == {"events": []}
    status = await client.result("run")
    assert status["run"] is None
    preview = status["preview"]
    assert set(preview["stages"].values()) == {0} and preview["held"] == {} and preview["releases"] == {}
    assert preview["admits"] == 0 and preview["which"] == [] and preview["applied"] == 0
    assert preview["running"] is False and preview["paused"] == [] and preview["blocked_models"] == {}
    settings = await client.result("settings")
    assert settings["tests_per_goal"] == 3 and settings["schedule"] == "off" and settings["paused"] == []
    assert settings["roles"]["analyzer"]["entity_id"] == "ai_task.opus"
    assert settings["roles"]["triage"] == {"entity_id": None, "size": "small", "optional": True, "problem": None}


# Improvements: the list, a review, and decisions.


@pytest.mark.asyncio
async def test_the_list_names_each_improvement_by_its_headline_and_what_it_gets_you_waiting_ones_first(home):
    home.post(improvement("imp-old", status="declined", created="2026-01-01T00:00:00+00:00"),
              improvement("imp-new", test=lamp_test(met_before=False)),
              improvement("imp-draft", written=False))  # Not written up yet: not up for review.
    listed = (await home.client().result("improvements"))["improvements"]
    assert [i["id"] for i in listed] == ["imp-new", "imp-old"]
    assert listed[0] == {"id": "imp-new", "title": "Call the front light lamp imp-new", "status": "awaiting_review",
                         "pending": True, "enabled": 1, "removed": 2,  # One goal now met; two turns no longer needed.
                         "spared": 0, "followed": 0, "revision": False}
    assert listed[1]["pending"] is False and listed[1]["status"] == "declined"
    # A revision of an earlier one says so.
    home.post(improvement("imp-rev", revises={"id": "imp-old", "note": "Name it after the room"}))
    listed = (await home.client().result("improvements"))["improvements"]
    assert {i["id"]: i["revision"] for i in listed} == {"imp-rev": True, "imp-new": False, "imp-old": False}
    # Without the editor's headline, the proposer's title.
    home.post({**improvement("imp-new"), "copy": {"status": "failed"}})
    assert (await home.client().result("improvements"))["improvements"][0]["title"] == "Idea imp-new"


@pytest.mark.asyncio
async def test_the_list_shows_decided_improvements_most_recent_first(home):
    home.post(improvement("imp-jan", status="declined", created="2026-01-01T00:00:00+00:00"),
              improvement("imp-mar", status="declined", created="2026-03-01T00:00:00+00:00"),
              improvement("imp-feb", status="revised", created="2026-02-01T00:00:00+00:00"))
    listed = (await home.client().result("improvements"))["improvements"]
    assert [i["id"] for i in listed] == ["imp-mar", "imp-feb", "imp-jan"]


@pytest.mark.asyncio
async def test_an_unknown_improvement_is_not_found_and_cannot_be_decided(home):
    client = home.client()
    assert await client.error("improvement", improvement_id="nope") == ("not_found", "Unknown improvement")
    assert await client.error("decide", improvement_id="nope", decision="accept") == ("decision", "Unknown improvement")


@pytest.mark.asyncio
async def test_a_review_is_checked_against_home_assistant_now_and_offers_accept_first(home):
    home.post(improvement("imp-a"), improvement("imp-gone", changes=[
        {"kind": "registry", "entity_id": "light.gone", "aliases": ["porch"]}]))
    client = home.client()
    review = await client.result("improvement", improvement_id="imp-a")
    assert review["title"] == "Call the front light lamp imp-a" and review["status"] == "awaiting_review"
    assert review["benefit"] == "Say “lamp” and the front light turns on." and review["cause"]
    assert review["choices"][0] == {"value": "accept", "label": "Accept changes"}  # The default.
    assert review["choices"] == [{"value": "accept", "label": "Accept changes"},
                                 {"value": "revise", "label": "Ask for a revision"},
                                 {"value": "decline", "label": "Decline", "reasons": ["not_an_issue"]}]
    [change] = review["changes"]
    assert change["kind"] == "aliases" and change["name"] == "Front light" and change["added"] == 1
    assert {"sign": "+", "old": None, "new": None, "text": "lamp imp-a"} in change["rows"]
    # Each test with its conversation as it went and its replay, to read turn by turn.
    assert review["tests"] == [{"summary": "Turn on the lamp", "improved": True, "result": "3 → 1 turns",
                                "before": [{"you": "Turn on the lamp.", "assist": ""},
                                           {"you": "The front light.", "assist": ""},
                                           {"you": "Yes, that one.", "assist": ""}],
                                "after": [{"you": "Turn on the lamp.", "assist": ""}]}]
    assert review["removed"] == 2  # Counted, as the list has it.
    assert review["conflicts"] == []
    # A device gone since it was tested: the review says so before anything is accepted.
    gone = await client.result("improvement", improvement_id="imp-gone")
    assert [(c["target"], c["why"]) for c in gone["conflicts"]] == [
        ("light.gone", "The device is no longer in Home Assistant.")]


@pytest.mark.asyncio
async def test_accepting_applies_the_change_and_undo_puts_back_what_was_there(home):
    client = home.client()
    home.post(improvement("imp-a"))
    before = aliases(home)
    reply = await client.result("decide", improvement_id="imp-a", decision="accept", note="")
    assert reply == {"result": "Applied. Assist uses it from your next request.",  # A new name: no script to run.
                     "status": "applied", "ok": True}
    assert aliases(home) == [*before, "lamp imp-a"]
    review = await client.result("improvement", improvement_id="imp-a")
    assert review["choices"] == [{"value": "undo", "label": "Undo changes"}]  # One click to undo.
    assert review["recent_outcome"] == reply["result"] and review["recent_outcome_ok"] is True
    reply = await client.result("decide", improvement_id="imp-a", decision="undo")
    assert reply == {"result": "Undone: what was there before is back.", "status": "awaiting_review", "ok": True}
    assert aliases(home) == before
    assert [c["value"] for c in (await client.result("improvement", improvement_id="imp-a"))["choices"]] == [
        "accept", "revise", "decline"]  # Undone: up for review again.


@pytest.mark.asyncio
async def test_a_change_that_no_longer_fits_home_assistant_changes_nothing_and_says_why(home):
    home.post(improvement("imp-gone", changes=[{"kind": "registry", "entity_id": "light.gone", "aliases": ["porch"]}]))
    reply = await home.client().result("decide", improvement_id="imp-gone", decision="accept")
    why = "The device is no longer in Home Assistant."
    assert reply == {"result": f"Nothing was changed: light.gone: {why}", "status": "conflict", "ok": False,
                     "asks": {"about": "conflict", "improvement_id": "imp-gone", "conflicts": [
                         {"kind": "alias", "target": "light.gone", "what": "light.gone", "why": why}]}}


@pytest.mark.asyncio
async def test_a_decision_is_recorded_with_who_made_it_and_shows_in_the_history(home):
    home.post(improvement("imp-a"))
    reply = await home.client(JORDAN).result("decide", improvement_id="imp-a", decision="revise",
                                          note="  Name it after the room  ")
    assert reply == {"result": "Thanks. The improver is revising it now.", "status": "awaiting_review",
                     "ok": True}  # Up for review while it's revised (P0.6).
    events = (await home.client().result("log"))["events"]
    assert events[0] == {"at": events[0]["at"], "kind": "decision", "id": "imp-a",
                         "title": "Call the front light lamp imp-a", "decision": "revise", "reason": None,
                         "outcome": "recorded", "by": "Jordan"}
    assert home.runtime.improvements.feedback()[0]["note"] == "Name it after the room"  # For the next run.


@pytest.mark.asyncio
async def test_a_decision_that_isnt_open_is_refused_and_changes_nothing(home):
    client = home.client()
    home.post(improvement("imp-a"))
    before = aliases(home)
    assert await client.error("decide", improvement_id="imp-a", decision="revise", note="  ") == (
        "decision", "Say what to change in the note")  # A revision says what to change.
    assert await client.error("decide", improvement_id="imp-a", decision="undo") == (
        "decision", "That decision isn't open for this improvement now")  # Nothing applied to undo.
    assert await client.error("decide", improvement_id="imp-a", decision="maybe") == (
        "decision", "That decision isn't open for this improvement now")
    await client.result("decide", improvement_id="imp-a", decision="decline")
    assert await client.error("decide", improvement_id="imp-a", decision="accept") == (
        "decision", "That decision isn't open for this improvement now")  # Declined stays declined.
    item = home.runtime.improvements.get("imp-a")
    assert item["status"] == "declined" and [d["decision"] for d in item["decisions"]] == ["decline"]
    assert aliases(home) == before


@pytest.mark.asyncio
async def test_a_decided_review_offers_the_next_one_waiting_until_all_caught_up(home):
    client = home.client()
    home.post(improvement("imp-a"), improvement("imp-b", headline="Call the porch light lamp"))
    await client.result("decide", improvement_id="imp-a", decision="decline", reason="not_an_issue")
    review = await client.result("improvement", improvement_id="imp-a")
    assert review["next_pending"] == {"id": "imp-b", "title": "Call the porch light lamp"}
    assert review["choices"] == []  # Declined as not an issue: nothing more to decide.
    await client.result("decide", improvement_id="imp-b", decision="decline")
    assert (await client.result("improvement", improvement_id="imp-b"))["next_pending"] is None  # All caught up.


@pytest.mark.asyncio
async def test_a_version_being_revised_says_for_which_note_and_offers_the_next_one_waiting_on_the_household(home,
                                                                                                         monkeypatch):
    """DETAILS P12.8.6: while its revision's round runs, its review says so with the household's note, and the next
    review waiting is one click away, none being revised; the list marks the ones being revised."""
    client = home.client()
    home.post(improvement("imp-a"), improvement("imp-b", headline="Call the porch light lamp"), improvement("imp-c"))
    await client.result("decide", improvement_id="imp-a", decision="revise", note="  Name it after the room  ")
    monkeypatch.setattr(home.runtime.flow, "revising", lambda iid: iid in ("imp-a", "imp-c"))
    review = await client.result("improvement", improvement_id="imp-a")
    assert review["revising"] == {"note": "Name it after the room"}
    assert review["next_pending"] == {"id": "imp-b", "title": "Call the porch light lamp"}
    listed = {i["id"]: i.get("revising", False) for i in (await client.result("improvements"))["improvements"]}
    assert listed == {"imp-a": True, "imp-b": False, "imp-c": True}
    review = await client.result("improvement", improvement_id="imp-b")
    assert review["revising"] is None and review["next_pending"] is None  # The others wait on the improver.


@pytest.mark.asyncio
async def test_the_outcome_of_a_decision_shows_with_the_review_for_a_couple_of_minutes(home, monkeypatch):
    client = home.client()
    home.post(improvement("imp-a"))
    await client.result("decide", improvement_id="imp-a", decision="decline")
    review = await client.result("improvement", improvement_id="imp-a")
    assert review["recent_outcome"] == "Thanks. Declined, and its conversation is archived."
    assert review["recent_outcome_ok"] is True
    later = dt_util.utcnow() + timedelta(minutes=3)
    monkeypatch.setattr(dt_util, "utcnow", lambda: later)
    review = await client.result("improvement", improvement_id="imp-a")
    assert review["recent_outcome"] is None and review["recent_outcome_ok"] is False


@pytest.mark.asyncio
async def test_a_decided_improvement_shows_its_changes_as_proposed(home):
    home.post(improvement("imp-a", status="declined", changes=[
        {"kind": "preference", "for": "this_user", "text": " Alex likes short answers. ", "replaces": ["Be brief."]},
        {"kind": "preference", "for": "everyone", "text": "The garage means both doors."},
        {"kind": "registry", "entity_id": "light.front", "aliases": ["lamp"]},
        {"kind": "registry", "entity_id": "light.porch", "expose": False},
        {"kind": "registry", "entity_id": "cover.garage", "expose": True},
        {"kind": "script", "id": "close_garage", "config": {"alias": "Close garage", "sequence": [
            {"action": "cover.close_cover", "target": {"entity_id": "cover.garage"}}, {"stop": "Closed."}]}},
        {"kind": "automation", "id": "night", "on": False},
        {"kind": "registry", "entity_id": "light.porch", "name": "Porch", "area_id": "garden"},
        {"kind": "assistant", "target": "e1/s1", "assistant": "Kitchen", "llm_hass_api": ["assist"]},
        {"kind": "assistant", "target": "e1/s1", "recommend": {"web_search": True}}]))
    review = await home.client().result("improvement", improvement_id="imp-a")
    assert all(c["rows"] == [] for c in review["changes"])
    # A better setting for the assistant is named, never applied: last, as a recommendation.
    assert [(c.get("proposed"), c.get("recommended")) for c in review["changes"]] == [(True, None)] * 9 + [(None, True)]
    assert [c["name"] for c in review["changes"]] == [
        "Add to Kitchen's instructions (for you): “Alex likes short answers.”, replacing “Be brief.”",
        "Add to Kitchen's instructions (for everyone): “The garage means both doors.”",
        "Give light.front the alias “lamp”",
        "Hide light.porch from Assist",
        "Expose cover.garage to Assist",
        "Script “Close garage”",
        "Turn off automation “night”",
        "Rename light.porch “Porch”; Move light.porch to garden",
        "Kitchen keeps only: assist",
        "For Kitchen, consider: web_search: true"]
    assert [c["kind"] for c in review["changes"]] == ["instructions", "instructions", "aliases", "exposure", "exposure",
                                                      "script", "automation", "device", "assistant", "assistant"]


@pytest.mark.asyncio
async def test_two_tabs_accepting_at_once_apply_the_change_once(home):
    home.post(improvement("imp-a"))
    before = aliases(home)
    first, second = await asyncio.gather(
        home.client(ALEX).send("decide", improvement_id="imp-a", decision="accept"),
        home.client(JORDAN).send("decide", improvement_id="imp-a", decision="accept"))
    # The second waited for the first, then found it applied: nothing applied twice, the undo record intact.
    assert first["success"] and first["result"]["status"] == "applied"
    assert not second["success"] and second["error"] == {
        "code": "decision", "message": "That decision isn't open for this improvement now"}
    assert aliases(home) == [*before, "lamp imp-a"]
    await home.client().result("decide", improvement_id="imp-a", decision="undo")
    assert aliases(home) == before


@pytest.mark.asyncio
async def test_improvements_and_decisions_are_kept_across_a_restart(home):
    home.post(improvement("imp-a"), improvement("imp-b"))
    await home.client().result("decide", improvement_id="imp-a", decision="decline")
    await home.unload()
    await home.setup()
    listed = (await home.client().result("improvements"))["improvements"]
    assert [(i["id"], i["status"]) for i in listed] == [("imp-b", "awaiting_review"), ("imp-a", "declined")]
    assert [e["kind"] for e in (await home.client().result("log"))["events"]][:1] == ["decision"]


# Runs: Run now, its progress, cancel.


@pytest.mark.asyncio
async def test_run_now_starts_one_run_shows_its_progress_and_refuses_a_second(home):
    """Run now moves what a manual full run admits, one run at a time, and shows where it is: the stage at work on the
    conversation it's on, of how many. An analysis the model fails is tried again in the run, then held at Analyze
    with why (P0.8), and the run's result says so; the next manual run releases it."""
    client = home.client()
    await home.arrives(conversation())
    preview = (await client.result("run"))["preview"]
    assert preview["stages"]["Your Assist"] == 1 and preview["admits"] == 1 and preview["releases"] == {}
    assert preview["quick"] == 0 and preview["analyses"] == 1  # No quick-check model: it passes Triage straight on.
    home.model.held = True
    started = (await client.result("run/start", confirmed=True))["run"]
    assert started["status"] == "running" and started["trigger"] == "manual" and started["only"] is None
    assert "force" not in started and "picked" not in started  # A full run.
    await asyncio.wait_for(home.model.entered.wait(), 10)  # Analysing the conversation, with the main model.
    status = await client.result("run")
    run = status["run"]
    assert run["id"] == started["id"] and run["status"] == "running" and status["preview"]["running"] is True
    assert run["admitted"] == run["moved"] == ["rec-one"] and run["released"] == []
    assert run["stage"] == "analyze" and {k: run["progress"][k] for k in ("total", "done", "conversation")} == {
        "total": 1, "done": 0, "conversation": "rec-one"}
    assert await client.error("run/start", confirmed=True) == ("busy", "A run is already in progress")  # One at a time.
    assert await home.client(JORDAN).error("run/start", confirmed=True, only="analyze") == (
        "busy", "A run is already in progress")  # From another tab too.
    home.model.release.set()
    await home.run_finished()
    run = (await client.result("run"))["run"]
    why = "Analyze failed 3 times in a row."
    assert run["status"] == "done" and run["results"] == {
        "rec-one": {"stage": "Analyze", "outcome": None, "held": {"cause": "failures", "why": why}}}
    assert {k: run["progress"][k] for k in ("total", "done", "taken_in", "analyzed", "held", "why")} == {
        "total": 1, "done": 1, "taken_in": 1, "analyzed": 0, "held": 1, "why": why}
    assert home.model.calls == [("journey_analyzer", "ai_task.opus")] * 3  # Tried again in the run, then held.
    [event] = [e for e in (await client.result("log"))["events"] if e["kind"] == "run"]
    assert event["status"] == "done" and event["trigger"] == "manual" and event["error"] is None
    assert event["progress"]["held"] == 1 and "picked" not in event
    preview = (await client.result("run"))["preview"]
    assert preview["stages"]["Analyze"] == 1 and preview["held"] == {"analyze": {"failures": 1}}
    assert preview["admits"] == 0 and preview["releases"] == {"analyze": 1}  # The next manual run releases it.


@pytest.mark.asyncio
async def test_cancel_stops_a_run_in_progress_and_with_none_running_does_nothing(home):
    client = home.client()
    assert await client.result("run/cancel") == {"cancelled": True}
    assert (await client.result("run"))["run"] is None
    await home.arrives(conversation())
    home.model.held = True
    await client.result("run/start", confirmed=True)
    await asyncio.wait_for(home.model.entered.wait(), 10)
    assert await client.result("run/cancel") == {"cancelled": True}
    await home.run_finished()
    status = await client.result("run")
    assert status["run"]["status"] == "cancelled" and status["preview"]["running"] is False
    assert [e["status"] for e in (await client.result("log"))["events"] if e["kind"] == "run"] == ["cancelled"]
    assert status["run"]["results"]["rec-one"]["held"]["cause"] == "cut off"
    assert status["preview"]["releases"] == {"analyze": 1}  # Nothing was lost: held for the next manual run.


@pytest.mark.asyncio
async def test_a_run_cut_off_by_unloading_starts_nothing_after_it(home, monkeypatch):
    """Conversations deleted during the run then go at the next start, never once the stores have closed."""
    after = []

    async def forget_queued():
        after.append("forget_queued")
        return []
    await home.arrives(conversation())
    home.model.held = True
    await home.client().result("run/start", confirmed=True)
    await asyncio.wait_for(home.model.entered.wait(), 10)
    monkeypatch.setattr(home.recorder, "forget_queued", forget_queued)
    await home.unload()
    await home.hass.async_block_till_done()
    assert after == []


@pytest.mark.asyncio
async def test_a_run_that_needs_a_missing_model_is_refused_before_anything_is_spent(home):
    client = home.client()
    await home.arrives(conversation())
    await client.result("settings/set", role="editor", entity_id="ai_task.haiku")
    home.hass.states.async_remove("ai_task.haiku")  # Gone from Home Assistant since it was chosen.
    assert (await client.result("run"))["preview"]["blocked_models"] == {"write": ["Write up"]}
    assert await client.error("run/start", confirmed=True) == (
        "busy", "Write up can't run: its AI model isn't available. Choose another in the settings.")
    assert (await client.result("run"))["run"] is None and home.model.calls == []
    # Run one step: a step that doesn't need it still runs; it releases only what's held at its step and takes in
    # nothing new (P0.7).
    started = (await client.result("run/start", confirmed=True, only="triage"))["run"]
    assert started["only"] == "triage"
    await home.run_finished()
    status = await client.result("run")
    assert status["run"]["status"] == "done" and home.model.calls == []
    assert status["run"]["admitted"] == status["run"]["moved"] == [] and status["run"]["results"] == {}
    assert status["preview"]["stages"]["Your Assist"] == 1 and status["preview"]["admits"] == 1  # Still new.


async def offline_ha(config_dir, people):
    """A fresh offline Home Assistant on a config directory (as after a restart), with the registries loaded."""
    hass = HomeAssistant(str(config_dir))
    await load_registries(hass)
    hass.config_entries = SimpleNamespace(async_loaded_entries=loaded_for(hass), async_entries=lambda domain=None: [],
                                          async_get_entry=lambda entry_id: None)
    hass.auth = SimpleNamespace(async_get_user=AsyncMock(side_effect=people.get))
    hass.states.async_set("ai_task.opus", "unknown")
    return hass


@pytest.mark.asyncio
async def test_a_run_cut_off_by_a_restart_shows_as_interrupted_and_is_never_restarted_on_its_own(home, tmp_path):
    await home.arrives(conversation())
    home.model.held = True
    started = (await home.client().result("run/start", confirmed=True))["run"]
    await asyncio.wait_for(home.model.entered.wait(), 10)
    # Home Assistant stops in the middle of the run: a fresh one starts from what was saved.
    saved = tmp_path / "after-restart"
    await home.hass.async_add_executor_job(shutil.copytree, Path(home.hass.config.config_dir) / ".storage",
                                           saved / ".storage")
    fresh = await offline_ha(saved, home.people)
    entry = Entry({CONF_AI_TASK: "ai_task.opus"})
    await async_setup_entry(fresh, entry)
    try:
        status = await Client(fresh, home.people[ALEX]).result("run")
        assert status["run"]["id"] == started["id"] and status["run"]["status"] == "interrupted"
        assert status["preview"]["running"] is False
        await fresh.async_block_till_done()
        assert home.model.calls == [("journey_analyzer", "ai_task.opus")]  # Nothing ran again.
    finally:
        await async_unload_entry(fresh, entry)
        await fresh.async_stop()


# Settings.


@pytest.mark.asyncio
async def test_settings_change_one_at_a_time_and_apply_right_away(home):
    client = home.client()
    view = await client.result("settings/set", tests_per_goal=5)
    assert view["tests_per_goal"] == 5 and home.entry.options[CONF_TESTS_PER_GOAL] == 5
    assert (await client.result("settings/set", schedule="daily"))["schedule"] == "daily"
    assert (await client.result("settings/set", stage="write", pause=True))["paused"] == ["write"]
    assert (await client.result("run"))["preview"]["paused"] == ["write"]
    assert (await client.result("settings/set", stage="write", pause=False))["paused"] == []
    view = await client.result("settings/set", role="triage", entity_id="ai_task.haiku")
    assert view["roles"]["triage"]["entity_id"] == "ai_task.haiku"
    assert view["roles"]["editor"]["entity_id"] == "ai_task.haiku"  # Write up uses the small model too.
    assert (await client.result("settings/set", role="triage", entity_id=None))["roles"]["triage"]["entity_id"] is None
    assert (await client.result("settings"))["schedule"] == "daily"  # As saved.
    await home.hass.async_block_till_done()
    assert home.reloads == []  # Applied live: nothing reloaded.


@pytest.mark.asyncio
async def test_a_setting_that_doesnt_fit_is_refused_with_why_and_nothing_is_saved(home):
    client = home.client()
    for fields, why in (
            ({"tests_per_goal": 0}, "Tests per conversation, each round, go from 1 to 20."),
            ({"tests_per_goal": 21}, "Tests per conversation, each round, go from 1 to 20."),
            ({"role": "analyzer", "entity_id": None}, "This step needs an AI model."),
            ({"role": "analyzer", "entity_id": "light.front"}, "light.front isn't an AI Task in this Home Assistant."),
            ({"role": "analyzer", "entity_id": "ai_task.gone"},
             "ai_task.gone isn't an AI Task in this Home Assistant."),
            ({"role": "nobody", "entity_id": "ai_task.haiku"}, "Unknown role nobody"),
            ({"schedule": "hourly"}, "Unknown schedule hourly"),
            ({"stage": "review", "pause": True}, "Unknown stage review")):
        assert await client.error("settings/set", **fields) == ("invalid", why), fields
    assert home.entry.options == {CONF_AI_TASK: "ai_task.opus"}


@pytest.mark.asyncio
async def test_a_setting_changed_during_a_run_applies_without_stopping_it(home):
    client = home.client()
    await home.arrives(conversation())
    home.model.held = True
    started = (await client.result("run/start", confirmed=True))["run"]
    await asyncio.wait_for(home.model.entered.wait(), 10)
    await client.result("settings/set", tests_per_goal=7)
    await client.result("settings/set", stage="write", pause=True)
    await client.result("settings/set", role="analyzer", entity_id="ai_task.haiku")  # The main model's step: live too.
    await home.hass.async_block_till_done()
    assert home.reloads == [] and home.runtime.pipeline.running
    assert (await client.result("run"))["run"]["id"] == started["id"]
    home.model.release.set()
    await home.run_finished()
    assert (await client.result("run"))["run"]["status"] == "done"


@pytest.mark.asyncio
async def test_the_developer_services_come_and_go_with_the_hidden_developer_setting(home):
    client = home.client()
    services = lambda: set(home.hass.services.async_services().get(DOMAIN, {}))
    assert services() == set(SERVICES) == {"status", "export_recording"}
    view = await client.result("settings/set", developer_services=True)
    assert "developer_services" not in view  # Hidden: no control in the panel.
    await home.hass.async_block_till_done()
    assert home.reloads == [home.entry.entry_id]  # Which services exist changes: the integration reloads.
    assert services() == set(SERVICES) | set(DEVELOPER_SERVICES)
    await client.result("settings/set", developer_services=False)
    await home.hass.async_block_till_done()
    assert services() == set(SERVICES)


@pytest.mark.asyncio
async def test_the_models_a_provider_offers_come_from_its_own_flow_or_the_panel_says_why_not(home):
    entry, flow = provider(home)
    found = await home.client().result("settings/models", entry_id=entry.entry_id)
    assert found == {"models": [{"value": "claude-opus-5-5", "label": "Claude Opus 5.5"},
                                {"value": "claude-haiku-5-5", "label": "Claude Haiku 5.5"}],
                     "default": "claude-opus-5-5", "named": True,
                     "suggested": {"large": "claude-opus-5-5", "small": "claude-haiku-5-5"}}
    assert flow["aborted"] == ["f"] and "sub-2" not in entry.subentries  # Read, then closed: nothing added.
    refusing, _ = provider(home, refuses="Invalid API key")
    assert await home.client().error("settings/models", entry_id=refusing.entry_id) == (
        "provider", "The provider couldn't list its models (Invalid API key).")


@pytest.mark.asyncio
async def test_a_new_ai_task_is_added_through_the_provider_and_used_for_the_step(home):
    entry, flow = provider(home)
    view = await home.client().result("settings/create", entry_id=entry.entry_id, model="claude-haiku-5-5",
                                      name="Assist Improver (Haiku)", role="triage")
    assert view["roles"]["triage"]["entity_id"] == "ai_task.haiku_new"
    assert home.entry.options["triage_ai_task_entity_id"] == "ai_task.haiku_new"
    assert flow["answers"] == [{"name": "Assist Improver (Haiku)", "recommended": False},
                               {"chat_model": "claude-haiku-5-5"}]  # The chosen model; the rest at its defaults.


@pytest.mark.asyncio
async def test_a_provider_that_stops_adding_an_ai_task_is_named_and_nothing_changes(home):
    entry, _ = provider(home, creates=False)
    assert await home.client().error("settings/create", entry_id=entry.entry_id, model="claude-haiku-5-5",
                                     role="triage") == ("create_failed", "The provider's setup stopped: cannot_connect")
    assert home.entry.options == {CONF_AI_TASK: "ai_task.opus"}


@pytest.mark.asyncio
async def test_an_unknown_step_is_refused_before_the_provider_adds_an_ai_task(home):
    entry, _ = provider(home)
    assert await home.client().error("settings/create", entry_id=entry.entry_id, model="claude-haiku-5-5",
                                     role="nobody") == ("invalid", "Unknown role nobody")
    assert home.entry.options == {CONF_AI_TASK: "ai_task.opus"}
    assert "sub-2" not in entry.subentries


@pytest.mark.asyncio
async def test_the_recommended_setup_comes_back_with_notes_or_says_what_to_add_first(home):
    view = await home.client().result("settings/recommended")
    assert view["notes"] == [] and view["roles"]["analyzer"]["entity_id"] == "ai_task.opus"
    assert home.entry.options[CONF_AI_TASK] == "ai_task.opus"
    home.entry.options = {}  # No model chosen, and no AI provider to add one.
    assert await home.client().error("settings/recommended") == (
        "invalid", "Add an AI provider, such as Anthropic, in Settings → Devices & services first.")
    assert home.entry.options == {}


# The preferred assistant.


@pytest.mark.asyncio
async def test_the_panel_lists_home_assistants_assistants_and_chooses_one_with_home_assistants_own_command(home):
    """Your Assist: Home Assistant's assistants, its preferred one first. Choosing one is Home Assistant's own command,
    as Settings > Voice assistants sends it; the improver has none of its own."""
    from homeassistant.components.assist_pipeline import pipeline as assist

    def saved(pipeline_id, name, engine):
        return assist.Pipeline(id=pipeline_id, name=name, conversation_engine=engine, conversation_language="en",
                               language="en", stt_engine=None, stt_language=None, tts_engine=None, tts_language=None,
                               tts_voice=None, wake_word_entity=None, wake_word_id=None).to_json()
    # HA's own assistants (Settings, Voice assistants), as saved: Home Assistant's preferred, and Claude.
    store = assist.PipelineStorageCollection(assist.PipelineStore(
        home.hass, assist.STORAGE_VERSION, assist.STORAGE_KEY, minor_version=assist.STORAGE_VERSION_MINOR))
    await store.store.async_save({"items": [saved("p-ha", "Home Assistant", "conversation.home_assistant"),
                                            saved("p-claude", "Claude", "conversation.claude")],
                                  "preferred_item": "p-ha"})
    await store.async_load()
    home.hass.data[assist.KEY_ASSIST_PIPELINE] = assist.PipelineData(store)
    assist.PipelineStorageCollectionWebsocket(store, "assist_pipeline/pipeline", "pipeline", assist.PIPELINE_FIELDS,
                                              assist.PIPELINE_FIELDS).async_setup(home.hass)  # As HA sets it up.
    client = home.client()

    async def listed():
        return [(p["name"], p["preferred"]) for p in (await client.result("household"))["pipelines"]]
    assert await listed() == [("Home Assistant", True), ("Claude", False)]
    assert await client.result("pipeline/set_preferred", domain="assist_pipeline", pipeline_id="p-claude") is None
    assert store.async_get_preferred_item() == "p-claude" and await listed() == [("Claude", True),
                                                                                 ("Home Assistant", False)]
    code, _ = await client.error("pipeline/set_preferred", domain="assist_pipeline", pipeline_id="gone")
    assert code == "not_found" and await listed() == [("Claude", True), ("Home Assistant", False)]
    assert await client.error("assistant/prefer", pipeline_id="p-ha") == ("unknown_command", "Unknown command.")


# The history.


@pytest.mark.asyncio
async def test_the_history_lists_finished_runs_reviews_and_decisions_newest_first(home):
    client = home.client()
    home.post(improvement("imp-a"))
    await client.result("run/start", confirmed=True)  # Nothing waiting: done straight away.
    await home.run_finished()
    home.post(improvement("imp-draft", written=False))  # Waiting for its write-up, not up for review: not in it.
    await client.result("decide", improvement_id="imp-a", decision="decline", note="Not needed")
    await home.arrives(conversation())
    home.model.held = True
    await client.result("run/start", confirmed=True)
    await asyncio.wait_for(home.model.entered.wait(), 10)
    events = (await client.result("log"))["events"]
    assert [e["kind"] for e in events] == ["decision", "run", "posted"]  # The run in progress isn't history yet.
    assert [e["at"] for e in events] == sorted((e["at"] for e in events), reverse=True)
    run = events[1]
    assert run == {"at": run["at"], "kind": "run", "status": "done", "trigger": "manual", "only": None,
                   "progress": run["progress"], "error": None}
    assert events[2] == {"at": events[2]["at"], "kind": "posted", "id": "imp-a",
                         "title": "Call the front light lamp imp-a"}
    assert events[0]["by"] == "Alex" and events[0]["decision"] == "decline"
    await client.result("run/cancel")
    await home.run_finished()
    events = (await client.result("log"))["events"]
    assert [(e["kind"], e.get("status")) for e in events] == [("run", "cancelled"), ("decision", None),
                                                               ("run", "done"), ("posted", None)]


@pytest.mark.asyncio
async def test_the_history_keeps_the_newest_hundred_events(home):
    home.post(*[improvement(f"imp-{n:03}", status="declined", created=f"2026-01-01T00:{n // 60:02}:{n % 60:02}+00:00")
                for n in range(120)])
    events = (await home.client().result("log"))["events"]
    assert len(events) == 100 and events[0]["id"] == "imp-119" and events[-1]["id"] == "imp-020"


# Services: what households get, and what only developer mode adds.


@pytest.fixture
def service_calls(effect_guards, monkeypatch):
    """HA's own service calls (schema, admin check, responses) for the improver's services; every other service stays
    blocked by the effect guards."""
    guarded, original = ServiceRegistry.async_call, _original_async_call()

    async def call(registry, domain, service, *args, **kwargs):
        if domain == DOMAIN:
            return await original(registry, domain, service, *args, **kwargs)
        return await guarded(registry, domain, service, *args, **kwargs)
    monkeypatch.setattr(ServiceRegistry, "async_call", call)


async def call(hass, service, data=None, *, user=ALEX, response=True):
    return await hass.services.async_call(DOMAIN, service, data or {}, blocking=True, return_response=response,
                                          context=Context(user_id=user))


@pytest.mark.asyncio
async def test_status_and_export_answer_administrators_with_what_the_improver_holds(home, service_calls):
    hass = home.hass
    await translated(hass)
    await home.arrives(conversation())
    assert await call(hass, "status") == {"recording": True, "sessions": 1, "pending_writes": 0, "last_error": None}
    exported = await call(hass, "export_recording", {"recording_id": "rec-one"})
    assert exported["id"] == "rec-one" and len(exported["turns"]) == 4
    with pytest.raises(ServiceValidationError, match="^Unknown recording ID$"):  # The caller's mistake, translated.
        await call(hass, "export_recording", {"recording_id": "nope"})
    with pytest.raises(probatio.Invalid):
        await call(hass, "export_recording", {})
    for service, data in (("status", {}), ("export_recording", {"recording_id": "rec-one"})):
        with pytest.raises(Unauthorized):  # Household conversations: administrators only.
            await call(hass, service, data, user=VISITOR)
        with pytest.raises(ServiceValidationError):  # They return data; there's nothing else to do.
            await call(hass, service, data, response=False)


@pytest.mark.asyncio
async def test_developer_services_never_call_a_paid_model_without_permission(home, service_calls):
    await home.client().result("settings/set", developer_services=True)
    await home.hass.async_block_till_done()
    await home.arrives(conversation())
    hass, data = home.hass, {"recording_id": "rec-one"}
    await translated(hass)
    with pytest.raises(ServiceValidationError, match="^Paid inference has not been authorized$"):
        await call(hass, "replay_recording", data)
    with pytest.raises(Unauthorized):
        await call(hass, "replay_recording", {**data, "allow_paid_inference": True}, user=VISITOR)
    assert home.model.calls == []


@pytest.mark.asyncio
async def test_developer_services_read_what_the_improver_holds(home, service_calls):
    await home.client().result("settings/set", developer_services=True)
    await home.hass.async_block_till_done()
    await home.arrives(conversation())
    home.post(improvement("imp-a"))
    hass = home.hass
    await translated(hass)
    assert await call(hass, "list_recordings") == {"recordings": [{"status": "session_expired", "user_turns": 4}]}
    assert [i["id"] for i in (await call(hass, "list_improvements"))["improvements"]] == ["imp-a"]
    with pytest.raises(ServiceValidationError, match="^Unknown recording ID$"):
        await call(hass, "replay_recording", {"recording_id": "nope", "allow_paid_inference": True})
    home.recorder.index["lost"] = {"status": "session_expired", "user_turns": 1}  # Listed, but not stored.
    with pytest.raises(HomeAssistantError, match="^Can't replay: Recording is unavailable$") as refused:
        await call(hass, "replay_recording", {"recording_id": "lost", "allow_paid_inference": True})
    assert refused.value.translation_key == "cannot_replay"
    assert home.model.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("fails", ["replays", "improvements"])
async def test_a_setup_that_fails_leaves_nothing_listening_or_registered(recording_hass, effect_guards, fails):
    from homeassistant.const import EVENT_HOMEASSISTANT_STOP, EVENT_STATE_CHANGED
    from homeassistant.exceptions import UnsupportedStorageVersionError
    from homeassistant.helpers.storage import Store
    hass = recording_hass
    await Store(hass, 2, f"{DOMAIN}.{fails}").async_save({})  # Saved by a newer version: this one can't read it.
    package = logging.getLogger("custom_components.assist_improver")
    counted = lambda: ({e: hass.bus.async_listeners().get(e, 0) for e in (EVENT_STATE_CHANGED, EVENT_HOMEASSISTANT_STOP)},
                       len(package.handlers))
    before = counted()
    entry = Entry({CONF_AI_TASK: "ai_task.opus"})
    with pytest.raises(UnsupportedStorageVersionError):
        await async_setup_entry(hass, entry)
    await entry.unloaded()  # As Home Assistant ends a setup that failed.
    assert counted() == before  # The recorder and the trace's log handler stopped again: nothing records half set up.
    assert DOMAIN not in hass.data and not hass.services.async_services().get(DOMAIN)
    assert effect_guards == []


@pytest.mark.asyncio
async def test_each_service_field_is_the_one_its_schema_takes(home):
    import yaml
    await home.client().result("settings/set", developer_services=True)  # Every service, the developer ones too.
    await home.hass.async_block_till_done()
    registered = home.hass.services.async_services()[DOMAIN]
    described = yaml.safe_load((Path(integration.__file__).parent / "services.yaml").read_text())
    assert set(described) == set(registered) == set(SERVICES) | set(DEVELOPER_SERVICES)
    # Each field by name, and whether it must be given, as the registered service's schema takes it.
    taken = {name: {str(key): isinstance(key, probatio.Required) for key in service.schema.schema}
             for name, service in registered.items()}
    assert {name: {field: bool((about or {}).get("required"))
                   for field, about in ((spec or {}).get("fields") or {}).items()}
            for name, spec in described.items()} == taken


def test_each_service_field_has_its_name_and_description():
    import yaml
    folder = Path(integration.__file__).parent
    described = yaml.safe_load((folder / "services.yaml").read_text())
    fields = {name: set((spec or {}).get("fields") or {}) for name, spec in described.items()}
    # strings.json is the source; translations/en.json is what Home Assistant reads for a custom integration.
    for source in ("strings.json", "translations/en.json"):
        strings = json.loads((folder / source).read_text())["services"]
        assert {name: set(spec.get("fields") or {}) for name, spec in strings.items()} == fields, source
        for name, spec in strings.items():
            assert spec.get("name") and spec.get("description"), (source, name)
            for field, text in (spec.get("fields") or {}).items():
                assert text.get("name") and text.get("description"), (source, name, field)


# Not loaded, and unloaded.


@pytest.mark.asyncio
async def test_before_the_improver_is_set_up_its_commands_answer_that_it_isnt_loaded(hass):
    from homeassistant import config_entries, loader
    loader.async_setup(hass)
    hass.config_entries = config_entries.ConfigEntries(hass, {})
    await hass.config_entries.async_initialize()
    assert await integration.async_setup(hass, {})  # What Home Assistant sets up first, before any improver entry:
    client = Client(hass, person("Alex", ALEX))       # the commands, registered once per run.
    for command, fields in COMMANDS.items():  # A provider's models need no improver: there's no such provider here.
        code, _ = await client.error(command, **fields)
        assert code == ("provider" if command == "settings/models" else "not_loaded"), command


@pytest.mark.asyncio
async def test_after_unloading_every_command_answers_with_an_error_and_the_services_are_gone(home):
    home.post(improvement("imp-a"))
    client = home.client()
    await client.result("decide", improvement_id="imp-a", decision="decline", reason="not_an_issue")
    await home.unload()
    for command, fields in COMMANDS.items():
        reply = await client.send(command, **fields)  # An answer, never stale data or a page left waiting.
        assert reply["success"] is False, (command, reply)
    assert not home.hass.services.async_services().get(DOMAIN)
    assert DOMAIN not in home.hass.data
    await home.setup()  # Loaded again: the panel works again, with what was kept.
    assert [(i["id"], i["status"]) for i in (await client.result("improvements"))["improvements"]] == [
        ("imp-a", "declined")]


@pytest.mark.asyncio
async def test_unloading_saves_the_conversations_words_the_list_was_about_to_save(home):
    from homeassistant.helpers.storage import Store
    from custom_components.assist_improver.const import STORAGE_VERSION
    await home.arrives(conversation())
    await home.client().result("conversations")  # Reads its words, saved a moment later.
    await home.unload()  # No save is left pending after the entry.
    saved = await Store(home.hass, STORAGE_VERSION, f"{DOMAIN}.transcripts").async_load()
    assert list(saved["transcripts"]) == ["rec-one"]


# What a review says, built by code.


def test_why_its_safe_is_stated_by_code_from_the_tests_and_the_changeset():
    from custom_components.assist_improver.household import API_ID
    steps = [
        {"kind": "script", "target": "close_garage", "before": None, "what": "Close garage",
         "after": {"alias": "Close garage", "sequence": [{"action": "cover.close_cover"}]}},
        {"kind": "exposure", "target": "script.close_garage", "before": False, "after": True, "what": "Close garage"},
        {"kind": "exposure", "target": "light.porch", "before": True, "after": False, "what": "Porch light"},
        {"kind": "exposure", "target": "light.hall", "before": True, "after": False, "what": "Hall light"},
        {"kind": "prompt", "target": "e1/s1", "before": "Be brief.", "after": "Be brief.\nSay the room.",
         "what": "Kitchen"},
        {"kind": "assistant_api", "target": "e1/s1", "before": ["assist", API_ID], "after": [API_ID],
         "what": "Kitchen"}]
    shown = panel.review(improvement("imp-a"), (steps, [], []))
    assert shown["safety"] == [
        "Tested on your own conversations, replayed in a sandboxed copy of your home: nothing in your home moved.",
        "Only settings change: 1 new script, 1 script made visible to Assist, 2 devices hidden from Assist, "
        "1 instruction change, 1 assistant's LLM APIs narrowed. Nothing runs when you accept.",
        "Checked against your Home Assistant first, applied all or nothing, and one click to undo."]
    rows = [[(r["sign"], r["text"]) for r in c["rows"]] for c in shown["changes"]]
    assert rows[1] == [("+", "exposed to Assist")] and rows[2] == [("-", "exposed to Assist")]
    assert ("+", "Say the room.") in rows[4] and rows[5] == [("-", "Assist")]
    assert [c["kind"] for c in shown["changes"]] == ["script", "exposure", "exposure", "exposure", "instructions",
                                                     "assistant"]
    # Only a name: nothing Assist gains.
    alias = [{"kind": "alias", "target": "light.front", "before": [], "after": [None, "lamp"], "what": "Front light"}]
    shown = panel.review(improvement("imp-b"), (alias, [], []))
    assert shown["safety"] == [
        "Tested on your own conversations, replayed in a sandboxed copy of your home: nothing in your home moved.",
        "Only settings change: 1 name added. Nothing runs when you accept.",
        "Checked against your Home Assistant first, applied all or nothing, and one click to undo."]
    assert [(r["sign"], r["text"]) for r in shown["changes"][0]["rows"]] == [("+", "(its own name)"), ("+", "lamp")]


def test_a_scripts_diff_keeps_each_value_on_one_line_as_long_as_it_is():
    said = "The garage door is closing now; it takes about twenty seconds, so keep clear of the door until it stops."
    step = {"kind": "script", "target": "close_garage", "before": None, "what": "Close garage", "after": {
        "alias": "Close garage", "sequence": [{"action": "cover.close_cover"}, {"action": "notify.notify",
                                                                                "data": {"message": said}}]}}
    rows = [r["text"] for r in panel.change_file(step)["rows"]]
    assert rows[-1] == f"    message: {said}" and len(rows) == 6  # Not folded at 80 characters.


def test_each_test_result_reads_in_a_few_words():
    shown = [panel.review(improvement("imp-a", test=test), ([], [], []))["tests"][0]
             for test in (lamp_test(after=("", "", "")), lamp_test(met_before=False), lamp_test())]
    assert [t["result"] for t in shown] == ["not improved", "goal now met", "3 → 1 turns"]
    assert [t["improved"] for t in shown] == [False, True, True]


def test_each_test_reads_turn_by_turn_as_it_went_and_with_the_change():
    from custom_components.assist_improver import records
    from test_records import problems
    lamp = {"step": "Turn on · lamp", "error": "No entity named lamp"}
    test = tested("Turn on the lamp", said=("Turn on the lamp.", "The front one."), replies=("Which lamp?", "Done."),
                  after=("Done: the front light is on.",),
                  recorded=[{"you": "Turn on the lamp.", "assist": "Which lamp?", "failed": [lamp]},
                            {"you": "The front one.", "assist": "Done."}])
    [shown] = panel.review(improvement("imp-a", test=test), ([], [], []))["tests"]
    # As the home page reads a conversation, each failed step in the turn it failed in.
    assert shown["before"] == [{"you": "Turn on the lamp.", "assist": "Which lamp?", "failed": [lamp]},
                               {"you": "The front one.", "assist": "Done."}]
    assert shown["after"] == [{"you": "Turn on the lamp.", "assist": "Done: the front light is on."}]
    assert all(problems(t, records.Said) == [] for t in shown["before"] + shown["after"])


def test_what_a_decision_did_is_said_in_a_sentence_or_two():
    said = lambda outcome, status="awaiting_review": panel.outcome_text({"status": status, "outcome": outcome})
    assert said({"status": "recorded"}, "declined") == "Thanks, your decision is recorded."
    assert said({"status": "done", "left": [{"what": "Porch light"}, {"what": "Close garage"}]}, "undone") == (
        "Undone: what was there before is back. Left as you changed it: Porch light; Close garage")
    assert said({"status": "conflict", "conflicts": [{"what": "Close garage", "why": "It was changed."}]}, "conflict") \
        == "Nothing was changed: Close garage: It was changed."
    assert said({"status": "rolled_back", "failure": {"what": "Close garage", "error": "disk full"}}) == (
        "Nothing was changed. Close garage failed: disk full. The steps already done were undone, so you can try "
        "again.")
    assert said({"status": "needs_attention", "stuck": [{"what": "Porch light", "why": "changed meanwhile"}]},
                "needs_attention") == "Some changes couldn't be undone after a failure. Porch light: changed meanwhile."
    assert said({"status": "busy", "error": "Another change is being applied."}) == "Another change is being applied."
