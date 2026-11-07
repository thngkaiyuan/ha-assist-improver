"""A state goal's target State (targets.py, DETAILS P9.7.1): a state plus any settable attribute, valid as Home
Assistant has it, met in any of its representations, numbers within tolerance."""

from types import SimpleNamespace

from custom_components.assist_improver import analyzer, targets

LAMP, THERMOSTAT, MODE = "light.desk_lamp", "climate.hallway", "select.dryer_program"
LAMP_SHOWS = {"friendly_name": "Desk lamp", "supported_color_modes": ["hs", "color_temp"], "color_mode": "color_temp",
              "color_temp_kelvin": 2700, "brightness": 77, "min_color_temp_kelvin": 2000,
              "max_color_temp_kelvin": 6500}
THERMOSTAT_SHOWS = {"friendly_name": "Hallway", "hvac_modes": ["off", "heat"], "min_temp": 7, "max_temp": 35,
                    "temperature": 18, "fan_modes": ["auto", "low"], "fan_mode": "auto",
                    "supported_features": 9}  # ClimateEntityFeature: a target temperature, a fan mode.


def home(*changes, lamp="on"):
    """The lamp, the thermostat and a select, and each change Assist made in turn 1 (entity, new state, attributes)."""
    states = {LAMP: {"state": lamp, "attributes": LAMP_SHOWS}, THERMOSTAT: {"state": "heat", "attributes": THERMOSTAT_SHOWS},
              MODE: {"state": "cotton", "attributes": {"options": ["cotton", "wool"]}}}
    found = {}
    for n, (entity, new, attributes) in enumerate(changes):
        was = states[entity]["attributes"]
        found.setdefault(entity, []).append({
            "at": f"2026-01-01T09:00:{n:02d}+00:00", "during_turn": 1, "old": states[entity]["state"], "new": new,
            "by": "assistant", "attributes_changed": {k: [was.get(k), v] for k, v in attributes.items()}})
    return SimpleNamespace(source={"states": states}, changes=found)


def goal(entity, state, **attributes):
    return {"turn": 1, "summary": "", "kind": "state", "entity_id": entity, "state": state,
            **({"attributes": attributes} if attributes else {})}


def test_a_colour_asked_as_rgb_is_met_by_the_light_showing_it_as_hue_and_saturation():
    red = home((LAMP, "on", {"color_mode": "hs", "hs_color": [0.0, 100.0], "rgb_color": None}))
    assert analyzer.reached(red, goal(LAMP, "on", rgb_color=[255, 0, 0])) is True
    green = home((LAMP, "on", {"color_mode": "hs", "hs_color": [120.0, 100.0]}))
    assert analyzer.reached(green, goal(LAMP, "on", rgb_color=[255, 0, 0])) is False
    named = home((LAMP, "on", {"color_mode": "xy", "xy_color": [0.701, 0.299]}))  # Red, as xy: Home Assistant's own.
    assert analyzer.reached(named, goal(LAMP, "on", color_name="red")) is True


def test_white_is_one_colour_whatever_hue_home_assistant_gives_it():
    """A bulb's own white, xy (0.3227, 0.329), Home Assistant shows as hue 240 at under 1% saturation: it's white, as
    is the D65 white point; a cool colour temperature is near white on a colour light; a pale blue isn't white."""
    for xy in ([0.3227, 0.329], [0.3127, 0.329], [0.32, 0.33]):
        white = home((LAMP, "on", {"color_mode": "xy", "xy_color": xy, "hs_color": None}))
        for asked in ({"rgb_color": [255, 255, 255]}, {"color_name": "white"}, {"hs_color": [0, 0]}):
            assert analyzer.reached(white, goal(LAMP, "on", **asked)) is True, (xy, asked)
    cool = home((LAMP, "on", {"color_mode": "hs", "hs_color": [240.0, 0.4]}))
    assert analyzer.reached(cool, goal(LAMP, "on", color_temp_kelvin=6500)) is True
    pale = home((LAMP, "on", {"color_mode": "hs", "hs_color": [220.0, 20.0]}))
    assert analyzer.reached(pale, goal(LAMP, "on", color_name="white")) is False


def test_a_brightness_as_a_percentage_and_as_home_assistants_0_to_255_are_one_setting():
    half = home((LAMP, "on", {"brightness": 128}))
    assert analyzer.reached(half, goal(LAMP, "on", brightness_pct=50)) is True
    assert analyzer.reached(half, goal(LAMP, "on", brightness=128)) is True
    assert analyzer.reached(half, goal(LAMP, "on", brightness_pct=60)) is False
    stored = {**goal(LAMP, "on"), "brightness_pct": 50}  # A stored analysis's goal reads unchanged.
    assert analyzer.reached(half, stored) is True and analyzer.reached(half, {**stored, "brightness_pct": 60}) is False


def test_a_thermostats_target_temperature_is_met_within_tolerance_of_its_range_and_not_outside():
    """5% of the range the thermostat shows (7 to 35: 1.4 degrees), in its own unit."""
    set_to = lambda degrees: home((THERMOSTAT, "heat", {"temperature": degrees}))  # noqa: E731
    assert analyzer.reached(set_to(21), goal(THERMOSTAT, "heat", temperature=22)) is True
    assert analyzer.reached(set_to(20), goal(THERMOSTAT, "heat", temperature=22)) is False
    assert analyzer.reached(set_to(22), goal(THERMOSTAT, "heat", temperature=22, fan_mode="low")) is False  # Unset.


def test_a_number_is_met_within_the_devices_own_step_where_its_coarser():
    """A three-speed fan asked for 50% goes to its nearest speed, which Home Assistant shows as 67: that's 50% for this
    fan (its percentage_step), not two speeds off."""
    fan = "fan.bedroom"
    shows = {"percentage": 0, "percentage_step": 100 / 3, "preset_modes": None}
    view = SimpleNamespace(source={"states": {fan: {"state": "off", "attributes": shows}}}, changes={fan: [
        {"at": "2026-01-01T09:00:00+00:00", "during_turn": 1, "old": "off", "new": "on", "by": "assistant",
         "attributes_changed": {"percentage": [0, 67]}}]})
    assert analyzer.reached(view, goal(fan, "on", percentage=50)) is True
    assert analyzer.reached(view, goal(fan, "on", percentage=30)) is False  # A speed away.


def test_a_numbers_value_is_its_state_compared_as_a_number_within_the_min_and_max_it_shows():
    """A number's value is its state, which Home Assistant keeps as a float's text (30.0): a goal of 30 is met by it,
    within 5% of the min and max it shows; its value is no setting, and a state outside them, or no number, is
    refused."""
    from homeassistant.components.number import NumberEntity
    dryer = "number.dryer_minutes"

    class Dryer(NumberEntity):
        _attr_native_value = 30.0  # As number.set_value hands it: its schema makes it a float.
    shows = {"min": 0.0, "max": 120.0, "step": 1.0, "mode": "box"}
    view = SimpleNamespace(source={"states": {dryer: {"state": "20.0", "attributes": shows}}}, changes={dryer: [
        {"at": "2026-01-01T09:00:00+00:00", "during_turn": 1, "old": "20.0", "new": str(Dryer().state),
         "by": "assistant"}]})
    assert [analyzer.reached(view, goal(dryer, s)) for s in ("30", "35", "40", "20")] == [True, True, False, False]
    view.digest, view._entities = {"turns": [{"turn": 1, "user_says": "x", "assistant": []}]}, lambda: [dryer]
    asking = lambda g: analyzer.problems(view, {"goals": [g], "not_requests": [], "question_for_user": ""})  # noqa: E731
    assert asking(goal(dryer, "30")) == []
    for state in ("500", "long"):
        assert asking(goal(dryer, state)) == [f"Goal 1 (turn 1): {dryer} has no state {state!r}; it takes 0.0 to 120.0."]
    assert "has no setting 'value'" in asking(goal(dryer, "30.0", value=30))[0]


def test_another_setting_moving_neither_reaches_nor_undoes_a_goal():
    later = home((THERMOSTAT, "heat", {"temperature": 22}), (THERMOSTAT, "heat", {"current_temperature": 19}))
    later.changes[THERMOSTAT][1]["by"] = "not the assistant"
    assert analyzer.reached(later, goal(THERMOSTAT, "heat", temperature=22)) is True


def test_a_state_or_setting_the_device_cant_have_is_refused_with_the_ones_it_can():
    view = home()
    view.digest, view._entities = {"turns": [{"turn": 1, "user_says": "x", "assistant": []}]}, lambda: list(
        view.source["states"])
    asking = lambda g: analyzer.problems(view, {"goals": [g], "not_requests": [], "question_for_user": ""})  # noqa: E731
    assert asking(goal(MODE, "silk")) == [
        "Goal 1 (turn 1): select.dryer_program has no state 'silk'; its states are cotton, wool."]
    assert asking(goal(THERMOSTAT, "heat", fan_mode="high")) == [
        "Goal 1 (turn 1): climate.hallway's fan_mode can't be 'high'; it takes auto, low."]
    assert asking(goal(THERMOSTAT, "heat", temperature=40)) == [
        "Goal 1 (turn 1): climate.hallway's temperature can't be 40; it takes 7 to 35."]
    assert asking(goal(THERMOSTAT, "cool")) == [
        "Goal 1 (turn 1): climate.hallway has no state 'cool'; its states are heat, off."]
    assert asking(goal(LAMP, "on", brightness_pct=150))[0].endswith("brightness_pct can't be 150; it takes 0 to 100.")
    assert "has no setting 'transition'" in asking(goal(LAMP, "on", transition=2))[0]  # A field, but no attribute.
    assert asking(goal(LAMP, "on", rgb_color=[255, 0, 0], brightness_pct=40)) == []


def test_a_setting_the_device_cant_take_is_refused_as_its_services_describe_it():
    """A setting is one the entity takes: a field of its domain's services whose filters it passes, as Home Assistant's
    services.yaml gives them (its supported features, a light's colour modes). A light that only switches takes no
    brightness, which Home Assistant itself drops for it; a radiator with a target temperature alone takes no fan mode
    or range."""
    porch, blind, radiator = "light.porch", "cover.study_blind", "climate.radiator"
    states = {porch: {"state": "off", "attributes": {"supported_color_modes": ["onoff"]}},
              "light.hall": {"state": "off", "attributes": {"supported_color_modes": ["brightness"]}},
              "light.bulb": {"state": "on", "attributes": {"supported_color_modes": ["color_temp", "white"],
                                                           "color_mode": "white", "brightness": 200}},
              blind: {"state": "open", "attributes": {"supported_features": 3}},  # Open and close, no position.
              radiator: {"state": "heat", "attributes": {"hvac_modes": ["off", "heat"], "supported_features": 1}}}
    view = SimpleNamespace(source={"states": states}, changes={}, digest={"turns": [{"turn": 1, "user_says": "x"}]},
                           _entities=lambda: list(states))
    asking = lambda g: analyzer.problems(view, {"goals": [g], "not_requests": [], "question_for_user": ""})  # noqa: E731
    assert asking(goal(porch, "on", brightness_pct=50)) == [
        "Goal 1 (turn 1): light.porch has no setting 'brightness_pct'; its settings are none."]
    assert "has no setting 'rgb_color'" in asking(goal("light.hall", "on", rgb_color=[255, 0, 0]))[0]
    assert asking(goal("light.hall", "on", brightness_pct=50)) == []
    assert "has no setting 'position'" in asking(goal(blind, "open", position=50))[0]
    assert "has no setting 'fan_mode'" in asking(goal(radiator, "heat", fan_mode="high"))[0]
    assert "has no setting 'target_temp_low'" in asking(goal(radiator, "heat", target_temp_low=18))[0]
    assert asking(goal(radiator, "heat", temperature=21)) == []
    assert asking(goal("light.bulb", "on", white=True)) == []  # Its white channel: white, as it shows.
    assert analyzer.reached(view, goal("light.bulb", "on", white=True)) is True
    assert analyzer.reached(view, goal("light.bulb", "on", color_name="white")) is True


def test_valid_states_and_settings_come_from_home_assistant_not_a_copy_of_its_lists():
    for name in ("STATES", "OPTION_ATTRIBUTES", "allowed_states", "BRIGHTNESS_TOLERANCE"):
        assert not hasattr(analyzer, name), name
    assert targets.states({"states": {}}, "cover.garage_door") >= {"open", "closed", "opening", "closing"}
    assert targets.states({"states": {}}, "switch.kettle") == {"on", "off"}
    assert {"brightness", "rgb_color", "color_temp_kelvin"} <= set(targets.settable("light"))
    assert "transition" not in targets.settable("light")  # Its service takes it; no light holds it.
