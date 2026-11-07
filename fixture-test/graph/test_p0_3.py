"""Layer 2 of the test graph under P0.3 (turns and goals): each test checks one narrower part of the rule.

Together: every kind of turn (said to Assist, a note sent with the conversation, a revision note) gets a goal, and no
analysis with a goalless turn is kept; a root goal is everything wanted at one time, never split or merged; a thanks
or a note hangs under a root goal, never is one; every sub-goal hangs from a goal of its own tree, and sub-goals nest;
what the household objected to is a sub-goal in their own words, about a turn of its goal's; and every goal, root or
sub-goal, has a state that says what happened to it.
"""

from household import (BOTH_DOORS, FILED, FRICTION, GOAL_STATES, HEATER_LEFT_OFF, NESTED, NO_GAIN, OBJECTED_AND_FILED,
                       THANKS_AND_NOTE, TROUBLE_IN_THE_MIDDLE, TWO_TIMES, UNCLEAR, UNMET,
                       UNMET_BESIDE_INDETERMINATE, Household)
from spec_graph import refines


def analyze_attempts(home, talk):
    return len(home.records(talk, round=1)["Analyze"].attempts)


def analysed(kind):
    home = Household()
    talk = home.converse(kind)
    home.run()
    return home.analysis(talk)


def all_sub_goals_under(goals, roots, tree):
    """Each goal is a sub-goal: not a root goal itself, its parent a goal of the same tree, its root goal in roots."""
    return all(goal not in tree.roots and goal.parent in tree.goals and goal.root in roots for goal in goals)


@refines("P0.3")
def test_a_note_sent_in_with_a_conversation_becomes_its_last_turn_with_a_goal_under_one_of_its_root_goals():
    """A conversation with two root goals, so the note has a choice; which one it's about is the analyzer's call."""
    home = Household()
    plain = home.converse(TWO_TIMES)
    home.run()
    noted = home.converse(TWO_TIMES)
    home.send_in(noted, note="close it again without asking next time")
    without, tree = home.analysis(plain), home.analysis(noted)
    assert len(tree.turns) == len(without.turns) + 1
    assert len(tree.roots) == len(without.roots) == 2
    note = tree.turns[-1]
    assert note.goals and all_sub_goals_under(note.goals, tree.roots, tree)


@refines("P0.3")
def test_the_households_answer_to_a_question_is_no_turn_of_its_conversation():
    """The answer tells the analyzer what was meant (P0.6); its conversation's turns stay those it lists."""
    home = Household()
    talk = home.converse(UNCLEAR)
    home.run()
    asked = [turn.key for turn in home.analysis(talk).turns]
    home.answer(talk, "the warm lights are the two lamps")
    # Positive control: analysed again with the answer, it can tell, so it asks nothing more.
    assert not [item for item in home.review_queue() if item.conversation == talk and item.question]
    assert [turn.key for turn in home.analysis(talk).turns] == asked


@refines("P0.3")
def test_a_revision_note_becomes_a_turn_of_its_conversation_with_a_goal_under_the_root_goal_it_revises():
    """A revision note is about the improvement, so the root goal it's about is the one the improvement is for; this
    conversation has one. The revision goes in at Propose (P0.7), and the note still gets its goal (one only about the
    improvement itself maps there with none: DETAILS P12.8.1)."""
    home = Household()
    talk = home.converse(FRICTION)
    home.run()
    before = home.analysis(talk)
    [root] = before.roots
    [improvement] = home.improvements(talk)
    home.decide(improvement, "revise", "only the left door")
    after = home.analysis(talk)
    assert len(after.turns) == len(before.turns) + 1 and after.roots == [root]
    revision = after.turns[-1]
    assert revision.goals and all_sub_goals_under(revision.goals, [root], after)


@refines("test_a_revision_note_becomes_a_turn_of_its_conversation_with_a_goal_under_the_root_goal_it_revises")
def test_a_revision_note_left_without_a_goal_or_hung_under_another_root_is_refused_and_the_retry_hangs_it_right():
    """DETAILS P12.8.1, P9.13.3: the round's fresh analysis gives the revision note its goal, under a root goal its
    improvement was for, the analyzer's call, checked by code. Only the porch light, asked for in the middle, had
    trouble: an answer that leaves the note without a goal, or hangs it under the garage, is refused, and the retry's,
    under the porch light, is kept; the round moves on. Control: answered right, it's kept at once."""
    for wrong_first, tries in ((None, 1), ("first leaves the revision note without a goal", 2),
                               ("first hangs the revision note under a goal its improvement wasn't for", 2)):
        home = Household()
        if wrong_first:
            home.stand_in("Analyze", wrong_first)
        talk = home.converse(TROUBLE_IN_THE_MIDDLE)
        home.run()
        opened, porch, closed = home.analysis(talk).roots
        [improvement] = home.improvements(talk)
        home.decide(improvement, "revise", "only the porch light")
        tree = home.analysis(talk)
        revision = tree.turns[-1]
        assert len(home.received("Analyze", talk)) == 1 + tries, wrong_first
        assert revision.goals and all_sub_goals_under(revision.goals, [porch], tree), wrong_first
        assert not home.place(talk).held and home.place(talk).round == 2


@refines("test_a_revision_note_becomes_a_turn_of_its_conversation_with_a_goal_under_the_root_goal_it_revises")
def test_a_revision_note_only_about_the_fix_adds_no_goal_and_reaches_the_proposer_in_full():
    """DETAILS P12.8.1: a revision note only about the improvement itself ("use a script, not a household line")
    reacts to the proposal, not to anything Assist did: the analyzer lists it apart, it adds no goal for a reply to be
    judged on, and the round goes on, the proposer reading the note in full."""
    home = Household()
    home.stand_in("Analyze", "reads the revision note as about the fix itself")
    talk = home.converse(FRICTION)
    home.run()
    [improvement] = home.improvements(talk)
    home.decide(improvement, "revise", "use a script, not a household line")
    tree = home.analysis(talk)
    note = tree.turns[-1]
    assert note.key == "revision:2" and note.goals == [] and len(home.received("Analyze", talk)) == 2
    assert home.place(talk).stage == "Your review" and not home.place(talk).held
    handed = home.received("Propose", talk)[-1].input
    assert handed["sent_back"]["note"] == "use a script, not a household line"


@refines("P0.3")
def test_an_analysis_that_leaves_a_turn_without_a_goal_is_never_kept_the_stage_retries_then_holds():
    """Only the analyzer can say what a thanks is for, so an answer that never gives it a goal fails the stage: after
    its retries the conversation is held at Analyze (P0.8) with no analysis kept, instead of moving on with a goalless
    turn."""
    home = Household()
    home.stand_in("Analyze", "first leaves the thanks without a goal")
    talk = home.converse(THANKS_AND_NOTE)
    home.run()
    assert analyze_attempts(home, talk) == 2 and not home.place(talk).held
    tree = home.analysis(talk)  # the retried answer is kept
    assert len(tree.turns) == 4 and all(turn.goals for turn in tree.turns)

    home = Household()
    home.stand_in("Analyze", "always leaves the thanks without a goal")
    talk = home.converse(THANKS_AND_NOTE)
    home.run()
    assert analyze_attempts(home, talk) > 1
    assert home.place(talk).stage == "Analyze" and home.place(talk).held
    assert home.analysis(talk) is None  # no goalless answer is kept as its analysis


@refines("P0.3")
def test_an_analysis_that_splits_one_request_per_device_is_refused_and_one_root_per_time_is_kept():
    """What's asked at one time is one world state, so one root goal however many devices it names: code sees two
    roots at one turn and refuses them. What's asked at different times is one root goal each; whether two requests
    were one is the analyzer's judgement (only a model can tell), so code keeps its answer as given."""
    home = Household()
    home.stand_in("Analyze", "first makes one goal per door")
    talk = home.converse(BOTH_DOORS)
    home.run()
    assert len(home.analysis(talk).roots) == 1 and analyze_attempts(home, talk) == 2

    for kind, roots in [(BOTH_DOORS, 1), (TWO_TIMES, 2)]:  # the right answer from the start is kept at once
        home = Household()
        talk = home.converse(kind)
        home.run()
        assert len(home.analysis(talk).roots) == roots and analyze_attempts(home, talk) == 1


@refines("P0.3")
def test_an_analysis_that_makes_the_thanks_or_the_note_a_root_goal_is_refused_and_retried():
    """Code knows which turn is the note (it was sent in with the conversation), so a root goal there is refused.
    Which turn is a "thanks" only the analyzer can tell (a model's judgement), so code doesn't tell; but a goal the
    analyzer itself calls a thanks, left as a root goal, is refused."""
    for wrong_first in ("first makes the thanks a root goal of its own", "first makes the note a root goal of its own"):
        home = Household()
        home.stand_in("Analyze", wrong_first)
        talk = home.converse(THANKS_AND_NOTE)
        home.run()
        tree = home.analysis(talk)
        [root] = tree.roots
        request, correction, thanks, note = tree.turns
        assert thanks.goals and note.goals and all_sub_goals_under([*thanks.goals, *note.goals], [root], tree)
        assert analyze_attempts(home, talk) == 2

    home = Household()  # the thanks and the note under the root from the start: kept at once
    talk = home.converse(THANKS_AND_NOTE)
    home.run()
    assert len(home.analysis(talk).roots) == 1 and analyze_attempts(home, talk) == 1


@refines("P0.3")
def test_every_sub_goal_hangs_from_a_goal_of_its_own_tree_and_a_sub_goal_can_serve_another_sub_goal():
    """A sub-goal exists only because of its parent. Turning the heater back on exists only because turning the fan off
    did, which exists only because of the lamp."""
    for kind in (THANKS_AND_NOTE, NESTED):
        tree = analysed(kind)
        sub_goals = [goal for goal in tree.goals if goal not in tree.roots]
        assert sub_goals and all_sub_goals_under(sub_goals, tree.roots, tree)

    [root] = tree.roots  # NESTED
    [heater] = [goal for goal in sub_goals if goal.parent not in tree.roots]
    assert heater.parent.parent == root and heater.root == heater.parent.root == root


@refines("P0.3")
def test_what_the_household_objected_to_is_a_conduct_goal_in_their_own_words_under_the_goal_it_is_about():
    """A sub-goal that reacts to what Assist said or did names it, in the household's own words (DETAILS P2.3.2): the
    note sent with the conversation and a revision note are conduct goals that quote it, under the root goal they're
    about, about Assist in general (P0.3; DETAILS P9.13)."""
    home = Household()
    talk = home.converse(THANKS_AND_NOTE)
    home.run()
    tree = home.analysis(talk)
    [root] = tree.roots
    [goal] = tree.turns[-1].goals
    assert (goal.kind, goal.quote, goal.about, goal.root) == ("conduct", talk.kind.note, "all", root)

    home = Household()
    talk = home.converse(FRICTION)
    home.run()
    [improvement] = home.improvements(talk)
    home.decide(improvement, "revise", "only the left door")
    tree = home.analysis(talk)
    [goal] = tree.turns[-1].goals
    assert (goal.kind, goal.quote, goal.about, goal.root) == ("conduct", "only the left door", "all", *tree.roots)


@refines("P0.3")
def test_any_goal_root_or_sub_goal_is_met_if_assist_reached_it_unmet_if_it_missed_and_indeterminate_while_unclear():
    """Unclear: the analyzer can't tell what the user wanted, so the goal can't be told while the question is open."""
    for kind, state in [(FRICTION, "met"), (UNMET, "unmet"), (UNCLEAR, "indeterminate")]:
        assert state in {goal.state for goal in analysed(kind).roots}
    assert {goal.state for goal in analysed(NESTED).goals} == {"met"}  # sub-goals Assist reached are met too
    tree = analysed(THANKS_AND_NOTE)  # the thanks and the note have a state like any goal
    assert len(tree.goals) > len(tree.roots) and {goal.state for goal in tree.goals} <= set(GOAL_STATES)


# Layer 3: each test below refines one layer-2 test above, by its function name.


def in_tree(goal, root):
    return goal == root or goal.root == root


def analysed_with(kind, behaviour):
    home = Household()
    home.stand_in("Analyze", behaviour)
    talk = home.converse(kind)
    home.run()
    return home.analysis(talk)


@refines("test_a_revision_note_becomes_a_turn_of_its_conversation_with_a_goal_under_the_root_goal_it_revises")
def test_a_revision_adds_its_note_as_the_last_turn_and_leaves_the_earlier_turns_and_goals_as_they_were():
    """A revision starts a new round at Propose (P0.7), whose first act reads the conversation afresh (DETAILS
    P12.8.1), and an earlier round's outcome stands (P0.6): the analysis gains the note's turn, last, and keeps every
    earlier turn, root goal and goal."""
    home = Household()
    talk = home.converse(FRICTION)
    home.run()
    before = home.analysis(talk)
    [improvement] = home.improvements(talk)
    home.decide(improvement, "revise", "only the left door")
    after = home.analysis(talk)
    assert after.turns[:-1] == before.turns and after.roots == before.roots
    assert set(before.goals) <= set(after.goals)


@refines("test_a_revision_note_becomes_a_turn_of_its_conversation_with_a_goal_under_the_root_goal_it_revises")
def test_a_revision_notes_goal_is_given_by_its_rounds_fresh_analysis_so_the_proposer_has_it():
    """DETAILS P12.8.1: a revision's round goes straight back in at Propose, whose first act is to read the
    conversation afresh, the note a turn after the others: that analysis, the round's own, gives the note its goal,
    and the proposer is given the note's turn with it."""
    home = Household()
    talk = home.converse(FRICTION)
    home.run()
    [improvement] = home.improvements(talk)
    home.decide(improvement, "revise", "only the left door")
    revision = home.analysis(talk).turns[-1]
    given = home.seen_by("Propose", talk, round=2)
    assert revision.goals and revision in given and all(goal in given for goal in revision.goals)
    assert "Analyze" in home.records(talk, round=2) and home.path(talk, round=2)[0] == "Propose"


@refines("test_a_revision_note_becomes_a_turn_of_its_conversation_with_a_goal_under_the_root_goal_it_revises")
def test_a_revision_note_hangs_under_the_root_goal_its_improvement_is_for_not_the_first_or_the_last_one_asked_for():
    """Only the porch light, asked for in the middle, had trouble, so the improvement is for its root goal. The note
    revising it hangs there, as a sub-goal whose parent is a goal of the tree: not under the first root goal, nor under
    the one the conversation ended on."""
    home = Household()
    talk = home.converse(TROUBLE_IN_THE_MIDDLE)
    home.run()
    opened, porch, closed = home.analysis(talk).roots  # in the order they were asked for
    [improvement] = home.improvements(talk)
    home.decide(improvement, "revise", "only the porch light")
    after = home.analysis(talk)
    revision = after.turns[-1]
    assert after.roots == [opened, porch, closed]
    assert revision.goals and all_sub_goals_under(revision.goals, [porch], after)


@refines("test_an_analysis_that_leaves_a_turn_without_a_goal_is_never_kept_the_stage_retries_then_holds")
def test_each_kind_of_turn_left_without_a_goal_gets_the_answer_refused_and_the_retry_maps_it_into_the_requests_tree():
    """A correction said to Assist, the thanks, and the note sent with the conversation. Without the rule each would
    pass without a goal: a correction counted as an extra turn, a thanks as not a request. A correction maps to the
    goal it makes more exact (DETAILS P2.2.1). The note too is refused, though here there's one
    root goal it could go under: which root goal a note is about is the analyzer's call, so code doesn't fill it in.
    The retried answer is kept with all four turns, and the conversation moves on: a refusal the retry fixes holds
    nothing."""
    for wrong_first in ("first leaves the correction without a goal", "first leaves the thanks without a goal",
                        "first leaves the note without a goal"):
        home = Household()
        home.stand_in("Analyze", wrong_first)
        talk = home.converse(THANKS_AND_NOTE)
        home.run()
        tree = home.analysis(talk)
        [root] = tree.roots
        assert analyze_attempts(home, talk) == 2 and not home.place(talk).held
        assert len(tree.turns) == 4
        assert all(turn.goals and all(in_tree(goal, root) for goal in turn.goals) for turn in tree.turns)

    home = Household()  # control: an answer with a goal for every turn is kept at once
    talk = home.converse(THANKS_AND_NOTE)
    home.run()
    assert analyze_attempts(home, talk) == 1


@refines("test_an_analysis_that_leaves_a_turn_without_a_goal_is_never_kept_the_stage_retries_then_holds")
def test_when_every_answer_leaves_a_turn_without_a_goal_it_is_held_at_analyze_for_failures_and_nothing_moves_on():
    """After the retries within the run it's held for failures (P0.8), and no part of the answer moves on. Control:
    answered in full, the same conversation is analysed and reaches Propose."""
    home = Household()
    talk = home.converse(THANKS_AND_NOTE)
    home.run()
    assert home.analysis(talk) and home.ideas(talk)

    home = Household()
    home.stand_in("Analyze", "always leaves the thanks without a goal")
    talk = home.converse(THANKS_AND_NOTE)
    home.run()
    place = home.place(talk)
    assert place.stage == "Analyze" and place.held and place.held.cause == "failures"
    assert home.analysis(talk) is None and home.ideas(talk) == []


@refines("test_every_sub_goal_hangs_from_a_goal_of_its_own_tree_and_a_sub_goal_can_serve_another_sub_goal")
def test_an_answer_that_hangs_a_sub_goal_from_a_goal_outside_its_tree_is_refused_and_retried():
    """The answer is checked against the recording, each sub-goal linking to a goal it serves, and refused with the
    reasons (DETAILS P9.15); no goal links to another conversation (DETAILS P9.5.1)."""
    for kind in (THANKS_AND_NOTE, NESTED):
        home = Household()
        home.stand_in("Analyze", "first hangs a sub-goal from a goal that isn't in the analysis")
        talk = home.converse(kind)
        home.run()
        tree = home.analysis(talk)
        sub_goals = [goal for goal in tree.goals if goal not in tree.roots]
        assert analyze_attempts(home, talk) == 2 and sub_goals and all_sub_goals_under(sub_goals, tree.roots, tree)


@refines("test_every_sub_goal_hangs_from_a_goal_of_its_own_tree_and_a_sub_goal_can_serve_another_sub_goal")
def test_a_sub_goal_serving_another_sub_goal_is_kept_at_the_first_try_not_refused_or_flattened_onto_its_root():
    """Sub-goals nest (DETAILS P2.3.2, P2.3.15). Control: an answer that does get refused is retried."""
    home = Household()
    talk = home.converse(NESTED)
    home.run()
    tree = home.analysis(talk)
    [root] = tree.roots
    [heater] = [goal for goal in tree.goals if goal not in tree.roots and goal.parent not in tree.roots]
    assert heater.parent.parent == root and analyze_attempts(home, talk) == 1

    home = Household()
    home.stand_in("Analyze", "first hangs a sub-goal from a goal that isn't in the analysis")
    talk = home.converse(NESTED)
    home.run()
    assert analyze_attempts(home, talk) == 2


@refines("test_any_goal_root_or_sub_goal_is_met_if_assist_reached_it_unmet_if_it_missed_and_indeterminate_while_unclear")
def test_whether_a_goal_was_met_follows_what_assist_did_whatever_the_analyzer_says_about_it():
    """Met or unmet follows from the home's start state and what Assist ran, so code derives it and the analyzer gives
    no verdict (DETAILS P9.10, P9.5.3). The stand-in says each goal went the other way; the reached goal stays met and
    the missed one unmet (each the other's control), and so do sub-goals at every depth: HEATER_LEFT_OFF is NESTED with
    the heater not turned back on, so the heater, two deep, is unmet beside the fan, met."""
    says_otherwise = "says each goal went the other way"
    assert "met" in {goal.state for goal in analysed_with(FRICTION, says_otherwise).roots}
    assert "unmet" in {goal.state for goal in analysed_with(UNMET, says_otherwise).roots}
    assert {goal.state for goal in analysed_with(NESTED, says_otherwise).goals} == {"met"}
    tree = analysed_with(HEATER_LEFT_OFF, says_otherwise)
    [heater] = [goal for goal in tree.goals if goal not in tree.roots and goal.parent not in tree.roots]
    assert heater.state == "unmet" and heater.parent.state == "met"


@refines("test_any_goal_root_or_sub_goal_is_met_if_assist_reached_it_unmet_if_it_missed_and_indeterminate_while_unclear")
def test_a_goal_whose_want_the_analyzer_cannot_tell_is_indeterminate_while_its_question_waits_for_the_household():
    """While the household hasn't said what they wanted, whether Assist got there can't be told (DETAILS P9.9.5,
    P9.9.6): the goal is indeterminate, not unmet, and the question waits at Your review."""
    home = Household()
    talk = home.converse(UNCLEAR)
    home.run()
    assert [item for item in home.review_queue() if item.conversation == talk and item.question]
    assert "indeterminate" in {goal.state for goal in home.analysis(talk).roots}


@refines("test_any_goal_root_or_sub_goal_is_met_if_assist_reached_it_unmet_if_it_missed_and_indeterminate_while_unclear")
def test_every_goal_carries_its_own_state_so_one_conversation_can_hold_goals_in_different_states():
    """A state per goal, not per conversation: a missed goal is unmet beside one that can't be told, and the thanks and
    the note, sub-goals in the tree's goals, carry states of their own, as every goal of the tree does. Which state a
    thanks or a note gets is for other tests (DETAILS P2.3.5, P6.1.9)."""
    assert {"unmet", "indeterminate"} <= {goal.state for goal in analysed(UNMET_BESIDE_INDETERMINATE).goals}
    tree = analysed(THANKS_AND_NOTE)
    request, correction, thanks, note = tree.turns
    assert thanks.goals and note.goals and {*thanks.goals, *note.goals} <= set(tree.goals) - set(tree.roots)
    assert {goal.state for goal in tree.goals} <= set(GOAL_STATES)


# Layer 4: children only of the layer-3 test on each kind of turn left without a goal, which runs three faults in one
# loop: one child per kind of turn. The other layer-3 tests are each already one concrete scenario on the driver (or a
# control and one stand-in run), every case in its own household on its own assert lines, so a child would rebuild the
# same household to check one of those lines.


def ran(kind, behaviour=None):
    """One conversation of that kind, run in a fresh household whose analyzer stand-in behaves so, if one is given."""
    home = Household()
    if behaviour:
        home.stand_in("Analyze", behaviour)
    talk = home.converse(kind)
    home.run()
    return home, talk


def maps_into(turn, root):
    """The turn has a goal, and every goal it has is the root goal or one of its sub-goals."""
    return bool(turn.goals) and all(in_tree(goal, root) for goal in turn.goals)


@refines("test_each_kind_of_turn_left_without_a_goal_gets_the_answer_refused_and_the_retry_maps_it_into_the_requests_tree")
def test_a_correction_with_no_goal_is_refused_not_counted_as_an_extra_turn_and_the_retry_maps_it_to_the_request():
    """A correction maps to the goal it makes more exact and adds no root goal of its own (DETAILS P2.2.1, P9.6.3).
    Reading: whether that's the request's goal itself or a sub-goal under it is the analyzer's call, so either is kept.
    Control: answered in full, the answer is kept at the first try."""
    assert analyze_attempts(*ran(THANKS_AND_NOTE)) == 1

    home, talk = ran(THANKS_AND_NOTE, "first leaves the correction without a goal")
    tree = home.analysis(talk)
    [root] = tree.roots
    request, correction, thanks, note = tree.turns
    assert analyze_attempts(home, talk) == 2
    assert maps_into(correction, root) and all(maps_into(turn, root) for turn in (request, thanks, note))
    assert not home.place(talk).held and home.ideas(talk)  # moved on to Propose


@refines("test_each_kind_of_turn_left_without_a_goal_gets_the_answer_refused_and_the_retry_maps_it_into_the_requests_tree")
def test_a_thanks_with_no_goal_is_refused_not_passed_as_not_a_request_and_the_retry_hangs_it_under_the_root_goal():
    """A thanks is a sub-goal of the root goal it's about (P0.3), so the retry's goal for it hangs under the request's
    root goal, and isn't that root goal itself. Control: answered in full, the answer is kept at the first try."""
    assert analyze_attempts(*ran(THANKS_AND_NOTE)) == 1

    home, talk = ran(THANKS_AND_NOTE, "first leaves the thanks without a goal")
    tree = home.analysis(talk)
    [root] = tree.roots
    request, correction, thanks, note = tree.turns
    assert analyze_attempts(home, talk) == 2
    assert maps_into(thanks, root) and all_sub_goals_under(thanks.goals, [root], tree)
    assert all(maps_into(turn, root) for turn in (request, correction, note))
    assert not home.place(talk).held and home.ideas(talk)  # moved on to Propose


@refines("test_each_kind_of_turn_left_without_a_goal_gets_the_answer_refused_and_the_retry_maps_it_into_the_requests_tree")
def test_a_note_with_no_goal_is_refused_though_one_root_goal_could_take_it_and_the_retry_hangs_it_there():
    """Which root goal a note is about is the analyzer's call, so code doesn't fill it in, even with only one root goal
    to choose: the first answer is refused, and the retry's goal for the note hangs under that root goal (P0.3;
    DETAILS P9.13.2, P9.13.3). Control: answered in full, the answer is kept at the first try."""
    assert analyze_attempts(*ran(THANKS_AND_NOTE)) == 1

    home, talk = ran(THANKS_AND_NOTE, "first leaves the note without a goal")
    tree = home.analysis(talk)
    [root] = tree.roots
    request, correction, thanks, note = tree.turns
    assert analyze_attempts(home, talk) == 2
    assert maps_into(note, root) and all_sub_goals_under(note.goals, [root], tree)
    assert all(maps_into(turn, root) for turn in (request, correction, thanks))
    assert not home.place(talk).held and home.ideas(talk)  # moved on to Propose


@refines("test_what_the_household_objected_to_is_a_conduct_goal_in_their_own_words_under_the_goal_it_is_about")
def test_an_objection_paraphrased_instead_of_quoted_is_refused_and_the_retry_quoting_it_is_kept():
    """Code checks the quote against what the household said (DETAILS P9.15): their words, not the analyzer's account
    of them. Control: quoted, the answer is kept at the first try."""
    assert analyze_attempts(*ran(THANKS_AND_NOTE)) == 1

    home, talk = ran(THANKS_AND_NOTE, "first paraphrases the note instead of quoting it")
    [goal] = home.analysis(talk).turns[-1].goals
    assert analyze_attempts(home, talk) == 2 and goal.quote == talk.kind.note
    assert not home.place(talk).held and home.ideas(talk)  # moved on to Propose


@refines("test_what_the_household_objected_to_is_a_conduct_goal_in_their_own_words_under_the_goal_it_is_about")
def test_an_objection_about_a_turn_outside_its_goals_tree_is_refused_and_retried():
    """An objection is about one of its own goal's Assist turns: here the note is about closing the garage, so the
    morning's opening isn't one of them. Control: the same note about Assist in general is kept at the first try."""
    note = "close it again without asking next time"
    for behaviour, attempts in ((None, 1), ("first points the note's objection at a turn outside its goal's tree", 2)):
        home = Household()
        if behaviour:
            home.stand_in("Analyze", behaviour)
        talk = home.converse(TWO_TIMES)
        home.send_in(talk, note=note)
        tree = home.analysis(talk)
        [goal] = tree.turns[-1].goals
        assert analyze_attempts(home, talk) == attempts
        assert (goal.kind, goal.about, goal.root) == ("conduct", "all", tree.roots[1])


def reacting(test, kind):
    """The goals of a kind that react to their goal, by turn, as a test's analysis has them."""
    return {goal for goal in test.before.goals if goal.kind == kind}


@refines("test_any_goal_root_or_sub_goal_is_met_if_assist_reached_it_unmet_if_it_missed_and_indeterminate_while_unclear")
def test_a_step_is_met_once_the_goal_it_reacts_to_was_met_before_its_turn_and_its_turn_said_until_then():
    """A step names its goal still unmet, and is met once that's resolved (DETAILS P2.3.2): here the fix meets the
    press in the first turn, so both steps are met with it and their turns aren't said (DETAILS P6.4); without it
    neither is, as in the conversation. The control: a fix that acts only in the step's own turn meets the goal there,
    too late for the step, which stays unmet and is said."""
    home = Household()
    talk = home.converse(FRICTION)
    home.run()
    test = home.improvements(talk)[0].test
    steps = reacting(test, "step")
    assert len(steps) == 2 and all(test.before.goals[s] == "unmet" and test.after.goals[s] == "met" for s in steps)
    assert not {turn for turn in test.after.said if set(turn.goals) & steps}

    home = Household()
    talk = home.converse(NO_GAIN)
    home.run()
    for test in home.tests(talk):
        [step] = reacting(test, "step")
        assert test.before.goals[step] == test.after.goals[step] == "unmet"
        assert any(step in turn.goals for turn in test.after.said)


@refines("test_any_goal_root_or_sub_goal_is_met_if_assist_reached_it_unmet_if_it_missed_and_indeterminate_while_unclear")
def test_a_wrap_up_is_met_once_its_goal_was_with_nothing_else_of_it_open_and_its_turn_said_until_then():
    """Filing the trouble reacts to it as a whole (DETAILS P2.3.2): here the fix meets the lamp the user filed, so the
    filing is met with it and its turn isn't said; without it, it's unmet and said, as in the conversation. The
    control: with what the household objected to still open (the fix's replies still show it, judged on the replies
    so far), the filing stays open and its turn is said, though the goal itself is met."""
    home = Household()
    talk = home.converse(FILED)
    home.run()
    test = home.improvements(talk)[0].test
    [wrap] = reacting(test, "wrap-up")
    assert test.before.goals[wrap] == "unmet" and test.after.goals[wrap] == "met"
    assert any(wrap in turn.goals for turn in test.before.said) and not any(wrap in t.goals for t in test.after.said)

    home = Household()
    talk = home.converse(OBJECTED_AND_FILED)
    home.run()
    test = home.tests(talk)[0]
    [wrap] = reacting(test, "wrap-up")
    [root] = home.analysis(talk).roots
    assert test.after.goals[root] == "met" and test.after.goals[wrap] == "unmet"
    assert any(wrap in turn.goals for turn in test.after.said)
