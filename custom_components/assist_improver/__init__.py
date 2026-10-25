"""Recording, offline audits, and explicitly authorized simulated baselines and candidates."""

from uuid import uuid4

import probatio
from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import SupportsResponse, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er, issue_registry as ir
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.service import async_register_admin_service

from . import ai_setup, analyzer, household, hypotheses, panel, practice, settings, triage
from .const import CONF_ACCEPTED_WEAK, CONF_AI_TASK, CONF_SCHEDULE, DOMAIN
from .casebook import CaseBook
from .baseline import BaselineManager
from .candidates import CandidateManager
from .cases import CaseError, digest as content_digest
from .exploration import ListImprovements, ReadImprovement, SessionAPI, SessionView, related_improvements
from .improvements import Improvements
from .pipeline import Pipeline, schedule
from .recorder import Recorder
from .reflective import RoleRuns, run_role
from .settings import role_model

PLATFORMS = ["switch"]
SERVICES = ("status", "list_recordings", "export_recording", "list_cases", "get_case",
            "create_case", "case_feedback", "audit_case", "prepare_baseline", "start_baseline",
            "review_baseline_turn", "continue_baseline", "prepare_candidate", "run_candidate",
            "review_candidate", "apply_candidate", "analyze_recording", "generate_hypotheses", "review_hypothesis",
            "check_replay_fidelity", "test_hypotheses", "list_improvements", "quick_check", "write_summaries")


async def async_setup(hass, config):
    """Household knowledge and its LLM API, once per run: assistants using it never find it missing."""
    await household.async_setup(hass)
    return True


async def async_remove_entry(hass, entry):
    """Uninstalling: assistants on the household API go back to Assist, keeping its lines in their instructions."""
    await household.async_remove(hass)


async def async_setup_entry(hass, entry):
    await household.async_setup(hass)
    recorder = Recorder(hass)
    await recorder.start()
    recorder.casebook = CaseBook(hass, recorder)
    try:
        await recorder.casebook.start()
    except Exception:
        await recorder.stop()
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
    except Exception:
        await recorder.stop()
        raise
    # Providers may create the chosen AI Task after this entry loads: judge it once HA has started, then on changes.
    @callback
    def recheck(_event_or_hass):
        _update_setup_issue(hass, entry)

    entry.async_on_unload(async_at_started(hass, recheck))

    @callback
    def household_issues(_now_or_hass=None):
        household.update_issues(hass)  # Assistants come and go in their providers' settings: re-checked regularly.

    from datetime import timedelta
    from homeassistant.helpers.event import async_track_time_interval
    entry.async_on_unload(async_at_started(hass, household_issues))
    entry.async_on_unload(async_track_time_interval(hass, household_issues, timedelta(minutes=5)))
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

    @callback
    def remove_old_practice_assistant(_hass):
        hass.async_create_task(_remove_old_practice_assistant(hass, entry), "assist_improver_practice_cleanup")

    entry.async_on_unload(async_at_started(hass, remove_old_practice_assistant))

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

    async def list_cases(call):
        return {"cases": recorder.casebook.summaries()}

    async def case_action(call):
        try:
            if call.service == "get_case":
                return recorder.casebook.get(call.data["case_id"])
            if call.service == "create_case":
                return await recorder.casebook.create(**call.data)
            if call.service == "case_feedback":
                return await recorder.casebook.feedback(**call.data)
            if call.service == "prepare_baseline":
                return await recorder.baselines.prepare(**call.data)
            if call.service == "start_baseline":
                return await recorder.baselines.start(**call.data)
            if call.service == "review_baseline_turn":
                return await recorder.baselines.review(**call.data)
            if call.service == "continue_baseline":
                return await recorder.baselines.continue_trial(**call.data)
            if call.service == "prepare_candidate":
                return await recorder.candidates.prepare(**call.data)
            if call.service == "run_candidate":
                return await recorder.candidates.run(**call.data)
            if call.service == "review_candidate":
                return await recorder.candidates.review(**call.data)
            if call.service == "analyze_recording":
                return await _analyze(hass, entry, recorder, **call.data)
            if call.service in ("generate_hypotheses", "review_hypothesis"):
                return await _hypotheses(hass, entry, recorder, call.service, **call.data)
            if call.service == "apply_candidate":
                return await recorder.candidates.apply(call.data["plan_id"], accepted_by=call.context.user_id)
            return await recorder.casebook.audit(call.data["case_id"])
        except (CaseError, ValueError) as err:
            raise HomeAssistantError(str(err)) from err

    grounding = {probatio.Optional("destination"): str, probatio.Optional("criteria"): dict,
                 probatio.Optional("evidence"): dict}
    case_id = {probatio.Required("case_id"): str}
    paid = {probatio.Optional("allow_paid_inference", default=False): bool}
    expected_turn = {probatio.Required("expected_turn"): probatio.All(int, probatio.Range(min=1))}
    plan_id = {probatio.Required("plan_id"): str}
    evidence = probatio.All([{probatio.Required("case_id"): str, probatio.Required("rationale"): str,
        probatio.Required("role"): probatio.In(["friction", "related_success", "counterexample"])}],
        probatio.Length(min=1))

    for name, handler, schema in (
        ("status", status, probatio.Schema({})),
        ("list_recordings", list_recordings, probatio.Schema({})),
        ("export_recording", export, probatio.Schema({probatio.Required("recording_id"): str})),
        ("list_cases", list_cases, probatio.Schema({})),
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
        ("get_case", case_action, probatio.Schema(case_id)),
        ("create_case", case_action, probatio.Schema({
            probatio.Required("recording_id"): str, probatio.Required("title"): str,
            probatio.Required("first_turn"): probatio.All(int, probatio.Range(min=1)),
            probatio.Required("last_turn"): probatio.All(int, probatio.Range(min=1)),
            probatio.Optional("intervention_turns"): [int], **grounding})),
        ("case_feedback", case_action, probatio.Schema({**case_id, **grounding,
            probatio.Required("expected_revision"): int, probatio.Required("feedback"): str,
            probatio.Optional("outcome", default="revision_requested"): probatio.In(
                ["revision_requested", "destination_confirmed", "fine_as_is"])})),
        ("audit_case", case_action, probatio.Schema(case_id)),
        ("prepare_baseline", case_action, probatio.Schema(case_id)),
        ("start_baseline", case_action, probatio.Schema({**case_id, **paid,
            probatio.Required("plan_id"): str})),
        ("review_baseline_turn", case_action, probatio.Schema({**case_id, **expected_turn,
            probatio.Required("reproduced"): bool, probatio.Required("feedback"): str})),
        ("continue_baseline", case_action, probatio.Schema({**case_id, **paid, **expected_turn})),
        ("prepare_candidate", case_action, probatio.Schema({
            probatio.Required("title"): str, probatio.Required("friction"): str,
            probatio.Required("evidence"): evidence, probatio.Required("evaluation_case_id"): str,
            probatio.Required("hypothesis"): str,
            probatio.Required("scripts"): probatio.All([str], probatio.Length(min=1))})),
        ("run_candidate", case_action, probatio.Schema({**plan_id, **paid})),
        ("review_candidate", case_action, probatio.Schema({**plan_id,
            probatio.Required("destination_reached"): bool, probatio.Required("no_regressions_expected"): bool,
            probatio.Required("feedback"): str})),
        ("apply_candidate", case_action, probatio.Schema(plan_id)),
        ("analyze_recording", case_action, probatio.Schema({
            probatio.Required("recording_id"): str, probatio.Optional("full", default=False): bool, **paid})),
        ("generate_hypotheses", case_action, probatio.Schema({
            probatio.Required("analyses"): probatio.All([dict], probatio.Length(min=1)),
            probatio.Optional("seeded"): [{probatio.Required("name"): str, probatio.Required("hypothesis"): dict}],
            **paid})),
        ("review_hypothesis", case_action, probatio.Schema({
            probatio.Required("hypothesis"): dict,
            probatio.Required("analyses"): probatio.All([dict], probatio.Length(min=1)), **paid})),
    ):
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
    steps = [settings.TITLES[role] for role, issue in settings.problems(hass, entry).items()
             if issue != "none" and role_model(entry, role) != chosen]  # The main model has the notice above.
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
           "digest_sha256": content_digest(view.digest), "entity_id": entity_id, "agentic": True}
    await recorder.role_runs.record(run)  # Persist the attempt BEFORE any inference.
    if (checker := role_model(entry, "triage")) and not full:
        check = kept_verdict(recorder, recording_id, run["digest_sha256"]) or await run_role(
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
    result = await run_role(hass, role=analyzer.ROLE, entity_id=entity_id,
                            instructions=analyzer.INSTRUCTIONS + (analyzer.DUPLICATES if existing else ""),
                            payload={**view.overview(),
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
    for run in reversed(getattr(recorder.role_runs, "runs", [])):
        if (run.get("kind") == analyzer.ROLE and run.get("recording_id") == recording_id
                and run.get("status") == "held" and (run.get("triage") or {}).get("status") == "completed"):
            return run["triage"] if run.get("digest_sha256") == digest_sha256 else None
    return None


async def _hypotheses(hass, entry, recorder, service, analyses, hypothesis=None, seeded=None, feedback=None,
                      settled=None, existing=None, allow_paid_inference=False):
    """The hypothesis generator over analysed sessions (with one long-running reviewer beside it), or the reviewer
    alone on one hypothesis. Paid AI Task calls with read-only tools, recorded either way; answers come from the
    roles' submit tools (last accepted, else best)."""
    if not allow_paid_inference:
        raise CaseError("Paid inference has not been authorized")
    generating = service == "generate_hypotheses"
    entity_id = role_model(entry, "generator" if generating else "reviewer")
    views = [SessionView(await recorder.export(a["recording_id"])) for a in analyses]
    # Every other recording too, analysed or not: rules are checked against all of them (regressions, and
    # whether a rule generalises), not only the sessions with friction.
    analyses, given = list(analyses), {a["recording_id"] for a in analyses}
    for recording_id in sorted(set(recorder.index) - given):
        try:
            views.append(SessionView(await recorder.export(recording_id)))
        except (CaseError, ValueError):
            continue  # Still being recorded, or not a conversation the tools can read.
        analyses.append(triage.unanalysed(recording_id))
    sessions = hypotheses.Sessions(analyses, views)
    sessions.feedback = list(feedback or [])  # The household's decisions on earlier improvements.
    sessions.settled = {tuple(k.split(":")[:1]) + (int(k.split(":")[1]),): v for k, v in (settled or {}).items()}
    sessions.existing = list(existing or [])  # Improvements waiting for review or applied: list_improvements.
    role = hypotheses.GENERATOR_ROLE if generating else hypotheses.REVIEWER_ROLE
    run = {"id": uuid4().hex, "kind": role, "status": "running", "entity_id": entity_id,
           "sessions": sorted(sessions.analyses)}
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


def _pipeline_store(hass):
    from homeassistant.components.assist_pipeline.pipeline import KEY_ASSIST_PIPELINE
    pipelines = hass.data.get(KEY_ASSIST_PIPELINE)
    return pipelines.pipeline_store if pipelines else None


async def _remove_old_practice_assistant(hass, entry):
    """0.6.0 added a "Practice (simulated home)" Assist assistant; practice mode replaced it in 0.6.2."""
    registry = er.async_get(hass)
    agent = registry.async_get_entity_id("conversation", DOMAIN, f"{entry.entry_id}-practice")
    if agent is None:
        return
    if (store := _pipeline_store(hass)) is None:
        return
    for pipeline in list(store.async_items()):
        if pipeline.conversation_engine == agent:
            await store.async_delete_item(pipeline.id)
    registry.async_remove(agent)


async def async_unload_entry(hass, entry):
    await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if getattr(entry.runtime_data, "panel", False):
        panel.remove_panel(hass)
    practice.uninstall()
    hass.data.pop(practice.MODE_KEY, None)
    await entry.runtime_data.stop()
    from .sandbox import stop_all
    await stop_all()  # No replay sandbox outlives the improver.
    for name in SERVICES:
        hass.services.async_remove(DOMAIN, name)
    hass.data.pop(DOMAIN, None)
    return True
