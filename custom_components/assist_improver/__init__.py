"""Assist Improver: setup and unload, the household services, and the developer services (hidden unless on)."""

from uuid import uuid4

import probatio
from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import SupportsResponse, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv, issue_registry as ir
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.service import async_register_admin_service

from . import ai_setup, analysis, household, panel, practice, proposal, retention, settings, trace, triage
from .const import CONF_ACCEPTED_WEAK, CONF_AI_TASK, CONF_DEVELOPER, CONF_SCHEDULE, DOMAIN
from .casebook import CaseBook
from .baseline import BaselineManager
from .candidates import CandidateManager
from .cases import CaseError
from .exploration import SessionView
from .improvements import Improvements
from .pipeline import Pipeline, schedule
from .recorder import Recorder
from .reflective import RoleRuns, run_role
from .runtime import RuntimeData
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


async def tidy(hass, runtime):
    """Retention (retention.py), daily: what an improvement or a case uses, and a conversation still waiting for
    analysis, stay in full. Traced in today's trace."""
    recorder, pipeline, improvements = runtime.recorder, runtime.pipeline, runtime.improvements
    with trace.today():
        async with trace.span("tidy"):
            referenced = {c.get("recording_id") for c in runtime.casebook.cases.values()}
            for item in improvements.items.values():
                referenced |= {g.split(":")[0] for g in improvements._covers(item)}
            referenced |= set(pipeline.to_analyze()) | set(pipeline._at())
            named = {rid: {e for g in analysis.get("goals", []) for e in [g.get("entity_id"),
                                                                         *(g.get("entity_ids") or [])] if e}
                     for rid, analysis in pipeline._analyzed().items()}
            done = await retention.prune(recorder, referenced, named=named)
            done["runs"] = await runtime.role_runs.prune(set(recorder.index))
            done["traces"] = await retention.prune_traces(hass, trace.active_ids())
            runtime.retention = done
        trace.journal("info", "Tidy-up done", **done)


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
    casebook = CaseBook(hass, recorder)
    try:
        await casebook.start()
    except Exception:
        await recorder.stop()
        await trace.async_unload()
        raise
    candidates = CandidateManager(hass, casebook)
    runtime = RuntimeData(recorder=recorder, role_runs=RoleRuns(hass), casebook=casebook,
                          baselines=BaselineManager(hass, casebook), candidates=candidates,
                          improvements=Improvements(hass, recorder, casebook, candidates))
    try:
        await candidates.start()
        await runtime.role_runs.start()
        await runtime.improvements.start()  # Recovers a change HA stopped in the middle of.
        # The review panel, with how many improvements wait for review in its sidebar title.
        runtime.panel = await panel.async_setup_panel(hass, len(runtime.improvements.pending()))
        runtime.pipeline = pipeline = Pipeline(hass, entry, runtime)  # Run now, and the schedule (off by default).
        await pipeline.load()
        pipeline.current = next((dict(r) for r in reversed(runtime.role_runs.runs) if r.get("kind") == "pipeline"),
                                None)
        recorder.on_filed_closed = pipeline.request  # On-demand: a filed conversation runs once it ends.
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
        hass.async_create_background_task(tidy(hass, runtime), "assist_improver_retention")

    from homeassistant.helpers.event import async_track_time_change
    entry.async_on_unload(async_at_started(hass, tidy_soon))
    entry.async_on_unload(async_track_time_change(hass, tidy_soon, hour=4, minute=30, second=0))

    @callback
    def recover(_hass):  # A change HA stopped in the middle of is undone once HA (and its scripts) has started.
        hass.async_create_task(runtime.improvements.recover(), "assist_improver_recover")

    entry.async_on_unload(async_at_started(hass, recover))

    @callback
    def filed_waiting(_hass):  # On-demand: conversations filed before a restart run once HA has started.
        if pipeline.on_demand and pipeline.to_analyze():
            pipeline.request()

    entry.async_on_unload(async_at_started(hass, filed_waiting))

    entry.async_on_unload(hass.bus.async_listen(EVENT_STATE_CHANGED, recheck, event_filter=_availability_changed))
    entry.runtime_data = runtime
    scheduled = {"options": dict(entry.options), "off": None}

    def reschedule():
        if scheduled["off"]:
            scheduled["off"]()
        scheduled["off"] = schedule(hass, entry, pipeline, entry.options.get(CONF_SCHEDULE, "off"))

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
            report = await run(hass, entry, runtime, **call.data)
        except (CaseError, ValueError) as err:
            raise HomeAssistantError(str(err)) from err
        await runtime.improvements.refresh()  # Improvements the tests showed go up for review.
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
        await runtime.role_runs.record(report)  # Kept, so a lost page or connection loses nothing.
        return report

    async def write_summaries(call):
        """The editor writes the headline and what-you-get for improvements (those without, or the ones given)."""
        if not call.data.get("allow_paid_inference"):
            raise HomeAssistantError("Paid inference has not been authorized")
        from .pipeline import editor_model
        ids = set(call.data["improvement_ids"]) if call.data.get("improvement_ids") else None
        return {"copy": await runtime.improvements.write_copy(call.data.get("entity_id") or editor_model(entry), ids)}

    async def list_improvements(call):
        return {"improvements": runtime.improvements.listed()}


    async def developer_action(call):
        try:
            if call.service == "analyze_recording":
                return await analysis.analyze(hass, entry, runtime, **call.data)
            return await proposal.propose(hass, entry, runtime, call.service, **call.data)
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


async def async_unload_entry(hass, entry):
    runtime = entry.runtime_data
    await runtime.pipeline.stop()  # A run in progress stops first, then the stores close.
    await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if runtime.panel:
        panel.remove_panel(hass)
    practice.uninstall()
    hass.data.pop(practice.MODE_KEY, None)
    await runtime.recorder.stop()
    from .sandbox import stop_all
    await stop_all()  # No replay sandbox outlives the improver.
    for name in SERVICES + DEVELOPER_SERVICES:
        if hass.services.has_service(DOMAIN, name):
            hass.services.async_remove(DOMAIN, name)
    await trace.async_unload()  # Last: what stopping wrote is in today's trace.
    return True
