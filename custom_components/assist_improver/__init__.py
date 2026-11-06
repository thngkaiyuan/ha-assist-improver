"""Assist Improver: setup and unload, the household services, and the developer services (hidden unless on)."""

import os

import probatio
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, EVENT_STATE_CHANGED
from homeassistant.core import SupportsResponse, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv, issue_registry as ir
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.service import async_register_admin_service
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.storage import Store

from . import ai_setup, household, panel, retention, settings, touchpoints, trace
from .const import CONF_ACCEPTED_WEAK, CONF_AI_TASK, CONF_DEVELOPER, CONF_SCHEDULE, DOMAIN, STORAGE_VERSION
from .cases import CaseError
from .conversations import Transcripts
from .flow import KEYS, Flow
from .improvements import Improvements
from .pipeline import Pipeline, schedule
from .recorder import Recorder
from .reflective import RoleRuns
from .runtime import RuntimeData
from .settings import role_model
from .simulate import Replays
from .worker import shutdown

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
SERVICES = ("status", "export_recording")  # For bug reports: what the improver holds, and one recording.
# Developer mode only (a hidden setting): what the improver holds, and a recording's replay, for debugging. They skip
# the panel's run guard and confirmation, so households never get them.
DEVELOPER_SERVICES = ("list_recordings", "list_improvements", "replay_recording")
ISSUES = ("ai_task_setup", "step_models", *touchpoints.ISSUES)  # Its Repairs notices (_update_setup_issue).


async def async_setup(hass, config):
    """Once per run: household knowledge, which Assist reads (llm.py) even while the entry reloads, and the panel's
    commands (Home Assistant can't unregister them; while the entry isn't loaded, they answer that it isn't)."""
    await household.async_setup(hass)
    panel.register_commands(hass)
    return True


async def async_remove_entry(hass, entry):
    """Uninstalling leaves only the applied improvements behind, and writes into no other integration's settings: the
    AI Tasks the improver set up and everything it stored (recordings, run log, traces, improvements' records) are
    removed, and the household's lines are left in a notification to save (household.async_remove). A change still
    waiting for the panel to finish its scripts (HA stopped midway, and no panel was opened since) stays as it stands:
    only a panel saves scripts, and the settings' Uninstall finishes it first."""
    await household.async_remove(hass)
    await ai_setup.remove_created(hass)
    _delete_issues(hass)
    # Each recording and trace has a store of its own, and no one index lists them all: they're found by name.
    for key in await hass.async_add_executor_job(_stored, hass.config.path(".storage")):
        await Store(hass, STORAGE_VERSION, key).async_remove()


async def tidy(hass, runtime):
    """Retention (retention.py), daily, from each conversation's flow record: what a replay or an improvement uses
    stays in full, and so does a conversation a round still has to work on (at Triage to Write up, or held) or one
    waiting for a run. Traced in today's trace."""
    recorder, flow = runtime.recorder, runtime.flow
    with trace.today():
        async with trace.span("tidy"):
            referenced = runtime.replays.recordings() | {item.get("conversation")
                                                         for item in runtime.improvements.items.values()}
            # Waiting for analysis: never condensed (DETAILS P8.13.7).
            referenced |= {rid for rid in recorder.index if (conv := flow.record(rid))["stage"] in KEYS or conv["held"]
                           or flow.waiting(rid)}
            named = {rid: {e for g in found.get("goals", []) for e in [g.get("entity_id"), *(g.get("entity_ids") or [])]
                           if e} for rid in recorder.index if (found := flow.analysis(rid))}  # Its latest analysis's.
            done = await retention.prune(recorder, referenced, named=named)
            done["runs"] = await runtime.role_runs.prune(set(recorder.index))
            done["traces"] = await retention.prune_traces(hass, trace.active_ids())
            runtime.retention = done
        trace.journal("info", "Tidy-up done", **done)


def _stored(path):
    """The keys of the stores the improver keeps."""
    return [name for name in (os.listdir(path) if os.path.isdir(path) else [])
            if name.startswith(f"{DOMAIN}.") and os.path.isfile(os.path.join(path, name))]


async def async_setup_entry(hass, entry):
    await household.async_setup(hass)
    await trace.async_setup(hass, entry)  # First, so the recorder's start is in today's trace.
    recorder, runtime, finished = Recorder(hass), None, False

    def undo_if_failed():
        """A setup that fails midway is undone as unloading undoes it (Home Assistant then runs only these callbacks):
        one undo, each step awaited in turn, as Home Assistant would run async callbacks side by side."""
        return None if finished else _undo(hass, recorder, runtime)

    entry.async_on_unload(undo_if_failed)
    await recorder.start()
    runtime = RuntimeData(recorder=recorder, role_runs=RoleRuns(hass), replays=Replays(hass),
                          improvements=Improvements(hass, recorder))
    await runtime.replays.start()
    await runtime.role_runs.start()
    runtime.flow = Flow(hass, entry, runtime)  # Where each conversation is, round by round (SPEC.md P0).
    await runtime.flow.load()
    # A version being revised waits on the improver, not the household (DETAILS P12.8.6): not counted as waiting,
    # from the first notification on; a question for the household is.
    runtime.improvements.revising = runtime.flow.revising
    runtime.improvements.asking = runtime.flow.questions
    await runtime.improvements.start()  # Recovers a change HA stopped in the middle of.
    # The review panel, with how many questions and improvements wait in its sidebar title.
    runtime.panel = await panel.async_setup_panel(hass, runtime.improvements.count())
    runtime.pipeline = pipeline = Pipeline(hass, entry, runtime)  # Run now, and the schedule (off by default).
    pipeline.current = next((dict(r) for r in reversed(runtime.role_runs.runs) if r.get("kind") == "pipeline"),
                            None)
    # Filed from Assist: sent in, as the panel sends one in (flow.sent_in).
    recorder.on_filed = lambda rid, note, user_id: runtime.flow.sent_in(rid, note, via="assist", by=user_id)

    def closed(rid):  # The fast lane: one sent in gets a run of its own once it ends.
        if pipeline.filed(rid):
            pipeline.request(rid)
    recorder.on_filed_closed = closed
    runtime.transcripts = Transcripts(hass, recorder)
    async def forgotten(keys):
        # What was derived from deleted conversations goes with them: their flow records, run-log records, words
        # and replays (an improvement keeps its own copy of its test).
        await runtime.flow.forget(keys)
        runtime.improvements.refresh()  # A question of one goes; a version being revised for one waits again.
        await runtime.role_runs.drop(keys)
        await runtime.transcripts.all()  # Drops what's no longer in the index.
        await runtime.replays.drop(keys)
    recorder.on_forgotten = forgotten
    # Deleted during a run that a restart cut off: they go now.
    entry.async_create_task(hass, recorder.forget_queued(), "assist_improver_forget_queued")
    # Providers may create the chosen AI Task after this entry loads: judge it once HA has started, then on changes.
    @callback
    def recheck(_event_or_hass):
        _update_setup_issue(hass, entry)

    entry.async_on_unload(async_at_started(hass, recheck))

    @callback
    def tidy_soon(_now_or_hass=None):  # After startup, then every night at 04:30; one cut off by unloading runs again.
        entry.async_create_background_task(hass, tidy(hass, runtime), "assist_improver_retention")

    entry.async_on_unload(async_at_started(hass, tidy_soon))
    entry.async_on_unload(async_track_time_change(hass, tidy_soon, hour=4, minute=30, second=0))

    @callback
    def recover(_hass):  # A change HA stopped in the middle of is undone once HA (and its scripts) has started.
        entry.async_create_task(hass, runtime.improvements.recover(), "assist_improver_recover")

    entry.async_on_unload(async_at_started(hass, recover))

    async def started(_hass):
        # Once HA has started: each conversation sent in before a restart and still waiting gets its fast lane, in turn.
        if sent := [rid for rid in recorder.index if runtime.flow.waiting(rid) and pipeline.filed(rid)]:
            pipeline.request(*sent)

    entry.async_on_unload(async_at_started(hass, started))

    entry.async_on_unload(hass.bus.async_listen(EVENT_STATE_CHANGED, recheck, event_filter=_availability_changed))

    async def stopping(_event):  # Home Assistant stopping: the replay workers end with it.
        await shutdown(hass)

    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, stopping))
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

    async def status(call):
        return recorder.status()

    async def list_recordings(call):
        return {"recordings": list(recorder.index.values())}

    def known(call):
        """The recording an action names, if the improver keeps it."""
        if call.data["recording_id"] not in recorder.index:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="unknown_recording")
        return call.data["recording_id"]

    def cannot_replay(err):
        return HomeAssistantError(translation_domain=DOMAIN, translation_key="cannot_replay",
                                  translation_placeholders={"error": str(err)})

    async def export(call):
        try:
            return await recorder.export(known(call))
        except ValueError as err:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="recording_unavailable") from err

    async def replay_recording(call):
        if not call.data.get("allow_paid_inference"):
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="paid_inference_not_allowed")
        known(call)
        from .simulate import replay_recording as replay
        try:
            return await replay(hass, recorder, call.data["recording_id"], call.data["last_turn"])
        except (CaseError, ValueError) as err:
            raise cannot_replay(err) from err

    async def list_improvements(call):
        return {"improvements": runtime.improvements.listed()}

    developer = bool(entry.options.get(CONF_DEVELOPER))
    for name, handler, schema in (
        ("status", status, probatio.Schema({})),
        ("export_recording", export, probatio.Schema({probatio.Required("recording_id"): str})),
        ("list_recordings", list_recordings, probatio.Schema({})),
        ("list_improvements", list_improvements, probatio.Schema({})),
        ("replay_recording", replay_recording, probatio.Schema({
            probatio.Required("recording_id"): str,
            probatio.Optional("last_turn", default=1): probatio.All(int, probatio.Range(min=1, max=20)),
            probatio.Optional("allow_paid_inference", default=False): bool})),
    ):
        if name in SERVICES or developer:
            async_register_admin_service(hass, DOMAIN, name, handler, schema,
                                         supports_response=SupportsResponse.ONLY)
    finished = True
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
    await _undo(hass, runtime.recorder, runtime)
    return True


async def _undo(hass, recorder, runtime):
    """What setup did, undone in order; also what a setup that failed midway did (runtime None: it failed before the
    recorder had started). Its Repairs notices go too: setup makes them again."""
    if runtime is not None and runtime.pipeline is not None:
        await runtime.pipeline.stop()  # A run in progress stops first, then the stores close.
    if runtime is not None and runtime.panel:
        panel.remove_panel(hass)
    await recorder.stop()
    if runtime is not None and runtime.transcripts is not None:
        await runtime.transcripts.close()
    await shutdown(hass)  # No replay worker, nor its sandbox, outlives the improver.
    for name in SERVICES + DEVELOPER_SERVICES:
        if hass.services.has_service(DOMAIN, name):
            hass.services.async_remove(DOMAIN, name)
    _delete_issues(hass)
    await trace.async_unload()  # Last: what stopping wrote is in today's trace.


def _delete_issues(hass):
    hass.data.pop(touchpoints.BROKEN, None)  # A break still there is reported again where it's next used.
    for issue_id in ISSUES:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
