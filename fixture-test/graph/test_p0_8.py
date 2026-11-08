"""Layer 2 under P0.8 (held): how a conversation comes to be held, what its why says, and what releases it.

Kai, 2026-10-26: "auto-fixed" means fixed by the retries within a run; after them, the conversation is held and needs a
manual release. A conversation that reaches a blocked stage is held there (SPEC.md P0.8), and unblocking the stage isn't
one of the three releases. The number of retries is DETAILS.md's (P4.4.2); here "a few" is more than one.
"""

from household import BROKEN, FINE, FRICTION, SIDE_EFFECT, UNCLEAR, UNMET, Household
from spec_graph import refines

# P0.8's four problems, as a held conversation's why names them, and the stage each holds it at in held_for().
HELD_AT = {"failures": "Analyze", "scope": "Propose", "cut off": "Propose", "blocked": "Propose"}
RELEASES = ("full", "stage", "picked")
WORKING = ("Triage", "Analyze", "Propose", "Write up")  # the stages with work of their own
NEXT = {"Analyze": "Propose", "Propose": "Write up"}


def held_for(cause, home=None):
    """A household (a new one, or the one given) with one more conversation, held for that cause."""
    home = home or Household()
    if cause == "blocked":
        home.block("Propose")
    if cause == "cut off":
        home.stand_in("Propose", "cuts off the next run it works in")
    if cause == "scope":
        home.block("Analyze")
    talk = home.converse(BROKEN if cause == "failures" else FRICTION)
    home.run()
    if cause == "scope":  # a run of Analyze alone works it there; its scope holds it at Propose
        home.unblock("Analyze")
        home.run("stage", stage="Analyze")
    return home, talk


def release(home, talk, kind, stage):
    """Each run that releases a held conversation: a manual full run, its stage's run, a run that picks it."""
    if kind == "full":
        return home.run("full")
    if kind == "stage":
        return home.run("stage", stage=stage)
    return home.run("picked", picked=[talk])


def every_other_run(home, talk, stage, revised, conflicted):
    """Each run P0.7 names but the three that release talk: a scheduled run, the fast lane (for one filed from Assist),
    a run picking another conversation, a run of each other stage, a revision (revised waits at Your review), an
    accept that meets a conflict (so does conflicted), and sending talk in."""
    runs = [home.run("scheduled")]
    home.converse(FRICTION, filed=True)
    runs.append(home.runs()[-1])
    runs.append(home.run("picked", picked=[home.converse(FINE)]))
    runs += [home.run("stage", stage=other) for other in WORKING if other != stage]
    runs.append(home.decide(improvement_for(home, revised), "revise", "only the left door").run)
    improvement = improvement_for(home, conflicted)
    home.edit_home(improvement)
    runs.append(home.agree(home.decide(improvement, "accept")))
    runs.append(home.send_in(talk))
    return runs


def attempts(home, talk, stage="Analyze"):
    return len(home.records(talk, round=1)[stage].attempts)


def state(home, talk):
    """Where it is, its records, and its improvements, each with its status and whether it waits at Your review: what
    any work on it would change."""
    queue = home.review_queue()
    return (home.place(talk), home.records(talk, round=1),
            {item: (item.status, item in queue) for item in home.improvements(talk)})


def improvement_for(home, talk):
    [improvement] = home.improvements(talk)
    return improvement


@refines("P0.8")
def test_a_stage_retries_a_failing_conversation_within_the_run_and_holds_it_there_only_when_the_retries_all_fail():
    """A failure the run's own retries fix holds nothing (Kai's "auto-fixed"); one they don't holds it, after one run.
    The stages with work of their own; Review and Simulate are stamps the proposer collects, not stages (P0.4)."""
    for stage in WORKING:
        for behaviour, held in [("fails the first try, then works", False), ("fails every try", True)]:
            home = Household()
            home.stand_in(stage, behaviour)
            talk = home.converse(FRICTION)
            home.run()
            assert attempts(home, talk, stage) > 1
            place = home.place(talk)
            if held:
                assert place.stage == stage and place.held.cause == "failures"
            else:
                assert place.stage == "Your review" and place.held is None


@refines("P0.8")
def test_a_held_conversation_waits_at_the_stage_its_problem_stopped_it_and_its_why_names_that_problem():
    """The why is what the household is shown: each problem's its own."""
    whys = set()
    for cause, stage in HELD_AT.items():
        home, talk = held_for(cause)
        held = home.place(talk).held
        assert home.place(talk).stage == stage and held.cause == cause and held.why
        assert set(home.shown(talk).values()) == {stage}  # every screen shows it held there, with that why
        whys.add(held.why)
    assert len(whys) == len(HELD_AT)


@refines("P0.8")
def test_whatever_held_it_every_other_run_leaves_a_held_conversation_exactly_as_it_was_until_a_pick_moves_it():
    """Exactly: not moved, the same place and why, the same records (for failures, no try spent). A blocked stage is
    unblocked first, so only the runs keep it held."""
    for cause, stage in HELD_AT.items():
        home = Household()
        revised, conflicted = home.converse(FRICTION), home.converse(UNMET)
        home.run()  # both wait at Your review
        home, talk = held_for(cause, home)
        if cause == "blocked":
            home.unblock(stage)
        before = state(home, talk)
        others = every_other_run(home, talk, stage, revised, conflicted)
        assert [run for run in others if talk in home.moved(run)] == []
        assert state(home, talk) == before

        assert talk in home.moved(home.run("picked", picked=[talk]))  # the same household, a run picking it
        assert state(home, talk) != before


@refines("P0.8")
def test_a_hold_whose_problem_has_passed_lasts_until_a_release_which_carries_the_conversation_past_its_stage():
    """The run's scope, a cut-off run, a block since lifted. A full run or a pick carries it to Your review; a run of its
    stage works it there, and its scope holds it at the next (as P0.8's own test reads a stage run of Analyze). Failures
    that persist hold it again: the next test."""
    for cause in ("scope", "cut off", "blocked"):
        stage = HELD_AT[cause]
        for kind in RELEASES:
            home, talk = held_for(cause)
            if cause == "blocked":
                home.unblock(stage)
            assert home.place(talk).stage == stage and home.place(talk).held.cause == cause
            assert talk in home.moved(release(home, talk, kind, stage))
            place = home.place(talk)
            if kind == "stage":
                assert place.stage == NEXT[stage] and place.held.cause == "scope"
            else:
                assert place.stage == "Your review" and place.held is None


@refines("P0.8")
def test_each_release_gives_one_more_try_at_its_stage_which_holds_it_again_or_carries_it_on():
    """The try is the round's, at the stage it's held at. A stage run works at its stage only, so what it passes on
    is held at the next by its scope (as P0.8's own test reads a stage run of Analyze)."""
    for kind in RELEASES:
        for behaviour, works in [("fails every try", False), ("fails every try in its first run, then works", True)]:
            home = Household()
            home.stand_in("Analyze", behaviour)
            talk = home.converse(FRICTION)
            home.run()
            tries = attempts(home, talk)
            release(home, talk, kind, "Analyze")
            assert attempts(home, talk) == tries + 1
            place = home.place(talk)
            if not works:
                assert place.stage == "Analyze" and place.held.cause == "failures"
            elif kind == "stage":
                assert place.stage == "Propose" and place.held.cause == "scope"
            else:
                assert place.stage == "Your review" and place.held is None


# Layer 3: children of the tests above, each refining its parent by name. The first test (retries, then a hold) needs
# none: it's already one scenario on the driver, run per stage. The last (one more try per release) needs one, for the
# release by a run of a stage that's still blocked.

LINE = ["Your Assist", "Triage", "Analyze", "Propose", "Write up", "Your review"]
PASSED = ("scope", "cut off", "blocked")  # the holds whose problem has passed once held_still() returns


def held_still(cause, home=None):
    """held_for(), with a block lifted at once: from then on only a run can keep it held or move it."""
    home, talk = held_for(cause, home)
    if cause == "blocked":
        home.unblock(HELD_AT[cause])
    return home, talk


def held_there(home, talk, cause):
    """Its record of where it is says: at the stage that cause holds it at, held for that cause."""
    place = home.place(talk)
    return place.stage == HELD_AT[cause] and place.held is not None and place.held.cause == cause


@refines("test_a_held_conversation_waits_at_the_stage_its_problem_stopped_it_and_its_why_names_that_problem")
def test_a_conversation_whose_every_analysis_fails_is_held_at_analyze_while_the_same_run_carries_another_on():
    """Failures: its round stops at Analyze, after more than one try there. The control: a conversation the same run
    took in goes on past Analyze (a failure stays with its conversation, DETAILS P13.2)."""
    home = Household()
    other = home.converse(FRICTION)
    home, talk = held_for("failures", home)
    assert held_there(home, talk, "failures")
    assert home.path(talk, round=1) == LINE[:3] and attempts(home, talk) > 1
    assert home.place(other).stage == "Your review"


@refines("test_a_held_conversation_waits_at_the_stage_its_problem_stopped_it_and_its_why_names_that_problem")
def test_a_conversation_reaching_a_blocked_stage_is_held_there_untried_after_the_stages_before_it_worked_on_it():
    """Blocked: the run carries it up to Propose, which works on nothing (DETAILS P4.3.2), and it's held there rather
    than just waiting (SPEC.md P0.8). The control: Analyze's try in the same run."""
    home, talk = held_for("blocked")
    assert held_there(home, talk, "blocked") and home.path(talk, round=1) == LINE[:4]
    assert attempts(home, talk, "Analyze") == 1 and attempts(home, talk, "Propose") == 0


@refines("test_a_held_conversation_waits_at_the_stage_its_problem_stopped_it_and_its_why_names_that_problem")
def test_what_a_run_of_one_stage_passes_on_is_held_untried_at_the_next_stage_by_the_runs_scope():
    """Scope: a run of Analyze alone works it at Analyze and no further (Kai's ruling: a scope hold comes from a stage
    run). The control: that run's try at Analyze, the only one, since Analyze was blocked before."""
    home, talk = held_for("scope")
    assert held_there(home, talk, "scope") and home.path(talk, round=1) == LINE[:4]
    assert attempts(home, talk, "Analyze") == 1 and attempts(home, talk, "Propose") == 0


@refines("test_a_held_conversation_waits_at_the_stage_its_problem_stopped_it_and_its_why_names_that_problem")
def test_a_run_cut_off_while_a_stage_works_on_a_conversation_holds_it_there_after_that_one_try():
    """Cut off: the try it was in is kept, and the stage doesn't try it again on its own (DETAILS P5.10, P13.6.1)."""
    home, talk = held_for("cut off")
    assert held_there(home, talk, "cut off") and home.path(talk, round=1) == LINE[:4]
    assert attempts(home, talk, "Propose") == 1


@refines("test_a_held_conversation_waits_at_the_stage_its_problem_stopped_it_and_its_why_names_that_problem")
def test_a_conversation_sent_back_while_its_runs_model_is_unavailable_is_held_where_it_went_the_refusal_its_why():
    """A revision and a conflict go back in at Propose, an answer to the stage that asked, each on a run of its own
    (P0.7). While a step that run needs has no AI model available, the run is refused before anything is spent
    (DETAILS P13.5.2), and kept in the history as a scheduled run's refusal is. The conversation isn't left there for
    no run to move: it's held there for failures, the refusal its why. With the model back, a manual full run releases
    it and carries its round on to where it stops (answered, the analyzer finds nothing to fix)."""
    for send_back, stage, stops in [("revision", "Propose", "Your review"), ("conflict", "Propose", "Your review"),
                                    ("answer", "Analyze", "Your household")]:
        home = Household()
        talk = home.converse(UNCLEAR if send_back == "answer" else FRICTION)
        home.run()  # it waits at Your review: an improvement, or the analyzer's question
        home.model_down("Propose")  # Propose's model, which Analyze shares
        if send_back == "revision":
            refused = home.decide(improvement_for(home, talk), "revise", "only the left door").run
        elif send_back == "conflict":
            improvement = improvement_for(home, talk)
            home.edit_home(improvement)
            refused = home.agree(home.decide(improvement, "accept"))
        else:
            refused = home.answer(talk, "the left one")
        assert (refused.trigger, refused.status) == (send_back, "failed") and "Propose" in refused.error
        place = home.place(talk)
        assert (place.stage, place.held.cause, place.held.why) == (stage, "failures", refused.error)
        assert set(home.shown(talk).values()) == {stage}  # every screen shows it held there, with that why
        home.model_up("Propose")
        assert talk in home.moved(home.run("full"))
        assert (home.place(talk).stage, home.place(talk).held, home.place(talk).round) == (stops, None, place.round)


@refines("test_whatever_held_it_every_other_run_leaves_a_held_conversation_exactly_as_it_was_until_a_pick_moves_it")
def test_a_scheduled_run_or_the_fast_lane_moves_only_the_new_conversation_it_takes_in_and_leaves_a_held_one_as_it_was():
    """The control: each run moved the new conversation it took in."""
    for cause in HELD_AT:
        home, talk = held_still(cause)
        before = state(home, talk)
        new = home.converse(FRICTION)
        assert home.moved(home.run("scheduled")) == {new}
        runs = len(home.runs())
        filed = home.converse(FRICTION, filed=True)
        [fast] = home.runs()[runs:]
        assert home.moved(fast) == {filed}
        assert state(home, talk) == before


@refines("test_whatever_held_it_every_other_run_leaves_a_held_conversation_exactly_as_it_was_until_a_pick_moves_it")
def test_a_run_picking_another_conversation_leaves_a_held_one_as_it_was_and_a_run_picking_it_moves_it():
    for cause in HELD_AT:
        home, talk = held_still(cause)
        before = state(home, talk)
        other = home.converse(FINE)
        assert home.moved(home.run("picked", picked=[other])) == {other}
        assert state(home, talk) == before
        assert home.moved(home.run("picked", picked=[talk])) == {talk} and state(home, talk) != before


@refines("test_whatever_held_it_every_other_run_leaves_a_held_conversation_exactly_as_it_was_until_a_pick_moves_it")
def test_a_run_of_any_other_stage_leaves_a_held_conversation_as_it_was_and_a_run_of_its_own_stage_moves_it():
    for cause, stage in HELD_AT.items():
        home, talk = held_still(cause)
        before = state(home, talk)
        for other in WORKING:
            if other != stage:
                assert talk not in home.moved(home.run("stage", stage=other))
        assert state(home, talk) == before
        assert home.moved(home.run("stage", stage=stage)) == {talk}


@refines("test_whatever_held_it_every_other_run_leaves_a_held_conversation_exactly_as_it_was_until_a_pick_moves_it")
def test_a_conversation_sent_back_to_propose_revised_or_after_a_conflict_moves_alone_and_leaves_a_held_one_as_it_was():
    """Both go back in at Propose, where three of the four causes hold their conversation. The two waiting are kinds
    whose fixes can't cover the held one's trouble, so it's held, not ended as a duplicate (Kai's ruling); it's checked
    held first. The control: each run moved the conversation sent back."""
    for cause in HELD_AT:
        home = Household()
        revised, conflicted = home.converse(SIDE_EFFECT), home.converse(UNMET)
        home.run()  # both wait at Your review
        home, talk = held_still(cause, home)
        assert held_there(home, talk, cause)
        before = state(home, talk)
        assert home.moved(home.decide(improvement_for(home, revised), "revise", "only the left door").run) == {revised}
        improvement = improvement_for(home, conflicted)
        home.edit_home(improvement)
        assert home.moved(home.agree(home.decide(improvement, "accept"))) == {conflicted}
        assert state(home, talk) == before


@refines("test_whatever_held_it_every_other_run_leaves_a_held_conversation_exactly_as_it_was_until_a_pick_moves_it")
def test_sending_a_held_conversation_in_neither_releases_it_nor_starts_a_new_round():
    """Sending in again is for what's in Your household (P0.12); a held conversation is still in its round. The
    control: a conversation sent in from Your household moves, in round 2."""
    for cause in HELD_AT:
        home, talk = held_still(cause)
        before = state(home, talk)
        assert talk not in home.moved(home.send_in(talk))
        assert state(home, talk) == before
        done = home.converse(FINE)
        home.run("picked", picked=[done])
        assert home.place(done).stage == "Your household"
        assert home.moved(home.send_in(done)) == {done} and home.place(done).round == 2


@refines("test_a_hold_whose_problem_has_passed_lasts_until_a_release_which_carries_the_conversation_past_its_stage")
def test_lifting_a_block_releases_nothing_and_what_it_held_stays_held_for_the_block_with_no_try_spent():
    """Kai's ruling: unblocking releases nothing by itself. The control: a run of the stage then tries it."""
    home, talk = held_for("blocked")
    records = home.records(talk, round=1)
    home.unblock("Propose")
    place = home.place(talk)
    assert place.stage == "Propose" and place.held.cause == "blocked" and home.records(talk, round=1) == records
    home.run("stage", stage="Propose")
    assert home.records(talk, round=1) != records


@refines("test_a_hold_whose_problem_has_passed_lasts_until_a_release_which_carries_the_conversation_past_its_stage")
def test_a_manual_full_run_carries_a_conversation_whose_hold_has_passed_on_to_your_review_in_the_same_round():
    """It's held until then (a block's hold outlasts the block), and goes on from the stage it was held at: a release
    isn't a new round (P0.6 lists what starts one)."""
    for cause in PASSED:
        home, talk = held_still(cause)
        assert held_there(home, talk, cause)
        assert talk in home.moved(home.run("full"))
        place = home.place(talk)
        assert place.stage == "Your review" and place.held is None and place.round == 1
        assert home.path(talk, round=1) == LINE


@refines("test_a_hold_whose_problem_has_passed_lasts_until_a_release_which_carries_the_conversation_past_its_stage")
def test_a_run_picking_a_conversation_whose_hold_has_passed_carries_it_on_to_your_review_in_the_same_round():
    """It's held until then (a block's hold outlasts the block)."""
    for cause in PASSED:
        home, talk = held_still(cause)
        assert held_there(home, talk, cause)
        assert home.moved(home.run("picked", picked=[talk])) == {talk}
        place = home.place(talk)
        assert place.stage == "Your review" and place.held is None and place.round == 1
        assert home.path(talk, round=1) == LINE


@refines("test_a_hold_whose_problem_has_passed_lasts_until_a_release_which_carries_the_conversation_past_its_stage")
def test_a_run_of_its_stage_tries_a_released_conversation_once_there_and_holds_it_untried_at_the_next_by_scope():
    """A stage run works at its stage only, so Write up doesn't try it (Kai's ruling: what a stage run passes on is
    held at the next stage). It's held until then (a block's hold outlasts the block). The control: its one more try at
    Propose."""
    for cause in PASSED:
        home, talk = held_still(cause)
        assert held_there(home, talk, cause)
        tried = attempts(home, talk, "Propose")
        assert home.moved(home.run("stage", stage="Propose")) == {talk}
        place = home.place(talk)
        assert place.stage == "Write up" and place.held.cause == "scope" and place.round == 1
        assert attempts(home, talk, "Propose") == tried + 1 and attempts(home, talk, "Write up") == 0


@refines("test_each_release_gives_one_more_try_at_its_stage_which_holds_it_again_or_carries_it_on")
def test_a_run_of_a_blocked_stage_still_gives_what_is_held_there_its_one_more_try_and_holds_it_at_the_next_stage():
    """Running a blocked stage on its own still runs it (DETAILS P3.6.3, P5.6.3): a run of Propose releases what its
    block holds there, tries it there once, and its scope holds what it passes on at Write up; Propose stays blocked.
    The control: with Write up blocked as well, what it passes on is held there for that block, before its scope."""
    for also in (None, "Write up"):
        home, talk = held_for("blocked")
        if also:
            home.block(also)
        assert held_there(home, talk, "blocked")
        tried = attempts(home, talk, "Propose")
        assert home.moved(home.run("stage", stage="Propose")) == {talk}
        place = home.place(talk)
        assert place.stage == "Write up" and place.held.cause == ("blocked" if also else "scope") and place.round == 1
        assert attempts(home, talk, "Propose") == tried + 1 and attempts(home, talk, "Write up") == 0
        new = home.converse(FRICTION)  # Propose is still blocked: a scheduled run holds a new one there for it.
        home.run("scheduled")
        assert held_there(home, new, "blocked")


# Layer 4: children of the two layer-3 tests that each put two separate runs in one household (a scheduled run and the
# fast lane; a revision and a resolved conflict): one child per run, so a failure names the run. Each control shows that
# run worked, on another conversation, at the stage holding this one. The cut-off test gets two children: two tests cut
# off at once, as Home Assistant runs one message's tool calls; and what else the cut-off run had taken in or released.
# The other twelve need none: each is already one scenario on the driver.


@refines("test_a_scheduled_run_or_the_fast_lane_moves_only_the_new_conversation_it_takes_in_and_leaves_a_held_one_as_it_was")
def test_a_scheduled_run_carries_a_new_conversation_past_the_stage_where_one_is_held_and_leaves_the_held_one_as_it_was():
    """It admits new ones only (DETAILS P5.3: what's held stays held). The control: in the same run, the new one goes
    through the stage holding the other, on to Your review."""
    for cause in HELD_AT:
        home, talk = held_still(cause)
        assert held_there(home, talk, cause)
        before = state(home, talk)
        new = home.converse(FRICTION)
        assert home.moved(home.run("scheduled")) == {new} and home.place(new).stage == "Your review"
        assert state(home, talk) == before


@refines("test_a_scheduled_run_or_the_fast_lane_moves_only_the_new_conversation_it_takes_in_and_leaves_a_held_one_as_it_was")
def test_the_fast_lane_carries_the_conversation_filed_past_the_stage_where_one_is_held_and_leaves_the_held_one_as_it_was():
    """Its run works only on the one filed (DETAILS P5.5.4): not the held one, nor one waiting at Your review, whose
    trouble the filed one's fix can't cover (each FRICTION conversation has a cause of its own). The control: in that
    run, the one filed goes through the stage holding the other, on to Your review."""
    for cause in HELD_AT:
        home = Household()
        waiting = home.converse(FRICTION)
        home.run()  # it waits at Your review
        home, talk = held_still(cause, home)
        assert held_there(home, talk, cause)
        before = state(home, talk)
        runs = len(home.runs())
        filed = home.converse(FRICTION, filed=True)
        [fast] = home.runs()[runs:]
        assert home.moved(fast) == {filed} and home.place(filed).stage == "Your review"
        assert state(home, talk) == before


@refines("test_a_conversation_sent_back_to_propose_revised_or_after_a_conflict_moves_alone_and_leaves_a_held_one_as_it_was")
def test_a_revisions_run_works_at_propose_on_the_revised_conversation_alone_and_leaves_a_held_one_as_it_was():
    """It goes in at Propose on a run of its own, and only it moves (DETAILS P5.12), though three of the four causes
    hold theirs at Propose and another improvement waits at Your review. As in the parent, neither waiting kind's fix
    can cover the held one's trouble, so it stays held, not a duplicate. The control: Propose tried the revised
    conversation in its new round."""
    for cause in HELD_AT:
        home = Household()
        revised, waiting = home.converse(SIDE_EFFECT), home.converse(UNMET)
        home.run()  # both improvements wait at Your review
        home, talk = held_still(cause, home)
        assert held_there(home, talk, cause)
        before = state(home, talk)
        run = home.decide(improvement_for(home, revised), "revise", "only the left door").run
        assert home.moved(run) == {revised} and home.records(revised, round=2)["Propose"].attempts
        assert state(home, talk) == before


@refines("test_a_conversation_sent_back_to_propose_revised_or_after_a_conflict_moves_alone_and_leaves_a_held_one_as_it_was")
def test_the_run_resolving_a_conflict_works_at_propose_on_that_conversation_alone_and_leaves_a_held_one_as_it_was():
    """Once the household agrees, it goes in at Propose on a run of its own, and only it moves (SPEC.md P0.7, DETAILS
    P5.12), though three of the four causes hold theirs at Propose and another improvement waits at Your review. As in
    the parent, the held one is no duplicate. The control: Propose tried the conflicted conversation in its new round."""
    for cause in HELD_AT:
        home = Household()
        waiting, conflicted = home.converse(SIDE_EFFECT), home.converse(UNMET)
        home.run()  # both improvements wait at Your review
        home, talk = held_still(cause, home)
        assert held_there(home, talk, cause)
        before = state(home, talk)
        improvement = improvement_for(home, conflicted)
        home.edit_home(improvement)  # Home Assistant changed since the improvement was tested
        run = home.agree(home.decide(improvement, "accept"))
        assert home.moved(run) == {conflicted} and home.records(conflicted, round=2)["Propose"].attempts
        assert state(home, talk) == before


@refines("test_a_run_cut_off_while_a_stage_works_on_a_conversation_holds_it_there_after_that_one_try")
def test_a_run_cut_off_while_two_tests_run_leaves_the_held_rounds_record_as_it_was_at_the_cut():
    """With P0.2: the proposer tests two ideas in one message, and the run is cut off while both tests run, at the
    first replay either of them asks for, so at the cut the round has no test and no trip back. Once it's held cut off,
    its round's record stays as it was at the cut while the world settles (no test or trip is added), and the
    household's assistant answers no replay after the cut."""
    home = Household()
    home.stand_in("Propose", "tests two ideas in one message")
    home.stand_in("Simulate", "is cut off while two tests run")
    talk = home.converse(FRICTION)
    home.run()
    assert held_there(home, talk, "cut off") and attempts(home, talk, "Propose") == 1
    assert home.tests(talk) == [] and home.trips_back(talk) == []
    assert [replay for replay in home.received("Simulate", talk) if replay.after_cut] == []


@refines("test_a_run_cut_off_while_a_stage_works_on_a_conversation_holds_it_there_after_that_one_try")
def test_a_cut_off_run_holds_what_else_it_took_in_or_released_where_each_stands_until_a_full_run_releases_them():
    """Cut off while Propose works on one conversation, the run hadn't reached a second new one it took in, nor one it
    released (held at Analyze for failures before): each is held cut off where it stands, in its round. A scheduled run
    leaves all three exactly as they were; a manual full run releases them and carries each on to Your review."""
    home = Household()
    home.stand_in("Analyze", "fails every try in the next run, then answers as usual")
    stuck = home.converse(FRICTION)
    home.run()
    assert (home.place(stuck).stage, home.place(stuck).held.cause) == ("Analyze", "failures")
    home.stand_in("Propose", "cuts off the next run it works in")
    first, second = home.converse(FRICTION), home.converse(FRICTION)
    assert home.moved(home.run("full")) == {first, second, stuck}
    stood = {first: "Your Assist", second: "Your Assist", stuck: "Analyze"}
    [worked] = [talk for talk in stood if home.place(talk).stage == "Propose"]  # the one Propose worked on
    for talk, stage in stood.items():
        place = home.place(talk)
        assert (place.stage, place.round, place.held and place.held.cause) == (
            "Propose" if talk == worked else stage, 1, "cut off")
    before = {talk: state(home, talk) for talk in stood}
    new = home.converse(FINE)
    assert home.moved(home.run("scheduled")) == {new}
    assert {talk: state(home, talk) for talk in stood} == before
    assert home.moved(home.run("full")) == set(stood)
    assert {(home.place(talk).stage, home.place(talk).round, home.place(talk).held) for talk in stood} == {
        ("Your review", 1, None)}
