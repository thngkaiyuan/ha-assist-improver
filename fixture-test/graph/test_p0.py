"""Layer 1 of the test graph: one test for each rule of SPEC.md's model (P0).

Each test checks its rule's claim end to end, the way the household would see it, and leaves the detail to the layers
below. It reaches the product only through household.py, and fails as Missing until the product offers what it needs.
A test may lean on a sibling rule for its setup (P0.3's retry is P0.8's); that's named where it happens.
"""

from household import (BOTH_DOORS, BROKEN, DECISIONS, FINE, FRICTION, GOAL_STATES, NO_FIX, NO_GAIN, PARTLY, SAME_CAUSE,
                       SIDE_EFFECT, SMOOTH, STAMPS, THANKS_AND_NOTE, UNCLEAR, UNMET, UNREACHABLE_AND_REPAIR,
                       UNREADABLE, Household)
from spec_graph import spec


def improvement_for(home, talk):
    """The conversation's newest improvement: a revision adds a newer version beside the earlier one."""
    return home.improvements(talk)[-1]


@spec("P0.1")
def test_a_conversation_goes_in_and_an_improvement_comes_out_that_makes_its_goals_easier_to_fulfil():
    home = Household()
    talk = home.converse(FRICTION)
    home.run()
    improvement = improvement_for(home, talk)
    assert improvement in home.review_queue()
    assert improvement.test.after.easier_than(improvement.test.before)


@spec("P0.1.1")
def test_fulfilment_is_goals_met_and_effort_is_turns_per_root_goal_and_side_effects_left():
    for kind, measure in [(UNMET, "goals met"), (UNREACHABLE_AND_REPAIR, "turns per root goal"),
                          (SIDE_EFFECT, "side effects left")]:
        home = Household()
        talk = home.converse(kind)
        home.run()
        assert improvement_for(home, talk).test.gains == {measure}

    home = Household()
    talk = home.converse(NO_GAIN)
    home.run()
    assert home.improvements(talk) == []  # a fix that moves none of the three is no improvement


@spec("P0.2")
def test_one_record_says_where_a_conversation_is_and_each_round_keeps_a_record_per_stage_that_screens_and_stages_read():
    home = Household()
    talk = home.converse(NO_FIX)
    home.run()
    assert set(home.shown(talk).values()) == {home.place(talk).stage}
    first_round = home.records(talk, round=1)
    assert set(first_round) == set(home.path(talk, round=1))
    assert first_round["Analyze"] in home.seen_by("Propose", talk)

    home.send_in(talk)
    assert home.place(talk).round == 2
    assert home.records(talk, round=1) == first_round
    assert set(home.records(talk, round=2)) == set(home.path(talk, round=2))


@spec("P0.3")
def test_every_turn_maps_to_goals_in_a_tree_whose_roots_are_each_one_world_state_at_one_time():
    home = Household()
    home.stand_in("Analyze", "first leaves the thanks without a goal")  # refused, then retried (P0.8)
    talk = home.converse(THANKS_AND_NOTE)
    home.run()
    tree = home.analysis(talk)
    assert all(turn.goals for turn in tree.turns)
    assert all(goal.parent in tree.goals for goal in tree.goals if goal not in tree.roots)
    assert {goal.state for goal in tree.goals} <= set(GOAL_STATES)
    [root] = tree.roots
    request, correction, thanks, note = tree.turns
    for turn in (thanks, note):
        assert all(goal.root == root and goal != root for goal in turn.goals)

    home.decide(improvement_for(home, talk), "revise", "open both at once")
    assert home.analysis(talk).turns[-1].goals  # the revision note is a turn with a goal too (this one asks again)

    home = Household()  # what's asked at once is one world state, so one root: one per door is refused (P0.8)
    home.stand_in("Analyze", "first makes one goal per door")
    talk = home.converse(BOTH_DOORS)
    home.run()
    assert len(home.analysis(talk).roots) == 1


@spec("P0.4")
def test_a_conversation_goes_through_the_stages_in_order_and_an_idea_goes_on_only_with_both_stamps():
    home = Household()
    talk = home.converse(FRICTION)
    home.run()
    home.decide(improvement_for(home, talk), "accept")
    path = home.path(talk)
    assert path == ["Your Assist", "Triage", "Analyze", "Propose", "Write up", "Your review", "Your household"]
    handoffs = home.handoffs(talk)
    assert [(handoff.start, handoff.end) for handoff in handoffs] == list(zip(path, path[1:]))
    for handoff in handoffs:
        assert handoff.contract.accepts(handoff.record) and handoff.contract.fields
        assert not any(handoff.contract.accepts(handoff.without(field)) for field in handoff.contract.fields)

    for refusing in ("Review", "Simulate", None):
        home = Household()
        if refusing:
            home.stand_in(refusing, "refuses every idea")
        talk = home.converse(FRICTION)
        home.run()
        reached = [idea for idea in home.ideas(talk) if idea.reached_write_up]
        assert all(set(idea.stamps) == set(STAMPS) for idea in reached)
        assert bool(reached) == (refusing is None)


@spec("P0.5")
def test_a_conversation_is_at_one_stage_at_a_time_with_all_its_goals():
    home = Household()
    talk = home.converse(PARTLY)  # two root goals: one has trouble a fix removes, the other went fine
    home.run()
    assert home.place(talk).stage == "Your review"
    goals = set(home.analysis(talk).goals)
    trace = home.trace(talk)
    assert trace and all(len(moment.stages) == 1 for moment in trace)
    past_analysis = [moment for moment in trace if moment.stages[0] not in ("Your Assist", "Triage", "Analyze")]
    assert past_analysis and all(set(moment.goals) == goals for moment in past_analysis)


@spec("P0.6")
def test_a_round_flows_until_it_waits_for_the_household_ends_or_is_held_and_each_new_round_starts_fresh():
    for kind, stage in [(FRICTION, "Your review"), (UNCLEAR, "Your review"), (BROKEN, "Analyze")]:
        home = Household()
        talk = home.converse(kind)
        home.run()
        assert home.place(talk).stage == stage
        assert bool(home.place(talk).held) == (kind == BROKEN)

    for kind, ended_at in [(SMOOTH, "Triage"), (FINE, "Analyze"), (NO_FIX, "Propose")]:
        home = Household()
        talk = home.converse(kind)
        home.run()
        assert home.place(talk).stage == "Your household" and home.path(talk)[-2] == ended_at
    home = Household()
    talk = home.converse(FINE)
    home.mark_test(talk)
    assert home.place(talk).stage == "Your household" and home.path(talk)[-2] == "Your Assist"

    for kind, asker in [(UNCLEAR, "Analyze"), (FRICTION, "Propose")]:
        home = Household()
        if kind == FRICTION:
            home.stand_in("Propose", "asks the household one question before it sends its idea on")
        talk = home.converse(kind)
        home.run()
        assert home.path(talk)[-2:] == [asker, "Your review"] and not home.place(talk).held
        paused = len(home.path(talk))
        home.answer(talk, "the left door")
        assert home.place(talk).round == 1 and home.path(talk)[paused] == asker

    home = Household()
    talk = home.converse(NO_FIX)
    home.run()
    assert home.tests_left(talk) == 0
    first_round = home.records(talk, round=1)
    home.send_in(talk)
    assert home.place(talk).round == 2 and home.tests(talk, round=2)
    assert home.records(talk, round=1) == first_round

    home = Household()
    talk = home.converse(FRICTION)
    home.run()
    earlier = improvement_for(home, talk)
    home.block("Propose")  # keeps the revision's round from finishing while we look
    home.decide(earlier, "revise", "only the left door")
    assert home.place(talk).round == 2 and home.place(talk).stage == "Propose"
    assert earlier in home.review_queue()

    home = Household()
    talk = home.converse(FRICTION)
    home.run()
    improvement = improvement_for(home, talk)
    home.edit_home(improvement)
    home.agree(home.decide(improvement, "accept"))
    assert home.place(talk).round == 2


def assorted():
    """A household with one conversation held at Analyze, then two new ones waiting for a run."""
    home = Household()
    stuck = home.converse(BROKEN)
    home.run()
    new = [home.converse(FRICTION), home.converse(FINE)]
    assert all(home.place(talk).stage == "Your Assist" for talk in new)
    return home, stuck, new


@spec("P0.7")
def test_a_run_moves_only_what_it_admits():
    home, stuck, new = assorted()
    assert home.moved(home.run("full")) == {stuck, *new}
    home, stuck, new = assorted()
    assert home.moved(home.run("scheduled")) == set(new)
    home, stuck, new = assorted()
    assert home.moved(home.run("picked", picked=[new[0]])) == {new[0]}
    assert home.place(new[1]).stage == "Your Assist"
    home, stuck, new = assorted()
    assert home.moved(home.run("stage", stage="Analyze")) == {stuck}
    home, stuck, new = assorted()
    filed = home.converse(FRICTION, filed=True)  # filed from Assist while it was going on
    assert home.moved(home.runs()[-1]) == {filed}

    home = Household()
    talk = home.converse(UNCLEAR)
    home.run()
    paused, newcomer = len(home.path(talk)), home.converse(FRICTION)
    assert home.moved(home.answer(talk, "the left door")) == {talk} and home.path(talk)[paused] == "Analyze"
    assert home.place(newcomer).stage == "Your Assist"

    home = Household()
    talk = home.converse(FRICTION)
    home.run()
    revised = home.decide(improvement_for(home, talk), "revise", "only the left door")
    assert home.moved(revised.run) == {talk} and home.path(talk, round=2)[0] == "Propose"

    home = Household()
    talk = home.converse(FRICTION)
    home.run()
    improvement = improvement_for(home, talk)
    home.edit_home(improvement)
    conflicted = home.decide(improvement, "accept")
    assert conflicted.asks.about == "conflict" and home.place(talk).stage == "Your review"
    assert home.moved(home.agree(conflicted)) == {talk} and home.path(talk, round=2)[0] == "Propose"


@spec("P0.8")
def test_a_held_conversation_says_why_and_only_a_manual_full_run_its_stages_run_or_a_pick_releases_it():
    home = Household()
    stuck = home.converse(BROKEN)
    home.run()
    assert home.place(stuck).stage == "Analyze" and home.place(stuck).held.cause == "failures"
    assert home.place(stuck).held.why
    tries = len(home.records(stuck, round=1)["Analyze"].attempts)
    assert tries > 1
    assert stuck not in home.moved(home.run("scheduled"))
    assert stuck not in home.moved(home.run("stage", stage="Triage"))
    assert stuck not in home.moved(home.send_in(stuck))
    for release in [dict(kind="full"), dict(kind="stage", stage="Analyze"), dict(kind="picked", picked=[stuck])]:
        assert stuck in home.moved(home.run(**release))
        tries += 1
        assert len(home.records(stuck, round=1)["Analyze"].attempts) == tries and home.place(stuck).held

    home = Household()
    home.block("Propose")
    talk = home.converse(FRICTION)
    home.run()
    assert home.place(talk).stage == "Propose" and home.place(talk).held.cause == "blocked"
    assert home.place(talk).held.why

    home = Household()
    home.block("Analyze")
    talk = home.converse(FRICTION)
    home.run()
    home.unblock("Analyze")
    home.run("stage", stage="Analyze")
    assert home.place(talk).stage == "Propose" and home.place(talk).held.cause == "scope"
    assert home.place(talk).held.why


@spec("P0.9")
def test_the_proposer_decides_how_to_use_its_rounds_tests_and_sends_on_only_what_they_validated():
    home = Household()
    home.stand_in("Propose", "after a validated idea, tries a better one while tests are left")
    talk = home.converse(FRICTION)
    home.run()
    tests = home.tests(talk)
    assert len(tests) > 1 and home.tests_left(talk) == 0
    assert tests[-1].validated and improvement_for(home, talk).idea == tests[-1].idea

    home = Household()
    home.stand_in("Propose", "sends on its first validated idea")
    talk = home.converse(FRICTION)
    home.run()
    tests = home.tests(talk)
    assert len(tests) == 1 and home.tests_left(talk) > 0
    assert tests[0].validated and improvement_for(home, talk).idea == tests[0].idea


@spec("P0.10")
def test_an_improvement_belongs_to_the_conversation_it_came_from_while_its_changes_and_its_view_are_broad():
    home = Household()
    home.stand_in("Propose", "proposes a change for the whole household")
    owner, other = home.converse(FRICTION), home.converse(FINE)
    home.run()
    improvement = improvement_for(home, owner)
    assert improvement.conversation == owner and improvement.tested_on == {owner}
    assert improvement.changes_beyond(owner)
    assert other in home.reached("Propose", owner) and other in home.reached("Review", owner)


@spec("P0.11")
def test_the_household_accepts_revises_or_declines_and_a_decline_only_declines_and_archives():
    home = Household()
    talk, bystander = home.converse(FRICTION), home.converse(UNMET)
    home.run()
    improvement = improvement_for(home, talk)
    assert set(improvement.decisions) == set(DECISIONS)
    written, waiting = home.writable(), improvement_for(home, bystander)
    home.decide(improvement, "decline")
    assert improvement_for(home, talk).status == "declined"
    assert home.place(talk).outcome == "declined" and home.place(talk).archived
    assert home.writable() == written
    assert improvement_for(home, bystander) == waiting and waiting in home.review_queue()

    home = Household()
    talk = home.converse(FRICTION)
    home.run()
    written = home.writable()
    home.decide(improvement_for(home, talk), "accept")
    assert home.writable() != written

    home = Household()
    talk = home.converse(UNCLEAR)
    home.run()
    assert [item.question for item in home.review_queue() if item.conversation == talk]


@spec("P0.12")
def test_everything_decided_is_out_of_the_pipeline_until_it_is_sent_in_again_for_a_new_round():
    home = Household()
    improved, declined = home.converse(UNMET), home.converse(SIDE_EFFECT)
    covering, duplicate = home.converse(FRICTION), home.converse(SAME_CAUSE)
    no_fix, no_trouble, smooth, unreadable = (home.converse(kind) for kind in (NO_FIX, FINE, SMOOTH, UNREADABLE))
    test, archived = home.converse(FINE), home.converse(FINE)
    home.mark_test(test)
    home.archive(archived)
    home.run()
    assert archived in improvement_for(home, improved).checked_on  # kept for checking other changes against
    assert home.place(duplicate).covered_by == improvement_for(home, covering)
    home.decide(improvement_for(home, improved), "accept")
    home.decide(improvement_for(home, declined), "decline")

    outcomes = {improved: "improved", declined: "declined", duplicate: "duplicate", no_fix: "no fix found",
                no_trouble: "no trouble found", smooth: "looked smooth", unreadable: "couldn't be read",
                test: "marked as a test", archived: "archived"}
    for talk, outcome in outcomes.items():
        assert home.place(talk).stage == "Your household" and home.place(talk).outcome == outcome
    assert not home.moved(home.run("full")) & set(outcomes)
    for talk in outcomes:
        assert home.moved(home.send_in(talk)) == {talk} and home.place(talk).round == 2


@spec("P0.13")
def test_every_way_back_costs_a_capped_budget_or_needs_a_person():
    ways = [("Review", "asks for changes to every idea", "keeps asking the reviewer while it can"),
            ("Simulate", "validates nothing", "keeps testing while it can")]
    for start, behaviour, proposer in ways:
        home = Household()
        home.stand_in(start, behaviour)
        home.stand_in("Propose", proposer)  # it would go on: only the cap stops it
        talk = home.converse(FRICTION)
        home.run()
        trips = home.trips_back(talk)
        assert start in {trip.start for trip in trips}
        for way in {trip.start for trip in trips}:
            [budget] = {trip.paid_by for trip in trips if trip.start == way}  # one budget per way back, per round
            assert budget != "person" and len([trip for trip in trips if trip.start == way]) <= budget.cap

    home = Household()
    talk = home.converse(FRICTION)
    home.run()
    home.run("full")
    assert [trip for trip in home.trips_back(talk) if trip.start == "Your review"] == []
    home.decide(improvement_for(home, talk), "revise", "only the left door")
    improvement = improvement_for(home, talk)
    home.edit_home(improvement)
    home.agree(home.decide(improvement, "accept"))
    home.send_in(talk)
    assert {trip.paid_by for trip in home.trips_back(talk) if trip.start in ("Your review", "Your household")} \
        == {"person"}
