"""One conversation's quick check and analysis: the roles' calls, recorded in the run log either way, with what the
pipeline reads from them (the analysis, a held verdict, why it failed)."""

from uuid import uuid4

from homeassistant.util.dt import utcnow

from . import analyzer, trace, triage
from . import contracts, records
from .cases import CaseError, digest as content_digest
from .exploration import ListImprovements, ReadImprovement, SessionAPI, SessionView, related_improvements
from .failures import closing
from .flow import deliberate
from .records import Analysis
from .reflective import index_of, run_role
from .settings import role_model


async def analyze(hass, entry, runtime, recording_id, triage_only=False, sent=None) -> Analysis:
    """Journey analyzer on one recording: one paid AI Task call with read-only session tools, recorded either way.

    The answer comes from the submit_goals tool, not the model's final text: the last accepted submission, or
    the best rejected one (with its problems) if none was accepted. triage_only: the quick check instead (with a
    triage model set); a session it's sure was smooth is recorded as one without friction, and one that needs
    analysis as waiting for it, with the verdict kept so the check isn't paid for twice. sent: how it was sent in
    (its round's mark, flow.Flow.sent_in), if it was.
    """
    entity_id = role_model(entry, "analyzer")
    recorder = runtime.recorder
    view = SessionView(await recorder.export(recording_id))
    run = {"id": uuid4().hex, "kind": analyzer.ROLE, "recording_id": recording_id, "status": "running",
           "digest_sha256": content_digest(view.digest), "entity_id": entity_id, "agentic": True, **trace.ids()}
    async with closing(runtime.role_runs.record, run) as keep:  # Kept before any inference; closed however it ends.
        if triage_only and (checker := role_model(entry, "triage")):
            kept = kept_verdict(runtime, recording_id, run["digest_sha256"])
            trace.tag(triage="kept" if kept else "checked")
            check = kept or await run_role(
                hass, role=triage.ROLE, entity_id=checker, instructions=triage.INSTRUCTIONS,
                payload=triage.payload(view), structure=triage.STRUCTURE, allow_paid_inference=True)
            run["triage"] = {**{k: check.get(k) for k in ("status", "output", "error", "usage", "entity_id")},
                             **({"kept": True} if kept else {})}  # A kept verdict's tokens were counted once already.
            if check["status"] == "completed":  # What Analyze, or the end, reads: the verdict's contract.
                contracts.check(check.get("output"), records.TriageVerdict, "The quick check's verdict")
            if check["status"] == "completed" and triage.skips_analysis(check.get("output")):
                # When it was judged.
                run.update(status="completed", output=check["output"], answer_status="triaged_smooth",
                           analysis=contracts.check(triage.smooth_analysis(recording_id, check["output"]),
                                                    records.AnalysisResult, "The quick check's analysis"),
                           finished=check.get("finished") or utcnow().isoformat())
                return await keep(run)
        if triage_only:  # Not judged smooth (or not checked): the session waits for analysis.
            run.update(status="held", answer_status="awaiting_analysis")
            return await keep(run)
        instructions, payload, tools, submit, mark = analyzer_input(runtime, recording_id, view, sent)
        result = await run_role(hass, role=analyzer.ROLE, entity_id=entity_id, instructions=instructions,
                                payload=payload, structure=None, llm_api=SessionAPI(hass, view, tools),
                                allow_paid_inference=True)
        run.update(result, reply=result["output"], submissions=len(submit.submissions))
        run["answer_status"], answer, found = submit.best()
        if answer is not None and run["status"] == "failed":  # The model erred after it answered: its answer stands
            run["status"] = "completed"  # (its error is kept with it).
        if answer is None:
            if run["status"] == "completed":
                run.update(status="failed", error={"message": "The AI model didn't use the tools this step needs. If "
                                                   "this keeps happening, choose a model that supports tool use, such "
                                                   "as Claude Opus 5.5."})
        else:
            run["output"] = answer
            if run["answer_status"] == "best_rejected":  # None accepted: only what passed the checks moves on.
                answer = analyzer.valid_part(answer, found)
            analysis = analyzer.to_analysis(recording_id, answer, view)
            # What the user asked to know, judged once on the reply that ends its run (DETAILS P9.7.4, P9.10).
            from .simulate import answered, judge_model
            facts, heard = await answered(hass, judge_model(entry), view, analysis["goals"])
            for n, goal in enumerate(analysis["goals"]):  # Each its own: two questions may share a turn.
                if n in facts:
                    goal.update(reached=facts[n]["gives"], answered=facts[n])
            if heard:
                run["fact_check"] = {k: heard.get(k) for k in ("status", "answers", "error", "usage")}
            run["analysis"] = contracts.check(  # What Propose and Simulate read: its contract, or it fails here.
                {**analysis, "problems": found}, records.AnalysisResult, "The analysis")
            if duplicates := mark.kept(answer):  # Covered by another conversation's improvement (P0.12).
                run["analysis"]["duplicates"] = duplicates
        return await keep(run)


def analyzer_input(runtime, recording_id, view, sent=None):
    """What the analyzer gets for one session, by code alone: (instructions, payload, its tools, the submit tool, the
    duplicate-marking tool). Existing improvements come along, so a goal one of them already fixes stops there
    (mark_duplicate) instead of going on. sent: how it was sent in (flow.Flow.sent_in), if it was. The preflight
    builds the same (tools/preflight.py)."""
    improvements = getattr(runtime, "improvements", None)
    existing = improvements.catalog(asking=recording_id, detailed=True) if improvements is not None else []
    # Sent in with a clear sign (flow.deliberate): the analyzer hears it, with the note if any. One picked, or sent in
    # without a note, that passed the quick check is analysed as any other (DETAILS P9.3.2).
    told = sent if deliberate(sent) else None
    mark = analyzer.MarkDuplicate(view, existing)
    # The note it was sent in with is its last turn: an answer that gives it no goal is rejected.
    submit = analyzer.SubmitGoals(view, *([mark.THEN] if existing else []), note=(told or {}).get("note"),
                                  existing=existing)
    related = related_improvements(existing, view.devices_touched())
    tools = [submit] + ([ListImprovements(view, existing), ReadImprovement(view, existing), mark] if existing else [])
    instructions = (analyzer.INSTRUCTIONS + (analyzer.FILED if told else "")
                    + (analyzer.DUPLICATES if existing else ""))
    # The instructions Assist ran with (who's speaking, the household knowledge), as the quick check gets them.
    from .triage import instructions_seen
    try:
        seen = instructions_seen(view)
    except (CaseError, KeyError, TypeError):  # A turn Home Assistant handled itself, or an older shape: left out.
        seen = {}
    payload = {**view.overview(), **seen, **({"filed_by_user": {"note": told.get("note", "")}} if told else {}),
               **({"improvements_on_these_devices": related} if related else {})}
    if (note := ((told or {}).get("note") or "").strip()) and isinstance(payload.get("conversation"), list):
        # The note it was sent in with is the conversation's last turn (SPEC.md P0.3), which needs its goal too; it
        # reads with read_turn as any turn does.
        view.note = note
        payload["conversation"] = [*payload["conversation"], {"turn": len(view.digest["turns"]) + 1,
                                                              "user_says": note, "sent_with_it": True}]
    return instructions, payload, tools, submit, mark


def kept_verdict(runtime, recording_id, digest_sha256):
    """The quick check's verdict on a session waiting for analysis (same content), if one was kept."""
    run = index_of(runtime.role_runs).held.get(recording_id)  # Its latest held record (reflective.RunIndex).
    return run["triage"] if run is not None and run.get("digest_sha256") == digest_sha256 else None
