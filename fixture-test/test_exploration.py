"""The agentic analyzer's session tools, and HA's real OpenRouter AI Task running them."""

import json
from types import SimpleNamespace

import pytest
from homeassistant.components.open_router.ai_task import OpenRouterAITaskEntity

from ha_task import stand_in
from test_ai_roles import ANALYSIS
from test_interception import completion
from recordings import SCRIPT, TARGET, make_recording
from test_milestone1 import recording_hass  # noqa: F401
from custom_components.assist_improver import analyzer
from custom_components.assist_improver.exploration import SessionAPI, SessionView
from custom_components.assist_improver.reflective import run_role


def test_overview_starts_with_turn_one_rooms_and_tools():
    view = SessionView(make_recording())
    overview = view.overview()
    assert [t["turn"] for t in overview["conversation"]] == [t["turn"] for t in view.digest["turns"]]
    assert overview["conversation"][0]["user_says"] and "assistant_says" in overview["conversation"][0]
    assert "state_changes_during_turn" not in json.dumps(overview)  # Details are fetched with read_turn.
    assert overview["areas"] == [{"area": "Fixture room", "aliases": [], "entities": 2}]
    assert "intent__HassTurnOn" in overview["assist_tools"]
    assert "capabilities" not in json.dumps(overview)  # The big device list is fetched on demand, not sent.


def test_the_overview_lists_each_tool_call_with_its_result_and_the_devices_touched_come_from_one_record():
    """DETAILS P9.10.5: what happened is one record, read the same way everywhere: the overview gives each of
    Assist's tool calls with Home Assistant's result, clipped, never a reading of its own of what an intent's response
    acted on; the devices a session touched are those its calls were at (their results, acted on or failed on, and
    the turn's call log, a script's own calls in it) and those that changed."""
    record = make_recording()
    view = SessionView(record)
    first = view.overview()["conversation"][0]
    [call] = first["assist_called"]
    assert call["call"] == view.calls[1][0]["tool"] and call["args"] == view.calls[1][0]["args"]
    assert call["result"] == json.dumps(view.calls[1][0]["result"], ensure_ascii=False, default=str)[:200] + (
        call["result"][200:])  # Clipped, past 200 characters, with how much it left out.
    assert "assist_acted_on" not in json.dumps(view.overview())
    long = view.read_turn(1)["assistant"]  # read_turn: the results past the overview's clip, and the state changes.
    assert [s["result"] for s in long if "result_of" in s] == [view.calls[1][0]["result"]]
    assert "tool results past the overview's clip, appear only in read_turn" in analyzer.INSTRUCTIONS
    assert not hasattr(view, "acted_on")
    assert view.devices_touched() == {TARGET}
    record["turns"][0]["actions"] = [{"action": SCRIPT, "data": {}}, {  # The script's own action, and its call.
        "action": "tts.speak", "data": {"entity_id": ["tts.fixture_engine"], "media_player_entity_id": "media_player.x",
                                        "message": "Dinner"}, "under": 0}]
    assert SessionView(record).devices_touched() == {TARGET, SCRIPT, "tts.fixture_engine", "media_player.x"}


def test_turns_carry_their_own_state_changes():
    view = SessionView(make_recording())
    changes = [c for t in view.digest["turns"] for c in view.read_turn(t["turn"])["state_changes_during_turn"]]
    assert changes and all(c["entity_id"] == TARGET for c in changes)
    assert "error" in view.read_turn(99)


def test_exploring_the_household():
    view = SessionView(make_recording())
    room = view.devices_in_area("fixture room")["devices"]
    assert {d["entity_id"]: d["exposed"] for d in room} == {TARGET: True, SCRIPT: False}
    assert "error" in view.devices_in_area("Attic")
    assert [d["entity_id"] for d in view.find_devices("fixture relay")["devices"]] == [TARGET]
    assert view.device_details(TARGET)["changes_during_session"]
    assert [(s["entity_id"], s["exposed"]) for s in view.find_devices("press")["devices"]] == [(SCRIPT, False)]


def test_search_reaches_every_entity_labelled_ranked_and_paged():
    """Kai: "fix all those problems ... as general and as broadly and cleanly as possible". Search and listings
    reach every entity, disabled, hidden and diagnostic ones included, each with its label; exposed entities and
    controls come first; search reads every text field of the entity, its device (a child device's maker is its
    parent's) and its integration; and a listing comes 25 to a page with the total."""
    from custom_components.assist_improver.exploration import PAGE, Setup
    record = make_recording()
    source = record["turns"][0]["source"]
    source["devices"].update({"hub": {"id": "hub", "name": "Hub", "manufacturer": "Acme", "model": "Relay Pro",
                                      "model_id": "RP-2X", "config_entry_id": "01JCAFE"},
                              "port": {"id": "port", "name": "Port 2", "parent_device_id": "hub"}})
    added = {"light.spare": {"disabled_by": "user"}, "switch.spare": {"hidden_by": "user"},
             "sensor.spare_signal": {"entity_category": "diagnostic"}}
    for entity_id, labels in added.items():
        source["entities"][entity_id] = {"entity_id": entity_id, "device_id": "port", "platform": "fixture_hub",
                                         **labels}
        source["exposure"][entity_id] = False
    for n in range(PAGE + 5):  # More than a page of readings' sensors in no area.
        source["states"][f"sensor.level_{n:02}"] = {"entity_id": f"sensor.level_{n:02}", "state": "1",
                                                    "attributes": {}}
    view = SessionView(record, setup=Setup({"switch.turn_on": {"action": "switch.turn_on",
                                                               "target": {"entity": [{"domain": ["switch"]}]}}},
                                           {"fixture_hub": "Fixture Hub"}))
    # The parent's maker, model and model ID; the integration's name.
    for query in ("acme", "relay pro", "rp-2x", "fixture hub"):
        found = {d["entity_id"]: d for d in view.find_devices(query)["devices"]}
        assert {e: {k: found[e].get(k) for k in labels} for e, labels in added.items()} == added, query
    hub = view.find_devices("acme")["devices"]
    assert [d["entity_id"] for d in hub] == ["switch.spare", "light.spare", "sensor.spare_signal"]  # A control first.
    assert view.find_devices("diagnostic")["devices"][0]["entity_id"] == "sensor.spare_signal"
    assert view.find_devices("cafe")["devices"] == []  # Not an ID's letters.
    first, second = view.devices_in_area("No area"), view.devices_in_area("No area", page=2)
    assert first["total"] == second["total"] == PAGE + 8 and len(first["devices"]) == PAGE
    assert first["next_page"] == 2 and len(second["devices"]) == 8 and "next_page" not in second
    assert view.device_details("light.spare")["same_physical_device"][0]["entity_id"] == "switch.spare"
    assert {"area": "No area", "aliases": [], "entities": PAGE + 8} in view.list_areas()


@pytest.mark.asyncio
async def test_a_view_goes_on_without_what_it_reads_of_home_assistant_when_that_fails(hass, monkeypatch):
    from custom_components.assist_improver import exploration

    async def failing(hass):
        raise RuntimeError("an integration unloaded mid-read")
    monkeypatch.setattr(exploration, "catalogue", failing)
    view = await exploration.session_view(hass, make_recording())
    assert view.setup.actions == {} and view.device_details(TARGET)["entity_id"] == TARGET


def test_a_devices_other_entities_page_too():
    from custom_components.assist_improver.exploration import PAGE
    record = make_recording()
    source = record["turns"][0]["source"]
    for n in range(PAGE + 3):  # One device with more entities than a page.
        source["entities"][f"sensor.part_{n:02}"] = {"entity_id": f"sensor.part_{n:02}", "device_id": "big"}
    view = SessionView(record)
    first, second = view.device_details("sensor.part_00"), view.device_details("sensor.part_00", page=2)
    assert len(first["same_physical_device"]) == PAGE and first["next_page"] == 2
    assert first["same_physical_device_total"] == second["same_physical_device_total"] == PAGE + 2
    assert len(second["same_physical_device"]) == 2 and "next_page" not in second


def test_readings_page_with_a_total():
    """Kai on readings: "what's the problem with 'too many'? ... give them a way to sift through the readings".
    Every reading is there to page through: per entity, how many, the first and the last, those with the most first;
    one entity's in time order; by turn or after a time; 25 to a page with the total."""
    from datetime import datetime, timedelta
    from custom_components.assist_improver.exploration import PAGE, ReadReadings
    record = make_recording()
    start = datetime.fromisoformat(record["turns"][0]["user"]["created"])
    for n in range(PAGE + 5):  # A sensor per entity, each reporting once; one reporting 40 times.
        record["state_changes"].append({"entity_id": f"sensor.level_{n:02}", "at": (start + timedelta(seconds=n)).isoformat(),
                                        "old": {"state": "1"}, "new": {"state": "2", "attributes": {"unit_of_measurement": "W"}}})
    for n in range(40):
        record["state_changes"].append({"entity_id": "sensor.power", "at": (start + timedelta(seconds=n)).isoformat(),
                                        "old": {"state": str(n)}, "new": {"state": str(n + 1), "attributes": {"unit_of_measurement": "W"}}})
    view = SessionView(record)
    tool = ReadReadings(view)
    first = tool.run()
    assert first["total"] == PAGE + 6 and first["readings_in_all"] == PAGE + 45 and len(first["entities"]) == PAGE
    assert first["entities"][0] == {"entity_id": "sensor.power", "readings": 40, "first": first["entities"][0]["first"],
                                    "last": first["entities"][0]["last"]}
    assert (first["entities"][0]["first"]["new"], first["entities"][0]["last"]["new"]) == ("1", "40")
    assert len(tool.run(page=2)["entities"]) == 6 and first["next_page"] == 2
    power = [tool.run(entity_id="sensor.power", page=p) for p in (1, 2)]
    assert power[0]["total"] == 40 and [r["new"] for r in power[0]["readings"] + power[1]["readings"]] == [
        str(n + 1) for n in range(40)]
    later = tool.run(entity_id="sensor.power", after=(start + timedelta(seconds=29.5)).isoformat())
    assert [r["new"] for r in later["readings"]] == [str(n + 1) for n in range(30, 40)]
    by_turn = [tool.run(turn=t)["readings_in_all"] for t in range(len(view.turns) + 1)]
    assert sum(by_turn) == PAGE + 45 and 0 < by_turn[1] < PAGE + 45
    assert "error" in tool.run(after="yesterday")


class Transport:
    """SDK-shaped scripted OpenRouter model: replies in order. Never builds a real client."""
    def __init__(self, messages):
        self.messages, self.requests = list(messages), []
        self.chat = SimpleNamespace(completions=self)

    async def create(self, **kwargs):
        self.requests.append(json.loads(json.dumps(kwargs, default=str)))
        return completion(self.messages.pop(0), kwargs.get("stream"))


@pytest.mark.asyncio
async def test_openrouter_ai_task_runs_the_session_tools(recording_hass, effect_guards):
    hass = recording_hass
    view = SessionView(make_recording())
    reads_turn_2 = SimpleNamespace(role="assistant", content=None, tool_calls=[SimpleNamespace(
        type="function", id="call-1", function=SimpleNamespace(name="read_turn", arguments=json.dumps({"turn": 2})))])
    answer = SimpleNamespace(role="assistant", content=json.dumps(ANALYSIS), tool_calls=None)
    transport = Transport([reads_turn_2, answer])
    entity = OpenRouterAITaskEntity(SimpleNamespace(entry_id="router", runtime_data=transport),
                                    SimpleNamespace(subentry_id="task", title="Router task", data={"model": "fixture/model"}))
    entity.hass, entity.entity_id = hass, "ai_task.router"
    entity.async_write_ha_state = lambda: None
    entity.platform = SimpleNamespace(domain="open_router")
    stand_in(hass, lambda e: entity if e == "ai_task.router" else None)
    run = await run_role(hass, role=analyzer.ROLE, entity_id="ai_task.router", instructions=analyzer.INSTRUCTIONS,
                         payload=view.overview(), structure=analyzer.STRUCTURE, llm_api=SessionAPI(hass, view),
                         allow_paid_inference=True)
    assert run["status"] == "completed", run.get("error")
    first, second = transport.requests
    offered = {t["function"]["name"] for t in first["extra_body"]["tools"]}
    assert {"read_turn", "devices_in_area", "find_devices", "device_details"} <= offered
    assert first["response_format"]["type"] == "json_schema"
    returned = next(m for m in second["messages"] if m["role"] == "tool")
    assert json.loads(returned["content"])["data"]["turn"] == 2  # HA ran our tool and gave the model turn 2.
    assert effect_guards == []


def test_changes_say_who_made_them_and_what_attributes_changed():
    record = make_recording()
    view = SessionView(record)
    changes = view.device_details(TARGET)["changes_during_session"]
    assert changes and all(c["by"] == "assistant" for c in changes)  # Assist's own tool results name the relay.
    outside = dict(record["state_changes"][0])
    outside = {**outside, "at": record["turns"][-1]["user"]["created"],
               "new": {**outside["new"], "context": {"id": "x", "parent_id": None, "user_id": "someone"}}}
    record["state_changes"].append(outside)
    later = SessionView(record).device_details(TARGET)["changes_during_session"][-1]
    assert later["by"] in ("assistant", "a person (signed in to Home Assistant)")


def test_cards_say_when_a_state_is_from_and_turns_can_be_read_together():
    view = SessionView(make_recording())
    card = view.devices_in_area("Fixture room")["devices"][0]
    assert "state_at_start" in card and "state" not in card
    assert [t["turn"] for t in view.read_turns([1, 2])["turns"]] == [1, 2]
    assert view.find_devices("fixture relays")["devices"]  # Plurals match.


def test_internal_recorder_notes_stay_out_of_the_overview():
    record = make_recording()
    record["limitations"] = ["provider_native_history_omitted; replay adapter must check compatibility", "other"]
    assert SessionView(record).overview()["limitations"] == ["other"]


def test_locally_handled_turns_are_read_for_analysis_but_not_replay():
    """HA can handle a request itself and delete the conversation before the recorder sees it again."""
    from custom_components.assist_improver.cases import CaseError, recording_turns
    record = make_recording()
    content = record["chat_log"]["content"]
    record["events"] = ([{"type": "created", "data": {"chat_log": {"content": content[:1]}}}]
                        + [{"type": "content_added", "data": {"content": m}} for m in content[1:]])
    record["chat_log"] = {**record["chat_log"], "content": content[:1]}  # The stale final copy.
    for turn in record["turns"]:
        turn.pop("preparations", None), turn.pop("prepared", None)
    turns = recording_turns(record, replay=False)
    assert len(turns) == len(record["turns"]) and all(t["handled_locally"] for t in turns)
    assert SessionView(record).read_turn(1)["handled_by"].startswith("Home Assistant itself")
    with pytest.raises(CaseError):
        recording_turns(record)


@pytest.mark.asyncio
async def test_what_an_entity_can_do_comes_from_home_assistants_own_actions(hass):
    """A card's operations are the actions Home Assistant describes for the entity (helpers.service's public
    descriptions), by its domain and supported features, with the fields that apply to it."""
    from homeassistant.setup import async_setup_component
    from conftest import load_registries
    from custom_components.assist_improver.actions import find_actions, operations
    from custom_components.assist_improver.exploration import PAGE, Setup
    await load_registries(hass)
    for component in ("homeassistant", "cover", "light"):
        assert await async_setup_component(hass, component, {})
    setup = await Setup.read(hass)
    blind = {"state": "open", "attributes": {"supported_features": 3}}  # Opens and closes; no position.
    assert {o["action"] for o in operations(setup.actions, "cover.blind", {}, blind)} == {
        "cover.open_cover", "cover.close_cover", "cover.toggle"}
    lamp = {"state": "on", "attributes": {"supported_features": 0, "supported_color_modes": ["brightness"]}}
    (turn_on,) = [o for o in operations(setup.actions, "light.lamp", {}, lamp) if o["action"] == "light.turn_on"]
    assert "brightness_pct" in turn_on["fields"] and "transition" not in turn_on["fields"]  # Not a feature it has.
    assert {"cover", "light"} <= setup.controls and setup.actions["light.turn_on"]["name"] == "Turn on light"
    found = find_actions(setup.actions, "brightness")  # In a field's words too, and paged.
    assert "light.turn_on" in [a["action"] for a in found["actions"]] and found["total"] >= 1 and found["page"] == 1
    every = find_actions(setup.actions, "")
    assert every["total"] == len(setup.actions) > PAGE and len(every["actions"]) == PAGE and every["next_page"] == 2
    assert find_actions(setup.actions, "", page=0)["next_page"] == 2  # A page below the first is the first.


def test_definitions_of_scripts_automations_and_scenes_read_from_the_view():
    """read_definition, for every role: a script, automation or scene as Home Assistant held it when the view's home
    was read (the recording's moment for the analyzer; today's setup for the proposer and reviewer)."""
    from custom_components.assist_improver.exploration import ReadDefinition
    record = make_recording()
    source = record["turns"][0]["source"]
    source["scripts"] = {SCRIPT.split(".", 1)[1]: {"sequence": [{"action": "switch.turn_off"}]}}
    source["automations"] = [{"id": "17", "alias": "Night", "triggers": [{"trigger": "sun", "event": "sunset"}]}]
    source["scenes"] = [{"id": "9", "name": "Evening", "entities": {TARGET: {"state": "on"}}}]
    source["states"]["automation.night"] = {"state": "on", "attributes": {"id": "17", "friendly_name": "Night"}}
    source["states"]["scene.evening"] = {"state": "unknown", "attributes": {"friendly_name": "Evening"}}
    tool = ReadDefinition(SessionView(record))
    assert tool.run(SCRIPT)["definition"]["sequence"][0]["action"] == "switch.turn_off"
    assert tool.run("automation.night")["definition"]["alias"] == "Night"
    assert tool.run("scene.evening")["definition"]["entities"] == {TARGET: {"state": "on"}}  # By its name.
    assert "error" in tool.run("script.nope") and "error" in tool.run(TARGET)


def test_the_assistant_as_the_session_recorded_it():
    """read_assistant, for the analyzer: every option of the assistant's settings, the tools Assist had and the voice
    assistant, as the session recorded them."""
    from recordings import prepared_recording
    from custom_components.assist_improver.exploration import TOOLS
    record = prepared_recording()
    record["turns"][0]["pipeline"] = {"name": "Home", "language": "en"}
    found = SessionView(record).assistant_setup()
    assert found["options"] == record["turns"][0]["prepared"]["provider_config"][0]["options"]
    assert found["tools"] == record["turns"][0]["prepared"]["tools"] and found["pipeline"]["name"] == "Home"
    assert "read_assistant" in {t.name for t in TOOLS}
