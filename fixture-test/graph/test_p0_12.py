"""Layer 2 under P0.12 (Your household): how each kind of conversation ends there, what keeps it out of the pipeline,
and how sending it in again brings it back for a new round.

Each test reaches the product only through household.py, and fails as Missing until the product offers what it needs.
"""

from household import (BROKEN, FINE, FRICTION, NO_FIX, SAME_CAUSE, SIDE_EFFECT, SMOOTH, UNMET, UNREADABLE,
                       Household)
from spec_graph import refines


def improvement_for(home, talk):
    [improvement] = home.improvements(talk)
    return improvement


def stays_out_until_sent_in_again(home, talk):
    """No run takes a decided conversation back in, though the same run takes in a new one beside it, and it keeps its
    outcome; sending it in again does, on its own, for round 2. (Picked runs are left to P0.7.)"""
    outcome = home.place(talk).outcome
    for kind in ("full", "scheduled"):
        new = home.converse(FINE)
        moved = home.moved(home.run(kind))
        assert new in moved and talk not in moved
    assert home.place(talk).stage == "Your household" and home.place(talk).outcome == outcome
    assert home.moved(home.send_in(talk)) == {talk} and home.place(talk).round == 2


@refines("P0.12")
def test_a_pipeline_verdict_ends_a_conversation_in_your_household_and_not_even_the_deciding_stages_run_takes_it_back():
    """Ended is not held (P0.8): a stage's run releases one held there by failures, never what that stage ended. That
    includes a conversation that couldn't be read: it too can be sent in again (P0.12), not set aside for good."""
    for kind, outcome in [(SMOOTH, "looked smooth"), (FINE, "no trouble found"), (NO_FIX, "no fix found"),
                          (UNREADABLE, "couldn't be read")]:
        home = Household()
        talk = home.converse(kind)
        home.run()
        place = home.place(talk)
        assert place.stage == "Your household" and place.outcome == outcome and not place.held
        ended_at = home.path(talk)[-2]
        home.stand_in(ended_at, "fails every try")
        held = home.converse(FRICTION)
        home.run()
        assert home.place(held).stage == ended_at and home.place(held).held
        assert home.moved(home.run("stage", stage=ended_at)) == {held}
        stays_out_until_sent_in_again(home, talk)


@refines("P0.12")
def test_a_conversation_up_for_review_is_decided_only_once_the_household_accepts_or_declines_its_improvement():
    for decision, outcome in [("accept", "improved"), ("decline", "declined")]:
        home = Household()
        talk = home.converse(FRICTION)
        home.run()
        assert home.place(talk).stage == "Your review" and home.place(talk).outcome is None
        home.decide(improvement_for(home, talk), decision)
        assert home.place(talk).stage == "Your household" and home.place(talk).outcome == outcome
        stays_out_until_sent_in_again(home, talk)


@refines("P0.12")
def test_marking_as_a_test_or_archiving_takes_a_conversation_out_of_the_pipeline_from_wherever_it_stands():
    """Each is done to one conversation while new and to one held at Analyze, beside a twin left alone that the same
    full run takes in or releases. Archived is an outcome in Your household, not a flag beside a stage."""
    home = Household()
    new_test, new_archived, new_twin = (home.converse(FINE) for _ in range(3))
    home.mark_test(new_test)
    home.archive(new_archived)
    held_test, held_archived, held_twin = (home.converse(BROKEN) for _ in range(3))
    assert home.moved(home.run("full")) == {new_twin, held_test, held_archived, held_twin}
    home.mark_test(held_test)
    home.archive(held_archived)
    assert home.moved(home.run("full")) == {held_twin}
    outcomes = {new_test: "marked as a test", held_test: "marked as a test", new_archived: "archived",
                held_archived: "archived"}
    for talk, outcome in outcomes.items():
        place = home.place(talk)
        assert place.stage == "Your household" and place.outcome == outcome and not place.held
        stays_out_until_sent_in_again(home, talk)


@refines("P0.12")
def test_a_conversation_whose_trouble_another_conversations_improvement_covers_ends_as_a_duplicate_pointing_to_it():
    """Covered means an improvement exists for that trouble: the duplicate ends even while that improvement still
    waits at Your review. It gets no improvement of its own; alone, the same conversation does, and once that's
    accepted, sent in again it's never a duplicate of its own improvement."""
    home = Household()
    covering, duplicate = home.converse(FRICTION), home.converse(SAME_CAUSE)
    home.run()
    improvement = improvement_for(home, covering)
    assert improvement in home.review_queue()
    place = home.place(duplicate)
    assert place.stage == "Your household" and place.outcome == "duplicate" and place.covered_by == improvement
    assert home.improvements(duplicate) == []
    stays_out_until_sent_in_again(home, duplicate)

    home = Household()
    alone = home.converse(SAME_CAUSE)
    home.run()
    own = improvement_for(home, alone)
    assert own in home.review_queue() and home.place(alone).covered_by is None
    home.decide(own, "accept")
    home.send_in(alone)
    place = home.place(alone)
    assert place.round == 2 and place.covered_by != own


@refines("P0.12")
def test_a_duplicate_stays_decided_whatever_the_household_then_decides_on_the_improvement_covering_it():
    """An earlier round's outcome stands (P0.6) and decline does nothing more (P0.11): each decision moves the covering
    conversation, never the duplicate, which stays a duplicate in its first round, still pointing there."""
    covering_after = {"accept": ("improved", 1), "revise": (None, 2), "decline": ("declined", 1)}
    for decision, (outcome, round_) in covering_after.items():
        home = Household()
        covering, duplicate = home.converse(FRICTION), home.converse(SAME_CAUSE)
        home.run()
        home.decide(improvement_for(home, covering), decision, "only the left door" if decision == "revise" else None)
        assert (home.place(covering).outcome, home.place(covering).round) == (outcome, round_)
        place = home.place(duplicate)
        assert place.stage == "Your household" and place.outcome == "duplicate" and place.round == 1
        assert place.covered_by.conversation == covering


@refines("P0.12")
def test_archived_and_declined_conversations_are_kept_for_checking_later_improvements_against():
    """Declining archives the conversation (P0.11), so a declined one is kept for checking too."""
    home = Household()
    archived, declined = home.converse(FRICTION), home.converse(SIDE_EFFECT)
    home.archive(archived)
    home.run()
    assert archived in improvement_for(home, declined).checked_on
    home.decide(improvement_for(home, declined), "decline")
    later = home.converse(UNMET)
    home.run()
    assert {archived, declined} <= improvement_for(home, later).checked_on
    assert home.place(archived).outcome == "archived" and home.place(declined).outcome == "declined"


# Layer 3: children of the tests above, each refining its parent by name.

VERDICTS = [(SMOOTH, "looked smooth", "Triage"), (FINE, "no trouble found", "Analyze"),
            (NO_FIX, "no fix found", "Propose"), (UNREADABLE, "couldn't be read", None)]


@refines("test_a_pipeline_verdict_ends_a_conversation_in_your_household_and_not_even_the_deciding_stages_run_takes_it_back")
def test_each_pipeline_verdict_ends_its_conversation_from_the_stage_that_gave_it_with_that_outcome_and_no_hold():
    """Triage's sure smooth, Analyze's no trouble, and Propose's no validated fix (Simulate is a stamp inside Propose,
    P0.4) each end the round straight from that stage. Which stage finds a conversation no stage can read is left to
    the code."""
    for kind, outcome, stage in VERDICTS:
        home = Household()
        talk = home.converse(kind)
        home.run()
        place = home.place(talk)
        assert place.stage == "Your household" and place.outcome == outcome and not place.held, kind
        assert stage is None or home.path(talk)[-2] == stage, kind

    home = Household()  # positive control: a conversation whose analysis keeps failing is held there, not ended
    failing = home.converse(BROKEN)
    home.run()
    assert home.place(failing).stage == "Analyze" and home.place(failing).held


@refines("test_a_pipeline_verdict_ends_a_conversation_in_your_household_and_not_even_the_deciding_stages_run_takes_it_back")
def test_a_run_of_the_stage_that_ended_a_conversation_releases_what_failed_there_and_leaves_the_ended_one_as_it_was():
    """Ended isn't held (P0.8). Once the deciding stage fails every try, a new conversation is held there; that stage's
    run moves only it, and the ended one keeps its place and its records (no stage tries it again)."""
    for kind, _, _ in VERDICTS:
        home = Household()
        talk = home.converse(kind)
        home.run()
        ended_at = home.path(talk)[-2]
        home.stand_in(ended_at, "fails every try")
        held = home.converse(FRICTION)
        home.run()
        assert home.place(held).stage == ended_at and home.place(held).held, kind
        before = home.place(talk), home.records(talk, round=1)
        assert home.moved(home.run("stage", stage=ended_at)) == {held}, kind
        assert (home.place(talk), home.records(talk, round=1)) == before, kind


@refines("test_a_pipeline_verdict_ends_a_conversation_in_your_household_and_not_even_the_deciding_stages_run_takes_it_back")
def test_full_and_scheduled_runs_take_in_a_new_conversation_and_leave_one_a_pipeline_verdict_ended_exactly_as_it_was():
    """Exactly: the same place and outcome, and round 1's records unchanged (no stage tries it again). Picked runs are
    left to P0.7, as in the parent."""
    for kind, _, _ in VERDICTS:
        home = Household()
        talk = home.converse(kind)
        home.run()
        before = home.place(talk), home.records(talk, round=1)
        for run_kind in ("full", "scheduled"):
            new = home.converse(FINE)
            moved = home.moved(home.run(run_kind))
            assert new in moved and talk not in moved, (kind, run_kind)
        assert (home.place(talk), home.records(talk, round=1)) == before, kind
        assert talk in home.moved(home.send_in(talk)), kind  # positive control: the household can move it


@refines("test_a_pipeline_verdict_ends_a_conversation_in_your_household_and_not_even_the_deciding_stages_run_takes_it_back")
def test_sending_in_again_a_conversation_a_pipeline_verdict_ended_moves_only_it_into_round_2_even_one_never_read():
    """A conversation that couldn't be read isn't set aside for good: like every outcome in Your household it can be
    sent in again (P0.12). A new conversation waiting beside it stays put."""
    for kind, _, _ in VERDICTS:
        home = Household()
        talk = home.converse(kind)
        home.run()
        waiting = home.converse(FINE)
        assert home.moved(home.send_in(talk)) == {talk}, kind
        assert home.place(talk).round == 2 and home.place(waiting).stage == "Your Assist", kind
