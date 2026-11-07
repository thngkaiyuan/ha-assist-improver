"""SPEC.md P0.1.1's measures (measures.py): goals met, turns per root goal and side effects left, judged by code from
what the home did, and how two replays compare."""

from types import SimpleNamespace

from custom_components.assist_improver import measures

LAMP, FAN, HEATER, PORCH = "light.lamp", "switch.fan", "switch.heater", "light.porch"


def view(start, *moves):
    """A home: its start states, and each change (entity, old, new, turn[, by])."""
    changes = {}
    for n, (entity, old, new, turn, *by) in enumerate(moves):
        changes.setdefault(entity, []).append({"at": f"2026-01-01T09:00:{n:02d}+00:00", "during_turn": turn,
                                               "old": old, "new": new, "by": by[0] if by else "assistant"})
    return SimpleNamespace(source={"states": {e: {"state": s} for e, s in start.items()}}, changes=changes)


def state(turn, entity, value, serves=None, summary="", **extra):
    goal = {"turn": turn, "summary": summary or f"{entity} {value}", "kind": "state", "entity_id": entity,
            "state": value, **extra}
    return {**goal, "serves": serves} if serves is not None else goal


NESTED = {"turns": 3, "goals": [state(1, LAMP, "on"), state(2, FAN, "off", serves=1), state(3, HEATER, "on", serves=2)]}
NESTED_HOME = view({LAMP: "off", FAN: "off", HEATER: "on"},
                   (LAMP, "off", "on", 1), (FAN, "off", "on", 1), (FAN, "on", "off", 2), (HEATER, "on", "off", 2),
                   (HEATER, "off", "on", 3))


def test_every_goal_is_keyed_as_the_flow_keys_it_with_the_note_last_and_each_revision_note_after():
    """DETAILS P12.8.1: a revision note is a turn of the analysis, as the note it was sent in with is: the turns after
    those said, in order, labelled by the notes the analysis read."""
    analysis = {"turns": 2, "notes": [{"sent": True}, {"revision": 2}], "goals": [
        state(1, LAMP, "on"), state(2, LAMP, "on", serves=1),
        {"turn": 3, "summary": "The note", "kind": "conduct", "about": "all", "quote": "Just the lamp", "serves": 1},
        {"turn": 4, "summary": "Only the left one", "kind": "step", "serves": 1}]}
    goals = measures.goals_of(analysis)
    assert [(g["key"], g["label"]) for g in goals] == [("1:0", 1), ("2:1", 2), ("3:2", "note"), ("4:3", "revision:2")]
    older = {**analysis, "goals": analysis["goals"][:3]}
    del older["notes"]  # Recorded before an analysis kept its notes: the one it was sent in with, if any.
    assert [g["label"] for g in measures.goals_of(older)] == [1, 2, "note"]


def test_a_goal_assist_reached_is_met_one_it_missed_unmet_and_one_whose_turn_wasnt_said_indeterminate():
    goals = measures.goals_of({"turns": 2, "goals": [state(1, LAMP, "on"), state(2, PORCH, "on")]})
    home = view({LAMP: "off", PORCH: "off"}, (LAMP, "off", "on", 1))
    assert measures.judge(goals, home, [1, 2])["goals"] == {"1:0": "met", "2:1": "unmet"}
    assert measures.judge(goals, home, [1])["goals"] == {"1:0": "met", "2:1": "indeterminate"}


def test_sub_goals_at_every_depth_get_a_state_and_each_repair_turn_counts_for_the_root():
    judged = measures.judge(measures.goals_of(NESTED), NESTED_HOME, [1, 2, 3])
    assert set(judged["goals"].values()) == {"met"}
    assert judged["turns_per_root_goal"] == {"1:0": 3} and judged["counted"] == {"1:0": [1, 2, 3]}


def test_an_objection_is_unmet_in_the_conversation_as_it_went_and_otherwise_as_the_judge_found_it():
    analysis = {"turns": 1, "goals": [state(1, LAMP, "on"), {"turn": 2, "summary": "The note", "kind": "conduct",
                                                             "about": "all", "quote": "Say less", "serves": 1}]}
    goals, home = measures.goals_of(analysis), view({LAMP: "off"}, (LAMP, "off", "on", 1))
    assert measures.judge(goals, home, [1])["goals"]["2:1"] == "unmet"
    assert measures.judge(goals, home, [1], {"2:1": "met"})["goals"]["2:1"] == "met"
    assert measures.judge(goals, home, [1], {"2:1": "unmet"})["goals"]["2:1"] == "unmet"
    assert measures.judge(goals, home, [1], {})["goals"]["2:1"] == "indeterminate"  # Not judged: can't be told.
    # The note is never said, so it's no turn: what it asks for is a goal of its own.
    assert measures.judge(goals, home, [1])["counted"] == {"1:0": [1]}


def test_a_thanks_is_met_once_said_and_counts_like_every_turn_said():
    analysis = {"turns": 2, "goals": [state(1, LAMP, "on"), {"turn": 2, "summary": "Thanks", "kind": "thanks",
                                                            "serves": 1}]}
    goals = measures.goals_of(analysis)
    done = measures.judge(goals, view({LAMP: "off"}, (LAMP, "off", "on", 1)), [1, 2])
    assert done["goals"]["2:1"] == "met" and done["turns_per_root_goal"] == {"1:0": 2}
    assert measures.judge(goals, view({LAMP: "off"}, (LAMP, "off", "on", 1)), [1])["goals"]["2:1"] == "indeterminate"


def test_side_effects_are_devices_assist_left_changed_that_no_goal_asked_for_at_once():
    goals = measures.goals_of(NESTED)
    left_off = view({LAMP: "off", FAN: "off", HEATER: "on"},
                    (LAMP, "off", "on", 1), (FAN, "off", "on", 1), (FAN, "on", "off", 2), (HEATER, "on", "off", 2))
    assert [s["entity_id"] for s in measures.judge(goals, left_off, [1, 2, 3])["side_effects_left"]] == [HEATER]
    doors = measures.goals_of({"turns": 1, "goals": [state(1, LAMP, "on"), state(1, PORCH, "on", serves=1)]})
    both = view({LAMP: "off", PORCH: "off"}, (LAMP, "off", "on", 1), (PORCH, "off", "on", 1))
    assert measures.judge(doors, both, [1])["side_effects_left"] == []  # asked at once: wanted


def step(turn, serves, summary="Again"):
    return {"turn": turn, "summary": summary, "kind": "step", "serves": serves}


def test_a_step_is_met_once_its_goal_was_met_before_its_turn_and_open_in_the_conversation_as_it_went():
    goals = measures.goals_of({"turns": 3, "goals": [state(1, LAMP, "on"), step(2, 1), step(3, 2)]})
    late = view({LAMP: "off"}, (LAMP, "off", "on", 2))
    assert measures.judge(goals, late, [1, 2, 3], {})["goals"] == {"1:0": "met", "2:1": "unmet", "3:2": "met"}
    assert measures.judge(goals, late, [1, 2, 3], measures.CONVERSATION)["goals"]["3:2"] == "unmet"  # It reacted.
    first = view({LAMP: "off"}, (LAMP, "off", "on", 1))
    assert measures.judge(goals, first, [1], {})["goals"] == {"1:0": "met", "2:1": "met", "3:2": "met"}
    asked = measures.goals_of({"turns": 2, "goals": [{"turn": 1, "summary": "Which?", "kind": "ask",
                                                      "entity_ids": [LAMP]}, step(2, 1)]})
    assert measures.judge(asked, first, [1, 2], measures.CONVERSATION)["goals"]["2:1"] == "indeterminate"


def test_a_wrap_up_is_met_once_its_root_was_met_with_nothing_else_of_the_roots_open_and_a_set_aside_one_is_neither():
    wrap = {"turn": 3, "summary": "Filed it", "kind": "wrap-up", "serves": 1}
    goals = measures.goals_of({"turns": 3, "goals": [state(1, LAMP, "on"), state(2, FAN, "off", serves=1), wrap]})
    fixed = view({LAMP: "off", FAN: "off"}, (LAMP, "off", "on", 1))
    assert measures.judge(goals, fixed, [1, 2, 3], {})["goals"]["3:2"] == "met"
    assert measures.judge(goals, fixed, [1, 2, 3], measures.CONVERSATION)["goals"]["3:2"] == "unmet"
    left = view({LAMP: "off", FAN: "off"}, (LAMP, "off", "on", 1), (FAN, "off", "on", 1))  # The repair still open.
    assert measures.judge(goals, left, [1, 2, 3], {})["goals"]["3:2"] == "unmet"
    assert measures.judge(goals, fixed, [1, 2, 3], {}, {"3:2"})["goals"]["3:2"] == "indeterminate"
    noted = measures.goals_of({"turns": 3, "goals": [state(1, LAMP, "on"), state(2, FAN, "off", serves=1), wrap, {
        "turn": 4, "summary": "n", "kind": "conduct", "about": 1, "quote": "n", "serves": 1}]})
    assert measures.judge(noted, fixed, [1, 2, 3], {"4:3": "unmet"})["goals"]["3:2"] == "unmet"  # The note's open.
    assert measures.judge(noted, fixed, [1, 2, 3], {"4:3": "met"})["goals"]["3:2"] == "met"
    # Without the change it was met, though the household filed it: it can't be tested, so it's set aside.
    before, went = measures.judge(goals, fixed, [1, 2, 3], {}), measures.judge(goals, fixed, [1, 2, 3])
    assert measures.set_aside(before, went, goals) == {"3:2"}


def test_a_replay_says_a_turn_only_while_a_goal_on_it_is_open_and_always_a_roots_and_a_thanks():
    """DETAILS P6.4: a part or repair not reached yet, a step whose goal isn't met yet, an objection (until it's
    judged as the replay goes), a wrap-up while anything of its root is open, a goal set aside; a turn with no goal."""
    thanks = {"turn": 3, "summary": "Thanks", "kind": "thanks", "serves": 1}
    goals = measures.goals_of({"turns": 3, "goals": [state(1, LAMP, "on"), step(2, 1), thanks]})
    met, missed = view({LAMP: "off"}, (LAMP, "off", "on", 1)), view({LAMP: "off"})
    assert measures.needed(goals, met, 1) and not measures.needed(goals, met, 2) and measures.needed(goals, met, 3)
    assert measures.needed(goals, missed, 2) and measures.needed(goals, met, 2, aside={"2:1"})
    repair = measures.goals_of({"turns": 2, "goals": [state(1, LAMP, "on"), state(2, FAN, "off", serves=1)]})
    assert not measures.needed(repair, view({LAMP: "off", FAN: "off"}), 2)
    assert measures.needed(repair, view({LAMP: "off", FAN: "off"}, (FAN, "off", "on", 1)), 2)
    wrap = {"turn": 2, "summary": "Filed it", "kind": "wrap-up", "serves": 1}
    filed = measures.goals_of({"turns": 2, "goals": [state(1, LAMP, "on"), wrap]})
    assert not measures.needed(filed, met, 2) and measures.needed(filed, missed, 2)
    assert measures.needed(filed, met, 2, aside={"2:1"})
    objected = measures.goals_of({"turns": 2, "goals": [state(1, LAMP, "on"), wrap, {
        "turn": 3, "summary": "n", "kind": "conduct", "about": "all", "quote": "n", "serves": 1}]})
    assert measures.needed(objected, met, 2)  # The note's objection, not judged yet: still open.
    assert not measures.needed(objected, met, 2, conduct={"3:2": "met"})  # Judged on the replies so far: closed.
    said = measures.goals_of({"turns": 2, "goals": [state(1, LAMP, "on"), {
        "turn": 2, "summary": "Less", "kind": "conduct", "about": 1, "quote": "less", "serves": 1}]})
    assert measures.needed(said, met, 2) and measures.needed(said, met, 2, conduct={"2:1": "unmet"})
    assert not measures.needed(said, met, 2, conduct={"2:1": "met"})  # No reply so far shows it.
    assert measures.needed(said, met, 2, aside={"2:1"}, conduct={"2:1": "met"})
    assert measures.needed(measures.goals_of({"turns": 2, "goals": [state(1, LAMP, "on")]}), met, 2)  # No goal.


def replay(goals=None, turns=None, left=(), couldnt=()):
    goals = goals or {"r": "met"}
    return {"goals": goals, "turns_per_root_goal": turns or {"r": 2},
            "side_effects_left": [{"entity_id": e, "turn": 1} for e in left], "counted": {},
            "home_couldnt": list(couldnt)}


def test_easier_means_strictly_better_on_one_measure_and_worse_on_none():
    before = replay({"r": "met"}, {"r": 2})
    assert measures.easier(replay({"r": "met"}, {"r": 1}), before)  # a turn saved
    assert measures.gains(replay({"r": "met"}, {"r": 1}), before) == ["turns per root goal"]
    assert not measures.easier(replay({"r": "met"}, {"r": 1}, left=[FAN]), before)  # a turn for a side effect
    assert not measures.easier(replay({"r": "met"}, {"r": 2}), before)  # no gain
    assert not measures.easier(replay({"r": "unmet"}, {"r": 1}), before)  # a goal lost
    missed = replay({"r": "unmet", "s": "met"}, {"r": 1, "s": 1})
    assert measures.easier(replay({"r": "met", "s": "met"}, {"r": 1, "s": 1}), missed)  # met more
    assert measures.gains(replay({"r": "met", "s": "met"}, {"r": 1, "s": 1}), missed) == ["goals met"]
    assert not measures.easier(replay({"r": "met", "s": "met"}, {"r": 1, "s": 1}, left=[FAN]), missed)  # with a side effect
    assert not measures.easier(replay({"r": "met", "s": "unmet"}, {"r": 1, "s": 1}),
                               replay({"r": "unmet", "s": "met"}, {"r": 1, "s": 1}))  # traded one for another
    noted = replay({"r": "met", "note": "unmet"}, {"r": 1})  # The note (a sub-goal of r) asked for what r didn't get.
    followed = replay({"r": "met", "note": "met"}, {"r": 1})
    assert measures.gains(followed, noted) == ["goals met"] and measures.easier(followed, noted)  # followed now
    assert measures.worse(noted, followed) == ["goals met"]  # and the other way, lost


def test_turns_compare_on_a_root_goal_met_both_ways_or_unmet_both_ways_because_the_home_couldnt_reach_it():
    """A root unmet both ways because its device was unavailable as asked (home_couldnt) that takes a turn fewer is a
    gain: nothing could have met it. One the home could reach, unmet both ways, isn't, nor one that can't be told (a
    question): a change that gives up on the request saves turns by abandoning it (DETAILS P6.1.6). One that ended
    differently isn't compared on turns: met before and unmet now is worse on goals met, unmet before and met now
    better on it."""
    blocked = replay({"r": "unmet"}, {"r": 2}, couldnt=["r"])
    assert measures.gains(replay({"r": "unmet"}, {"r": 1}, couldnt=["r"]), blocked) == ["turns per root goal"]
    assert measures.easier(replay({"r": "unmet"}, {"r": 1}, couldnt=["r"]), blocked)
    for ended in ("unmet", "indeterminate"):
        assert measures.gains(replay({"r": ended}, {"r": 1}), replay({"r": ended}, {"r": 2})) == []
    assert measures.gains(replay({"r": "met"}, {"r": 1}), replay({"r": "unmet"}, {"r": 2})) == ["goals met"]
    assert measures.worse(replay({"r": "unmet"}, {"r": 1}), replay({"r": "met"}, {"r": 2})) == ["goals met"]


def test_a_replay_without_the_change_went_as_it_went_only_with_the_same_goals_turns_and_side_effects():
    goals = measures.goals_of(NESTED)
    conversation = measures.judge(goals, NESTED_HOME, [1, 2, 3])
    assert measures.went_as_it_went(measures.judge(goals, NESTED_HOME, [1, 2, 3]), conversation) is None
    no_lamp = view({LAMP: "off", FAN: "off", HEATER: "on"}, (FAN, "off", "on", 1), (FAN, "on", "off", 2))
    assert measures.went_as_it_went(measures.judge(goals, no_lamp, [1, 2, 3]), conversation)
    # A goal left out isn't compared (a model judges it later); the others still are.
    differs = {**conversation, "goals": {**conversation["goals"], "3:2": "unmet"}}
    assert measures.went_as_it_went(differs, conversation, leave_out={"3:2"}) is None
    assert "3:2" in measures.went_as_it_went(differs, conversation, leave_out={"2:1"})


def test_a_change_helps_the_roots_it_meets_or_speeds_up_and_the_one_whose_side_effect_it_removes():
    judged = {"1:0": {"turn": 1, "root": "1:0"}, "2:1": {"turn": 2, "root": "2:1"}}
    before = {"goals": {"1:0": "met", "2:1": "unmet"}, "turns_per_root_goal": {"1:0": 1, "2:1": 1},
              "side_effects_left": [{"entity_id": FAN, "turn": 1}], "counted": {}}
    after = {"goals": {"1:0": "met", "2:1": "met"}, "turns_per_root_goal": {"1:0": 1, "2:1": 1},
             "side_effects_left": [], "counted": {}}
    assert measures.helped(before, after, judged) == ["1:0", "2:1"]
    unmet = {**before, "goals": {"1:0": "met", "2:1": "unmet"}, "turns_per_root_goal": {"1:0": 1, "2:1": 2},
             "side_effects_left": [], "home_couldnt": ["2:1"]}  # Its device was unavailable: a turn saved helps.
    assert measures.helped(unmet, {**unmet, "turns_per_root_goal": {"1:0": 1, "2:1": 1}}, judged) == ["2:1"]
    assert measures.helped({**unmet, "home_couldnt": []}, {**unmet, "turns_per_root_goal": {"1:0": 1, "2:1": 1}},
                           judged) == []
    judged["note"] = {"turn": "note", "root": "1:0"}  # Only the note, a sub-goal of 1:0, is met with the change.
    before.update(goals={"1:0": "met", "2:1": "met", "note": "unmet"}, side_effects_left=[])
    after.update(goals={"1:0": "met", "2:1": "met", "note": "met"})
    assert measures.helped(before, after, judged) == ["1:0"]


def recorded_turns(*states):
    """A recording's turns, as recording_turns gives them: each turn's snapshot of the home ({entity: state})."""
    return [{"number": n, "prepared": {"context": {"context": {"id": f"turn-{n}"}}},
             "source": {"at": f"2026-01-01T09:0{n - 1}:00+00:00", "states": {e: {"state": v} for e, v in s.items()}}}
            for n, s in enumerate(states, 1)]


def test_a_replays_view_holds_its_own_changes_and_brightness_and_what_people_did_to_untouched_devices():
    turns = recorded_turns({LAMP: "on", PORCH: "off"}, {LAMP: "on", PORCH: "on"})
    recorded = view({}, (PORCH, "off", "on", 1, "person"), (LAMP, "on", "on", 1))
    recorded.recording = {"state_changes": []}
    steps = [{"turn": 1, "changes": [{"entity_id": LAMP, "old": "on", "new": "on",
                                      "attributes_changed": {"brightness": [0, 255]}}]},
             {"turn": 2, "changes": [{"entity_id": FAN, "old": "off", "new": "on"}]}]
    home = measures.replay_view(turns, recorded, steps)
    assert home.changes[LAMP][0]["attributes_changed"] == {"brightness": [0, 255], "brightness_pct": [0, 100]}
    assert home.changes[FAN][0]["during_turn"] == 2 and home.changes[PORCH][0]["by"] == "person"


def test_a_replays_view_holds_what_others_moved_between_turns_on_a_device_assist_changed_as_its_sandbox_did():
    """Assist turned the lamp on, someone turned it off before turn 2, and turn 2 asked again (a step): the replay's
    sandbox puts the lamp off before turn 2 too (simulation.moved_by_others), so its view has it off there, and the
    step is unmet as in the conversation, not met by a lamp that stayed on only in the view."""
    turns = recorded_turns({LAMP: "off"}, {LAMP: "off"})
    recorded = view({LAMP: "off"}, (LAMP, "off", "on", 1), (LAMP, "on", "off", 1, "person"), (LAMP, "off", "on", 2))
    recorded.recording = {"state_changes": [
        {"entity_id": LAMP, "at": "2026-01-01T09:00:01+00:00", "old": {"state": "off"},
         "new": {"state": "on", "context": {"id": "turn-1"}}},
        {"entity_id": LAMP, "at": "2026-01-01T09:00:30+00:00", "old": {"state": "on"},
         "new": {"state": "off", "context": {"id": "someone"}}}]}
    steps = [{"turn": 1, "changes": [{"entity_id": LAMP, "old": "off", "new": "on"}]},
             {"turn": 2, "changes": [{"entity_id": LAMP, "old": "off", "new": "on"}]}]
    home = measures.replay_view(turns, recorded, steps)
    assert [(c["during_turn"], c["new"], c["by"] == "assistant") for c in home.changes[LAMP]] == [
        (1, "on", True), (1, "off", False), (2, "on", True)]
    goals = measures.goals_of({"turns": 2, "goals": [state(1, LAMP, "on"), step(2, 1)]})
    assert measures.judge(goals, home, [1, 2], {})["goals"] == {"1:0": "met", "2:1": "unmet"}


QUESTION = {"turns": 4, "goals": [
    {"turn": 1, "summary": "Whether the fan is on", "kind": "fact", "fact": "The fan's state", "entity_ids": [FAN],
     "reached": True, "answered": {"turn": 3, "gives": True, "why": "Says on."}},
    {"turn": 2, "summary": "Asks again", "kind": "step", "serves": 1},
    {"turn": 3, "summary": "Asks once more", "kind": "step", "serves": 2},
    {"turn": 4, "summary": "Filed it", "kind": "wrap-up", "serves": 1}]}


def test_a_question_is_as_the_judge_found_the_latest_reply_of_its_run():
    """DETAILS P9.10.4: in the conversation as it went, as the analysis judged the reply that ends its run (each reply
    a step followed gave nothing); in a replay, as the judge found the latest reply of its run that was said. A step
    under it is met once a reply before it gave the answer, and its wrap-up once the root was met."""
    goals = measures.goals_of(QUESTION)
    home = view({FAN: "on"})
    assert measures.run_of(goals, goals[0]) == [1, 2, 3]
    assert measures.as_answered(goals) == {"1:0": [{"turn": 1, "gives": False}, {"turn": 2, "gives": False},
                                                   {"turn": 3, "gives": True}]}
    went = measures.judge(goals, home, [1, 2, 3, 4])
    assert went["goals"] == {"1:0": "met", "2:1": "unmet", "3:2": "unmet", "4:3": "unmet"}
    first = {"1:0": [{"turn": 1, "gives": True}]}  # Right first time: what hangs on it is met.
    assert measures.judge(goals, home, [1, 4], {}, (), first)["goals"] == {
        "1:0": "met", "2:1": "met", "3:2": "met", "4:3": "met"}
    wrong = {"1:0": [{"turn": 1, "gives": False}, {"turn": 2, "gives": False}, {"turn": 3, "gives": False}]}
    assert measures.judge(goals, home, [1, 2, 3, 4], {}, (), wrong)["goals"] == {
        "1:0": "unmet", "2:1": "unmet", "3:2": "unmet", "4:3": "unmet"}
    assert measures.judge(goals, home, [1, 4], {}, ())["goals"]["1:0"] == "indeterminate"  # No reply judged.


def test_a_step_under_a_question_is_said_only_while_no_reply_of_its_run_so_far_gave_the_answer():
    goals = measures.goals_of(QUESTION)
    home = view({FAN: "on"})
    assert measures.needed(goals, home, 2, (), {}, {"1:0": [{"turn": 1, "gives": False}]})
    assert not measures.needed(goals, home, 2, (), {}, {"1:0": [{"turn": 1, "gives": True}]})
    assert measures.needed(goals, home, 2, (), {}, {})  # Not judged yet: open.
    later = {"1:0": [{"turn": 1, "gives": False}, {"turn": 2, "gives": True}]}
    assert not measures.needed(goals, home, 3, (), {}, later)


def test_a_question_the_baseline_answered_otherwise_at_any_reply_of_its_run_is_set_aside():
    """DETAILS P6.6.2: a reply varies run to run, so a baseline whose verdict differs from the conversation's at any
    reply of the root's run (right where the conversation was wrong, or the other way) can't test it; nor one the
    conversation couldn't tell."""
    goals = measures.goals_of(QUESTION)
    alike = {"1:0": [{"turn": 1, "gives": False}, {"turn": 2, "gives": False}, {"turn": 3, "gives": True}]}
    assert measures.answered_otherwise(goals, alike) == set()
    right_first = {"1:0": [{**alike["1:0"][0], "gives": True}, *alike["1:0"][1:]]}
    assert measures.answered_otherwise(goals, right_first) == {"1:0"}
    assert measures.answered_otherwise(goals, {"1:0": [*alike["1:0"][:2], {"turn": 3, "gives": False}]}) == {"1:0"}
    untold = measures.goals_of({**QUESTION, "goals": [{**QUESTION["goals"][0], "reached": None},
                                                      *QUESTION["goals"][1:]]})
    assert measures.answered_otherwise(untold, alike) == {"1:0"}


def test_an_action_is_judged_on_its_own_turns_by_device_whatever_the_words():
    """DETAILS P9.10.5: an announce is met when something was spoken on every device it's for, whatever the words,
    over the turns of its own tree from its turn on: another request's turns between don't cut it short, and another
    request's calls don't count for it."""
    speaker, voice = "media_player.speaker", "assist_satellite.voice"
    broadcast = lambda message, *reached: {"tool": "HassBroadcast", "args": {"message": message}, "result": {
        "data": {"data": {"success": [{"id": e} for e in reached], "failed": []}}}}
    home = view({speaker: "idle"})
    home.source["entities"] = {speaker: {"device_id": "d1"}, voice: {"device_id": "d1"}}
    home.calls, home.scripts_seen = {1: [broadcast("dinner is ready")], 2: [], 3: [broadcast("dinner is ready", voice)],
                                     4: [broadcast("dinner is ready", voice)]}, False
    said = {"turn": 1, "summary": "Dinner", "kind": "action", "outcome": "announce", "entity_ids": [speaker]}
    asked = {"turn": 2, "summary": "Is it on", "kind": "fact", "fact": "Its state"}
    again = {"turn": 3, "summary": "Again", "kind": "step", "serves": 1}
    goals = measures.goals_of({"turns": 3, "goals": [said, asked, again]})
    assert measures.judge(goals, home, [1, 2, 3], {})["goals"]["1:0"] == "met"  # Spoken at turn 3, its own step.
    alone = measures.goals_of({"turns": 4, "goals": [said, asked, {**again, "turn": 4, "serves": 2}]})
    assert measures.judge(alone, home, [1, 2, 4], {})["goals"]["1:0"] == "unmet"  # Turn 4 is the question's.
    for said in ("Everyone, dinner is ready!", "Come and eat"):  # Whatever the words (Kai, 2026-10-30).
        home.calls[3] = [broadcast(said, voice)]
        assert measures.judge(goals, home, [1, 2, 3], {})["goals"]["1:0"] == "met", said


def test_a_step_or_a_filing_under_an_action_reads_whether_it_was_spoken_before_its_turn():
    """DETAILS P6.4 for actions: a step asking again for an announcement is met once something was spoken on its
    devices before the step's turn, so a replay with the change doesn't say it; a filing under it is met once it was
    spoken and nothing else of its request is open, an action asked beside it included."""
    speaker, porch = "media_player.speaker", "media_player.porch"
    said_on = lambda *reached: {"service": "tts.speak", "message": "dinner is ready", "targets": list(reached)}  # noqa
    home = view({speaker: "idle", porch: "idle"})
    home.calls, home.scripts_seen = {1: [], 2: [said_on(speaker)], 3: []}, True
    action = {"turn": 1, "summary": "Dinner", "kind": "action", "outcome": "announce", "entity_ids": [speaker]}
    again = {"turn": 2, "summary": "Again", "kind": "step", "serves": 1}
    filed = {"turn": 3, "summary": "Filed it", "kind": "wrap-up", "serves": 1}
    goals = measures.goals_of({"turns": 3, "goals": [action, again, filed]})
    replay = measures.judge(goals, home, [1, 2, 3], {})
    assert replay["goals"]["1:0"] == "met" and replay["goals"]["2:1"] == "unmet"  # Not spoken before turn 2.
    assert replay["goals"]["3:2"] == "met" and not measures.needed(goals, home, 3, conduct={})
    home.calls = {1: [said_on(speaker)]}
    assert not measures.needed(goals, home, 2, conduct={})  # Spoken at once: no need to ask again.
    part = {**action, "summary": "And the porch", "entity_ids": [porch], "serves": 1}
    goals = measures.goals_of({"turns": 3, "goals": [action, part, again, filed]})
    assert measures.judge(goals, home, [1, 3], {})["goals"]["3:3"] == "unmet"  # The porch part is still open.
    home.calls = {1: [said_on(speaker, porch)]}
    assert measures.judge(goals, home, [1, 3], {})["goals"]["3:3"] == "met"


def test_a_replays_view_hears_what_a_script_spoke_so_a_script_that_spoke_nothing_is_unmet_not_untold():
    """DETAILS P9.10.5: a replay's sandbox logs what scripts speak, so a script Assist ran in a replay is told by
    what it spoke: any words meet the action, and nothing spoken leaves it unmet, where a recording couldn't tell."""
    speaker = "media_player.speaker"
    turns = recorded_turns({speaker: "idle"})
    recorded = view({speaker: "idle"})
    recorded.recording = {"state_changes": []}
    ran = [{"role": "assistant", "tool_calls": [{"id": "c1", "tool_name": "script__dinner_call", "tool_args": {}}]},
           {"role": "tool_result", "tool_call_id": "c1", "tool_name": "script__dinner_call", "tool_result": {}}]
    said = lambda *words: [{"turn": 1, "transitions": [], "output": ran, "spoken": [  # noqa: E731
        {"service": "tts.speak", "message": w, "targets": [speaker]} for w in words]}]
    action = {"turn": 1, "summary": "Dinner", "kind": "action", "outcome": "announce", "entity_ids": [speaker]}
    goals = measures.goals_of({"turns": 1, "goals": [action]})
    for words, judged in ((("Dinner's on",), "met"), ((), "unmet"), (("",), "unmet")):  # Blank: nothing heard.
        home = measures.replay_view(turns, recorded, said(*words))
        assert home.scripts_seen and home.calls[1][0]["tool"] == "script__dinner_call"
        assert measures.judge(goals, home, [1], {})["goals"]["1:0"] == judged


def test_a_chime_or_a_legacy_engines_say_announces_whatever_it_carries():
    """Kai, 2026-10-30: any speech or audio sent to the devices announces, its content not judged for now: a chime a
    speaker played as an announcement, or a legacy engine's say, meets the action (analyzer.carries, a stub that says
    yes); nothing at all doesn't."""
    from custom_components.assist_improver import analyzer
    speaker, chime = "media_player.speaker", "media-source://media_source/local/chime.mp3"
    turns = recorded_turns({speaker: "idle"})
    recorded = view({speaker: "idle"})
    recorded.recording = {"state_changes": []}
    action = {"turn": 1, "summary": "Dinner", "kind": "action", "outcome": "announce", "entity_ids": [speaker]}
    goals = measures.goals_of({"turns": 1, "goals": [action]})
    for spoke, judged in (({"service": "media_player.play_media", "message": "", "media": chime}, "met"),
                          ({"service": "tts.cloud_say", "message": "Dinner's on"}, "met"),
                          ({"service": "tts.cloud_say", "message": ""}, "unmet")):
        home = measures.replay_view(turns, recorded, [{"turn": 1, "transitions": [], "output": [], "spoken": [
            {**spoke, "targets": [speaker]}]}])
        assert measures.judge(goals, home, [1], {})["goals"]["1:0"] == judged, spoke
    assert analyzer.carries(action, {"message": "", "media": chime}) is True


def test_what_hangs_on_an_action_a_recording_cant_tell_is_left_out_with_it():
    """Assist ran a script at turn 1, which a recording doesn't see into, and the user asked again at turn 2, then
    filed it: the action can't be told in the conversation, so neither can the step towards it nor the filing under
    it, and a replay that tells them doesn't count as going otherwise."""
    speaker = "media_player.speaker"
    ran = {"tool": "script__dinner_call", "args": {}, "result": {"data": {}}}
    spoke = {"service": "tts.speak", "message": "dinner is ready", "targets": [speaker]}
    conversation, replay = view({speaker: "idle"}), view({speaker: "idle"})
    conversation.calls, conversation.scripts_seen = {1: [ran], 2: [ran], 3: []}, False
    replay.calls, replay.scripts_seen = {1: [ran, spoke], 2: [ran, spoke], 3: []}, True
    goals = measures.goals_of({"turns": 3, "goals": [
        {"turn": 1, "summary": "Dinner", "kind": "action", "outcome": "announce", "entity_ids": [speaker]}, step(2, 1),
        {"turn": 3, "summary": "Filed it", "kind": "wrap-up", "serves": 1}]})
    went = measures.judge(goals, conversation, [1, 2, 3], measures.CONVERSATION)
    replayed = measures.judge(goals, replay, [1, 2, 3], {})
    assert measures.told_by(goals, {"1:0"}) == {"1:0", "2:1", "3:2"}
    assert measures.went_as_it_went(replayed, went, leave_out={"1:0"})  # The step and filing differ...
    assert measures.went_as_it_went(replayed, went, leave_out=measures.told_by(goals, {"1:0"})) is None  # ...aside.


def test_a_test_branches_at_the_first_turn_of_the_earliest_root_with_trouble_an_unmet_part_of_it_too():
    """DETAILS P6.2, P6.8: any goal unmet is trouble, filed under its root, so a part asked for in turn 1 that the
    baseline missed puts the branch there, though a later root took another turn; with it met, the branch is that
    later root's first turn."""
    goals = measures.goals_of({"turns": 3, "goals": [
        state(1, LAMP, "on"), state(1, FAN, "on", serves=1), state(2, HEATER, "off"),
        state(3, HEATER, "off", serves=2)]})
    missed = view({LAMP: "off", FAN: "off", HEATER: "on"}, (LAMP, "off", "on", 1), (HEATER, "on", "off", 3))
    before = measures.judge(goals, missed, [1, 2, 3])
    assert before["goals"]["1:1"] == "unmet" and before["counted"]["2:2"] == [2, 3]
    assert measures.branch(goals, before, missed) == 1
    assert measures.branch(goals, before, missed, aside={"1:1"}) == 2  # Set aside: no trouble of turn 1's.
    met = view({LAMP: "off", FAN: "off", HEATER: "on"}, (LAMP, "off", "on", 1), (FAN, "off", "on", 1),
               (HEATER, "on", "off", 3))
    assert measures.branch(goals, measures.judge(goals, met, [1, 2, 3]), met) == 2
