"""Scorer: hand labels score perfectly; each kind of wrong answer is caught."""

from copy import deepcopy
import json

import pytest

from conftest import private
from custom_components.assist_improver.cases import CaseError
from evaluation.scoring import score_recording, score_set


def press(turn, n):
    return {"turn": turn, "summary": "Press", "kind": "sequence", "entity_id": f"switch.garage_door_{n}",
            "states": ["off", "on"]}


GARAGE = {"recording_id": "garage", "not_requests": [],
          "goals": [press(1, n) for n in (1, 2)] + [press(4, n) for n in (1, 2)]}
LIGHTS = {"recording_id": "lights", "not_requests": [], "goals": [
    {"turn": 1, "summary": "Night", "kind": "state", "entity_id": "light.ceiling", "state": "off"},
    {"turn": 1, "summary": "Night", "kind": "state", "entity_id": "light.bedside", "state": "on", "brightness_pct": 50}]}
SPEECH = {"recording_id": "speech", "goals": [],
          "not_requests": [{"turn": 1, "what": "speech test", "also_acceptable": "a fact goal, such as a speech test"}]}


def failed(report):
    return [c["check"] for c in report["checks"] if not c["passed"]]


@pytest.mark.parametrize("label", [GARAGE, LIGHTS, SPEECH])
def test_hand_labels_score_perfectly(label):
    assert score_recording(deepcopy(label), label)["passed"]


def test_merged_garage_trip_is_caught():
    merged = deepcopy(GARAGE)
    merged["goals"] = [{**press(1, n), "states": ["off", "on", "off", "on"]} for n in (1, 2)]
    report = score_recording(merged, GARAGE, turn_count=4)
    assert "turn 1: switch.garage_door_1 ['off', 'on']" in failed(report)
    assert "turn 4: switch.garage_door_1 ['off', 'on']" in failed(report)
    assert "extra turns" in failed(report)  # Turn 4 became an extra turn.


@pytest.mark.parametrize("change,expected", [
    (lambda p: p["goals"].pop(), "turn 4: switch.garage_door_2 ['off', 'on']"),
    (lambda p: p["goals"][0].update(states=["on", "off"]), "turn 1: switch.garage_door_1 ['off', 'on']"),
    (lambda p: p["goals"][0].update(turn=2), "turn 1: switch.garage_door_1 ['off', 'on']"),
    (lambda p: p["goals"].append({**press(1, 9)}), "no invented goals"),
    (lambda p: p["not_requests"].append({"turn": 2, "what": "?"}), "extra turns"),
])
def test_each_kind_of_wrong_answer_is_reported(change, expected):
    predicted = deepcopy(GARAGE)
    change(predicted)
    assert expected in failed(score_recording(predicted, GARAGE, turn_count=4))


def test_any_reading_that_leaves_the_turn_not_extra_where_the_label_allows_it():
    loose = {**SPEECH, "not_requests": [{"turn": 1, "what": "speech test", "any_without_interventions": True}]}
    as_device = {"recording_id": "speech", "not_requests": [],
                 "goals": [{"turn": 1, "summary": "?", "kind": "state", "entity_id": "fan.x", "state": "on"}]}
    assert score_recording(as_device, loose)["passed"] and not score_recording(as_device, SPEECH)["passed"]
    nothing = {"recording_id": "speech", "not_requests": [], "goals": []}
    assert not score_recording(nothing, loose)["passed"]  # Turn 1 would be extra.


def test_an_open_question_leaves_turns_pending_not_extra():
    asks = {**deepcopy(GARAGE), "question_for_user": "Did you mean the garage doors?"}
    assert "extra turns" in failed(score_recording(asks, GARAGE, turn_count=4))  # Turns 2 and 3 are extra here.


def test_sub_goals_count_as_extra_turns_and_never_stand_in_for_goals():
    label = {"recording_id": "r", "not_requests": [],
             "goals": [{"turn": 1, "summary": "Heat off", "kind": "state", "entity_id": "climate.f", "state": "fan_only"}]}
    controls = {"turn": 2, "summary": "Know the controls", "kind": "fact", "fact": "controls"}
    as_sub_goal = {**deepcopy(label), "goals": label["goals"] + [{**controls, "serves": 1}]}
    assert score_recording(as_sub_goal, label, turn_count=2)["passed"]
    as_goal = {**deepcopy(label), "goals": label["goals"] + [controls]}
    assert failed(score_recording(as_goal, label, turn_count=2)) == ["extra turns"]
    closing_as_sub_goal = deepcopy(GARAGE)
    for goal in closing_as_sub_goal["goals"][2:]:
        goal["serves"] = 1
    assert "turn 4: a goal starts" in failed(score_recording(closing_as_sub_goal, GARAGE))


def test_brightness_tolerance_and_state():
    predicted = deepcopy(LIGHTS)
    predicted["goals"][1]["brightness_pct"] = 54
    assert score_recording(predicted, LIGHTS)["passed"]
    predicted["goals"][1]["brightness_pct"] = 60
    assert failed(score_recording(predicted, LIGHTS)) == ["turn 1: light.bedside on"]
    del predicted["goals"][1]["brightness_pct"]
    assert failed(score_recording(predicted, LIGHTS)) == ["turn 1: light.bedside on"]


@pytest.mark.parametrize("label, said, same", [
    ({"brightness_pct": 50}, {"attributes": {"brightness": 128}}, True),
    ({"brightness_pct": 50}, {"attributes": {"brightness": 200}}, False),
    ({"attributes": {"rgb_color": [255, 0, 0]}}, {"attributes": {"color_name": "red"}}, True),
    ({"attributes": {"rgb_color": [255, 0, 0]}}, {"attributes": {"color_name": "blue"}}, False),
])
def test_a_setting_scores_in_any_of_its_forms_as_targets_reads_it(label, said, same):
    """A predicted goal's settings are the label's as targets compares them (DETAILS P9.7.1): a brightness as a
    percentage or 0 to 255, a colour by name or as rgb."""
    goal = {"turn": 1, "summary": "Lamp", "kind": "state", "entity_id": "light.lamp", "state": "on"}
    labelled = {"recording_id": "lamp", "not_requests": [], "goals": [{**goal, **label}]}
    assert score_recording({**labelled, "goals": [{**goal, **said}]}, labelled)["passed"] is same


def test_a_state_goal_may_accept_another_end_state_where_a_household_fact_isnt_known():
    label = {"recording_id": "tv", "not_requests": [], "goals": [
        {"turn": 1, "summary": "TV off", "kind": "state", "entity_id": "media_player.tv", "state": "off",
         "or_state": ["on"]}]}
    for state in ("off", "on"):
        answer = {"recording_id": "tv", "not_requests": [], "goals": [
            {"turn": 1, "summary": "TV", "kind": "state", "entity_id": "media_player.tv", "state": state}]}
        assert score_recording(answer, label)["passed"], state
    other = {**answer, "goals": [{**answer["goals"][0], "state": "idle"}]}
    assert not score_recording(other, label)["passed"]


def test_an_action_is_scored_by_its_outcome_and_devices():
    spoken = {"turn": 1, "summary": "Hello on the speakers", "kind": "action", "outcome": "announce",
              "entity_ids": ["media_player.kitchen", "media_player.study"]}
    label = {"recording_id": "say", "not_requests": [], "goals": [spoken]}
    same = {**label, "goals": [{**spoken, "entity_ids": list(reversed(spoken["entity_ids"]))}]}
    assert score_recording(same, label)["passed"] and score_recording(same, label)["findings"] == []
    one = {**label, "goals": [{**spoken, "entity_ids": ["media_player.kitchen"]}]}
    assert not score_recording(one, label)["passed"]
    extra = {**label, "goals": [spoken, {**spoken, "entity_ids": ["media_player.porch"]}]}
    assert failed(score_recording(extra, label)) == ["no invented goals"]
    as_part = {"recording_id": "say", "not_requests": [], "goals": [
        {"turn": 1, "summary": "Lamp on", "kind": "state", "entity_id": "light.lamp", "state": "on"}],
        "sub_goals": [{**spoken, "serves": 1}]}
    assert failed(score_recording({**as_part, "sub_goals": []}, as_part)) == [
        "turn 1: announce on media_player.kitchen, media_player.study"]


def test_speech_test_alternatives():
    as_fact = {"recording_id": "speech", "not_requests": [], "question_for_user": None,
               "goals": [{"turn": 1, "summary": "Check it hears me", "kind": "fact", "fact": "Assist hears me"}]}
    assert score_recording(as_fact, SPEECH)["passed"]
    as_question = {"recording_id": "speech", "not_requests": [], "goals": [], "question_for_user": "What did you want?"}
    assert not score_recording(as_question, SPEECH)["passed"]
    assert score_recording(as_question, {**SPEECH, "not_requests": [{**SPEECH["not_requests"][0],
                                                                     "question_acceptable": True}]})["passed"]
    as_device = {"recording_id": "speech", "not_requests": [],
                 "goals": [{"turn": 1, "summary": "?", "kind": "state", "entity_id": "light.x", "state": "on"}]}
    assert "turn 1: not a request (speech test)" in failed(score_recording(as_device, SPEECH))
    strict = {**SPEECH, "not_requests": [{"turn": 1, "what": "speech test"}]}
    assert not score_recording(as_fact, strict)["passed"]


def test_fact_goals_check_only_the_turn_and_set_totals_count_missing():
    label = {"recording_id": "info", "not_requests": [],
             "goals": [{"turn": 1, "summary": "Which lights are warm", "kind": "fact", "fact": "The table lamps"}]}
    other_words = deepcopy(label)
    other_words["goals"][0]["fact"] = "Table lamps are the warm ones"
    assert score_recording(other_words, label)["passed"]
    assert not score_recording({**label, "goals": []}, label)["passed"]
    with pytest.raises(CaseError):
        score_recording({**label, "recording_id": "other"}, label)
    totals = score_set({}, {"info": label})
    assert totals["recordings_passed"] == 0 and totals["checks"] == 1
    about = {**label, "goals": [{**label["goals"][0], "entity_ids": ["light.table_lamp"]}]}  # The devices: soft.
    other = score_recording({**about, "goals": [{**about["goals"][0], "entity_ids": ["light.other"]}]}, about)
    assert other["passed"] and other["findings"] == [
        "turn 1: the fact names ['light.other']; the label's devices are ['light.table_lamp']"]
    assert score_recording(deepcopy(about), about)["findings"] == []
    thanked = {**about, "goals": [{"turn": 1, "summary": "Thanks", "kind": "thanks", "serves": 1}]}
    assert score_recording(thanked, about)["findings"] == []  # A thanks taking the fact names no devices.


def test_a_sequence_may_start_from_the_state_before_the_first_change():
    predicted = deepcopy(GARAGE)
    for goal in predicted["goals"]:
        goal["states"] = ["on", "off", "on"]
    assert score_recording(predicted, GARAGE)["passed"]
    predicted["goals"][0]["states"] = ["off", "off", "on"]
    assert not score_recording(predicted, GARAGE)["passed"]


def test_optional_goals_are_accepted_but_not_enough():
    label = {"recording_id": "r", "not_requests": [],
             "goals": [{"turn": 1, "summary": "Heat off", "kind": "state", "entity_id": "climate.f", "state": "fan_only"}],
             "optional_goals": [{"turn": 1, "summary": "Already on", "kind": "state", "entity_id": "fan.f", "state": "on"}]}
    fan = {"turn": 1, "summary": "Fan on", "kind": "state", "entity_id": "fan.f", "state": "on"}
    with_fan = {**deepcopy(label), "goals": label["goals"] + [fan]}
    assert score_recording(deepcopy(label), label)["passed"]
    assert score_recording(with_fan, label)["passed"]
    assert not score_recording({**with_fan, "goals": [fan]}, label)["passed"]  # Fan on alone is not enough.
    assert not score_recording({**with_fan, "goals": [{**fan, "state": "off"}] + label["goals"]}, label)["passed"]


def test_private_hand_labels_score_perfectly():
    labels = {p.stem: json.loads(p.read_text()) for p in private("labels").glob("*.json")}
    assert len(labels) == 5
    totals = score_set(deepcopy(labels), labels)
    assert totals["recordings_passed"] == 5 and totals["checks_passed"] == totals["checks"]


def test_derived_reached_matches_private_labels():
    from custom_components.assist_improver.analyzer import to_analysis
    from custom_components.assist_improver.exploration import SessionView
    labels, recordings = private("labels"), private("recordings")
    for path in sorted(labels.glob("*.json")):
        label = json.loads(path.read_text())
        view = SessionView(json.loads((recordings / path.name).read_text())["data"])
        analysis = to_analysis(label["recording_id"], label, view)
        assert [w["reached"] for w in analysis["goals"]] == [w["reached"] for w in label["goals"]], path.name


def test_ask_goals_give_soft_findings_not_failures():
    heater, rug, lamp, lights = "select.den_heater", "switch.den_rug", "select.den_lamp", "light.porch"
    label = {"recording_id": "den", "not_requests": [], "goals": [
        {"turn": 1, "summary": "Warm the den", "kind": "ask", "must_include": [heater, rug], "allowed": [heater, rug, lamp],
         "intended": [{"entity_id": heater, "state": "high"}, {"entity_id": rug, "state": "on"}]}]}
    def answer(*goals, question=""):
        return {"recording_id": "den", "not_requests": [], "goals": list(goals), "question_for_user": question}
    ask = lambda *ids: {"turn": 1, "summary": "Which settings?", "kind": "ask", "entity_ids": list(ids)}
    guess = lambda e, s: {"turn": 1, "summary": "Guess", "kind": "state", "entity_id": e, "state": s}
    good = score_recording(answer(ask(heater, rug, lamp)), label)
    assert good["passed"] and good["findings"] == [f"turn 1: asked about {heater}, {lamp}, {rug}, as expected"]
    narrow = score_recording(answer(ask(heater)), label)
    assert narrow["passed"] and narrow["findings"] == [f"turn 1: ask too narrow: {rug} not left open (the user meant 'on')"]
    guessed = score_recording(answer(guess(heater, "medium"), guess(rug, "on")), label)
    assert guessed["passed"] and guessed["findings"] == [
        f"turn 1: guessed {heater} = 'medium'; the user meant 'high'", f"turn 1: guessed {rug} = 'on' (what the user meant, but unstated)"]
    broad = score_recording(answer(ask(heater, rug, lights)), label)
    assert broad["findings"] == [f"turn 1: ask too broad: also {lights}"]
    asked = score_recording(answer(question="What did you want?"), label, turn_count=1)
    assert asked["findings"][0].startswith("turn 1: a free-text question instead of an ask")
    assert not score_recording(answer(guess(lights, "on")), label)["passed"]  # Outside the ask: invented.


def test_a_goal_may_accept_any_of_several_devices():
    label = {"recording_id": "tv", "not_requests": [], "goals": [
        {"turn": 1, "summary": "TV off", "kind": "state", "any_of": ["media_player.a", "media_player.b"], "state": "off"}]}
    one = {"recording_id": "tv", "not_requests": [], "goals": [
        {"turn": 1, "summary": "TV off", "kind": "state", "entity_id": "media_player.b", "state": "off"}]}
    assert score_recording(one, label)["passed"]
    both = {**one, "goals": one["goals"] + [{**one["goals"][0], "entity_id": "media_player.a"}]}
    assert score_recording(both, label)["passed"]
    assert not score_recording({**one, "goals": [{**one["goals"][0], "state": "on"}]}, label)["passed"]
    assert not score_recording({**one, "goals": [{**one["goals"][0], "entity_id": "media_player.c"}]}, label)["passed"]


RELAY = {"turn": 1, "summary": "Relay off", "kind": "state", "entity_id": "switch.relay", "state": "off"}
REACTED = {"recording_id": "reacted", "not_requests": [], "goals": [RELAY], "sub_goals": [
    {"turn": 2, "summary": "Again", "kind": "step", "serves": 1},
    {"turn": 3, "summary": "Too wordy", "kind": "conduct", "serves": 1, "about": 1, "quote": "less wordy, please"},
    {"turn": 3, "summary": "Thanks", "kind": "thanks", "serves": 1},
    {"turn": 4, "summary": "Filed it", "kind": "wrap-up", "serves": 1}]}


def answer(*subs):
    return {"recording_id": "reacted", "not_requests": [], "goals": [RELAY, *subs], "question_for_user": ""}


def test_each_labelled_sub_goal_is_scored_by_its_kind_its_root_goal_and_what_it_is_about():
    right = answer(*REACTED["sub_goals"])
    assert score_recording(right, REACTED, turn_count=4)["passed"]
    assert score_recording(deepcopy(REACTED), REACTED, turn_count=4)["passed"]  # A label read as an answer.
    step, objection, thanks, wrap = REACTED["sub_goals"]
    as_fact = answer({**step, "kind": "fact", "fact": "again"}, objection, thanks, wrap)
    assert failed(score_recording(as_fact, REACTED, turn_count=4)) == ["turn 2: step under the goal at turn 1"]
    elsewhere = answer(step, {**objection, "about": 2}, thanks, wrap)
    assert failed(score_recording(elsewhere, REACTED, turn_count=4)) == [
        "turn 3: conduct under the goal at turn 1, about 1"]
    unfiled = answer(step, objection, thanks)
    assert "turn 4: wrap-up under the goal at turn 1" in failed(score_recording(unfiled, REACTED, turn_count=4))


def test_another_span_of_their_words_is_a_soft_finding_not_a_failure():
    shorter = answer(REACTED["sub_goals"][0], {**REACTED["sub_goals"][1], "quote": "less wordy"},
                     *REACTED["sub_goals"][2:])
    report = score_recording(shorter, REACTED, turn_count=4)
    assert report["passed"] and report["findings"] == [
        "turn 3: quoted 'less wordy'; the label's span is 'less wordy, please'"]
    same = answer(REACTED["sub_goals"][0], {**REACTED["sub_goals"][1], "quote": "Less  wordy, please"},
                  *REACTED["sub_goals"][2:])
    assert score_recording(same, REACTED, turn_count=4)["findings"] == []


def test_a_turns_devices_are_scored_as_a_set_whichever_the_answer_makes_its_root():
    """Several devices asked for at once are one root goal and its parts; which is the root is the analyzer's pick.
    Each must be there with its value, and a later repair by its device and value too; none may be invented."""
    doors = [press(1, 1), press(1, 2), {**press(1, 1), "entity_id": "switch.gate"}]
    label = {"recording_id": "garage", "not_requests": [], "goals": [doors[0]],
             "sub_goals": [{**d, "serves": 1} for d in doors[1:]]}
    middle_first = {"recording_id": "garage", "not_requests": [], "goals": [
        doors[1], {**doors[0], "serves": 1}, {**doors[2], "serves": 1}]}
    assert score_recording(middle_first, label, turn_count=1)["passed"]
    one_left_out = {**middle_first, "goals": middle_first["goals"][:2]}
    assert failed(score_recording(one_left_out, label, turn_count=1)) == ["turn 1: switch.gate ['off', 'on']"]
    wrong_value = {**middle_first, "goals": [*middle_first["goals"][:2], {**doors[2], "states": ["on"], "serves": 1}]}
    assert "turn 1: switch.gate ['off', 'on']" in failed(score_recording(wrong_value, label, 1))
    extra = {**middle_first, "goals": [*middle_first["goals"], {**press(1, 9), "serves": 1}]}
    assert failed(score_recording(extra, label, turn_count=1)) == ["no invented goals"]


def test_a_step_may_continue_a_step_and_an_objection_may_be_about_any_turn_the_label_accepts():
    label = {**REACTED, "sub_goals": [
        {"turn": 2, "summary": "Again", "kind": "step", "serves": 1},
        {"turn": 3, "summary": "Again, again", "kind": "step", "serves": 2},
        {"turn": 4, "summary": "Not that", "kind": "conduct", "serves": 1, "about": [1, 2], "quote": "not that"}]}
    chained = answer(*label["sub_goals"][:2], {**label["sub_goals"][2], "about": 2})
    assert score_recording(chained, label, turn_count=4)["passed"]
    direct = answer(label["sub_goals"][0], {**label["sub_goals"][1], "serves": 1}, {**label["sub_goals"][2], "about": 1})
    assert score_recording(direct, label, turn_count=4)["passed"]
    assert not score_recording(answer(*label["sub_goals"][:2], {**label["sub_goals"][2], "about": 3}), label, 4)["passed"]


def test_one_answer_stands_for_one_labelled_reaction_and_a_span_is_compared_word_for_word():
    two = {**REACTED, "sub_goals": [
        {"turn": 2, "summary": "Too long", "kind": "conduct", "serves": 1, "about": 1, "quote": "too long"},
        {"turn": 2, "summary": "Too loud", "kind": "conduct", "serves": 1, "about": 1, "quote": "too loud"}]}
    merged = answer({**two["sub_goals"][0], "quote": "too long, too loud"})
    assert failed(score_recording(merged, two, turn_count=2)) == ["turn 2: conduct under the goal at turn 1, about 1"]
    punctuated = answer({**two["sub_goals"][0], "quote": "Too long."}, two["sub_goals"][1])
    assert score_recording(punctuated, two, turn_count=2)["findings"] == []
    swapped = answer({**two["sub_goals"][1], "quote": "too loud"}, two["sub_goals"][0])  # In either order.
    assert score_recording(swapped, two, turn_count=2)["findings"] == []
    either = {**two, "sub_goals": [{**two["sub_goals"][0], "quote": "it's too long"}]}
    assert score_recording(answer({**either["sub_goals"][0], "quote": "It‘s too long"}), either,
                           turn_count=2)["findings"] == []
