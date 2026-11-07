"""Testable specs: household knowledge.

Household knowledge is the instruction lines improvements add: lines for everyone, and lines for one person that only
their requests see. Every assistant that controls Home Assistant through Assist reads them: Home Assistant's own Assist
gathers them from the improver's llm platform, with no second API to choose (or to tick beside Assist and read every
tool twice). Uninstalling writes nothing into any assistant's instructions: the settings' Uninstall shows the lines to
save, and removing the improver from Home Assistant's own page leaves them in a notification.

Each promise is checked the way the household meets it: the panel's websocket commands go through Home Assistant's
own websocket handling (schema, admin check, then the command), and what an assistant reads is the system prompt Home
Assistant's own chat log builds for it. Synthetic people and lines only.
"""

import json
import logging
from types import MappingProxyType, SimpleNamespace

import pytest
import pytest_asyncio
from homeassistant.components import conversation
from homeassistant.components.websocket_api.connection import ActiveConnection
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import Context
from homeassistant.helpers import issue_registry as ir, llm

import custom_components.assist_improver as integration
from conftest import assist_home, improver_entry, serve
from kit import runtime_with_flow
from test_changeset import tested
from test_milestone1 import fake_entry
from custom_components.assist_improver import household, panel
from custom_components.assist_improver.const import DOMAIN

ALEX = "0123456789abcdef0123456789abcdef"
JORDAN = "fedcba9876543210fedcba9876543210"
GONE = "00000000000000000000000000000000"  # Someone whose Home Assistant user was removed.
KNOWLEDGE = {"everyone": ["Doors close at 10.", "Shoes off inside."], "people": {ALEX: ["Alex likes short answers."]}}
LINES = ["Doors close at 10.", "Shoes off inside.", "Alex likes short answers."]


@pytest_asyncio.fixture
async def home(hass):
    """The offline HA with Assist's real tools, two people (Alex and Jordan), and the panel's websocket commands."""
    await assist_home(hass, {"light.porch": ("off", "Porch light")})
    people = {ALEX: "Alex", JORDAN: "Jordan"}

    async def get_user(user_id):
        return SimpleNamespace(id=user_id, name=people[user_id]) if user_id in people else None
    hass.auth = SimpleNamespace(async_get_user=get_user)
    improver_entry(hass)  # Installed, with its settings as a new install has them.
    panel.register_commands(hass)
    return hass


class Assistant:
    """An AI provider's assistant: a conversation subentry, kept in HA's config entries as the provider keeps it."""

    def __init__(self, hass, title, apis, prompt="Be brief.", kind="conversation"):
        data = {"prompt": prompt, "chat_model": "fixture"}
        if apis is not None:
            data["llm_hass_api"] = apis
        self.entry = ConfigEntry(domain="fixture_provider", title=title, data={}, source="user", version=1,
                                 minor_version=1, options={}, unique_id=None, discovery_keys=MappingProxyType({}),
                                 state=ConfigEntryState.NOT_LOADED, subentries_data=[{
                                     "data": data, "subentry_type": kind, "title": title, "unique_id": None,
                                     "subentry_id": "sub"}])
        hass.config_entries._entries[self.entry.entry_id] = self.entry
        self.target = f"{self.entry.entry_id}/sub"

    @property
    def data(self):
        return dict(self.entry.subentries["sub"].data)

    @property
    def apis(self):
        return self.data.get("llm_hass_api")

    @property
    def prompt(self):
        return self.data.get("prompt")


class Panel:
    """The panel signed in as one person: every message goes through HA's websocket handling."""

    def __init__(self, hass, user_id=ALEX, name="Alex", admin=True):
        self.hass, self.sent, self.last = hass, [], 0
        user = SimpleNamespace(id=user_id, name=name, is_admin=admin)
        self.connection = ActiveConnection(logging.getLogger(__name__), hass, self.sent.append, user, None, None)

    def send(self, command, **fields):
        self.last += 1
        self.connection.async_handle({"id": self.last, "type": f"{DOMAIN}/{command}", **fields})
        return self.last

    async def reply(self, msg_id):
        """The command's result, or ("refused", error code)."""
        await self.hass.async_block_till_done(wait_background_tasks=True)
        for raw in self.sent:
            message = raw if isinstance(raw, dict) else json.loads(raw)
            if message["id"] == msg_id:
                return message["result"] if message["success"] else ("refused", message["error"]["code"])
        raise AssertionError(f"No reply to message {msg_id}")

    async def __call__(self, command, **fields):
        return await self.reply(self.send(command, **fields))


async def what_it_reads(hass, assistant, user_id=None):
    """The system prompt HA's chat log builds for one request to this assistant: its own instructions (rendered as a
    template), then its LLM API's prompt. user_id None: a request nobody is signed in to (a voice satellite)."""
    log = conversation.ChatLog(hass, f"spec-{user_id}")
    await log.async_provide_llm_data(request(user_id), assistant.apis, assistant.prompt)
    return log.content[0].content


def request(user_id=None):
    """One request's LLM context, as a conversation agent passes it."""
    return llm.LLMContext(platform="fixture_provider", context=Context(user_id=user_id), language="en",
                          assistant="conversation", device_id=None)


def disabled(hass, off):
    """The improver disabled (or enabled again) on Home Assistant's own page."""
    object.__setattr__(hass.config_entries.async_entries(DOMAIN)[0], "disabled_by", "user" if off else None)


async def stored(hass):
    """What the household store holds, as the next start of Home Assistant loads it."""
    fresh = household.Household(hass)
    await fresh.load()
    return fresh.knowledge


# Assistants reading the household knowledge.

@pytest.mark.asyncio
async def test_assist_is_the_only_api_the_improver_leaves_so_no_tool_is_offered_twice(home, monkeypatch):
    own = [api.id for api in llm.async_get_apis(home)]  # Home Assistant's own.
    await integration.async_setup(home, {})
    entry = fake_entry()
    await integration.async_setup_entry(home, entry)
    try:
        await (await household.async_setup(home)).save(KNOWLEDGE)
        assert llm.LLM_API_ASSIST in own and [api.id for api in llm.async_get_apis(home)] == own
        assist = await llm.async_get_api(home, llm.LLM_API_ASSIST, request(ALEX))
        names = [t.name for t in assist.tools]
        assert len(names) == len(set(names))
        assert all(assist.api_prompt.count(line) == 1 for line in LINES)
    finally:
        await integration.async_unload_entry(home, entry)


@pytest.mark.asyncio
async def test_with_nothing_learned_or_the_improver_disabled_assist_is_exactly_home_assistants_own(home):
    assert household.prompt(home, request(ALEX)) is None  # Before the knowledge is loaded.
    store = await household.async_setup(home)
    plain = await llm.async_get_api(home, llm.LLM_API_ASSIST, request(ALEX))
    assert household.HEADING not in plain.api_prompt and household.prompt(home, request(ALEX)) is None
    await store.save(KNOWLEDGE)
    assert "Doors close at 10." in (await llm.async_get_api(home, llm.LLM_API_ASSIST, request(ALEX))).api_prompt
    disabled(home, True)
    for user_id in (ALEX, None):
        off = await llm.async_get_api(home, llm.LLM_API_ASSIST, request(user_id))
        assert off.api_prompt == plain.api_prompt and [t.name for t in off.tools] == [t.name for t in plain.tools]
    disabled(home, False)
    assert "Doors close at 10." in (await llm.async_get_api(home, llm.LLM_API_ASSIST, request(ALEX))).api_prompt


@pytest.mark.asyncio
async def test_every_saved_line_reaches_every_assistant_on_assist_at_its_next_request(home):
    await household.async_setup(home)
    claude, single = Assistant(home, "Claude", ["assist"]), Assistant(home, "Single", "assist")  # One name, or a list.
    chat = Assistant(home, "Chat only", None)  # Doesn't control Home Assistant: nothing of Assist's reaches it.
    assert "Shoes off inside." not in await what_it_reads(home, claude, ALEX)
    assert await Panel(home)("household/edit", scope="everyone", action="add", text="Shoes off inside.") == {"lines": 1}
    assert "Shoes off inside." in await what_it_reads(home, claude, ALEX)
    assert "Shoes off inside." in await what_it_reads(home, single, JORDAN)
    assert "Shoes off inside." not in await what_it_reads(home, chat, ALEX)


@pytest.mark.asyncio
async def test_everyone_reads_the_shared_lines_and_only_the_person_speaking_reads_their_own(home):
    await (await household.async_setup(home)).save(KNOWLEDGE)
    claude = Assistant(home, "Claude", ["assist"])
    alex = await what_it_reads(home, claude, ALEX)
    assert alex.startswith("Be brief.")  # Its own instructions first, then Assist's, with the household's lines.
    assert alex.index("Doors close at 10.") < alex.index("Shoes off inside.") < alex.index("Alex likes short answers.")
    for someone_else in (JORDAN, None):  # Jordan, and a voice satellite nobody is signed in to.
        reads = await what_it_reads(home, claude, someone_else)
        assert "Doors close at 10." in reads and "Shoes off inside." in reads
        assert "Alex likes short answers." not in reads


@pytest.mark.asyncio
async def test_household_lines_reach_the_assistant_word_for_word_never_run_as_template_code(home):
    lines = ["Say {{ 6 * 7 }} as written.", "Keep {% if %} as written.", "Keep {# this #} too."]
    store = await household.async_setup(home)
    await store.save({"everyone": lines[:2], "people": {ALEX: lines[2:]}})
    reads = await what_it_reads(home, Assistant(home, "Claude", ["assist"]), ALEX)
    assert all(line in reads for line in lines) and "42" not in reads


@pytest.mark.asyncio
async def test_the_household_knowledge_is_loaded_once_per_run_and_read_while_the_integration_reloads(home,
                                                                                                   monkeypatch):
    assert await integration.async_setup(home, {})
    first = home.data[household.KEY]
    assert await household.async_setup(home) is first  # Once per run: the same knowledge.
    entry = fake_entry()
    await integration.async_setup_entry(home, entry)
    await Panel(home)("household/edit", scope="everyone", action="add", text="Doors close at 10.")
    await integration.async_unload_entry(home, entry)
    # While the integration reloads, every assistant on Assist still gets its lines.
    claude = Assistant(home, "Claude", ["assist"])
    assert "Doors close at 10." in await what_it_reads(home, claude, ALEX)
    again = fake_entry()
    await integration.async_setup_entry(home, again)
    try:
        assert (await what_it_reads(home, claude, ALEX)).count("Doors close at 10.") == 1
    finally:
        await integration.async_unload_entry(home, again)


@pytest.mark.asyncio
async def test_it_is_on_while_the_improver_is_set_up_and_off_while_it_is_disabled_or_gone(home):
    store = await household.async_setup(home)
    await store.save(KNOWLEDGE)
    entry = home.config_entries.async_entries(DOMAIN)[0]
    assert store.on
    disabled(home, True)
    assert not store.on and household.prompt(home, request(ALEX)) is None
    disabled(home, False)
    del home.config_entries._entries[entry.entry_id]  # Removed: gone until Home Assistant restarts.
    assert not store.on and household.prompt(home, request(ALEX)) is None


@pytest.mark.asyncio
async def test_each_turn_records_the_household_knowledge_and_whether_assist_read_it(home):
    from custom_components.assist_improver.snapshot import snapshot
    await (await household.async_setup(home)).save(KNOWLEDGE)
    taken = snapshot(home)
    assert (taken["household"], taken["household_on"]) == (KNOWLEDGE, True)  # A replay rebuilds the same prompt.
    disabled(home, True)
    assert snapshot(home)["household_on"] is False
    disabled(home, False)


@pytest.mark.asyncio
async def test_an_assistant_not_reading_the_knowledge_is_never_raised_in_repairs(home):
    ir.async_create_issue(home, "other", "household_sub1", is_fixable=False, severity=ir.IssueSeverity.WARNING,
                          translation_key="household_assistant")
    Assistant(home, "Claude", ["assist"])
    await integration.async_setup(home, {})
    entry = fake_entry()
    await integration.async_setup_entry(home, entry)
    try:
        issues = set(ir.async_get(home).issues)
        assert not {i for d, i in issues if d == DOMAIN and i.startswith("household_")}
        assert ("other", "household_sub1") in issues  # Another integration's items are its own.
    finally:
        await integration.async_unload_entry(home, entry)


@pytest.mark.asyncio
async def test_the_household_knowledge_is_kept_across_restarts(home):
    await household.async_setup(home)
    ui = Panel(home)
    await ui("household/edit", scope="everyone", action="add", text="Doors close at 10.")
    await ui("household/edit", scope=ALEX, action="add", text="Alex likes short answers.")
    assert await stored(home) == {"everyone": ["Doors close at 10."], "people": {ALEX: ["Alex likes short answers."]}}


# The panel's view of the household knowledge.

@pytest.mark.asyncio
async def test_before_anything_is_learned_the_household_view_is_empty(home):
    await household.async_setup(home)
    Assistant(home, "Claude", ["assist"])
    view = await Panel(home)("household")
    assert view == {"lines": 0, "everyone": [], "people": [], "text": "", "me": {"user_id": ALEX, "name": "Alex"},
                    "pipelines": []}


@pytest.mark.asyncio
async def test_the_household_view_names_each_person_and_someone_ha_no_longer_has(home):
    await (await household.async_setup(home)).save(
        {"everyone": ["Doors close at 10."], "people": {JORDAN: ["Jordan likes jazz."], ALEX: ["Alex likes short answers."],
                                                        GONE: ["Old line."]}})
    view = await Panel(home, ALEX, name=None)("household")
    assert view["lines"] == 4 and view["everyone"] == ["Doors close at 10."]
    assert view["people"] == [{"user_id": GONE, "name": "Someone", "lines": ["Old line."]},
                              {"user_id": ALEX, "name": "Alex", "lines": ["Alex likes short answers."]},
                              {"user_id": JORDAN, "name": "Jordan", "lines": ["Jordan likes jazz."]}]
    assert view["me"] == {"user_id": ALEX, "name": "You"}  # A signed-in user without a name.


@pytest.mark.asyncio
async def test_before_the_knowledge_is_loaded_the_view_is_empty_and_edits_are_refused(home):
    ui = Panel(home)
    view = await ui("household")
    assert view["lines"] == 0 and view["everyone"] == [] and view["people"] == []
    assert await ui("household/edit", scope="everyone", action="add", text="Doors close at 10.") == (
        "refused", "not_ready")


# Editing lines from the panel.

@pytest.mark.asyncio
async def test_a_line_is_added_for_everyone_or_for_one_person_as_written_without_surrounding_spaces(home):
    await household.async_setup(home)
    ui = Panel(home)
    assert await ui("household/edit", scope="everyone", action="add", text="  Doors close at 10.\n") == {"lines": 1}
    assert await ui("household/edit", scope=JORDAN, action="add", text="Jordan likes jazz.") == {"lines": 2}
    view = await ui("household")
    assert view["everyone"] == ["Doors close at 10."]
    assert view["people"] == [{"user_id": JORDAN, "name": "Jordan", "lines": ["Jordan likes jazz."]}]


@pytest.mark.asyncio
async def test_a_line_is_changed_and_removed_in_place(home):
    await (await household.async_setup(home)).save(KNOWLEDGE)
    ui = Panel(home)
    assert await ui("household/edit", scope="everyone", action="update", index=1, old="Shoes off inside.",
                    text="Shoes off at the door.") == {"lines": 3}
    assert await ui("household/edit", scope="everyone", action="delete", index=0, old="Doors close at 10.") == {
        "lines": 2}
    assert await stored(home) == {"everyone": ["Shoes off at the door."], "people": KNOWLEDGE["people"]}


@pytest.mark.parametrize("edit", [
    {"action": "update", "index": 0, "old": "Doors close at 9.", "text": "New."},
    {"action": "delete", "index": 0, "old": "Doors close at 9."},
    {"action": "delete", "index": 2, "old": "Doors close at 10."},
    {"action": "delete", "index": -2, "old": "Doors close at 10."},
    {"action": "update", "old": "Doors close at 10.", "text": "New."},
    {"action": "delete", "index": 0},
], ids=["update-changed", "delete-changed", "index-past-the-end", "negative-index", "no-index", "no-old-text"])
@pytest.mark.asyncio
async def test_a_line_must_still_read_as_it_did_to_be_changed_or_removed(home, edit):
    await (await household.async_setup(home)).save(KNOWLEDGE)
    assert await Panel(home)("household/edit", scope="everyone", **edit) == ("refused", "changed")
    assert await stored(home) == KNOWLEDGE


@pytest.mark.asyncio
async def test_an_empty_line_is_refused(home):
    await (await household.async_setup(home)).save(KNOWLEDGE)
    ui = Panel(home)
    assert await ui("household/edit", scope="everyone", action="add", text="   ") == ("refused", "empty")
    assert await ui("household/edit", scope=ALEX, action="add") == ("refused", "empty")
    assert await ui("household/edit", scope="everyone", action="update", index=0, old="Doors close at 10.",
                    text="\n") == ("refused", "empty")
    assert await stored(home) == KNOWLEDGE


@pytest.mark.asyncio
async def test_lines_are_kept_only_for_people_set_up_in_home_assistant(home):
    await household.async_setup(home)
    assert await Panel(home)("household/edit", scope=GONE, action="add", text="Old line.") == (
        "refused", "unknown_person")
    assert await stored(home) == {"everyone": [], "people": {}}


@pytest.mark.asyncio
async def test_a_removed_persons_lines_can_still_be_deleted_from_the_panel(home):
    await (await household.async_setup(home)).save({"everyone": [], "people": {GONE: ["Old line."]}})
    ui = Panel(home)
    assert (await ui("household"))["people"] == [{"user_id": GONE, "name": "Someone", "lines": ["Old line."]}]
    assert await ui("household/edit", scope=GONE, action="delete", index=0, old="Old line.") == {"lines": 0}


@pytest.mark.asyncio
async def test_removing_a_persons_last_line_removes_them_from_the_household_view(home):
    await (await household.async_setup(home)).save(KNOWLEDGE)
    ui = Panel(home)
    assert await ui("household/edit", scope=ALEX, action="delete", index=0, old="Alex likes short answers.") == {
        "lines": 2}
    assert (await ui("household"))["people"] == []
    assert await stored(home) == {"everyone": KNOWLEDGE["everyone"], "people": {}}


@pytest.mark.asyncio
async def test_only_add_change_or_delete_with_a_scope_are_accepted(home):
    await (await household.async_setup(home)).save(KNOWLEDGE)
    ui = Panel(home)
    assert await ui("household/edit", scope="everyone", action="replace", text="New.") == ("refused", "invalid_format")
    assert await ui("household/edit", action="add", text="New.") == ("refused", "invalid_format")
    assert await ui("household/edit", scope="everyone", action="delete", index="0", old="Doors close at 10.") == (
        "refused", "invalid_format")
    assert await stored(home) == KNOWLEDGE


@pytest.mark.asyncio
async def test_only_administrators_read_or_edit_the_household_knowledge(home):
    await (await household.async_setup(home)).save(KNOWLEDGE)
    claude = Assistant(home, "Claude", ["assist"])
    guest = Panel(home, JORDAN, "Jordan", admin=False)
    for command, fields in (("household", {}), ("household/edit", {"scope": "everyone", "action": "add", "text": "x"})):
        assert await guest(command, **fields) == ("refused", "unauthorized")
    assert await stored(home) == KNOWLEDGE and claude.apis == ["assist"] and home.data[household.KEY].on


@pytest.mark.asyncio
async def test_two_edits_at_once_never_cross(home):
    await (await household.async_setup(home)).save({"everyone": ["Doors close at 10."], "people": {}})
    alex, jordan = Panel(home), Panel(home, JORDAN, "Jordan")
    # Both opened the same line and saved a change: the first one wins, the second is told it changed.
    first = alex.send("household/edit", scope="everyone", action="update", index=0, old="Doors close at 10.",
                     text="Doors close at 11.")
    second = jordan.send("household/edit", scope="everyone", action="update", index=0, old="Doors close at 10.",
                      text="Doors close at 9.")
    # Two lines added at once: both kept.
    mine = alex.send("household/edit", scope=ALEX, action="add", text="Alex likes short answers.")
    theirs = jordan.send("household/edit", scope="everyone", action="add", text="Shoes off inside.")
    assert await alex.reply(first) == {"lines": 1}
    assert await jordan.reply(second) == ("refused", "changed")
    assert await alex.reply(mine) == {"lines": 2} and await jordan.reply(theirs) == {"lines": 3}
    assert await stored(home) == {"everyone": ["Doors close at 11.", "Shoes off inside."],
                                  "people": {ALEX: ["Alex likes short answers."]}}


# Uninstalling and reinstalling.

# The household's lines as one plain text to save: everyone's, then each person's under their name.
TEXT = "- Doors close at 10.\n- Shoes off inside.\n\nFor Alex:\n- Alex likes short answers."


def notice(hass):
    """The notification removing the improver left, as Home Assistant shows it, or None."""
    from homeassistant.components import persistent_notification
    return persistent_notification._async_get_or_create_notifications(hass).get(household.NOTICE)


def to_save(shown):
    """The text the notification gives to save: its code block."""
    return shown["message"].split("```\n", 1)[1].split("\n```", 1)[0]


async def removed_from_home_assistant(hass):
    """Removed as Home Assistant's own page removes it (Settings, Devices & services, Assist Improver, Delete)."""
    await hass.config_entries.async_remove(hass.config_entries.async_entries(DOMAIN)[0].entry_id)
    await hass.async_block_till_done()


def uninstalling(hass, changesets=None):
    """The improver loaded as the settings' Uninstall finds it: its config entry, and its changesets."""
    from custom_components.assist_improver.changeset import Changesets
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    serve(hass, SimpleNamespace(pipeline=SimpleNamespace(entry=entry),
                                improvements=SimpleNamespace(changesets=changesets or Changesets(hass))))
    return entry


@pytest.mark.asyncio
async def test_removing_it_from_home_assistants_page_writes_into_no_assistant_and_leaves_the_lines_to_save(home):
    await (await household.async_setup(home)).save(KNOWLEDGE)
    claude = Assistant(home, "Claude", ["assist", "other_api"], prompt="Be brief.\n")
    haiku = Assistant(home, "Haiku", ["assist"], prompt="Be kind.")
    chat = Assistant(home, "Chat only", None, prompt="Chat.")  # Doesn't control Home Assistant.
    task = Assistant(home, "Task", ["assist"], prompt="Task.", kind="ai_task_data")  # An AI Task, not an assistant.
    everyone = (claude, haiku, chat, task)
    before = [a.data for a in everyone]
    await removed_from_home_assistant(home)
    assert [a.data for a in everyone] == before and not home.config_entries.async_entries(DOMAIN)
    shown = notice(home)
    assert shown["title"] == "Assist Improver"
    assert shown["message"] == f"{household.GOODBYE}\n\n```\n{TEXT}\n```"
    reads = await what_it_reads(home, haiku, ALEX)
    assert not any(line in reads for line in LINES)  # Nothing reads its store now.


@pytest.mark.asyncio
async def test_each_persons_lines_are_named_never_only_by_an_id(home):
    await (await household.async_setup(home)).save({"everyone": [], "people": {
        JORDAN: ["Jordan likes jazz."], GONE: ["Lights off at 11."]}})
    await removed_from_home_assistant(home)
    text = to_save(notice(home))
    assert "For Jordan:\n- Jordan likes jazz." in text and "For a removed user:\n- Lights off at 11." in text
    assert JORDAN not in text and GONE not in text


@pytest.mark.asyncio
async def test_removing_it_with_nothing_learned_leaves_no_notification(home):
    await household.async_setup(home)
    await removed_from_home_assistant(home)
    assert notice(home) is None


@pytest.mark.asyncio
async def test_removing_it_after_a_failed_start_still_hands_the_lines_over(home):
    await household.Household(home).save(KNOWLEDGE)  # In the store, but never loaded in this run.
    assert household.KEY not in home.data
    await removed_from_home_assistant(home)
    assert to_save(notice(home)) == TEXT


@pytest.mark.asyncio
async def test_uninstall_in_the_settings_shows_the_same_lines_to_save_then_removes_it_with_no_notification(home):
    await (await household.async_setup(home)).save(KNOWLEDGE)
    uninstalling(home)
    claude = Assistant(home, "Claude", ["assist"], prompt="Be brief.")
    haiku = Assistant(home, "Haiku", ["assist"], prompt="")
    before = (claude.data, haiku.data)
    panel = Panel(home)
    assert (await panel("household"))["text"] == TEXT  # What its step shows to copy.
    assert await panel("uninstall") == {"removed": True}
    assert (claude.data, haiku.data) == before and notice(home) is None
    assert not home.config_entries.async_entries(DOMAIN) and await stored(home) == household.EMPTY
    reads = await what_it_reads(home, claude, ALEX)
    assert not any(line in reads for line in LINES)


@pytest.mark.asyncio
async def test_uninstall_waits_for_a_change_left_open(home):
    """A change still waiting for a panel to finish its scripts would be lost with the improver's stores: Uninstall is
    refused until it's finished, and nothing is removed."""
    from custom_components.assist_improver.changeset import Changesets
    await (await household.async_setup(home)).save(KNOWLEDGE)
    changesets = Changesets(home)
    changesets.journal = {"improvement_id": "imp1", "action": "apply", "steps": [], "done": 0}  # As load() finds it.
    uninstalling(home, changesets)
    panel = Panel(home)
    assert await panel("uninstall") == ("refused", "busy")
    assert json.loads(panel.sent[-1])["error"]["message"] == "Another change is still being applied."
    assert home.config_entries.async_entries(DOMAIN) and await stored(home) == KNOWLEDGE and notice(home) is None


@pytest.mark.asyncio
async def test_only_an_administrator_can_uninstall(home):
    await (await household.async_setup(home)).save(KNOWLEDGE)
    uninstalling(home)
    assert await Panel(home, JORDAN, "Jordan", admin=False)("uninstall") == ("refused", "unauthorized")
    assert home.config_entries.async_entries(DOMAIN) and await stored(home) == KNOWLEDGE


@pytest.mark.asyncio
async def test_reinstalled_it_starts_with_no_lines_and_never_doubles_them(home):
    await (await household.async_setup(home)).save(KNOWLEDGE)
    claude = Assistant(home, "Claude", ["assist"], prompt="Be brief.")
    uninstalling(home)
    assert await Panel(home)("uninstall") == {"removed": True}
    improver_entry(home)  # Reinstalled, before Home Assistant restarts.
    assert (await household.async_setup(home)).knowledge == household.EMPTY and claude.prompt == "Be brief."
    await (await household.async_setup(home)).save(KNOWLEDGE)  # The same lines learned again.
    reads = await what_it_reads(home, claude, ALEX)
    assert all(reads.count(line) == 1 for line in LINES)


# Improvements adding lines.

@pytest.mark.asyncio
async def test_an_accepted_line_reaches_the_persons_requests_on_every_assistant_and_its_review_names_them(home):
    from custom_components.assist_improver.improvements import Improvements
    await household.async_setup(home)
    claude = Assistant(home, "Claude", ["assist"])
    haiku = Assistant(home, "Haiku", ["assist"])  # Not the one it was tested on.
    improvements = Improvements(home, None)
    await improvements.start()
    improvements.items["imp1"] = {
        "id": "imp1", "title": "Short answers for Alex", "cause": "Alex asked for shorter answers twice.",
        "status": "awaiting_review", "created": "1", "decisions": [], "application": None, "history": [],
        "copy": {"status": "failed"}, "test": tested(), "fixes": [],
        "changes": [{"kind": "preference", "for": "this_user", "text": "Alex likes short answers."}],
        "setup": {}, "tested": {"scripts": {}, "exposure": {}}, "user_ids": [ALEX],
        "provider": {"config_entry_id": claude.entry.entry_id, "subentry_id": "sub", "title": "Claude"}}
    serve(home, runtime_with_flow(home, improvements=improvements, recorder=SimpleNamespace(index={})))
    ui = Panel(home)
    shown = await ui("improvement", improvement_id="imp1")
    assert [c["kind"] for c in shown["changes"]] == ["household"]  # No assistant to switch.
    added = [r["text"] for r in shown["changes"][0]["rows"] if r["sign"] == "+"]
    assert "For Alex:" in added and "- Alex likes short answers." in added and not any(ALEX in t for t in added)
    assert any("Only settings change: 1 household line." in fact for fact in shown["safety"])
    decided = await ui("decide", improvement_id="imp1", decision="accept")
    assert decided["ok"] and decided["status"] == "applied"
    for assistant in (claude, haiku):
        assert "Alex likes short answers." in await what_it_reads(home, assistant, ALEX)
        assert "Alex likes short answers." not in await what_it_reads(home, assistant, JORDAN)
    assert claude.data == haiku.data == {"prompt": "Be brief.", "chat_model": "fixture", "llm_hass_api": ["assist"]}
    assert (await ui("household"))["people"] == [{"user_id": ALEX, "name": "Alex", "lines": ["Alex likes short answers."]}]


def test_an_improvements_line_goes_to_the_person_speaking_or_to_everyone_and_is_never_added_twice():
    mine = {"kind": "preference", "for": "this_user", "text": " Alex likes short answers. "}
    shared = {"kind": "preference", "for": "everyone", "text": "Doors close at 10."}
    # Other kinds of change aren't household lines: a script's description, a device's aliases, an assistant's
    # instructions.
    unrelated = [{"kind": "script", "id": "porch", "config": {"description": "Turns on the porch light.",
                                                              "sequence": []}},
                 {"kind": "registry", "entity_id": "light.porch", "aliases": ["porch lamp"]},
                 {"kind": "assistant", "target": "e/s", "text": "Say the room.", "replaces": ["Be brief."]}]
    start = {"everyone": [], "people": {}}
    knowledge, prompt, problems = household.edit(start, [mine, shared, *unrelated], ALEX, "Be brief.")
    assert knowledge == {"everyone": ["Doors close at 10."], "people": {ALEX: ["Alex likes short answers."]}}
    assert prompt == "Be brief." and problems == [] and start == {"everyone": [], "people": {}}
    assert household.edit(knowledge, [mine, shared], ALEX)[0] == knowledge  # Applied again: not added twice.
    # Nobody known to be speaking: a line meant for them goes to everyone.
    assert household.edit(start, [mine], None)[0] == {"everyone": ["Alex likes short answers."], "people": {}}


def test_a_replaced_line_is_removed_where_it_is_and_one_changed_since_is_a_problem():
    knowledge = {"everyone": ["Doors close at 10."], "people": {JORDAN: ["Jordan likes jazz."]}}
    new = lambda old, text="Doors close at 11.": {"kind": "preference", "text": text, "replaces": [old]}
    # In the household knowledge (anyone's lines): removed there.
    after, prompt, problems = household.edit(knowledge, [new("Jordan likes jazz.")], ALEX, "Be brief.")
    assert after == {"everyone": ["Doors close at 10.", "Doors close at 11."], "people": {}} and not problems
    # In the assistant's own instructions (written there before): removed from them.
    after, prompt, problems = household.edit(knowledge, [new("Lights off at night.")], ALEX,
                                             "Be brief.\nLights off at night.")
    assert prompt == "Be brief.\n" and "Doors close at 11." in after["everyone"] and not problems
    # In neither: it changed since the improvement was tested.
    after, prompt, problems = household.edit(knowledge, [new("Lights off at night.")], ALEX, "Be brief.")
    assert len(problems) == 1 and "Lights off at night." in problems[0]
    # Unless the new line is already there (the same improvement, applied again).
    assert household.edit(knowledge, [new("Lights off at night.", "Doors close at 10.")], ALEX, "Be brief.")[2] == []

