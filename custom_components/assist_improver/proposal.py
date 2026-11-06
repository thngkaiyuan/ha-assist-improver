"""The proposal: the hypothesis generator over the analysed sessions, with one long-running reviewer beside it; recorded
in the run log however it ends."""

from uuid import uuid4

from . import hypotheses, trace, triage
from .cases import CaseError
from .exploration import session_view
from .failures import closing
from .records import Proposal
from .sessions import catalog
from .settings import role_model
from .snapshot import at_moment, on_today


def as_today(recording, home):
    """A recording with each turn's home as today's at that turn's moment (snapshot.on_today, at_moment): the household
    as the proposer, its reviewer and code's checks look its devices, names, aliases, what Assist can see and the
    household knowledge up (DETAILS P6.9), on the setup replays run on."""
    recording = on_today(recording, home)
    return {**recording, "turns": [{**t, "source": at_moment(home, t["source"])} if t.get("source") else t
                                   for t in recording.get("turns") or []]}


async def window(runtime, analyses, *, feedback=None, sent_back=None, so_far=None, answered=None, existing=None,
                 today=None):
    """The sessions a proposal works over, by code alone (hypotheses.Sessions): the analyses given that can be read,
    and every other recording too, analysed or not, as a rule is tested where it would apply, sessions without
    friction included. Each comes as a brief; a full recording is loaded only when a tool or a check opens it. With
    the household's decisions on earlier improvements (feedback), what the household sent back for this round (a
    revision or a conflict, with the improvement in full: sent_back), the round's tests and reviews so far for a
    proposer picking it up (so_far: stamps.so_far), the household's answers to the proposer's questions this round
    (answered: [{question, answer}]), and the other conversations' improvements waiting for review or applied
    (existing). The household is today's setup (today: replay.setup_now's, read now unless given) at its newest
    conversation's moment. The preflight loads the same (tools/preflight.py), on a setup of its own."""
    from .replay import setup_now
    recorder = runtime.recorder
    analyses, given = list(analyses), {a["recording_id"] for a in analyses}
    others = [rid for rid in sorted(set(recorder.index) - given)
              if recorder.index[rid].get("user_turns") != 0 and not recorder.index[rid].get("deleting")]
    briefs, household, assistant = await catalog(recorder, [a["recording_id"] for a in analyses] + others)
    readable = {b["recording_id"] for b in briefs}
    if household is None:
        raise CaseError("No recorded conversation can be read")
    analyses = [a for a in analyses if a["recording_id"] in readable] + [
        triage.unanalysed(rid) for rid in others if rid in readable]

    async def load(recording_id):
        return await session_view(recorder.hass, await recorder.export(recording_id))
    today = today if today is not None else await setup_now(recorder.hass)
    household = await session_view(recorder.hass, as_today(household.recording, today["home"]))
    sessions = hypotheses.Sessions(analyses, briefs=briefs, loader=load, household=household, assistant=assistant)
    sessions.today = today
    sessions.feedback, sessions.sent_back, sessions.so_far = list(feedback or []), sent_back, dict(so_far or {})
    sessions.answered = list(answered or [])
    sessions.existing = list(existing or [])  # For list_improvements.
    await sessions.load_assistants(recorder.hass)  # Each assistant's instructions now, beside those it ran with.
    return sessions


async def propose(hass, entry, runtime, analyses, *, tester, feedback=None, sent_back=None, so_far=None,
                  answered=None, existing=None) -> Proposal:
    """The hypothesis generator over analysed sessions, with one long-running reviewer beside it: paid AI Task calls
    with read-only tools, recorded however they end. The answer comes from the generator's submit tool (last accepted,
    else best). tester: as hypotheses.run_generator takes it."""
    entity_id = role_model(entry, "generator")
    sessions = await window(runtime, analyses, feedback=feedback, sent_back=sent_back, so_far=so_far,
                            answered=answered, existing=existing)
    role = hypotheses.GENERATOR_ROLE
    run = {"id": uuid4().hex, "kind": role, "status": "running", "entity_id": entity_id,
           "sessions": sorted(sessions.analyses), **trace.ids()}
    async with closing(runtime.role_runs.record, run) as keep:  # Kept before any inference; closed however it ends.
        result, submit, ask, reviewer_run = await hypotheses.run_generator(
            hass, sessions, entity_id, reviewer_entity_id=role_model(entry, "reviewer"), tester=tester)
        run["reviews"] = ask.reviews
        run["reviewer_run"] = {k: reviewer_run.get(k) for k in ("status", "error", "usage", "output")}
        run.update(result, reply=result["output"], submissions=len(submit.submissions))
        run["answer_status"], answer, found = submit.best()
        if answer is not None and run["status"] == "failed":  # The model erred after it answered: its answer stands
            run["status"] = "completed"  # (its error is kept with it), as an analysis's does.
        if answer is None:
            if run["status"] == "completed":
                run.update(status="failed", error={"message": f"The {role} never called {submit.name}"})
        else:
            run.update(output=answer, problems=found)
        return await keep(run)
