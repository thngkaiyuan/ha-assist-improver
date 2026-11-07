"""A hypothesis's changes, placed in the sandbox: HA's own configuration, validated and run by HA."""

from copy import deepcopy

import pytest
import pytest_asyncio

from conftest import assist_home
from recordings import REPLAY_ENTITIES, SCRIPT, TARGET, at_home, bind_agent, make_recording, recorded_today
from custom_components.assist_improver.cases import CaseError, recording_turns
from custom_components.assist_improver.changes import setup_from_changes
from custom_components.assist_improver.replay import SandboxHome
from custom_components.assist_improver.simulate import Trial, after_plan, planned, setup_for


@pytest_asyncio.fixture
async def hass(hass):
    return await assist_home(hass, REPLAY_ENTITIES)


SOURCE = recording_turns(make_recording())[0]["source"]
SCRIPTS = {"fixture_press": {"alias": "Fixture press", "sequence": [
    {"action": "switch.turn_off", "target": {"entity_id": TARGET}}, {"action": "switch.turn_on", "target": {"entity_id": TARGET}}]}}


def test_each_change_kind_becomes_its_own_configuration():
    """Each kind as a sandbox runs it, Home Assistant's own format whole: an editor's item by its id, a registry
    entry's fields, household lines; a new script or scene exposed to Assist."""
    night = {"alias": "Night", "triggers": [{"trigger": "time", "at": "22:00:00"}],
             "actions": [{"action": "switch.turn_off", "target": {"entity_id": TARGET}}]}
    setup = setup_from_changes([
        {"kind": "preference", "text": "The relay is the garage door.", "for": "this_user"},
        {"kind": "preference", "text": "Press relays off then on.", "for": "everyone", "replaces": ["Old line."]},
        {"kind": "registry", "entity_id": TARGET, "aliases": ["Garage door"], "name": "Relay"},
        {"kind": "registry", "entity_id": SCRIPT, "expose": True},
        {"kind": "script", "id": "open_the_garage", "config": {
            "alias": "Open the garage", "description": "Presses the relay.", "mode": "queued",
            "sequence": [{"action": "switch.turn_off", "target": {"entity_id": TARGET}},
                         {"action": "switch.turn_on", "target": {"entity_id": TARGET}}, {"stop": "Opening."}]}},
        {"kind": "script", "id": "fixture_press", "config": {**SCRIPTS["fixture_press"],
                                                             "description": "Presses the relay once."}},
        {"kind": "automation", "id": "night", "config": night},
        {"kind": "scene", "id": "evening", "config": {"name": "Evening", "entities": {TARGET: "off"}}},
    ], source=SOURCE, prompt="Be brief.\nOld line.\n", scripts=SCRIPTS, user_id="u1")
    # Instruction lines go to the household knowledge; a replaced line is removed where it was (here, the prompt).
    assert setup["prompt"] == "Be brief.\n\n"
    assert setup["household"] == {"everyone": ["Press relays off then on."], "people": {"u1": ["The relay is the garage door."]}}
    assert setup["aliases"] == {TARGET: ["Garage door"]}
    assert setup["registry"] == {"entities": {TARGET: {"name": "Relay"}}}
    assert setup["exposure"] == {SCRIPT: True, "script.open_the_garage": True, "scene.evening": True}
    assert setup["scripts"]["open_the_garage"]["mode"] == "queued"
    assert setup["scripts"]["fixture_press"]["description"] == "Presses the relay once."
    assert setup["automations"] == {"night": {**night, "id": "night"}}
    assert setup["scenes"]["evening"]["entities"] == {TARGET: "off"}
    assert setup["prompt_changed"]
    for bad in ({"kind": "preference", "text": "x", "for": "everyone", "replaces": ["not there"]},
                {"kind": "registry", "entity_id": "light.nope", "aliases": ["x"]},
                {"kind": "script", "id": "fixture_press"}):
        with pytest.raises(CaseError):
            setup_from_changes([bad], source=SOURCE, prompt="Be brief.", scripts=SCRIPTS)


def test_an_assistant_change_reaches_only_its_own_assistants_conversations_and_a_recommendation_none():
    """An edit of an assistant's instructions or LLM APIs changes a conversation's setup only when that assistant
    answered it; a recommendation is never replayed."""
    for_it = [{"kind": "assistant", "target": "a/1", "text": "Be brief.", "llm_hass_api": ["assist"]}]
    setup = setup_from_changes(for_it, source=SOURCE, prompt="Hello.", scripts=SCRIPTS, assistant="a/1")
    assert setup["prompt"] == "Hello.\nBe brief." and setup["llm_hass_api"] == ["assist"]
    other = setup_from_changes(for_it, source=SOURCE, prompt="Hello.", scripts=SCRIPTS, assistant="b/2")
    assert other["prompt"] is None and "llm_hass_api" not in other
    advice = [{"kind": "assistant", "target": "a/1", "recommend": {"chat_model": "a larger model"}}]
    assert setup_from_changes(advice, source=SOURCE, prompt="Hello.", scripts=SCRIPTS, assistant="a/1") == \
        setup_from_changes([], source=SOURCE, prompt="Hello.", scripts=SCRIPTS, assistant="a/1")


@pytest.mark.asyncio
async def test_a_registry_change_is_seeded_as_home_assistants_registry_would_hold_it(hass, effect_guards):
    """Aliases given whole replace the entity's own: the sandbox holds them as Home Assistant's registry would after
    the update, with its new name."""
    record = make_recording()
    turn = recording_turns(record)[0]
    today = recorded_today(record, scripts=SCRIPTS, automations=[])
    today["home"]["entities"][TARGET]["aliases"] = ["Old name", "Kept name"]  # Its names now.
    setup = setup_from_changes([{"kind": "registry", "entity_id": TARGET, "aliases": ["Kept name", "Garage door"],
                                 "name": "Relay"}], source=today["home"], prompt="", scripts=SCRIPTS)
    assert setup["aliases"] == {TARGET: ["Garage door"]} and setup["alias_removals"] == {TARGET: ["Old name"]}
    home = SandboxHome(hass, record, turn, "fixture-alias", today, change=setup)
    try:
        await home.start()
        from homeassistant.helpers import entity_registry as er
        async def entry_of(sandbox_hass):
            entry = er.async_get(sandbox_hass).async_get(TARGET)
            return list(entry.aliases), entry.name
        from conftest import inside
        assert await inside(home.sandbox).run(entry_of) == (["Kept name", "Garage door"], "Relay")
    finally:
        await home.stop()


@pytest.mark.asyncio
async def test_automations_and_scenes_are_tested_on_top_of_todays_setup(hass, effect_guards):
    """Simulate tests every kind in the replay sandbox ("could we just support these?"): an automation edited, one
    turned off and a new one, and a new scene, seeded on top of today's setup and loaded by Home Assistant itself; the
    household's other items stay as they are."""
    from homeassistant.components.automation.const import DATA_COMPONENT
    record = make_recording()
    turn = recording_turns(record)[0]
    at = lambda time: {"trigger": "time", "at": time}
    press = [{"action": "switch.turn_on", "target": {"entity_id": TARGET}}]
    evening = {"id": "evening", "alias": "Evening", "triggers": [at("18:00:00")], "actions": press}
    morning = {"id": "morning", "alias": "Morning", "triggers": [at("07:00:00")], "actions": press}
    today = recorded_today(record, scripts=SCRIPTS, automations=[evening, morning])
    for item in (evening, morning):
        today["home"]["states"][f"automation.{item['id']}"] = {
            "state": "on", "attributes": {"id": item["id"], "friendly_name": item["alias"]}}
    setup = setup_from_changes([
        {"kind": "automation", "id": "evening", "config": {**evening, "triggers": [at("19:00:00")]}},
        {"kind": "automation", "id": "morning", "on": False},
        {"kind": "automation", "id": "night", "config": {"alias": "Night", "triggers": [at("23:00:00")],
                                                         "actions": press}},
        {"kind": "scene", "id": "dim", "config": {"name": "Dim", "entities": {TARGET: "off"}}},
    ], source=today["home"], prompt="", scripts=SCRIPTS)
    home = SandboxHome(hass, record, turn, "fixture-kinds", today, change=setup)
    try:
        await home.start()

        async def loaded(sandbox_hass):
            automations = {e.entity_id: (e.raw_config, sandbox_hass.states.get(e.entity_id).state)
                           for e in sandbox_hass.data[DATA_COMPONENT].entities}
            return automations, sandbox_hass.states.get("scene.dim")
        from conftest import inside
        automations, scene = await inside(home.sandbox).run(loaded)
        assert automations["automation.evening"] == ({**evening, "triggers": [at("19:00:00")]}, "on")
        assert automations["automation.morning"] == (morning, "off")
        assert automations["automation.night"][0]["triggers"] == [at("23:00:00")]
        assert automations["automation.night"][1] == "on"
        assert scene is not None and scene.attributes["entity_id"] == [TARGET]
    finally:
        await home.stop()


@pytest.mark.asyncio
async def test_a_generated_hypothesis_replays_with_its_changes(hass, monkeypatch, effect_guards):
    from recordings import prepared_recording
    record = prepared_recording()
    record["turns"][0]["source"].update(scripts=deepcopy(SCRIPTS), automations=[])  # The household's, as now.
    hypothesis = {"title": "Open the garage with a press", "cause": "No press tool", "expected": "One turn",
                  "goals": [{"session": "synthetic", "goal_turn": 1}], "support": [], "conflicts": [],
                  "changes": [{"kind": "preference", "text": "PREFERENCE_MARKER: a press is off then on.", "for": "this_user"},
                              {"kind": "script", "id": "open_the_garage", "config": {
                                  "alias": "Open the garage", "description": "Presses the relay.", "sequence": [
                                      {"action": "switch.turn_off", "target": {"entity_id": TARGET}},
                                      {"action": "switch.turn_on", "target": {"entity_id": TARGET}},
                                      {"stop": "Opening."}]}}]}
    agent, client = await bind_agent(hass, record)
    at_home(monkeypatch, record)
    before, today = await planned(hass, record)
    setup, _, _ = setup_for(hypothesis, record, today, hass)
    client.blocks = [[{"type": "tool_use", "id": "s1", "name": "script__open_the_garage", "input": {}}],
                     [{"type": "text", "text": "Opening."}]] + [[{"type": "text", "text": "Ok."}]] * 3
    hass.states.async_set(TARGET, "LIVE")
    result = await Trial(hass, after_plan(before, setup, []), record, today, setup=setup).run()
    assert result["status"] == "done", result["error"]
    assert [t["new"] for t in result["steps"][0]["transitions"] if t["entity_id"] == TARGET] == ["off", "on"]
    assert "PREFERENCE_MARKER" in client.requests[0]["system"][0]["text"]  # The changed instructions, as HA renders them.
    assert "script__open_the_garage" in {t["name"] for t in client.requests[0]["tools"]}
    assert hass.states.get(TARGET).state == "LIVE" and effect_guards == []


@pytest.mark.asyncio
async def test_household_knowledge_comes_with_assist_for_the_person_speaking(hass, effect_guards):
    from homeassistant.core import Context
    from homeassistant.helpers import llm
    from conftest import improver_entry
    from custom_components.assist_improver import household
    context = lambda user: llm.LLMContext(platform="fixture", context=Context(user_id=user), language="en",
                                          assistant="conversation", device_id=None)
    improver_entry(hass)
    plain = await llm.async_get_api(hass, llm.LLM_API_ASSIST, context("alex"))
    await (await household.async_setup(hass)).save(
        {"everyone": ["The relay is the garage door."], "people": {"alex": ["Alex likes short answers."]}})
    mine = await llm.async_get_api(hass, llm.LLM_API_ASSIST, context("alex"))
    theirs = await llm.async_get_api(hass, llm.LLM_API_ASSIST, context("someone"))
    assert [t.name for t in mine.tools] == [t.name for t in plain.tools]  # The same tools, the same names.
    assert mine.api_prompt.startswith(household.HEADING) and mine.api_prompt.endswith(plain.api_prompt)
    assert "The relay is the garage door." in mine.api_prompt and "Alex likes short answers." in mine.api_prompt
    assert "Alex likes short answers." not in theirs.api_prompt


@pytest.mark.asyncio
async def test_a_replay_reads_a_changes_household_lines_through_assist(hass, effect_guards):
    record = make_recording()
    turn = recording_turns(record)[0]
    knowledge = {"everyone": ["Press relays off then on."], "people": {}}
    home = SandboxHome(hass, record, turn, "fixture-household", recorded_today(record, scripts=SCRIPTS, automations=[]),
                       change={"household": knowledge})
    try:
        built = await home.assist(turn)
        assert "Press relays off then on." in built["prompt"]
        names = [t["name"] for t in built["tools"]]
        assert "intent__HassTurnOn" in names and len(names) == len(set(names))  # Assist's own tools, each once.
        assert built["api_prompt"].startswith("Household knowledge")  # Where Assist puts it, as on the real instance.
    finally:
        await home.stop()


@pytest.mark.parametrize("now, reads", [
    ({"household_on": True}, True),  # The improver is set up: Assist reads the lines.
    ({"household_on": False}, False),  # Disabled: Assist is Home Assistant's own.
], ids=["improver-on", "improver-off"])
@pytest.mark.asyncio
async def test_a_replay_reads_the_household_lines_exactly_when_assist_reads_them_at_home_now(hass, effect_guards, now,
                                                                                           reads):
    """The household knowledge, and whether the improver is on, are the household's setup: a replay reads them as they
    are now, whatever the conversation's Assist read."""
    record = make_recording()
    knowledge = {"everyone": ["Press relays off then on."], "people": {}}
    for raw in record["turns"]:  # Recorded with the improver the other way.
        raw["source"].update(household={"everyone": ["Recorded line."], "people": {}},
                             household_on=not now["household_on"])
    turn = recording_turns(record)[0]
    home = SandboxHome(hass, record, turn, "fixture-household-now",
                       recorded_today(record, scripts=SCRIPTS, automations=[], household=knowledge, **now))
    try:
        built = await home.assist(turn)
        assert ("Press relays off then on." in built["prompt"]) is reads and "Recorded line." not in built["prompt"]
    finally:
        await home.stop()


@pytest.mark.asyncio
async def test_a_replay_whose_assist_misses_the_household_lines_stops_never_tested_without_them(
        hass, effect_guards, monkeypatch):
    """The sandbox's fault, not the conversation's: it stops as a failure of the machinery, which says nothing about
    the idea (simulate.TRANSIENT)."""
    from custom_components.assist_improver import household
    from custom_components.assist_improver.worker import WorkerError
    async def serve(hass, knowledge):  # The improver isn't set up: Assist never asks it.
        hass.data[household.KEY] = household.Fixed(knowledge)
    monkeypatch.setattr(household, "serve", serve)
    record = make_recording()
    turn = recording_turns(record)[0]
    home = SandboxHome(hass, record, turn, "fixture-missed", recorded_today(record, scripts=SCRIPTS, automations=[]),
                       change={"household": {"everyone": ["Press relays off then on."], "people": {}}})
    try:
        with pytest.raises(WorkerError, match="household knowledge"):
            await home.start()
    finally:
        await home.stop()


@pytest.mark.asyncio
async def test_a_script_that_takes_inputs_is_checked_in_the_replays_not_run_without_them():
    """The proposer's script check runs each script with nothing given: one that takes inputs from Assist would run
    with them empty, which says nothing of the fix, so it's left to the replays."""
    from custom_components.assist_improver.evidence import Evidence

    ran = []

    async def run(recording, turn, setup, scripts, targets):
        ran.append(scripts)
        return {"scripts": scripts, "reaches_goal": reaches}
    household = {"scripts": {"announce": {"fields": {"message": {}}, "sequence": []}}}  # Exposed by the change.
    found = Evidence(None, {"home": household})  # The household's scripts now.
    found._run_scripts = run
    lamp = [{"entity_id": "light.lamp", "state": "on"}]
    for setup, checked in (({"scripts": {"say_it": {"fields": {"message": {}}, "sequence": []}}}, False),
                           ({"scripts": {"lamp_on": {"sequence": []}}}, True)):
        for reaches in (True, False):
            ran.clear()
            out = await found._try_scripts({}, {"source": {}}, setup, [*setup["scripts"], "announce"], lamp, "s:1")
            assert out["left_to_the_replays"][-1] == "script.announce takes inputs that Assist gives when it runs it"
            assert ran == ([["lamp_on"]] if checked else [])
            assert out["reaches_goal"] is (True if checked and reaches else None)  # Not reached yet: the replays tell.
