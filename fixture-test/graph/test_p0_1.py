"""Layer 2 of the test graph under P0.1: a conversation goes in and an improvement comes out that makes its goals
easier to fulfil.

"Easier" holds only if the improvement's test compares the right things: every goal of that conversation; without the
change, the conversation as it actually went, on the household's Assist as it was then; with it, that same
conversation with only the change, on their Assist as accepting the improvement leaves it. Each test checks one of
those. What counts as easier (the measures) is P0.1.1's; how Simulate uses its tests is P0.9's.
"""

from household import (FRICTION, GOAL_STATES, PARTLY, SIDE_EFFECT_AND_REPEAT, THANKS_AND_NOTE, UNMET,
                       UNMET_BESIDE_INDETERMINATE, Household)
from spec_graph import refines

BREAKS_THE_ONE_THAT_WENT_FINE = "for the goal in trouble, proposes only fixes that break the goal that went fine"
GOES_UNLIKE_IT_WENT = "replays it without the change leaving its goal unmet, though the conversation met it"


def improvement_for(home, talk):
    [improvement] = home.improvements(talk)
    return improvement


def met(replay):
    """The goals a replay met, as its test judged them."""
    return {goal for goal, state in replay.goals.items() if state == "met"}


@refines("P0.1")
def test_an_improvements_test_judges_every_goal_of_its_conversation_so_a_fix_cannot_trade_away_one_that_went_fine():
    """"Its goals" are all the goals the analysis found in the conversation, settled before the test (DETAILS
    P6.1.12): the goals of a "thanks" and of a note sent with it (P0.3), and a root goal that went fine (DETAILS P6.1,
    P6.8). Each is judged without the change and with it, so a fix for the goal in trouble that breaks the one that
    went fine is no improvement (DETAILS P6.1.3). A revision note is a turn with a goal too (P0.3), so the revised
    version's test judges it beside the rest. What state a thanks or a note gets in a replay is the next layer's."""
    home = Household()
    talk = home.converse(THANKS_AND_NOTE)
    home.run()
    test = improvement_for(home, talk).test
    assert set(test.before.goals) == set(test.after.goals) == set(home.analysis(talk).goals)

    home = Household()
    talk = home.converse(FRICTION)
    home.run()
    home.decide(improvement_for(home, talk), "revise", "only the left door")
    tree = home.analysis(talk)
    assert tree.turns[-1].goals  # the revision note's goal
    [revised] = [version for version in home.improvements(talk) if version.test in home.tests(talk, round=2)]
    test = revised.test
    assert set(test.before.goals) == set(test.after.goals) == set(tree.goals)

    home = Household()
    talk = home.converse(PARTLY)
    home.run()
    test = improvement_for(home, talk).test
    assert set(test.before.goals) == set(test.after.goals) == set(home.analysis(talk).goals)
    assert met(test.before) <= met(test.after)  # the root goal that went fine is met with the change too

    home = Household()
    home.stand_in("Propose", BREAKS_THE_ONE_THAT_WENT_FINE)
    talk = home.converse(PARTLY)
    home.run()
    tests = home.tests(talk)
    assert tests and not any(met(test.before) <= met(test.after) for test in tests)
    assert home.improvements(talk) == []


@refines("P0.1")
def test_without_the_change_the_test_goes_as_the_conversation_went_or_no_improvement_comes_out():
    """Easier means easier than it was for the household: the test's before is the conversation replayed without the
    change, and it must go as the conversation went (DETAILS P6.2.1, P6.6, P6.6.1). Here the replay without the change
    leaves unmet a goal the conversation met. The fix would still look easier than that replay, so only comparing it
    with how the conversation went keeps an improvement from coming out (DETAILS P6.6.5). Reading: "went the same" is
    checked here by each goal's state; the same turns and side effects are left to the next layer."""
    home = Household()
    talk = home.converse(FRICTION)
    home.run()
    test = improvement_for(home, talk).test
    assert test.before.goals == {goal: goal.state for goal in home.analysis(talk).goals}

    home = Household()
    home.stand_in("Simulate", GOES_UNLIKE_IT_WENT)
    talk = home.converse(FRICTION)
    home.run()
    assert home.improvements(talk) == []


@refines("P0.1")
def test_with_the_change_the_test_replays_the_same_conversation_from_the_same_home_and_time_in_the_users_own_words():
    """The improvement is what makes it easier only if nothing else differs between the two replays. With the change,
    the replay starts from the same home and time as without it (the recorded ones, DETAILS P6.2.4, P6.2.5), and says
    only the user's recorded turns, unchanged and in their order, never a simulated user's (DETAILS P6.5). It may leave
    out turns no longer needed, while the replay without the change says them all (DETAILS P6.4, P6.5.1)."""
    home = Household()
    talk = home.converse(FRICTION)
    home.run()
    test = improvement_for(home, talk).test
    turns = home.analysis(talk).turns
    assert test.before.said == turns
    assert test.after.said == [turn for turn in turns if turn in test.after.said]
    assert test.after.started_from == test.before.started_from
    assert test.after.easier_than(test.before)


@refines("P0.1")
def test_the_test_without_the_change_runs_on_the_assistant_as_it_is_at_the_run_not_as_it_was_during_the_conversation():
    """Easier than it is for the household now: the before says the conversation again to their Assist as it's set up
    at the run, not as it was when they had the conversation (DETAILS P6.2.2, P6.2.4; Kai, 2026-11-05: "replays should
    use the whole setup as it is today"), which the improvement keeps (DETAILS P2.4.3). Here the household accepts
    another conversation's improvement before this one is run."""
    home = Household()
    talk = home.converse(FRICTION)
    as_it_was = home.config()
    other = home.converse(UNMET)
    home.run("picked", picked=[other])
    home.decide(improvement_for(home, other), "accept")
    as_it_is = home.config()
    home.run("picked", picked=[talk])
    assert as_it_is != as_it_was
    assert improvement_for(home, talk).test.before.config == as_it_is


@refines("P0.1")
def test_accepting_the_improvement_at_your_review_leaves_the_assistant_exactly_as_its_test_ran_with_the_change():
    """What waits at Your review is what was tested: accepting it leaves the household's Assist as the test's replay
    with the change ran (DETAILS P2.4.3). Nothing changed since the conversation here; a household that changed what
    the improvement changes meets a conflict instead (P0.7)."""
    home = Household()
    talk = home.converse(FRICTION)
    home.run()
    improvement = improvement_for(home, talk)
    assert improvement in home.review_queue()
    home.decide(improvement, "accept")
    assert home.config() == improvement.test.after.config


# Layer 3: children of the tests above, each refining its parent by name. The test on the assistant as it is, and the
# one on accepting, are each one scenario already, so they have none.

EXTRA_TURN_GONE = ("replays it without the change meeting its goal in the first turn, though the conversation took an "
                   "extra turn")
SIDE_EFFECT_GONE = "replays it without the change leaving no side effect, though the conversation left one"
ASKS_SOMETHING_NEW = ("replays it with the change asking the user, before it acts, something the conversation never "
                      "answered")


def unmet_after_met(test):
    """The goals met without the change that are unmet with it."""
    return {goal for goal in test.before.goals_met if test.after.goals[goal] == "unmet"}


def helps(test, root):
    """Whether the change does something for a root goal, in P0.1.1's measures: met with it, and not met without it,
    or met in fewer turns, or met leaving fewer side effects."""
    before, after = test.before, test.after
    return root in after.goals_met and (root not in before.goals_met
                                        or after.turns_per_root_goal[root] < before.turns_per_root_goal[root]
                                        or len(after.side_effects_left) < len(before.side_effects_left))


def in_order(said, turns):
    """Whether a replay said only some of the conversation's turns, unchanged and in their order."""
    return said == [turn for turn in turns if turn in said]


@refines("test_an_improvements_test_judges_every_goal_of_its_conversation_so_a_fix_cannot_trade_away_one_that_went_fine")
def test_the_thanks_and_the_note_are_judged_in_both_replays_and_no_goal_met_without_the_change_is_unmet_with_it():
    """The thanks and the note sent with the conversation are turns whose goals sit under the request's root goal
    (P0.3); the test judges them in both replays like any other goal (DETAILS P6.1, P6.1.12). The thanks is met by
    saying it (DETAILS P2.3.5), so it went fine. Reading: with the change a turn may no longer be needed, and its
    goal is met then, a step whose goal was met before it (DETAILS P6.4); which turns a replay says is the code's.
    What holds either way: no goal met without the change is unmet with it (DETAILS P6.1.3)."""
    home = Household()
    talk = home.converse(THANKS_AND_NOTE)
    home.run()
    tree = home.analysis(talk)
    request, correction, thanks, note = tree.turns
    test = improvement_for(home, talk).test
    assert set(test.before.goals) == set(test.after.goals) == set(tree.goals)
    for goal in [*thanks.goals, *note.goals]:
        assert {test.before.goals[goal], test.after.goals[goal]} <= set(GOAL_STATES)
    assert thanks.goals and all(test.before.goals[goal] == "met" for goal in thanks.goals)
    assert unmet_after_met(test) == set()


@refines("test_an_improvements_test_judges_every_goal_of_its_conversation_so_a_fix_cannot_trade_away_one_that_went_fine")
def test_a_fix_for_the_goal_in_trouble_that_leaves_the_one_that_went_fine_unmet_is_not_validated():
    """Judging every goal is what catches the trade. The control: Simulate tests the conversation for its goal in
    trouble, yet judges the root goal that went fine too, though no fix targets it (DETAILS P6.8), and a fix that keeps
    every goal met is validated. Here each fix the proposer offers does something for the goal in trouble, yet leaves
    unmet a goal met without the change, so no test of one is validated (DETAILS P6.1.3), and no improvement comes
    out."""
    home = Household()
    talk = home.converse(PARTLY)
    home.run()
    test = improvement_for(home, talk).test
    assert set(test.before.goals) == set(test.after.goals) == set(home.analysis(talk).goals)
    assert test.before.goals_met <= test.after.goals_met and test.validated

    home = Household()
    home.stand_in("Propose", BREAKS_THE_ONE_THAT_WENT_FINE)
    talk = home.converse(PARTLY)
    home.run()
    roots = home.analysis(talk).roots
    tests = home.tests(talk)
    assert tests
    for test in tests:
        assert any(helps(test, root) for root in roots)  # it does something for the goal in trouble
        assert unmet_after_met(test) and not test.validated  # but loses the one that went fine
    assert home.improvements(talk) == []


@refines("test_without_the_change_the_test_goes_as_the_conversation_went_or_no_improvement_comes_out")
def test_without_the_change_the_test_gives_every_goal_the_state_the_analysis_gave_it_unmet_and_indeterminate_too():
    """Going as it went, goal by goal: without the change, each goal comes out met, unmet or indeterminate as the
    analysis judged it in the conversation. The replay without the change needn't meet the goals (DETAILS P6.6.3): a
    goal Assist missed stays unmet. Reading: a goal that couldn't be told stays indeterminate, since the same judge
    hears the same recorded words (DETAILS P6.1.12)."""
    for kind in (FRICTION, UNMET_BESIDE_INDETERMINATE):
        home = Household()
        talk = home.converse(kind)
        home.run()
        tree = home.analysis(talk)
        before = improvement_for(home, talk).test.before
        assert before.goals == {goal: goal.state for goal in tree.goals}
    assert {"unmet", "indeterminate"} <= set(before.goals.values())  # the second conversation's


@refines("test_without_the_change_the_test_goes_as_the_conversation_went_or_no_improvement_comes_out")
def test_a_replay_without_the_change_that_misses_a_goal_the_conversation_met_leaves_every_test_of_a_change_unvalidated():
    """The fix would look easier than such a replay, since it meets the goal that replay missed; only comparing with
    how the conversation went stops it, and it stops at validation, not later on the way to the household (DETAILS
    P6.6.5). The control: the replay without the change goes as it went, and the fix's test is validated."""
    home = Household()
    talk = home.converse(FRICTION)
    home.run()
    assert improvement_for(home, talk).test.validated

    home = Household()
    home.stand_in("Simulate", GOES_UNLIKE_IT_WENT)
    talk = home.converse(FRICTION)
    home.run()
    assert not any(test.validated for test in home.tests(talk))
    assert home.improvements(talk) == []


@refines("test_without_the_change_the_test_goes_as_the_conversation_went_or_no_improvement_comes_out")
def test_without_the_change_the_extra_turn_still_happens_or_no_test_of_a_change_is_validated():
    """Going as it went, turn by turn: without the change, the root goal takes the turns it took in the conversation,
    here the request and its repeat (DETAILS P6.1.5, P6.6.1). The conversation also left a side effect, and the fix
    avoids both. A replay without the change that meets the goal in the first turn, still leaving the side effect,
    didn't go as the conversation went, so no test of a change is validated against it (DETAILS P6.6.5). The fix would
    still look easier than such a replay, since it leaves no side effect, however that replay counts its turns: only
    comparing with how the conversation went stops it. The control: the replay without the change goes as it went."""
    home = Household()
    talk = home.converse(SIDE_EFFECT_AND_REPEAT)
    home.run()
    [root] = home.analysis(talk).roots
    test = improvement_for(home, talk).test
    assert test.before.turns_per_root_goal == {root: 2} and test.validated
    assert test.gains == {"goals met", "turns per root goal", "side effects left"}  # The repeat closed, too.

    home = Household()
    home.stand_in("Simulate", EXTRA_TURN_GONE)
    talk = home.converse(SIDE_EFFECT_AND_REPEAT)
    home.run()
    assert not any(test.validated for test in home.tests(talk))
    assert home.improvements(talk) == []


@refines("test_without_the_change_the_test_goes_as_the_conversation_went_or_no_improvement_comes_out")
def test_without_the_change_the_side_effect_is_still_left_or_no_test_of_a_change_is_validated():
    """Going as it went, in side effects: without the change, the replay leaves a side effect where the conversation
    left one (DETAILS P6.6.1, P6.1.10). The conversation also repeated its request, and the fix avoids both. A replay
    without the change that leaves no side effect, still taking the repeat, didn't go as the conversation went, so no
    test of a change is validated against it (DETAILS P6.6.5; today's code marks it not reproducible). The fix would
    still look easier than such a replay, since it saves the repeat: only comparing with how the conversation went
    stops it. Reading: that a side effect is left is checked here; which device it is, is the code's."""
    home = Household()
    talk = home.converse(SIDE_EFFECT_AND_REPEAT)
    home.run()
    test = improvement_for(home, talk).test
    assert test.before.side_effects_left and test.validated
    assert test.gains == {"goals met", "turns per root goal", "side effects left"}  # The repeat closed, too.

    home = Household()
    home.stand_in("Simulate", SIDE_EFFECT_GONE)
    talk = home.converse(SIDE_EFFECT_AND_REPEAT)
    home.run()
    assert not any(test.validated for test in home.tests(talk))
    assert home.improvements(talk) == []


@refines("test_with_the_change_the_test_replays_the_same_conversation_from_the_same_home_and_time_in_the_users_own_words")
def test_both_replays_start_from_the_home_and_time_recorded_with_the_conversation_though_the_home_moved_on_since():
    """The same home and time is the one Your Assist recorded when the conversation started (DETAILS P6.2.4, P6.2.5),
    for the replay with the change as for the one without, never the home as it is at the run. The control: a later
    conversation started from another home and time, so the home moved on before the run."""
    home = Household()
    talk = home.converse(FRICTION)
    later = home.converse(UNMET)
    home.run()
    recorded = home.records(talk, round=1)["Your Assist"].started_from
    assert home.records(later, round=1)["Your Assist"].started_from != recorded
    test = improvement_for(home, talk).test
    assert test.before.started_from == test.after.started_from == recorded


@refines("test_with_the_change_the_test_replays_the_same_conversation_from_the_same_home_and_time_in_the_users_own_words")
def test_a_replay_never_answers_for_the_user_so_a_change_that_needs_a_new_answer_is_not_validated():
    """The user's own words only: a replay never says what the user didn't, so when the change has Assist ask, before
    it acts, something the conversation never answered, nothing answers for them: the replay is cut off there, Assist
    never gets to the goal, and the change is rejected (DETAILS P6.5, P6.5.2). An answer made up for the user wouldn't
    be one of the conversation's turns, so what the replay said can't show it; the goal left unmet does. The control:
    a change that needs no new answer replays in the user's turns, and is validated."""
    home = Household()
    talk = home.converse(FRICTION)
    home.run()
    turns = home.analysis(talk).turns
    test = improvement_for(home, talk).test
    assert test.before.said == turns and in_order(test.after.said, turns)
    assert test.validated and test.after.easier_than(test.before)

    home = Household()
    home.stand_in("Simulate", ASKS_SOMETHING_NEW)
    talk = home.converse(FRICTION)
    home.run()
    tree = home.analysis(talk)
    [root] = tree.roots
    tests = home.tests(talk)
    assert tests
    for test in tests:
        assert in_order(test.after.said, tree.turns)
        assert root not in test.after.goals_met and not test.validated
    assert home.improvements(talk) == []


@refines("test_with_the_change_the_test_replays_the_same_conversation_from_the_same_home_and_time_in_the_users_own_words")
def test_neither_replay_says_to_assist_a_note_sent_with_the_conversation_or_a_revision_note():
    """The user's own words are what they said to Assist. A note sent with the conversation, and a revision note, are
    turns with goals (P0.3), but were said to the improver, afterwards: saying them to Assist would replay another
    conversation, with hindsight (DETAILS P6.2.10). Reading: the parent's "the user's recorded turns" are the turns
    said to Assist. The control: the request is said in both replays."""
    home = Household()
    talk = home.converse(THANKS_AND_NOTE)
    home.run()
    request, correction, thanks, note = home.analysis(talk).turns
    test = improvement_for(home, talk).test
    for replay in (test.before, test.after):
        assert request in replay.said and note not in replay.said

    home = Household()
    talk = home.converse(FRICTION)
    home.run()
    home.decide(improvement_for(home, talk), "revise", "only the left door")
    turns = home.analysis(talk).turns
    request, revision = turns[0], turns[-1]
    [revised] = [version for version in home.improvements(talk) if version.test in home.tests(talk, round=2)]
    for replay in (revised.test.before, revised.test.after):
        assert request in replay.said and revision not in replay.said


# Layer 4: none. Each layer-3 test above is already one concrete scenario on the driver, or two, each in its own
# household with its own assert lines, so a failing line names the case and the part that broke. A child would rebuild
# the same household to check one of those lines, and none would be simpler to make pass:
# - the thanks and the note judged: one THANKS_AND_NOTE run; goals judged, their states, the thanks met, none lost.
# - the fix that loses the goal that went fine: PARTLY as it is (the control), then with BREAKS_THE_ONE_THAT_WENT_FINE.
# - every goal's state as analysed: the same steps for FRICTION and for UNMET_BESIDE_INDETERMINATE.
# - a replay without the change that misses a goal, loses the extra turn or loses the side effect: each is a control,
#   then one stand-in run.
# - the recorded device states and time: one household, two conversations, one run.
# - never answering for the user: a control, then ASKS_SOMETHING_NEW.
# - notes never said to Assist: a note sent with the conversation, then a revision note, each one assert line on what
#   the replays said. Which other turns a replay says is how replays work; the notes' goals being judged is
#   the test on the thanks and the note, not this one.
