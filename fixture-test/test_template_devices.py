"""A template device as Home Assistant runs it: a template light whose own turn_on action switches a helper, as the
end-to-end suite's household lights are set up (tools/e2e/config/configuration.yaml). HA carries that action out under
Assist's request, so the recording shows the helper changing with the light. The light is the device Assist changed;
the helper is the light's own follow-on, not a change Assist made itself: the replay without a change compares the
light (DETAILS P6.6.1; the sandbox emulates the light as a device, P11.1), and the helper is no side effect left
(P6.1.10). The household is HA's own template and input_boolean integrations; the replay's model is scripted."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from homeassistant.const import EVENT_CALL_SERVICE
from homeassistant.core import Context, ServiceRegistry, callback

from conftest import household_user, load_registries
from kit import variant
from test_changeset import _original_async_call
from test_openrouter_replay import bind_openrouter, call, openrouter_recording, reply
from custom_components.assist_improver.analyzer import side_effects_left
from custom_components.assist_improver.exploration import SessionView
from custom_components.assist_improver.recorder import Actions
from custom_components.assist_improver.simulate import Trial, planned
from custom_components.assist_improver.snapshot import snapshot, state_record

DESK, CEILING = "light.desk_lamp", "light.ceiling_light"
POWER, CEILING_POWER = "input_boolean.desk_lamp_power", "input_boolean.ceiling_light_power"
SAID = "Turn on the desk lamp."
GOAL = {"key": "1:0", "label": 1, "turn": 1, "kind": "state", "entity_id": DESK, "state": "on", "serves": None}


def template_light(name, helper):
    """A template light that switches its helper, and shows the helper's state, as the end-to-end household's do."""
    return {"name": name, "unique_id": helper.split(".")[1], "state": f"{{{{ is_state('{helper}', 'on') }}}}",
            "turn_on": [{"action": "input_boolean.turn_on", "target": {"entity_id": helper}}],
            "turn_off": [{"action": "input_boolean.turn_off", "target": {"entity_id": helper}}]}


@pytest_asyncio.fixture
async def household(hass):
    """The desk lamp and the ceiling light, off, each a template light over a helper Assist isn't shown; the person
    speaking runs the home."""
    from homeassistant import loader
    from homeassistant.setup import async_setup_component
    hass.auth = SimpleNamespace(async_get_user=AsyncMock(return_value=household_user("Alex")))
    loader.async_setup(hass)
    await load_registries(hass)
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(hass, "input_boolean", {"input_boolean": {
        "desk_lamp_power": {"name": "Desk lamp power", "initial": False},
        "ceiling_light_power": {"name": "Ceiling light power", "initial": False}}})
    assert await async_setup_component(hass, "template", {"template": [{"light": [
        template_light("Desk lamp", POWER), template_light("Ceiling light", CEILING_POWER)]}]})
    await hass.async_start()
    await hass.async_block_till_done()
    return hass


async def turned_on(hass, context):
    """What Home Assistant does when Assist's request turns the desk lamp on, as the recorder keeps it: at home, so
    its services run (the light's own action among them), past the guard that keeps replays off the home. The changes,
    and the action calls under the request."""
    changes, actions, calls = [], [], Actions()
    calls.watch("recording", context.id, actions)
    # A callback, as the recorder's own listener is: in the loop, in order (a plain function runs in a thread).
    unsubscribe = [hass.bus.async_listen("state_changed", callback(lambda event: changes.append({
        "entity_id": event.data["entity_id"], "old": state_record(event.data["old_state"]),
        "new": state_record(event.data["new_state"])}))), hass.bus.async_listen(EVENT_CALL_SERVICE, calls.heard)]
    with patch.object(ServiceRegistry, "async_call", _original_async_call()):
        await hass.services.async_call("light", "turn_on", {"entity_id": DESK}, blocking=True, context=context)
        await hass.async_block_till_done()
    for stop in unsubscribe:
        stop()
    return changes, actions


async def recorded(hass):
    """"Turn on the desk lamp.", which Assist did with HA's own intent, through the household's OpenRouter assistant:
    the home as the recorder snapshots it, and the changes HA made under the request."""
    record = variant(openrouter_recording(), "template-light", SAID, turns=1)
    first = record["turns"][0]
    home = snapshot(hass)
    lamps = (DESK, CEILING, POWER, CEILING_POWER)
    for key in ("states", "entities"):
        first["source"][key] = {e: home[key][e] for e in lamps}
    first["source"]["exposure"] = {DESK: True, CEILING: True, POWER: False, CEILING_POWER: False}
    asked = next(m for m in record["chat_log"]["content"] if m.get("tool_calls"))
    asked["tool_calls"][0].update(tool_name="intent__HassTurnOn", tool_args={"name": "Desk lamp", "domain": ["light"]})
    result = next(m for m in record["chat_log"]["content"] if m["role"] == "tool_result")
    result["tool_name"] = "intent__HassTurnOn"
    result["result"]["data"]["data"]["success"] = [{"name": "Desk lamp", "type": "entity", "id": DESK}]
    [relay] = record["state_changes"]  # When the fixture's Assist acted: HA's changes are stamped then, in order.
    changes, first["actions"] = await turned_on(hass, Context(**first["prepared"]["context"]["context"]))
    record["state_changes"] = [{**change, "at": relay["at"].replace("+00:00", f".{n:06d}+00:00")}
                               for n, change in enumerate(changes, 1)]
    return record


@pytest.mark.asyncio
async def test_the_recorder_keeps_the_calls_under_the_request_each_inside_the_one_it_was_made_in(household):
    """Follow-ons are found by context (DETAILS P8.2.10): the light's own action, which HA runs in the same context
    as Assist's call to the light, is kept inside it; then the helper, and the light, which follows it, change."""
    context = Context(id="fixture-context-1")
    changes, actions = await turned_on(household, context)
    assert [(a["action"], a["data"], a["context"]["id"], a.get("under")) for a in actions] == [
        ("light.turn_on", {"entity_id": DESK}, context.id, None),
        ("input_boolean.turn_on", {"entity_id": [POWER]}, context.id, 0)]
    assert [(c["entity_id"], c["old"]["state"], c["new"]["state"], c["new"]["context"]["id"]) for c in changes] == [
        (POWER, "off", "on", context.id), (DESK, "off", "on", context.id)]


@pytest.mark.parametrize("lamp, status", [("Desk lamp", "reproduced"), ("Ceiling light", "not_reproducible")])
@pytest.mark.asyncio
async def test_the_before_reproduces_the_template_light_assist_turned_on_and_no_other(household, effect_guards, lamp,
                                                                                    status):
    """The replay turns the desk lamp on as the recorded Assist did: it reproduces, though the sandbox, emulating the
    light as a device, never switches the light's helper. Turning the ceiling light on instead doesn't."""
    record = await recorded(household)
    await bind_openrouter(household, record, [call("intent__HassTurnOn", {"name": lamp, "domain": ["light"]}, "c1"),
                                              reply(f"Turned on the {lamp}.")])
    original = deepcopy(record)
    plan, today = await planned(household, record)  # Today's setup: the household's Home Assistant, as it is.
    found = await Trial(household, plan, record, today).run()
    [step] = found["steps"]
    assert found["status"] == status, (found["error"], step)
    assert step["recorded_transitions"] == [{"entity_id": DESK, "old": "off", "new": "on"}]
    assert step["differences_elsewhere"] == ({} if lamp == "Desk lamp" else {
        CEILING: {"replay": [["off", "on"]], "recorded": []}})
    assert record == original and effect_guards == []


@pytest.mark.asyncio
async def test_a_template_lights_helper_is_its_own_follow_on_not_a_side_effect_left(household):
    """The conversation went as asked: the desk lamp on, nothing else Assist changed. A helper Assist switched itself,
    with no template light changed, still is a side effect left."""
    record = await recorded(household)
    view = SessionView(record)
    assert [c["by"] for c in view.changes[POWER]] == ["follow-on of an assistant change on the same physical device"]
    assert [c["by"] for c in view.changes[DESK]] == ["assistant"]
    assert side_effects_left(view, [GOAL]) == []
    record["state_changes"] = [c for c in record["state_changes"] if c["entity_id"] == POWER]
    helper = record["turns"][0]["actions"][1]
    record["turns"][0]["actions"] = [{k: v for k, v in helper.items() if k != "under"}]  # Assist's own call.
    assert side_effects_left(SessionView(record), [GOAL]) == [
        {"entity_id": POWER, "start": "off", "end": "on", "turn": 1}]
