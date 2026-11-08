"""Layer 2 under P0.7 (runs): what each kind of run admits, in a household with a conversation in every place a run
can find one, and that what a run leaves out stays exactly as it was. Each run admits what SPEC.md lists for it; Kai
decided a run that picks a held conversation releases it (SPEC.md P0.8).

A test that says something didn't move ends with its control: a manual full run (or the household) moves it.
"""

from household import BROKEN, FINE, FRICTION, SAME_CAUSE, SIDE_EFFECT, SMOOTH, STAGES, UNCLEAR, UNMET, Household
from spec_graph import refines


def improvement_for(home, talk):
    [improvement] = home.improvements(talk)
    return improvement


def where(home, *talks):
    """Where each conversation is, what each stage recorded of it this round (P0.2), and its improvements, each with
    its status and whether it waits at Your review: an extra try, a fresh proposal or a write-up changes it even where
    the place stays the same."""
    queue = home.review_queue()
    return {talk: (home.place(talk), home.records(talk, round=home.place(talk).round),
                   {item: (item.status, item in queue) for item in home.improvements(talk)}) for talk in talks}


def held_at_propose(home, kind):
    """A new conversation held at Propose by a run's scope: its analysis fails through one scheduled run, so it's held
    at Analyze; then a run of Analyze alone analyses it and leaves it at Propose."""
    home.stand_in("Analyze", "fails every try in the next run, then answers as usual")
    talk = home.converse(kind)
    home.run("scheduled")
    home.run("stage", stage="Analyze")
    return talk


def crowded():
    """A conversation in every place a run can find one: two new at Your Assist, one held at Analyze (its analysis
    keeps failing), one held at Propose (by a run's scope), one waiting at Your review, one in Your household."""
    home = Household()
    waiting, done, failing = home.converse(FRICTION), home.converse(FINE), home.converse(BROKEN)
    home.run()
    parked = held_at_propose(home, UNMET)  # the run of Analyze tries failing again, and it fails again
    new = [home.converse(SIDE_EFFECT), home.converse(SMOOTH)]
    assert [home.place(talk).stage for talk in (*new, failing, parked, waiting, done)] == \
        ["Your Assist", "Your Assist", "Analyze", "Propose", "Your review", "Your household"]
    assert home.place(failing).held and home.place(parked).held
    return home, new, failing, parked, waiting, done


@refines("P0.7")
def test_a_manual_full_run_takes_in_every_new_conversation_and_releases_every_held_one_wherever_it_is_held():
    """What waits on the household, or is decided, isn't new or held: the full run leaves it exactly as it was (no
    fresh proposal), and the household moves it."""
    home, new, failing, parked, waiting, done = crowded()
    settled = where(home, waiting, done)
    assert home.moved(home.run("full")) == {*new, failing, parked}
    assert where(home, waiting, done) == settled
    assert home.moved(home.decide(improvement_for(home, waiting), "revise", "only the left door").run) == {waiting}
    assert home.moved(home.send_in(done)) == {done}


@refines("P0.7")
def test_a_scheduled_run_takes_in_every_new_conversation_and_leaves_every_held_one_exactly_as_it_was():
    """Exactly: the same place, the same why, the same records (no extra try)."""
    home, new, failing, parked, waiting, done = crowded()
    held = where(home, failing, parked)
    assert home.moved(home.run("scheduled")) == set(new)
    assert where(home, failing, parked) == held
    assert home.moved(home.run("full")) >= {failing, parked}


@refines("P0.7")
def test_a_picked_run_moves_exactly_the_conversations_picked_whether_new_or_held():
    """A pick releases a held conversation wherever it's held (SPEC.md P0.8, Kai's decision); nothing else moves, not
    even a conversation sent in and not yet analysed (SPEC.md: "a picked run, the picked ones"; DETAILS P5.4.2 adds
    those sent in)."""
    home, new, failing, parked, waiting, done = crowded()
    sent = home.converse(BROKEN)
    home.send_in(sent)
    assert home.place(sent).stage == "Analyze" and home.place(sent).held  # sent in, not yet analysed
    left_out = where(home, new[1], failing, sent)
    assert home.moved(home.run("picked", picked=[new[0], parked])) == {new[0], parked}
    assert where(home, new[1], failing, sent) == left_out
    assert home.moved(home.run("full")) >= {new[1], failing, sent}


@refines("P0.7")
def test_a_stage_run_releases_what_is_held_at_that_stage_and_nothing_held_elsewhere_or_new():
    """Stages after Triage. A run of Triage takes no new conversation in either: SPEC.md lists none for a stage run."""
    home, new, failing, parked, waiting, done = crowded()
    left_out = where(home, *new, failing)
    assert home.moved(home.run("stage", stage="Propose")) == {parked}
    assert where(home, *new, failing) == left_out
    assert home.moved(home.run("stage", stage="Analyze")) == {failing}
    assert home.moved(home.run("full")) >= set(new)


@refines("P0.7")
def test_a_conversation_sent_in_gets_a_run_of_its_own_as_soon_as_it_ends_and_only_it_moves():
    """Filed from Assist while it went on, or sent in from the panel once it ended (DETAILS P5.5). Its run starts once
    the conversation ends, not when Assist files it, and is there when the driver's converse (which ends it) returns."""
    home, new, failing, parked, waiting, done = crowded()
    left_out = where(home, *new, failing, parked)
    before = len(home.runs())
    filed = home.converse(FRICTION, filed=True)
    [own] = home.runs()[before:]
    assert home.moved(own) == {filed} and own.started >= home.records(filed, round=1)["Your Assist"].ended
    late = home.converse(FINE)
    assert home.moved(home.send_in(late)) == {late}
    assert where(home, *new, failing, parked) == left_out
    assert home.moved(home.run("full")) >= {*new, failing, parked}


@refines("P0.7")
def test_an_answer_goes_straight_back_to_the_stage_that_asked_on_a_run_of_its_own_and_only_it_moves():
    """The answer starts the run itself, as a revision does, and the run takes the conversation back to Analyze, which
    asked, in the same round."""
    home, new, failing, parked, waiting, done = crowded()
    asked = home.converse(UNCLEAR)
    home.run("picked", picked=[asked])
    left_out, paused = where(home, *new, failing, parked, waiting), len(home.path(asked))
    before = len(home.runs())
    answered = home.answer(asked, "the left door")
    assert home.runs()[before:] == [answered] and home.moved(answered) == {asked}
    assert home.path(asked)[paused - 2:paused + 1] == ["Analyze", "Your review", "Analyze"]
    assert home.place(asked).round == 1
    assert where(home, *new, failing, parked, waiting) == left_out
    assert home.moved(home.run("full")) >= {*new, failing, parked}


@refines("P0.7")
def test_a_revision_goes_back_in_at_propose_on_a_run_of_its_own_right_away_and_only_it_moves():
    """The decision starts the run itself; nothing waits for a full run (DETAILS P5.12)."""
    home, new, failing, parked, waiting, done = crowded()
    left_out = where(home, *new, failing, parked)
    before = len(home.runs())
    revised = home.decide(improvement_for(home, waiting), "revise", "only the left door")
    assert home.runs()[before:] == [revised.run]
    assert home.moved(revised.run) == {waiting} and home.path(waiting, round=2)[0] == "Propose"
    assert where(home, *new, failing, parked) == left_out
    assert home.moved(home.run("full")) >= {*new, failing, parked}


@refines("P0.7")
def test_an_accept_that_meets_a_conflict_sends_nothing_back_until_the_household_agrees_then_only_it_goes_in():
    home, new, failing, parked, waiting, done = crowded()
    left_out = where(home, *new, failing, parked)
    improvement = improvement_for(home, waiting)
    home.edit_home(improvement)  # Home Assistant changed since the improvement was tested
    before = len(home.runs())
    conflicted = home.decide(improvement, "accept")
    assert conflicted.asks.about == "conflict" and conflicted.run is None and len(home.runs()) == before
    assert home.place(waiting).stage == "Your review" and home.place(waiting).round == 1
    assert home.moved(home.agree(conflicted)) == {waiting} and home.path(waiting, round=2)[0] == "Propose"
    assert where(home, *new, failing, parked) == left_out
    assert home.moved(home.run("full")) >= {*new, failing, parked}


def same_cause_held():
    """A SAME_CAUSE conversation analysed and held at Propose (by a run's scope, as in crowded), and a new FRICTION one
    whose fix also removes its trouble."""
    home = Household()
    other = held_at_propose(home, SAME_CAUSE)
    assert home.place(other).stage == "Propose" and home.place(other).held
    return home, other, home.converse(FRICTION)


@refines("P0.7")
def test_a_fix_found_in_a_run_settles_no_conversation_the_run_left_out_though_the_proposer_reads_it():
    """The proposer reads the whole household (P0.10), so the run's scope holds at every stage, not only in what the
    proposer is asked (DETAILS P5.4.3). The control picks both: then the fix covers the other, a duplicate (P0.12)."""
    home, other, talk = same_cause_held()
    held = where(home, other)
    home.run("picked", picked=[talk])
    assert improvement_for(home, talk).conversation == talk and other in home.reached("Propose", talk)
    assert where(home, other) == held

    home, other, talk = same_cause_held()
    home.run("picked", picked=[talk, other])
    assert home.place(other).covered_by == improvement_for(home, talk)


# Layer 3: the children of the tests above, each in a smaller household than crowded()'s, so a failure names one
# mechanism. The fix-found-in-a-run test has none: it's already one scenario with its control.

WORKING = ("Triage", "Analyze", "Propose", "Write up")  # the stages with work of their own
NEXT = dict(zip(STAGES, STAGES[1:]))  # the stage after each


def holds():
    """One household per way a conversation can be held before Your review, each holding just one FRICTION
    conversation: for failures at each stage with work of its own (its stand-in fails every try through a scheduled
    run); sent in, with its analysis failing in the fast lane's run (so it isn't analysed yet); at Propose by a run's
    scope. Each is checked held where and why it should be, so a stage that holds its failures elsewhere fails here."""
    for way in (*WORKING, "sent in", "scope"):
        home = Household()
        if way == "scope":
            talk, expected = held_at_propose(home, FRICTION), ("Propose", "scope")
        else:
            stage = way if way in WORKING else "Analyze"
            home.stand_in(stage, "fails every try in the next run, then answers as usual")
            talk, expected = home.converse(FRICTION), (stage, "failures")
            if way == "sent in":
                home.send_in(talk)
            else:
                home.run("scheduled")
        assert (home.place(talk).stage, home.place(talk).held.cause) == expected
        yield home, talk


def new_ones(home):
    """Four new conversations: one Triage ends, one Analyze ends, one whose improvement will wait at Your review, and
    one whose analysis keeps failing."""
    return [home.converse(kind) for kind in (SMOOTH, FINE, FRICTION, BROKEN)]


def conflict(home, talk):
    """Accepting talk's improvement, waiting at Your review, after Home Assistant changed what it changes."""
    improvement = improvement_for(home, talk)
    home.edit_home(improvement)
    return home.decide(improvement, "accept")


@refines("test_a_manual_full_run_takes_in_every_new_conversation_and_releases_every_held_one_wherever_it_is_held")
def test_a_full_run_takes_in_every_new_conversation_whatever_becomes_of_it():
    home = Household()
    new = new_ones(home)
    assert home.moved(home.run("full")) == set(new)
    assert "Your Assist" not in {home.place(talk).stage for talk in new}


@refines("test_a_manual_full_run_takes_in_every_new_conversation_and_releases_every_held_one_wherever_it_is_held")
def test_a_full_run_releases_a_held_conversation_whichever_stage_holds_it_and_carries_it_to_your_review():
    for home, talk in holds():
        assert home.moved(home.run("full")) == {talk}
        assert home.place(talk).stage == "Your review" and home.place(talk).held is None


@refines("test_a_manual_full_run_takes_in_every_new_conversation_and_releases_every_held_one_wherever_it_is_held")
def test_a_full_run_leaves_what_waits_at_your_review_or_is_in_your_household_exactly_as_it_was():
    """No fresh analysis or proposal for either. The run still takes in a new one, and the household moves both."""
    home = Household()
    waiting, done = home.converse(FRICTION), home.converse(FINE)
    home.run()
    settled = where(home, waiting, done)
    new = home.converse(SMOOTH)
    assert home.moved(home.run("full")) == {new}
    assert where(home, waiting, done) == settled
    assert home.moved(home.decide(improvement_for(home, waiting), "revise", "only the left door").run) == {waiting}
    assert home.moved(home.send_in(done)) == {done}


@refines("test_a_scheduled_run_takes_in_every_new_conversation_and_leaves_every_held_one_exactly_as_it_was")
def test_a_scheduled_run_takes_in_every_new_conversation_whatever_becomes_of_it():
    home = Household()
    new = new_ones(home)
    assert home.moved(home.run("scheduled")) == set(new)
    assert "Your Assist" not in {home.place(talk).stage for talk in new}


@refines("test_a_scheduled_run_takes_in_every_new_conversation_and_leaves_every_held_one_exactly_as_it_was")
def test_a_scheduled_run_leaves_a_held_conversation_exactly_as_it_was_whichever_stage_holds_it():
    """Exactly: the same place, the same why, the same records (no try spent). The run still takes in a new one."""
    for home, talk in holds():
        held = where(home, talk)
        new = home.converse(FINE)
        assert home.moved(home.run("scheduled")) == {new}
        assert where(home, talk) == held
        assert talk in home.moved(home.run("full"))


@refines("test_a_picked_run_moves_exactly_the_conversations_picked_whether_new_or_held")
def test_a_picked_run_takes_in_the_new_conversations_picked_and_leaves_the_others_at_your_assist():
    """It works on what it takes in at every stage (DETAILS P5.4.2), so both picked ones reach Your review."""
    home = Household()
    picked, other = [home.converse(FRICTION), home.converse(UNMET)], home.converse(SIDE_EFFECT)
    left_out = where(home, other)
    assert home.moved(home.run("picked", picked=picked)) == set(picked)
    assert {home.place(talk).stage for talk in picked} == {"Your review"}
    assert where(home, other) == left_out
    assert home.moved(home.run("full")) == {other}


@refines("test_a_picked_run_moves_exactly_the_conversations_picked_whether_new_or_held")
def test_a_picked_run_releases_a_held_conversation_it_picks_whichever_stage_holds_it():
    for home, talk in holds():
        assert home.moved(home.run("picked", picked=[talk])) == {talk}
        assert home.place(talk).stage == "Your review" and home.place(talk).held is None


@refines("test_a_picked_run_moves_exactly_the_conversations_picked_whether_new_or_held")
def test_a_picked_run_leaves_a_held_conversation_it_didnt_pick_exactly_as_it_was_even_one_sent_in():
    """One of the holds was sent in and isn't analysed yet: it's left out too (SPEC.md "a picked run, the picked ones",
    over DETAILS P5.4.2). The control picks it."""
    for home, talk in holds():
        held = where(home, talk)
        new = home.converse(FINE)
        assert home.moved(home.run("picked", picked=[new])) == {new}
        assert where(home, talk) == held
        assert home.moved(home.run("picked", picked=[talk])) == {talk}


@refines("test_a_stage_run_releases_what_is_held_at_that_stage_and_nothing_held_elsewhere_or_new")
def test_a_stage_run_releases_a_conversation_held_at_its_stage_whichever_stage_it_is():
    """It works at its stage only: what it passes on waits at the next (held there by its scope, DETAILS P5.6.1)."""
    for home, talk in holds():
        stage = home.place(talk).stage
        assert home.moved(home.run("stage", stage=stage)) == {talk}
        assert home.place(talk).stage == NEXT[stage]


@refines("test_a_stage_run_releases_what_is_held_at_that_stage_and_nothing_held_elsewhere_or_new")
def test_a_stage_run_leaves_a_conversation_held_at_another_stage_exactly_as_it_was():
    for home, talk in holds():
        stage = home.place(talk).stage
        held = where(home, talk)
        others = [home.run("stage", stage=other) for other in WORKING if other != stage]
        assert not any(home.moved(run) for run in others)
        assert where(home, talk) == held
        assert home.moved(home.run("stage", stage=stage)) == {talk}


@refines("test_a_stage_run_releases_what_is_held_at_that_stage_and_nothing_held_elsewhere_or_new")
def test_a_stage_run_takes_in_no_new_conversation_not_even_a_run_of_triage():
    """SPEC.md lists none for a stage run. The same run releases what's held at its stage."""
    for home, talk in holds():
        new = home.converse(FINE)
        left_out = where(home, new)
        assert home.moved(home.run("stage", stage=home.place(talk).stage)) == {talk}
        assert where(home, new) == left_out
        assert new in home.moved(home.run("full"))


@refines("test_a_conversation_sent_in_gets_a_run_of_its_own_as_soon_as_it_ends_and_only_it_moves")
def test_a_conversation_filed_from_assist_gets_one_run_of_its_own_which_starts_once_the_conversation_ends():
    home = Household()
    filed = home.converse(FRICTION, filed=True, ends=False)
    assert home.runs() == []
    home.end(filed)
    [own] = home.runs()
    assert home.moved(own) == {filed}
    assert own.started >= home.records(filed, round=1)["Your Assist"].ended


@refines("test_a_conversation_sent_in_gets_a_run_of_its_own_as_soon_as_it_ends_and_only_it_moves")
def test_conversations_filed_from_assist_that_end_while_a_run_works_each_get_a_run_of_their_own_after_it():
    """Home Assistant's cleanup ends chat sessions together: the first one's run starts, and the others end while it
    works (DETAILS P5.5.3)."""
    home = Household()
    filed = [home.converse(kind, filed=True, ends=False) for kind in (FRICTION, FINE, SMOOTH)]
    home.end(*filed)
    runs = home.runs()
    assert [len(home.moved(run)) for run in runs] == [1, 1, 1]
    assert set().union(*map(home.moved, runs)) == set(filed)


@refines("test_a_conversation_sent_in_gets_a_run_of_its_own_as_soon_as_it_ends_and_only_it_moves")
def test_a_conversation_sent_in_from_the_panel_gets_one_run_of_its_own_at_once():
    """Not sent in, an ended conversation gets no run; sent in, it gets one there and then."""
    home = Household()
    talk = home.converse(FINE)
    assert home.runs() == []
    run = home.send_in(talk)
    assert home.runs() == [run] and home.moved(run) == {talk}


@refines("test_a_conversation_sent_in_gets_a_run_of_its_own_as_soon_as_it_ends_and_only_it_moves")
def test_the_fast_lane_leaves_every_other_new_or_held_conversation_exactly_as_it_was():
    """Held anywhere, even one sent in before whose analysis failed: the fast lane takes the one sent in (SPEC.md), and
    only a full run, its stage's run or a pick releases a held one (P0.8)."""
    for home, stuck in holds():
        new = home.converse(SMOOTH)
        left_out = where(home, stuck, new)
        sent = home.converse(FINE)
        assert home.moved(home.send_in(sent)) == {sent}
        filed = home.converse(FRICTION, filed=True)
        assert home.moved(home.runs()[-1]) == {filed}
        assert where(home, stuck, new) == left_out
        assert home.moved(home.run("full")) >= {stuck, new}


@refines("test_a_revision_goes_back_in_at_propose_on_a_run_of_its_own_right_away_and_only_it_moves")
def test_a_revision_starts_one_run_of_its_own_with_the_decision_which_takes_the_conversation_in_at_propose():
    home = Household()
    talk = home.converse(FRICTION)
    home.run()
    before = len(home.runs())
    revised = home.decide(improvement_for(home, talk), "revise", "only the left door")
    assert home.runs()[before:] == [revised.run] and home.moved(revised.run) == {talk}
    assert home.path(talk, round=2)[0] == "Propose"


@refines("test_a_revision_goes_back_in_at_propose_on_a_run_of_its_own_right_away_and_only_it_moves")
def test_a_revision_decided_while_a_run_works_gets_one_run_of_its_own_right_after_it_which_takes_it_in_at_propose():
    """The household revises while a full run analyses another conversation: the revision doesn't wait for a full run
    (DETAILS P5.12), nor join the run at work, which admitted what it moves as it started."""
    home = Household()
    talk = home.converse(FRICTION)
    home.run()  # its improvement waits at Your review
    other = home.converse(FINE)
    before = len(home.runs())
    home.meanwhile("Analyze", home.decide, improvement_for(home, talk), "revise", "only the left door")
    home.run("full")
    assert [home.moved(run) for run in home.runs()[before:]] == [{other}, {talk}]
    assert home.path(talk, round=2)[0] == "Propose"


@refines("test_a_revision_goes_back_in_at_propose_on_a_run_of_its_own_right_away_and_only_it_moves")
def test_a_revisions_run_leaves_every_new_or_held_conversation_exactly_as_it_was():
    """Held anywhere, even at Propose, where the revision goes in."""
    for home, stuck in holds():
        talk = home.converse(FRICTION)
        home.run("picked", picked=[talk])  # its improvement waits at Your review
        new = home.converse(SMOOTH)
        left_out = where(home, stuck, new)
        revised = home.decide(improvement_for(home, talk), "revise", "only the left door")
        assert home.moved(revised.run) == {talk}
        assert where(home, stuck, new) == left_out
        assert home.moved(home.run("full")) >= {stuck, new}


@refines("test_an_accept_that_meets_a_conflict_sends_nothing_back_until_the_household_agrees_then_only_it_goes_in")
def test_an_accept_that_meets_a_conflict_asks_first_and_no_run_sends_the_conversation_back_until_the_household_agrees():
    """Neither the decision nor a full run: it waits at Your review, in its round. The control: agreeing sends it in."""
    home = Household()
    talk = home.converse(FRICTION)
    home.run()
    before = len(home.runs())
    conflicted = conflict(home, talk)
    assert conflicted.asks.about == "conflict" and conflicted.run is None and len(home.runs()) == before
    assert talk not in home.moved(home.run("full"))
    assert home.place(talk).stage == "Your review" and home.place(talk).round == 1
    assert home.moved(home.agree(conflicted)) == {talk}


@refines("test_an_accept_that_meets_a_conflict_sends_nothing_back_until_the_household_agrees_then_only_it_goes_in")
def test_agreeing_to_resolve_a_conflict_starts_one_run_of_its_own_which_takes_the_conversation_in_at_propose():
    home = Household()
    talk = home.converse(FRICTION)
    home.run()
    conflicted = conflict(home, talk)
    before = len(home.runs())
    agreed = home.agree(conflicted)
    assert home.runs()[before:] == [agreed] and home.moved(agreed) == {talk}
    assert home.path(talk, round=2)[0] == "Propose"


@refines("test_an_accept_that_meets_a_conflict_sends_nothing_back_until_the_household_agrees_then_only_it_goes_in")
def test_a_conflict_agreed_to_while_a_run_works_gets_one_run_of_its_own_right_after_it_which_takes_it_in_at_propose():
    """The household agrees to resolve it while a full run analyses another conversation: it doesn't wait for a full
    run (DETAILS P5.12), nor join the run at work, which admitted what it moves as it started."""
    home = Household()
    talk = home.converse(FRICTION)
    home.run()
    conflicted = conflict(home, talk)
    other = home.converse(FINE)
    before = len(home.runs())
    home.meanwhile("Analyze", home.agree, conflicted)
    home.run("full")
    assert [home.moved(run) for run in home.runs()[before:]] == [{other}, {talk}]
    assert home.path(talk, round=2)[0] == "Propose"


@refines("test_an_accept_that_meets_a_conflict_sends_nothing_back_until_the_household_agrees_then_only_it_goes_in")
def test_the_run_that_resolves_a_conflict_leaves_every_new_or_held_conversation_exactly_as_it_was():
    """Held anywhere, even at Propose, where the conversation goes back in."""
    for home, stuck in holds():
        talk = home.converse(FRICTION)
        home.run("picked", picked=[talk])  # its improvement waits at Your review
        conflicted = conflict(home, talk)
        new = home.converse(SMOOTH)
        left_out = where(home, stuck, new)
        assert home.moved(home.agree(conflicted)) == {talk}
        assert where(home, stuck, new) == left_out
        assert home.moved(home.run("full")) >= {stuck, new}


def held_failing():
    """A BROKEN conversation held at Analyze for failures after a scheduled run: every analysis of it fails."""
    home = Household()
    talk = home.converse(BROKEN)
    home.run("scheduled")
    assert (home.place(talk).stage, home.place(talk).held.cause) == ("Analyze", "failures")
    return home, talk


def tries(home, talk):
    """How many times Analyze has tried the conversation this round, by its record."""
    return len(home.records(talk, round=1)["Analyze"].attempts)


@refines("test_a_manual_full_run_takes_in_every_new_conversation_and_releases_every_held_one_wherever_it_is_held")
def test_a_full_run_releases_a_held_conversation_every_time_though_it_fails_again_and_is_held_where_it_was():
    """Each release gives one more try (SPEC.md P0.8), not one per round; a person starts each, so it's no endless loop
    (P0.13). It moved, though it ends held at the same stage."""
    home, talk = held_failing()
    for _ in range(2):
        before = tries(home, talk)
        assert home.moved(home.run("full")) == {talk}
        assert (home.place(talk).stage, home.place(talk).held.cause) == ("Analyze", "failures")
        assert tries(home, talk) == before + 1


@refines("test_a_stage_run_releases_what_is_held_at_that_stage_and_nothing_held_elsewhere_or_new")
def test_a_stage_run_releases_a_conversation_held_at_its_stage_every_time_though_it_fails_again_and_is_held_there():
    """Each release gives one more try (SPEC.md P0.8), not one per round. It moved, though it ends held where it was."""
    home, talk = held_failing()
    for _ in range(2):
        before = tries(home, talk)
        assert home.moved(home.run("stage", stage="Analyze")) == {talk}
        assert (home.place(talk).stage, home.place(talk).held.cause) == ("Analyze", "failures")
        assert tries(home, talk) == before + 1


def settled():
    """A household with one conversation whose improvement waits at Your review and one in Your household."""
    home = Household()
    waiting, done = home.converse(FRICTION), home.converse(FINE)
    home.run()
    assert [home.place(talk).stage for talk in (waiting, done)] == ["Your review", "Your household"]
    return home, waiting, done


@refines("test_a_scheduled_run_takes_in_every_new_conversation_and_leaves_every_held_one_exactly_as_it_was")
def test_a_scheduled_run_leaves_what_waits_at_your_review_or_is_in_your_household_exactly_as_it_was():
    """Neither is new. The run still takes in a new one, and the household moves both."""
    home, waiting, done = settled()
    left_out = where(home, waiting, done)
    new = home.converse(SMOOTH)
    assert home.moved(home.run("scheduled")) == {new}
    assert where(home, waiting, done) == left_out
    assert home.moved(home.decide(improvement_for(home, waiting), "revise", "only the left door").run) == {waiting}
    assert home.moved(home.send_in(done)) == {done}


@refines("test_a_picked_run_moves_exactly_the_conversations_picked_whether_new_or_held")
def test_a_picked_run_leaves_what_waits_at_your_review_or_is_in_your_household_exactly_as_it_was():
    """Neither is picked. The run still takes in the one it picks, and the household moves both."""
    home, waiting, done = settled()
    left_out = where(home, waiting, done)
    picked = home.converse(SIDE_EFFECT)
    assert home.moved(home.run("picked", picked=[picked])) == {picked}
    assert where(home, waiting, done) == left_out
    assert home.moved(home.decide(improvement_for(home, waiting), "revise", "only the left door").run) == {waiting}
    assert home.moved(home.send_in(done)) == {done}


@refines("test_a_stage_run_releases_what_is_held_at_that_stage_and_nothing_held_elsewhere_or_new")
def test_a_stage_run_leaves_what_waits_at_your_review_or_is_in_your_household_exactly_as_it_was():
    """Neither is held at any stage: not even a run of Propose or Write up works on the improvement waiting at Your
    review. The household moves both."""
    home, waiting, done = settled()
    left_out = where(home, waiting, done)
    assert not any(home.moved(home.run("stage", stage=stage)) for stage in WORKING)
    assert where(home, waiting, done) == left_out
    assert home.moved(home.decide(improvement_for(home, waiting), "revise", "only the left door").run) == {waiting}
    assert home.moved(home.send_in(done)) == {done}


@refines("test_a_conversation_sent_in_gets_a_run_of_its_own_as_soon_as_it_ends_and_only_it_moves")
def test_the_fast_lane_leaves_what_waits_at_your_review_or_is_in_your_household_exactly_as_it_was():
    """Even when the one filed is of the same kind as the one waiting (with a cause of its own, so not covered by it):
    that improvement stays as it was. The household moves both."""
    home, waiting, done = settled()
    left_out = where(home, waiting, done)
    filed = home.converse(FRICTION, filed=True)
    assert home.moved(home.runs()[-1]) == {filed}
    sent = home.converse(FINE)
    assert home.moved(home.send_in(sent)) == {sent}
    assert where(home, waiting, done) == left_out
    assert home.moved(home.decide(improvement_for(home, waiting), "revise", "only the left door").run) == {waiting}
    assert home.moved(home.send_in(done)) == {done}


@refines("test_a_revision_goes_back_in_at_propose_on_a_run_of_its_own_right_away_and_only_it_moves")
def test_a_revisions_run_leaves_what_is_in_your_household_exactly_as_it_was():
    """The control: sent in again, it moves."""
    home, waiting, done = settled()
    left_out = where(home, done)
    revised = home.decide(improvement_for(home, waiting), "revise", "only the left door")
    assert home.moved(revised.run) == {waiting}
    assert where(home, done) == left_out
    assert home.moved(home.send_in(done)) == {done}


@refines("test_an_accept_that_meets_a_conflict_sends_nothing_back_until_the_household_agrees_then_only_it_goes_in")
def test_the_run_that_resolves_a_conflict_leaves_what_is_in_your_household_exactly_as_it_was():
    """The control: sent in again, it moves."""
    home, waiting, done = settled()
    conflicted = conflict(home, waiting)
    left_out = where(home, done)
    assert home.moved(home.agree(conflicted)) == {waiting}
    assert where(home, done) == left_out
    assert home.moved(home.send_in(done)) == {done}


# Layer 4: children of the layer-3 tests above whose ways of holding a conversation (holds()) meet different rules: one
# sent in earlier (still not the picked run's or the fast lane's), a run of Triage (it takes no new one in), and the
# stages a revision's or a conflict's run works at. The other layer-3 tests are each already one scenario on the
# driver, run per way at most; a pick releases a held conversation by one rule whichever way it's held, as a full run
# does.

FAILURES_OR_SCOPE = (*WORKING, "scope")  # every way holds() holds a conversation, but "sent in"


def held_by(way):
    """holds()'s household for one way alone: failures at a stage with work of its own (named by the stage), "sent in"
    (its analysis failed in the fast lane's run, so it isn't analysed yet), or "scope" (held at Propose by a run of
    Analyze). Checked held where and why it should be."""
    home = Household()
    if way == "scope":
        talk, expected = held_at_propose(home, FRICTION), ("Propose", "scope")
    else:
        stage = way if way in WORKING else "Analyze"
        home.stand_in(stage, "fails every try in the next run, then answers as usual")
        talk, expected = home.converse(FRICTION), (stage, "failures")
        if way == "sent in":
            home.send_in(talk)
        else:
            home.run("scheduled")
    assert (home.place(talk).stage, home.place(talk).held.cause) == expected
    return home, talk


@refines("test_a_picked_run_leaves_a_held_conversation_it_didnt_pick_exactly_as_it_was_even_one_sent_in")
def test_a_picked_run_leaves_a_conversation_held_for_failures_or_by_scope_that_it_didnt_pick_exactly_as_it_was():
    """Held for failures at any stage with work of its own, or at Propose by a run's scope. The control picks it."""
    for way in FAILURES_OR_SCOPE:
        home, stuck = held_by(way)
        held = where(home, stuck)
        new = home.converse(FINE)
        assert home.moved(home.run("picked", picked=[new])) == {new}
        assert where(home, stuck) == held
        assert home.moved(home.run("picked", picked=[stuck])) == {stuck}


@refines("test_a_picked_run_leaves_a_held_conversation_it_didnt_pick_exactly_as_it_was_even_one_sent_in")
def test_a_picked_run_leaves_a_conversation_sent_in_earlier_whose_analysis_failed_exactly_as_it_was_unless_picked():
    """Sent in and not analysed yet, it isn't picked, so it stays out: SPEC.md's "a picked run, the picked ones" (Kai's
    ruling), over DETAILS P5.4.2, which also admits those sent in. The control picks it."""
    home, stuck = held_by("sent in")
    held = where(home, stuck)
    new = home.converse(FINE)
    assert home.moved(home.run("picked", picked=[new])) == {new}
    assert where(home, stuck) == held
    assert home.moved(home.run("picked", picked=[stuck])) == {stuck}


@refines("test_a_stage_run_takes_in_no_new_conversation_not_even_a_run_of_triage")
def test_a_run_of_triage_releases_what_is_held_at_triage_and_takes_in_no_new_conversation():
    """SPEC.md lists no new conversation for a stage run (Kai's ruling). The control: a full run takes it in."""
    home, stuck = held_by("Triage")
    new = home.converse(FINE)
    left_out = where(home, new)
    assert home.moved(home.run("stage", stage="Triage")) == {stuck}
    assert where(home, new) == left_out
    assert new in home.moved(home.run("full"))


@refines("test_a_stage_run_takes_in_no_new_conversation_not_even_a_run_of_triage")
def test_a_run_of_a_later_stage_releases_what_is_held_there_and_takes_in_no_new_conversation():
    """A run of Analyze, Propose or Write up, whatever holds the conversation there. The control: a full run takes the
    new one in."""
    for way in ("Analyze", "Propose", "Write up", "sent in", "scope"):
        home, stuck = held_by(way)
        new = home.converse(FINE)
        left_out = where(home, new)
        assert home.moved(home.run("stage", stage=home.place(stuck).stage)) == {stuck}
        assert where(home, new) == left_out
        assert new in home.moved(home.run("full"))


@refines("test_the_fast_lane_leaves_every_other_new_or_held_conversation_exactly_as_it_was")
def test_the_fast_lane_leaves_a_new_conversation_nobody_sent_in_at_your_assist_exactly_as_it_was():
    """Sent in from the panel or filed from Assist, only that one goes in; one nobody sent in gets no run as it ends.
    The control: a full run takes it in."""
    home = Household()
    new = home.converse(SMOOTH, ends=False)
    going_on = home.place(new)
    assert home.end(new) is None and home.place(new) == going_on
    left_out = where(home, new)
    sent = home.converse(FINE)
    assert home.moved(home.send_in(sent)) == {sent}
    filed = home.converse(FRICTION, filed=True)
    assert home.moved(home.runs()[-1]) == {filed}
    assert where(home, new) == left_out
    assert new in home.moved(home.run("full"))


@refines("test_the_fast_lane_leaves_every_other_new_or_held_conversation_exactly_as_it_was")
def test_the_fast_lane_leaves_a_conversation_held_for_failures_or_by_scope_exactly_as_it_was():
    """Held for failures at any stage with work of its own, or at Propose by a run's scope: only a full run, its stage's
    run or a pick releases it (P0.8). The control: a full run does."""
    for way in FAILURES_OR_SCOPE:
        home, stuck = held_by(way)
        held = where(home, stuck)
        sent = home.converse(FINE)
        assert home.moved(home.send_in(sent)) == {sent}
        filed = home.converse(FRICTION, filed=True)
        assert home.moved(home.runs()[-1]) == {filed}
        assert where(home, stuck) == held
        assert stuck in home.moved(home.run("full"))


@refines("test_the_fast_lane_leaves_every_other_new_or_held_conversation_exactly_as_it_was")
def test_the_fast_lane_leaves_a_conversation_sent_in_earlier_whose_analysis_failed_exactly_as_it_was():
    """It went in on a fast lane of its own; a later one doesn't take it back in, though it was sent in and isn't
    analysed yet: what another run admitted waits for a full run (DETAILS P5.5.4), and only a full run, its stage's run
    or a pick releases a held one (P0.8). The control: a full run does."""
    home, stuck = held_by("sent in")
    held = where(home, stuck)
    sent = home.converse(FINE)
    assert home.moved(home.send_in(sent)) == {sent}
    filed = home.converse(FRICTION, filed=True)
    assert home.moved(home.runs()[-1]) == {filed}
    assert where(home, stuck) == held
    assert stuck in home.moved(home.run("full"))


@refines("test_a_revisions_run_leaves_every_new_or_held_conversation_exactly_as_it_was")
def test_a_revisions_run_leaves_a_conversation_held_at_propose_or_write_up_exactly_as_it_was_though_it_works_there():
    """Held for failures at either, or at Propose by a run's scope: the run works at both on the revised conversation
    alone (DETAILS P5.12), as its path in the new round shows. The two FRICTION conversations have causes of their own,
    so neither covers the other. The control: a full run releases the held one."""
    for way in ("Propose", "Write up", "scope"):
        home, stuck = held_by(way)
        talk = home.converse(FRICTION)
        home.run("picked", picked=[talk])  # its improvement waits at Your review
        held = where(home, stuck)
        revised = home.decide(improvement_for(home, talk), "revise", "only the left door")
        assert home.moved(revised.run) == {talk}
        path = home.path(talk, round=2)
        assert path[0] == "Propose" and "Write up" in path
        assert where(home, stuck) == held
        assert stuck in home.moved(home.run("full"))


@refines("test_a_revisions_run_leaves_every_new_or_held_conversation_exactly_as_it_was")
def test_a_revisions_run_leaves_a_new_conversation_or_one_held_before_propose_exactly_as_it_was_even_one_sent_in():
    """It takes in the revised conversation, at Propose, and nothing at Your Assist, Triage or Analyze: not a new one,
    nor one held for failures there, even one sent in and not analysed yet. The control: a full run moves both."""
    for way in ("Triage", "Analyze", "sent in"):
        home, stuck = held_by(way)
        talk = home.converse(FRICTION)
        home.run("picked", picked=[talk])  # its improvement waits at Your review
        new = home.converse(SMOOTH)
        left_out = where(home, stuck, new)
        revised = home.decide(improvement_for(home, talk), "revise", "only the left door")
        assert home.moved(revised.run) == {talk}
        assert where(home, stuck, new) == left_out
        assert home.moved(home.run("full")) >= {stuck, new}


@refines("test_the_run_that_resolves_a_conflict_leaves_every_new_or_held_conversation_exactly_as_it_was")
def test_the_run_resolving_a_conflict_leaves_a_conversation_held_at_propose_or_write_up_as_it_was_though_it_works_there():
    """Held for failures at either, or at Propose by a run's scope: the run works at both on the conflicted
    conversation alone (DETAILS P5.12), as its path in the new round shows. The two FRICTION conversations have causes
    of their own, so neither covers the other. The control: a full run releases the held one."""
    for way in ("Propose", "Write up", "scope"):
        home, stuck = held_by(way)
        talk = home.converse(FRICTION)
        home.run("picked", picked=[talk])  # its improvement waits at Your review
        conflicted = conflict(home, talk)
        held = where(home, stuck)
        assert home.moved(home.agree(conflicted)) == {talk}
        path = home.path(talk, round=2)
        assert path[0] == "Propose" and "Write up" in path
        assert where(home, stuck) == held
        assert stuck in home.moved(home.run("full"))


@refines("test_the_run_that_resolves_a_conflict_leaves_every_new_or_held_conversation_exactly_as_it_was")
def test_the_run_resolving_a_conflict_leaves_a_new_conversation_or_one_held_before_propose_as_it_was_even_one_sent_in():
    """It takes in the conflicted conversation, at Propose, and nothing at Your Assist, Triage or Analyze: not a new
    one, nor one held for failures there, even one sent in and not analysed yet. The control: a full run moves both."""
    for way in ("Triage", "Analyze", "sent in"):
        home, stuck = held_by(way)
        talk = home.converse(FRICTION)
        home.run("picked", picked=[talk])  # its improvement waits at Your review
        conflicted = conflict(home, talk)
        new = home.converse(SMOOTH)
        left_out = where(home, stuck, new)
        assert home.moved(home.agree(conflicted)) == {talk}
        assert where(home, stuck, new) == left_out
        assert home.moved(home.run("full")) >= {stuck, new}


# Layer 5: children of the three layer-4 fast-lane tests, each of which runs two fast lanes in one household (one sent
# in from the panel, one filed from Assist, DETAILS P5.5) before it checks what they left: one child per fast lane,
# so a failure names it, as P0.8's layer 4 splits its two-run tests. Both failing points at the fast lane's own run;
# one alone, at how that conversation got in. The other eight layer-4 tests have one run between what they note and
# what they check, each already one scenario on the driver (per way at most), so they stop at the code.


@refines("test_the_fast_lane_leaves_a_new_conversation_nobody_sent_in_at_your_assist_exactly_as_it_was")
def test_sending_a_conversation_in_from_the_panel_leaves_a_new_one_nobody_sent_in_at_your_assist_exactly_as_it_was():
    """The control: a full run takes it in."""
    home = Household()
    new = home.converse(SMOOTH)
    left_out = where(home, new)
    sent = home.converse(FINE)
    assert home.moved(home.send_in(sent)) == {sent}
    assert where(home, new) == left_out
    assert new in home.moved(home.run("full"))


@refines("test_the_fast_lane_leaves_a_new_conversation_nobody_sent_in_at_your_assist_exactly_as_it_was")
def test_filing_a_conversation_from_assist_leaves_a_new_one_nobody_sent_in_at_your_assist_exactly_as_it_was():
    """The filed conversation's run starts once it ends. The control: a full run takes the other one in."""
    home = Household()
    new = home.converse(SMOOTH)
    left_out = where(home, new)
    filed = home.converse(FRICTION, filed=True)
    assert home.moved(home.runs()[-1]) == {filed}
    assert where(home, new) == left_out
    assert new in home.moved(home.run("full"))


@refines("test_the_fast_lane_leaves_a_conversation_held_for_failures_or_by_scope_exactly_as_it_was")
def test_sending_a_conversation_in_from_the_panel_leaves_one_held_for_failures_or_by_scope_exactly_as_it_was():
    """Held for failures at any stage with work of its own, or at Propose by a run's scope. Its problem has passed (the
    stand-in answers as usual again; the run whose scope held it is over), yet it stays held: only a full run, its
    stage's run or a pick releases it (SPEC.md P0.8, over DETAILS P4.3, where it can go back on by itself). The one sent
    in goes through every stage with work of its own, the held one's too, and has a cause of its own, so its fix
    doesn't cover the held one. The control: a full run releases it."""
    for way in FAILURES_OR_SCOPE:
        home, stuck = held_by(way)
        held = where(home, stuck)
        sent = home.converse(FRICTION)
        assert home.moved(home.send_in(sent)) == {sent}
        assert where(home, stuck) == held
        assert stuck in home.moved(home.run("full"))


@refines("test_the_fast_lane_leaves_a_conversation_held_for_failures_or_by_scope_exactly_as_it_was")
def test_filing_a_conversation_from_assist_leaves_one_held_for_failures_or_by_scope_exactly_as_it_was():
    """Held as in its sibling, its problem passed. The one filed has a cause of its own, so its fix doesn't cover the
    held one, which stays held, not a duplicate. The control: a full run releases it."""
    for way in FAILURES_OR_SCOPE:
        home, stuck = held_by(way)
        held = where(home, stuck)
        filed = home.converse(FRICTION, filed=True)
        assert home.moved(home.runs()[-1]) == {filed}
        assert where(home, stuck) == held
        assert stuck in home.moved(home.run("full"))


@refines("test_the_fast_lane_leaves_a_conversation_sent_in_earlier_whose_analysis_failed_exactly_as_it_was")
def test_sending_a_conversation_in_from_the_panel_leaves_one_sent_in_earlier_whose_analysis_failed_exactly_as_it_was():
    """Both went in from the panel, and neither is analysed when the later one's run starts: that run takes only the one
    sent in (SPEC.md P0.7), not every conversation sent in and not analysed yet. The control: a full run releases the
    earlier one."""
    home, stuck = held_by("sent in")
    held = where(home, stuck)
    sent = home.converse(FINE)
    assert home.moved(home.send_in(sent)) == {sent}
    assert where(home, stuck) == held
    assert stuck in home.moved(home.run("full"))


@refines("test_the_fast_lane_leaves_a_conversation_sent_in_earlier_whose_analysis_failed_exactly_as_it_was")
def test_filing_a_conversation_from_assist_leaves_one_sent_in_earlier_whose_analysis_failed_exactly_as_it_was():
    """The earlier one went in from the panel; the run of the one filed from Assist takes only it (SPEC.md P0.7), not
    every conversation sent in and not analysed yet. The control: a full run releases the earlier one."""
    home, stuck = held_by("sent in")
    held = where(home, stuck)
    filed = home.converse(FRICTION, filed=True)
    assert home.moved(home.runs()[-1]) == {filed}
    assert where(home, stuck) == held
    assert stuck in home.moved(home.run("full"))
