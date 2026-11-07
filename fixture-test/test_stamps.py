"""The proposer's stamps on real runs (stamps.py, flow.test; SPEC.md P0.4, P0.13): what goes on to Write up is the idea
the proposer sent with both stamps, each on exactly its changes, and Simulate's way back follows a test. The AI roles
and the household's assistant are scripted (kit.py), on Home Assistant's real AI Task path (ha_task.py): a model's
tool calls in one message run at once, and only argument errors come back to it."""

import asyncio
from copy import deepcopy

import pytest
from homeassistant.helpers import llm

from ha_task import use, use_many
from recordings import before
from kit import (NOT_FIXED, Model, approve, candidate_turn, idea, improver, propose_record,  # noqa: F401 - a fixture.
                 tools_of_a_round)
from custom_components.assist_improver import evidence, simulate
from custom_components.assist_improver.cases import digest

REWORDED = "The relay needs one press, not a toggle."
OTHER = "Presses the fixture relay, off then on."  # Another version of the press script: other changes.
BETTER = "Presses the fixture relay, then says it's pressed."


async def round_with(improver, proposer, blocks=None):
    """The friction conversation through every stage, with this proposer (hypothesis_generator's part); the
    household's assistant replays it without the change, then with the press script, unless blocks say otherwise."""
    improver.model.hypothesis_generator = proposer
    await improver.arrives(improver.friction)
    improver.client.blocks = before(improver.friction) + candidate_turn() if blocks is None else blocks
    run = await improver.run()
    assert run["status"] == "done", run["error"]
    return run


def recorded(improver, changes):
    """The Propose record's ideas with exactly these changes."""
    return [i for i in propose_record(improver)["ideas"] if digest(i["hypothesis"]["changes"]) == digest(changes)]


def sent_on(improver):
    """The changes of every improvement, as digests."""
    return [digest(item["changes"]) for item in improver.runtime.improvements.items.values()]


@pytest.mark.asyncio
async def test_the_improvement_is_the_idea_sent_on_with_the_test_of_its_changes(improver, monkeypatch):
    """P0.4: an idea goes on to Write up only with both stamps. The proposer may reword an idea after its test (the
    same changes are tested once a round): the improvement is the idea it sent on, carrying that test."""
    use = Model.use

    async def reworded(hass, tool, args=None):
        if tool.name == "submit_hypotheses":
            args = {**args, "hypotheses": [{**h, "cause": REWORDED} for h in args["hypotheses"]]}
        return await use(hass, tool, args)
    monkeypatch.setattr(Model, "use", staticmethod(reworded))
    await improver.arrives(improver.friction)
    improver.client.blocks = before(improver.friction) + candidate_turn()
    run = await improver.run()
    assert run["status"] == "done", run["error"]
    [item] = improver.runtime.improvements.items.values()
    [test] = improver.runtime.flow.tests(improver.friction["id"])
    assert item["cause"] == item["hypothesis"]["cause"] == REWORDED != test["hypothesis"]["cause"]
    assert item["test"] == test and test["validated"]


@pytest.mark.asyncio
async def test_an_idea_that_cant_be_tested_is_no_trip_back_from_simulate(improver):
    """P0.13: Simulate's way back is paid by the round's tests, each trip following a test. An approved idea that
    can't be tested on the conversation (it edits an assistant the conversation didn't use: DETAILS P6.8.5) is never
    replayed, so it's no test (P6.3.3) and spends none, and no trip follows it."""
    model, out = improver.model, {}
    press = idea()
    elsewhere = {**press, "title": "Elsewhere", "changes": [
        {"kind": "assistant", "assistant": "Nobody's assistant", "text": "Be brief."}]}

    async def proposer(hass, payload, tools):
        model.calls.append(("proposer", None))
        await approve(model, hass, tools, press)
        await model.use(hass, tools["test_idea"], press)
        out["approved"] = await approve(model, hass, tools, elsewhere)
        out["elsewhere"] = await model.use(hass, tools["test_idea"], elsewhere)
        await model.use(hass, tools["submit_hypotheses"], {"hypotheses": [press]})
        return "Submitted."
    await round_with(improver, proposer)
    assert out["approved"]["outcome"] == "approve" and out["elsewhere"].get("error") == simulate.NOT_REACHED
    rid = improver.friction["id"]
    assert len(improver.runtime.flow.tests(rid)) == 1
    assert [t for t in improver.runtime.flow.record(rid)["trips"] if t["start"] == "Simulate"] == []


@pytest.mark.asyncio
async def test_every_tool_any_role_gets_is_shared_not_copied_by_home_assistants_trace(improver):
    """HA's conversation trace deep-copies the tools a model is given, and run_role reads that trace (trace.as_dict()).
    A tool holding the flow (and so hass) can't be copied, which killed every proposal on a real install (0.40.0.dev1),
    and a big one is slow to copy: every tool any role gets is shared, not copied. Each role's call completed after its
    trace was read."""
    copied = {}
    for role, tools in (await tools_of_a_round(improver)).items():
        for name, tool in tools.items():
            try:
                copy = deepcopy(tool)
            except Exception as err:  # noqa: BLE001 - one that can't be copied is copied wrong too.
                copy = err
            if copy is not tool:
                copied.setdefault(role, []).append(name)
    assert copied == {}
    runs = improver.runtime.role_runs.runs
    [generator] = [r for r in runs if r.get("kind") == "hypothesis_generator"]
    statuses = {r["role"]: r["status"] for r in runs if r.get("role")}
    statuses["hypothesis_reviewer"] = generator["reviewer_run"]["status"]
    assert {"journey_analyzer", "hypothesis_generator", "hypothesis_reviewer"} <= set(statuses)
    assert set(statuses.values()) == {"completed"}


@pytest.mark.asyncio
async def test_an_idea_the_reviewer_hasnt_approved_isnt_tested_and_an_approval_says_to_test_it(improver, monkeypatch):
    """P0.4, approval first: test_idea refuses an idea the reviewer hasn't approved (so an idea that validates has both
    stamps; on a real install a proposer collected each stamp on a different wording), and an approval's answer says
    to test that same idea. The control: the approved idea is tested and validates."""
    use, answers = Model.use, {}

    async def watched(hass, tool, args=None):
        if tool.name == "test_idea" and "unreviewed" not in answers:
            answers["unreviewed"] = await use(hass, tool, {**args, "title": "Not reviewed yet",
                                                           "changes": idea(text=OTHER)["changes"]})
        result = await use(hass, tool, args)
        answers.setdefault(tool.name, []).append(result)
        return result
    monkeypatch.setattr(Model, "use", staticmethod(watched))
    await improver.arrives(improver.friction)
    improver.client.blocks = before(improver.friction) + candidate_turn()
    run = await improver.run()
    assert run["status"] == "done", run["error"]
    assert "ask_reviewer" in answers["unreviewed"]["error"] and "validated" not in answers["unreviewed"]
    [approval] = [a for a in answers["ask_reviewer"] if a["outcome"] == "approve"]
    assert "test_idea" in approval["next_step"] and answers["test_idea"][-1]["validated"]


@pytest.mark.asyncio
@pytest.mark.parametrize("approved", ["Y", "X"])
async def test_an_approval_holds_only_for_the_exact_changes_the_reviewer_last_judged(improver, approved):
    """P0.4: the reviewer's stamp is on the changes it judged, not on the idea's title. Under one title, the reviewer
    blocks version X and approves version Y: test_idea refuses X, and X sent on doesn't go on. Or it approves X, and
    the proposer then tests Y: test_idea refuses Y, and Y sent on doesn't go on."""
    x, y = idea(), idea(text=OTHER)
    model, out = improver.model, {}
    judged, other = (y, x) if approved == "Y" else (x, y)

    async def proposer(hass, payload, tools):
        model.calls.append(("proposer", None))
        if approved == "Y":
            out["blocked"] = await model.use(hass, tools["ask_reviewer"], x)  # The first time, it asks for changes.
        out["approved"] = await approve(model, hass, tools, judged)
        out["test"] = await model.use(hass, tools["test_idea"], other)
        await model.use(hass, tools["submit_hypotheses"], {"hypotheses": [other]})
        return "Submitted."
    await round_with(improver, proposer)
    assert out.get("blocked", {"outcome": "request_changes"})["outcome"] == "request_changes"
    assert out["approved"]["outcome"] == "approve"
    assert "ask_reviewer" in out["test"].get("error", "") and "validated" not in out["test"]
    assert digest(other["changes"]) not in sent_on(improver)


@pytest.mark.asyncio
async def test_a_blocked_better_version_under_the_same_title_leaves_the_approved_validated_version_to_go_on(improver):
    """P0.9, sends on what's validated or tries to make it better first: X is approved and validated; a better
    version Y under the same title is blocked; the proposer sends X on. X goes on as the improvement, with exactly its
    changes, the record shows X with both stamps, and the round doesn't end No fix found."""
    x, y = idea(), idea(text=BETTER)
    model, out = improver.model, {}

    async def reviewer(hass, payload, tools):  # Approves X; blocks the better version, which claims what it can't see.
        while "draft" in (got := await model.use(hass, tools["wait_for_draft"])):
            draft = got["draft"]
            comment = ({"label": "issue", "decorations": ["blocking"], "subject": "It can't tell it's pressed",
                        "evidence": [{"session": draft["goals"][0]["session"], "turn": 1}]}
                       if draft["changes"][0]["config"]["description"] == BETTER else
                       {"label": "praise", "decorations": ["non-blocking"], "subject": "One press, as asked"})
            await model.use(hass, tools["submit_review"], {"comments": [comment]})
        return "Finished."

    async def proposer(hass, payload, tools):
        model.calls.append(("proposer", None))
        out["x"] = await model.use(hass, tools["ask_reviewer"], x)
        out["test"] = await model.use(hass, tools["test_idea"], x)
        out["y"] = await model.use(hass, tools["ask_reviewer"], y)
        await model.use(hass, tools["submit_hypotheses"], {"hypotheses": [x]})
        return "Submitted."
    model.hypothesis_reviewer = reviewer
    await round_with(improver, proposer)
    assert (out["x"]["outcome"], out["test"].get("validated"), out["y"]["outcome"]) == ("approve", True,
                                                                                         "request_changes")
    assert sent_on(improver) == [digest(x["changes"])]
    [kept] = recorded(improver, x["changes"])
    assert kept["stamps"] == {"Review": True, "Simulate": True} and kept["sent_on"]
    assert improver.runtime.flow.place(improver.friction["id"])["outcome"] != "no fix found"


@pytest.mark.asyncio
@pytest.mark.parametrize("at_once", ["gathered", "in one message"])
async def test_parallel_test_idea_calls_never_spend_more_tests_than_are_left_and_test_the_same_changes_once(improver,
                                                                                                         at_once):
    """P0.9, one allowance; P6.3.4, the same changes tested once: three test_idea calls at once with one test left, two
    of them with the same changes, on the round's own TestIdea (asyncio.gather), or as one message with three tool
    calls, which Home Assistant runs at once. Exactly one test is recorded, and each other call says the tests are used
    up or answers with that same test. None is left, no trip back from Simulate is made, and the replays without and
    with the change each run once."""
    model, out = improver.model, {}
    improver.entry.options = {**improver.entry.options, "tests_per_goal": 1}
    a, b = idea(), idea("Press it once, off then on", OTHER)
    calls = [a, deepcopy(a), b]

    async def proposer(hass, payload, tools):
        model.calls.append(("proposer", None))
        await approve(model, hass, tools, a)
        await approve(model, hass, tools, b)
        test = tools["test_idea"]
        if at_once == "gathered":
            answers = await asyncio.gather(*[test.async_call(hass, llm.ToolInput(tool_name=test.name, tool_args=h),
                                                             None) for h in calls])
            out["answers"] = [getattr(answer, "data", answer) for answer in answers]
        else:
            out["answers"] = await use_many(hass, [(test, h) for h in calls])
        validated = [h for h, answer in zip(calls, out["answers"]) if answer.get("validated")]
        await model.use(hass, tools["submit_hypotheses"], {"hypotheses": validated[:1]} if validated else NOT_FIXED)
        return "Submitted."
    await round_with(improver, proposer)
    rid, flow = improver.friction["id"], improver.runtime.flow
    assert len(flow.tests(rid)) == 1 and flow.tests_left(rid) == 0
    tested = [answer for answer in out["answers"] if "validated" in answer]
    assert tested and all(answer == tested[0] or "used up" in answer.get("error", "") for answer in out["answers"])
    assert [t for t in flow.record(rid)["trips"] if t["start"] == "Simulate"] == []
    assert len(improver.client.requests) == len(before(improver.friction)) + len(candidate_turn())


@pytest.mark.asyncio
async def test_simulate_is_blamed_only_for_its_own_failures(improver):
    """P0.8 holds Propose when Simulate couldn't test (a stamp it can't get), not when the proposer's call was wrong:
    test_idea with a change missing its kind, while no idea validates. The model gets the argument error back,
    Simulate isn't marked as crashed, and Propose ends the round No fix found on its one try, instead of failing with
    "Simulate couldn't test"."""
    model, out = improver.model, {}
    press = idea()
    kindless = {**press, "changes": [{k: v for k, v in c.items() if k != "kind"} for c in press["changes"]]}

    async def proposer(hass, payload, tools):
        model.calls.append(("proposer", None))
        await approve(model, hass, tools, press)
        out["tester"] = tools["test_idea"]
        out["test"] = await use(hass, tools["test_idea"], kindless, slip=True)
        await model.use(hass, tools["submit_hypotheses"], NOT_FIXED)
        return "Submitted."
    await round_with(improver, proposer, blocks=[])
    assert "Invalid" in out["test"].get("error", "") and out["tester"].crashed is None
    assert improver.runtime.flow.place(improver.friction["id"])["outcome"] == "no fix found"
    assert [attempt["status"] for attempt in propose_record(improver)["attempts"]] == ["done"]


@pytest.mark.asyncio
async def test_one_change_under_two_titles_is_one_idea_and_one_improvement_handed_on_once(improver):
    """P0.2, one record: two titles with the same changes, both approved, one test of them under the first, sent on
    under the second. It's one improvement, handed on once: Write up and the Proposed handoff list it once, the run
    counts one new improvement, and the Propose record holds one idea for those changes, listing both titles."""
    model = improver.model
    a, b = idea(), idea("One press script")

    async def proposer(hass, payload, tools):
        model.calls.append(("proposer", None))
        await approve(model, hass, tools, a)
        await approve(model, hass, tools, b)
        await model.use(hass, tools["test_idea"], a)
        await model.use(hass, tools["submit_hypotheses"], {"hypotheses": [b]})
        return "Submitted."
    run = await round_with(improver, proposer)
    [item] = improver.runtime.improvements.items.values()
    conv = improver.runtime.flow.record(improver.friction["id"])
    current = conv["rounds"][str(conv["round"])]
    [proposed] = [handoff for handoff in current["handoffs"] if handoff["start"] == "Propose"]
    assert current["records"]["Write up"]["improvements"] == proposed["record"]["improvements"] == [item["id"]]
    assert run["progress"]["new_improvements"] == 1
    [one] = recorded(improver, a["changes"])
    assert sorted(one["titles"]) == sorted([a["title"], b["title"]])


@pytest.mark.asyncio
async def test_a_cut_off_proposal_leaves_no_test_running_to_change_the_held_round(improver):
    """P0.8 with P0.2: two test_idea calls in one message, and the run is cut off (its task cancelled, as Cancel or a
    restart cuts it) at the first replay either asks for. Once Home Assistant has settled, the held round's tests and
    trips are those at the cut, and the household's assistant got no request after it."""
    model, client, pipeline = improver.model, improver.client, improver.runtime.pipeline
    rid, flow = improver.friction["id"], improver.runtime.flow
    a, b = idea(), idea("Press it once, off then on", OTHER)
    at_cut = {}

    async def proposer(hass, payload, tools):
        model.calls.append(("proposer", None))
        await approve(model, hass, tools, a)
        await approve(model, hass, tools, b)
        await use_many(hass, [(tools["test_idea"], a), (tools["test_idea"], b)])
        return "Submitted."

    async def cut_at_the_first_replay(request):
        if not at_cut:
            at_cut.update(tests=deepcopy(flow.tests(rid)), trips=deepcopy(flow.record(rid)["trips"]),
                          requests=len(client.requests))
            pipeline._task.cancel()
        while not pipeline._task.done():  # Nothing is answered while the run winds down.
            await asyncio.sleep(0.01)
        return client.blocks.pop(0)
    model.hypothesis_generator = proposer
    client.reply = cut_at_the_first_replay
    await improver.arrives(improver.friction)
    client.blocks = before(improver.friction) * 2 + candidate_turn() * 2
    pipeline.start(trigger="manual")
    with pytest.raises(asyncio.CancelledError):
        await pipeline._task
    await improver.hass.async_block_till_done()
    assert at_cut and flow.place(rid)["held"]["cause"] == "cut off"
    assert (flow.tests(rid), flow.record(rid)["trips"]) == (at_cut["tests"], at_cut["trips"])
    assert len(client.requests) == at_cut["requests"]


@pytest.mark.asyncio
async def test_a_cut_off_proposal_leaves_no_review_asked_after_the_cut(improver, monkeypatch):
    """P0.8, the reviewer's stamp too: two ask_reviewer calls in one message, and the run is cut off while the reviewer
    has the first draft. The second, waiting its turn, stops with the run: no sandbox check of it and no review of it
    after the cut."""
    model, pipeline, out = improver.model, improver.runtime.pipeline, {}
    checked, check = [], evidence.Evidence.check

    async def counted(self, hypothesis, hass=None):
        checked.append(hypothesis["title"])
        return await check(self, hypothesis, hass)
    monkeypatch.setattr(evidence.Evidence, "check", counted)

    async def proposer(hass, payload, tools):
        out["ask"] = tools["ask_reviewer"]
        await use_many(hass, [(tools["ask_reviewer"], idea()), (tools["ask_reviewer"], idea("Off then on", OTHER))])
        return "Submitted."

    async def reviewer(hass, payload, tools):
        assert "draft" in await use(hass, tools["wait_for_draft"])
        pipeline._task.cancel()  # Cut off, as Cancel or a restart cuts it, with the first draft under review.
        await asyncio.Event().wait()
    model.hypothesis_generator, model.hypothesis_reviewer = proposer, reviewer
    await improver.arrives(improver.friction)
    pipeline.start(trigger="manual")
    with pytest.raises(asyncio.CancelledError):
        await pipeline._task
    await improver.hass.async_block_till_done()
    assert len(checked) == 1 and out["ask"].reviews == []


def test_the_proposer_reads_why_a_question_is_still_unanswered_and_which_was_set_aside():
    """A question the change doesn't get answered shows with the judge's reason on its root; one the replay without
    the change answered otherwise couldn't be told."""
    from custom_components.assist_improver.stamps import result
    judged = {"1:0": {"summary": "Whether the fan is on", "root": "1:0", "kind": "fact"},
              "2:1": {"summary": "Whether the lamp is on", "root": "2:1", "kind": "fact"}}
    replay = {"goals": {"1:0": "unmet", "2:1": "indeterminate"}, "turns_per_root_goal": {"1:0": 1, "2:1": 1},
              "side_effects_left": [], "conduct": {}, "facts": {
                  "1:0": {"state": "unmet", "answers": [{"turn": 1, "gives": False, "why": "Says off; it's on."}]},
                  "2:1": {"state": "indeterminate", "answers": [], "set_aside": True}}}
    found = result({"validated": False, "gains": [], "judged": judged, "before": replay, "after": replay})
    assert found["roots"][0]["why"] == "Says off; it's on." and "why" not in found["roots"][1]
    assert found["set_aside"] == ["Whether the lamp is on"]
