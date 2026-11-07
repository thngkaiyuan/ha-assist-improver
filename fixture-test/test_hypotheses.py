"""Hypothesis generator and reviewer: friction, validation, read tools, and both roles through HA's AI Task."""

from types import SimpleNamespace

import pytest

from test_ai_roles import ANALYSIS, task_entity
from recordings import SCRIPT, TARGET, at_home, make_recording
from test_milestone1 import fake_entry, recording_hass  # noqa: F401
from custom_components.assist_improver import hypotheses
from custom_components.assist_improver.analyzer import to_analysis
from custom_components.assist_improver.cases import digest
from custom_components.assist_improver.const import CONF_AI_TASK
from custom_components.assist_improver.exploration import SessionView

TASK = "ai_task.fixture_task"


def sessions():
    record = make_recording()
    view = SessionView(record)
    analysis = to_analysis(record["id"], ANALYSIS, view)
    return hypotheses.Sessions([analysis], [view]), analysis


def hypothesis(*changes, goals=(1,), session=None):
    s, analysis = sessions()
    return {"title": "Press the relay", "cause": "Assist can't see the press script",
            "goals": [{"session": session or hypotheses.short(analysis["recording_id"]), "goal_turn": t} for t in goals],
            "changes": list(changes), "expected": "One turn",
            "support": [{"session": session or hypotheses.short(analysis["recording_id"]), "turn": 3,
                         "note": "the user had to finish the press"}], "conflicts": []}


def test_friction_scenarios_and_overview():
    s, analysis = sessions()
    assert [f["turn"] for f in hypotheses.friction(analysis)] == [2, 3]
    overview = s.overview()
    (script,) = overview["friction_scenarios"]
    session = hypotheses.short(analysis["recording_id"])
    assert script["session"] == session and script["goals_with_friction"] == [1]
    turns = {t["turn"]: t for t in script["turns"]}
    assert turns[1]["goals_started"][0]["kind"] == "sequence" and turns[1]["user"]
    goal = next(g for g in analysis["goals"] if g["turn"] == 1)  # Every field the goal has: its turn is the line's.
    assert turns[1]["goals_started"][0] == {k: v for k, v in goal.items() if k != "turn"}
    assert turns[2]["extra_turn_for_goal_at_turn"] == 1 and turns[3]["extra_turn_for_goal_at_turn"] == 1
    assert hypotheses.removes(s, hypothesis()) == {(session, 2), (session, 3)}
    assert s.listing()["sessions"][0]["state"] == "open_friction"
    assert hypotheses.ReadSession(s).run(session=session)["turns"] == script["turns"]
    assert s.summary()["sessions"] == {"open_friction": 1}


def test_hypotheses_are_checked_against_sessions_and_household():
    s, _ = sessions()
    expose = {"kind": "registry", "entity_id": SCRIPT, "expose": True}
    assert hypotheses.problems(s, {"hypotheses": [hypothesis(expose)]}) == []
    found = hypotheses.problems(s, {"hypotheses": [hypothesis(
        {"kind": "registry", "entity_id": TARGET, "expose": True}, {"kind": "preference"},
        {"kind": "registry", "entity_id": "switch.nope", "aliases": ["x"]},
        {"kind": "preference", "text": "x", "replaces": ["a line nobody wrote"]}, goals=(4,))]})
    assert any("no goal with friction at turn 4" in p for p in found)
    assert any("already exposed" in p for p in found)
    assert any("needs text" in p for p in found)
    assert any("there's no entity 'switch.nope'" in p for p in found)
    assert any("isn't in Assist's current instructions" in p for p in found)
    assert "no session 'nope'" in hypotheses.problems(s, {"hypotheses": [hypothesis(expose, session="nope")]})[0]


def test_read_tools():
    s, analysis = sessions()
    reader = hypotheses.ReadSessionTurn(s)
    assert reader.run(session=hypotheses.short(analysis["recording_id"]), turn=2)["turn"] == 2
    assert "error" in reader.run(session="nope", turn=1)
    names = {t.name for t in hypotheses.tools(s, hypotheses.SubmitHypotheses(s),
                                              hypotheses.AskReviewer(s, hypotheses.ReviewChannel()))}
    assert {"read_session_turn", "read_session", "list_sessions", "read_assistant_instructions", "read_definition",
            "submit_hypotheses", "ask_reviewer", "find_devices", "device_details", "read_readings",
            "find_actions"} <= names and "read_turn" not in names
    assert "ask_reviewer" not in {t.name for t in hypotheses.reviewer_tools(s, hypotheses.SubmitReview(s))}


def test_submit_keeps_the_best_answer():
    s, _ = sessions()
    submit = hypotheses.SubmitHypotheses(s)
    good = {"hypotheses": [hypothesis({"kind": "registry", "entity_id": SCRIPT, "expose": True})]}
    bad = {"hypotheses": [hypothesis({"kind": "registry", "entity_id": TARGET, "expose": True})]}
    assert submit.run(**bad)["accepted"] is False and submit.best()[0] == "best_rejected"
    assert submit.run(**good)["accepted"] is True and submit.best() == ("accepted", good, [])
    review = hypotheses.SubmitReview(s)
    session = hypotheses.short(sessions()[1]["recording_id"])
    unsupported = {"label": "issue", "decorations": ["blocking"], "subject": "Wrong cause"}
    assert "household evidence" in review.run(comments=[unsupported])["problems"][0]
    bad_ref = {**unsupported, "evidence": [{"session": session, "turn": 9}, {"entity_id": "switch.nope"}]}
    assert len(review.run(comments=[bad_ref])["problems"]) == 2
    good = {**unsupported, "evidence": [{"session": session, "turn": 2, "note": "the user had to correct it"}]}
    assert review.run(comments=[good, {"label": "praise", "decorations": ["non-blocking"], "subject": "Nice"}])["accepted"]
    assert hypotheses.verdict(review.best()[1]) == "request_changes"
    assert hypotheses.verdict({"comments": [{**good, "decorations": ["non-blocking"]}]}) == "approve"


def test_an_idea_sent_on_whose_exact_changes_lack_a_stamp_is_named_while_it_can_get_it_and_otherwise_stands():
    """P0.4, nothing sent on is dropped unsaid. The one idea an answer sends on, when its exact changes lack a stamp,
    is named with that stamp's tool: a re-typed version of the validated idea, an idea approved but untested, an idea
    with no stamp. It's named while the stamp can still be had (approval while reviews are left and the reviewer runs,
    a validated test while tests are left and Simulate can test it), and whatever is left while the answer leaves out
    changes that hold both, which it can send instead. Nothing is wrong with it, so were it the answer left when the
    model stopped, it's the answer (accepted, not best_rejected); once nothing is pending, it's accepted outright, and
    it stays behind (Flow sends on only what holds both)."""
    from custom_components.assist_improver.cases import digest
    from custom_components.assist_improver.stamps import TestIdea
    s, _ = sessions()
    ask = hypotheses.AskReviewer(s, hypotheses.ReviewChannel())
    tests, left = [], {"tests": 2}
    flow = SimpleNamespace(tests=lambda rid, number=None: list(tests), tests_left=lambda rid: left["tests"])
    submit = hypotheses.SubmitHypotheses(s, ask, TestIdea(flow, "rid", ask, 3))
    validated = hypothesis({"kind": "preference", "text": "Press the relay once.", "for": "everyone"})
    retyped = {**validated, "changes": [{**validated["changes"][0], "text": "Press the relay once. "}]}
    untested = {**hypothesis({"kind": "registry", "entity_id": SCRIPT, "expose": True}), "title": "Let Assist see the press script"}
    unstamped = {**hypothesis({"kind": "preference", "text": "The relay sticks.", "for": "everyone"}),
                 "title": "Say it sticks"}
    for idea in (validated, untested):
        ask.reviews.append({"hypothesis": {**idea, "changes": s.with_targets(idea["changes"])}, "verdict": "approve"})
    tests.append({"idea": validated["title"], "changes": digest(s.with_targets(validated["changes"])),
                  "validated": True})

    def named(idea):
        """The tools of the stamps the pending lines name for this idea, sent on alone."""
        result = submit.run(hypotheses=[idea])
        assert result["accepted"] == (not result.get("problems")), result
        return [tool for line in result.get("problems") or [] for tool in ("ask_reviewer", "test_idea")
                if f"({tool})" in line]
    assert named(validated) == []
    assert named(retyped) == named(unstamped) == ["ask_reviewer"] and named(untested) == ["test_idea"]
    assert submit.best() == ("accepted", {"hypotheses": [untested]}, [])
    # No stamp can be had any more, but the validated changes can still be sent instead: each is named all the same.
    left["tests"], ask.cap, ask.stopped = 0, len(ask.reviews), True
    assert named(retyped) == named(unstamped) == ["ask_reviewer"] and named(untested) == ["test_idea"]
    left["tests"], ask.cap, ask.stopped = 2, hypotheses.REVIEWS_PER_ROUND, False
    tests.clear()  # No changes hold both: an idea is named only while its stamp can still be had.
    assert named(untested) == ["test_idea"] and named(unstamped) == ["ask_reviewer"]
    submit.tester.untestable.add(digest(s.with_targets(untested["changes"])))  # Simulate said it can't test it.
    assert named(untested) == []
    submit.tester.untestable.clear()
    left["tests"] = 0  # No test left.
    assert named(untested) == [] and named(unstamped) == ["ask_reviewer"]
    ask.cap = len(ask.reviews)  # The review budget is used up.
    assert named(unstamped) == []
    ask.cap = hypotheses.REVIEWS_PER_ROUND
    ask.stopped = True  # The reviewer stopped.
    assert named(unstamped) == [] and submit.best() == ("accepted", {"hypotheses": [unstamped]}, [])


class RoleRouter:
    """A scripted model serving several concurrent AI Task conversations: replies chosen by which tools it got."""

    def __init__(self, client, scripts):
        self.client, self.scripts = client, scripts
        client.create = self.create

    async def create(self, **kwargs):
        self.client.requests.append(kwargs)
        names = {t.get("name") for t in kwargs.get("tools") or []}
        role = ("loop_reviewer" if "wait_for_draft" in names else "generator" if "submit_hypotheses" in names
                else "reviewer")
        from anthropic_fixture import EventStream
        return EventStream(self.scripts[role].pop(0))


@pytest.mark.asyncio
async def test_the_generator_and_its_reviewer_through_real_ai_task_code(recording_hass, effect_guards, monkeypatch):
    """The generator and its long-running reviewer run side by side, on the household's home as it is today."""
    from custom_components.assist_improver import async_setup_entry, async_unload_entry, proposal
    from custom_components.assist_improver.stamps import TestIdea
    hass = recording_hass
    _, client = task_entity(hass, "Done.")
    s, analysis = sessions()
    answer = {"hypotheses": [hypothesis({"kind": "registry", "entity_id": SCRIPT, "expose": True})]}
    approve = {"comments": [{"label": "praise", "decorations": ["non-blocking"], "subject": "Minimal and right"}]}
    tool = lambda n, name, args: [{"type": "tool_use", "id": n, "name": name, "input": args}]
    RoleRouter(client, {
        "generator": [tool("g1", "ask_reviewer", answer["hypotheses"][0]), tool("g2", "submit_hypotheses", answer),
                      [{"type": "text", "text": "Done."}]],
        "loop_reviewer": [tool("r1", "wait_for_draft", {}), tool("r2", "submit_review", approve),
                          tool("r3", "wait_for_draft", {}), [{"type": "text", "text": "Finished."}]]})
    entry = fake_entry({CONF_AI_TASK: TASK}); await async_setup_entry(hass, entry)
    record = make_recording()
    other = {**make_recording(), "id": "other-recording"}  # Never analysed: the roles still see it.
    at_home(monkeypatch, record)
    for r in (record, other):
        await entry.runtime_data.recorder.store(r["id"]).async_save(r)
        entry.runtime_data.recorder.index[r["id"]] = {"status": r["status"]}
    no_tests = SimpleNamespace(tests=lambda rid, number=None: [], tests_left=lambda rid: 0)  # A round with none left.
    try:
        run = await proposal.propose(hass, entry, entry.runtime_data, [analysis],
                                     tester=lambda ask: TestIdea(no_tests, record["id"], ask, 0))
        assert run["status"] == "completed" and run["answer_status"] == "accepted", run.get("error")
        assert run["output"] == answer and run["kind"] == "hypothesis_generator"
        assert run["sessions"] == sorted([hypotheses.short(record["id"]), "other-re"])
        (loop,) = run["reviews"]
        assert loop["review"] == approve and loop["verdict"] == "approve"
        assert run["reviewer_run"]["status"] == "completed"
        assert effect_guards == []
    finally:
        await async_unload_entry(hass, entry)


@pytest.mark.asyncio
async def test_the_review_budget_is_enforced(recording_hass):
    s, _ = sessions()
    reviewer = hypotheses.AskReviewer(s, hypotheses.ReviewChannel())
    reviewer.reviews = [{"hypothesis": {"title": "Press the relay"}}] * hypotheses.REVIEWS_PER_IDEA
    call = SimpleNamespace(tool_args=hypothesis({"kind": "registry", "entity_id": SCRIPT, "expose": True}))
    assert "budget" in (await reviewer.async_call(recording_hass, call, None)).data["error"]
    reviewer.reviews = [{"hypothesis": {"title": f"idea {n}"}} for n in range(hypotheses.REVIEWS_PER_ROUND)]
    assert "budget for this round" in (await reviewer.async_call(recording_hass, call, None)).data["error"]


@pytest.mark.asyncio
async def test_parallel_asks_stay_within_the_review_budget_and_each_review_answers_its_own_draft(recording_hass):
    """P0.13: a model's tool calls in one message run at once. Two asks at once with one review left: one review, and
    one answer that the budget is used up. With two left, and a reviewer whose review names the draft it read, each
    answer names its own draft."""
    import asyncio
    from homeassistant.helpers import llm
    s, _ = sessions()
    one = hypothesis({"kind": "registry", "entity_id": SCRIPT, "expose": True})
    two = {**hypothesis({"kind": "preference", "text": "The relay sticks.", "for": "everyone"}),
           "title": "Say it sticks"}

    async def asked(cap):
        channel = hypotheses.ReviewChannel()
        ask, wait = hypotheses.AskReviewer(s, channel), hypotheses.WaitForDraft(channel)
        submit = hypotheses.ChannelSubmitReview(s, channel)
        ask.cap = cap

        async def reviewer():
            waiting = llm.ToolInput(tool_name=wait.name, tool_args={})
            while "draft" in (got := (await wait.async_call(recording_hass, waiting, None)).data):
                assert submit.run(comments=[{"label": "praise", "decorations": ["non-blocking"],
                                             "subject": got["draft"]["title"]}])["accepted"]
        channel.reviewer = asyncio.ensure_future(reviewer())
        answers = await asyncio.gather(*[ask.async_call(recording_hass, llm.ToolInput(tool_name=ask.name, tool_args=d),
                                                        None) for d in (one, two)])
        channel.close()
        await asyncio.wait_for(channel.reviewer, 1)
        return ask, [answer.data for answer in answers]
    ask, answers = await asked(1)
    assert len(ask.reviews) == 1
    assert sorted("budget" in answer.get("error", "") for answer in answers) == [False, True]
    ask, answers = await asked(2)
    assert [answer["comments"][0]["subject"] for answer in answers] == [one["title"], two["title"]]


@pytest.mark.asyncio
async def test_wait_for_draft_twice_at_once_or_after_finishing_never_blocks_the_reviewer(recording_hass):
    """The reviewer is a model too: it may call wait_for_draft twice in one message, which Home Assistant runs at once,
    or again after it was told it's finished. With one draft waiting, the first call gets the draft and the second is
    told to submit its review first; once the channel is closed, every call says it's finished. Nothing waits for ever
    (each message here has a second to come back)."""
    import asyncio
    from homeassistant.components.conversation import ToolResultContent
    from homeassistant.components.conversation.chat_log import ChatLog
    from homeassistant.helpers import llm
    from custom_components.assist_improver.exploration import SessionAPI
    hass = recording_hass
    context = llm.LLMContext(platform="test", context=None, language=None, assistant="ai_task", device_id=None)

    async def message(channel, calls):
        """One reviewer message with this many wait_for_draft calls, through Home Assistant's chat log: each answer."""
        tool = hypotheses.WaitForDraft(channel)
        chat_log = ChatLog(hass, "reviewer")
        chat_log.llm_api = await SessionAPI(hass, tools=[tool]).async_get_api_instance(context)
        inputs = [llm.ToolInput(tool_name=tool.name, tool_args={}) for _ in range(calls)]

        async def stream():
            yield {"role": "assistant"}
            yield {"tool_calls": inputs}
        return [content.result.data async for content in chat_log.async_add_delta_content_stream("ai_task.reviewer",
                                                                                                    stream())
                if isinstance(content, ToolResultContent)]
    channel = hypotheses.ReviewChannel()
    asking = asyncio.ensure_future(channel.ask({"title": "One idea"}))
    try:
        first, second = await asyncio.wait_for(message(channel, 2), 1)
    finally:
        asking.cancel()
    assert first == {"draft": {"title": "One idea"}} and "submit_review" in str(second)
    closed = hypotheses.ReviewChannel()
    closed.close()
    for calls in (1, 1, 2):
        assert all("finished" in answer for answer in await asyncio.wait_for(message(closed, calls), 1))


def test_every_goal_with_friction_is_fixed_or_explained():
    s, analysis = sessions()
    session = hypotheses.short(analysis["recording_id"])
    assert "still have friction that no hypothesis fixes" in hypotheses.problems(s, {"hypotheses": []})[0]
    explained = {"hypotheses": [], "not_fixed": [{"session": session, "goal_turn": 1, "why": "speech fault"}]}
    assert hypotheses.problems(s, explained) == []


def test_the_generator_and_reviewer_hear_what_the_household_sent_back_for_its_round_and_its_decisions_as_made():
    """One explicit input for what the household sent back (a revision, with its note and goal, and the improvement in
    full), beside a plain history of their decisions (with a decline's reason): the reviewer hears both as the
    generator does."""
    s, _ = sessions()
    line = {"kind": "preference", "for": "everyone", "text": "The relay is pressed once."}
    s.feedback = [{"title": "Press once", "changes": [line], "decision": "decline", "note": "",
                   "reason": "not_an_issue", "status": "declined"}]
    s.sent_back = {"why": "revision", "note": "Press it twice", "goal": {"summary": "Press it twice"},
                   "improvement": {"improvement": "imp1", "changes": [{"kind": "script", "steps": [{"stop": "x"}]}]}}
    overview = s.overview()
    assert overview["sent_back"] == s.sent_back
    assert overview["household_decisions"] == [{"title": "Press once", "decision": "decline", "status": "declined",
                                                "reason": "not_an_issue", "changes": [line]}]
    summary = s.summary()
    assert (summary["sent_back"], summary["household_decisions"]) == (s.sent_back, overview["household_decisions"])


@pytest.mark.asyncio
async def test_drafts_reach_the_reviewer_and_rejections_come_back_with_a_next_step(recording_hass):
    import asyncio
    s, analysis = sessions()
    channel = hypotheses.ReviewChannel()
    ask, wait = hypotheses.AskReviewer(s, channel), hypotheses.WaitForDraft(channel)
    submit = hypotheses.ChannelSubmitReview(s, channel)
    assert "call wait_for_draft first" in submit.run(comments=[])["problems"][0]
    answers = [{"comment": 1, "resolution": "resolved", "note": "narrowed"}]
    draft = {**hypothesis({"kind": "preference", "text": "x", "for": "everyone"}), "responses": answers}
    asking = asyncio.ensure_future(ask.async_call(recording_hass, SimpleNamespace(tool_args=draft), None))
    received = (await wait.async_call(recording_hass, SimpleNamespace(tool_args={}), None)).data["draft"]
    assert received["responses"] == answers  # The reviewer sees the generator's answers to its comments.
    session = hypotheses.short(analysis["recording_id"])
    assert submit.run(comments=[{"label": "issue", "decorations": ["blocking"], "subject": "Inconsistent",
                                 "evidence": [{"session": session, "turn": 1}]}])["accepted"]
    result = (await asking).data
    assert result["outcome"] == "request_changes" and "still need a fix" in result["next_step"]
    channel.close()
    assert "finished" in (await wait.async_call(recording_hass, SimpleNamespace(tool_args={}), None)).data


def test_device_details_as_of_a_session_and_line_placement():
    s, analysis = sessions()
    session = hypotheses.short(analysis["recording_id"])
    details = hypotheses.DeviceDetailsAt(s)
    assert details.run(TARGET, session)["as_of_session"] == session and "error" in details.run(TARGET, "nope")
    unplaced = hypotheses.problems(s, {"hypotheses": [hypothesis({"kind": "preference", "text": "x"})]})
    assert any("say who a preference line is for" in p for p in unplaced)
    placed = hypotheses.problems(s, {"hypotheses": [hypothesis({"kind": "preference", "text": "x", "for": "everyone"})]})
    assert placed == [] and "time_zone" in s.overview()


def test_an_editor_item_waits_for_home_assistants_own_check():
    """Home Assistant's validators check a script, automation or scene as the submit tool takes it (check_changes):
    one it never checked is sent back to submit again, and what it found is each problem."""
    s, _ = sessions()
    change = {"kind": "script", "id": "press", "config": {"alias": "Press", "sequence": [
        {"action": "switch.turn_off", "target": {"entity_id": TARGET}}, {"stop": "Pressed the relay once."}]}}
    answer = {"hypotheses": [hypothesis(change)]}
    said = lambda: [p.split(": ", 1)[1] for p in hypotheses.problems(s, answer)]
    assert said() == [hypotheses.checks.UNCHECKED]
    s.checked[digest(change)] = []
    assert said() == []
    s.checked[digest(change)] = ["Home Assistant doesn't accept this script: it says why."]
    assert said() == ["Home Assistant doesn't accept this script: it says why."]


def test_hypotheses_carry_the_sessions_they_agree_and_disagree_with():
    s, _ = sessions()
    expose = {"kind": "registry", "entity_id": SCRIPT, "expose": True}
    bare = {**hypothesis(expose), "support": []}
    assert any("list in support" in p for p in hypotheses.problems(s, {"hypotheses": [bare]}))
    wrong = {**hypothesis(expose), "conflicts": [{"session": "nope", "note": "x"}]}
    assert any("conflicts names no session" in p for p in hypotheses.problems(s, {"hypotheses": [wrong]}))


def test_existing_improvements_on_the_same_devices_are_shown_upfront_and_can_answer_for_a_goal():
    s, analysis = sessions()
    session = hypotheses.short(analysis["recording_id"])
    started = s.views[session].digest["started"]
    pending = {"improvement": "p1", "title": "Press the relay with one script", "status": "waiting for review",
               "cause": "No script", "changes": [{"kind": "registry", "entity_id": SCRIPT, "expose": True}], "goals": ["older:1"],
               "devices": [TARGET, SCRIPT]}
    elsewhere = {**pending, "improvement": "p2", "title": "Kitchen lights", "devices": ["light.kitchen"]}
    s.existing = [pending, elsewhere]
    # Code finds the improvement on the same device and shows it upfront; the other isn't related.
    (related,) = s.overview()["related_improvements"]
    assert related["improvement"] == "p1" and related["may_address"] == [f"{session}:1"]
    assert related["shared_devices"] == [TARGET] and "devices" not in related
    listed = hypotheses.ListImprovements(s.household, s.existing).run()["improvements"]  # All of them on request.
    assert [i["improvement"] for i in listed] == ["p1", "p2"] and "devices" not in listed[0]
    s.existing = [{**pending, "details": {"expected": "One turn", "tested_on": [{"goal": "older:1", "before": []}]}},
                  elsewhere]
    full = hypotheses.ReadImprovement(s.household, s.existing).run(improvement="p1")  # In full, to tell a duplicate.
    assert full["expected"] == "One turn" and full["tested_on"][0]["goal"] == "older:1"
    assert full["title"] == pending["title"]
    assert "details" not in hypotheses.ListImprovements(s.household, s.existing).run()["improvements"][0]
    missing = hypotheses.ReadImprovement(s.household, s.existing).run(improvement="nope")
    assert "no improvement 'nope'" in missing["error"]
    addressed = {"hypotheses": [], "already_addressed": [{"session": session, "goal_turn": 1, "improvement": "p1",
                                                          "why": "It adds the press script this goal needed"}]}
    assert hypotheses.problems(s, addressed) == []  # The goal is accounted for, without a new idea.
    assert hypotheses.open_goals(s, addressed) == []
    wrong = lambda **ref: hypotheses.problems(s, {"hypotheses": [], "already_addressed": [
        {"session": session, "goal_turn": 1, "improvement": "p1", "why": "Same cause", **ref}]})
    assert "no improvement 'nope'" in wrong(improvement="nope")[0]
    assert "no goal with friction" in wrong(goal_turn=4)[0]
    assert "a hypothesis fixes it too" in hypotheses.problems(s, {
        **addressed, "hypotheses": [hypothesis({"kind": "registry", "entity_id": SCRIPT, "expose": True})]})[0]
    # An improvement applied before the session didn't stop its friction, so it can't answer for it.
    s.existing = [{**pending, "status": "applied", "applied": "2000-01-01T00:00:00+00:00"}]
    assert "was applied before this session" in wrong()[0]
    s.existing = [{**pending, "status": "applied", "applied": "2999-01-01T00:00:00+00:00"}]
    assert wrong() == [] and started < "2999"


@pytest.mark.asyncio
async def test_a_reviewer_draft_carries_the_related_improvements(recording_hass):
    import asyncio
    s, _ = sessions()
    s.existing = [{"improvement": "p1", "title": "Press script", "status": "applied", "cause": "", "changes": [],
                   "applied": "2999-01-01T00:00:00+00:00", "goals": [], "devices": [SCRIPT]}]
    channel = hypotheses.ReviewChannel()
    ask, wait = hypotheses.AskReviewer(s, channel), hypotheses.WaitForDraft(channel)
    draft = hypothesis({"kind": "registry", "entity_id": SCRIPT, "expose": True})
    asking = asyncio.ensure_future(ask.async_call(recording_hass, SimpleNamespace(tool_args=draft), None))
    received = (await wait.async_call(recording_hass, SimpleNamespace(tool_args={}), None)).data["draft"]
    assert [r["improvement"] for r in received["related_improvements"]] == ["p1"]  # Same device as its change.
    channel.close()
    asking.cancel()
