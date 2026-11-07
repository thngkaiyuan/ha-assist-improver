"""Layer 2 under P0.10: an improvement belongs to the one conversation it came from and was tested on; its changes can
be broad; the proposer and its reviewer look at the household as a whole.

Each test checks one part of that more concretely: which conversation owns an improvement even when each change is for
the whole household, when its change also helps another conversation, after a revision, under a run that admits only
one conversation, and which conversations the proposer and reviewer can see. What a role sees is what it was really
handed, upfront or within reach on demand through its tools (DETAILS P7.6): no rule that it gets every conversation
upfront.
"""

from household import FINE, FRICTION, SAME_CAUSE, SIDE_EFFECT, SMOOTH, UNMET, Household
from spec_graph import refines


def improvement_for(home, talk):
    [improvement] = home.improvements(talk)
    return improvement


@refines("P0.10")
def test_each_improvement_is_listed_under_the_one_conversation_it_came_from_and_tested_on_that_one_alone():
    """Several conversations with trouble in one run, each fixed with a change for the whole household: each has its own
    improvements, and each improvement names its own conversation, grew from one of that conversation's ideas, and was
    tested only there (its test is one of that conversation's), though its change reaches beyond it and the run took the
    others in too. None is left without one.
    Sources: P0.10; DETAILS P2.4 (each conversation either has improvements or it doesn't), P2.4.3, P6.8.7."""
    home = Household()
    home.stand_in("Propose", "proposes a change for the whole household")
    talks = [home.converse(kind) for kind in (FRICTION, UNMET, SIDE_EFFECT)]
    home.run()
    for talk in talks:
        assert home.improvements(talk)
        for improvement in home.improvements(talk):
            assert improvement.conversation == talk and improvement.idea in home.ideas(talk)
            assert improvement.tested_on == {talk} and improvement.test in home.tests(talk)
            assert improvement.changes_beyond(talk)
    assert {improvement.conversation for improvement in home.improvements()} == set(talks)


@refines("P0.10")
def test_a_fix_that_also_removes_another_conversations_trouble_covers_it_but_stays_with_the_conversation_it_came_from():
    """FRICTION's fix also removes SAME_CAUSE's trouble (that's what the kind means, so FRICTION's is the conversation
    it came from, as P0.12's test reads it). Whether the other conversation comes in the same run or a later one, the
    change helps it (it's covered) while the improvement is tested only where it came from and the other conversation
    gets no improvement of its own, not even a copy. Alone, SAME_CAUSE does get one: the control.
    Sources: P0.10, P0.12 (duplicate); DETAILS P2.4 (solutions can be broad), P2.4.2, P7.1."""
    for later in (False, True):
        home = Household()
        owner = home.converse(FRICTION)
        if later:
            home.run()
        same = home.converse(SAME_CAUSE)
        home.run()
        improvement = improvement_for(home, owner)
        assert improvement.conversation == owner and improvement.tested_on == {owner}
        assert home.place(same).covered_by == improvement and home.improvements(same) == []

    home = Household()
    alone = home.converse(SAME_CAUSE)
    home.run()
    assert home.improvements(alone) and home.place(alone).covered_by is None


@refines("P0.10")
def test_a_revised_improvement_stays_with_its_conversation_and_is_tested_on_it_in_the_new_round():
    """The household's revision note is a turn of the same conversation (P0.3), and the revision is a new round of it
    (P0.6): the new version belongs to that conversation and is tested on it, in that round.
    Sources: P0.10, P0.3, P0.6; DETAILS P7.12.5, P2.4.3."""
    home = Household()
    talk = home.converse(FRICTION)
    home.run()
    home.decide(improvement_for(home, talk), "revise", "only the left door")
    [revised] = [version for version in home.improvements(talk) if version.test in home.tests(talk, round=2)]
    assert revised.conversation == talk and revised.tested_on == {talk}


@refines("P0.10")
def test_a_run_that_admits_only_one_conversation_narrows_neither_how_far_its_change_reaches_nor_what_its_roles_see():
    """A run's scope decides what moves (P0.7), not the improvement's reach or its roles' view: a picked run moves the
    picked conversation and not the one beside it, which stays new, not analysed yet. That one is still within the
    proposer's and reviewer's reach, and a change for the whole household still goes up whole, tested only where it
    came from.
    Sources: P0.10, P0.7; DETAILS P7.13.1, P7.6.2."""
    home = Household()
    home.stand_in("Propose", "proposes a change for the whole household")
    owner, other = home.converse(FRICTION), home.converse(FINE)
    assert home.moved(home.run("picked", picked=[owner])) == {owner}
    improvement = improvement_for(home, owner)
    assert improvement.conversation == owner and improvement.tested_on == {owner}
    assert improvement.changes_beyond(owner)
    assert other in home.reached("Propose", owner) and other in home.reached("Review", owner)


@refines("P0.10")
def test_the_proposer_and_reviewer_also_see_conversations_already_out_of_the_pipeline():
    """The household as a whole includes what's out of the pipeline: a conversation decided in an earlier run, one
    triage found smooth in that run (never analysed in depth), and one archived by hand (kept for checking other changes
    against). The smooth one is set aside before the owner comes in, so it's seen as out of the pipeline, not just as
    not analysed yet (DETAILS P7.6.2 keeps those within reach too).
    Sources: P0.10, P0.12; DETAILS P7.6.2, P7.6.3, P3.3.2, P7.8."""
    home = Household()
    earlier, smooth = home.converse(FINE), home.converse(SMOOTH)
    home.run()
    owner, archived = home.converse(FRICTION), home.converse(FINE)
    home.archive(archived)
    home.run()
    for other in (earlier, smooth, archived):
        assert other in home.reached("Propose", owner) and other in home.reached("Review", owner)


# Layer 3: each test below refines one layer-2 test above, by its name. Only the one that bundles two mechanisms has
# children: a fix that covers another conversation found in the same run, or one matched later against an improvement
# still waiting for review. The others are each one scenario already; the out-of-pipeline one's loop names the
# conversation that isn't seen.


def same_cause_alone_gets_its_own_improvement():
    """The control for "no improvement of its own": with no other conversation's fix to cover it, SAME_CAUSE gets one."""
    home = Household()
    alone = home.converse(SAME_CAUSE)
    home.run()
    assert home.improvements(alone) and home.place(alone).covered_by is None


@refines("test_a_fix_that_also_removes_another_conversations_trouble_covers_it_but_stays_with_the_conversation_it_came_from")
def test_a_conversation_the_fix_also_helps_in_the_same_run_is_covered_by_it_and_gets_no_improvement_of_its_own():
    """FRICTION and SAME_CAUSE come in together and one run takes both. The fix is FRICTION's (the kind says so): it
    goes up as FRICTION's one improvement, tested there alone, and SAME_CAUSE points to it as covered.
    Sources: P0.10, P0.12 (a covered conversation ends as a duplicate pointing to the improvement);
    DETAILS P2.4, P2.4.2 (its goal stays in its own tree, worked on in its own round), P7.3.1."""
    home = Household()
    owner, same = home.converse(FRICTION), home.converse(SAME_CAUSE)
    home.run()
    improvement = improvement_for(home, owner)
    assert improvement.conversation == owner and improvement.tested_on == {owner}
    assert home.place(same).covered_by == improvement and home.improvements(same) == []
    same_cause_alone_gets_its_own_improvement()


@refines("test_a_fix_that_also_removes_another_conversations_trouble_covers_it_but_stays_with_the_conversation_it_came_from")
def test_a_conversation_the_fix_also_helps_in_a_later_run_is_covered_by_the_improvement_still_waiting_for_review():
    """FRICTION's improvement already waits at Your review when SAME_CAUSE comes in. The later run leaves it FRICTION's:
    it doesn't hand it to the newcomer, test it there, or add a version of it or a copy. SAME_CAUSE points to the
    waiting one as covered.
    Sources: P0.10, P0.12 (duplicate); DETAILS P9.14.1 (nothing is fixed twice), P7.7.7 (a goal an existing
    improvement addresses is noted with it and skipped), P7.6.7."""
    home = Household()
    owner = home.converse(FRICTION)
    home.run()
    waiting = improvement_for(home, owner)
    assert waiting in home.review_queue()
    same = home.converse(SAME_CAUSE)
    home.run()
    improvement = improvement_for(home, owner)
    assert improvement == waiting and improvement.conversation == owner and improvement.tested_on == {owner}
    assert home.place(same).covered_by == waiting and home.improvements(same) == []
    same_cause_alone_gets_its_own_improvement()


# Layer 4: none. Each layer-3 test above is one household, one or two runs and one control already; a split would only
# unroll its asserts over the same setup, and no child would be simpler to make pass:
# - covered in the same run: owning the fix and covering the other both need the one run that takes both in; its asserts
#   show which failed (the owner, where it was tested, the pointer, or an improvement of the other's own).
# - covered in a later run: matching the newcomer needs the improvement already waiting, and leaving that improvement as
#   it was needs the later run; its asserts show which failed (a new version, a copy, a test on the newcomer, the
#   pointer).
