"""Layer 2 under P0.2: one record of where a conversation is, and for each round one record per stage of its state
there, which every screen and stage reads.

Each test takes one part of the parent's claim to a case or a boundary: every place a conversation can wait or end,
archiving by hand, the stages a round never reached, the round a stage reads from, and an earlier round's records
while a later round waits and once it has run to its end.
"""

from household import (BROKEN, FINE, FRICTION, NO_FIX, SAME_CAUSE, SIDE_EFFECT, SMOOTH, STAGES, UNCLEAR, UNREADABLE,
                       Household)
from spec_graph import refines


def improvement_for(home, talk):
    [improvement] = home.improvements(talk)
    return improvement


def waiting_at(stage):
    """A household with one conversation waiting or ended at the stage: still new at Your Assist, blocked at Triage,
    Propose or Write up, held at Analyze after its analysis keeps failing, up for review, or ended without a fix."""
    home = Household()
    if stage in ("Triage", "Propose", "Write up"):
        home.block(stage)
    talk = home.converse({"Analyze": BROKEN, "Your household": NO_FIX}.get(stage, FRICTION))
    if stage != "Your Assist":
        home.run()
    assert home.place(talk).stage == stage
    return home, talk


def assert_shown_only_at(home, talk, stage):
    """Every screen that shows the household's one conversation shows it at the stage, and the stage counts, which
    count every conversation wherever it is (DETAILS P14.1.2), count it there and nowhere else."""
    assert set(home.shown(talk).values()) == {stage}
    assert {at: n for at, n in home.counts().items() if n} == {stage: 1}


@refines("P0.2")
def test_every_screen_shows_a_conversation_at_the_stage_its_record_names_at_every_stage_it_can_wait_or_end():
    """A screen may leave a conversation out (the list hides decided ones, DETAILS P14.4.1); the stage counts never
    do, held or not. Write up is blockable per DETAILS P3.6.1; waiting there, it is never shown as up for review
    (DETAILS P12.1.10)."""
    for stage in STAGES:
        home, talk = waiting_at(stage)
        assert_shown_only_at(home, talk, stage)

    home = Household()
    talk = home.converse(UNCLEAR)  # a question for the household waits at Your review (P0.11)
    home.run()
    assert home.place(talk).stage == "Your review"
    assert_shown_only_at(home, talk, "Your review")


@refines("P0.2")
def test_a_conversation_archived_while_it_waits_shows_in_your_household_on_every_screen_not_where_it_waited():
    """Archived is in Your household (P0.12), so no screen keeps showing or counting the stage it left (DETAILS
    P14.3.19)."""
    home, talk = waiting_at("Propose")
    assert_shown_only_at(home, talk, "Propose")
    home.archive(talk)
    assert home.place(talk).stage == "Your household" and home.place(talk).outcome == "archived"
    assert_shown_only_at(home, talk, "Your household")


@refines("P0.2")
def test_a_round_keeps_a_record_for_each_stage_it_reached_and_none_for_a_stage_it_never_reached():
    """Rounds that end early, are held, wait for review, or start at Propose (a revision, P0.7)."""
    kept = {}
    for kind in (SMOOTH, FINE, BROKEN, NO_FIX, FRICTION):
        home = Household()
        talk = home.converse(kind)
        home.run()
        kept[kind] = set(home.records(talk, round=1))
        assert kept[kind] == set(home.path(talk, round=1))
    assert "Analyze" not in kept[SMOOTH] and "Analyze" in kept[FINE]
    assert "Propose" not in kept[FINE] | kept[BROKEN] and "Propose" in kept[NO_FIX]
    assert "Write up" not in kept[NO_FIX] and "Write up" in kept[FRICTION]
    assert "Your household" not in kept[BROKEN] | kept[FRICTION] and "Your household" in kept[SMOOTH]

    home = Household()
    talk = home.converse(FRICTION)
    home.run()
    home.block("Propose")  # keeps the revision's round at Propose while we look
    home.decide(improvement_for(home, talk), "revise", "only the left door")
    assert set(home.records(talk, round=2)) == set(home.path(talk, round=2)) == {"Propose"}
    assert {"Triage", "Analyze"} <= set(home.records(talk, round=1))


@refines("P0.2")
def test_propose_in_a_round_sent_in_again_reads_only_that_rounds_fresh_analysis_record():
    """A fresh analysis replaces the old one for every later stage (DETAILS P4.5.1, P9.16). The note is one more turn
    (P0.3), so a fresh analysis has it and a copy of the first wouldn't. A revision starts at Propose (P0.7), with no
    analysis of its own, so it isn't this case. What the proposer was handed, from the stand-in's own log, carries the
    note: the record its Propose record names is the one it read."""
    home = Household()
    talk = home.converse(NO_FIX)
    home.run()
    first = home.records(talk, round=1)["Analyze"]
    assert first in home.seen_by("Propose", talk)

    note = "it should have worked the first time"
    home.send_in(talk, note=note)
    fresh = home.records(talk, round=2)["Analyze"]
    assert len(fresh.turns) == len(first.turns) + 1  # it read the note
    assert fresh in home.seen_by("Propose", talk, round=2)
    assert first not in home.seen_by("Propose", talk, round=2)
    handed = home.received("Propose", talk)[-1].input
    assert note in [n["quote"] for s in handed["friction_scenarios"] for n in (s.get("note") or {}).get("conduct", [])]


@refines("P0.2")
def test_screens_follow_the_current_round_while_the_earlier_rounds_records_stand_unchanged():
    """Round 1 ended in Your household; round 2 waits at a blocked Analyze. Where it is comes from the one record,
    not from round 1's last stage record."""
    home = Household()
    talk = home.converse(NO_FIX)
    home.run()
    first_round = home.records(talk, round=1)
    assert_shown_only_at(home, talk, "Your household")

    home.block("Analyze")  # keeps the new round at Analyze while we look
    home.send_in(talk)
    assert home.place(talk).round == 2 and home.place(talk).stage == "Analyze"
    assert_shown_only_at(home, talk, "Analyze")
    assert "Analyze" in home.records(talk, round=2)  # the new round's record was written, in its own round
    assert set(home.records(talk, round=2)) == set(home.path(talk, round=2))
    assert home.records(talk, round=1) == first_round


@refines("P0.2")
def test_a_round_run_to_its_end_writes_its_own_records_and_leaves_the_earlier_rounds_as_they_were():
    """Round 2 reaches the same stages round 1 did (Propose, then Your household), and each round keeps its own."""
    home = Household()
    talk = home.converse(NO_FIX)
    home.run()
    first_round = home.records(talk, round=1)

    home.send_in(talk)  # nothing blocked: the fast lane runs round 2 to its end
    assert home.place(talk).round == 2 and home.place(talk).stage == "Your household"
    second_round = home.records(talk, round=2)
    assert set(second_round) == set(home.path(talk, round=2))
    assert {"Propose", "Your household"} <= set(second_round)
    assert home.records(talk, round=1) == first_round


@refines("P0.2")
def test_in_a_first_round_what_waits_at_your_review_is_exactly_what_the_records_place_there():
    """Your review's queue reads the same record as the screens. An improvement waiting at a blocked Write up isn't
    in it (DETAILS P12.1.10). Archive and Mark as test aren't open for a conversation while its improvement waits: the
    household's decision is its way out (P0.11), so the two never part. First rounds only: once a revision sends its
    conversation back to Propose, its earlier improvement still waits by round 1's record while the conversation is at
    Propose (P0.6)."""
    home = Household()
    friction, unclear = home.converse(FRICTION), home.converse(UNCLEAR)
    talks = [friction, unclear, *(home.converse(kind) for kind in (NO_FIX, BROKEN, FINE))]
    home.run()
    at_review = {talk for talk in talks if home.place(talk).stage == "Your review"}
    assert at_review == {friction, unclear}
    assert {item.conversation for item in home.review_queue()} == at_review
    for take_out in (home.archive, home.mark_test):
        take_out(friction)
        assert home.place(friction).stage == "Your review" and home.counts()["Your review"] == len(at_review)
        assert {item.conversation for item in home.review_queue()} == at_review

    home = Household()
    home.block("Write up")
    talk = home.converse(FRICTION)
    home.run()
    assert home.place(talk).stage == "Write up"
    assert [item for item in home.review_queue() if item.conversation == talk] == []


# Layer 3: each test refines one layer-2 test above, by its function name.


@refines("test_every_screen_shows_a_conversation_at_the_stage_its_record_names_at_every_stage_it_can_wait_or_end")
def test_a_conversation_held_at_triage_analyze_or_propose_shows_and_counts_at_that_stage_while_its_record_holds_it():
    """Held for a block, or for failures once Analyze's retries are spent (P0.8). The hold is part of the one record
    of where it is: no screen moves it on or back, and no count drops it."""
    for stage, cause in [("Triage", "blocked"), ("Analyze", "failures"), ("Propose", "blocked")]:
        home, talk = waiting_at(stage)
        assert home.place(talk).held.cause == cause
        assert_shown_only_at(home, talk, stage)


@refines("test_every_screen_shows_a_conversation_at_the_stage_its_record_names_at_every_stage_it_can_wait_or_end")
def test_a_fix_waiting_at_a_blocked_write_up_shows_and_counts_at_write_up_and_only_once_written_up_at_your_review():
    """Its idea has gone on to Write up with both stamps, yet until it's written up no screen shows it as up for
    review (DETAILS P12.1.10, P14.3.8). The same kind with nothing blocked goes on to Your review."""
    home, talk = waiting_at("Write up")
    assert any(idea.reached_write_up for idea in home.ideas(talk))
    assert_shown_only_at(home, talk, "Write up")

    home, talk = waiting_at("Your review")
    assert_shown_only_at(home, talk, "Your review")


@refines("test_every_screen_shows_a_conversation_at_the_stage_its_record_names_at_every_stage_it_can_wait_or_end")
def test_a_question_for_the_household_shows_and_counts_at_your_review_while_no_trouble_found_shows_in_your_household():
    """Neither has trouble the analyzer can name; the unclear one waits for the household's answer (P0.11), so no
    screen shows it as no trouble found (DETAILS P9.9.6, P14.3.17). The fine one ends as no trouble found (P0.12)."""
    for kind, stage, outcome in [(UNCLEAR, "Your review", None), (FINE, "Your household", "no trouble found")]:
        home = Household()
        talk = home.converse(kind)
        home.run()
        assert home.place(talk).stage == stage and home.place(talk).outcome == outcome
        assert_shown_only_at(home, talk, stage)


@refines("test_every_screen_shows_a_conversation_at_the_stage_its_record_names_at_every_stage_it_can_wait_or_end")
def test_the_screens_move_one_conversation_from_your_assist_to_your_household_as_its_record_does():
    """Before any run and after its round ends, where nothing works on it: no screen keeps the stage it left."""
    home = Household()
    talk = home.converse(NO_FIX)
    assert home.place(talk).stage == "Your Assist"
    assert_shown_only_at(home, talk, "Your Assist")

    home.run()
    assert home.place(talk).stage == "Your household" and home.place(talk).outcome == "no fix found"
    assert_shown_only_at(home, talk, "Your household")


@refines("test_every_screen_shows_a_conversation_at_the_stage_its_record_names_at_every_stage_it_can_wait_or_end")
def test_every_screen_shows_each_conversation_in_your_household_with_the_outcome_its_record_names():
    """Each outcome in Your household (P0.12) has a chip of its own (DETAILS P14.3), so a screen showing another
    outcome than the record's works it out differently."""
    home = Household()
    accepted, declined = home.converse(FRICTION), home.converse(SIDE_EFFECT)
    talks = [home.converse(kind) for kind in (SAME_CAUSE, SMOOTH, FINE, NO_FIX, UNREADABLE)]  # FRICTION's covers the 1st
    tested, archived = home.converse(FINE), home.converse(FINE)
    home.mark_test(tested)
    home.archive(archived)
    home.run()
    home.decide(improvement_for(home, accepted), "accept")
    home.decide(improvement_for(home, declined), "decline")
    talks += [accepted, declined, tested, archived]
    assert len({home.place(talk).outcome for talk in talks}) == len(talks)  # all nine
    for talk in talks:
        assert set(home.shown(talk).values()) == {"Your household"}, home.place(talk).outcome


@refines("test_a_round_keeps_a_record_for_each_stage_it_reached_and_none_for_a_stage_it_never_reached")
def test_a_round_that_ends_early_keeps_records_up_to_the_stage_that_ended_it_and_one_in_your_household():
    """Triage ends a smooth one, Analyze one with no trouble, Propose one with no fix (Simulate is a stamp, P0.4, so
    no fix found ends at Propose). None of them has a record at a stage after the one that ended it."""
    ended = {SMOOTH: ["Triage"], FINE: ["Triage", "Analyze"], NO_FIX: ["Triage", "Analyze", "Propose"]}
    for kind, stages in ended.items():
        home = Household()
        talk = home.converse(kind)
        home.run()
        expected = {"Your Assist", *stages, "Your household"}
        assert set(home.records(talk, round=1)) == set(home.path(talk, round=1)) == expected


@refines("test_a_round_keeps_a_record_for_each_stage_it_reached_and_none_for_a_stage_it_never_reached")
def test_a_round_held_at_analyze_keeps_records_up_to_analyze_and_none_after_it():
    """Held once its retries fail (P0.8): the round stops there, so no later stage, Your household included, has a
    record of it."""
    home = Household()
    talk = home.converse(BROKEN)
    home.run()
    assert home.place(talk).stage == "Analyze" and home.place(talk).held.cause == "failures"
    assert set(home.records(talk, round=1)) == set(home.path(talk, round=1)) == {"Your Assist", "Triage", "Analyze"}


@refines("test_a_round_keeps_a_record_for_each_stage_it_reached_and_none_for_a_stage_it_never_reached")
def test_a_round_waiting_at_your_review_keeps_records_through_your_review_and_none_in_your_household():
    home = Household()
    talk = home.converse(FRICTION)
    home.run()
    assert home.place(talk).stage == "Your review"
    assert set(home.records(talk, round=1)) == set(home.path(talk, round=1)) == set(STAGES) - {"Your household"}


@refines("test_a_round_keeps_a_record_for_each_stage_it_reached_and_none_for_a_stage_it_never_reached")
def test_a_revision_round_keeps_only_a_propose_record_while_it_waits_there_and_round_one_keeps_the_records_it_had():
    """A revision goes straight back in at Propose (P0.7), so its round never reaches Triage or Analyze. Round 1's
    records stand unchanged, its Your review record included: the earlier version still waits there by it."""
    home = Household()
    talk = home.converse(FRICTION)
    home.run()
    first_round = home.records(talk, round=1)
    home.block("Propose")  # keeps the revision's round at Propose while we look
    home.decide(improvement_for(home, talk), "revise", "only the left door")
    assert home.place(talk).round == 2 and home.place(talk).stage == "Propose"
    assert set(home.records(talk, round=2)) == set(home.path(talk, round=2)) == {"Propose"}
    assert home.records(talk, round=1) == first_round


# Layer 4: each test refines one layer-3 test above, by its function name. One held case or early end per test, so
# one failing hides no other, and the earliest stage's case needs the least of the product. A held conversation keeps
# its record at the stage it came from, yet only the stage its one record names shows and counts it.


@refines("test_a_conversation_held_at_triage_analyze_or_propose_shows_and_counts_at_that_stage_while_its_record_holds_it")
def test_a_conversation_held_at_a_blocked_triage_shows_and_counts_at_triage_not_at_your_assist_it_came_from():
    """No AI role has worked on it: the run took it in and the block held it (P0.8). Triage counts it (DETAILS
    P4.3.2.3), and every screen names Triage for it, a place with a chip of its own (DETAILS P14.3), never the New
    it had at Your Assist."""
    home, talk = waiting_at("Triage")
    assert home.place(talk).held.cause == "blocked"
    assert "Your Assist" in home.records(talk, round=1)
    assert_shown_only_at(home, talk, "Triage")


@refines("test_a_conversation_held_at_triage_analyze_or_propose_shows_and_counts_at_that_stage_while_its_record_holds_it")
def test_a_conversation_held_at_analyze_for_failures_shows_and_counts_at_analyze_not_at_triage_it_came_from():
    """Triage passed it on; Analyze held it once its retries failed (P0.8)."""
    home, talk = waiting_at("Analyze")
    assert home.place(talk).held.cause == "failures"
    assert "Triage" in home.records(talk, round=1)
    assert_shown_only_at(home, talk, "Analyze")


@refines("test_a_conversation_held_at_triage_analyze_or_propose_shows_and_counts_at_that_stage_while_its_record_holds_it")
def test_a_conversation_held_at_a_blocked_propose_shows_and_counts_at_propose_not_at_analyze_it_came_from():
    """Analyze found its trouble and passed it on; the block holds it at Propose (P0.8)."""
    home, talk = waiting_at("Propose")
    assert home.place(talk).held.cause == "blocked"
    assert "Analyze" in home.records(talk, round=1)
    assert_shown_only_at(home, talk, "Propose")


@refines("test_a_round_that_ends_early_keeps_records_up_to_the_stage_that_ended_it_and_one_in_your_household")
def test_a_round_triage_ends_as_looked_smooth_keeps_records_at_your_assist_triage_and_your_household_only():
    """Triage's sure smooth verdict ends it (DETAILS P9.2): no other AI role works on it."""
    home = Household()
    talk = home.converse(SMOOTH)
    home.run()
    assert home.place(talk).stage == "Your household" and home.place(talk).outcome == "looked smooth"
    ended = {"Your Assist", "Triage", "Your household"}
    assert set(home.records(talk, round=1)) == set(home.path(talk, round=1)) == ended


@refines("test_a_round_that_ends_early_keeps_records_up_to_the_stage_that_ended_it_and_one_in_your_household")
def test_a_round_analyze_ends_as_no_trouble_found_keeps_records_through_analyze_and_one_in_your_household():
    """Analyze finds nothing to fix, so Propose never has it."""
    home = Household()
    talk = home.converse(FINE)
    home.run()
    assert home.place(talk).stage == "Your household" and home.place(talk).outcome == "no trouble found"
    ended = {"Your Assist", "Triage", "Analyze", "Your household"}
    assert set(home.records(talk, round=1)) == set(home.path(talk, round=1)) == ended


@refines("test_a_round_that_ends_early_keeps_records_up_to_the_stage_that_ended_it_and_one_in_your_household")
def test_a_round_that_ends_with_no_fix_found_keeps_records_through_propose_though_simulate_tested_its_ideas():
    """Simulate tested ideas and validated none. It is a stamp the proposer collects, not a stage (P0.4), so it keeps
    no record of its own, and with no idea stamped nothing reaches Write up."""
    home = Household()
    talk = home.converse(NO_FIX)
    home.run()
    assert home.place(talk).stage == "Your household" and home.place(talk).outcome == "no fix found"
    tests = home.tests(talk, round=1)
    assert tests and not any(test.validated for test in tests)
    ended = {"Your Assist", "Triage", "Analyze", "Propose", "Your household"}
    assert set(home.records(talk, round=1)) == set(home.path(talk, round=1)) == ended
