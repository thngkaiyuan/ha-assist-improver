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


def test_a_setting_is_a_field_an_action_sets_it_with_not_one_another_action_takes():
    """DETAILS P9.7.1: a setting is an attribute a field of one of its domain's actions sets. A fan's increase_speed
    steps its speed by percentage_step, which Home Assistant's fan carries out as a set_percentage: percentage_step is
    no setting, though the fan shows one, so a goal naming it is refused and a recorded call stepping by it sets
    nothing the before compares. Nor is a field naming which device an action uses (a speech engine's say: which
    speaker). A setting an action sets under another name is one (a speaker's volume_set: its volume_level; its
    play_media: the media it plays)."""
    from custom_components.assist_improver.simulation import assist_settings
    fan = "fan.bedroom"
    source = {"states": {fan: {"state": "on", "attributes": {"percentage": 33, "percentage_step": 100 / 3,
                                                               "supported_features": 1}}}}  # It sets a speed.
    assert targets.problems(source, fan, "on", {"percentage_step": 50}) == [
        f"{fan} has no setting 'percentage_step'; its settings are percentage."]
    assert targets.problems(source, fan, "on", {"percentage": 67}) == []
    stepped = {"number": 1, "source": source, "actions": [{"action": "fan.increase_speed",
                                                           "data": {"entity_id": fan, "percentage_step": 50}}]}
    assert assist_settings({"turns": [stepped]}, stepped) == {}
    assert "entity_id" not in targets.settable("tts")
    assert {"volume_level", "is_volume_muted", "media_content_id", "media_content_type"} <= set(
        targets.settable("media_player"))


def test_a_setting_is_the_attribute_its_device_reports_whatever_its_field_or_property_is_called():
    """A setting is the attribute Home Assistant's entity class reports for the field that sets it (its own enums of
    them name them), whatever property the class reads it from or field sets it: a fan's direction (read from
    current_direction), a remote's activity (shown as current_activity, its options as activity_list). For every domain
    the stand-in stands in for, each field naming an attribute its class reports is one its devices hold, unless it
    sets the state (a water heater's operation mode), and none names one its class reports under a longer name
    (current_<field>) that standin.FIELDS doesn't map it to."""
    from homeassistant.components.fan import FanEntityFeature
    from homeassistant.components.remote import RemoteEntityFeature
    from custom_components.assist_improver import standin
    for domain in standin._table():
        reported = standin.reports(domain)
        for fields in targets._services(domain).values():
            for field, spec in fields.items():
                if (name := standin.attribute_of(domain, field)) in reported:
                    assert standin.held(domain, name) or targets._sets_state(spec), (domain, field)
                else:
                    assert not [a for a in reported if a.endswith(f"_{field}")], (domain, field)
    fan, remote = "fan.ceiling", "remote.living_room"
    states = {fan: {"state": "on", "attributes": {"direction": "forward",
                                                  "supported_features": FanEntityFeature.DIRECTION}},
              "fan.desk": {"state": "on", "attributes": {"supported_features": FanEntityFeature.SET_SPEED}},
              remote: {"state": "on", "attributes": {"current_activity": "Music", "activity_list": ["Music", "Watch TV"],
                                                     "supported_features": RemoteEntityFeature.ACTIVITY}}}
    view = SimpleNamespace(source={"states": states}, changes={e: [
        {"at": "2026-01-01T09:00:00+00:00", "during_turn": 1, "old": "on", "new": "on", "by": "assistant",
         "attributes_changed": {name: [states[e]["attributes"][name], value]}}]
        for e, name, value in ((fan, "direction", "reverse"), (remote, "current_activity", "Watch TV"))})
    assert {"direction"} <= set(targets.settable("fan")) and {"current_activity"} <= set(targets.settable("remote"))
    assert targets.problems(view.source, fan, "on", {"direction": "reverse"}) == []
    assert "has no setting 'direction'" in targets.problems(view.source, "fan.desk", "on", {"direction": "reverse"})[0]
    assert analyzer.reached(view, goal(fan, "on", direction="reverse")) is True
    assert analyzer.reached(view, goal(fan, "on", direction="forward")) is False
    assert targets.problems(view.source, remote, "on", {"activity": "Watch TV"}) == []
    assert targets.problems(view.source, remote, "on", {"activity": "Gaming"}) == [
        f"{remote}'s activity can't be 'Gaming'; it takes Music, Watch TV."]
    assert targets.problems(view.source, remote, "on", {"current_activity": "Gaming"}) == [
        f"{remote}'s current_activity can't be 'Gaming'; it takes Music, Watch TV."]  # As its settings name it.
    assert analyzer.reached(view, goal(remote, "on", activity="Watch TV")) is True


def test_the_before_compares_a_setting_as_its_device_shows_it_whatever_the_field_that_set_it_is_called():
    """DETAILS P6.6.1: the settings the recorded Assist's calls set are compared with a replay's devices by the
    attribute each shows them as: a cover's position as current_position, a remote's activity as current_activity. A
    replay that leaves them as the calls asked goes as it went; one that leaves them otherwise doesn't."""
    from homeassistant.components.cover import CoverEntityFeature
    from homeassistant.components.remote import RemoteEntityFeature
    from custom_components.assist_improver.simulation import assist_settings, same_settings
    blind, remote = "cover.garden_blind", "remote.living_room"
    source = {"states": {
        blind: {"state": "open", "attributes": {"current_position": 100,
                                                "supported_features": CoverEntityFeature.SET_POSITION}},
        remote: {"state": "on", "attributes": {"current_activity": "Music", "activity_list": ["Music", "Watch TV"],
                                               "supported_features": RemoteEntityFeature.ACTIVITY}}}}
    turn = {"number": 1, "source": source, "actions": [
        {"action": "cover.set_cover_position", "data": {"entity_id": blind, "position": 50}},
        {"action": "remote.turn_on", "data": {"entity_id": remote, "activity": "Watch TV"}}]}
    asked = assist_settings({"turns": [turn]}, turn)
    assert asked == {blind: {"current_position": 50}, remote: {"current_activity": "Watch TV"}}
    for entity, name, as_asked, otherwise in ((blind, "current_position", 50, 20),
                                              (remote, "current_activity", "Watch TV", "Music")):
        domain, shows = entity.split(".", 1)[0], source["states"][entity]["attributes"]
        for left, same in ((as_asked, True), (otherwise, False)):
            assert same_settings(domain, asked[entity], {**shows, name: left}, targets.shown(source, entity)) is same


def test_valid_states_and_settings_come_from_home_assistant_not_a_copy_of_its_lists():
    for name in ("STATES", "OPTION_ATTRIBUTES", "allowed_states", "BRIGHTNESS_TOLERANCE"):
        assert not hasattr(analyzer, name), name
    assert targets.states({"states": {}}, "cover.garage_door") >= {"open", "closed", "opening", "closing"}
    assert targets.states({"states": {}}, "switch.kettle") == {"on", "off"}
    assert {"brightness", "rgb_color", "color_temp_kelvin"} <= set(targets.settable("light"))
    assert "transition" not in targets.settable("light")  # Its service takes it; no light holds it.
