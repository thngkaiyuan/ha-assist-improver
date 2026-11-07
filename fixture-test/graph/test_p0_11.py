"""Layer 2 under P0.11 (Your review): what each part of the parent's claim means as the household sees it.

The household accepts, asks for a revision, or declines; a decline declines the improvement and archives the
conversation, nothing more; a question for the household waits at Your review too. Kai, 2026-10-26: "decline just
declines the improvement and archives the conversation. don't bother with adding convoluted logic to prevent it from
coming back."
"""

from household import (ANSWERED_BY_AN_AUTOMATION, ANSWERED_LOCALLY, ANSWERED_LOCALLY_COVERED, ANSWERED_LOCALLY_FINE,
                       BROKEN, DECISIONS, FINE, FRICTION, SAME_CAUSE, UNCLEAR, UNMET, Household)
from spec_graph import refines


def improvement_for(home, talk):
    [improvement] = home.improvements(talk)
    return improvement


def where(home, talk):
    """Where a conversation is, as its place records it."""
    place = home.place(talk)
    return place.stage, place.round, place.outcome, place.archived, place.held


def analyze_attempts(home, talk):
    """How many tries Analyze's record holds for it in round 1 (a release keeps the round)."""
    return len(home.records(talk, round=1)["Analyze"].attempts)


@refines("P0.11")
def test_declining_closes_the_improvement_which_offered_exactly_accept_revise_and_decline_while_it_waited():
    home = Household()
    talk = home.converse(FRICTION)
    home.run()
    improvement = improvement_for(home, talk)
    assert improvement in home.review_queue() and set(improvement.decisions) == set(DECISIONS)
    home.decide(improvement, "decline")
    declined = improvement_for(home, talk)
    assert declined.status == "declined" and not declined.decisions
    assert not [item for item in home.review_queue() if item.conversation == talk]


@refines("P0.11")
def test_a_declined_conversation_goes_from_your_review_straight_to_your_household_in_the_same_round():
    """A decline ends the round (P0.6); only a revision or a conflict the household sends back starts a new one. Nothing
    here is said about runs: P0.7 doesn't settle whether this move is a run's."""
    home = Household()
    talk = home.converse(FRICTION)
    home.run()
    home.decide(improvement_for(home, talk), "decline")
    assert home.place(talk).stage == "Your household" and home.place(talk).round == 1
    assert home.place(talk).outcome == "declined" and home.place(talk).archived
    assert home.path(talk)[-2:] == ["Your review", "Your household"]

    home = Household()  # positive control: asking for a revision instead sends it back in at Propose, for round 2
    talk = home.converse(FRICTION)
    home.run()
    home.decide(improvement_for(home, talk), "revise", "only the left door")
    assert home.place(talk).round == 2 and home.path(talk, round=2)[0] == "Propose"


@refines("P0.11")
def test_declining_the_earlier_version_while_its_revisions_round_runs_archives_the_conversation_and_ends_that_round():
    """While a revision's round runs, the earlier version stays up for review with decline open (P0.6). The clause has
    no carve-out: declining it archives the conversation as a decline at Your review does. The round ends in Your
    household from the stage it reached (P0.6), and nothing takes it on: the next full run doesn't move it, and no
    revised version comes."""
    home = Household()
    talk = home.converse(FRICTION)
    home.run()
    earlier = improvement_for(home, talk)
    home.block("Propose")  # keeps the revision's round at Propose
    home.decide(earlier, "revise", "only the left door")
    assert home.place(talk).stage == "Propose" and home.place(talk).round == 2
    home.decide(earlier, "decline")
    assert improvement_for(home, talk).status == "declined"
    assert where(home, talk) == ("Your household", 2, "declined", True, None)
    assert home.path(talk, round=2) == ["Propose", "Your household"]
    home.unblock("Propose")
    newcomer = home.converse(UNMET)
    moved = home.moved(home.run("full"))
    assert newcomer in moved and talk not in moved  # positive control: the same run moves a newcomer
    assert home.improvements(talk) == [earlier] and where(home, talk)[:4] == ("Your household", 2, "declined", True)


@refines("P0.11")
def test_accepting_the_earlier_version_while_its_revisions_round_runs_ends_that_round_improved():
    """Accept, decline's twin: while a revision's round runs, the earlier version stays up for review with accept open
    (P0.6). Accepted, it's applied, and the conversation is improved (P0.12): its round ends in Your household from the
    stage it reached (P0.6), not later as no fix found, and nothing takes it on."""
    home = Household()
    talk = home.converse(FRICTION)
    home.run()
    earlier = improvement_for(home, talk)
    home.block("Propose")  # keeps the revision's round at Propose
    home.decide(earlier, "revise", "only the left door")
    assert home.place(talk).stage == "Propose" and home.place(talk).round == 2
    home.decide(earlier, "accept")
    assert improvement_for(home, talk).status == "applied"
    assert where(home, talk) == ("Your household", 2, "improved", False, None)
    assert home.path(talk, round=2) == ["Propose", "Your household"]
    home.unblock("Propose")
    newcomer = home.converse(UNMET)
    moved = home.moved(home.run("full"))
    assert newcomer in moved and talk not in moved  # positive control: the same run moves a newcomer
    assert home.improvements(talk) == [earlier] and where(home, talk)[:3] == ("Your household", 2, "improved")


@refines("P0.11")
def test_a_declined_conversation_is_kept_out_of_runs_and_later_improvements_are_checked_against_it():
    """Archived as P0.12 has it: kept for checking other changes against, and out of the pipeline."""
    home = Household()
    talk = home.converse(FRICTION)
    home.run()
    home.decide(improvement_for(home, talk), "decline")
    later = home.converse(UNMET)
    moved = home.moved(home.run("full"))
    assert later in moved and talk not in moved  # the same run moves the new one
    assert talk in improvement_for(home, later).checked_on


@refines("P0.11")
def test_a_decline_with_a_note_or_as_not_an_issue_ends_just_like_a_plain_one_and_writes_nothing():
    """Decline's note is optional, and "not an issue" is a reason it can carry (decide's reason); the decline does the
    same either way (DETAILS P12.4.5, P12.4.13). Whether the reason is kept is left to the next layer."""
    for given in [{}, {"note": "I'd rather keep it as it is"}, {"reason": "not an issue"}]:
        home = Household()
        talk = home.converse(FRICTION)
        home.run()
        written = home.writable()
        home.decide(improvement_for(home, talk), "decline", **given)
        assert improvement_for(home, talk).status == "declined"
        assert home.place(talk).outcome == "declined" and home.place(talk).archived
        assert home.writable() == written

    home = Household()  # positive control: accepting the same improvement does write to Home Assistant
    talk = home.converse(FRICTION)
    home.run()
    written = home.writable()
    home.decide(improvement_for(home, talk), "accept")
    assert home.writable() != written


@refines("P0.11")
def test_a_decline_asks_nothing_and_sends_nothing_back_even_where_an_accept_would_meet_a_conflict():
    """Reading: a conflict is something an accept meets (P0.7); a decline writes nothing, so it has nothing to ask."""
    home = Household()
    talk = home.converse(FRICTION)
    home.run()
    improvement = improvement_for(home, talk)
    home.edit_home(improvement)
    declined = home.decide(improvement, "decline")
    assert declined.asks is None
    assert home.place(talk).outcome == "declined" and home.place(talk).archived and home.place(talk).round == 1

    home = Household()  # positive control: accepting it instead asks about the conflict, then goes back in
    talk = home.converse(FRICTION)
    home.run()
    improvement = improvement_for(home, talk)
    home.edit_home(improvement)
    conflicted = home.decide(improvement, "accept")
    assert conflicted.asks.about == "conflict"
    assert home.moved(home.agree(conflicted)) == {talk} and home.place(talk).round == 2


@refines("P0.11")
def test_a_decline_leaves_every_other_conversation_where_it_was_even_a_duplicate_its_improvement_covered():
    """Reading of "nothing more": the decline touches only its own improvement and conversation. A duplicate the
    declined improvement covered keeps its outcome and still points at it; the household can send it in again
    (P0.12)."""
    home = Household()
    talk, duplicate, bystander = home.converse(FRICTION), home.converse(SAME_CAUSE), home.converse(UNMET)
    home.run()
    improvement = improvement_for(home, talk)
    assert home.place(duplicate).covered_by == improvement
    mine, others = where(home, talk), {other: where(home, other) for other in (duplicate, bystander)}
    waiting = improvement_for(home, bystander)
    home.decide(improvement, "decline")
    assert where(home, talk) != mine  # positive control: the decline does move its own conversation
    assert {other: where(home, other) for other in others} == others
    assert home.place(duplicate).outcome == "duplicate"
    assert home.place(duplicate).covered_by == improvement_for(home, talk)  # still the improvement just declined
    assert improvement_for(home, bystander) == waiting and waiting in home.review_queue()


@refines("P0.11")
def test_nothing_keeps_a_declined_improvement_from_coming_back_when_its_conversation_is_sent_in_again():
    """The stand-ins answer the same way every time, so only the product could stop the same idea coming back, and a
    decline does nothing more than decline and archive (Kai, 2026-10-26; DETAILS P12.4.12)."""
    home = Household()
    talk = home.converse(FRICTION)
    home.run()
    declined = improvement_for(home, talk)
    home.decide(declined, "decline")
    assert home.moved(home.send_in(talk)) == {talk} and home.place(talk).round == 2
    assert home.place(talk).stage == "Your review"
    [back] = [improvement for improvement in home.improvements(talk) if improvement in home.review_queue()]
    assert back.idea == declined.idea
    assert [improvement.status for improvement in home.improvements(talk)].count("declined") == 1  # the decline stands


@refines("P0.11")
def test_a_question_for_the_household_waits_at_your_review_beside_the_improvements_until_it_is_answered():
    """It waits in Your review's queue beside the improvements (DETAILS P9.9.6); the household's answer takes it back
    to the stage that asked, in the same round (P0.6)."""
    home = Household()
    friction, unclear = home.converse(FRICTION), home.converse(UNCLEAR)
    home.run()
    queue = home.review_queue()
    assert improvement_for(home, friction) in queue
    assert [item.question for item in queue if item.conversation == unclear]
    assert home.place(unclear).stage == "Your review" and not home.place(unclear).held
    assert unclear not in home.moved(home.run("full"))
    assert home.place(unclear).stage == "Your review"
    answered = home.answer(unclear, "the warm lights are the two lamps")  # positive control: the answer moves it
    assert home.moved(answered) == {unclear} and home.place(unclear).round == 1
    assert not [item for item in home.review_queue() if item.conversation == unclear and item.question]


# Layer 3: children of the layer-2 tests above that bundle several ways the claim could fail.


@refines("test_a_question_for_the_household_waits_at_your_review_beside_the_improvements_until_it_is_answered")
def test_a_conversation_home_assistant_answered_itself_waits_as_cant_improve_with_nothing_spent_on_a_fix():
    """DETAILS P9.9.9: a turn Home Assistant answered itself can't be replayed, so no fix for it can be tested.
    Analysed (Kai: it still reaches the analyzer "if we can't determine confidently that all goals were met"), and
    with trouble, its conversation waits at Your review as a question for the household, quoting what they said
    there, with no idea proposed and no test run; the household archives it. Positive controls: the same missed goal,
    answered by the AI, goes on to a fix; one Home Assistant answered with no trouble ends as any other."""
    home = Household()
    local, by_ai, fine = (home.converse(kind) for kind in (ANSWERED_LOCALLY, UNMET, ANSWERED_LOCALLY_FINE))
    home.run()
    assert home.received("Analyze", local) and home.place(fine).outcome == "no trouble found"
    [asked] = [item for item in home.review_queue() if item.conversation == local]
    assert asked.question["turn"] == 1 and asked.question["quote"] == local.said
    assert home.place(local).stage == "Your review" and home.ideas(local) == [] and home.tests(local) == []
    assert home.improvements(by_ai)
    home.archive(local)
    assert home.place(local).outcome == "archived" and asked not in home.review_queue()


@refines("test_a_conversation_home_assistant_answered_itself_waits_as_cant_improve_with_nothing_spent_on_a_fix")
def test_only_home_assistants_own_commands_make_a_conversation_cant_improve_whatever_improvement_covers_it():
    """Trouble an existing improvement covers is still Can't improve when Home Assistant's commands answered the turn:
    that improvement can't reach it while Home Assistant answers first, and the household should hear of the setting.
    A turn an automation's sentence trigger answered is no such turn: no setting gives it to the AI, so it goes on as
    any other."""
    home = Household()
    home.converse(UNMET)
    covered, automation = home.converse(ANSWERED_LOCALLY_COVERED), home.converse(ANSWERED_BY_AN_AUTOMATION)
    home.run()
    assert [item for item in home.review_queue() if item.conversation == covered and item.question]
    assert home.place(covered).outcome is None and home.place(covered).stage == "Your review"
    assert not [item for item in home.review_queue() if item.conversation == automation]
    assert home.received("Propose", automation)


@refines("test_a_decline_leaves_every_other_conversation_where_it_was_even_a_duplicate_its_improvement_covered")
def test_declining_an_improvement_leaves_the_other_improvements_and_questions_at_your_review_as_they_were():
    """Another conversation's improvement still offers all three decisions, and a question still waits."""
    home = Household()
    talk, bystander, unclear = home.converse(FRICTION), home.converse(UNMET), home.converse(UNCLEAR)
    home.run()
    waiting = improvement_for(home, bystander)
    mine, others = where(home, talk), {other: where(home, other) for other in (bystander, unclear)}
    home.decide(improvement_for(home, talk), "decline")
    assert where(home, talk) != mine  # positive control: the decline does move its own conversation
    assert {other: where(home, other) for other in others} == others
    still = improvement_for(home, bystander)
    assert still == waiting and still in home.review_queue() and set(still.decisions) == set(DECISIONS)
    assert [item for item in home.review_queue() if item.conversation == unclear and item.question]


@refines("test_a_decline_leaves_every_other_conversation_where_it_was_even_a_duplicate_its_improvement_covered")
def test_a_duplicate_of_a_declined_improvement_stays_a_duplicate_pointing_at_it_and_the_next_run_leaves_it_there():
    """Reading of "nothing more": the decline doesn't free the duplicate to get an improvement of its own, then or at
    the next run. The household can send it in again (P0.12)."""
    home = Household()
    talk, duplicate = home.converse(FRICTION), home.converse(SAME_CAUSE)
    home.run()
    before = where(home, duplicate)
    home.decide(improvement_for(home, talk), "decline")
    newcomer = home.converse(UNMET)
    moved = home.moved(home.run("full"))
    assert newcomer in moved and duplicate not in moved  # positive control: the same run moves a newcomer
    assert where(home, duplicate) == before and home.place(duplicate).outcome == "duplicate"
    declined = improvement_for(home, talk)
    assert declined.status == "declined" and home.place(duplicate).covered_by == declined
    assert home.improvements(duplicate) == []


@refines("test_a_decline_leaves_every_other_conversation_where_it_was_even_a_duplicate_its_improvement_covered")
def test_a_decline_moves_no_new_or_held_conversation_though_the_next_full_run_moves_both():
    """The parent's household has none of these, so a decline that set them going would pass there. A release would
    leave the held one where it was after one more failed try (P0.8), so its tries are counted too. Whether the
    decline's own move is a run's is left open (P0.7 doesn't settle it): only where the others are is read."""
    home = Household()
    talk, held = home.converse(FRICTION), home.converse(BROKEN)
    home.run()
    new = home.converse(FINE)
    assert home.place(new).stage == "Your Assist" and home.place(held).held
    before, tries = {other: where(home, other) for other in (new, held)}, analyze_attempts(home, held)
    home.decide(improvement_for(home, talk), "decline")
    assert home.place(talk).outcome == "declined"  # positive control: the decline moved its own conversation
    assert {other: where(home, other) for other in before} == before and analyze_attempts(home, held) == tries
    assert {new, held} <= home.moved(home.run("full"))  # positive control: both were there for a run to move
    assert analyze_attempts(home, held) == tries + 1  # and the run's release gave the held one a try


@refines("test_nothing_keeps_a_declined_improvement_from_coming_back_when_its_conversation_is_sent_in_again")
def test_sending_a_declined_conversation_in_again_takes_it_out_of_the_archive_into_a_new_round_with_a_fresh_analysis():
    """Sent in again, it goes back to Your Assist for one fresh full analysis (DETAILS P4.5), unlike a revision, which
    goes in at Propose (P0.7)."""
    home = Household()
    talk = home.converse(FRICTION)
    home.run()
    home.decide(improvement_for(home, talk), "decline")
    assert home.place(talk).archived
    assert home.moved(home.send_in(talk)) == {talk}
    assert home.place(talk).round == 2 and not home.place(talk).archived
    assert home.path(talk, round=2)[0] == "Your Assist" and "Analyze" in home.records(talk, round=2)
    assert home.seen_by("Analyze", talk, round=2)  # the analyzer worked on it again, not a copy of round 1's


@refines("test_nothing_keeps_a_declined_improvement_from_coming_back_when_its_conversation_is_sent_in_again")
def test_in_the_new_round_the_declined_improvements_trouble_is_proposed_for_and_tested_again():
    """The decline leaves nothing settled that keeps the trouble from Propose: the new round's fresh analysis voids
    round 1's ideas (DETAILS P7.11), so an idea for it is tested anew and goes on to Write up."""
    home = Household()
    talk = home.converse(FRICTION)
    home.run()
    home.decide(improvement_for(home, talk), "decline")
    home.send_in(talk)
    assert home.path(talk, round=2)[-3:] == ["Propose", "Write up", "Your review"]
    assert [test for test in home.tests(talk, round=2) if test.validated]


@refines("test_nothing_keeps_a_declined_improvement_from_coming_back_when_its_conversation_is_sent_in_again")
def test_the_improvement_that_comes_back_is_a_new_one_waiting_beside_the_declined_one_which_stays_declined():
    """The same changes in a new round aren't folded into the declined improvement: an earlier round's outcome stands
    (P0.6)."""
    home = Household()
    talk = home.converse(FRICTION)
    home.run()
    home.decide(improvement_for(home, talk), "decline")
    home.send_in(talk)
    declined, back = home.improvements(talk)  # oldest first
    assert back in home.review_queue() and back.idea == declined.idea  # positive control for the line below
    assert declined.status == "declined" and not declined.decisions and declined not in home.review_queue()
    assert back.status != "declined" and set(back.decisions) == set(DECISIONS)  # it waits like any improvement
    assert home.place(talk).stage == "Your review" and home.place(talk).round == 2


@refines("test_nothing_keeps_a_declined_improvement_from_coming_back_when_its_conversation_is_sent_in_again")
def test_a_note_or_not_an_issue_on_the_decline_does_not_keep_the_same_idea_from_coming_back_either():
    """A decline's note, or "not an issue" as its reason, changes nothing about what the decline does (DETAILS
    P12.4.13), so the same idea still comes back."""
    for given in [{"note": "I'd rather keep it as it is"}, {"reason": "not an issue"}]:
        home = Household()
        talk = home.converse(FRICTION)
        home.run()
        declined = improvement_for(home, talk)
        home.decide(declined, "decline", **given)
        assert home.moved(home.send_in(talk)) == {talk}
        [back] = [improvement for improvement in home.improvements(talk) if improvement in home.review_queue()]
        assert back.idea == declined.idea


def decided_as_the_revision_is_proposed(behaviour):
    """The household asks for a revision, then accepts or declines the earlier version (the behaviour says which) just
    as the revision's proposer sends on a validated revised version: the household, the conversation and the earlier
    version."""
    home = Household()
    talk = home.converse(FRICTION)
    home.run()
    earlier = improvement_for(home, talk)
    home.stand_in("Propose", behaviour)
    home.decide(earlier, "revise", "only the left door")
    assert [test for test in home.tests(talk, round=2) if test.validated]  # positive control: one was ready to go on
    return home, talk, earlier


@refines("test_declining_the_earlier_version_while_its_revisions_round_runs_archives_the_conversation_and_ends_that_round")
def test_declining_the_earlier_version_as_its_revisions_proposer_finishes_leaves_no_revised_version():
    """The decline lands just as the revision's proposer sends on a revised version. The round still ends declined and
    archived in Your household, and no revised version is built: nothing of the conversation waits at Your review,
    written up or not."""
    home, talk, earlier = decided_as_the_revision_is_proposed(
        "finishes its revision just as the household declines the earlier version")
    [only] = home.improvements(talk)
    assert only == earlier and only.status == "declined"
    assert where(home, talk) == ("Your household", 2, "declined", True, None)
    assert not [item for item in home.review_queue() if item.conversation == talk]


@refines("test_accepting_the_earlier_version_while_its_revisions_round_runs_ends_that_round_improved")
def test_accepting_the_earlier_version_as_its_revisions_proposer_finishes_leaves_no_revised_version():
    """Accept, decline's twin: the accept lands just as the revision's proposer sends on a revised version. The earlier
    version is applied and the round ends improved in Your household, and no revised version is built: nothing of the
    conversation waits at Your review, written up or not."""
    home, talk, earlier = decided_as_the_revision_is_proposed(
        "finishes its revision just as the household accepts the earlier version")
    [only] = home.improvements(talk)
    assert only == earlier and only.status == "applied"
    assert where(home, talk) == ("Your household", 2, "improved", False, None)
    assert not [item for item in home.review_queue() if item.conversation == talk]


# Layer 4: none. Each layer-3 test above is one household, one decision and at most one run or send-in after it, with
# its control; its asserts name the part that broke. A child would rebuild the same household to check one of them,
# and none would be simpler to make pass:
# - the others at Your review: one run, one decline; one assert each for where the others are, the bystander's
#   improvement and its decisions, and the question.
# - a duplicate of a declined improvement: one decline, then one full run with a newcomer as control; its asserts show
#   which failed (what moved, where the duplicate is, its pointer, an improvement of its own).
# - no new or held conversation moved: one decline, then one full run as control; the places before and after show
#   which one moved, and the count of tries whether the held one got a try.
# - sent in again out of the archive: one send-in; its asserts show which failed (what moved, the round, the archive,
#   where round 2 starts, round 2's analysis).
# - proposed for and tested again: one send-in; round 2's path shows where it stopped, and its tests whether Simulate
#   validated anything. How Propose, Simulate and Review work in a round is P0.2's, P0.4's and P0.9's subtrees, the
#   same whatever took the conversation to Your household; whether a result is reused is how replays work.
# - a new improvement beside the declined one: one send-in, one read of the improvements, oldest first.
# - a note or "not an issue" on the decline: the same scenario for two inputs; a child per input would be the loop
#   unrolled, no simpler. Whether a decline's note becomes a turn isn't this test's claim.
# - decided as the revision's proposer finishes (decline, accept): one revision, one decision landing beside it; its
#   asserts show which failed (a revised version, where the conversation is, the queue).
