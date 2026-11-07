"""Testable specs for the settings: the recommended setup (Opus for the thinking steps, a small model for Triage and
Write up), model problems and the run block, the setup dialog and its weak-model confirmation, Configure pointing to
the panel, the AI Task repair flow, which settings apply live and which reload, and developer mode.

Behaviour is checked through the public functions, the panel's websocket commands and the integration's flows; the
providers' own add-AI-Task flows are scripted, and every AI Task is synthetic.
"""

import json
import pathlib
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import probatio
import pytest
import pytest_asyncio
from homeassistant import config_entries, loader
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import Context, ServiceCall
from homeassistant.data_entry_flow import UnknownFlow
from homeassistant.exceptions import Unauthorized
from homeassistant.helpers import entity_registry as er, issue_registry as ir
from homeassistant.helpers.selector import SelectSelector, SelectSelectorConfig

import custom_components.assist_improver as integration
from conftest import serve
from custom_components.assist_improver import ai_setup, panel, repairs, settings
from custom_components.assist_improver.config_flow import USE_ANYWAY, AssistantImproverConfigFlow
from custom_components.assist_improver.const import (CONF_ACCEPTED_WEAK, CONF_AI_TASK, CONF_CHECK_AI_TASK, CONF_PAUSED,
                                                     CONF_TESTS_PER_GOAL, CONF_TRIAGE_AI_TASK, DOMAIN, NAME)
from test_ai_roles import _ai_list, _start, anthropic_entry, task_device
from test_changeset import _ws
from test_milestone1 import fake_entry, recording_hass  # noqa: F401  (fixture)

EDITOR = settings.ROLES["editor"]["key"]
REVIEWER = settings.ROLES["reviewer"]["key"]
OPUS = {"recommended": False, **ai_setup.RECOMMENDED}  # Claude Opus 5.5 at the recommended settings.
HAIKU = {"recommended": False, "chat_model": "claude-haiku-5-5", "max_tokens": 16000, "effort": "high"}
SONNET = {"recommended": False, "chat_model": "claude-sonnet-5-5", "max_tokens": 16000, "effort": "high"}
STRINGS = "custom_components/assist_improver/strings.json"


def ai_tasks(hass, *tasks):
    """Loaded AI providers that can add AI Tasks, each with the AI Task given: (entity_id, or None for a provider with
    none yet; provider domain; the AI Task's settings). Returns {entity_id, else domain: provider entry}."""
    found = {entity_id or domain: anthropic_entry(data, sub_title=entity_id or domain, domain=domain)
             for entity_id, domain, data in tasks}
    known = {e.entry_id: e for e in found.values()}
    hass.config_entries.async_get_entry = known.get
    hass.config_entries.async_entries = lambda domain=None: [e for e in known.values() if domain in (None, e.domain)]
    registry = er.async_get(hass)
    for entity_id, domain, _ in tasks:
        entry = found[entity_id or domain]
        object.__setattr__(entry, "_supported_subentry_types", {"ai_task_data": {"supports_reconfigure": True}})
        if entity_id:
            registry.async_get_or_create("ai_task", domain, f"uid-{entity_id}", config_entry=entry,
                                         config_subentry_id="sub-1", suggested_object_id=entity_id.split(".", 1)[1],
                                         device_id=task_device(hass, entry, "sub-1"))
            hass.states.async_set(entity_id, "unknown")
    return found


class ProviderFlows:
    """The providers' own add-AI-Task flows, scripted as OpenRouter's: one step offering their models; answering it adds
    the AI Task. refuse: the provider refuses instead ("raise", or an abort reason)."""

    def __init__(self, hass, models, refuse=None):
        self.hass, self.models, self.refuse = hass, models, refuse
        self.started, self.answers, self.aborted = [], [], []
        hass.config_entries.subentries = self

    async def async_init(self, handler, context):
        entry_id, _ = handler
        self.started.append(entry_id)
        if self.refuse == "raise":
            raise RuntimeError("provider offline")
        if self.refuse:
            return {"type": "abort", "reason": self.refuse}
        options = [{"value": m, "label": m} for m in self.models[entry_id]]
        return {"type": "form", "flow_id": entry_id, "errors": None, "data_schema": probatio.Schema(
            {probatio.Required("model"): SelectSelector(SelectSelectorConfig(options=options))})}

    async def async_configure(self, flow_id, answer):
        self.answers.append(answer)
        entry = self.hass.config_entries.async_get_entry(flow_id)
        subentry_id = f"sub-{len(entry.subentries) + 1}"
        entry.subentries = {**entry.subentries, subentry_id: SimpleNamespace(
            subentry_type="ai_task_data", title=answer["model"], data={"model": answer["model"]})}
        object_id = "new_" + re.sub(r"\W+", "_", answer["model"].split("/")[-1])
        er.async_get(self.hass).async_get_or_create("ai_task", entry.domain, f"uid-{object_id}", config_entry=entry,
                                                    config_subentry_id=subentry_id, suggested_object_id=object_id,
                                                    device_id=task_device(self.hass, entry, subentry_id))
        self.hass.states.async_set(f"ai_task.{object_id}", "unknown")
        return {"type": "create_entry"}

    def async_abort(self, flow_id):
        self.aborted.append(flow_id)


def admin(sent):
    """A signed-in admin's websocket connection; what it's sent goes to `sent`."""
    return SimpleNamespace(user=SimpleNamespace(id="alex", name="Alex", is_admin=True),
                           send_result=lambda i, r: sent.append(("ok", r)),
                           send_error=lambda i, code, message: sent.append(("error", code, message)))


def improver(hass, options):
    """The improver's entry as the panel's commands find it; saving changes its options."""
    entry = SimpleNamespace(entry_id="fixture-improver", options=dict(options))
    serve(hass, SimpleNamespace(pipeline=SimpleNamespace(entry=entry)))
    hass.config_entries.async_update_entry = lambda e, options: setattr(e, "options", options)
    return entry


async def set_up(hass, options):
    """The integration set up on an entry with these options, as Home Assistant does: a saved change reaches the update
    listener, and a reload is recorded. Returns (entry, unloads)."""
    entry, listeners, unloads = fake_entry(dict(options)), [], []
    entry.add_update_listener = lambda listener: (listeners.append(listener), lambda: None)[1]
    entry.async_on_unload = unloads.append
    hass.config_entries.async_reload = AsyncMock()
    await integration.async_setup_entry(hass, entry)

    def update(changed, options):
        changed.options = options
        hass.async_create_task(listeners[0](hass, changed))
    hass.config_entries.async_update_entry = update
    return entry, unloads


async def tear_down(hass, entry, unloads):
    for unsub in unloads:
        unsub()
    await integration.async_unload_entry(hass, entry)


async def save(hass, entry, **options):
    """A setting saved (as the panel or a fix flow does), with its update listener run."""
    hass.config_entries.async_update_entry(entry, options=options)
    await hass.async_block_till_done()


def issue(hass, issue_id):
    return ir.async_get(hass).async_get_issue(DOMAIN, issue_id)


def notices(hass):
    return {issue_id for domain, issue_id in ir.async_get(hass).issues if domain == DOMAIN}


@pytest_asyncio.fixture
async def setup_hass(recording_hass):
    """Home Assistant's real config entries, where the setup dialog runs."""
    loader.async_setup(recording_hass)
    recording_hass.config_entries = config_entries.ConfigEntries(recording_hass, {})
    await recording_hass.config_entries.async_initialize()
    return recording_hass


def setup_dialog(hass):
    """Settings → Devices & services → Add integration → Assist Improver."""
    flow = AssistantImproverConfigFlow()
    flow.hass, flow.handler, flow.context = hass, DOMAIN, {"source": "user"}
    return flow


# The recommended setup: Claude Opus 5.5 for the steps that need judgement, a small model for Triage and Write up.


@pytest.mark.asyncio
async def test_use_recommended_setup_reuses_opus_in_use_and_prefers_an_available_haiku(recording_hass):
    hass = recording_hass
    ai_tasks(hass, ("ai_task.a_opus", "anthropic", OPUS), ("ai_task.b_opus", "anthropic", OPUS),
             ("ai_task.a_mini", "openai_conversation", {"chat_model": "gpt-5-mini"}),
             ("ai_task.b_haiku", "anthropic", HAIKU), ("ai_task.z_haiku", "anthropic", HAIKU),
             ("ai_task.sonnet", "anthropic", SONNET))
    hass.states.async_set("ai_task.b_haiku", "unavailable")
    flows = ProviderFlows(hass, {})  # Nothing needs adding: any provider flow would show here.
    tuned = SimpleNamespace(options={CONF_AI_TASK: "ai_task.b_opus", REVIEWER: "ai_task.sonnet",
                                     CONF_TESTS_PER_GOAL: 5, CONF_PAUSED: ["test"]})
    assert settings.is_recommended_setup(hass, tuned) is False  # So the model menus offer it.
    # Opus on every judgement step isn't enough: the quick steps still run on Opus instead of a small model.
    assert settings.is_recommended_setup(hass, SimpleNamespace(options={CONF_AI_TASK: "ai_task.b_opus"})) is False
    options, notes = await settings.recommended_options(hass, tuned)
    assert notes == [] and flows.started == []
    # Opus in use stays (not the first one alphabetically); the reviewer's own model gives way to it. Haiku goes
    # before the provider's other small model, and an unavailable one isn't picked. Other settings stay.
    assert options == {CONF_AI_TASK: "ai_task.b_opus", CONF_TRIAGE_AI_TASK: "ai_task.z_haiku", EDITOR: "ai_task.z_haiku",
                       CONF_TESTS_PER_GOAL: 5, CONF_PAUSED: ["test"]}
    applied = SimpleNamespace(options=options)
    assert {role: settings.role_model(applied, role) for role in settings.ROLES} == {
        "triage": "ai_task.z_haiku", "analyzer": "ai_task.b_opus", "generator": "ai_task.b_opus",
        "reviewer": "ai_task.b_opus", "feedback_check": None, "editor": "ai_task.z_haiku"}
    assert settings.is_recommended_setup(hass, applied) is True
    assert settings.view(hass, applied)["recommended"] is True  # Nothing to offer: the button goes away.
    # A small model already in use is reused as it is; the feedback check keeps its own model.
    mine = SimpleNamespace(options={CONF_AI_TASK: "ai_task.a_opus", CONF_TRIAGE_AI_TASK: "ai_task.a_mini",
                                    CONF_CHECK_AI_TASK: "ai_task.z_haiku"})
    options, _ = await settings.recommended_options(hass, mine)
    assert options == {CONF_AI_TASK: "ai_task.a_opus", CONF_TRIAGE_AI_TASK: "ai_task.a_mini", EDITOR: "ai_task.a_mini",
                       CONF_CHECK_AI_TASK: "ai_task.z_haiku"}


@pytest.mark.asyncio
async def test_use_recommended_setup_adds_the_newest_haiku_beside_opus_on_the_same_provider(recording_hass):
    hass = recording_hass
    found = ai_tasks(hass, (None, "anthropic", {}),
                     ("ai_task.opus", "open_router", {"model": "anthropic/claude-opus-5.5"}))
    router = found["ai_task.opus"]
    flows = ProviderFlows(hass, {router.entry_id: ["openai/gpt-5-mini", "anthropic/claude-haiku-4.5",
                                                   "anthropic/claude-haiku-5.5", "anthropic/claude-opus-5.5"]})
    tuned = SimpleNamespace(options={CONF_AI_TASK: "ai_task.opus"})
    options, notes = await settings.recommended_options(hass, tuned)
    # Anthropic would come first for a new AI Task, but the small model goes beside Opus, on OpenRouter.
    assert flows.started == [router.entry_id, router.entry_id] and flows.aborted == [router.entry_id]
    assert flows.answers == [{"model": "anthropic/claude-haiku-5.5"}] and notes == []
    assert options == {CONF_AI_TASK: "ai_task.opus", CONF_TRIAGE_AI_TASK: "ai_task.new_claude_haiku_5_5",
                       EDITOR: "ai_task.new_claude_haiku_5_5"}
    assert settings.is_recommended_setup(hass, SimpleNamespace(options=options)) is True


@pytest.mark.asyncio
async def test_use_recommended_setup_says_what_it_couldnt_add_and_does_the_rest(recording_hass):
    hass = recording_hass
    found = ai_tasks(hass, ("ai_task.sonnet", "open_router", {"model": "anthropic/claude-sonnet-5.5"}))
    router = found["ai_task.sonnet"]
    ProviderFlows(hass, {router.entry_id: ["openai/gpt-5", "openai/gpt-5-mini", "anthropic/claude-sonnet-5.5"]})
    entry, sent = improver(hass, {CONF_AI_TASK: "ai_task.sonnet", CONF_TESTS_PER_GOAL: 4}), []
    await _ws(panel.ws_settings_recommended, hass, admin(sent), {"id": 1})
    status, shown = sent[-1]
    assert status == "ok" and shown["notes"] == [
        "Claude Opus 5.5 couldn't be added: Claude Opus 5.5 isn't available through this provider account."]
    # The account has no Haiku: the provider's own small model goes to the quick steps; the rest keep Sonnet.
    assert entry.options == {CONF_AI_TASK: "ai_task.sonnet", CONF_TESTS_PER_GOAL: 4,
                             CONF_TRIAGE_AI_TASK: "ai_task.new_gpt_5_mini", EDITOR: "ai_task.new_gpt_5_mini"}
    assert shown["roles"]["triage"]["entity_id"] == "ai_task.new_gpt_5_mini"
    assert shown["recommended"] is False  # Still not the recommended setup, and the panel says why (the notes).


@pytest.mark.asyncio
async def test_use_recommended_setup_without_any_ai_provider_says_to_add_one_and_changes_nothing(recording_hass):
    hass = recording_hass
    entry, sent = improver(hass, {CONF_TESTS_PER_GOAL: 5}), []
    assert settings.is_recommended_setup(hass, entry) is None  # Not offered at all.
    await _ws(panel.ws_settings_recommended, hass, admin(sent), {"id": 1})
    assert sent[-1] == ("error", "invalid",
                        "Add an AI provider, such as Anthropic, in Settings → Devices & services first.")
    assert entry.options == {CONF_TESTS_PER_GOAL: 5}


@pytest.mark.asyncio
async def test_a_small_model_that_cant_be_added_is_explained_and_the_quick_steps_keep_theirs(recording_hass):
    hass = recording_hass
    found = ai_tasks(hass, ("ai_task.opus", "open_router", {"model": "anthropic/claude-opus-5.5"}))
    router = found["ai_task.opus"]
    ProviderFlows(hass, {}, refuse="raise")
    options, notes = await settings.recommended_options(hass, SimpleNamespace(options={CONF_AI_TASK: "ai_task.opus"}))
    assert options == {CONF_AI_TASK: "ai_task.opus"}
    assert notes == ["A small model couldn't be added: The provider couldn't list its models (provider offline)."]
    ProviderFlows(hass, {router.entry_id: ["openai/gpt-5", "anthropic/claude-sonnet-5.5"]})
    assert await settings.small_ai_task(hass, router.entry_id) == (None, "The provider doesn't offer a small model.")


# The settings view and changing one setting.


@pytest.mark.asyncio
async def test_each_step_in_the_settings_says_why_its_model_cant_run(recording_hass):
    hass = recording_hass
    hass.states.async_set("ai_task.opus", "unknown")
    hass.states.async_set("ai_task.flash", "unavailable")
    shown = settings.view(hass, SimpleNamespace(options={CONF_AI_TASK: "ai_task.opus",
                                                         CONF_TRIAGE_AI_TASK: "ai_task.flash",
                                                         EDITOR: "ai_task.deleted"}))
    assert {role: shown["roles"][role]["problem"] for role in settings.ROLES} == {
        "triage": "unavailable", "analyzer": None, "generator": None, "reviewer": None, "feedback_check": None,
        "editor": "missing"}
    empty = settings.view(hass, SimpleNamespace(options={}))["roles"]
    # The steps that need a model ask for one; the optional ones are simply off.
    assert {role: (r["problem"], r["optional"]) for role, r in empty.items()} == {
        "triage": (None, True), "analyzer": ("none", False), "generator": ("none", False),
        "reviewer": ("none", False), "feedback_check": (None, True), "editor": ("none", False)}
    assert {r["size"] for role, r in empty.items() if role in settings.SMALL} == {"small"}
    assert {role for role, r in empty.items() if r["size"] == "large"} == {"analyzer", "generator", "reviewer"}


@pytest.mark.asyncio
async def test_the_model_menus_mark_recommended_small_and_fit_unknown_models(recording_hass):
    hass = recording_hass
    found = ai_tasks(hass, ("ai_task.opus", "anthropic", OPUS),
                     ("ai_task.mini", "openai_conversation", {"chat_model": "gpt-5-mini"}),
                     ("ai_task.orphan", "anthropic", OPUS))
    hass.states.async_set("ai_task.mystery", "unknown")  # A provider whose settings can't be read.
    removed = found.pop("ai_task.orphan").entry_id  # This one's provider was removed since.
    lookup = hass.config_entries.async_get_entry
    hass.config_entries.async_get_entry = lambda entry_id: None if entry_id == removed else lookup(entry_id)
    hass.config_entries.async_entries = lambda domain=None: list(found.values())
    tasks = {t["entity_id"]: t for t in settings.view(hass, SimpleNamespace(options={}))["ai_tasks"]}
    assert set(tasks) == {"ai_task.opus", "ai_task.mini", "ai_task.mystery", "ai_task.orphan"}
    fit = {e: {k: t[k] for k in ("strong", "weak", "small", "known")} for e, t in tasks.items()}
    assert fit["ai_task.opus"] == {"strong": True, "weak": False, "small": False, "known": True}
    assert fit["ai_task.mini"] == {"strong": False, "weak": True, "small": True, "known": True}
    assert "gpt-5-mini, a small model" in tasks["ai_task.mini"]["warnings"][0]  # Why it's not for judgement.
    for unknown in ("ai_task.mystery", "ai_task.orphan"):
        assert fit[unknown] == {"strong": False, "weak": False, "small": False, "known": False}  # "Fit unknown".
        assert "can't read" in tasks[unknown]["warnings"][0]
    assert tasks["ai_task.opus"]["provider"] == "anthropic"  # Read from the provider's own settings.


@pytest.mark.asyncio
async def test_models_are_named_by_their_claude_version_else_the_ai_tasks_own_name(recording_hass):
    hass = recording_hass
    ai_tasks(hass, ("ai_task.router_sonnet", "open_router", {"model": "anthropic/claude-sonnet-5.5"}),
             ("ai_task.router_gemini", "open_router", {"model": "google/gemini-3-flash"}))
    hass.states.async_set("ai_task.router_gemini", "unknown", {"friendly_name": "Google: Gemini 3 Flash"})
    hass.states.async_set("ai_task.kitchen", "unknown", {"friendly_name": "Kitchen AI"})
    hass.states.async_set("ai_task.bare", "unknown")
    labels = {t["entity_id"]: t["label"] for t in settings.view(hass, SimpleNamespace(options={}))["ai_tasks"]}
    assert labels == {"ai_task.router_sonnet": "Sonnet 5.5", "ai_task.router_gemini": "Gemini 3 Flash",
                      "ai_task.kitchen": "Kitchen AI", "ai_task.bare": "ai_task.bare"}
    # The run confirmation's estimate names a Claude model in full, as the setup dialog does.
    assert {e: settings.model_name(hass, e) for e in labels} == {
        "ai_task.router_sonnet": "Claude Sonnet 5.5", "ai_task.router_gemini": "Gemini 3 Flash",
        "ai_task.kitchen": "Kitchen AI", "ai_task.bare": "ai_task.bare"}


@pytest.mark.asyncio
async def test_only_loaded_providers_that_can_add_an_ai_task_are_offered_for_a_new_one(recording_hass):
    hass = recording_hass
    found = ai_tasks(hass, (None, "anthropic", {}), (None, "open_router", {}), (None, "openai_conversation", {}))
    object.__setattr__(found["open_router"], "state", ConfigEntryState.SETUP_RETRY)  # Not loaded right now.
    object.__setattr__(found["openai_conversation"], "_supported_subentry_types", {"conversation": {}})
    shown = settings.view(hass, SimpleNamespace(options={}))["providers"]
    assert shown == [{"entry_id": found["anthropic"].entry_id, "title": "Claude", "domain": "anthropic"}]


@pytest.mark.asyncio
async def test_a_setting_is_checked_without_touching_the_saved_options(hass):
    hass.states.async_set("ai_task.flash", "unavailable")
    saved = {CONF_AI_TASK: "ai_task.opus", CONF_TRIAGE_AI_TASK: "ai_task.flash", CONF_PAUSED: ["write"]}
    current = SimpleNamespace(options=dict(saved))
    assert settings.updated(hass, current, tests_per_goal=1)[CONF_TESTS_PER_GOAL] == 1
    assert settings.updated(hass, current, tests_per_goal=settings.MAX_TESTS_PER_GOAL)[CONF_TESTS_PER_GOAL] == 20
    # An AI Task that's unavailable right now can still be chosen; its step then says so.
    assert settings.updated(hass, current, role="feedback_check", entity_id="ai_task.flash")[CONF_CHECK_AI_TASK] == \
        "ai_task.flash"
    for role in ("triage", "feedback_check"):  # Optional steps can be turned off.
        assert settings.ROLES[role]["key"] not in settings.updated(hass, current, role=role, entity_id=None)
    for role in ("analyzer", "generator", "reviewer", "editor"):
        with pytest.raises(settings.SettingsError, match="This step needs an AI model."):
            settings.updated(hass, current, role=role, entity_id=None)
    assert settings.updated(hass, current, stage="write", pause=True)[CONF_PAUSED] == ["write"]  # Once.
    assert settings.updated(hass, current, stage="triage", pause=False)[CONF_PAUSED] == ["write"]  # Not paused: no-op.
    assert settings.updated(hass, current, stage="triage", pause=True)[CONF_PAUSED] == ["triage", "write"]  # In order.
    assert current.options == saved  # Nothing changes until the panel saves it.
    assert settings.paused(SimpleNamespace(options={CONF_PAUSED: ["write", "review", "bogus"]})) == {"write"}


@pytest.mark.asyncio
async def test_the_settings_view_shows_the_defaults_and_an_older_tests_setting(recording_hass):
    hass = recording_hass
    shown = settings.view(hass, SimpleNamespace(options={}))
    assert (shown["tests_per_goal"], shown["schedule"], shown["paused"]) == (3, "off", [])
    assert settings.view(hass, SimpleNamespace(options={CONF_TESTS_PER_GOAL: 2}))["tests_per_goal"] == 2


@pytest.mark.asyncio
async def test_a_new_ai_task_from_the_panel_is_added_through_the_providers_flow_and_used_for_that_step(recording_hass):
    hass = recording_hass
    found = ai_tasks(hass, ("ai_task.opus", "open_router", {"model": "anthropic/claude-opus-5.5"}))
    router = found["ai_task.opus"]
    flows = ProviderFlows(hass, {router.entry_id: ["openai/gpt-5-mini", "anthropic/claude-haiku-5.5"]})
    entry, sent = improver(hass, {CONF_AI_TASK: "ai_task.opus"}), []
    connection = admin(sent)
    await _ws(panel.ws_settings_models, hass, connection, {"id": 1, "entry_id": router.entry_id})
    assert sent[-1][0] == "ok" and sent[-1][1]["suggested"] == {"large": None, "small": "anthropic/claude-haiku-5.5"}
    assert flows.aborted == [router.entry_id]  # Only listed: the provider's flow is closed, nothing is added.
    await _ws(panel.ws_settings_create, hass, connection, {"id": 2, "entry_id": router.entry_id, "role": "triage",
                                                           "model": "anthropic/claude-haiku-5.5"})
    assert sent[-1][0] == "ok" and sent[-1][1]["roles"]["triage"]["entity_id"] == "ai_task.new_claude_haiku_5_5"
    assert entry.options == {CONF_AI_TASK: "ai_task.opus", CONF_TRIAGE_AI_TASK: "ai_task.new_claude_haiku_5_5"}
    flows.refuse = "entry_not_loaded"
    await _ws(panel.ws_settings_create, hass, connection, {"id": 3, "entry_id": router.entry_id, "role": "editor",
                                                           "model": "openai/gpt-5-mini"})
    assert sent[-1] == ("error", "create_failed", "The provider's setup stopped: entry_not_loaded")
    assert EDITOR not in entry.options
    flows.refuse = "raise"
    await _ws(panel.ws_settings_models, hass, connection, {"id": 4, "entry_id": router.entry_id})
    assert sent[-1] == ("error", "provider", "The provider couldn't list its models (provider offline).")


@pytest.mark.asyncio
async def test_listing_a_providers_models_stops_at_anything_unexpected_and_always_closes_its_flow(recording_hass):
    hass = recording_hass
    closed = []
    steps = {"api_key": {"type": "form", "flow_id": "f1", "errors": None,
                         "data_schema": probatio.Schema({probatio.Required("api_key"): str})},
             "errors": {"type": "form", "flow_id": "f2", "errors": {"base": "cannot_connect"},  # Its models aren't read.
                        "data_schema": probatio.Schema({probatio.Required("model"): SelectSelector(
                            SelectSelectorConfig(options=["anthropic/claude-haiku-5.5"]))})}}

    def close(flow_id):
        closed.append(flow_id)
        raise UnknownFlow  # Already closed by the provider: never an error here.
    for step, flow_id in (("api_key", "f1"), ("errors", "f2")):
        async def init(handler, context, step=step):
            return steps[step]
        hass.config_entries.subentries = SimpleNamespace(async_init=init, async_configure=AsyncMock(), async_abort=close)
        found = await settings.provider_models(hass, "provider")
        assert found == {"models": [], "default": None, "named": False, "suggested": {"large": None, "small": None}}
        assert closed[-1] == flow_id and not hass.config_entries.subentries.async_configure.called


@pytest.mark.asyncio
async def test_a_new_ai_task_at_the_providers_defaults_answers_only_its_name(recording_hass):
    hass = recording_hass
    openai = ai_tasks(hass, (None, "openai_conversation", {}))["openai_conversation"]
    answers = []

    async def init(handler, context):
        return {"type": "form", "flow_id": "f", "errors": None, "data_schema": probatio.Schema(
            {probatio.Required("name"): str, probatio.Required("recommended"): bool})}

    async def configure(flow_id, answer):
        answers.append(answer)
        openai.subentries = {**openai.subentries, "sub-2": SimpleNamespace(subentry_type="ai_task_data",
                                                                           data={"recommended": True})}
        er.async_get(hass).async_get_or_create("ai_task", "openai_conversation", "uid-gpt", config_entry=openai,
                                               config_subentry_id="sub-2", suggested_object_id="assist_improver")
        hass.states.async_set("ai_task.assist_improver", "unknown")
        return {"type": "create_entry"}
    hass.config_entries.subentries = SimpleNamespace(async_init=init, async_configure=configure)
    assert await ai_setup.create_ai_task(hass, openai.entry_id, "default") == ("ai_task.assist_improver", None)
    assert answers == [{"name": "Assist Improver", "recommended": True}]  # OpenAI's own default model.


@pytest.mark.asyncio
async def test_adding_an_ai_task_stops_and_says_why_when_the_providers_flow_goes_wrong(recording_hass, monkeypatch):
    hass = recording_hass
    anthropic = ai_tasks(hass, (None, "anthropic", {}))["anthropic"]

    closed = []

    def close(flow_id):
        closed.append(flow_id)
        raise UnknownFlow  # Already closed by the provider: never an error here.

    def provider(init, configure=None):
        hass.config_entries.subentries = SimpleNamespace(async_init=init, async_configure=configure, async_abort=close)

    async def offline(handler, context):
        raise RuntimeError("offline")
    provider(offline)
    assert await ai_setup.create_ai_task(hass, anthropic.entry_id, "recommended") == (
        None, "The provider's setup failed (RuntimeError)")
    assert closed == []  # None was open.

    async def endless(*_args, **_kwargs):
        return {"type": "form", "flow_id": "f", "errors": None, "data_schema": probatio.Schema({})}
    provider(endless, endless)
    assert await ai_setup.create_ai_task(hass, anthropic.entry_id, "recommended") == (
        None, "The provider's setup took more steps than expected")
    assert closed == ["f"]  # The provider's flow isn't left open.

    async def created(*_args, **_kwargs):  # Created, but the provider never adds the entity.
        return {"type": "create_entry"}
    provider(created)
    monkeypatch.setattr(ai_setup, "APPEAR_TIMEOUT", 0.01)  # Don't wait the 10 s here.
    entity_id, reason = await ai_setup.create_ai_task(hass, anthropic.entry_id, "recommended")
    assert entity_id is None and reason.endswith("choose it after a moment")


@pytest.mark.asyncio
async def test_a_new_ai_task_is_chosen_as_soon_as_the_provider_adds_its_entity(recording_hass):
    hass = recording_hass
    openai = ai_tasks(hass, (None, "openai_conversation", {}))["openai_conversation"]

    def add_entity():  # The provider reloads, then adds it: its registry entry, then its state.
        er.async_get(hass).async_get_or_create("ai_task", "openai_conversation", "uid-later", config_entry=openai,
                                               config_subentry_id="sub-2", suggested_object_id="assist_improver")
        hass.loop.call_later(0.05, hass.states.async_set, "ai_task.assist_improver", "unknown")

    async def init(handler, context):
        return {"type": "form", "flow_id": "f", "errors": None, "data_schema": probatio.Schema(
            {probatio.Required("name"): str, probatio.Required("recommended"): bool})}

    async def configure(flow_id, answer):
        openai.subentries = {**openai.subentries, "sub-2": SimpleNamespace(subentry_type="ai_task_data",
                                                                           data={"recommended": True})}
        hass.loop.call_later(0.05, add_entity)
        return {"type": "create_entry"}
    hass.config_entries.subentries = SimpleNamespace(async_init=init, async_configure=configure)
    started = hass.loop.time()
    assert await ai_setup.create_ai_task(hass, openai.entry_id, "default") == ("ai_task.assist_improver", None)
    assert hass.loop.time() - started < 0.4  # Not at a next look half a second on.
    assert hass.states.get("ai_task.assist_improver") is not None  # Ready to be checked (assess).


# Model problems: Repairs, and runs that wait instead of failing midway.


@pytest.mark.asyncio
async def test_a_steps_model_going_away_or_coming_back_updates_repairs_without_a_restart(recording_hass):
    hass = recording_hass
    hass.states.async_set("ai_task.opus", "unknown")
    hass.states.async_set("ai_task.flash", "unknown")
    entry, unloads = await set_up(hass, {CONF_AI_TASK: "ai_task.opus", CONF_TRIAGE_AI_TASK: "ai_task.flash"})
    try:
        await _start(hass)
        assert notices(hass) == set()
        hass.states.async_set("ai_task.flash", "unavailable")
        await hass.async_block_till_done()
        # Write up uses the Triage model too, while it has none of its own.
        assert issue(hass, "step_models").translation_placeholders == {"steps": "Triage and Write up",
                                                                       "link": "/assist-improver?view=settings"}
        hass.states.async_set("ai_task.flash", "unknown")
        await hass.async_block_till_done()
        assert notices(hass) == set()
        hass.states.async_remove("ai_task.flash")  # Deleted from Home Assistant.
        await hass.async_block_till_done()
        assert issue(hass, "step_models").translation_placeholders["steps"] == "Triage and Write up"
        assert not hass.config_entries.async_reload.called
    finally:
        await tear_down(hass, entry, unloads)


@pytest.mark.asyncio
async def test_unloading_takes_the_improvers_repairs_notices_and_loading_again_brings_back_those_still_true(
        recording_hass):
    hass = recording_hass  # Home Assistant doesn't remove an integration's notices for it.
    hass.states.async_set("ai_task.opus", "unknown")
    hass.states.async_set("ai_task.flash", "unavailable")
    options = {CONF_AI_TASK: "ai_task.gone", CONF_TRIAGE_AI_TASK: "ai_task.flash"}
    entry, unloads = await set_up(hass, options)
    await _start(hass)
    assert notices(hass) == {"ai_task_setup", "step_models"}
    await tear_down(hass, entry, unloads)
    assert notices(hass) == set()
    entry, unloads = await set_up(hass, options)
    try:
        await _start(hass)
        assert notices(hass) == {"ai_task_setup", "step_models"}
    finally:
        await tear_down(hass, entry, unloads)


@pytest.mark.asyncio
async def test_a_repair_fix_left_open_while_the_improver_is_removed_closes_saying_so(recording_hass):
    hass = recording_hass
    ai_tasks(hass, ("ai_task.opus", "anthropic", OPUS))
    entry, unloads = await set_up(hass, {CONF_AI_TASK: "ai_task.gone"})
    lookup, there = hass.config_entries.async_get_entry, [entry]
    hass.config_entries.async_get_entry = lambda entry_id: next(
        (e for e in there if e.entry_id == entry_id), None) or lookup(entry_id)
    try:
        await _start(hass)
        notice = issue(hass, "ai_task_setup")
        flow = await repairs.async_create_fix_flow(hass, "ai_task_setup", notice.data)
        flow.hass, flow.handler = hass, DOMAIN
        assert (await flow.async_step_init(notice.data))["type"] == "form"
        there.clear()  # Removed meanwhile.
        done = await flow.async_step_choose({CONF_AI_TASK: "ai_task.opus"})
        assert (done["type"], done["reason"]) == ("abort", "not_set_up")
        assert _strings_abort("not_set_up")
    finally:
        await tear_down(hass, entry, unloads)


def _strings_abort(reason):
    return json.load(open(STRINGS))["issues"]["ai_task_setup"]["fix_flow"]["abort"].get(reason)


@pytest.mark.asyncio
async def test_an_unavailable_main_model_is_named_in_repairs(recording_hass):
    hass = recording_hass
    hass.states.async_set("ai_task.opus", "unknown")
    entry, unloads = await set_up(hass, {CONF_AI_TASK: "ai_task.opus"})
    try:
        await _start(hass)
        assert notices(hass) == set()
        hass.states.async_set("ai_task.opus", "unavailable")  # Its provider failed to load, say.
        await hass.async_block_till_done()
        # The panel shows it, and a run waits for it ...
        assert settings.blocked_stages(hass, entry) == {"analyze": ["Analyze"], "propose": ["Propose", "Review"],
                                                        "write": ["Write up"]}
        assert settings.view(hass, entry)["roles"]["analyzer"]["problem"] == "unavailable"
        # ... and Repairs says so too: "A step whose model is gone or unavailable shows ⚠ on its model and in Repairs".
        assert issue(hass, "step_models").translation_placeholders["steps"] == "Analyze, Propose, Review and Write up"
    finally:
        await tear_down(hass, entry, unloads)


@pytest.mark.asyncio
async def test_steps_on_a_missing_main_model_are_left_to_its_own_notice(recording_hass):
    hass = recording_hass
    hass.states.async_set("ai_task.flash", "unavailable")
    entry = fake_entry({CONF_AI_TASK: "ai_task.gone", CONF_TRIAGE_AI_TASK: "ai_task.flash",
                        CONF_CHECK_AI_TASK: "ai_task.deleted", EDITOR: "ai_task.deleted_too"})
    integration._update_setup_issue(hass, entry)
    assert issue(hass, "ai_task_setup").is_fixable  # Fixed in place: the main model.
    # Analyze, Propose and Review use the main model, so only the steps with their own model are listed here.
    assert issue(hass, "step_models").translation_placeholders["steps"] == "Triage, Simulate and Write up"
    assert not issue(hass, "step_models").is_fixable  # Chosen again in the panel, where the link goes.


@pytest.mark.asyncio
async def test_an_accepted_weak_model_quiets_the_notice_only_while_it_exists(recording_hass):
    hass = recording_hass
    ai_tasks(hass, ("ai_task.weak", "anthropic", {"recommended": True}))  # The provider's defaults: a small model.
    entry = fake_entry({CONF_AI_TASK: "ai_task.weak", CONF_ACCEPTED_WEAK: "ai_task.weak"})
    integration._update_setup_issue(hass, entry)
    assert issue(hass, "ai_task_setup") is None
    hass.states.async_remove("ai_task.weak")
    integration._update_setup_issue(hass, entry)
    assert issue(hass, "ai_task_setup") is not None


# Which settings apply live, and which reload the integration.


@pytest.mark.asyncio
async def test_pausing_and_turning_a_step_off_apply_live_but_developer_mode_reloads(recording_hass):
    hass = recording_hass
    hass.states.async_set("ai_task.haiku", "unknown")
    entry, unloads = await set_up(hass, {CONF_AI_TASK: "ai_task.opus", CONF_TRIAGE_AI_TASK: "ai_task.haiku"})
    sent = []
    try:
        for change in ({"stage": "analyze", "pause": True}, {"role": "feedback_check", "entity_id": "ai_task.haiku"},
                       {"role": "triage", "entity_id": None}):  # Off: Triage passes everything on.
            await _ws(panel.ws_settings_set, hass, admin(sent), {"id": 1, **change})
            await hass.async_block_till_done()
            assert sent[-1][0] == "ok"
        assert entry.options == {CONF_AI_TASK: "ai_task.opus", CONF_PAUSED: ["analyze"],
                                 CONF_CHECK_AI_TASK: "ai_task.haiku"}
        await save(hass, entry, **entry.options, **{CONF_ACCEPTED_WEAK: "ai_task.opus"})
        assert not hass.config_entries.async_reload.called  # A run in progress keeps going.
        await _ws(panel.ws_settings_set, hass, admin(sent), {"id": 2, "developer_services": True})
        await hass.async_block_till_done()
        assert entry.options["developer_services"] is True
        hass.config_entries.async_reload.assert_awaited_once_with(entry.entry_id)  # So its services register.
    finally:
        await tear_down(hass, entry, unloads)


@pytest.mark.asyncio
async def test_a_live_change_rechecks_repairs_at_once(recording_hass):
    hass = recording_hass
    hass.states.async_set("ai_task.opus", "unknown")
    entry, unloads = await set_up(hass, {CONF_AI_TASK: "ai_task.opus"})
    try:
        await _start(hass)
        assert notices(hass) == set()
        await save(hass, entry, **{CONF_AI_TASK: "ai_task.opus", EDITOR: "ai_task.deleted"})
        assert notices(hass) == {"step_models"} and issue(hass, "step_models").translation_placeholders["steps"] == \
            "Write up"
        await save(hass, entry, **{CONF_AI_TASK: "ai_task.gone"})
        assert notices(hass) == {"ai_task_setup"}
        await save(hass, entry, **{CONF_AI_TASK: "ai_task.opus"})
        assert notices(hass) == set() and not hass.config_entries.async_reload.called
    finally:
        await tear_down(hass, entry, unloads)


# Developer mode: a hidden setting for debugging and evaluation rounds.


@pytest.mark.asyncio
async def test_developer_services_exist_only_in_developer_mode_and_only_for_admins(recording_hass):
    hass = recording_hass
    entry, unloads = await set_up(hass, {"developer_services": True})
    try:
        assert set(hass.services.async_services()[DOMAIN]) == set(integration.SERVICES) | set(
            integration.DEVELOPER_SERVICES)
        hass.auth = SimpleNamespace(async_get_user=AsyncMock(return_value=SimpleNamespace(is_admin=False)))
        listing = hass.services.async_services()[DOMAIN]["list_recordings"].job.target
        with pytest.raises(Unauthorized):
            await listing(ServiceCall(hass, DOMAIN, "list_recordings", context=Context(user_id="visitor"),
                                      return_response=True))
        await save(hass, entry, developer_services=False)
        hass.config_entries.async_reload.assert_awaited_once_with(entry.entry_id)  # Off reloads too ...
    finally:
        await tear_down(hass, entry, unloads)
    entry, unloads = await set_up(hass, entry.options)  # ... and the reload leaves the household's services only.
    try:
        assert set(hass.services.async_services()[DOMAIN]) == set(integration.SERVICES)
    finally:
        await tear_down(hass, entry, unloads)
    # Hidden: the panel (panel.js and its modules) never turns it on; it shows its view once on.
    for script in pathlib.Path("custom_components/assist_improver/www").rglob("*.js"):
        assert "developer_services" not in script.read_text(), script


# The setup dialog, Configure, and the AI Task repair flow.


@pytest.mark.asyncio
async def test_the_setup_dialog_confirms_a_weak_choice_with_why_and_can_choose_again(setup_hass, monkeypatch):
    hass = setup_hass
    weak = {"recommended": False, "chat_model": "claude-opus-5-5", "effort": "low", "max_tokens": 4000}
    found = ai_tasks(hass, ("ai_task.weak", "anthropic", weak))
    monkeypatch.setattr(settings, "small_ai_task", AsyncMock(side_effect=AssertionError("nothing new to add")))
    flow = setup_dialog(hass)
    form = await flow.async_step_user()
    labels, default = _ai_list(form)
    assert form["step_id"] == "user" and default == f"create:{found['ai_task.weak'].entry_id}:recommended"
    assert labels["ai_task.weak"] == "Claude Opus 5.5 · ai_task.weak · not recommended"
    confirm = await flow.async_step_user({CONF_AI_TASK: "ai_task.weak"})
    assert (confirm["type"], confirm["step_id"], confirm["menu_options"]) == ("menu", "confirm",
                                                                             [USE_ANYWAY, "choose_again"])
    assert confirm["description_placeholders"]["warnings"] == (
        "Its effort is low. High effort is recommended.\n\nIts output limit is 4000 tokens, which can cut the "
        "improver's answers short. 16000 is recommended.")
    again = await flow.async_step_choose_again()
    assert (again["type"], again["step_id"]) == ("form", "user")
    await flow.async_step_user({CONF_AI_TASK: "ai_task.weak"})
    done = await flow.async_step_use_anyway()
    assert (done["type"], done["title"], done["data"]) == ("create_entry", NAME, {})
    # Repairs stops asking about this choice; not a small model, so the quick steps aren't given it.
    assert done["options"] == {CONF_AI_TASK: "ai_task.weak", CONF_ACCEPTED_WEAK: "ai_task.weak"}


@pytest.mark.asyncio
async def test_first_setup_adds_a_small_model_only_beside_a_new_entry_and_never_stops_for_one(setup_hass, monkeypatch):
    hass = setup_hass
    found = ai_tasks(hass, ("ai_task.opus", "anthropic", OPUS), ("ai_task.haiku", "anthropic", HAIKU))
    provider = found["ai_task.opus"].entry_id
    hass.states.async_set("ai_task.haiku", "unavailable")  # Not reused while it can't run.
    small = AsyncMock(return_value=(None, "The provider doesn't offer a small model."))
    monkeypatch.setattr(settings, "small_ai_task", small)
    done = await setup_dialog(hass).async_step_user({CONF_AI_TASK: "ai_task.opus"})
    assert done["type"] == "create_entry" and done["options"] == {CONF_AI_TASK: "ai_task.opus"}
    assert not small.called  # An existing AI Task was chosen: nothing is added to the provider.

    async def create(hass, entry_id, kind, model=None, name=None):
        hass.states.async_set("ai_task.new_opus", "unknown")
        return "ai_task.new_opus", None
    monkeypatch.setattr(ai_setup, "create_ai_task", create)
    done = await setup_dialog(hass).async_step_user({CONF_AI_TASK: f"create:{provider}:recommended"})
    small.assert_awaited_once_with(hass, provider)  # Tried beside the new entry ...
    assert done["type"] == "create_entry" and done["options"] == {CONF_AI_TASK: "ai_task.new_opus"}  # ... not needed.


@pytest.mark.asyncio
async def test_with_only_weak_models_and_no_provider_to_add_one_the_dialog_recommends_opus(setup_hass):
    hass = setup_hass
    found = ai_tasks(hass, ("ai_task.weak", "anthropic", {"recommended": True}))
    object.__setattr__(found["ai_task.weak"], "state", ConfigEntryState.SETUP_ERROR)  # Can't add an AI Task now.
    form = await setup_dialog(hass).async_step_user()
    labels, default = _ai_list(form)
    assert list(labels) == ["ai_task.weak"] and default == "ai_task.weak"
    assert form["description_placeholders"]["guidance"] == "Claude Opus 5.5 is recommended."


@pytest.mark.asyncio
async def test_the_setup_dialog_preselects_a_recommended_ai_task_else_a_working_one_over_adding_one(setup_hass):
    hass = setup_hass
    strong = {"recommended": False, "chat_model": "claude-opus-5-5", "effort": "high", "max_tokens": 16000}
    ai_tasks(hass, ("ai_task.a", "anthropic", {"recommended": True}), ("ai_task.b", "ollama", {"model": "qwen3:32b"}),
             ("ai_task.c", "anthropic", strong), (None, "open_router", {"model": "fixture/model"}))
    hass.states.async_remove("ai_task.a")
    assert _ai_list(await setup_dialog(hass).async_step_user())[1] == "ai_task.c"  # Over the working one listed first.
    hass.states.async_set("ai_task.a", "unknown")
    hass.states.async_remove("ai_task.c")
    labels, default = _ai_list(await setup_dialog(hass).async_step_user())
    assert default == "ai_task.b" and any(value.startswith("create:") for value in labels)


@pytest.mark.asyncio
async def test_every_word_the_setup_dialog_shows_has_its_placeholders_filled(setup_hass):
    hass = setup_hass
    ai_tasks(hass, ("ai_task.weak", "anthropic", {"recommended": True}))
    strings = json.load(open(STRINGS))
    flow = setup_dialog(hass)
    shown = {"user": await flow.async_step_user(), "confirm": await flow.async_step_user({CONF_AI_TASK: "ai_task.weak"})}
    fix = strings["issues"]["ai_task_setup"]["fix_flow"]
    for step, result in shown.items():
        wanted = set(re.findall(r"\{(\w+)\}", strings["config"]["step"][step]["description"]))
        assert wanted <= set(result["description_placeholders"]), step
        same = {k: v for k, v in strings["config"]["step"][step].items() if k != "title"}
        assert {k: v for k, v in fix["step"]["choose" if step == "user" else step].items() if k != "title"} == same
    assert set(strings["config"]["step"]["confirm"]["menu_options"]) == {USE_ANYWAY, "choose_again"}
    assert "{reason}" in strings["config"]["error"]["create_failed"] == fix["error"]["create_failed"]


@pytest.mark.asyncio
async def test_configure_on_the_integration_page_points_to_the_panels_settings(setup_hass):
    options = AssistantImproverConfigFlow.async_get_options_flow(SimpleNamespace(entry_id="fixture-improver"))
    options.hass, options.handler = setup_hass, "fixture-improver"
    shown = await options.async_step_init()
    assert (shown["type"], shown["reason"]) == ("abort", "settings_in_panel")
    link = shown["description_placeholders"]["link"]
    assert link == "/assist-improver?view=settings"
    assert "[Open the settings]({link})" in json.load(open(STRINGS))["options"]["abort"]["settings_in_panel"]


@pytest.mark.asyncio
async def test_the_repair_fix_can_choose_again_and_saving_a_strong_model_clears_the_notice_live(recording_hass):
    hass = recording_hass
    ai_tasks(hass, ("ai_task.weak", "anthropic", {"recommended": True}), ("ai_task.opus", "anthropic", OPUS))
    entry, unloads = await set_up(hass, {CONF_AI_TASK: "ai_task.gone", CONF_ACCEPTED_WEAK: "ai_task.gone",
                                         CONF_TESTS_PER_GOAL: 7, CONF_TRIAGE_AI_TASK: "ai_task.weak"})
    lookup = hass.config_entries.async_get_entry
    hass.config_entries.async_get_entry = lambda entry_id: entry if entry_id == entry.entry_id else lookup(entry_id)
    try:
        await _start(hass)
        notice = issue(hass, "ai_task_setup")
        assert notice.is_fixable
        flow = await repairs.async_create_fix_flow(hass, "ai_task_setup", notice.data)
        flow.hass, flow.handler = hass, DOMAIN
        form = await flow.async_step_init(notice.data)
        labels, default = _ai_list(form)
        assert default == "ai_task.opus" and not any(v.startswith("create:") for v in labels)  # Ready to pick.
        assert (await flow.async_step_choose({CONF_AI_TASK: "ai_task.weak"}))["step_id"] == "confirm"
        again = await flow.async_step_choose_again()
        assert (again["type"], again["step_id"]) == ("form", "choose")
        done = await flow.async_step_choose({CONF_AI_TASK: "ai_task.opus"})  # Strong: no confirmation needed.
        assert done["type"] == "create_entry"
        await hass.async_block_till_done()
        # The main model changes; the other settings stay, and the old accepted warning goes.
        assert entry.options == {CONF_AI_TASK: "ai_task.opus", CONF_TESTS_PER_GOAL: 7,
                                 CONF_TRIAGE_AI_TASK: "ai_task.weak"}
        assert notices(hass) == set() and not hass.config_entries.async_reload.called
    finally:
        await tear_down(hass, entry, unloads)
