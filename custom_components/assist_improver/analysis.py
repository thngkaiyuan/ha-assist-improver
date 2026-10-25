"""One conversation's quick check and analysis: the roles' calls, recorded in the run log either way, with what the
pipeline reads from them (the analysis, a held verdict, why it failed)."""

from uuid import uuid4

from . import analyzer, hypotheses, trace, triage
from .cases import CaseError, digest as content_digest
from .exploration import ListImprovements, ReadImprovement, SessionAPI, SessionView, related_improvements
from .records import Analysis
from .reflective import index_of, run_role
from .settings import role_model


async def analyze(hass, entry, runtime, recording_id, allow_paid_inference=False, full=False,
                  triage_only=False) -> Analysis:
    """Journey analyzer on one recording: one paid AI Task call with read-only session tools, recorded either way.

    The answer comes from the submit_goals tool, not the model's final text: the last accepted submission, or
    the best rejected one (with its problems) if none was accepted. With a triage model set (and not full), a
    session gets a quick check first; one it's sure was smooth is recorded as one without friction.
    triage_only (analysis paused): the quick check runs alone, and a session that needs analysis is recorded as
    waiting for it, with the verdict kept so the check isn't paid for twice.
    """
    if not allow_paid_inference:
        raise CaseError("Paid inference has not been authorized")
    entity_id = role_model(entry, "analyzer")
    recorder = runtime.recorder
    view = SessionView(await recorder.export(recording_id))
    run = {"id": uuid4().hex, "kind": analyzer.ROLE, "recording_id": recording_id, "status": "running",
           "digest_sha256": content_digest(view.digest), "entity_id": entity_id, "agentic": True, **trace.ids()}
    await runtime.role_runs.record(run)  # Persist the attempt BEFORE any inference.
    if (checker := role_model(entry, "triage")) and not full:
        kept = kept_verdict(runtime, recording_id, run["digest_sha256"])
        trace.tag(triage="kept" if kept else "checked")
        check = kept or await run_role(
            hass, role=triage.ROLE, entity_id=checker, instructions=triage.INSTRUCTIONS, payload=triage.payload(view),
            structure=triage.STRUCTURE, allow_paid_inference=True)
        run["triage"] = {**{k: check.get(k) for k in ("status", "output", "error", "usage", "entity_id")},
                         **({"kept": True} if kept else {})}  # A kept verdict's tokens were counted once already.
        if check["status"] == "completed" and triage.skips_analysis(check.get("output")):
            run.update(status="completed", output=check["output"], answer_status="triaged_smooth",
                       analysis=triage.smooth_analysis(recording_id, check["output"]))
            return await runtime.role_runs.record(run)
        if triage_only and check["status"] == "completed":
            run.update(status="held", answer_status="awaiting_analysis")
            return await runtime.role_runs.record(run)
    if triage_only:  # Nothing for the quick check to do here; the session waits for analysis.
        run.update(status="held", answer_status="awaiting_analysis")
        return await runtime.role_runs.record(run)
    # Existing improvements, so a goal one of them already fixes stops here (mark_duplicate) instead of going on.
    improvements = getattr(runtime, "improvements", None)
    existing = improvements.catalog(detailed=True) if improvements is not None else []
    mark = analyzer.MarkDuplicate(view, existing)
    submit = analyzer.SubmitGoals(view, mark.THEN) if existing else analyzer.SubmitGoals(view)
    related = related_improvements(existing, view.devices_touched())
    tools = [submit] + ([ListImprovements(view, existing), ReadImprovement(view, existing), mark] if existing else [])
    filed = (recorder.index.get(recording_id) or {}).get("filed")  # On-demand: the user filed it, maybe with a note.
    result = await run_role(hass, role=analyzer.ROLE, entity_id=entity_id,
                            instructions=analyzer.INSTRUCTIONS + (analyzer.FILED if filed else "")
                            + (analyzer.DUPLICATES if existing else ""),
                            payload={**view.overview(),
                                     **({"filed_by_user": {"note": filed.get("note", "")}} if filed else {}),
                                     **({"improvements_on_these_devices": related} if related else {})},
                            structure=None, llm_api=SessionAPI(hass, view, tools), allow_paid_inference=True)
    run.update(result, reply=result["output"], submissions=len(submit.submissions))
    run["answer_status"], answer, found = submit.best()
    if answer is None:
        if run["status"] == "completed":
            run.update(status="failed", error={"message": "The AI model didn't use the tools this step needs. If this "
                                               "keeps happening, choose a model that supports tool use, such as "
                                               "Claude Opus 5.5."})
    else:
        run["output"] = answer
        run["analysis"] = {**analyzer.to_analysis(recording_id, answer, view), "problems": found}
        if duplicates := mark.kept(answer):  # Kept with the improvement, so these goals go no further.
            run["analysis"]["duplicates"] = duplicates
            await improvements.note_addressed([{"session": hypotheses.short(recording_id), **d} for d in duplicates])
    return await runtime.role_runs.record(run)


def kept_verdict(runtime, recording_id, digest_sha256):
    """The quick check's verdict on a session waiting for analysis (same content), if one was kept."""
    run = index_of(runtime.role_runs).held.get(recording_id)  # Its latest held record (reflective.RunIndex).
    return run["triage"] if run is not None and run.get("digest_sha256") == digest_sha256 else None
