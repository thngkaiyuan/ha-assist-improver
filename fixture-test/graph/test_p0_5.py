"""Layer 2 under P0.5 (conversations travel whole): what makes a conversation be at one stage at a time, with all
its goals.

The parent follows a conversation with two root goals, one in trouble and one that went fine, to Your review. These
tests take its claim apart: the conversation leaves Analyze whole; Review and Simulate are stamps its ideas collect at
Propose, not stages of their own (P0.4); nothing of it goes up for review before it reaches Your review itself; and
every goal goes with it, through every stage to Your review, and to the proposer, its reviewer and the tests.

Kai decided (2026-10-26): while a revision runs as a new round, the earlier version of the improvement stays up for
review. The conversation itself is at Propose; only its earlier improvement waits at Your review. That isn't a second
stage for the conversation.
"""

from household import FINE, FRICTION, PARTLY, STAGES, Household
from spec_graph import refines

TO_REVIEW = ["Your Assist", "Triage", "Analyze", "Propose", "Write up", "Your review"]


def improvement_for(home, talk):
    [improvement] = home.improvements(talk)
    return improvement


@refines("P0.5")
def test_a_conversation_with_one_goal_in_trouble_goes_on_from_analyze_whole_where_one_with_none_ends_there():
    """Analyze ends a conversation only when none of its goals has trouble (DETAILS P3.1.2, P4.1): the goal that went
    fine doesn't end in Your household while the other goes on. At every moment the conversation is at one stage, on
    its way to Your review."""
    home = Household()
    fine, partly = home.converse(FINE), home.converse(PARTLY)
    home.run()
    assert home.place(fine).stage == "Your household" and home.path(fine)[-2] == "Analyze"
    assert home.place(fine).outcome == "no trouble found" and home.trace(fine)[-1].stages == ["Your household"]
    assert home.path(partly) == TO_REVIEW
    assert home.place(partly).stage == "Your review" and home.place(partly).outcome is None
    assert all(len(moment.stages) == 1 and moment.stages[0] in TO_REVIEW for moment in home.trace(partly))


@refines("P0.5")
def test_the_goal_that_went_fine_travels_with_the_one_in_trouble_through_propose_and_write_up_to_your_review():
    """Every stage after Analyze gets the whole conversation, not only what the improvement is for: each moment at
    Propose, Write up and Your review carries both root goals and everything under them."""
    home = Household()
    talk = home.converse(PARTLY)
    home.run()
    tree = home.analysis(talk)
    assert len(tree.roots) == 2  # the goal that went fine is in the analysis too
    trace = home.trace(talk)
    for stage in ("Propose", "Write up", "Your review"):
        there = [moment for moment in trace if moment.stages == [stage]]
        assert there and all(set(moment.goals) == set(tree.goals) for moment in there)


@refines("P0.5")
def test_a_conversation_stays_at_propose_while_several_of_its_ideas_collect_their_stamps():
    """Review and Simulate are stamps the proposer collects (P0.4), so two ideas reviewed and tested at once leave
    the conversation at Propose, once, and never at a stamp."""
    home = Household()
    home.stand_in("Propose", "proposes two ideas at once and sends both on")
    talk = home.converse(FRICTION)
    home.run()
    assert len(home.tests(talk)) == 2  # both ideas got their stamps
    assert home.path(talk) == TO_REVIEW
    assert all(len(moment.stages) == 1 and moment.stages[0] in STAGES for moment in home.trace(talk))


@refines("P0.5")
def test_within_a_round_nothing_of_a_conversation_goes_up_for_review_before_the_conversation_reaches_your_review():
    """A validated idea waits with its conversation: not while the proposer is still trying a better one and the run
    is cut off (held at Propose, P0.8), nor while Write up is blocked (DETAILS P12.1.10). Within a round only: an
    earlier round's improvement can still wait while a revision runs (Kai's decision, the next test)."""
    for hold_up, stage in [(None, "Your review"), ("cut off", "Propose"), ("blocked", "Write up")]:
        home = Household()
        home.stand_in("Propose", "after a validated idea, tries a better one while tests are left")
        if hold_up == "cut off":
            home.stand_in("Simulate", "is cut off during its second test")
        if hold_up == "blocked":
            home.block("Write up")
        talk = home.converse(PARTLY)
        home.run()
        assert home.tests(talk)[0].validated and home.place(talk).stage == stage
        waiting = [item for item in home.review_queue() if item.conversation == talk]
        assert bool(waiting) == (hold_up is None)


@refines("P0.5")
def test_while_a_revision_runs_its_conversation_is_at_propose_alone_though_its_earlier_improvement_waits_for_review():
    """Kai's decision: the earlier version stays up for review, but every screen shows the conversation at Propose
    (DETAILS P14.1.2), and it carries its goals there; once Propose reads it afresh, the revision note's are among
    them (P0.3, DETAILS P12.8.1)."""
    home = Household()
    talk = home.converse(FRICTION)
    home.run()
    earlier = improvement_for(home, talk)
    assert set(home.shown(talk).values()) == {"Your review"}
    before = home.analysis(talk)
    home.block("Propose")  # keeps the revision's round at Propose while we look
    home.decide(earlier, "revise", "only the left door")
    assert earlier in home.review_queue()
    assert home.place(talk).stage == "Propose" and set(home.shown(talk).values()) == {"Propose"}
    now = home.trace(talk)[-1]
    assert now.stages == ["Propose"] and set(now.goals) == set(home.analysis(talk).goals)
    home.unblock("Propose")
    home.run()
    after = home.analysis(talk)
    note = after.turns[-1]
    assert len(after.turns) == len(before.turns) + 1 and note.goals


@refines("P0.5")
def test_the_proposer_and_its_reviewer_are_given_the_whole_conversation_not_only_the_goal_in_trouble():
    """Slicing a conversation into goals loses context (DETAILS P4.1): both roles get every turn and every goal, as
    what they were really handed shows: the proposer in its input, the reviewer from the session tools it was handed
    (DETAILS P7.6)."""
    home = Household()
    talk = home.converse(PARTLY)
    home.run()
    tree = home.analysis(talk)
    assert len(tree.roots) == 2  # the goal that went fine is in the analysis too
    for role in ("Propose", "Review"):
        got = home.reached(role, talk)
        assert all(turn in got for turn in tree.turns) and all(goal in got for goal in tree.goals)


@refines("P0.5")
def test_a_fix_for_the_goal_in_trouble_is_judged_on_every_goal_of_its_conversation():
    """A test replays the whole conversation and re-judges every goal (DETAILS P6.1, P6.8), so the improvement that
    goes up shows the goal that went fine too, and a fix that breaks it isn't validated."""
    home = Household()
    talk = home.converse(PARTLY)
    home.run()
    assert set(improvement_for(home, talk).test.judged) == set(home.analysis(talk).goals)

    home = Household()
    home.stand_in("Propose", "for the goal in trouble, proposes only fixes that break the goal that went fine")
    talk = home.converse(PARTLY)
    home.run()
    assert home.tests(talk) and not any(test.validated for test in home.tests(talk))
    assert home.improvements(talk) == []


# Layer 3: children of the layer-2 tests above, each refining its parent by name.

@refines("test_within_a_round_nothing_of_a_conversation_goes_up_for_review_before_the_conversation_reaches_your_review")
def test_the_proposer_hands_no_improvement_on_while_it_still_means_to_try_a_better_idea():
    """While the proposer still tries for better (P0.9), its validated idea stays with it at Propose: a run cut off
    then holds the conversation there with its work kept (DETAILS P4.3.1), no improvement built, nothing handed to
    Write up, and nothing of it up for review. Released, the proposer finishes and the conversation takes its
    improvement through Write up to Your review."""
    home = Household()
    home.stand_in("Propose", "after a validated idea, tries a better one while tests are left")
    home.stand_in("Simulate", "cuts the first run off after one test")
    talk = home.converse(PARTLY)
    home.run()
    assert home.tests(talk)[0].validated and home.place(talk).stage == "Propose"
    assert home.improvements(talk) == [] and "Write up" not in home.records(talk, round=1)
    assert [item for item in home.review_queue() if item.conversation == talk] == []
    home.run("full")  # releases it (P0.8)
    assert home.place(talk).stage == "Your review" and "Write up" in home.records(talk, round=1)
    assert improvement_for(home, talk) in home.review_queue()


@refines("test_within_a_round_nothing_of_a_conversation_goes_up_for_review_before_the_conversation_reaches_your_review")
def test_an_improvement_waiting_for_its_write_up_is_not_up_for_review_until_it_is_written_up():
    """DETAILS P12.1.10: with Write up blocked, the improvement is built and its conversation is at Write up on every
    screen, but nothing of it is up for review. Unblocked and released, it is written up and goes up."""
    home = Household()
    home.block("Write up")
    talk = home.converse(FRICTION)
    home.run()
    assert improvement_for(home, talk) and home.place(talk).stage == "Write up"
    assert set(home.shown(talk).values()) == {"Write up"}
    assert [item for item in home.review_queue() if item.conversation == talk] == []
    home.unblock("Write up")
    home.run("full")  # unblocking releases nothing by itself (P0.8)
    assert home.place(talk).stage == "Your review" and improvement_for(home, talk) in home.review_queue()


# Layer 4: none. Each layer-3 test above is one concrete scenario on the driver, and each of its asserts names its own
# failure; a child would repeat that assert with the same setup:
# - the proposer handing nothing on while it tries for better: the one claim of P0.5's here is a single line (no
#   improvement, no Write up record, nothing up for review while held at Propose). The hold and its release are P0.8's
#   subtree, the round's tests lasting across runs P0.13's, trying to better a validated idea P0.9's.
# - an improvement waiting for its write-up: one blocked stage, one release; the hold at a blocked stage and its
#   release are P0.8's subtree, and what's left is one read of the review queue before and after.
