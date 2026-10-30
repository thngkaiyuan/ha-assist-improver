"""Assist Improver: setup and unload, the household services, and the developer services (hidden unless on)."""

import probatio
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, EVENT_STATE_CHANGED
from homeassistant.core import SupportsResponse, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv, issue_registry as ir
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.service import async_register_admin_service

from . import ai_setup, household, panel, practice, retention, settings, trace
from .const import (CONF_ACCEPTED_WEAK, CONF_AI_TASK, CONF_DEVELOPER, CONF_HOUSEHOLD, CONF_OPT_OUT, CONF_RECORDING,
                    CONF_SCHEDULE, DOMAIN)
from .cases import CaseError
from .flow import KEYS
from .improvements import Improvements
from .pipeline import Pipeline, schedule
from .recorder import Recorder
from .reflective import RoleRuns
from .runtime import RuntimeData
from .settings import role_model
from .simulate import Replays

PLATFORMS = ["switch"]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
SERVICES = ("status", "export_recording")  # For bug reports: what the improver holds, and one recording.
# Developer mode only (a hidden setting): what the improver holds, and a recording's replay, for debugging. They skip
# the panel's run guard and confirmation, so households never get them.
DEVELOPER_SERVICES = ("list_recordings", "list_improvements", "check_replay_fidelity", "replay_recording")


async def async_setup(hass, config):
    """Household knowledge, once per run: Assist reads it (llm.py) even while the entry reloads."""
    await household.async_setup(hass)
    return True


async def async_remove_entry(hass, entry):
    """Uninstalling leaves only the applied improvements behind: the household's lines are written into the own
    instructions of each assistant on Assist (while the Household knowledge switch is on), and the AI Tasks the
    improver set up and everything it stored (recordings, run log, traces, improvements' records) are removed. Sandbox
    mode ends for everyone first."""
    if (mode := hass.data.get(practice.MODE_KEY)) is not None:
        await mode.async_end()
    await household.async_remove(hass, on=entry.options.get(CONF_HOUSEHOLD, True))
    await ai_setup.remove_created(hass)
    await hass.async_add_executor_job(_remove_stores, hass.config.path(".storage"))


async def tidy(hass, runtime):
    """Retention (retention.py), daily, from each conversation's flow record: what a replay or an improvement uses
    stays in full, and so does a conversation a round still has to work on (at Triage to Write up, or held) or one
    waiting for a run. Traced in today's trace."""
    recorder, flow, pipeline = runtime.recorder, runtime.flow, runtime.pipeline
    with trace.today():
        async with trace.span("tidy"):
            referenced = runtime.replays.recordings() | {item.get("conversation")
                                                         for item in runtime.improvements.items.values()}
            # Waiting for a run: with on-demand improvements on, only those sent in (the fast lane's); the rest are
            # kept in full for 30 days, then condensed like any other (as the README says), schedule or not. A full run
            # still analyses a condensed one; it just can't replay it as a test.
            referenced |= {rid for rid in recorder.index if (conv := flow.record(rid))["stage"] in KEYS or conv["held"]
                           or (flow.waiting(rid) and (pipeline.filed(rid) or not pipeline.on_demand))}
            named = {rid: {e for g in found.get("goals", []) for e in [g.get("entity_id"), *(g.get("entity_ids") or [])]
                           if e} for rid in recorder.index if (found := flow.analysis(rid))}  # Its latest analysis's.
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
    if practice.MODE_KEY not in hass.data:  # Who's in sandbox mode, first: from then on they never get the real home.
        mode = practice.PracticeMode(hass)
        await mode.async_load()
        hass.data[practice.MODE_KEY] = mode
    await household.async_setup(hass)
    await trace.async_setup(hass, entry)  # First, so the recorder's start is in today's trace.
    recorder = Recorder(hass)
    # Privacy controls, read live from the settings, and who's speaking (practice.py's hooks): in place before the
    # recorder hears anything.
    recorder.recording_on = lambda: entry.options.get(CONF_RECORDING, True)
    recorder.opted_out = lambda user_id: user_id in entry.options.get(CONF_OPT_OUT, [])
    mode = hass.data[practice.MODE_KEY]
    mode.recorder = recorder
    mode.warm()  # A sandbox waiting for anyone already in sandbox mode.
    try:
        await recorder.start()
    except Exception:
        await _unhook(hass)
        await trace.async_unload()
        raise
    runtime = RuntimeData(recorder=recorder, role_runs=RoleRuns(hass), replays=Replays(hass),
                          improvements=Improvements(hass, recorder))
    try:
        await runtime.replays.start()
        await runtime.role_runs.start()
        await runtime.improvements.start()  # Recovers a change HA stopped in the middle of.
        # The review panel, with how many improvements wait for review in its sidebar title.
        runtime.panel = await panel.async_setup_panel(hass, len(runtime.improvements.pending()))
        from .flow import Flow
        runtime.flow = Flow(hass, entry, runtime)  # Where each conversation is, round by round (SPEC.md P0).
        await runtime.flow.load()
        runtime.pipeline = pipeline = Pipeline(hass, entry, runtime)  # Run now, and the schedule (off by default).
        pipeline.current = next((dict(r) for r in reversed(runtime.role_runs.runs) if r.get("kind") == "pipeline"),
                                None)
        # Filed from Assist: sent in, as the panel sends one in (flow.sent_in).
        recorder.on_filed = lambda rid, note, user_id: runtime.flow.sent_in(rid, note, via="assist", by=user_id)

        def closed(rid):  # On-demand: one sent in gets a run of its own once it ends.
            if pipeline.filed(rid):
                pipeline.request(rid)
        recorder.on_filed_closed = closed
        from .conversations import Transcripts
        runtime.transcripts = Transcripts(hass, recorder)
        async def forgotten(keys):
            # What was derived from deleted conversations goes with them: their flow records, run-log records, words
            # and replays (an improvement keeps its own copy of its test).
            await runtime.flow.forget(keys)
            await runtime.role_runs.drop(keys)
            await runtime.transcripts.all()  # Drops what's no longer in the index.
            await runtime.replays.drop(keys)
        recorder.on_forgotten = forgotten
        # Deleted during a run that a restart cut off: they go now.
        hass.async_create_task(recorder.forget_queued(), "assist_improver_forget_queued")
    except Exception:
        await _unhook(hass)
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

    async def started(_hass):
        # Once HA has started: each conversation sent in before a restart and still waiting gets its fast lane, in turn.
        if pipeline.on_demand and (sent := [rid for rid in recorder.index
                                            if runtime.flow.waiting(rid) and pipeline.filed(rid)]):
            pipeline.request(*sent)

    entry.async_on_unload(async_at_started(hass, started))

    entry.async_on_unload(hass.bus.async_listen(EVENT_STATE_CHANGED, recheck, event_filter=_availability_changed))

    async def stopping(_event):  # Home Assistant stopping: the replay worker ends with it.
        from .worker import shutdown
        await shutdown()

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
            return await check_fidelity(hass, await recorder.export(call.data["recording_id"]), assistant=True)
        except (CaseError, ValueError) as err:
            raise HomeAssistantError(str(err)) from err

    async def replay_recording(call):
        if not call.data.get("allow_paid_inference"):
            raise HomeAssistantError("Paid inference has not been authorized")
        from .simulate import replay_recording as replay
        try:
            return await replay(hass, recorder, call.data["recording_id"], call.data["last_turn"])
        except (CaseError, ValueError) as err:
            raise HomeAssistantError(str(err)) from err

    async def list_improvements(call):
        return {"improvements": runtime.improvements.listed()}

    developer = bool(entry.options.get(CONF_DEVELOPER))
    for name, handler, schema in (
        ("status", status, probatio.Schema({})),
        ("export_recording", export, probatio.Schema({probatio.Required("recording_id"): str})),
        ("list_recordings", list_recordings, probatio.Schema({})),
        ("list_improvements", list_improvements, probatio.Schema({})),
        ("check_replay_fidelity", check_replay_fidelity, probatio.Schema({probatio.Required("recording_id"): str})),
        ("replay_recording", replay_recording, probatio.Schema({
            probatio.Required("recording_id"): str,
            probatio.Optional("last_turn", default=1): probatio.All(int, probatio.Range(min=1, max=20)),
            probatio.Optional("allow_paid_inference", default=False): bool})),
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


async def _unhook(hass):
    """Sandbox mode stays, unrecorded, while the improver isn't loaded (practice._hook); its copies of the home go
    with the replay worker, which stops with the improver, and start afresh on the next turn."""
    if (mode := hass.data.get(practice.MODE_KEY)) is not None:
        mode.recorder = None
        await mode.cool()


async def async_unload_entry(hass, entry):
    runtime = entry.runtime_data
    await runtime.pipeline.stop()  # A run in progress stops first, then the stores close.
    await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if runtime.panel:
        panel.remove_panel(hass)
    await _unhook(hass)
    if getattr(entry, "disabled_by", None) is not None:  # Disabled: sandbox mode ends for everyone (DETAILS P11.11).
        await hass.data[practice.MODE_KEY].async_end()
    await runtime.recorder.stop()
    from .worker import shutdown
    await shutdown()  # No replay worker, nor any sandbox in it, outlives the improver.
    for name in SERVICES + DEVELOPER_SERVICES:
        if hass.services.has_service(DOMAIN, name):
            hass.services.async_remove(DOMAIN, name)
    await trace.async_unload()  # Last: what stopping wrote is in today's trace.
    return True
