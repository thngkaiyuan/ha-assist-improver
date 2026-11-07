"""Layer 2 under P0.13 (no endless loops): each way a conversation goes back, and what pays for it.

Simulate's and Review's ways back each spend a capped budget that lasts the round; once it's spent the round ends, and
only a new round, which a person starts, brings a fresh one. In such a round they are its only trips back. The
household's own ways back (a revision, an answer, a conflict it agrees to resolve, sending in again) happen only when it
acts, one trip per act. A failing stage's retries are capped in a run, then wait for a person's run. That a run leaves
a conversation alone is read from what the run moved, not only from the trips the product logs.
"""

from household import BROKEN, FINE, FRICTION, NO_FIX, SMOOTH, UNCLEAR, Household
from spec_graph import refines


def improvement_for(home, talk):
    """The conversation's newest improvement: a revision adds a newer version beside the earlier one."""
    return home.improvements(talk)[-1]


def back_from(home, talk, start):
    """Its trips back that start at that stage, in order."""
    return [trip for trip in home.trips_back(talk) if trip.start == start]


def runs_leave_alone(home, kinds, *waiting):
    """Each run of these kinds moves a new conversation beside them, and none of them (P0.7)."""
    for kind in kinds:
        new = home.converse(FINE)
        moved = home.moved(home.run(kind))
        assert new in moved and not moved & set(waiting)


@refines("P0.13")
def test_a_test_that_finds_no_fix_sends_the_conversation_back_only_while_its_round_has_tests_left():
    """Simulate's way back ("No fix yet") spends the round's tests, which last the whole round, across its runs
    (DETAILS P4.4, P6.3.2, P7.12.2): here the first run is cut off and a manual full run picks the round up again
    (P0.8). That pick-up resumes the round; it isn't one of DETAILS P4.4's ways back. Each trip follows a test with
    tests left, so the last test ends the round No fix found (P0.12) instead of going back. The proposer would test on
    past them: only the cap stops it."""
    home = Household()
    home.stand_in("Simulate", "validates nothing; the first run is cut off once its first test is done")
    home.stand_in("Propose", "keeps testing while it can")
    talk = home.converse(FRICTION)
    home.run()
    assert home.place(talk).held and home.tests_left(talk) > 0
    home.run("full")
    trips, tests = home.trips_back(talk), home.tests(talk, round=1)
    [budget] = {trip.paid_by for trip in trips}
    assert {trip.start for trip in trips} == {"Simulate"} and budget != "person"
    assert len(trips) < len(tests) == budget.cap and home.tests_left(talk) == 0
    assert home.place(talk).round == 1 and home.place(talk).outcome == "no fix found"

    home.send_in(talk)  # a person starts a new round: the way back opens again
    assert home.place(talk).round == 2 and len(back_from(home, talk, "Simulate")) > len(trips)


@refines("P0.13")
def test_the_reviewer_sends_ideas_back_only_until_the_rounds_review_budget_is_spent():
    """Review's way back spends the round's reviews (DETAILS P4.4, P7.9.6), per round as the parent holds, so a later
    run in the same round doesn't refill them. With a reviewer that never approves and a proposer that keeps asking,
    the trips stay within the cap and the round ends No fix found. Reading: a spent budget isn't one of P0.8's reasons
    to hold, and no idea has both stamps (P0.4), so the round ends (P0.6, P0.12)."""
    home = Household()
    home.stand_in("Review", "asks for changes every time; the first run is cut off once its first review is done")
    home.stand_in("Propose", "keeps asking the reviewer while it can")
    talk = home.converse(FRICTION)
    home.run()
    assert home.place(talk).held
    home.run("full")
    trips = home.trips_back(talk)
    [budget] = {trip.paid_by for trip in trips}
    assert {trip.start for trip in trips} == {"Review"} and budget != "person" and len(trips) <= budget.cap
    assert home.place(talk).round == 1 and home.place(talk).outcome == "no fix found"

    home.send_in(talk)  # a person starts a new round: the way back opens again
    assert home.place(talk).round == 2 and len(back_from(home, talk, "Review")) > len(trips)


@refines("P0.13")
def test_each_revision_or_answer_the_household_gives_sends_a_conversation_back_once_and_runs_alone_never_do():
    """Your review's ways back need a person: a revision, or the household's answer to a question (DETAILS P4.4).
    Runs leave what waits there."""
    home = Household()
    revised, asked = home.converse(FRICTION), home.converse(UNCLEAR)
    home.run()
    runs_leave_alone(home, ("scheduled", "full"), revised, asked)
    assert back_from(home, revised, "Your review") == back_from(home, asked, "Your review") == []
    for note in ("only the left door", "and the right one after dark"):
        home.decide(improvement_for(home, revised), "revise", note)
    home.answer(asked, "I meant the study lights")
    assert [trip.paid_by for trip in back_from(home, revised, "Your review")] == ["person", "person"]
    assert [trip.paid_by for trip in back_from(home, asked, "Your review")] == ["person"]


@refines("P0.13")
def test_an_accept_that_meets_a_conflict_sends_the_conversation_back_only_once_the_household_says_yes():
    """The conflict's way back needs a person (P0.7, DETAILS P12.6): the accept that met it only asks, and no run sends
    it back."""
    home = Household()
    talk = home.converse(FRICTION)
    home.run()
    improvement = improvement_for(home, talk)
    home.edit_home(improvement)
    conflicted = home.decide(improvement, "accept")
    runs_leave_alone(home, ("scheduled", "full"), talk)
    assert conflicted.asks.about == "conflict" and back_from(home, talk, "Your review") == []
    home.agree(conflicted)
    assert [trip.paid_by for trip in back_from(home, talk, "Your review")] == ["person"]


@refines("P0.13")
def test_a_decided_conversation_goes_back_in_only_when_the_household_sends_it_in_again():
    """Your household's way back needs a person (DETAILS P4.4, P8.8.3): no run brings back what's decided, one whose
    tests ran out included."""
    home = Household()
    decided = [home.converse(kind) for kind in (NO_FIX, FINE, SMOOTH)]
    home.run()
    runs_leave_alone(home, ("scheduled", "full"), *decided)
    for talk in decided:
        assert home.place(talk).stage == "Your household" and back_from(home, talk, "Your household") == []
        home.send_in(talk)
        assert [trip.paid_by for trip in back_from(home, talk, "Your household")] == ["person"]


@refines("P0.13")
def test_a_failing_stage_retries_on_a_capped_budget_and_tries_again_only_on_a_persons_run():
    """A retry sends the conversation back into the stage it failed at, so it's a way back too (DETAILS P4.4 counts
    it; the retrying itself is P0.8's). A run's retries spend a capped budget; then the conversation is held, and each
    further try is paid by the person whose run released it, never by a scheduled run, which nobody starts by hand."""
    home = Household()
    stuck = home.converse(BROKEN)
    home.run()
    retries = back_from(home, stuck, "Analyze")
    [budget] = {trip.paid_by for trip in retries}
    assert budget != "person" and len(retries) <= budget.cap and home.place(stuck).held
    runs_leave_alone(home, ("scheduled",), stuck)
    assert back_from(home, stuck, "Analyze") == retries
    assert stuck in home.moved(home.run("full"))
    assert [trip.paid_by for trip in back_from(home, stuck, "Analyze")[len(retries):]] == ["person"]


# Layer 3: the budgets' parts, one at a time (one run, a run that picks a cut-off round up, a new round, reviews from an
# attempt that didn't finish, asks made at once), and each of the household's acts on its own. The conflict's way back
# and sending in again are one scenario each: no children.


def budgets_across_rounds(home, talk, start):
    """The budget that paid its round-1 trips back from start, and the one that pays them once it's sent in again."""
    first = back_from(home, talk, start)
    [spent] = {trip.paid_by for trip in first}
    home.send_in(talk)
    later = back_from(home, talk, start)[len(first):]
    [fresh] = {trip.paid_by for trip in later}
    assert home.place(talk).round == 2 and len(later) <= fresh.cap
    return spent, fresh


def analyze_attempts(home, talk):
    """How many tries Analyze's record holds for it in round 1 (a release keeps the round)."""
    return len(home.records(talk, round=1)["Analyze"].attempts)


@refines("test_a_test_that_finds_no_fix_sends_the_conversation_back_only_while_its_round_has_tests_left")
def test_in_one_run_each_test_but_the_last_sends_a_conversation_back_and_the_last_ends_it_no_fix_found():
    """Nothing cut off. The proposer puts one idea in each proposal (its choice, P0.9), so each test is a proposal's
    only one: each that finds no fix with tests left makes one trip, paid by the round's tests (DETAILS P7.12.2); the
    last, with none left, ends the round No fix found (DETAILS P6.3.6) instead of sending it back. The earlier trips are
    the control."""
    home = Household()
    home.stand_in("Propose", "proposes one idea at a time")
    talk = home.converse(NO_FIX)
    home.run()
    trips, tests = back_from(home, talk, "Simulate"), home.tests(talk, round=1)
    [budget] = {trip.paid_by for trip in trips}
    assert budget != "person" and 0 < len(trips) == len(tests) - 1 and len(tests) == budget.cap
    assert home.tests_left(talk) == 0 and home.place(talk).outcome == "no fix found"


@refines("test_a_test_that_finds_no_fix_sends_the_conversation_back_only_while_its_round_has_tests_left")
def test_a_run_that_picks_up_a_cut_off_round_spends_only_the_tests_that_round_had_left_on_the_same_budget():
    """The round's tests last across its runs (DETAILS P6.3.2): the run that releases it carries on with what's left,
    never a fresh allowance, and ends the round No fix found once they're spent, as one run would. Picking the round up
    resumes it, the same round, and isn't a trip back: with one idea per proposal, each test but the last makes exactly
    one trip. Reading: the trip the first test earns counts once, whether it's made before the cut-off or as the round
    resumes."""
    home = Household()
    home.stand_in("Simulate", "validates nothing; the first run is cut off once its first test is done")
    home.stand_in("Propose", "proposes one idea at a time")
    talk = home.converse(FRICTION)
    home.run()
    used, left = len(home.tests(talk, round=1)), home.tests_left(talk)
    assert home.place(talk).held.cause == "cut off" and used and left
    home.run("full")
    trips, tests = home.trips_back(talk), home.tests(talk, round=1)
    [budget] = {trip.paid_by for trip in trips}
    assert {trip.start for trip in trips} == {"Simulate"} and budget != "person"
    assert len(tests) == used + left == budget.cap and len(trips) == len(tests) - 1 and home.tests_left(talk) == 0
    assert home.place(talk).round == 1 and home.place(talk).outcome == "no fix found"


@refines("test_a_test_that_finds_no_fix_sends_the_conversation_back_only_while_its_round_has_tests_left")
def test_a_new_round_pays_simulates_trips_back_from_a_fresh_budget_with_the_same_cap():
    """Only a person starts the new round, by sending it in again (no run does: the decided conversations' test). Its
    trips are paid by a budget of its own, as large as the first round's (P0.6's fresh limits, DETAILS P6.3.7)."""
    home = Household()
    talk = home.converse(NO_FIX)
    home.run()
    assert home.place(talk).outcome == "no fix found"
    spent, fresh = budgets_across_rounds(home, talk, "Simulate")
    assert fresh != spent and fresh.cap == spent.cap


@refines("test_the_reviewer_sends_ideas_back_only_until_the_rounds_review_budget_is_spent")
def test_a_reviewer_that_never_approves_sends_ideas_back_within_the_cap_in_one_run_and_the_round_ends_no_fix_found():
    """Nothing cut off. Every trip from Review is paid by the round's one review budget; once it's spent no idea has
    Review's stamp, so none goes on (P0.4, DETAILS P7.9.7) and the round ends No fix found (P0.12)."""
    home = Household()
    home.stand_in("Review", "asks for changes to every idea")
    home.stand_in("Propose", "keeps asking the reviewer while it can")
    talk = home.converse(FRICTION)
    home.run()
    trips = back_from(home, talk, "Review")
    [budget] = {trip.paid_by for trip in trips}
    assert budget != "person" and 0 < len(trips) <= budget.cap
    assert home.place(talk).outcome == "no fix found"


@refines("test_the_reviewer_sends_ideas_back_only_until_the_rounds_review_budget_is_spent")
def test_a_run_that_picks_up_a_cut_off_round_carries_on_with_the_same_review_budget_instead_of_a_fresh_one():
    """The review budget is the round's, not each run's (SPEC.md P0.6). Reading: the first review is done once its
    request for changes is back with the proposer, so each run sends ideas back, and one budget pays for both runs. Once
    it's spent, the resumed round ends No fix found, as one run would, rather than being held or left at Propose."""
    home = Household()
    home.stand_in("Review", "asks for changes every time; the first run is cut off once its first review is done")
    home.stand_in("Propose", "keeps asking the reviewer while it can")
    talk = home.converse(FRICTION)
    home.run()
    first = back_from(home, talk, "Review")
    assert home.place(talk).held.cause == "cut off" and first
    home.run("full")
    trips = home.trips_back(talk)
    [budget] = {trip.paid_by for trip in trips}
    assert {trip.start for trip in trips} == {"Review"} and trips[:len(first)] == first
    assert len(first) < len(trips) <= budget.cap and home.place(talk).round == 1
    assert home.place(talk).outcome == "no fix found"


@refines("test_the_reviewer_sends_ideas_back_only_until_the_rounds_review_budget_is_spent")
def test_a_new_round_pays_the_reviewers_trips_back_from_a_fresh_budget_with_the_same_cap():
    home = Household()
    home.stand_in("Review", "asks for changes to every idea")
    home.stand_in("Propose", "keeps asking the reviewer while it can")
    talk = home.converse(FRICTION)
    home.run()
    assert home.place(talk).outcome == "no fix found"
    spent, fresh = budgets_across_rounds(home, talk, "Review")
    assert fresh != spent and fresh.cap == spent.cap


@refines("test_the_reviewer_sends_ideas_back_only_until_the_rounds_review_budget_is_spent")
def test_reviews_from_an_attempt_that_failed_or_was_cut_off_count_once_against_the_rounds_budget_and_stay_in_its_record():
    """Every review that asked for changes is one trip back, paid by the round's one budget within its cap, however
    its attempt ended: one that failed after its first review (the next try works), or a run cut off once its first
    review was done (a full run picks the round up). Read three ways that must agree: the trips back, the
    request_changes reviews in the round's Propose record, and those the stand-in reviewer logged. The reviewer never
    approves, so the round ends No fix found."""
    for propose, review, runs in [("fails after its first review, then works", "asks for changes to every idea", 1),
                                  (None, "asks for changes every time; the first run is cut off once its first review "
                                         "is done", 2)]:
        home = Household()
        home.stand_in("Review", review)
        if propose:
            home.stand_in("Propose", propose)
        talk = home.converse(FRICTION)
        for _ in range(runs):
            home.run("full")
        trips = back_from(home, talk, "Review")
        recorded = [r for r in home.records(talk, round=1)["Propose"].reviews if r["verdict"] == "request_changes"]
        judged = [r for r in home.reviews(talk) if r.verdict == "request_changes"]
        [budget] = {trip.paid_by for trip in trips}
        assert budget != "person" and len(trips) == len(recorded) == len(judged) <= budget.cap
        assert home.place(talk).round == 1 and home.place(talk).outcome == "no fix found"


@refines("test_the_reviewer_sends_ideas_back_only_until_the_rounds_review_budget_is_spent")
def test_asks_the_proposer_makes_in_one_message_spend_the_same_review_budget():
    """Home Assistant runs one message's tool calls at once: the proposer asks the reviewer about more ideas in one
    message than the round's budget has left. The round's reviews still never exceed its cap, every trip back is paid
    by the one budget, and each review the stand-in reviewer logged answers a draft that was asked about: the titles in
    the round's record are those it judged, in order."""
    home = Household()
    home.stand_in("Review", "asks for changes to every idea")
    home.stand_in("Propose", "asks the reviewer about more ideas in one message than the budget has left")
    talk = home.converse(FRICTION)
    home.run()
    trips, judged = back_from(home, talk, "Review"), home.reviews(talk)
    [budget] = {trip.paid_by for trip in trips}
    assert budget != "person" and len(trips) <= len(judged) <= budget.cap
    recorded = home.records(talk, round=1)["Propose"].reviews
    assert [review["hypothesis"]["title"] for review in recorded] == [review.title for review in judged]


@refines("test_each_revision_or_answer_the_household_gives_sends_a_conversation_back_once_and_runs_alone_never_do")
def test_each_revision_of_a_waiting_improvement_is_one_trip_back_paid_by_a_person_and_runs_in_between_make_none():
    """The second revision is of the version the first brought back (DETAILS P7.12.5), not of the earlier one, which
    waits until that version replaces it. Before each, scheduled and full runs move a newcomer (the control) and leave
    the waiting improvement alone."""
    home = Household()
    talk = home.converse(FRICTION)
    home.run()
    revised = []
    for count, note in enumerate(("only the left door", "and the right one after dark"), start=1):
        runs_leave_alone(home, ("scheduled", "full"), talk)
        assert len(back_from(home, talk, "Your review")) == count - 1
        waiting = improvement_for(home, talk)
        assert waiting not in revised  # the version the last revision brought back
        home.decide(waiting, "revise", note)
        revised.append(waiting)
        assert [trip.paid_by for trip in back_from(home, talk, "Your review")] == ["person"] * count


@refines("test_each_revision_or_answer_the_household_gives_sends_a_conversation_back_once_and_runs_alone_never_do")
def test_the_households_answer_to_a_question_is_one_trip_back_paid_by_a_person_and_runs_before_it_make_none():
    """The answer takes the conversation back to the stage that asked, in the same round (P0.6)."""
    home = Household()
    asked = home.converse(UNCLEAR)
    home.run()
    runs_leave_alone(home, ("scheduled", "full"), asked)
    assert back_from(home, asked, "Your review") == []
    home.answer(asked, "I meant the study lights")
    assert [trip.paid_by for trip in back_from(home, asked, "Your review")] == ["person"]


@refines("test_a_failing_stage_retries_on_a_capped_budget_and_tries_again_only_on_a_persons_run")
def test_within_one_run_each_retry_of_a_failing_stage_is_a_trip_back_on_one_capped_budget_and_then_it_is_held():
    """Each retry is one more attempt the stage records after the first (DETAILS P4.4.2: three in the run that meets the
    fault), and one trip back into it."""
    home = Household()
    stuck = home.converse(BROKEN)
    home.run()
    retries = back_from(home, stuck, "Analyze")
    [budget] = {trip.paid_by for trip in retries}
    assert budget != "person" and len(retries) <= budget.cap
    assert len(retries) == len(home.records(stuck, round=1)["Analyze"].attempts) - 1
    assert home.place(stuck).stage == "Analyze" and home.place(stuck).held.cause == "failures"


@refines("test_a_failing_stage_retries_on_a_capped_budget_and_tries_again_only_on_a_persons_run")
def test_once_held_for_failures_a_scheduled_run_tries_nothing_and_each_persons_release_tries_once_paid_by_that_person():
    """A person's run is one of P0.8's three releases: a manual full run, a run of its stage, a run that picks it. Each
    gives one try: one more attempt in Analyze's record and one trip, paid by "person"; the failure holds it again. A
    scheduled run moves a newcomer (the control) and tries nothing: no attempt, no trip."""
    home = Household()
    stuck = home.converse(BROKEN)
    home.run()
    trips, attempts = home.trips_back(stuck), analyze_attempts(home, stuck)
    runs_leave_alone(home, ("scheduled",), stuck)
    assert home.trips_back(stuck) == trips and analyze_attempts(home, stuck) == attempts
    for release in (dict(kind="full"), dict(kind="stage", stage="Analyze"), dict(kind="picked", picked=[stuck])):
        assert stuck in home.moved(home.run(**release))
        added = home.trips_back(stuck)[len(trips):]
        assert [(trip.start, trip.paid_by) for trip in added] == [("Analyze", "person")]
        assert analyze_attempts(home, stuck) == attempts + 1 and home.place(stuck).held.cause == "failures"
        trips, attempts = home.trips_back(stuck), attempts + 1


# Layer 4: a conversation held for failures, under each kind of run on its own from a fresh hold, then the parent's chain
# of three releases of different kinds on one hold. A fresh hold can't show that a release after other releases still
# gives a try (a product could cap the tries in total), so the chain has a test of its own. The other layer-3 tests need
# no children: each is already one scenario on the driver, and its asserts name the part that failed.


def held_for_failures():
    """A household with one conversation whose every analysis fails, held at Analyze after its first run's retries."""
    home = Household()
    stuck = home.converse(BROKEN)
    home.run()
    assert (home.place(stuck).stage, home.place(stuck).held.cause) == ("Analyze", "failures")
    return home, stuck


def each_release_tries_once_paid_by_a_person(home, stuck, **release):
    """Two such runs in a row, so a release of one a release held again counts the same."""
    releases_in_turn_each_try_once_paid_by_a_person(home, stuck, release, release)


def releases_in_turn_each_try_once_paid_by_a_person(home, stuck, *releases):
    """These runs in turn on one conversation held for failures: each moves it, adds one attempt to Analyze's record and
    one trip back into Analyze paid by "person", and the failure holds it there again."""
    for release in releases:
        trips, attempts = home.trips_back(stuck), analyze_attempts(home, stuck)
        assert stuck in home.moved(home.run(**release))
        added = home.trips_back(stuck)[len(trips):]
        assert [(trip.start, trip.paid_by) for trip in added] == [("Analyze", "person")]
        assert analyze_attempts(home, stuck) == attempts + 1
        assert (home.place(stuck).stage, home.place(stuck).held.cause) == ("Analyze", "failures")


@refines("test_once_held_for_failures_a_scheduled_run_tries_nothing_and_each_persons_release_tries_once_paid_by_that_person")
def test_scheduled_runs_give_a_conversation_held_for_failures_no_try_and_no_trip_while_they_move_newcomers():
    """Nobody starts a scheduled run by hand, so it pays for no try: P0.8's releases are all a person's, and nothing
    costs a paid call every run without end (DETAILS P4.4.1). Two in a row; each moves a newcomer, the control."""
    home, stuck = held_for_failures()
    trips, attempts = home.trips_back(stuck), analyze_attempts(home, stuck)
    runs_leave_alone(home, ("scheduled", "scheduled"), stuck)
    assert home.trips_back(stuck) == trips and analyze_attempts(home, stuck) == attempts
    assert home.place(stuck).held.cause == "failures"


@refines("test_once_held_for_failures_a_scheduled_run_tries_nothing_and_each_persons_release_tries_once_paid_by_that_person")
def test_each_manual_full_run_gives_a_conversation_held_for_failures_one_try_paid_by_a_person():
    home, stuck = held_for_failures()
    each_release_tries_once_paid_by_a_person(home, stuck, kind="full")


@refines("test_once_held_for_failures_a_scheduled_run_tries_nothing_and_each_persons_release_tries_once_paid_by_that_person")
def test_each_run_of_analyze_gives_a_conversation_held_there_for_failures_one_try_paid_by_a_person():
    """A stage run releases what's held at its stage (P0.7); its one try is at Analyze, where it fails again."""
    home, stuck = held_for_failures()
    each_release_tries_once_paid_by_a_person(home, stuck, kind="stage", stage="Analyze")


@refines("test_once_held_for_failures_a_scheduled_run_tries_nothing_and_each_persons_release_tries_once_paid_by_that_person")
def test_each_run_picking_a_conversation_held_for_failures_gives_it_one_try_paid_by_a_person():
    """A run that picks a held conversation releases it (P0.8)."""
    home, stuck = held_for_failures()
    each_release_tries_once_paid_by_a_person(home, stuck, kind="picked", picked=[stuck])


@refines("test_once_held_for_failures_a_scheduled_run_tries_nothing_and_each_persons_release_tries_once_paid_by_that_person")
def test_a_full_run_then_a_run_of_analyze_then_a_pick_each_give_a_conversation_held_again_one_more_try_paid_by_a_person():
    """The parent's three releases in turn on one hold. Each gives one more try, whatever kind of release and however
    many came before it (P0.8): a count of failures (DETAILS P13.4.3) holds it again, never sets it aside for good."""
    home, stuck = held_for_failures()
    releases_in_turn_each_try_once_paid_by_a_person(
        home, stuck, dict(kind="full"), dict(kind="stage", stage="Analyze"), dict(kind="picked", picked=[stuck]))
