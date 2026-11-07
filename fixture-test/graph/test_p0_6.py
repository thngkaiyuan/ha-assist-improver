"""Layer 2 under P0.6 (rounds): how a round stops (it waits at Your review, ends in Your household, or is held by a
failing stage or a cut-off run), and what a new round gets (fresh tests) and leaves alone (the earlier round's records).

Kai decided (2026-10-26): while a revision runs as a new round, the earlier version stays up for review; a revised
version replaces it when one comes.
"""

from household import BROKEN, DECISIONS, FINE, FRICTION, NO_FIX, SMOOTH, UNCLEAR, Household
from spec_graph import refines


def improvement_for(home, talk):
    [improvement] = home.improvements(talk)
    return improvement


def waiting_for(home, talk):
    """What waits at Your review for this conversation."""
    return [item for item in home.review_queue() if item.conversation == talk]


@refines("P0.6")
def test_what_waits_at_your_review_stays_there_through_later_runs_until_the_household_answers_it():
    """An improvement and a question both stop the round at Your review, not held: a later full run leaves them there,
    and only the household's answer sends the conversation on. A revision starts a new round; a question's answer
    takes the conversation back to the stage that asked, and the round goes on. A newcomer gives the full run work to
    do."""
    for kind in (FRICTION, UNCLEAR):
        home = Household()
        talk = home.converse(kind)
        home.run()
        assert home.place(talk).stage == "Your review" and not home.place(talk).held
        newcomer = home.converse(FRICTION)
        assert home.moved(home.run("full")) == {newcomer}
        assert home.place(talk).stage == "Your review" and home.place(talk).round == 1

        if kind == FRICTION:
            answered = home.decide(improvement_for(home, talk), "revise", "only the left door").run
            assert home.moved(answered) == {talk} and home.place(talk).round == 2
        else:
            answered = home.answer(talk, "the left door")
            assert home.moved(answered) == {talk} and home.place(talk).round == 1
            assert home.path(talk)[2:5] == ["Analyze", "Your review", "Analyze"]


@refines("P0.6")
def test_a_question_from_propose_pauses_the_round_and_the_answer_takes_it_back_to_propose_with_the_tests_it_had_left():
    """The proposer asks after a test: the round waits at Your review with the question, and the answer takes it back
    to Propose in the same round, its limits going on (no fresh set of tests), on to an improvement."""
    home = Household()
    home.stand_in("Propose", "asks the household one question before it sends its idea on")
    talk = home.converse(FRICTION)
    home.run()
    assert home.place(talk).stage == "Your review" and home.path(talk)[-2:] == ["Propose", "Your review"]
    assert not home.improvements(talk)
    used, left = len(home.tests(talk, round=1)), home.tests_left(talk)

    assert home.moved(home.answer(talk, "the left one")) == {talk}
    assert home.place(talk).round == 1 and home.path(talk)[-4:] == ["Your review", "Propose", "Write up",
                                                                    "Your review"]
    assert len(home.tests(talk, round=1)) + home.tests_left(talk) == used + left
    [_improvement] = home.improvements(talk)


@refines("P0.6")
def test_a_round_sends_one_improvement_on_to_your_review_even_when_its_proposer_sends_on_two():
    """Within a round a conversation flows until an improvement waits at Your review: one, belonging to the
    conversation, its changes as broad as they need to be (P0.10). The proposer gets two ideas validated and sends both
    on: that's refused, and it sends on one. One improvement waits, and the other idea stays behind with its stamps."""
    home = Household()
    home.stand_in("Propose", "proposes two ideas at once and sends both on")
    talk = home.converse(FRICTION)
    home.run()
    first, second = home.tests(talk)
    assert first.validated and second.validated  # both ideas hold both stamps
    [improvement] = home.improvements(talk)
    assert waiting_for(home, talk) == [improvement] and home.place(talk).stage == "Your review"
    assert [idea for idea in home.ideas(talk) if idea.reached_write_up] == [improvement.idea]


@refines("P0.6")
def test_a_round_that_ends_skips_the_stages_after_the_one_that_ended_it():
    """A round that ends goes from the stage that ended it straight to Your household. Simulate is a stamp inside
    Propose (P0.4), so a fix that's never validated ends the round from Propose."""
    for kind, path in [(SMOOTH, ["Your Assist", "Triage"]),
                       (FINE, ["Your Assist", "Triage", "Analyze"]),
                       (NO_FIX, ["Your Assist", "Triage", "Analyze", "Propose"])]:
        home = Household()
        talk = home.converse(kind)
        home.run()
        assert home.place(talk).stage == "Your household" and home.path(talk) == [*path, "Your household"]

    home = Household()
    talk = home.converse(FINE)
    home.mark_test(talk)
    assert home.place(talk).stage == "Your household" and home.path(talk) == ["Your Assist", "Your household"]

    for decision in ("accept", "decline"):
        home = Household()
        talk = home.converse(FRICTION)
        home.run()
        home.decide(improvement_for(home, talk), decision)
        assert home.place(talk).stage == "Your household"
        assert home.path(talk) == ["Your Assist", "Triage", "Analyze", "Propose", "Write up", "Your review",
                                   "Your household"]


@refines("P0.6")
def test_a_round_whose_analysis_keeps_failing_is_held_at_analyze_where_a_round_analysed_without_trouble_ends():
    """A failing stage is a problem (P0.8 names failures): the round stops there, held, neither moved on nor ended in
    Your household (not even as couldn't be read). In the same run, a conversation analysed without trouble ends from
    Analyze."""
    home = Household()
    failing, fine = home.converse(BROKEN), home.converse(FINE)
    home.run()
    assert home.place(failing).stage == "Analyze" and home.place(failing).held and home.place(failing).round == 1
    assert home.path(failing, round=1) == ["Your Assist", "Triage", "Analyze"]
    assert home.place(fine).stage == "Your household" and home.path(fine)[-2] == "Analyze"


@refines("P0.6")
def test_archiving_ends_a_round_in_your_household_from_whichever_stage_it_has_reached():
    """Ending "from any stage" includes the household archiving a conversation wherever its round is, Write up among
    them, where no stage of its own ends a round. Blocking a stage only parks the conversation there for the test."""
    home = Household()
    talk = home.converse(FRICTION)
    home.archive(talk)
    assert home.place(talk).outcome == "archived" and home.path(talk) == ["Your Assist", "Your household"]

    for stage in ("Triage", "Analyze", "Propose", "Write up"):
        home = Household()
        home.block(stage)
        talk = home.converse(FRICTION)
        home.run()
        assert home.place(talk).stage == stage
        home.archive(talk)
        assert home.place(talk).stage == "Your household" and home.place(talk).outcome == "archived"
        assert home.path(talk)[-2:] == [stage, "Your household"]


@refines("P0.6")
def test_unarchived_a_round_the_archive_ended_midway_stands_and_a_run_takes_the_conversation_in_for_a_new_round():
    """Unarchiving puts it back in the pipeline for a new round (DETAILS P3.3.3): new, for its next run, with the note
    it was sent in with still a turn (P0.3), never back in the round the archive ended, which stays ended once. One
    archived once its round had ended in Your household is back with that round's outcome, as it stands."""
    home = Household()
    home.block("Propose")
    talk = home.converse(NO_FIX)
    home.send_in(talk, note="the relay never answers")
    assert home.place(talk).held and home.place(talk).stage == "Propose"
    home.archive(talk)
    home.unarchive(talk)
    home.unblock("Propose")
    assert home.moved(home.run("full")) == {talk}
    assert home.place(talk).round == 2 and home.place(talk).outcome == "no fix found"
    note = home.analysis(talk).turns[-1]
    assert note.key == "note" and note.goals
    assert [home.path(talk, round=number).count("Your household") for number in (1, 2)] == [1, 1]
    assert home.records(talk, round=1)["Your household"].outcome == "archived"

    home.archive(talk)
    home.unarchive(talk)
    newcomer = home.converse(FRICTION)
    assert home.moved(home.run("full")) == {newcomer}
    assert home.place(talk).round == 2 and home.place(talk).outcome == "no fix found"


@refines("P0.6")
def test_a_release_carries_the_held_round_on_with_the_tests_it_had_left_and_only_a_new_round_brings_a_full_set():
    """A held round is the same round when a run releases it: its limits carry on, so waiting for another run never
    brings fresh tests; sending the conversation in again does (DETAILS P6.3.2). A test here is one the round counted
    (a replay the cut-off stopped isn't one)."""
    home = Household()
    home.stand_in("Simulate", "cuts the first run off after one test")
    talk = home.converse(NO_FIX)
    home.run()
    assert home.place(talk).held and home.place(talk).stage == "Propose" and home.place(talk).round == 1
    assert home.path(talk, round=1)[-1] == "Propose"
    used, left = len(home.tests(talk, round=1)), home.tests_left(talk)
    assert used and left

    home.run("full")
    assert home.place(talk).round == 1 and home.place(talk).stage == "Your household"
    assert len(home.tests(talk, round=1)) == used + left

    home.send_in(talk)
    assert home.place(talk).round == 2 and len(home.tests(talk, round=2)) == used + left


WAYS_BACK_IN = ("sent in again", "revision", "conflict")


def go_back_in(home, talk, way):
    """Start the next round of a conversation whose improvement waits at Your review, the given way. Returns round 1's
    records as they stood when the next round started."""
    improvement = improvement_for(home, talk)
    if way == "sent in again":
        home.decide(improvement, "decline")  # round 1 ends declined in Your household, from where it's sent in again
        first_round = home.records(talk, round=1)
        home.send_in(talk)
    elif way == "revision":
        first_round = home.records(talk, round=1)
        home.decide(improvement, "revise", "only the left door")
    else:
        home.edit_home(improvement)
        conflicted = home.decide(improvement, "accept")  # still round 1: the household is asked first
        first_round = home.records(talk, round=1)
        home.agree(conflicted)
    return first_round


@refines("P0.6")
def test_each_way_back_in_starts_a_round_with_as_many_tests_as_the_first_and_leaves_the_first_rounds_records_alone():
    """The next round's work (a fresh analysis, a revised version) is written to its own round, never over round 1's
    records: DETAILS P4.5.1's fresh analysis replaces the old one for the new round's stages, not in round 1's record.
    A declined round stays declined."""
    for way in WAYS_BACK_IN:
        home = Household()
        home.stand_in("Propose", "after a validated idea, tries a better one while tests are left")
        talk = home.converse(FRICTION)
        home.run()
        first = home.tests(talk, round=1)
        assert first and home.tests_left(talk) == 0

        first_round = go_back_in(home, talk, way)
        assert home.place(talk).round == 2 and home.records(talk, round=1) == first_round
        assert home.tests(talk, round=1) == first
        assert len(home.tests(talk, round=2)) == len(first)


@refines("P0.6")
def test_while_a_revisions_round_runs_the_earlier_version_stays_up_for_review_open_to_accept_and_decline():
    """Not to revise again: that round is revising it. Nor is its conversation archived or marked as a test meanwhile:
    accepting or declining is the way out (P0.11), so nothing starts or ends a round the records don't show."""
    home = Household()
    talk = home.converse(FRICTION)
    home.run()
    earlier = improvement_for(home, talk)
    home.block("Propose")  # keeps the revision's round at Propose while we look
    home.decide(earlier, "revise", "only the left door")
    assert home.place(talk).round == 2 and home.place(talk).stage == "Propose"
    still = improvement_for(home, talk)
    assert still == earlier and still in waiting_for(home, talk) and set(still.decisions) == {"accept", "decline"}
    again = home.decide(still, "revise", "the right door instead")
    home.archive(talk)
    home.mark_test(talk)
    assert again.error and home.place(talk).round == 2 and home.place(talk).stage == "Propose"
    assert earlier in waiting_for(home, talk)


@refines("P0.6")
def test_a_revisions_round_tests_its_ideas_on_the_conversation_as_its_own_fresh_analysis_reads_it():
    """DETAILS P12.8.1, P6.3.7.1: a revision's round reads the conversation afresh, the revision note a turn of it,
    and that analysis is the round's: its tests judge every goal of that tree, the note's among them, never round 1's
    tree with the note's goal glued on."""
    home = Household()
    talk = home.converse(FRICTION)
    home.run()
    home.decide(improvement_for(home, talk), "revise", "only the left door")
    tree = home.analysis(talk)
    revision = tree.turns[-1]
    assert "Analyze" in home.records(talk, round=2) and revision.goals
    tests = home.tests(talk, round=2)
    assert tests and all(test.judged == set(tree.goals) for test in tests)
    assert all(set(revision.goals) <= test.judged for test in tests)


@refines("P0.6")
def test_a_revised_version_replaces_the_earlier_one_only_when_the_revisions_round_brings_one():
    """Kai: a revised version replaces the earlier one when one comes; until then (and if none comes) the earlier
    version stays up for review. Reading (SPEC.md leaves it open): a revision's round that brings none stops at Your
    review, where the earlier version waits. It doesn't end No fix found in Your household, which holds only what's
    decided (P0.12); DETAILS P6.3.6 is read as for a round with nothing waiting at Your review."""
    home = Household()
    talk = home.converse(FRICTION)
    home.run()
    earlier = improvement_for(home, talk)
    home.stand_in("Simulate", "validates nothing")  # the revision's round brings no revised version
    home.decide(earlier, "revise", "only the left door")
    waiting = waiting_for(home, talk)
    assert earlier in waiting and len(waiting) == 1
    assert home.place(talk).stage == "Your review" and home.place(talk).round == 2
    # It says so, with why, never as if nothing happened: the note, and the proposer's reason.
    [still] = waiting
    assert still.unrevised["note"] == "only the left door" and still.unrevised["why"]
    home.decide(still, "revise", "only the right door")  # Decided again: what it says is this round's alone.
    [again] = waiting_for(home, talk)
    assert again.unrevised["note"] == "only the right door"
    home.decide(again, "decline")  # Any decision clears it: it no longer stands.
    assert [i.unrevised for i in home.improvements(talk) if i.status == "declined"] == [None]

    home = Household()
    talk = home.converse(FRICTION)
    home.run()
    earlier = improvement_for(home, talk)
    home.decide(earlier, "revise", "only the left door")
    [revised] = waiting_for(home, talk)
    assert revised != earlier and earlier in home.improvements(talk)


@refines("P0.6")
def test_an_accept_that_meets_a_conflict_starts_a_new_round_only_once_the_household_sends_it_back():
    home = Household()
    talk = home.converse(FRICTION)
    home.run()
    improvement = improvement_for(home, talk)
    home.edit_home(improvement)
    conflicted = home.decide(improvement, "accept")
    assert home.place(talk).round == 1 and not conflicted.run

    home.agree(conflicted)
    assert home.place(talk).round == 2


@refines("test_an_accept_that_meets_a_conflict_starts_a_new_round_only_once_the_household_sends_it_back")
def test_a_conflicts_round_that_brings_no_new_version_stops_at_your_review_with_the_conflicted_one_up_for_a_decision():
    """Reading (SPEC.md leaves it open), as for a revision's round above: the round the household's agreeing starts
    brings no new version, and stops at Your review, where the improvement that met the conflict waits for the
    household's decision again. It doesn't end No fix found with that improvement left where nothing can be decided."""
    home = Household()
    talk = home.converse(FRICTION)
    home.run()
    improvement = improvement_for(home, talk)
    home.edit_home(improvement)
    conflicted = home.decide(improvement, "accept")
    home.stand_in("Simulate", "validates nothing")  # the conflict's round brings no new version
    home.agree(conflicted)
    assert home.place(talk).stage == "Your review" and home.place(talk).round == 2
    [waiting] = waiting_for(home, talk)
    assert waiting == improvement and set(waiting.decisions) == set(DECISIONS)


# Layer 3: one child, for the conflict's round that brings no new version: its parent stops once the household agrees,
# and the revision's case has its own test above. Each other test above is one concrete scenario on the driver, or a
# few cases that each build their own household, so a failure is already pinned to its case.
