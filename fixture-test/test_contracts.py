"""The stages' contracts (DETAILS P3.2; Kai, 2026-10-26: "well-defined input & output shapes of everything that goes
in and out of each stage, and tests to make sure input and output shapes are well connected, and that each stage
handles its input shape well"; and, later that day: Triage, Analyze, the proposer's Review, Write-up and the
household's Review are stages of their own, "the contract between the Proposer and Reviewer, and Reviewer back with
Proposer, and Proposer and Write-up, should all be well defined, typed and tested"). Each stage's real output meets
its contract, checked where it's made; each next stage reads the least its contract allows; a stage whose output
breaks its contract fails that item, with why, and hands nothing on."""

import pytest

from types import SimpleNamespace

from recordings import before
from kit import candidate_turn, improver, runtime_with_flow  # noqa: F401 - improver is a fixture.
from editors import Editors
from custom_components.assist_improver import contracts, records
from custom_components.assist_improver.contracts import ContractError, problems


HANDOFFS = {  # Each stage's output, by the record type its contract is: Triage → ... → the household's decision.
    "Triage": records.TriageVerdict, "Analyze": records.AnalysisResult, "Propose → Review": records.Draft,
    "Review → its loop": records.ReviewAnswer, "Review → Propose": records.ReviewResult,
    "Propose → Simulate": records.Hypothesis, "Simulate": records.Test, "Simulate → Write-up": records.Improvement,
    "Write-up": records.Copy, "the household's Review": records.Decision}


@pytest.mark.asyncio
async def test_each_stages_real_output_meets_its_contract(improver, monkeypatch):
    """A full run on a conversation with trouble, then the household's accept: every hand-off between stages went
    through its contract's check where it was made, and each met it, with every field the next stage reads."""
    runtime, client = improver.runtime, improver.client
    real, checked = contracts.check, []

    def spied(value, kind, what="record"):
        checked.append((kind, value))
        return real(value, kind, what)
    for module in ("analysis", "improvements", "hypotheses.tools"):
        target = __import__(f"custom_components.assist_improver.{module}", fromlist=["contracts"])
        monkeypatch.setattr(target.contracts, "check", spied)
    await improver.arrives(improver.friction)
    client.blocks = before(improver.friction) + candidate_turn()
    run = await improver.run()
    assert run["status"] == "done", run["error"]
    [item] = runtime.improvements.pending()
    saves = Editors(improver.hass).save  # The panel's half: it saves the scripts.
    assert (await runtime.improvements.decide(item["id"], "accept", user_id="alex", editors=saves))["status"] == "applied"
    seen = {kind for kind, _ in checked}
    assert {stage: kind for stage, kind in HANDOFFS.items() if kind not in seen and kind is not records.Hypothesis
            } == {}  # Each checked where it's made (Propose's ideas: below).
    for kind, value in checked:
        assert problems(value, kind) == [], kind.__name__
    runs = runtime.role_runs.runs
    analyses = [r["analysis"] for r in runs if r.get("kind") == "journey_analyzer" and r.get("analysis")]
    proposals = [r["output"] for r in runs if r.get("kind") == "hypothesis_generator" and r.get("output")]
    tests = runtime.flow.tests(improver.friction["id"])
    assert analyses and proposals and tests
    for analysis in analyses:
        assert problems(analysis, records.AnalysisResult) == []
    for answer in proposals:
        assert problems(answer, records.ProposalAnswer) == []
    for test in tests:
        assert problems(test, records.Test) == []
    triaged = [r["triage"] for r in runs if r.get("kind") == "journey_analyzer" and r.get("triage")]
    assert triaged and all(problems(t, records.TriageCheck, required=False) == [] for t in triaged)


@pytest.mark.asyncio
async def test_each_stage_reads_the_least_its_contract_allows(hass):
    """The fewest fields a contract allows, read by the stage after it: nothing more is needed, nothing breaks. A move
    between stages keeps its handoff on the flow record with just the fields its contract (records.HANDOFFS) has."""
    from custom_components.assist_improver import triage
    from custom_components.assist_improver.editor import payload
    from custom_components.assist_improver.hypotheses.schema import verdict
    from custom_components.assist_improver.hypotheses.tools import review_result
    from custom_components.assist_improver.improvements import display_title
    sure = {"smooth": True, "sure": True}
    assert problems(sure, records.TriageVerdict) == [] and triage.skips_analysis(sure)  # Triage → the end.
    assert not triage.skips_analysis({"smooth": True, "sure": False})  # ...or on to Analyze.
    draft = {"title": "A change", "goals": [], "changes": [], "friction_it_targets": {},
             "sessions_where_it_would_apply": {}}
    assert problems(draft, records.Draft) == []  # Propose → Review: the reviewer reads it as it comes.
    review = {"comments": [{"label": "praise", "decorations": ["non-blocking"], "subject": "Fine"}]}
    assert problems(review, records.ReviewAnswer) == [] and verdict(review) == "approve"
    back = review_result(draft, review)  # Review → Propose.
    assert problems(back, records.ReviewResult) == [] and back["outcome"] == "approve"
    replay = {"plan": "p", "status": "done", "said": [1], "replies": [{"turn": 1, "you": "Hi", "assistant": "Hi"}],
              "goals": {"1:0": "met"}, "goals_met": ["1:0"], "turns_per_root_goal": {"1:0": 1}, "counted": {"1:0": [1]},
              "side_effects_left": [], "started_from": {}, "config": {}, "conduct": {}, "facts": {}}
    test = {"idea": "A change", "changes": "c", "hypothesis": {"title": "A change", "changes": []},
            "judged": {"1:0": {"summary": "Hi", "turn": 1, "root": "1:0", "devices": []}}, "before": replay,
            "after": replay, "gains": [], "validated": False, "setup": {}, "tested": {}, "provider": {}, "user_id": None,
            "recorded": [{"you": "Hi", "assist": "Hi"}]}
    assert problems(test, records.Test) == []  # Simulate → Propose, and the improvement copies it.
    item = {"id": "i", "title": "A change", "status": "awaiting_review", "changes": [], "test": test, "fixes": []}
    assert problems(item, records.Improvement) == []
    assert payload(item)["idea"] == "A change"  # Simulate → Write-up: the editor reads it.
    written = {"status": "failed"}
    assert problems(written, records.Copy) == [] and display_title({**item, "copy": written}) == "A change"
    decided = {"decision": "revise", "at": "2026-10-26T09:00:00+00:00"}
    assert problems(decided, records.Decision) == []
    rid = "abcdefgh0001"  # The household's Review → Propose: a revision, a new round for its conversation.
    flow = runtime_with_flow(hass, recorder=SimpleNamespace(index={rid: {"id": rid, "user_turns": 1}}),
                             improvements=SimpleNamespace(refresh=lambda: None)).flow  # Told it's being revised.
    flow.record(rid).update(stage="Your review", round=1,
                            rounds={"1": {"path": ["Your review"], "records": {}, "handoffs": []}})
    assert await flow.decided({**item, "conversation": rid}, decided["decision"], note="Say less") == [rid]
    [handoff] = flow.round_of(rid)["handoffs"]
    kind = records.HANDOFFS[handoff["contract"]]
    assert kind is records.SentBack and problems(handoff["record"], kind) == []
    assert set(handoff["record"]) == set(kind.__annotations__)  # Its contract's fields, no more.
    assert flow.round_of(rid)["records"]["Propose"]["revision"]["improvement"] == "i"  # What Propose reads with it.
    from custom_components.assist_improver.hypotheses import friction_by_goal
    from custom_components.assist_improver.simulate import reaches
    analysis = {"recording_id": "abcdefgh0001", "goals": []}
    assert problems(analysis, records.AnalysisResult) == []
    assert friction_by_goal(analysis) == {}  # Propose's window reads it so.
    idea = {"title": "A change", "goals": [{"session": "abcdefgh", "goal_turn": 1}], "changes": []}
    assert problems(idea, records.Hypothesis) == []
    assert reaches(idea, {})  # Simulate reads it so.


def test_a_contract_names_what_is_missing_or_undeclared():
    assert problems({"goals": []}, records.AnalysisResult, "The analysis") == ["The analysis.recording_id: missing"]
    assert problems({"title": "x", "goals": [{"session": "s"}], "changes": []}, records.Hypothesis, "An idea") == [
        "An idea.goals[0].goal_turn: missing"]
    with pytest.raises(ContractError, match="The test broke its contract: .*idea: missing"):
        contracts.check({"tested": {}}, records.Test, "The test")
    assert problems({"comments": [{"label": "issue"}]}, records.ReviewAnswer, "The review") == [
        "The review.comments[0].decorations: missing", "The review.comments[0].subject: missing"]
    assert problems({"title": "x", "goals": [], "changes": []}, records.Draft, "A draft") == [
        "A draft.friction_it_targets: missing", "A draft.sessions_where_it_would_apply: missing"]
    assert problems({"smooth": "yes", "sure": True}, records.TriageVerdict, "The verdict") == [
        "The verdict.smooth: str, not bool"]
    assert problems({"decision": "accept"}, records.Decision, "The decision") == ["The decision.at: missing"]
    # What's stored keeps earlier shapes: the fields a stage must hand on aren't asked of it.
    assert problems({"goals": []}, records.AnalysisResult, required=False) == []
