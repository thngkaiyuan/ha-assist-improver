"""The proposal: the hypothesis generator over the analysed sessions, with one long-running reviewer beside it, or the
reviewer alone on one hypothesis; recorded in the run log either way."""

from uuid import uuid4

from . import hypotheses, trace, triage
from .cases import CaseError
from .exploration import SessionAPI, SessionView
from .reflective import run_role
from .settings import role_model


async def propose(hass, entry, runtime, service, analyses, hypothesis=None, seeded=None, feedback=None,
                      settled=None, existing=None, allow_paid_inference=False):
    """The hypothesis generator over analysed sessions (with one long-running reviewer beside it), or the reviewer
    alone on one hypothesis. Paid AI Task calls with read-only tools, recorded either way; answers come from the
    roles' submit tools (last accepted, else best)."""
    if not allow_paid_inference:
        raise CaseError("Paid inference has not been authorized")
    generating = service == "generate_hypotheses"
    entity_id = role_model(entry, "generator" if generating else "reviewer")
    from .sessions import catalog
    recorder = runtime.recorder
    # Every other recording too, analysed or not: a rule is tested where it would apply, sessions without friction
    # included. Each comes as a brief; a full recording is loaded only when a tool or a check opens it.
    analyses, given = list(analyses), {a["recording_id"] for a in analyses}
    others = [rid for rid in sorted(set(recorder.index) - given) if recorder.index[rid].get("user_turns") != 0]
    briefs, household, assistant = await catalog(recorder, [a["recording_id"] for a in analyses] + others)
    readable = {b["recording_id"] for b in briefs}
    if household is None:
        raise CaseError("No recorded conversation can be read")
    analyses = [a for a in analyses if a["recording_id"] in readable] + [
        triage.unanalysed(rid) for rid in others if rid in readable]

    async def load(recording_id):
        return SessionView(await recorder.export(recording_id))
    sessions = hypotheses.Sessions(analyses, briefs=briefs, loader=load, household=household, assistant=assistant)
    sessions.feedback = list(feedback or [])  # The household's decisions on earlier improvements.
    sessions.settled = {tuple(k.split(":")[:1]) + (int(k.split(":")[1]),): v for k, v in (settled or {}).items()}
    sessions.existing = list(existing or [])  # Improvements waiting for review or applied: list_improvements.
    role = hypotheses.GENERATOR_ROLE if generating else hypotheses.REVIEWER_ROLE
    run = {"id": uuid4().hex, "kind": role, "status": "running", "entity_id": entity_id,
           "sessions": sorted(sessions.analyses), **trace.ids()}
    await runtime.role_runs.record(run)  # Persist the attempt BEFORE any inference.
    if generating:
        result, submit, ask, reviewer_run, seeded_reviews = await hypotheses.run_generator(
            hass, sessions, entity_id, [(s["name"], s["hypothesis"]) for s in seeded or []],
            reviewer_entity_id=role_model(entry, "reviewer"))
        run["reviews"], run["seeded_reviews"] = ask.reviews, seeded_reviews
        run["reviewer_run"] = {k: reviewer_run.get(k) for k in ("status", "error", "usage", "output")}
    else:
        submit = hypotheses.SubmitReview(sessions)
        result = await run_role(hass, role=role, entity_id=entity_id, instructions=hypotheses.REVIEWER_INSTRUCTIONS,
                                payload={"hypothesis": hypothesis, "household": sessions.summary()}, structure=None,
                                allow_paid_inference=True,
                                llm_api=SessionAPI(hass, tools=hypotheses.reviewer_tools(sessions, submit)))
    run.update(result, reply=result["output"], submissions=len(submit.submissions))
    run["answer_status"], answer, found = submit.best()
    if answer is None:
        if run["status"] == "completed":
            run.update(status="failed", error={"message": f"The {role} never called {submit.name}"})
    else:
        run.update(output=answer, problems=found)
    return await runtime.role_runs.record(run)
