"""Trace digest: deterministic, and complete for what labels depend on."""

from copy import deepcopy
import json

import pytest

from conftest import private
from recordings import make_recording, TARGET
from custom_components.assist_improver.cases import CaseError
from custom_components.assist_improver.digest import build_digest, label_coverage


def label_for(record, criteria_entity=TARGET):
    return {"recording_id": record["id"], "not_requests": [{"turn": 4, "what": "test utterance"}],
            "goals": [{"turn": 1, "summary": "Press", "kind": "sequence", "entity_id": criteria_entity,
                       "states": ["off", "on"]}]}


def test_digest_keeps_conversation_actions_changes_and_capabilities():
    record = make_recording()
    record["state_changes"].append({"entity_id": "sensor.noise", "at": record["state_changes"][0]["at"],
                                    "old": {"state": "1"}, "new": {"state": "2", "attributes": {
                                        "unit_of_measurement": "dB"}}})  # A reading: listed last.
    record["state_changes"].append({"entity_id": TARGET, "at": record["state_changes"][0]["at"],
                                    "old": {"state": "on"}, "new": {"state": "unavailable"}})
    original = deepcopy(record)
    digest = build_digest(record)
    assert record == original
    assert [t["user_says"] for t in digest["turns"]] == [
        "Toggle the fixture relay.", "Use the press script.", "Turn it back on.", "Again, off then on."]
    assert digest["turns"][0]["assistant"][0] == {"calls": "intent__HassTurnOff",
                                                  "args": {"name": "Fixture relay", "domain": ["switch"]}}
    (relay,) = digest["relevant_entities"]
    assert relay["entity_id"] == TARGET and relay["area"] == "Fixture room"
    assert [(c["during_turn"], c["new"]) for c in relay["changes_during_session"]] == [
        (1, "off"), (3, "on"), (4, "off"), (4, "on"), (1, "unavailable")]  # Going unavailable is kept too.
    assert [(r["entity_id"], r["readings"]) for r in digest["readings"]] == [("sensor.noise", 1)]
    assert {t["name"] for t in digest["capabilities"]["assist_tools"]} == {
        "intent__HassTurnOn", "intent__HassTurnOff", "homeassistant__GetLiveContext"}
    assert "Frozen instructions" not in json.dumps(digest)  # The system prompt is not copied.


def test_label_coverage_reports_what_is_missing():
    record = make_recording(); digest = build_digest(record)
    assert label_coverage(digest, label_for(record)) == []
    assert label_coverage(digest, label_for(record, "switch.not_recorded")) == ["switch.not_recorded"]
    other = label_for(record); other["goals"][0]["turn"] = 6
    assert label_coverage(digest, other) == ["turn 6"]
    noted = label_for(record); noted["goals"][0]["turn"] = len(digest["turns"]) + 1  # The note sent with it.
    assert label_coverage(digest, {**noted, "note": "Say less"}) == []  # Its last turn, for the analyzer.
    assert label_coverage(digest, noted) == [f"turn {len(digest['turns']) + 1}"]
    with pytest.raises(CaseError):
        label_coverage(digest, {**label_for(record), "recording_id": "another"})


def test_long_tool_results_are_clipped_not_dropped():
    record = make_recording()
    result = next(m for m in record["chat_log"]["content"] if m["role"] == "tool_result")
    result["result"] = {"data": "x" * 20_000}
    step = next(s for s in build_digest(record)["turns"][0]["assistant"] if "result" in s)
    assert len(step["result"]) < 7_000 and "more characters omitted" in step["result"]


def test_the_households_recordings_cover_their_labels():
    folder = private()
    recordings = sorted(private("recordings").glob("*.json"))
    assert recordings
    for path in recordings:
        digest = build_digest(json.loads(path.read_text())["data"])
        found = [f for f in sorted(folder.glob(f"labels*/{path.name}"))]
        if found:  # Some recordings were left unlabelled (a premise that didn't hold).
            assert label_coverage(digest, json.loads(found[0].read_text())) == [], path.name


def test_a_card_carries_the_state_and_registry_entries_whole():
    """A detailed card carries the state as Home Assistant writes it and the entity's and its device's registry
    entries, invented fields too, each without its empty fields; a child device's maker and model are its parent's."""
    from custom_components.assist_improver.digest import entity_card
    state = {"entity_id": "sensor.power", "state": "1234.5678", "last_changed": "2026-10-30T14:00:00+00:00",
             "attributes": {"unit_of_measurement": "W", "invented": 1}}
    source = {"states": {"sensor.power": state},
              "entities": {"sensor.power": {"entity_id": "sensor.power", "device_id": "port", "name": None,
                                            "aliases": [], "entity_category": "diagnostic", "invented": "kept"}},
              "devices": {"hub": {"id": "hub", "manufacturer": "Maker", "model": "Hub 1", "name": "Hub"},
                          "port": {"id": "port", "parent_device_id": "hub", "name": "Port 1", "labels": []}}}
    card = entity_card(source, "sensor.power", detailed=True)
    assert card["state"] == state and card["entity_category"] == "diagnostic"
    assert card["entity"] == {"device_id": "port", "entity_category": "diagnostic", "invented": "kept"}
    assert card["device"] == {"id": "port", "parent_device_id": "hub", "name": "Port 1", "manufacturer": "Maker",
                              "model": "Hub 1"}
    assert entity_card(source, "sensor.power")["state"] == "1234.5678"  # A listing's card: the state alone.
