"""Recording, offline audits, and explicitly authorized simulated baselines and candidates."""

from uuid import uuid4

import probatio
from homeassistant.const import __version__ as CORE_VERSION
from homeassistant.core import SupportsResponse, callback
from homeassistant.exceptions import ConfigEntryError, HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.service import async_register_admin_service

from . import ai_setup, analyzer
from .const import CONF_ACCEPTED_WEAK, CONF_AI_TASK, DOMAIN, SUPPORTED_CORE
from .casebook import CaseBook
from .baseline import BaselineManager
from .candidates import CandidateManager
from .cases import CaseError, digest as content_digest
from .digest import build_digest
from .recorder import Recorder
from .reflective import RoleRuns, run_role

SERVICES = ("status", "list_recordings", "export_recording", "list_cases", "get_case",
            "create_case", "case_feedback", "audit_case", "prepare_baseline", "start_baseline",
            "review_baseline_turn", "continue_baseline", "prepare_candidate", "run_candidate",
            "review_candidate", "apply_candidate", "analyze_recording")


async def async_setup_entry(hass, entry):
    if CORE_VERSION != SUPPORTED_CORE:
        raise ConfigEntryError("Recording compatibility is tested for HA 2026.10.2 only")
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
    try:
        await recorder.candidates.start()
        await recorder.role_runs.start()
    except Exception:
        await recorder.stop()
        raise
    # Providers may create the chosen AI Task after this entry loads: judge it once HA has started, then on changes.
    @callback
    def recheck(_event_or_hass):
        _update_setup_issue(hass, entry)

    entry.async_on_unload(async_at_started(hass, recheck))
    if chosen := entry.options.get(CONF_AI_TASK):
        entry.async_on_unload(async_track_state_change_event(hass, [chosen], recheck))
    entry.async_on_unload(entry.add_update_listener(_reload_on_options))
    entry.runtime_data = recorder
    hass.data[DOMAIN] = recorder

    async def status(call):
        return recorder.status()

    async def list_recordings(call):
        return {"recordings": list(recorder.index.values())}

    async def export(call):
        try:
            return await recorder.export(call.data["recording_id"])
        except ValueError as err:
            raise HomeAssistantError(str(err)) from err

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
            probatio.Required("recording_id"): str, **paid})),
    ):
        async_register_admin_service(hass, DOMAIN, name, handler, schema,
                                     supports_response=SupportsResponse.ONLY)
    return True


async def _reload_on_options(hass, entry):
    await hass.config_entries.async_reload(entry.entry_id)


def _update_setup_issue(hass, entry):
    """A Repairs notice while no suitable AI Task is chosen; fixed in place (repairs.py) or via Configure."""
    chosen = entry.options.get(CONF_AI_TASK)
    accepted = chosen and hass.states.get(chosen) is not None and entry.options.get(CONF_ACCEPTED_WEAK) == chosen
    if accepted or ai_setup.assess(hass, chosen)["ok"]:
        ir.async_delete_issue(hass, DOMAIN, "ai_task_setup")
        return
    ir.async_create_issue(hass, DOMAIN, "ai_task_setup", is_fixable=True, severity=ir.IssueSeverity.WARNING,
                          translation_key="ai_task_setup", data={"entry_id": entry.entry_id})


async def _analyze(hass, entry, recorder, recording_id, allow_paid_inference=False):
    """Journey analyzer on one recording: one paid AI Task call, recorded either way."""
    if not allow_paid_inference:
        raise CaseError("Paid inference has not been authorized")
    entity_id = entry.options.get(CONF_AI_TASK)
    payload = build_digest(await recorder.export(recording_id))
    run = {"id": uuid4().hex, "kind": analyzer.ROLE, "recording_id": recording_id, "status": "running",
           "digest_sha256": content_digest(payload), "entity_id": entity_id}
    await recorder.role_runs.record(run)  # Persist the attempt BEFORE any inference.
    result = await run_role(hass, role=analyzer.ROLE, entity_id=entity_id, instructions=analyzer.INSTRUCTIONS,
                            payload=payload, structure=analyzer.STRUCTURE, allow_paid_inference=True)
    run.update(result)
    if result["status"] == "completed":
        run["analysis"] = analyzer.to_label(recording_id, result["output"])
    return await recorder.role_runs.record(run)


async def async_unload_entry(hass, entry):
    await entry.runtime_data.stop()
    for name in SERVICES:
        hass.services.async_remove(DOMAIN, name)
    hass.data.pop(DOMAIN, None)
    return True
