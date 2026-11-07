"""Layer 2 of the test graph under P0.9 (Simulate): the proposer decides how to use its round's tests, and sends on
only what they validated.

Each test checks a narrower part of the parent's claim: what "validated" means, what goes on, what the proposer is
given to decide with, and that the round's tests are one fixed allowance. How a conversation is replayed is left to
the code (Kai: implementation detail, not spec).
"""

from household import FRICTION, NO_FIX, NO_GAIN, OBJECTED_AND_FILED, Household
from spec_graph import refines

TRIES_BETTER = "after a validated idea, tries a better one while tests are left"
SENDS_FIRST = "sends on its first validated idea"


def went_on(home, talk):
    return [idea for idea in home.ideas(talk) if idea.reached_write_up]


@refines("P0.9")
def test_an_idea_is_validated_only_when_its_conversation_went_easier_with_it_whatever_the_proposer_claims():
    """Validated means P0.1's improvement, shown on evidence: tried with the change, the conversation's goals came
    out easier to fulfil than without it. The proposer saying so counts for nothing."""
    for kind, easier in [(FRICTION, True), (NO_GAIN, False)]:
        home = Household()
        home.stand_in("Propose", "claims each idea it tests makes the goals easier")
        talk = home.converse(kind)
        home.run()
        tests = home.tests(talk)
        assert tests
        for test in tests:
            assert test.after.easier_than(test.before) == easier
            assert test.validated == easier


@refines("P0.9")
def test_an_idea_goes_on_only_if_its_own_test_validated_it_whatever_the_proposer_sends_on():
    """The proposer decides what it sends on, but only an idea its test validated goes on, with Simulate's stamp.
    An idea it sends on untested isn't tested for it either: spending the round's tests is the proposer's call. It
    tests two ideas (one validates) and sends on a third it never tested: nothing goes on, and the round ends No fix
    found."""
    home = Household()
    home.stand_in("Simulate", "validates only the first idea")
    home.stand_in("Propose", "tests two ideas, then sends on a third it never tested")
    talk = home.converse(FRICTION)
    home.run()
    first, second = home.tests(talk)
    assert first.validated and not second.validated
    assert len(home.ideas(talk)) == 3
    assert went_on(home, talk) == [] and home.place(talk).outcome == "no fix found"
    assert [idea for idea in home.ideas(talk) if "Simulate" in idea.stamps] == [first.idea]


@refines("P0.9")
def test_trying_to_better_a_validated_idea_sends_on_the_better_one_if_it_validates_and_the_first_one_if_not():
    """Trying to make it better first: a better idea that validates replaces the first, which never goes on beside
    it; one that doesn't validate leaves the first to go on. Reading: an idea validated in a round stays validated,
    so trying to better it never loses it, even once the round's tests run out."""
    for simulate, sent in [(None, -1), ("validates only the first idea", 0)]:
        home = Household()
        home.stand_in("Propose", TRIES_BETTER)
        if simulate:
            home.stand_in("Simulate", simulate)
        talk = home.converse(FRICTION)
        home.run()
        tests = home.tests(talk)
        assert len(tests) > 1 and tests[0].validated and home.tests_left(talk) == 0
        assert tests[0].idea != tests[-1].idea
        assert went_on(home, talk) == [tests[sent].idea]
        assert [improvement.idea for improvement in home.improvements(talk)] == [tests[sent].idea]


@refines("P0.9")
def test_each_tests_result_reaches_the_proposer_before_it_decides_to_try_again_or_send_on():
    """Simulate is the proposer's way to validate: each test's result goes back to the proposer before its next move,
    and the proposer decides that move from it. One proposer, two moves: after an idea that didn't validate, it puts
    forward another, knowing the first one's result; once one validates, it sends that one on, with tests left."""
    home = Household()
    home.stand_in("Simulate", "doesn't validate the first idea it tests")
    home.stand_in("Propose", SENDS_FIRST)
    talk = home.converse(FRICTION)
    home.run()
    first, second = home.tests(talk)
    assert not first.validated and second.validated and home.tests_left(talk) > 0
    assert first in second.idea.given and second not in second.idea.given
    assert went_on(home, talk) == [second.idea]


@refines("P0.9")
def test_a_round_has_the_same_number_of_tests_whatever_the_proposer_does_with_them():
    """The round's tests are one allowance: each test spends one. The proposer's choices decide how many it spends,
    never how many there are; one that never gets an idea validated stops when they're spent."""
    allowances = set()
    cases = [(FRICTION, SENDS_FIRST, False), (FRICTION, TRIES_BETTER, True), (NO_FIX, None, True)]
    for kind, behaviour, spent in cases:
        home = Household()
        if behaviour:
            home.stand_in("Propose", behaviour)
        talk = home.converse(kind)
        home.run()
        left = home.tests_left(talk)
        assert left >= 0 and (left == 0) == spent
        allowances.add(len(home.tests(talk)) + left)
    assert len(allowances) == 1


@refines("P0.9")
def test_a_test_tells_the_proposer_what_is_still_open_with_the_change_and_why():
    """To decide whether to spend another test on a better idea, the proposer reads what each one left open: here the
    fix leaves the fan alone, but its reply still does what the household objected to, as the judge said, and so the
    filing stays open too; each named, with why (P0.9)."""
    home = Household()
    talk = home.converse(OBJECTED_AND_FILED)
    home.run()
    [answered, *_] = home.answered(talk)
    still = {item["kind"]: item["why"] for item in answered["open"]}
    assert set(still) == {"conduct", "wrap-up"}
    assert "turn 1 still does what the household objected to" in still["conduct"] and "It still does that." in still[
        "conduct"]


# Layer 3: children where a real proposer (several tool calls in one message, its own wording) or a cut-off can make a
# parent's reading slip: bettering an idea under its own title, sending on re-typed changes, tests asked for at once,
# and a round picked up after a cut-off. The other layer-2 tests need none: what "validated" means has one setup per
# kind (what makes a replay easier is P0.1.1's subtree), and each of the others is one scenario on the driver.


@refines("test_trying_to_better_a_validated_idea_sends_on_the_better_one_if_it_validates_and_the_first_one_if_not")
def test_trying_to_better_a_validated_idea_under_its_own_title_keeps_it_when_the_reviewer_blocks_the_better_version():
    """The proposer tries a better version of its validated idea under the same title (asking again about an idea
    keeps its title), and the reviewer approves only the first version of each idea. Trying never loses the first:
    an improvement carries exactly its changes, and the round doesn't end No fix found. The better version is in the
    record without the Review stamp, and doesn't reach Write up."""
    home = Household()
    home.stand_in("Propose", "after a validated idea, tries a better version under the same title")
    home.stand_in("Review", "approves only the first version of each idea")
    talk = home.converse(FRICTION)
    home.run()
    first = home.tests(talk)[0]
    assert first.validated
    assert [improvement.changes for improvement in home.improvements(talk)] == [first.changes]
    assert home.place(talk).outcome != "no fix found"
    better = [idea for idea in home.ideas(talk) if idea.changes != first.changes]
    assert better and all("Review" not in idea.stamps and not idea.reached_write_up for idea in better)


@refines("test_an_idea_goes_on_only_if_its_own_test_validated_it_whatever_the_proposer_sends_on")
def test_an_idea_sent_on_with_changes_other_than_its_tested_ones_is_held_back_until_the_proposer_sends_the_tested_version():
    """The proposer re-types its changes when it sends them on: its script's text gains a trailing space. Told what
    that version lacks, it sends the tested version. What goes on is exactly the tested changes, and the validated idea
    goes on instead of the round ending No fix found."""
    home = Household()
    home.stand_in("Propose", "re-types its changes when it sends them on")
    talk = home.converse(FRICTION)
    home.run()
    [improvement] = home.improvements(talk)
    assert improvement.changes == improvement.test.changes and improvement.test.validated
    assert home.place(talk).outcome != "no fix found"


@refines("test_a_round_has_the_same_number_of_tests_whatever_the_proposer_does_with_them")
def test_tests_the_proposer_asks_for_in_one_message_spend_the_same_one_allowance():
    """Home Assistant runs one message's tool calls at once: the proposer tests four approved ideas in one message, two
    of them with the same changes, more than the round has tests left. Each test still spends one of the one allowance
    (the cap of the budget Simulate's trips back are paid by): tests plus tests left make the allowance, none is left
    below zero, each set of changes is tested once, and each test but the first sent the proposer back once."""
    home = Household()
    home.stand_in("Propose", "tests more ideas in one message than the round has tests left")
    talk = home.converse(FRICTION)
    home.run()
    tests, left = home.tests(talk), home.tests_left(talk)
    trips = [trip for trip in home.trips_back(talk) if trip.start == "Simulate"]
    [budget] = {trip.paid_by for trip in trips}
    assert left >= 0 and len(tests) + left == budget.cap
    assert len({test.changes for test in tests}) == len(tests) and len(trips) == len(tests) - 1


@refines("test_each_tests_result_reaches_the_proposer_before_it_decides_to_try_again_or_send_on")
def test_a_proposer_picking_up_a_cut_off_round_is_given_the_tests_that_round_already_ran():
    """The first run is cut off once its first test is done (P0.8), and the run that releases the round picks it up
    with a new proposal. Read from the stand-in's own log: that proposer was given the earlier test's result and how
    many tests the round had left (its input's tests and tests_left). The record's given for the first idea it tests
    names exactly those tests. Nothing validates, so it keeps trying."""
    home = Household()
    home.stand_in("Simulate", "validates nothing; the first run is cut off once its first test is done")
    home.stand_in("Propose", "proposes one idea at a time")
    talk = home.converse(FRICTION)
    home.run()
    earlier, left = home.tests(talk), home.tests_left(talk)
    assert home.place(talk).held and earlier and left
    second = home.run("full")
    given = next(received.input for received in home.received("Propose", talk) if received.run == second)
    assert given.get("tests_left") == left
    assert [test.get("validated") for test in given.get("tests") or []] == [test.validated for test in earlier]
    tests = home.tests(talk)
    assert len(tests) > len(earlier)
    assert [test for test in tests if test in tests[len(earlier)].idea.given] == earlier
