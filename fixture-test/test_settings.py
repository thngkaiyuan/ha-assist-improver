"""The settings view: each AI role's model, the other settings, and how changes apply (live, without a reload)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import probatio
import pytest
from homeassistant.helpers.selector import SelectSelector, SelectSelectorConfig

import custom_components.assist_improver as integration
from conftest import serve
from custom_components.assist_improver import ai_setup, panel, settings
from custom_components.assist_improver.const import (CONF_AI_TASK, CONF_CHECK_AI_TASK, CONF_SCHEDULE,
                                                     CONF_TESTS_PER_GOAL, CONF_TRIAGE_AI_TASK, DOMAIN)
from test_ai_roles import anthropic_entry, register_task, task_device
from test_changeset import _ws
from test_milestone1 import fake_entry, recording_hass  # noqa: F401  (fixture)


def entry(**options):
    return SimpleNamespace(options=options)


def test_each_role_reads_its_own_model_else_falls_back():
    main = entry(**{CONF_AI_TASK: "ai_task.opus"})
    assert {role: settings.role_model(main, role) for role in settings.ROLES} == {
        "triage": None, "analyzer": "ai_task.opus", "generator": "ai_task.opus", "reviewer": "ai_task.opus",
        "feedback_check": None, "editor": "ai_task.opus"}  # Optional roles are off until chosen.
    tuned = entry(**{CONF_AI_TASK: "ai_task.opus", CONF_TRIAGE_AI_TASK: "ai_task.haiku",
                     "reviewer_ai_task_entity_id": "ai_task.sonnet"})
    assert settings.role_model(tuned, "reviewer") == "ai_task.sonnet"
    assert settings.role_model(tuned, "generator") == "ai_task.opus"
    assert settings.role_model(tuned, "editor") == "ai_task.haiku"  # A small role: the quick-check model first.
    assert settings.role_model(entry(**{**tuned.options, "editor_ai_task_entity_id": "ai_task.flash"}), "editor") == \
        "ai_task.flash"


@pytest.mark.asyncio
async def test_a_change_is_checked_before_it_is_saved(hass):
    hass.states.async_set("ai_task.haiku", "unknown")
    hass.states.async_set("light.desk", "on")
    current = entry(**{CONF_AI_TASK: "ai_task.opus", CONF_TRIAGE_AI_TASK: "ai_task.haiku", "kept": 1})
    assert settings.updated(hass, current, role="analyzer", entity_id="ai_task.haiku") == {
        **current.options, "analyzer_ai_task_entity_id": "ai_task.haiku"}
    assert CONF_TRIAGE_AI_TASK not in settings.updated(hass, current, role="triage", entity_id=None)  # Off.
    assert settings.updated(hass, current, tests_per_goal=5)[CONF_TESTS_PER_GOAL] == 5
    assert settings.updated(hass, current, schedule="weekly")[CONF_SCHEDULE] == "weekly"
    paused = entry(**settings.updated(hass, current, stage="write", pause=True))
    paused = entry(**settings.updated(hass, paused, stage="analyze", pause=True))
    assert paused.options["paused_stages"] == ["analyze", "write"] and settings.paused(paused) == {"analyze", "write"}
    assert settings.updated(hass, paused, stage="analyze", pause=False)["paused_stages"] == ["write"]
    for bad in ({"role": "analyzer", "entity_id": None}, {"role": "analyzer", "entity_id": "light.desk"},
                {"role": "analyzer", "entity_id": "ai_task.gone"}, {"role": "nobody", "entity_id": "ai_task.haiku"},
                {"tests_per_goal": 0}, {"tests_per_goal": 21}, {"schedule": "hourly"}, {"stage": "review", "pause": True}):
        with pytest.raises(settings.SettingsError):
            settings.updated(hass, current, **bad)


@pytest.mark.asyncio
async def test_the_view_names_each_model_and_says_which_fit(recording_hass):
    hass = recording_hass
    strong = register_task(hass, anthropic_entry(dict(ai_setup.RECOMMENDED), sub_title="Assist Improver"))
    small = anthropic_entry({"recommended": False, "chat_model": "claude-haiku-5-5"}, sub_title="Assist Improver (Haiku)")
    first = hass.config_entries.async_get_entry
    hass.config_entries.async_get_entry = lambda i: small if i == small.entry_id else first(i)
    from homeassistant.helpers import entity_registry as er
    er.async_get(hass).async_get_or_create("ai_task", "anthropic", "uid-small", config_entry=small,
                                           config_subentry_id="sub-1", suggested_object_id="haiku",
                                           device_id=task_device(hass, small, "sub-1"))
    hass.states.async_set("ai_task.haiku", "unknown", {"friendly_name": "Assist Improver (Haiku)"})
    hass.config_entries.async_entries = lambda d=None: []
    shown = settings.view(hass, entry(**{CONF_AI_TASK: strong, CONF_TRIAGE_AI_TASK: "ai_task.haiku"}))
    tasks = {t["entity_id"]: t for t in shown["ai_tasks"]}
    assert tasks[strong]["label"] == "Opus 5.5" and tasks[strong]["strong"] and not tasks[strong]["weak"]
    assert tasks["ai_task.haiku"]["label"] == "Haiku 5.5" and tasks["ai_task.haiku"]["small"]
    assert tasks["ai_task.haiku"]["weak"] and tasks["ai_task.haiku"]["title"] == "Assist Improver (Haiku)"
    assert shown["roles"]["triage"] == {"entity_id": "ai_task.haiku", "size": "small", "optional": True,
                                        "problem": None}
    assert shown["roles"]["analyzer"] == {"entity_id": strong, "size": "large", "optional": False, "problem": None}
    assert shown["recommended"] is True  # Opus for the steps that need judgement, Haiku for Triage and Write up.
    assert shown["tests_per_goal"] == 3 and shown["schedule"] == "off"


@pytest.mark.asyncio
async def test_settings_apply_live_and_others_reload(recording_hass, monkeypatch):
    hass = recording_hass
    listeners, schedules = [], []
    monkeypatch.setattr(integration, "schedule", lambda hass, entry, pipeline, choice: (
        schedules.append(choice), (lambda: schedules.append(f"stop {choice}")))[1])
    improver = fake_entry({CONF_AI_TASK: "ai_task.opus"})
    improver.add_update_listener = lambda listener: (listeners.append(listener), lambda: None)[1]
    hass.config_entries.async_reload = AsyncMock()
    await integration.async_setup_entry(hass, improver)
    try:
        assert schedules == ["off"]
        improver.options = {**improver.options, "analyzer_ai_task_entity_id": "ai_task.sonnet", CONF_TESTS_PER_GOAL: 5}
        await listeners[0](hass, improver)
        improver.options = {**improver.options, CONF_SCHEDULE: "daily"}
        await listeners[0](hass, improver)
        assert schedules == ["off", "stop off", "daily"]  # Rescheduled in place.
        assert not hass.config_entries.async_reload.called  # A run in progress keeps going.
        improver.options = {**improver.options, CONF_AI_TASK: "ai_task.other"}  # The main model: live too.
        await listeners[0](hass, improver)
        assert not hass.config_entries.async_reload.called
        improver.options = {**improver.options, "something_else": 1}
        await listeners[0](hass, improver)
        hass.config_entries.async_reload.assert_awaited_once_with(improver.entry_id)
    finally:
        await integration.async_unload_entry(hass, improver)


@pytest.mark.asyncio
async def test_a_new_ai_task_uses_the_providers_own_flow_with_the_chosen_model(recording_hass):
    hass = recording_hass
    provider = anthropic_entry({"recommended": True})
    register_task(hass, provider)
    models = SelectSelector(SelectSelectorConfig(options=[{"value": "claude-opus-5-5", "label": "Claude Opus 5.5"},
                                                          {"value": "claude-haiku-5-5", "label": "Claude Haiku 5.5"}]))
    steps = lambda: iter([probatio.Schema({probatio.Required("name"): str, probatio.Required("recommended"): bool}),
                          probatio.Schema({probatio.Optional("chat_model", default="claude-opus-5-5"): models}),
                          probatio.Schema({probatio.Optional("max_tokens"): int})])
    flows, answers, aborted = {"current": steps()}, [], []

    async def init(handler, context):
        flows["current"] = steps()
        return {"type": "form", "flow_id": "f", "data_schema": next(flows["current"]), "errors": None}

    async def configure(flow_id, answer):
        answers.append(answer)
        try:
            return {"type": "form", "flow_id": "f", "data_schema": next(flows["current"]), "errors": None}
        except StopIteration:
            provider.subentries = {**provider.subentries, "sub-2": SimpleNamespace(
                subentry_type="ai_task_data", title="Assist Improver (Haiku)",
                data={"recommended": False, "chat_model": "claude-haiku-5-5"})}
            register_task(hass, provider, "ai_task.assist_improver_haiku", "sub-2")
            return {"type": "create_entry"}

    hass.config_entries.subentries = SimpleNamespace(async_init=init, async_configure=configure,
                                                     async_abort=lambda flow_id: aborted.append(flow_id))
    found = await settings.provider_models(hass, provider.entry_id)
    assert found == {"models": [{"value": "claude-opus-5-5", "label": "Claude Opus 5.5"},
                                {"value": "claude-haiku-5-5", "label": "Claude Haiku 5.5"}],
                     "default": "claude-opus-5-5", "named": True,
                     "suggested": {"large": "claude-opus-5-5", "small": "claude-haiku-5-5"}}
    assert answers == [{"name": "Assist Improver", "recommended": False}] and aborted == ["f"]  # Closed, not created.
    answers.clear()
    created = await ai_setup.create_ai_task(hass, provider.entry_id, "chosen", model="claude-haiku-5-5",
                                            name="Assist Improver (Haiku)")
    assert created == ("ai_task.assist_improver_haiku", None)
    assert answers == [{"name": "Assist Improver (Haiku)", "recommended": False}, {"chat_model": "claude-haiku-5-5"},
                       {}]  # Everything else at the provider's defaults.


@pytest.mark.asyncio
async def test_the_panel_changes_a_setting(recording_hass, monkeypatch):
    hass = recording_hass
    hass.states.async_set("ai_task.haiku", "unknown")
    improver = SimpleNamespace(entry_id="fixture-improver", options={CONF_AI_TASK: "ai_task.opus"})
    serve(hass, SimpleNamespace(pipeline=SimpleNamespace(entry=improver)))

    def update(entry, options):
        entry.options = options
    hass.config_entries.async_update_entry = update
    hass.config_entries.async_entries = lambda d=None: []
    sent = []
    connection = SimpleNamespace(user=SimpleNamespace(id="alex", name="Alex", is_admin=True),
                                 send_result=lambda i, r: sent.append(("ok", r)),
                                 send_error=lambda i, code, m: sent.append(("error", code, m)))
    await _ws(panel.ws_settings_set, hass, connection, {"id": 1, "role": "feedback_check", "entity_id": "ai_task.haiku"})
    assert improver.options[CONF_CHECK_AI_TASK] == "ai_task.haiku"
    assert sent[-1][1]["roles"]["feedback_check"]["entity_id"] == "ai_task.haiku"
    await _ws(panel.ws_settings_set, hass, connection, {"id": 2, "tests_per_goal": 99})
    assert sent[-1][:2] == ("error", "invalid") and CONF_TESTS_PER_GOAL not in improver.options


def test_the_assistants_list_puts_the_one_assist_uses_by_default_first(monkeypatch):
    """Read through assist_pipeline's own functions: its assistants, and the one it prefers."""
    from custom_components.assist_improver import household
    pipelines = {"p1": SimpleNamespace(id="p1", name="Home Assistant", conversation_engine="conversation.home_assistant"),
                 "p2": SimpleNamespace(id="p2", name="Claude", conversation_engine="conversation.claude")}
    loaded = {"hass": True}

    def all_of(hass):
        if not loaded["hass"]:
            raise KeyError("assist_pipeline")  # As Home Assistant's own before its assistants are loaded.
        return list(pipelines.values())
    monkeypatch.setattr(household, "async_get_pipelines", all_of)
    monkeypatch.setattr(household, "async_get_pipeline", lambda hass: pipelines["p2"])
    assert household.pipelines(None) == [{"id": "p2", "name": "Claude", "preferred": True},
                                         {"id": "p1", "name": "Home Assistant", "preferred": False}]
    loaded["hass"] = False
    assert household.pipelines(None) == []  # Before Home Assistant's assistants are loaded.


def test_the_suggested_models_are_opus_and_the_newest_haiku_else_the_providers_small_one():
    options = lambda *ids: [{"value": i, "label": i.title()} for i in ids]
    claude = options("claude-sonnet-5-5", "claude-haiku-4-5", "claude-opus-5-5", "claude-haiku-5-5")
    assert settings.pick(claude, "large")["value"] == "claude-opus-5-5"
    assert settings.pick(claude, "small")["value"] == "claude-haiku-5-5"
    assert settings.pick(options("gpt-5", "gpt-5-mini", "gpt-5-nano"), "small")["value"] == "gpt-5-mini"
    assert settings.pick(options("gpt-5"), "large") is None and settings.pick(options("gpt-5"), "small") is None


@pytest.mark.asyncio
async def test_a_missing_or_unavailable_model_is_named_and_listed_in_repairs(recording_hass):
    from homeassistant.helpers import issue_registry as ir
    hass = recording_hass
    hass.states.async_set("ai_task.opus", "unknown")
    hass.states.async_set("ai_task.flash", "unavailable")
    improver = SimpleNamespace(entry_id="fixture", options={
        CONF_AI_TASK: "ai_task.opus", CONF_TRIAGE_AI_TASK: "ai_task.flash", "editor_ai_task_entity_id": "ai_task.deleted"})
    assert settings.problems(hass, improver) == {"triage": "unavailable", "editor": "missing"}
    assert settings.blocked_stages(hass, improver) == {"triage": ["Triage"], "write": ["Write up"]}
    assert settings.problems(hass, SimpleNamespace(options={})) == {
        role: "none" for role in ("analyzer", "generator", "reviewer", "editor")}
    integration._update_setup_issue(hass, improver)
    issue = ir.async_get(hass).async_get_issue(DOMAIN, "step_models")
    assert issue.translation_placeholders == {"steps": "Triage and Write up", "link": "/assist-improver?view=settings"}
    hass.states.async_set("ai_task.flash", "unknown")
    improver.options.pop("editor_ai_task_entity_id")
    integration._update_setup_issue(hass, improver)
    assert ir.async_get(hass).async_get_issue(DOMAIN, "step_models") is None


@pytest.mark.asyncio
async def test_the_recommended_setup_reuses_what_fits_and_adds_the_rest(recording_hass, monkeypatch):
    hass = recording_hass
    models = {"ai_task.opus": "claude-opus-5-5", "ai_task.haiku": "claude-haiku-5-5",
              "ai_task.sonnet": "claude-sonnet-5-5"}
    for entity_id in models:
        hass.states.async_set(entity_id, "unknown")
    monkeypatch.setattr(ai_setup, "assess", lambda hass, e: {"ok": True, "warnings": [], "model": models.get(e)})
    provider = {"entry_id": "claude", "title": "Claude", "domain": "anthropic"}
    monkeypatch.setattr(settings, "providers", lambda hass: [provider])
    created = []

    async def create(hass, entry_id, kind, model=None, name=None):
        created.append((entry_id, kind))
        models["ai_task.new_opus"] = "claude-opus-5-5"
        hass.states.async_set("ai_task.new_opus", "unknown")
        return "ai_task.new_opus", None

    monkeypatch.setattr(ai_setup, "create_ai_task", create)
    tuned = SimpleNamespace(options={CONF_AI_TASK: "ai_task.sonnet", "reviewer_ai_task_entity_id": "ai_task.haiku",
                                     CONF_TESTS_PER_GOAL: 5})
    assert settings.is_recommended_setup(hass, tuned) is False
    options, notes = await settings.recommended_options(hass, tuned)
    assert created == [] and notes == []  # Both fit already: reused.
    assert options == {CONF_AI_TASK: "ai_task.opus", CONF_TRIAGE_AI_TASK: "ai_task.haiku",
                       "editor_ai_task_entity_id": "ai_task.haiku", CONF_TESTS_PER_GOAL: 5}
    assert settings.is_recommended_setup(hass, SimpleNamespace(options=options)) is True
    del models["ai_task.opus"]
    no_small = (None, "The provider doesn't offer a small model.")
    monkeypatch.setattr(settings, "small_ai_task", AsyncMock(return_value=no_small))
    del models["ai_task.haiku"]
    options, notes = await settings.recommended_options(hass, SimpleNamespace(options={CONF_AI_TASK: "ai_task.sonnet"}))
    assert created == [("claude", "recommended")] and options == {CONF_AI_TASK: "ai_task.new_opus"}
    assert notes == ["A small model couldn't be added: The provider doesn't offer a small model."]
    settings.small_ai_task.assert_awaited_once_with(hass, "claude")
    monkeypatch.setattr(settings, "providers", lambda hass: [{"entry_id": "gpt", "title": "OpenAI",
                                                              "domain": "openai_conversation"}])
    del models["ai_task.new_opus"]
    assert settings.is_recommended_setup(hass, SimpleNamespace(options={})) is None  # No way to Claude Opus 5.5.


def test_a_model_that_cant_use_tools_is_explained():
    from custom_components.assist_improver.reflective import NO_TOOLS_MESSAGE, explained
    ollama = "registry.ollama.ai/library/gemma3:4b does not support tools (status code: 400)"
    assert explained(ollama) == NO_TOOLS_MESSAGE
    assert explained("Rate limited") == "Rate limited"


@pytest.mark.asyncio
async def test_other_providers_models_are_read_where_they_name_them(recording_hass):
    hass = recording_hass
    register_task(hass, anthropic_entry({"model": "qwen3:8b"}, domain="ollama"), "ai_task.ollama")
    assessed = ai_setup.assess(hass, "ai_task.ollama")  # As Ollama names it on its device.
    assert assessed["model"] == "qwen3" and assessed["ok"] and "recommended" in assessed["warnings"][0]
    assert settings.view(hass, entry())["ai_tasks"][0]["known"]  # So the panel can judge its fit.
