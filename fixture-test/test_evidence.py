"""Facts from HA's own engine about a hypothesis: implementation, regressions, capabilities."""

import pytest

from recordings import SCRIPT, TARGET, make_recording, recorded_today, relay_renamed
from custom_components.assist_improver.evidence import Evidence, capabilities, replay_calls
from custom_components.assist_improver.exploration import SessionView
from custom_components.assist_improver.hypotheses import Sessions

# Today's setup: the home as the conversation found it, with the household's press script.
TODAY = recorded_today(make_recording(), scripts={"fixture_press": {"alias": "Fixture press", "sequence": [
    {"action": "switch.turn_off", "target": {"entity_id": TARGET}}, {"action": "switch.turn_on", "target": {"entity_id": TARGET}}]}},
    automations=[])


def sessions():
    record = make_recording()
    analysis = {"recording_id": record["id"], "extra_turns": [2, 3], "goals": [
        {"turn": 1, "summary": "Press the relay", "kind": "sequence", "entity_id": TARGET, "states": ["off", "on"]}]}
    return Sessions([analysis], [SessionView(record)]), record


def hypothesis(*changes):
    return {"title": "t", "cause": "c", "expected": "e", "support": [], "conflicts": [],
            "goals": [{"session": "synthetic-recording", "goal_turn": 1}], "changes": list(changes)}


def script(object_id, *sequence, alias=None):
    """A new script, in Home Assistant's own format."""
    return {"kind": "script", "id": object_id, "config": {"alias": alias or object_id, "sequence": list(sequence)}}


def act(service, entity_id=TARGET):
    return {"action": service, "target": {"entity_id": entity_id}}


PRESS = script("press_the_relay", act("switch.turn_off"), act("switch.turn_on"), {"stop": "Pressed."},
               alias="Press the relay")


@pytest.mark.asyncio
async def test_a_script_is_run_from_the_goals_starting_state(hass, effect_guards):
    found, _ = sessions()
    facts = await Evidence(found, TODAY).implementation(hypothesis(PRESS), Evidence(found, TODAY).setup(hypothesis(PRESS)))
    alone = facts[0]["each_script_alone"][0]
    assert facts[0]["reaches_goal"] and facts[0]["reached_by"] == ["script.press_the_relay"]
    assert alone["loads"] and alone["goal_devices"][0]["went_through"] == ["off", "on"] and alone["runs"][0]["devices_changed"]
    wrong = script("press_the_relay", act("switch.turn_on"))  # Only "on": not a press.
    facts = await Evidence(found, TODAY).implementation(hypothesis(wrong), Evidence(found, TODAY).setup(hypothesis(wrong)))
    assert facts[0]["each_script_alone"][0]["runs"] and facts[0]["reaches_goal"] is False and effect_guards == []


@pytest.mark.asyncio
async def test_a_goals_device_whose_entity_id_changed_since_is_checked_under_its_new_one(hass, effect_guards):
    """Code's checks look devices up in today's setup (DETAILS P6.9): the relay's entity ID was changed since the
    conversation, so a script pressing it under its new one reaches the goal, which named the old one (P6.2.4)."""
    workshop = "switch.workshop_relay"
    record, home = relay_renamed(make_recording(), workshop)
    today = recorded_today(record, **home, scripts={}, automations=[])
    analysis = {"recording_id": record["id"], "extra_turns": [2, 3], "goals": [
        {"turn": 1, "summary": "Press the relay", "kind": "sequence", "entity_id": TARGET, "states": ["off", "on"]}]}
    found = Sessions([analysis], [SessionView(record)])
    press = script("press_the_relay", act("switch.turn_off", workshop), act("switch.turn_on", workshop))
    evidence = Evidence(found, today)
    facts = await evidence.implementation(hypothesis(press), evidence.setup(hypothesis(press)))
    assert facts[0]["reaches_goal"] and facts[0]["goal_devices_wanted"] == [
        {"entity_id": workshop, "states": ["off", "on"]}] and effect_guards == []


@pytest.mark.asyncio
async def test_the_capability_probe_gets_a_device_renamed_since_under_its_entity_id_today(monkeypatch):
    """What Assist can do with each device in open friction is tried in today's setup (DETAILS P6.9): the analysis
    names the relay by its entity ID then, changed since, so the probe tries it under its new one (P6.2.4)."""
    from custom_components.assist_improver import evidence
    from custom_components.assist_improver.proposal import as_today
    workshop = "switch.workshop_relay"
    record, home = relay_renamed(make_recording(), workshop)
    today = recorded_today(record, **home, scripts={}, automations=[])
    analysis = {"recording_id": record["id"], "extra_turns": [2, 3], "goals": [
        {"turn": 1, "summary": "Press the relay", "kind": "sequence", "entity_id": TARGET, "states": ["off", "on"]}]}
    found = Sessions([analysis], [SessionView(record)])
    found.household, found.today = SessionView(as_today(record, today["home"])), today  # As proposal.window has it.
    probed = []

    async def capabilities(recording, entity_ids, today):
        probed.append(entity_ids)
        return {}
    monkeypatch.setattr(evidence, "capabilities", capabilities)
    await found.load_evidence()
    assert probed == [[workshop]]
    assert [d["entity_id"] for d in found.devices_in_friction()] == [workshop]


@pytest.mark.asyncio
async def test_a_new_alias_is_matched_by_has_own_name_matching(hass, effect_guards):
    found, _ = sessions()
    evidence = Evidence(found, TODAY)
    alias = {"kind": "registry", "entity_id": TARGET, "aliases": ["Garage door"]}
    facts = await evidence.implementation(hypothesis(alias), evidence.setup(hypothesis(alias)))
    assert facts == [{"alias_for": TARGET, "matches": {"Garage door": [TARGET]}, "ambiguous": []}]


@pytest.mark.asyncio
async def test_recorded_calls_replay_and_changed_resolutions_are_signals(hass, effect_guards):
    found, record = sessions()
    calls = await replay_calls(record, TODAY)
    assert [c[1] for c in calls] == ["intent__HassTurnOff", "intent__HassTurnOn", "intent__HassTurnOff", "intent__HassTurnOn"]
    assert calls[0][3]["acted_on"] == [TARGET]
    evidence = Evidence(found, TODAY)
    hiding = hypothesis({"kind": "registry", "entity_id": TARGET, "expose": False})  # Hiding the relay breaks them.
    report = await evidence.check(hiding)
    changed = report["resolution_signals"]["calls_resolving_differently"]
    assert len(changed) == 4 and changed[0]["before"]["acted_on"] == [TARGET] and "error" in changed[0]["after"]
    same = await evidence.check(hypothesis({"kind": "preference", "text": "Be brief.", "for": "everyone"}))
    assert "note" in same["resolution_signals"] and effect_guards == []


@pytest.mark.asyncio
async def test_capabilities_come_from_what_ha_carries_out(hass, effect_guards):
    _, record = sessions()
    found = await capabilities(record, [TARGET, SCRIPT], TODAY)
    tools = {c["tool"] for c in found[TARGET]["assist_can"]}
    assert {"intent__HassTurnOn", "intent__HassTurnOff"} <= tools and found[TARGET]["exposed"]
    assert found[SCRIPT] == {"exposed": False, "assist_can": []} and effect_guards == []


@pytest.mark.asyncio
async def test_alternative_scripts_are_judged_alone_so_order_never_decides(hass, effect_guards):
    found, _ = sessions()
    on_only = script("a_relay_on", act("switch.turn_on"))
    evidence = Evidence(found, TODAY)
    both = hypothesis(PRESS, on_only)  # "A relay on" sorts first; alone, PRESS still reaches the goal.
    facts = await evidence.implementation(both, evidence.setup(both))
    assert facts[0]["reaches_goal"] and facts[0]["reached_by"] == ["script.press_the_relay"] and "together" not in facts[0]

