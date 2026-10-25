"""Recording, offline audits, and explicitly authorized simulated baselines and candidates."""

from uuid import uuid4

import probatio
from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import SupportsResponse, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv, issue_registry as ir
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.service import async_register_admin_service

from . import ai_setup, analyzer, household, hypotheses, panel, practice, retention, settings, trace, triage
from .const import CONF_ACCEPTED_WEAK, CONF_AI_TASK, CONF_DEVELOPER, CONF_SCHEDULE, DOMAIN
from .casebook import CaseBook
from .baseline import BaselineManager
from .candidates import CandidateManager
from .cases import CaseError, digest as content_digest
from .exploration import ListImprovements, ReadImprovement, SessionAPI, SessionView, related_improvements
from .improvements import Improvements
from .pipeline import Pipeline, schedule
from .recorder import Recorder
from .reflective import RoleRuns, index_of, run_role
from .settings import role_model

PLATFORMS = ["switch"]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
SERVICES = ("status", "export_recording")  # For bug reports: what the improver holds, and one recording.
# Developer mode only (a hidden setting): run one AI role on chosen inputs, for debugging and evaluation rounds.
# They skip the panel's run guard and confirmation, so households never get them.
DEVELOPER_SERVICES = ("list_recordings", "list_improvements", "analyze_recording", "quick_check",
                      "generate_hypotheses", "review_hypothesis", "test_hypotheses", "write_summaries",
                      "check_replay_fidelity")


async def async_setup(hass, config):
    """Household knowledge and its LLM API, once per run: assistants using it never find it missing."""
    await household.async_setup(hass)
    return True


async def async_migrate_entry(hass, entry):
    """What earlier versions left behind, upgraded once (migrations.py)."""
    from .migrations import async_migrate
    return await async_migrate(hass, entry)


async def async_remove_entry(hass, entry):
    """Uninstalling leaves only the applied improvements behind: assistants on the household API go back to Assist
    with its lines written into their own instructions, and the AI Tasks the improver set up and everything it
    stored (recordings, run log, traces, improvements' records) are removed."""
    await household.async_remove(hass)
    await ai_setup.remove_created(hass)
    await hass.async_add_executor_job(_remove_stores, hass.config.path(".storage"))


async def tidy(hass, recorder):
    """Retention (retention.py), daily: what an improvement or a case uses, and a conversation still waiting for
    analysis, stay in full. Traced in today's trace."""
    with trace.today():
        async with trace.span("tidy"):
            referenced = {c.get("recording_id") for c in recorder.casebook.cases.values()}
            for item in recorder.improvements.items.values():
                referenced |= {g.split(":")[0] for g in recorder.improvements._covers(item)}
            referenced |= set(recorder.pipeline.to_analyze()) | set(recorder.pipeline._at())
            named = {rid: {e for g in analysis.get("goals", []) for e in [g.get("entity_id"),
                                                                         *(g.get("entity_ids") or [])] if e}
                     for rid, analysis in recorder.pipeline._analyzed().items()}
            recorder.retention = await retention.prune(recorder, referenced, named=named)
            recorder.retention["runs"] = await recorder.role_runs.prune(set(recorder.index))
            recorder.retention["traces"] = await retention.prune_traces(hass, trace.active_ids())
        trace.journal("info", "Tidy-up done", **recorder.retention)


def _remove_stores(path):
    import os
    for name in os.listdir(path) if os.path.isdir(path) else []:
        if name.startswith(f"{DOMAIN}."):
            os.remove(os.path.join(path, name))


async def async_setup_entry(hass, entry):
    await household.async_setup(hass)
    await trace.async_setup(hass, entry)  # First, so the recorder's start is in today's trace.
    recorder = Recorder(hass)
    try:
        await recorder.start()
    except Exception:
        await trace.async_unload()
        raise
    recorder.casebook = CaseBook(hass, recorder)
    try:
        await recorder.casebook.start()
    except Exception:
        await recorder.stop()
        await trace.async_unload()
        raise
    recorder.baselines = BaselineManager(hass, recorder.casebook)
    recorder.candidates = CandidateManager(hass, recorder.casebook)
    recorder.role_runs = RoleRuns(hass)
    recorder.improvements = Improvements(hass, recorder)
    try:
        await recorder.candidates.start()
        await recorder.role_runs.start()
        await recorder.improvements.start()  # Recovers a change HA stopped in the middle of.
        # The review panel, with how many improvements wait for review in its sidebar title.
        recorder.panel = await panel.async_setup_panel(hass, len(recorder.improvements.pending()))
        recorder.pipeline = Pipeline(hass, entry, recorder)  # Run now, and the schedule (off by default).
        recorder.pipeline.current = next((dict(r) for r in reversed(recorder.role_runs.runs)
                                          if r.get("kind") == "pipeline"), None)
        recorder.on_filed_closed = recorder.pipeline.request  # On-demand: a filed conversation runs once it ends.
    except Exception:
        await recorder.stop()
        await trace.async_unload()
        raise
    # Providers may create the chosen AI Task after this entry loads: judge it once HA has started, then on changes.
    @callback
    def recheck(_event_or_hass):
        _update_setup_issue(hass, entry)

    entry.async_on_unload(async_at_started(hass, recheck))

    @callback
    def tidy_soon(_now_or_hass=None):  # After startup, then every night at 04:30.
        hass.async_create_background_task(tidy(hass, recorder), "assist_improver_retention")

    from homeassistant.helpers.event import async_track_time_change
    entry.async_on_unload(async_at_started(hass, tidy_soon))
    entry.async_on_unload(async_track_time_change(hass, tidy_soon, hour=4, minute=30, second=0))

    @callback
    def recover(_hass):  # A change HA stopped in the middle of is undone once HA (and its scripts) has started.
        hass.async_create_task(recorder.improvements.recover(), "assist_improver_recover")

    entry.async_on_unload(async_at_started(hass, recover))

    @callback
    def filed_waiting(_hass):  # On-demand: conversations filed before a restart run once HA has started.
        if recorder.pipeline.on_demand and recorder.pipeline.to_analyze():
            recorder.pipeline.request()

    entry.async_on_unload(async_at_started(hass, filed_waiting))

    entry.async_on_unload(hass.bus.async_listen(EVENT_STATE_CHANGED, recheck, event_filter=_availability_changed))
    entry.runtime_data = recorder
    hass.data[DOMAIN] = recorder
    scheduled = {"options": dict(entry.options), "off": None}

    def reschedule():
        if scheduled["off"]:
            scheduled["off"]()
        scheduled["off"] = schedule(hass, entry, recorder.pipeline, entry.options.get(CONF_SCHEDULE, "off"))

    async def options_updated(hass, entry):
        """The settings the panel changes apply live (a run in progress keeps going); others reload."""
        before, scheduled["options"] = scheduled["options"], dict(entry.options)
        changed = {k for k in before.keys() | entry.options.keys() if before.get(k) != entry.options.get(k)}
        trace.journal("info", f"Settings changed: {', '.join(sorted(changed)) or 'none'}",
                      changed={k: entry.options.get(k) for k in sorted(changed)})
        if changed <= settings.LIVE:
            if CONF_SCHEDULE in changed:
                reschedule()
            _update_setup_issue(hass, entry)
            return
        await hass.config_entries.async_reload(entry.entry_id)

    reschedule()
    entry.async_on_unload(lambda: scheduled["off"] and scheduled["off"]())
    entry.async_on_unload(entry.add_update_listener(options_updated))
    hass.data[practice.MODE_KEY] = mode = practice.PracticeMode(hass, recorder)
    await mode.async_load()
    practice.install()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    async def status(call):
        return recorder.status()

    async def list_recordings(call):
        return {"recordings": list(recorder.index.values())}

    async def export(call):
        try:
            return await recorder.export(call.data["recording_id"])
        except ValueError as err:
            raise HomeAssistantError(str(err)) from err

    async def check_replay_fidelity(call):
        from .replay import check_fidelity
        try:
            return await check_fidelity(hass, await recorder.export(call.data["recording_id"]))
        except (CaseError, ValueError) as err:
            raise HomeAssistantError(str(err)) from err

    async def test_hypotheses(call):
        from .validation import test_hypotheses as run
        try:
            report = await run(hass, entry, recorder, **call.data)
        except (CaseError, ValueError) as err:
            raise HomeAssistantError(str(err)) from err
        await recorder.improvements.refresh()  # Improvements the tests showed go up for review.
        return report

    async def quick_check(call):
        """The quick check alone on some recordings, with a given model (for calibrating it); nothing else runs."""
        if not call.data.get("allow_paid_inference"):
            raise HomeAssistantError("Paid inference has not been authorized")
        checker = call.data.get("entity_id") or role_model(entry, "triage")
        answers = {}
        for recording_id in call.data["recording_ids"]:
            try:
                view = SessionView(await recorder.export(recording_id))
            except (CaseError, ValueError) as err:
                answers[recording_id] = {"status": "unreadable", "error": str(err)}
                continue
            check = await run_role(hass, role=triage.ROLE, entity_id=checker, instructions=triage.INSTRUCTIONS,
                                   payload=triage.payload(view), structure=triage.STRUCTURE, allow_paid_inference=True)
            answers[recording_id] = {k: check.get(k) for k in ("status", "output", "error", "usage")}
        report = {"id": uuid4().hex, "kind": "quick_check", "status": "completed", "entity_id": checker,
                  "output": answers}
        await recorder.role_runs.record(report)  # Kept, so a lost page or connection loses nothing.
        return report

    async def write_summaries(call):
        """The editor writes the headline and what-you-get for improvements (those without, or the ones given)."""
        if not call.data.get("allow_paid_inference"):
            raise HomeAssistantError("Paid inference has not been authorized")
        from .pipeline import editor_model
        ids = set(call.data["improvement_ids"]) if call.data.get("improvement_ids") else None
        return {"copy": await recorder.improvements.write_copy(call.data.get("entity_id") or editor_model(entry), ids)}

    async def list_improvements(call):
        return {"improvements": recorder.improvements.listed()}


    async def developer_action(call):
        try:
            if call.service == "analyze_recording":
                return await _analyze(hass, entry, recorder, **call.data)
            return await _hypotheses(hass, entry, recorder, call.service, **call.data)
        except (CaseError, ValueError) as err:
            raise HomeAssistantError(str(err)) from err

    paid = {probatio.Optional("allow_paid_inference", default=False): bool}

    developer = bool(entry.options.get(CONF_DEVELOPER))
    for name, handler, schema in (
        ("status", status, probatio.Schema({})),
        ("export_recording", export, probatio.Schema({probatio.Required("recording_id"): str})),
        ("list_recordings", list_recordings, probatio.Schema({})),
        ("list_improvements", list_improvements, probatio.Schema({})),
        ("write_summaries", write_summaries, probatio.Schema({
            probatio.Optional("improvement_ids"): [str], probatio.Optional("entity_id"): str, **paid})),
        ("quick_check", quick_check, probatio.Schema({
            probatio.Required("recording_ids"): probatio.All([str], probatio.Length(min=1)),
            probatio.Optional("entity_id"): str, **paid})),
        ("check_replay_fidelity", check_replay_fidelity, probatio.Schema({probatio.Required("recording_id"): str})),
        ("test_hypotheses", test_hypotheses, probatio.Schema({
            probatio.Required("hypotheses"): probatio.All([dict], probatio.Length(min=1)),
            probatio.Required("analyses"): probatio.All([dict], probatio.Length(min=1)),
            probatio.Optional("per_goal"): probatio.All(int, probatio.Range(min=1, max=20)), **paid})),
        ("analyze_recording", developer_action, probatio.Schema({
            probatio.Required("recording_id"): str, probatio.Optional("full", default=False): bool, **paid})),
        ("generate_hypotheses", developer_action, probatio.Schema({
            probatio.Required("analyses"): probatio.All([dict], probatio.Length(min=1)),
            probatio.Optional("seeded"): [{probatio.Required("name"): str, probatio.Required("hypothesis"): dict}],
            **paid})),
        ("review_hypothesis", developer_action, probatio.Schema({
            probatio.Required("hypothesis"): dict,
            probatio.Required("analyses"): probatio.All([dict], probatio.Length(min=1)), **paid})),
    ):
        if name in SERVICES or developer:
            async_register_admin_service(hass, DOMAIN, name, handler, schema,
                                         supports_response=SupportsResponse.ONLY)
    return True


@callback
def _availability_changed(event_data):
    """An AI Task appeared, went away, or became available or unavailable."""
    old, new = event_data.get("old_state"), event_data.get("new_state")
    return event_data["entity_id"].startswith("ai_task.") and (
        old is None or new is None or (old.state == "unavailable") != (new.state == "unavailable"))


def _update_setup_issue(hass, entry):
    """Repairs notices: one while no suitable main AI Task is chosen (fixed in place, repairs.py), and one naming the
    steps whose own model is missing or unavailable (chosen again in the panel)."""
    chosen = entry.options.get(CONF_AI_TASK)
    accepted = chosen and hass.states.get(chosen) is not None and entry.options.get(CONF_ACCEPTED_WEAK) == chosen
    if accepted or ai_setup.assess(hass, chosen)["ok"]:
        ir.async_delete_issue(hass, DOMAIN, "ai_task_setup")
    else:
        ir.async_create_issue(hass, DOMAIN, "ai_task_setup", is_fixable=True, severity=ir.IssueSeverity.WARNING,
                              translation_key="ai_task_setup", data={"entry_id": entry.entry_id})
    # A missing or unsuitable main model has the notice above; one that's there but unavailable is named here.
    steps = [settings.TITLES[role] for role, issue in settings.problems(hass, entry).items()
             if issue != "none" and (role_model(entry, role) != chosen or issue == "unavailable")]
    if not steps:
        ir.async_delete_issue(hass, DOMAIN, "step_models")
        return
    ir.async_create_issue(hass, DOMAIN, "step_models", is_fixable=False, severity=ir.IssueSeverity.WARNING,
                          translation_key="step_models", translation_placeholders={
                              "steps": settings.listed(steps),
                              "link": f"/{panel.URL_PATH}?view=settings"})


async def _analyze(hass, entry, recorder, recording_id, allow_paid_inference=False, full=False, triage_only=False):
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
    view = SessionView(await recorder.export(recording_id))
    run = {"id": uuid4().hex, "kind": analyzer.ROLE, "recording_id": recording_id, "status": "running",
           "digest_sha256": content_digest(view.digest), "entity_id": entity_id, "agentic": True, **trace.ids()}
    await recorder.role_runs.record(run)  # Persist the attempt BEFORE any inference.
    if (checker := role_model(entry, "triage")) and not full:
        kept = kept_verdict(recorder, recording_id, run["digest_sha256"])
        trace.tag(triage="kept" if kept else "checked")
        check = kept or await run_role(
            hass, role=triage.ROLE, entity_id=checker, instructions=triage.INSTRUCTIONS, payload=triage.payload(view),
            structure=triage.STRUCTURE, allow_paid_inference=True)
        run["triage"] = {k: check.get(k) for k in ("status", "output", "error", "usage", "entity_id")}
        if check["status"] == "completed" and triage.skips_analysis(check.get("output")):
            run.update(status="completed", output=check["output"], answer_status="triaged_smooth",
                       analysis=triage.smooth_analysis(recording_id, check["output"]))
            return await recorder.role_runs.record(run)
        if triage_only and check["status"] == "completed":
            run.update(status="held", answer_status="awaiting_analysis")
            return await recorder.role_runs.record(run)
    if triage_only:  # Nothing for the quick check to do here; the session waits for analysis.
        run.update(status="held", answer_status="awaiting_analysis")
        return await recorder.role_runs.record(run)
    # Existing improvements, so a goal one of them already fixes stops here (mark_duplicate) instead of going on.
    improvements = getattr(recorder, "improvements", None)
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
    return await recorder.role_runs.record(run)


def kept_verdict(recorder, recording_id, digest_sha256):
    """The quick check's verdict on a session waiting for analysis (same content), if one was kept."""
    run = index_of(recorder.role_runs).held.get(recording_id)  # Its latest held record (reflective.RunIndex).
    return run["triage"] if run is not None and run.get("digest_sha256") == digest_sha256 else None


async def _hypotheses(hass, entry, recorder, service, analyses, hypothesis=None, seeded=None, feedback=None,
                      settled=None, existing=None, allow_paid_inference=False):
    """The hypothesis generator over analysed sessions (with one long-running reviewer beside it), or the reviewer
    alone on one hypothesis. Paid AI Task calls with read-only tools, recorded either way; answers come from the
    roles' submit tools (last accepted, else best)."""
    if not allow_paid_inference:
        raise CaseError("Paid inference has not been authorized")
    generating = service == "generate_hypotheses"
    entity_id = role_model(entry, "generator" if generating else "reviewer")
    from .sessions import catalog
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
    await recorder.role_runs.record(run)  # Persist the attempt BEFORE any inference.
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
    return await recorder.role_runs.record(run)


async def async_unload_entry(hass, entry):
    await entry.runtime_data.pipeline.stop()  # A run in progress stops first, then the stores close.
    await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if getattr(entry.runtime_data, "panel", False):
        panel.remove_panel(hass)
    practice.uninstall()
    hass.data.pop(practice.MODE_KEY, None)
    await entry.runtime_data.stop()
    from .sandbox import stop_all
    await stop_all()  # No replay sandbox outlives the improver.
    for name in SERVICES + DEVELOPER_SERVICES:
        if hass.services.has_service(DOMAIN, name):
            hass.services.async_remove(DOMAIN, name)
    hass.data.pop(DOMAIN, None)
    await trace.async_unload()  # Last: what stopping wrote is in today's trace.
    return True
