"""Layer 2 under P0.1.1: what fulfilment and effort are, measure by measure.

For now, fulfilment is goals met; effort is turns per root goal and side effects left (Kai). Each test below checks one
part of that, the way a test of a fix records it: its replay without the change (before) and with it (after). A fix is
an improvement only if it's strictly better: better on one measure and worse on none, with no order between them (Kai,
2026-10-27). A replay says only the conversation's own recorded turns, so a fix can't add one.
"""

import json

from household import (CHIMED_UNASKED, DIMMED, FRICTION, HEATER_FAILED, HEATER_UNREACHABLE, IN_BETWEEN, NO_GAIN,
                       OBJECTED_AND_FILED, UNPRESSED,
                       SIDE_EFFECT_AND_REPEAT, SIDE_EFFECT_PUT_BACK, SIDE_EFFECT_PUT_BACK_BY_AUTOMATION,
                       SIDE_EFFECT_PUT_BACK_SIGNED_IN, SIDE_EFFECT_PUT_BACK_UNMARKED,
                       SIDE_EFFECT_UNDONE_ON_REQUEST, THANKS_AND_NOTE, THANKS_AND_OBJECTION,
                       TURN_FOR_SIDE_EFFECT, TWO_TURNS_FOR_SIDE_EFFECT, UNDONE_SIDE_EFFECT_AND_REPEAT, UNMET, UNMET_BESIDE_INDETERMINATE,
                       UNREACHABLE_AND_REPAIR, UNSPOKEN, UNSPOKEN_ASKED_AGAIN, UNSPOKEN_EVERYWHERE, WORDY,
                       Household)
from spec_graph import refines


def improvement_for(home, talk):
    [improvement] = home.improvements(talk)
    return improvement


def goals_in(tree, state):
    return {goal for goal in tree.goals if goal.state == state}


@refines("P0.1.1")
def test_a_goal_assist_left_unmet_and_the_fix_meets_counts_as_one_more_goal_met():
    """Goals met are the conversation's own goals (its analysis, P0.3). Without the change, the goals met are the ones
    the analysis judged met; with it, the ones it judged unmet are met too, and none is lost. An indeterminate goal is
    neither met nor unmet (P0.3), so it counts in neither: the second conversation has one beside the goal Assist
    missed."""
    for kind in (UNMET, UNMET_BESIDE_INDETERMINATE):
        home = Household()
        talk = home.converse(kind)
        home.run()
        tree = home.analysis(talk)
        met, unmet, indeterminate = (goals_in(tree, state) for state in ("met", "unmet", "indeterminate"))
        test = improvement_for(home, talk).test
        assert unmet and test.before.goals_met == met
        assert test.after.goals_met == met | unmet
        assert test.gains == {"goals met"}
    assert indeterminate and not indeterminate & test.after.goals_met  # the second conversation's


@refines("P0.1.1")
def test_a_note_assist_didnt_follow_and_the_fix_follows_counts_as_one_more_goal_met():
    """A note sent with the conversation is a goal too (P0.3), so meeting it is fulfilment though Assist met every goal
    said to it: the case of a reply that met the goal but for its length (DETAILS P6.1.9). Without the change the
    note's goal is unmet, as in the conversation; with it, met, and goals met is the test's only gain."""
    home = Household()
    talk = home.converse(WORDY)
    home.run()
    tree = home.analysis(talk)
    [note] = [turn for turn in tree.turns if turn.key == "note"]
    test = improvement_for(home, talk).test
    assert note.goals and set(tree.roots) <= test.before.goals_met and not set(note.goals) & test.before.goals_met
    assert test.after.goals_met == test.before.goals_met | set(note.goals)
    assert test.gains == {"goals met"}


@refines("P0.1.1")
def test_each_root_goal_counts_only_its_own_turns_so_a_request_said_in_between_is_not_one_of_them():
    """Turns are counted per root goal: a root goal's turns are the turns whose goals sit in its tree (DETAILS
    P6.1.5). Here the garage is asked for twice, with the porch light asked for in between; the fix saves the repeat,
    so the garage's root goal takes one turn fewer and the porch light's as many as before (and the repeat, a step the
    fix closes, is one more goal met)."""
    home = Household()
    talk = home.converse(IN_BETWEEN)
    home.run()
    garage, porch = home.analysis(talk).roots  # in the order they were first asked for
    test = improvement_for(home, talk).test
    assert test.before.turns_per_root_goal == {garage: 2, porch: 1}
    assert test.after.turns_per_root_goal == {garage: 1, porch: 1}
    assert test.gains == {"goals met", "turns per root goal"}


@refines("P0.1.1")
def test_a_root_goal_the_home_couldnt_reach_either_way_still_counts_the_turns_a_fix_saves():
    """Turns per root goal compare for a root goal met in both replays, and for one unmet in both because the home
    couldn't reach it (DETAILS P6.1.5, P6.1.6). Here the porch light was unavailable, so its goal is unmet with the
    change as without it; the fix saves the turn the user spent having Assist turn the fan back off. The saved turn is
    the test's gain, and the fix is validated. (A root the home could reach, unmet both ways, isn't compared: a change
    that gave up on it would save turns by abandoning the request; test_measures holds that.)"""
    home = Household()
    talk = home.converse(UNREACHABLE_AND_REPAIR)
    home.run()
    [root] = home.analysis(talk).roots
    test = improvement_for(home, talk).test
    assert test.before.goals[root] == test.after.goals[root] == "unmet"
    assert test.before.turns_per_root_goal == {root: 2} and test.after.turns_per_root_goal == {root: 1}
    assert test.gains == {"turns per root goal"} and test.validated


@refines("test_a_root_goal_the_home_couldnt_reach_either_way_still_counts_the_turns_a_fix_saves")
def test_a_command_that_failed_at_home_is_one_the_home_couldnt_do_and_the_proposer_is_told_it_failed():
    """The home couldn't do a root goal when, in every turn its tree took, its command failed at home or its device was
    unavailable, and the roles are told which (DETAILS P9.10.6). The heater's command failed at home both times it was
    asked for: the proposer is told it failed, not that the heater was unavailable, and a replay's sandbox refuses it
    again, the fix's script's call too, so the goal is unmet with the change as without it."""
    home = Household()
    talk = home.converse(HEATER_FAILED)
    home.run()
    [root] = home.analysis(talk).roots
    given = json.dumps(home.received("Propose", talk)[-1].input)
    assert "its command failed at home" in given and "was unavailable" not in given
    tests = home.tests(talk)
    assert tests and all(test.before.goals[root] == test.after.goals[root] == "unmet" for test in tests)


@refines("P0.1.1")
def test_an_objection_the_fix_closes_is_one_more_goal_met_though_the_home_couldnt_reach_the_goal_it_is_under():
    """What the household objected to is a goal under the goal it counts on (P0.3), met once no Assist turn the replay
    says before it shows it, as the judge finds one turn at a time (DETAILS P6.1.9): here Assist promised the
    unavailable heater would warm the room; the fix's reply says only what it did, so the objection is met with it and
    unmet without, though the heater stays unreachable both ways. Met before its turn, the objection's turn isn't said
    (DETAILS P6.4): a turn saved on a goal the home couldn't reach either way."""
    home = Household()
    talk = home.converse(HEATER_UNREACHABLE)
    home.run()
    tree = home.analysis(talk)
    [root] = tree.roots
    [objection] = [goal for goal in tree.goals if goal.kind == "conduct"]
    test = improvement_for(home, talk).test
    assert test.before.goals[root] == test.after.goals[root] == "unmet"
    assert test.before.goals[objection] == "unmet" and test.after.goals[objection] == "met"
    assert test.before.turns_per_root_goal == {root: 3} and test.after.turns_per_root_goal == {root: 2}
    assert test.gains == {"goals met", "turns per root goal"} and test.validated


@refines("P0.1.1")
def test_an_objection_said_with_a_thanks_is_judged_and_closing_it_is_a_goal_met_not_a_turn_saved():
    """One turn thanks Assist and objects to its reply: the objection is a goal of its own (no longer lost beside the
    thanks), the fix closes it, and the turn is said both ways for the thanks (DETAILS P2.3.5), so the gain is the
    goal met and no turn."""
    home = Household()
    talk = home.converse(THANKS_AND_OBJECTION)
    home.run()
    tree = home.analysis(talk)
    [root] = tree.roots
    [objection] = [goal for goal in tree.goals if goal.kind == "conduct"]
    test = improvement_for(home, talk).test
    assert test.before.goals[objection] == "unmet" and test.after.goals[objection] == "met"
    assert test.before.turns_per_root_goal == test.after.turns_per_root_goal == {root: 2}
    assert test.gains == {"goals met"}


@refines("P0.1.1")
def test_a_thanks_is_said_in_both_replays_so_it_counts_both_ways_and_is_never_a_turn_saved():
    """A thanks is met only by being said (DETAILS P2.3.5), so the replay with the change says it too, whatever else
    the fix saves, and its root goal counts it both ways (Kai, 2026-10-27): here the fix saves the correction and the
    thanks stays."""
    home = Household()
    talk = home.converse(THANKS_AND_NOTE)
    home.run()
    tree = home.analysis(talk)
    [root] = tree.roots
    [thanks] = [goal for goal in tree.goals if goal.kind == "thanks"]
    test = improvement_for(home, talk).test
    assert any(thanks in turn.goals for turn in test.after.said)
    assert test.before.goals[thanks] == test.after.goals[thanks] == "met"
    assert test.before.turns_per_root_goal[root] == 3 and test.after.turns_per_root_goal[root] == 2  # The correction.


@refines("P0.1.1")
def test_a_filing_turn_said_while_an_objection_stands_is_never_a_turn_saved():
    """Filing the trouble stays open while anything of its goal is (DETAILS P2.3.2, P6.4): here the fix leaves the
    fan alone, but what the household objected to is still open, so the filing turn is said with the change as
    without it, and the fix gains only the side effect it removes."""
    home = Household()
    talk = home.converse(OBJECTED_AND_FILED)
    home.run()
    [root] = home.analysis(talk).roots
    test = improvement_for(home, talk).test
    assert test.before.turns_per_root_goal == test.after.turns_per_root_goal == {root: 3}
    assert test.gains == {"side effects left"}


@refines("P0.1.1")
def test_a_side_effect_counts_only_while_it_is_left_so_avoiding_a_change_assist_undid_itself_gains_nothing():
    """A side effect left is a device Assist changed that no goal asked for, and that Assist didn't leave as it started
    (DETAILS P1.3.3, P6.1.10). Both conversations also have a repeated request, so a fix is tested either way; it
    saves the repeat and avoids the change. The control: Assist leaves the device changed, so avoiding it is a gain.
    The difference: Assist changes it back itself, in the same turn, so nothing was left and avoiding the change
    gains nothing."""
    home = Household()
    talk = home.converse(SIDE_EFFECT_AND_REPEAT)
    home.run()
    test = improvement_for(home, talk).test
    assert test.before.side_effects_left and not test.after.side_effects_left
    assert test.gains == {"goals met", "turns per root goal", "side effects left"}

    home = Household()
    talk = home.converse(UNDONE_SIDE_EFFECT_AND_REPEAT)
    home.run()
    test = improvement_for(home, talk).test
    assert not test.before.side_effects_left and not test.after.side_effects_left
    assert test.gains == {"goals met", "turns per root goal"}  # The repeat closed and saved: no side effect gone.


@refines("P0.1.1")
def test_a_fix_that_saves_a_turn_by_leaving_a_side_effect_is_not_easier_and_makes_no_improvement():
    """Strictly better or no improvement (Kai, 2026-10-27): a fix that cuts one measure by adding to another is
    worse on one, so neither replay is easier than the other. A fix can't add a turn (a replay says only the
    conversation's own), so the trade one can make is a turn saved for a side effect left. The control: saving a turn
    alone is an improvement."""
    for plain, traded, measure in [(UNREACHABLE_AND_REPAIR, TURN_FOR_SIDE_EFFECT, "turns per root goal")]:
        home = Household()
        talk = home.converse(plain)
        home.run()
        assert improvement_for(home, talk).test.gains == {measure}

        home = Household()
        talk = home.converse(traded)
        home.run()
        tests = home.tests(talk)
        assert tests and not any(test.after.easier_than(test.before) or test.before.easier_than(test.after)
                                 for test in tests)
        assert home.improvements(talk) == []


@refines("P0.1.1")
def test_a_tested_fix_that_moves_none_of_the_three_measures_is_not_easier_so_it_is_not_validated():
    """The fix is tested, so the measures, not a missing test, keep it from being an improvement: whatever else it
    changes, it moves none of goals met, turns per root goal or side effects left. The control: a fix that moves one."""
    home = Household()
    talk = home.converse(FRICTION)
    home.run()
    test = improvement_for(home, talk).test
    assert test.validated and test.after.easier_than(test.before)

    home = Household()
    talk = home.converse(NO_GAIN)
    home.run()
    tests = home.tests(talk)
    assert tests and not any(test.gains or test.validated or test.after.easier_than(test.before) for test in tests)
    assert home.improvements(talk) == []


# Layer 3: each refines one layer-2 test above, by its name.

@refines("test_a_goal_assist_left_unmet_and_the_fix_meets_counts_as_one_more_goal_met")
def test_a_fix_that_meets_the_one_goal_assist_missed_adds_just_that_goal_to_the_goals_met():
    """The plain case, with no indeterminate goal beside it. Without the change, the goals met are the ones the
    analysis judged met; with it, the goal Assist missed is met too, none is lost, and goals met is the test's only
    gain. Newly met is counted against the replay without the change (DETAILS P6.1.2)."""
    home = Household()
    talk = home.converse(UNMET)
    home.run()
    tree = home.analysis(talk)
    [missed] = goals_in(tree, "unmet")
    test = improvement_for(home, talk).test
    assert test.before.goals_met == goals_in(tree, "met")
    assert test.after.goals_met == test.before.goals_met | {missed}
    assert test.gains == {"goals met"}


@refines("test_a_goal_assist_left_unmet_and_the_fix_meets_counts_as_one_more_goal_met")
def test_a_message_a_broadcast_spoke_nowhere_is_unmet_and_a_fix_that_speaks_it_on_the_speaker_meets_it():
    """A goal is its outcome, whichever service gets there. Asked to say a message on the kitchen speaker, Assist
    broadcast it, which speaks only on voice satellites: the goal is unmet, in the analysis and in the replay without
    the change. The fix, a script that speaks it on the speaker (tts.speak), meets it in the replay with the change,
    where the sandbox hears what the script spoke."""
    home = Household()
    talk = home.converse(UNSPOKEN)
    home.run()
    tree = home.analysis(talk)
    [missed] = goals_in(tree, "unmet")
    test = improvement_for(home, talk).test
    assert test.before.goals[missed] == "unmet"
    assert test.after.goals_met == test.before.goals_met | {missed}
    assert test.gains == {"goals met"}


@refines("test_a_message_a_broadcast_spoke_nowhere_is_unmet_and_a_fix_that_speaks_it_on_the_speaker_meets_it")
def test_a_script_that_takes_the_message_from_assist_speaks_it_on_every_speaker_and_is_applied_taking_it():
    """A script may take text inputs, which Assist fills when it runs it (Kai, 2026-10-29: yes to script inputs). The
    fix speaks a message on every speaker, the message given by Assist: the replay hears it spoken there, so the goal
    is met. Accepted, the household's script takes the message too, as the test ran it."""
    home = Household()
    talk = home.converse(UNSPOKEN_EVERYWHERE)
    home.run()
    tree = home.analysis(talk)
    [missed] = goals_in(tree, "unmet")
    improvement = improvement_for(home, talk)
    assert improvement.test.before.goals[missed] == "unmet" and improvement.test.after.goals[missed] == "met"
    home.decide(improvement, "accept")
    [script] = [s for s in home.config()["scripts"].values() if s.get("fields")]
    assert script["fields"] == {"message": {"name": "Message", "description": "The words to say", "required": True,
                                            "selector": {"text": {}}}}
    assert home.config() == improvement.test.after.config


@refines("test_a_script_that_takes_the_message_from_assist_speaks_it_on_every_speaker_and_is_applied_taking_it")
def test_an_announcement_is_met_by_speech_on_every_speaker_whatever_its_words_so_asking_again_isnt_said():
    """A broadcast revision (Kai, 2026-10-30: "the outcome to measure here is whether
    or not the assistant was able to trigger a broadcast with words/audio of its choice ... not the content of those").
    Asked in reported speech, the broadcast reached no device and the user said it didn't work. The fix's script
    speaks words Assist chose, none of the user's, on every speaker: the announcement is met at turn 1, so the replay
    with the change ends there, and "That didn't work" isn't said."""
    home = Household()
    talk = home.converse(UNSPOKEN_ASKED_AGAIN)
    home.run()
    tree = home.analysis(talk)
    [missed] = [goal for goal in goals_in(tree, "unmet") if goal.parent is None]  # The step under it is unmet too.
    test = improvement_for(home, talk).test
    assert test.before.goals[missed] == "unmet" and [t.key for t in test.before.said] == [1, 2]
    assert test.after.goals[missed] == "met" and [t.key for t in test.after.said] == [1]
    assert test.gains >= {"goals met"}


@refines("test_a_goal_assist_left_unmet_and_the_fix_meets_counts_as_one_more_goal_met")
def test_a_setting_asked_for_is_part_of_the_goal_and_is_met_in_any_of_its_forms():
    """A device's destination is a target State: its state plus any setting the user asked for, a setting met in any
    of Home Assistant's forms of it, a number within 5% of its range (DETAILS P9.7.1). Asked for the lamp at half
    brightness, Assist turned it on undimmed: on, but not at half, so unmet. The fix's script turns it on at brightness
    128 of 255 where the goal asked for 50%: met."""
    home = Household()
    talk = home.converse(DIMMED)
    home.run()
    [missed] = goals_in(home.analysis(talk), "unmet")
    test = improvement_for(home, talk).test
    assert test.before.goals[missed] == "unmet" and test.after.goals[missed] == "met"
    assert test.gains == {"goals met"}


@refines("test_a_goal_assist_left_unmet_and_the_fix_meets_counts_as_one_more_goal_met")
def test_a_button_asked_to_be_pressed_is_met_by_a_call_that_reaches_it_a_scripts_own_among_them():
    """Something done that leaves no device in a new state is met by a call that reached it, read from what happened
    on both sides alike: the tool calls with their results and the call log, a script's own calls in it (DETAILS P9.7.6,
    P9.10.5). Asked to ring the chime, Assist couldn't: no call reached it, so unmet. The fix's script presses it: its
    own call reached the chime, so met."""
    home = Household()
    talk = home.converse(UNPRESSED)
    home.run()
    [missed] = goals_in(home.analysis(talk), "unmet")
    test = improvement_for(home, talk).test
    assert test.before.goals[missed] == "unmet" and test.after.goals[missed] == "met"
    assert test.gains == {"goals met"}


@refines("test_a_goal_assist_left_unmet_and_the_fix_meets_counts_as_one_more_goal_met")
def test_an_indeterminate_goal_is_neither_met_nor_unmet_in_either_replay_while_the_missed_goal_beside_it_is_met():
    """An indeterminate goal is neither met nor unmet (P0.3), so each replay judges it indeterminate, with the change
    as without it, and it's in neither replay's goals met. Reading: it's one the evidence can't show (DETAILS
    P9.10.2), which the replays, seeded with the recorded states, can't show either; not an unclear request, which would
    make the conversation a question for the household (DETAILS P9.9.6). The positive control, in the same
    conversation: the goal Assist missed is one more goal met."""
    home = Household()
    talk = home.converse(UNMET_BESIDE_INDETERMINATE)
    home.run()
    tree = home.analysis(talk)
    [unclear] = goals_in(tree, "indeterminate")
    unmet = goals_in(tree, "unmet")
    test = improvement_for(home, talk).test
    assert test.before.goals[unclear] == test.after.goals[unclear] == "indeterminate"
    assert unclear not in test.before.goals_met | test.after.goals_met
    assert unmet and test.before.goals_met == goals_in(tree, "met")
    assert test.after.goals_met == test.before.goals_met | unmet
    assert test.gains == {"goals met"}


# A lopsided trade: a product that adds or weighs the measures could tie a one-for-one trade, but not two turns for one
# side effect.


@refines("test_a_fix_that_saves_a_turn_by_leaving_a_side_effect_is_not_easier_and_makes_no_improvement")
def test_a_fix_that_saves_two_turns_but_leaves_one_side_effect_is_neither_easier_nor_harder_and_makes_no_improvement():
    """Two turns for one side effect. With the fix, the conversation's one root goal takes two turns fewer, and one
    device is left changed that no goal asked for and that was left alone without it (DETAILS P6.1.10).
    Neither replay is easier than the other (strictly better or not at all, Kai), so the fix isn't validated: two turns
    don't outweigh one side effect. The control: saving a turn without leaving anything is an improvement."""
    home = Household()
    talk = home.converse(UNREACHABLE_AND_REPAIR)
    home.run()
    assert improvement_for(home, talk).test.gains == {"turns per root goal"}

    home = Household()
    talk = home.converse(TWO_TURNS_FOR_SIDE_EFFECT)
    home.run()
    [root] = home.analysis(talk).roots
    tests = home.tests(talk)
    assert tests
    for test in tests:
        assert test.before.turns_per_root_goal[root] == test.after.turns_per_root_goal[root] + 2
        assert len(test.after.side_effects_left) == 1 and not test.before.side_effects_left
        assert not test.after.easier_than(test.before) and not test.before.easier_than(test.after)
        assert not test.validated
    assert home.improvements(talk) == []


def still_left_after_someone_else_puts_it_back(kind):
    """Assist turns the fan on beside the lamp, and a while after the conversation someone else turns it back off. One
    side effect is still left, as Assist left it (on, from off), on the lamp's goal, whose turn turned the fan on
    (DETAILS P9.12.4): the proposer is given it there. No replay plays what came after the conversation's last turn,
    so the replay without the change leaves it too, as the conversation did, and a fix that leaves the fan alone gains
    it."""
    home = Household()
    talk = home.converse(kind)
    home.run()
    [scenario] = home.received("Propose", talk)[-1].input["friction_scenarios"]
    [left] = scenario["side_effects_left"]
    assert (left["start"], left["end"], left["changed_in_turn"], left["for_goal_at_turn"]) == ("off", "on", 1, 1)
    test = improvement_for(home, talk).test
    assert len(test.before.side_effects_left) == 1 and not test.after.side_effects_left
    assert test.gains == {"side effects left"}


@refines("test_a_side_effect_counts_only_while_it_is_left_so_avoiding_a_change_assist_undid_itself_gains_nothing")
def test_a_buttons_last_press_is_never_a_side_effect_since_it_keeps_no_state_to_reach():
    """A side effect left is a device's settable state not left as it started; a button's last press is never one
    (DETAILS P9.12). The fix's script turns on the porch light Assist missed and presses the chime, which no goal asked
    for: the chime's state is the time it was last pressed, now changed, yet no side effect is left, so the goal met is
    the test's only gain and the fix is validated."""
    home = Household()
    talk = home.converse(CHIMED_UNASKED)
    home.run()
    [missed] = goals_in(home.analysis(talk), "unmet")
    test = improvement_for(home, talk).test
    assert test.after.goals[missed] == "met" and not test.after.side_effects_left
    assert test.gains == {"goals met"} and test.validated


@refines("test_a_side_effect_counts_only_while_it_is_left_so_avoiding_a_change_assist_undid_itself_gains_nothing")
def test_a_side_effect_put_back_by_no_one_signed_in_still_counts_on_the_goal_whose_turn_made_it():
    """Only how Assist left the device matters (Kai, 2026-11-02: "Any put-back counts"; DETAILS P9.12.5): anyone or
    anything else putting it back (a person, an automation, the device's own timer) doesn't clear it, since putting it
    back was work Assist caused. Here the fan is turned back off by no one signed in to Home Assistant, which is how it
    records a person at the switch or in the device's own app, the device's own timer and an automation set off by
    anything but Assist alike."""
    still_left_after_someone_else_puts_it_back(SIDE_EFFECT_PUT_BACK)


@refines("test_a_side_effect_counts_only_while_it_is_left_so_avoiding_a_change_assist_undid_itself_gains_nothing")
def test_a_side_effect_put_back_after_a_change_reported_without_assists_request_still_counts():
    """Anyone else putting it back doesn't clear it (DETAILS P9.12.5), even when the fan reports Assist's change
    without Assist's request on it, so only Assist's results name the change as Assist's: the put-back, after Assist's
    reply, is no one's carrying that change through, so it's someone else's."""
    still_left_after_someone_else_puts_it_back(SIDE_EFFECT_PUT_BACK_UNMARKED)


@refines("test_a_side_effect_counts_only_while_it_is_left_so_avoiding_a_change_assist_undid_itself_gains_nothing")
def test_a_side_effect_put_back_by_an_automation_assists_change_set_off_still_counts():
    """An automation putting it back doesn't clear it (Kai's approved rule, 2026-11-02: "an automation ... doesn't
    clear it"; DETAILS P9.12.5), even one Assist's own change set off, whose run Home Assistant records with Assist's
    request as its parent: here one turns the fan back off a while after Assist turned it on."""
    still_left_after_someone_else_puts_it_back(SIDE_EFFECT_PUT_BACK_BY_AUTOMATION)


@refines("test_a_side_effect_counts_only_while_it_is_left_so_avoiding_a_change_assist_undid_itself_gains_nothing")
def test_a_side_effect_put_back_by_someone_signed_in_still_counts_on_the_goal_whose_turn_made_it():
    """Any put-back counts (Kai, 2026-11-02, asking "why only "signed in" people?"; DETAILS P9.12.5): here someone
    signed in to Home Assistant turns the fan back off, in its app. Their change is theirs, never counted as Assist's
    (DETAILS P9.12.2), and Assist's is still left."""
    still_left_after_someone_else_puts_it_back(SIDE_EFFECT_PUT_BACK_SIGNED_IN)


@refines("test_a_side_effect_counts_only_while_it_is_left_so_avoiding_a_change_assist_undid_itself_gains_nothing")
def test_a_change_the_user_has_assist_put_back_counts_once_as_that_turn_and_leaves_no_side_effect():
    """Asking Assist to put it back counts as that turn instead (Kai, 2026-11-02; DETAILS P9.12.5), so each slip
    counts once: as a turn if the user asked Assist to undo it, otherwise as a side effect (the tests above). Assist
    turns the fan on beside the lamp, and the user has Assist turn it back off: a sub-goal of the lamp's goal (DETAILS
    P2.3.15), so the lamp's goal took two turns, and Assist left the fan as it started, so no side effect is left. The
    proposer is given none, and a fix that leaves the fan alone saves the turn and gains nothing in side effects."""
    home = Household()
    talk = home.converse(SIDE_EFFECT_UNDONE_ON_REQUEST)
    home.run()
    [root] = home.analysis(talk).roots
    [scenario] = home.received("Propose", talk)[-1].input["friction_scenarios"]
    assert "side_effects_left" not in scenario
    test = improvement_for(home, talk).test
    assert not test.before.side_effects_left and not test.after.side_effects_left
    assert test.before.turns_per_root_goal == {root: 2} and test.after.turns_per_root_goal == {root: 1}
    assert test.gains == {"turns per root goal"}


# Layer 4: none. Each layer-3 test above is one concrete scenario on the driver (one kind, one run, one test read),
# plus at most a control its layer-2 parent already holds. Its asserts run in the order the product works (each replay's
# measures, then the comparison, then validation and the improvement), so a failing line already names the mechanism,
# and every child would rebuild the same household and run the same pipeline to check one of those lines: none would
# be simpler to make pass. The comparison itself (easier_than) can only be reached through replays these kinds make.
