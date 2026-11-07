"""Layer 2 under P0.4 (Stages): the line, the contracts between its stages, and the proposer's two stamps.

Review and Simulate are stamps the proposer collects, not stages: while it collects them, the conversation is at
Propose. Each test reaches the product only through household.py, and fails as Missing until the product offers it.
"""

from household import FINE, FRICTION, NO_FIX, SMOOTH, STAGES, STAMPS, UNCLEAR, Household
from spec_graph import refines

NEXT = dict(zip(STAGES, STAGES[1:]))  # The next stage in the line.
# One of each way a first round can go: ended at Triage, Analyze or Propose; waiting at Your review with a question;
# an improvement accepted; an improvement declined.
WAYS = [(SMOOTH, None), (FINE, None), (NO_FIX, None), (UNCLEAR, None), (FRICTION, "accept"), (FRICTION, "decline")]


def one_round(kind, decision=None, note=None):
    home = Household()
    talk = home.converse(kind)
    home.run()
    if decision:
        [improvement] = home.improvements(talk)
        home.decide(improvement, decision, note)
    return home, talk


def keeps_its_contract(handoff):
    """Its contract types every field of the record handed on, takes the record, and refuses it with any field missing
    or mistyped."""
    contract, carried = handoff.contract, set(handoff.record.fields)
    return bool(carried and carried <= set(contract.fields) and contract.accepts(handoff.record)
                and not any(contract.accepts(handoff.without(field)) for field in contract.fields)
                and not any(contract.accepts(handoff.mistyped(field)) for field in contract.fields))


def handed_on_from(home, talk, stage):
    return [handoff for handoff in home.handoffs(talk) if handoff.start == stage]


@refines("P0.4")
def test_a_stage_hands_a_conversation_only_to_the_next_stage_in_the_line_or_out_to_your_household():
    """Whichever way its round goes, a conversation starts at Your Assist and each move takes it one stage on, or out to
    Your household (P0.6: it can end there from any stage). None skips a stage or goes back within the round, but for
    the two moves SPEC.md names besides: a question pauses the round at Your review (P0.6), straight from the stage
    that asked, and the answer goes straight back to it (P0.7)."""
    for kind, decision in WAYS:
        home, talk = one_round(kind, decision)
        if kind == UNCLEAR:
            home.answer(talk, "the left door")
        path = home.path(talk)
        assert path[0] == "Your Assist"
        off_line = [(before, after) for before, after in zip(path, path[1:])
                    if after not in (NEXT.get(before), "Your household")]
        assert off_line == ([("Analyze", "Your review"), ("Your review", "Analyze")] if kind == UNCLEAR else [])


@refines("P0.4")
def test_a_conversation_filed_from_assist_or_with_no_triage_model_still_passes_through_triage():
    """A clear sign (filed from Assist, DETAILS P9.3.1) or no Triage model (DETAILS P9.2.1) skips the quick check, not
    the stage: the conversation still goes from Your Assist to Triage to Analyze."""
    home = Household()
    talk = home.converse(FRICTION, filed=True)  # the fast lane runs it as soon as it ends (P0.7)
    assert home.path(talk)[:3] == ["Your Assist", "Triage", "Analyze"]

    home = Household()
    home.stand_in("Triage", "has no model chosen")
    talk = home.converse(FRICTION)
    home.run()
    assert home.path(talk)[:3] == ["Your Assist", "Triage", "Analyze"]


@refines("P0.4")
def test_the_proposers_trips_to_review_and_simulate_stay_inside_propose():
    """Review asks for changes once and Simulate doesn't validate one idea, so the proposer goes back from each; the
    conversation still goes through the line once, in order, every moment of it at a stage of the line. A run cut off
    while Review or Simulate works on an idea (held, P0.8) shows where the conversation is meanwhile: at Propose, never
    at a Review or Simulate stage."""
    home = Household()
    home.stand_in("Review", "asks for changes to the first idea only")
    home.stand_in("Simulate", "doesn't validate the first idea it tests")
    talk = home.converse(FRICTION)
    home.run()
    assert {trip.start for trip in home.trips_back(talk)} == {"Review", "Simulate"}
    [improvement] = home.improvements(talk)
    home.decide(improvement, "accept")
    assert home.path(talk) == list(STAGES)
    trace = home.trace(talk)
    assert trace and all(set(moment.stages) <= set(STAGES) for moment in trace)

    for stamp in STAMPS:
        home = Household()
        home.stand_in(stamp, "is cut off while it works on the first idea")
        talk = home.converse(FRICTION)
        home.run()
        assert home.place(talk).stage == "Propose" and home.place(talk).held
        assert home.trace(talk)[-1].stages == ["Propose"]


@refines("P0.4")
def test_a_conversation_whose_review_or_simulate_keeps_crashing_is_held_at_propose():
    """A stamp the proposer can't get holds the conversation where the proposer is, at Propose, with why (P0.8), not at
    a stage of the stamp's own."""
    for stamp in STAMPS:
        home = Household()
        home.stand_in(stamp, "crashes every time")
        talk = home.converse(FRICTION)
        home.run()
        assert home.place(talk).stage == "Propose" and home.place(talk).held


@refines("P0.4")
def test_every_handoff_has_a_contract_that_types_every_field_it_hands_on_and_refuses_one_missing_or_mistyped():
    """Every move a conversation makes is a handoff with a contract, the ways out to Your household, a question's way to
    Your review and its answer's way back, and a revision's way back to Propose included. The contract types every
    field of the record handed on, not just some of them (DETAILS P3.2.1), takes the record, and refuses it with any
    field missing, or holding a value its type doesn't allow (DETAILS P3.2.2: a field that steers what happens next has
    a fixed set of values)."""
    for kind, decision in WAYS:
        home, talk = one_round(kind, decision)
        if kind == UNCLEAR:
            home.answer(talk, "the left door")
        path, handoffs = home.path(talk), home.handoffs(talk)
        assert [(handoff.start, handoff.end) for handoff in handoffs] == list(zip(path, path[1:]))
        assert all(keeps_its_contract(handoff) for handoff in handoffs)

    home, talk = one_round(FRICTION, "revise", "only the left door")
    handoffs = home.handoffs(talk)
    assert ("Your review", "Propose") in [(handoff.start, handoff.end) for handoff in handoffs]
    assert all(keeps_its_contract(handoff) for handoff in handoffs)


@refines("P0.4")
def test_a_stage_whose_answer_breaks_its_contract_hands_nothing_on_and_holds_the_conversation():
    """An answer without a field the next stage reads is never handed on: the conversation stays at that stage, held
    with why (P0.8; DETAILS P3.2.3). Shown for Analyze and Propose; Triage and Write up may instead hand on a fallback
    that keeps the contract (DETAILS P9.2, P12.1.8), left to the next layer."""
    for stage in ("Analyze", "Propose"):
        home = Household()
        talk = home.converse(FRICTION)
        home.run()
        assert handed_on_from(home, talk, stage)

        home = Household()
        home.stand_in(stage, "answers without a field the next stage reads")
        talk = home.converse(FRICTION)
        home.run()
        assert not handed_on_from(home, talk, stage)
        assert home.place(talk).stage == stage and home.place(talk).held


@refines("P0.4")
def test_a_better_idea_tried_after_a_validated_one_goes_on_only_with_both_stamps_of_its_own():
    """Stamps belong to the one idea that earned them. A better idea the proposer tries next (P0.9) doesn't inherit the
    first one's: when it misses a stamp, it stays behind and the first idea goes on."""
    for stamp, behaviour in [("Review", "approves only the first idea"), ("Simulate", "validates only the first idea")]:
        home = Household()
        home.stand_in("Propose", "after a validated idea, tries a better one while tests are left")
        home.stand_in(stamp, behaviour)
        talk = home.converse(FRICTION)
        home.run()
        first, *better = home.ideas(talk)
        assert set(first.stamps) == set(STAMPS) and first.reached_write_up
        assert better and all(stamp not in idea.stamps and not idea.reached_write_up for idea in better)


@refines("P0.4")
def test_when_no_idea_earns_both_stamps_none_reaches_write_up_not_even_one_holding_the_other_stamp():
    """Review or Simulate refuses every idea while the proposer asks for both stamps on each. One stamp isn't enough:
    the proposer doesn't send its best idea on anyway, and the conversation never reaches Write up. Approval comes first
    (a real proposer once put each stamp on a different wording of one idea), so with Review refusing no idea is tested
    at all, and with Simulate refusing ideas hold the reviewer's stamp alone. Without the refusal, ideas do reach Write
    up, every one of them with both stamps."""
    for refusing in ("Review", "Simulate"):
        home = Household()
        home.stand_in("Propose", "asks for both stamps on every idea")
        home.stand_in(refusing, "refuses every idea")
        talk = home.converse(FRICTION)
        home.run()
        ideas = home.ideas(talk)
        if refusing == "Review":
            assert ideas and not any("Simulate" in idea.stamps for idea in ideas) and not home.tests(talk)
        else:
            assert any(idea.stamps == ["Review"] for idea in ideas)
        assert not any(idea.reached_write_up for idea in ideas) and "Write up" not in home.path(talk)

    home = Household()
    home.stand_in("Propose", "asks for both stamps on every idea")
    talk = home.converse(FRICTION)
    home.run()
    reached = [idea for idea in home.ideas(talk) if idea.reached_write_up]
    assert reached and all(set(idea.stamps) == set(STAMPS) for idea in reached)
    assert "Write up" in home.path(talk)


@refines("P0.4")
def test_the_improvement_up_for_review_is_an_idea_that_reached_write_up_with_both_stamps_and_its_validating_test():
    """What goes on to Write up and up for review is built from one of the proposer's ideas that reached Write up with
    both stamps, and carries the validated test behind its Simulate stamp."""
    home, talk = one_round(FRICTION)
    [improvement] = home.improvements(talk)
    assert improvement.idea in home.ideas(talk) and improvement.idea.reached_write_up
    assert set(improvement.idea.stamps) == set(STAMPS)
    assert improvement.test.idea == improvement.idea and improvement.test.validated


# Layer 3: children of the layer-2 tests above, each refining its parent by name.

def every_handoff():
    """The handoffs of one first round per way it can go, and of a revision's rounds, its way back included."""
    rounds = [one_round(kind, decision) for kind, decision in WAYS]
    rounds.append(one_round(FRICTION, "revise", "only the left door"))
    return [handoff for home, talk in rounds for handoff in home.handoffs(talk)]


def moves(handoffs):
    """Each handoff as the move it made: (the stage it left, the stage it entered)."""
    return [(handoff.start, handoff.end) for handoff in handoffs]


@refines("test_every_handoff_has_a_contract_that_types_every_field_it_hands_on_and_refuses_one_missing_or_mistyped")
def test_every_move_a_conversation_makes_is_one_handoff_from_the_stage_it_left_to_the_stage_it_entered():
    """One handoff per move, in the order of its path, whichever way its round goes, the ways out to Your household
    included; a revision's way back from Your review to Propose is a handoff too."""
    for kind, decision in WAYS:
        home, talk = one_round(kind, decision)
        path = home.path(talk)
        assert moves(home.handoffs(talk)) == list(zip(path, path[1:]))

    home, talk = one_round(FRICTION, "revise", "only the left door")
    assert ("Your review", "Propose") in moves(home.handoffs(talk))


@refines("test_every_handoff_has_a_contract_that_types_every_field_it_hands_on_and_refuses_one_missing_or_mistyped")
def test_each_handoffs_contract_types_every_field_its_record_hands_on_and_takes_that_record():
    """Not just some of the fields (DETAILS P3.2.1): every field the record carries has its type in the contract. A
    failure names the moves whose contract falls short."""
    handoffs = every_handoff()
    assert handoffs
    assert moves(handoff for handoff in handoffs if not handoff.record.fields) == []
    assert moves(handoff for handoff in handoffs
                 if not set(handoff.record.fields) <= set(handoff.contract.fields)) == []
    assert moves(handoff for handoff in handoffs if not handoff.contract.accepts(handoff.record)) == []


@refines("test_every_handoff_has_a_contract_that_types_every_field_it_hands_on_and_refuses_one_missing_or_mistyped")
def test_each_handoffs_contract_refuses_its_record_with_any_one_field_missing():
    """Reading: a contract names the fields the next stage reads (DETAILS P3.2.1), so none of them is optional. The
    whole record is taken, so a refusal comes from the missing field alone."""
    handoffs = every_handoff()
    assert handoffs
    assert all(handoff.contract.fields and handoff.contract.accepts(handoff.record) for handoff in handoffs)
    assert moves(handoff for handoff in handoffs
                 if any(handoff.contract.accepts(handoff.without(field)) for field in handoff.contract.fields)) == []


@refines("test_every_handoff_has_a_contract_that_types_every_field_it_hands_on_and_refuses_one_missing_or_mistyped")
def test_each_handoffs_contract_refuses_its_record_with_any_one_field_holding_a_value_its_type_refuses():
    """A field that steers what happens next has a fixed set of values (DETAILS P3.2.2): a value outside it is mistyped
    too, even one of the right kind. Reading: the household's decision steers where a conversation goes from Your
    review (P0.11), so every contract out of Your review names such a field. The whole record is taken, so a refusal
    comes from the one field alone."""
    handoffs = every_handoff()
    assert handoffs
    assert all(handoff.contract.fields and handoff.contract.accepts(handoff.record) for handoff in handoffs)
    assert moves(handoff for handoff in handoffs
                 if any(handoff.contract.accepts(handoff.mistyped(field)) for field in handoff.contract.fields)) == []
    from_review = [handoff for handoff in handoffs if handoff.start == "Your review"]
    assert from_review and all(handoff.contract.steering for handoff in from_review)
    assert moves(handoff for handoff in handoffs
                 if any(handoff.contract.accepts(handoff.outside(field)) for field in handoff.contract.steering)) == []


def stamped(home, talk, improvement):
    """Both stamps on exactly its changes, read apart from the product's own stamps: the stand-in reviewer's latest
    verdict on those changes, in its own log, approved them, and a test of exactly those changes validated."""
    verdicts = [review.verdict for review in home.reviews(talk) if review.changes == improvement.changes]
    return (bool(verdicts) and verdicts[-1] == "approve"
            and any(test.validated for test in home.tests(talk) if test.changes == improvement.changes))


@refines("test_the_improvement_up_for_review_is_an_idea_that_reached_write_up_with_both_stamps_and_its_validating_test")
def test_the_changes_up_for_review_are_the_exact_changes_the_reviewer_last_approved_and_simulate_validated():
    """An idea goes on to Write up only with both stamps, and a stamp is on exactly the changes it was given for. Once
    its idea is approved, the proposer tests and sends on a changed version under the same title without asking the
    reviewer again: no improvement carries that version. The control, the same household without that proposer, has an
    improvement, its changes approved and validated."""
    for behaviour in (None, "after its idea is approved, tests and sends on a changed version under the same title "
                            "without asking again"):
        home = Household()
        if behaviour:
            home.stand_in("Propose", behaviour)
        talk = home.converse(FRICTION)
        home.run()
        improvements = home.improvements(talk)
        assert all(stamped(home, talk, improvement) for improvement in improvements)
        if behaviour is None:  # The control.
            assert improvements


@refines("test_the_improvement_up_for_review_is_an_idea_that_reached_write_up_with_both_stamps_and_its_validating_test")
def test_an_idea_sent_on_under_another_ideas_title_is_one_idea_in_the_records_carrying_the_stamps_its_changes_earned():
    """With P0.2's one record, which nothing works out differently: idea A is approved and validated, idea B is
    approved and never tested, and the proposer sends on A's changes under B's title. The improvement carries A's
    changes, approved and validated; the Propose record holds one idea for them, which reached Write up with both
    stamps, and no idea that reached Write up without both."""
    home = Household()
    home.stand_in("Propose", "sends on its validated idea under another idea's title")
    talk = home.converse(FRICTION)
    home.run()
    [improvement] = home.improvements(talk)
    assert stamped(home, talk, improvement)
    ideas = home.ideas(talk)
    [idea] = [idea for idea in ideas if idea.changes == improvement.changes]
    assert idea.reached_write_up and set(idea.stamps) == set(STAMPS)
    assert all(set(idea.stamps) == set(STAMPS) for idea in ideas if idea.reached_write_up)


@refines("test_a_conversation_whose_review_or_simulate_keeps_crashing_is_held_at_propose")
def test_a_proposer_that_slips_in_a_stamps_tool_call_is_told_and_its_round_goes_on_without_a_crash_or_a_retry():
    """A slip in a stamp's tool call isn't Review or Simulate crashing: Home Assistant hands an argument error back to
    the model. The proposer asks the reviewer with a goal's turn as text, and tests the idea with its changes sent as
    one JSON string, then makes both calls right. The conversation reaches Write up in that run on one Propose attempt,
    isn't held, and the round's tests count only the call made right."""
    home = Household()
    home.stand_in("Propose", "slips once in each stamp's tool call")
    talk = home.converse(FRICTION)
    home.run()
    assert "Write up" in home.path(talk) and not home.place(talk).held
    assert len(home.records(talk, round=1)["Propose"].attempts) == 1 and len(home.tests(talk)) == 1


@refines("test_a_conversation_whose_review_or_simulate_keeps_crashing_is_held_at_propose")
def test_a_reviewer_that_waits_for_two_drafts_at_once_or_again_after_its_finished_doesnt_stall_the_run():
    """Two ways the reviewer waits that aren't Review crashing: it calls wait_for_draft twice in one message, or once
    more after it's told it's finished. Either way the run ends within the harness's deadline, and the conversation
    reaches Write up in that run on one Propose attempt. Reading: the reviewer's run completing is what lets the round
    go on (a reviewer stopped early holds Propose after its retries)."""
    for behaviour in ("waits for two drafts at once", "waits again after it's told it's finished"):
        home = Household()
        home.stand_in("Review", behaviour)
        talk = home.converse(FRICTION)
        home.run()
        assert "Write up" in home.path(talk) and not home.place(talk).held
        assert len(home.records(talk, round=1)["Propose"].attempts) == 1


# Layer 4: none. Each handoff test above is one sweep of the same rounds on the driver, every check its own assert line
# naming the moves that fail it (one core makes every move, DETAILS P3.8), so a child per stage, per way or per check
# would run that same check again. Each stamps test is one scenario on the driver.
