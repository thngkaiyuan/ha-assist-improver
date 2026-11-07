"""AI-role runtime and guided AI Task setup: real HA AI Task and Anthropic code, scripted transport."""

from datetime import datetime, UTC
import json
import re
from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock

from anthropic.types import ModelInfo
import probatio
import pytest
from homeassistant.components.anthropic.ai_task import AnthropicTaskEntity
from homeassistant.components.openai_conversation.entity import OpenAIBaseLLMEntity
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.helpers import device_registry as dr, entity_registry as er, issue_registry as ir

from anthropic_fixture import anthropic_agent
from ha_task import stand_in
from recordings import make_recording
from test_changeset import tested
from test_milestone1 import fake_entry, recording_hass  # noqa: F401  (fixture)
from conftest import loaded_for
from custom_components.assist_improver import ai_setup, analyzer, touchpoints
from custom_components.assist_improver.cases import CaseError
from custom_components.assist_improver.exploration import SessionView
from custom_components.assist_improver.const import CONF_ACCEPTED_WEAK, CONF_AI_TASK, DOMAIN
from custom_components.assist_improver.reflective import run_role
from evaluation.scoring import score_recording

TASK = "ai_task.fixture_task"
PRESS = {"summary": "One relay press", "kind": "sequence", "entity_id": "switch.fixture_relay", "states": ["off", "on"]}
# Pressed in turn 1, finished by hand in turns 2 and 3 (steps towards that press: every turn has a goal, P0.3), and
# pressed again in turn 4.
ANALYSIS = {"goals": [{"turn": 1, **PRESS},
                      {"turn": 2, "summary": "Finish the press with the press script", "kind": "step", "serves": 1},
                      {"turn": 3, "summary": "Relay back on", "kind": "step", "serves": 1},
                      {"turn": 4, **PRESS}], "not_requests": [], "question_for_user": ""}
LABEL = {"recording_id": "synthetic-recording", "not_requests": [],
         "goals": [{"turn": 1, **PRESS, "reached": True}, {"turn": 4, **PRESS, "reached": True}]}


def task_entity(hass, reply):
    _, client, _ = anthropic_agent(hass)
    client.blocks = [[{"type": "text", "text": reply if isinstance(reply, str) else json.dumps(reply)}]]
    entry = SimpleNamespace(entry_id="fixture-entry", runtime_data=_coordinator(client))
    subentry = SimpleNamespace(subentry_id="fixture-task", title="Fixture AI Task",
                               data={"chat_model": "fixture-no-model", "max_tokens": 64, "prompt_caching": "off",
                                     "thinking_budget": 0, "thinking_type": "disabled"})
    entity = AnthropicTaskEntity(entry, subentry)
    entity.hass, entity.entity_id = hass, TASK
    entity.async_write_ha_state = lambda: None  # Not added through an entity platform here.
    entity.platform = SimpleNamespace(domain="anthropic")
    stand_in(hass, lambda e: entity if e == TASK else None)
    return entity, client


def _coordinator(client):
    agent, _, _ = anthropic_agent(None)
    coordinator = agent.entry.runtime_data
    coordinator.client = client
    return coordinator


@pytest.mark.asyncio
async def test_role_call_needs_consent_and_an_entity(recording_hass, effect_guards):
    _, client = task_entity(recording_hass, ANALYSIS)
    common = dict(role="r", instructions="i", payload={}, structure=analyzer.STRUCTURE)
    with pytest.raises(CaseError, match="not been authorized"):
        await run_role(recording_hass, entity_id=TASK, **common)
    with pytest.raises(CaseError, match="No AI Task"):
        await run_role(recording_hass, entity_id=None, allow_paid_inference=True, **common)
    assert client.requests == [] and effect_guards == []


@pytest.mark.asyncio
async def test_the_analyzer_through_real_ai_task_code(recording_hass, monkeypatch, effect_guards):
    from custom_components.assist_improver import analysis, async_setup_entry, async_unload_entry
    hass = recording_hass
    _, client = task_entity(hass, "Done.")
    wrong = {**ANALYSIS, "goals": [{**ANALYSIS["goals"][0], "states": ["off", "pressed"]}, *ANALYSIS["goals"][1:]]}
    client.blocks = [[{"type": "tool_use", "id": "t1", "name": "submit_goals", "input": wrong}],
                     [{"type": "tool_use", "id": "t2", "name": "submit_goals", "input": ANALYSIS}],
                     [{"type": "text", "text": "Done."}]]
    entry = fake_entry({CONF_AI_TASK: TASK}); await async_setup_entry(hass, entry)
    record = make_recording()
    await entry.runtime_data.recorder.store(record["id"]).async_save(record)
    entry.runtime_data.recorder.index[record["id"]] = {"status": record["status"]}
    try:
        run = await analysis.analyze(hass, entry, entry.runtime_data, record["id"])
        assert run["status"] == "completed", run.get("error")
        assert score_recording(run["analysis"], LABEL, turn_count=4)["passed"]
        assert run["analysis"]["extra_turns"] == [2, 3]
        assert [w["reached"] for w in run["analysis"]["goals"]] == [True, False, False, True]  # Steps: it reacted.
        assert run["answer_status"] == "accepted" and run["submissions"] == 2 and run["reply"] == "Done."
        assert run["usage"]["status"] == "provider_reported_partial" and run["usage"]["events"]
        first, second, _ = client.requests
        assert "format" not in (first.get("output_config") or {})  # The answer comes from the tool, not a format.
        assert any(t["name"] == "submit_goals" for t in first["tools"])
        rejected = json.dumps(second["messages"][-1])
        assert "has no state" in rejected and "pressed" in rejected  # The agent was told what to fix.
        sent = json.dumps(first["messages"])
        assert "List the user's goals" in sent and "Toggle the fixture relay." in sent
        # The improver's own call is not recorded as a household session.
        assert not entry.runtime_data.recorder.sessions
        stored = entry.runtime_data.role_runs.runs
        assert [r["status"] for r in stored] == ["completed"] and stored[0]["recording_id"] == record["id"]
        assert effect_guards == []
    finally:
        await async_unload_entry(hass, entry)


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", [{"smooth": True, "sure": True}, {"smooth": True, "sure": False},
                                    {"smooth": False, "sure": True}])
async def test_only_a_sure_smooth_quick_check_skips_the_analyzer(recording_hass, effect_guards, monkeypatch, answer):
    from custom_components.assist_improver import analysis, async_setup_entry, async_unload_entry
    from custom_components.assist_improver.const import CONF_TRIAGE_AI_TASK
    from custom_components.assist_improver.hypotheses import Sessions, scenario
    hass = recording_hass
    _, client = task_entity(hass, "Done.")
    client.blocks = [[{"type": "tool_use", "id": "t1", "name": "submit_goals", "input": ANALYSIS}],
                     [{"type": "text", "text": "Done."}]]
    # The fixture has four turns: the quick check reads every conversation, however long.
    client.blocks.insert(0, [{"type": "text", "text": json.dumps({**answer, "why": "Assist did what was asked."})}])
    entry = fake_entry({CONF_AI_TASK: TASK, CONF_TRIAGE_AI_TASK: TASK})
    await async_setup_entry(hass, entry)
    record = make_recording()
    await entry.runtime_data.recorder.store(record["id"]).async_save(record)
    entry.runtime_data.recorder.index[record["id"]] = {"status": record["status"]}
    try:
        run = await analysis.analyze(hass, entry, entry.runtime_data, record["id"], triage_only=True)
        assert run["triage"]["output"]["smooth"] is answer["smooth"]
        skipped = answer == {"smooth": True, "sure": True}
        if not skipped:  # Held for the analyzer, as Analyze then runs it.
            assert run["answer_status"] == "awaiting_analysis"
            run = await analysis.analyze(hass, entry, entry.runtime_data, record["id"])
        assert len(client.requests) == (1 if skipped else 3)  # The check, then the analyzer unless it skipped.
        assert "Toggle the fixture relay." in json.dumps(client.requests[0]["messages"])
        if skipped:
            assert run["answer_status"] == "triaged_smooth" and run["analysis"]["goals"] == []
            # The generator and reviewer still see the session: without friction, its goals not extracted.
            view = SessionView(record)
            sessions = Sessions([run["analysis"]], [view])
            assert sessions.listing()["sessions"][0]["state"] == "without_friction"
            assert "not_analysed" in scenario(run["analysis"], view)
        else:
            assert run["answer_status"] == "accepted" and run["analysis"]["extra_turns"] == [2, 3]
        assert effect_guards == []
    finally:
        await async_unload_entry(hass, entry)


@pytest.mark.asyncio
async def test_unparseable_reply_is_recorded_as_failed_without_retry(recording_hass, effect_guards):
    _, client = task_entity(recording_hass, "not json at all")
    run = await run_role(recording_hass, role="r", entity_id=TASK, instructions="i", payload={"x": 1},
                         structure=analyzer.STRUCTURE, allow_paid_inference=True)
    assert run["status"] == "failed" and len(client.requests) == 1 and effect_guards == []


@pytest.mark.asyncio
async def test_a_completed_role_call_keeps_its_answer_when_reading_its_trace_fails(hass, effect_guards):
    """run_role reads the call's usage from Home Assistant's conversation trace (trace.as_dict(), which deep-copies
    every tool the model was given), through a real AI Task entity. A tool that can't be copied (one holding Home
    Assistant) mustn't turn a completed call into a failure: it's completed, its answer kept, its usage unavailable."""
    from homeassistant.helpers import llm
    from conftest import assist_home
    from ha_task import serve
    from custom_components.assist_improver.exploration import SessionAPI

    class HoldsHass(llm.Tool):
        name, description, parameters = "holds_hass", "A tool holding Home Assistant.", probatio.Schema({})
        integration = "assist_improver"

        def __init__(self, hass):
            self.hass = hass

        async def async_call(self, hass, tool_input, llm_context):
            return llm.ToolResult(data={})

    class Answers:
        async def answer(self, hass, role, payload, tools):
            return "Done."
    await assist_home(hass, {})
    hass.states.async_set(TASK, "unknown")
    serve(hass, Answers(), (TASK,))
    run = await run_role(hass, role="r", entity_id=TASK, instructions="i", payload={}, structure=None,
                         llm_api=SessionAPI(hass, tools=[HoldsHass(hass)]), allow_paid_inference=True)
    assert (run["status"], run["output"], run["usage"]["status"]) == ("completed", "Done.", "unavailable"), run["error"]
    assert effect_guards == []


def fake_view(start, changes, turns):
    """A session view with given start states and changes: {entity: [(turn, old, new, by)]}."""
    return SimpleNamespace(
        source={"states": {e: {"state": s} for e, s in start.items()}},
        changes={e: [{"at": f"2026-01-01T00:00:{i:02d}+00:00", "during_turn": t, "old": o, "new": n, "by": b}
                     for i, (t, o, n, b) in enumerate(cs)] for e, cs in changes.items()},
        digest={"turns": [{"turn": n} for n in range(1, turns + 1)]})


def test_sub_goals_and_side_effects_left():
    # "Turn on light A": Assist turns on A and B; the user then has B turned off again.
    view = fake_view({"light.a": "off", "light.b": "off"},
                     {"light.a": [(1, "off", "on", "assistant")],
                      "light.b": [(1, "off", "on", "assistant"), (2, "on", "off", "assistant")]}, turns=2)
    output = {"goals": [{"turn": 1, "summary": "A on", "kind": "state", "entity_id": "light.a", "state": "on"},
                        {"turn": 2, "summary": "B off again", "kind": "state", "entity_id": "light.b", "state": "off",
                         "serves": 1}], "not_requests": [], "question_for_user": ""}
    analysis = analyzer.to_analysis("rec", output, view)
    assert analysis["goals"][0]["reached"] is True and analysis["goals"][1]["reached"] is True  # Sub-goals too.
    assert analysis["extra_turns"] == [2] and analysis["side_effects_left"] == []
    left_on = fake_view({"light.a": "off", "light.b": "off"},
                        {"light.a": [(1, "off", "on", "assistant")], "light.b": [(1, "off", "on", "assistant")]}, turns=1)
    one_goal = {**output, "goals": output["goals"][:1]}
    assert analyzer.to_analysis("rec", one_goal, left_on)["side_effects_left"] == [
        {"entity_id": "light.b", "start": "off", "end": "on", "turn": 1}]


def test_analysis_derives_extra_turns_and_reached():
    view = SessionView(make_recording())
    only_first = {"goals": [{"turn": 1, **PRESS, "brightness_pct": None, "fact": None}], "not_requests": [],
                  "question_for_user": " "}
    analysis = analyzer.to_analysis("rec", only_first, view)
    assert analysis["goals"] == [{"turn": 1, **PRESS, "reached": True}]  # Nulls dropped; the press ends in turn 4.
    assert analysis["extra_turns"] == [2, 3, 4] and analysis["question_for_user"] is None
    asks = analyzer.to_analysis("rec", {**only_first, "question_for_user": "Which relay?"}, view)
    assert asks["extra_turns"] == [] and asks["pending_turns"] == [2, 3, 4]
    both = analyzer.to_analysis("rec", ANALYSIS, view)
    assert [w["reached"] for w in both["goals"]] == [True, False, False, True] and both["extra_turns"] == [2, 3]
    turn_one_only = analyzer.reached(view, {"turn": 1, **PRESS}, until_turn=2)
    assert turn_one_only is False  # Switched off in turn 1, but not back on until turn 3.
    stays_on = analyzer.reached(view, {"turn": 1, "summary": "", "kind": "state", "entity_id": "switch.fixture_relay",
                                       "state": "on"}, until_turn=2)
    assert stays_on is False and analyzer.reached(view, {"turn": 1, "summary": "", "kind": "fact", "fact": "x"}) is None


@pytest.mark.asyncio
async def test_the_analyzer_stops_a_goal_an_existing_improvement_already_fixes(recording_hass, effect_guards):
    from custom_components.assist_improver import async_setup_entry, async_unload_entry
    hass = recording_hass
    _, client = task_entity(hass, "Done.")
    entry = fake_entry({CONF_AI_TASK: TASK}); await async_setup_entry(hass, entry)
    record = make_recording()
    await entry.runtime_data.recorder.store(record["id"]).async_save(record)
    entry.runtime_data.recorder.index[record["id"]] = {"status": record["status"]}
    improvements = entry.runtime_data.improvements
    improvements.items["imp1"] = {
        "id": "imp1", "title": "Press the relay with one script", "status": "awaiting_review", "cause": "No script",
        "changes": [{"kind": "registry", "entity_id": "switch.fixture_relay", "expose": True}], "test": tested(), "fixes": []}
    why = "The press script it adds does the off-then-on the user had to finish by hand."
    client.blocks = [[{"type": "tool_use", "id": "t1", "name": "submit_goals", "input": ANALYSIS}],
                     [{"type": "tool_use", "id": "t2", "name": "mark_duplicate",
                       "input": {"goal_turn": 1, "improvement": "imp1", "why": why}}],
                     [{"type": "text", "text": "Done."}]]
    hass.states.async_set(TASK, "unknown")  # There for the run, as Run now checks.
    try:
        pipeline, flow = entry.runtime_data.pipeline, entry.runtime_data.flow
        pipeline.start(trigger="manual")
        await pipeline._task
        assert pipeline.current["status"] == "done", pipeline.current["error"]
        first, second, _ = client.requests
        sent = json.dumps(first)
        # Code found the improvement on a device this session touched, and the analyzer was told to check.
        assert "improvements_on_these_devices" in sent and "mark_duplicate" in sent and "Mark a goal only" in sent
        assert {"list_improvements", "read_improvement", "mark_duplicate"} <= {t["name"] for t in first["tools"]}
        assert "mark_duplicate" in json.dumps(second["messages"][-1])  # The accepted answer points to the check.
        analysis = flow.round_of(record["id"])["records"]["Analyze"]["analysis"]
        assert analysis["duplicates"] == [{"goal_turn": 1, "improvement": "imp1", "why": why}]
        assert "also_addressed" not in improvements.items["imp1"]  # The flow record is where it's kept (P0.2).
        # Its only friction is already fixed: it ends a duplicate of that improvement, and nothing is proposed.
        assert {k: flow.place(record["id"])[k] for k in ("stage", "outcome", "covered_by")} == {
            "stage": "Your household", "outcome": "duplicate", "covered_by": "imp1"}
        assert effect_guards == []
    finally:
        await async_unload_entry(hass, entry)


def test_a_duplicate_mark_is_checked_by_code():
    from custom_components.assist_improver.exploration import SessionView
    view = SessionView(make_recording())
    item = {"improvement": "imp1", "title": "Press script", "status": "applied", "applied": "2999-01-01T00:00:00+00:00"}
    mark = analyzer.MarkDuplicate(view, [item])
    assert "no improvement 'nope'" in mark.run(1, "nope", "x")["problem"]
    assert "no turn 9" in mark.run(9, "imp1", "x")["problem"]
    assert mark.run(1, "imp1", "Same cause")["accepted"] and mark.run(3, "imp1", "Same")["accepted"]
    assert mark.kept(ANALYSIS) == [{"goal_turn": 1, "improvement": "imp1", "why": "Same cause"}]  # 3 isn't a goal.
    early = analyzer.MarkDuplicate(view, [{**item, "applied": "2000-01-01T00:00:00+00:00"}])
    assert "applied before this session" in early.run(1, "imp1", "Same cause")["problem"]  # It didn't stop it.


def anthropic_entry(sub_data, sub_title="Claude AI Task", domain="anthropic"):
    entry = ConfigEntry(domain=domain, data={}, title="Claude", source="user", version=1, minor_version=1,
                       options={}, unique_id=None, discovery_keys=MappingProxyType({}),
                       subentries_data=[{"subentry_type": "ai_task_data", "data": sub_data, "title": sub_title,
                                         "unique_id": None, "subentry_id": "sub-1"}])
    object.__setattr__(entry, "state", ConfigEntryState.LOADED)  # create_options offers loaded providers only.
    return entry


def task_device(hass, entry, subentry_id):
    """The device an AI Task's provider registers for it, its model named as the provider's own entity names it:
    Anthropic's and OpenAI's entity classes here; for others (the offline suite lacks their libraries), the model their
    settings name, Ollama's without its tag; OpenRouter names none. Its ID."""
    sub = entry.subentries[subentry_id]
    subentry = SimpleNamespace(subentry_id=subentry_id, subentry_type="ai_task_data", title=sub.title,
                               data=dict(sub.data))
    if entry.domain == "anthropic":  # Home Assistant's lookup names a model it doesn't list by its ID.
        coordinator = SimpleNamespace(get_model_info=lambda model: (ModelInfo(
            id=model, type="model", display_name=model, created_at=datetime(1970, 1, 1, tzinfo=UTC),
            lifecycle="active"), True))
        info = AnthropicTaskEntity(SimpleNamespace(runtime_data=coordinator), subentry).device_info
    elif entry.domain == "openai_conversation":
        info = OpenAIBaseLLMEntity(SimpleNamespace(), subentry).device_info
    else:
        model = sub.data.get("chat_model") or sub.data.get("model")
        info = {"model": model.partition(":")[0]} if model and entry.domain != "open_router" else {}
    return dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, config_subentry_id=subentry_id,
        identifiers={(entry.domain, f"{entry.entry_id}-{subentry_id}")}, name=sub.title, model=info.get("model"),
        model_id=info.get("model_id")).id


def register_task(hass, entry, entity_id="ai_task.claude_ai_task", subentry_id="sub-1"):
    hass.config_entries = SimpleNamespace(async_loaded_entries=loaded_for(hass), async_get_entry=lambda i: entry if i == entry.entry_id else None,
                                          async_entries=lambda d=None: [entry] if d in (None, "anthropic") else [])
    registry = er.async_get(hass)
    registry.async_get_or_create("ai_task", "anthropic", f"uid-{subentry_id}", config_entry=entry,
                                 config_subentry_id=subentry_id, suggested_object_id=entity_id.split(".", 1)[1],
                                 device_id=task_device(hass, entry, subentry_id))
    hass.states.async_set(entity_id, "unknown")
    return entity_id


@pytest.mark.asyncio
async def test_assessment_warns_about_weak_defaults_and_passes_strong_settings(recording_hass):
    """An Anthropic AI Task on Anthropic's recommended settings runs Home Assistant's default model (a small one
    today); that it's on those settings is said plainly, without numbers the improver can't read, and isn't judged:
    on a strong model, it works."""
    hass = recording_hass
    entity = register_task(hass, anthropic_entry({"recommended": True}))
    weak = ai_setup.assess(hass, entity)
    assert not weak["ok"] and weak["uses_recommended_defaults"]
    assert len(weak["warnings"]) == 2 and "haiku" in weak["warnings"][0].lower()
    assert weak["warnings"][1] == "It uses Anthropic's recommended settings."
    dr.async_get(hass).async_update_device(er.async_get(hass).async_get(entity).device_id,
                                           model_id=ai_setup.RECOMMENDED["chat_model"])
    opus = ai_setup.assess(hass, entity)
    assert opus["ok"] and ai_setup.is_recommended(opus)
    assert opus["warnings"] == ["It uses Anthropic's recommended settings."]
    strong = anthropic_entry({"recommended": False, **ai_setup.RECOMMENDED})
    hass2 = recording_hass
    er.async_get(hass2).async_remove(entity)
    assert ai_setup.assess(hass2, register_task(hass2, strong))["ok"]
    assert not ai_setup.assess(hass2, None)["ok"] and not ai_setup.assess(hass2, "ai_task.missing")["ok"]


@pytest.mark.asyncio
async def test_an_ai_task_on_its_providers_own_settings_names_its_model(recording_hass):
    """An OpenAI AI Task on OpenAI's recommended settings, as the improver adds one at the provider's defaults: its
    settings name no model, the device its provider registers does."""
    from homeassistant.components.openai_conversation.const import RECOMMENDED_CHAT_MODEL
    hass = recording_hass
    openai = anthropic_entry({"recommended": True}, domain="openai_conversation")  # As its flow keeps the answers.
    assessed = ai_setup.assess(hass, register_task(hass, openai))
    assert assessed["model"] == RECOMMENDED_CHAT_MODEL and not any("can't read" in w for w in assessed["warnings"])


@pytest.mark.asyncio
async def test_unknown_provider_is_allowed_with_a_note(recording_hass):
    hass = recording_hass
    hass.states.async_set("ai_task.other", "unknown")
    result = ai_setup.assess(hass, "ai_task.other")
    assert result["ok"] and "can't read" in result["warnings"][0]


@pytest.mark.asyncio
async def test_create_recommended_drives_the_providers_own_flow(recording_hass):
    hass = recording_hass
    entry = anthropic_entry({"recommended": True})
    register_task(hass, entry)
    answers = []
    steps = iter([probatio.Schema({probatio.Required("name"): str, probatio.Required("recommended"): bool}),
                  probatio.Schema({probatio.Optional("chat_model"): str, probatio.Optional("prompt_caching"): str}),
                  probatio.Schema({probatio.Optional("max_tokens"): int, probatio.Optional("effort"): str})])

    async def init(handler, context):
        return {"type": "form", "flow_id": "f", "data_schema": next(steps), "errors": None}

    async def configure(flow_id, answer):
        answers.append(answer)
        try:
            return {"type": "form", "flow_id": "f", "data_schema": next(steps), "errors": None}
        except StopIteration:
            entry.subentries = {**entry.subentries, "sub-2": SimpleNamespace(
                subentry_type="ai_task_data", title=ai_setup.RECOMMENDED_NAME,
                data={"recommended": False, **ai_setup.RECOMMENDED})}
            register_task(hass, entry, "ai_task.assist_improver", "sub-2")
            return {"type": "create_entry"}

    hass.config_entries.subentries = SimpleNamespace(async_init=init, async_configure=configure)
    entity, reason = await ai_setup.create_ai_task(hass, entry.entry_id, "recommended")
    assert (entity, reason) == ("ai_task.assist_improver", None)
    assert answers == [{"name": "Assist Improver", "recommended": False}, {"chat_model": "claude-opus-5-5"},
                       {"max_tokens": 16000, "effort": "high"}]


@pytest.mark.asyncio
async def test_create_recommended_stops_cleanly_when_the_flow_objects(recording_hass):
    hass = recording_hass
    entry = anthropic_entry({"recommended": True}); register_task(hass, entry)

    async def init(handler, context):
        return {"type": "form", "flow_id": "f", "data_schema": probatio.Schema({}), "errors": {"base": "entry_not_loaded"}}

    closed = []
    hass.config_entries.subentries = SimpleNamespace(async_init=init, async_configure=None, async_abort=closed.append)
    entity, reason = await ai_setup.create_ai_task(hass, entry.entry_id, "recommended")
    assert entity is None and "stopped" in reason
    assert closed == ["f"]  # The provider's flow isn't left open.


def _steps(hass, options=None, first_setup=False):
    """The setup steps first setup and the Repairs fix share, on an entry with these options."""
    from homeassistant.data_entry_flow import FlowHandler
    from custom_components.assist_improver.config_flow import _AiTaskSteps

    class Steps(_AiTaskSteps, FlowHandler):
        _first_setup = first_setup

        async def async_step_init(self, user_input=None):
            return await self._choose(user_input, (options or {}).get(CONF_AI_TASK), "init")

        async def _back(self):
            return await self.async_step_init()

        def _finish(self, choice):
            return self.async_create_entry(data=self._options(options or {}, choice))

    flow = Steps()
    flow.hass, flow.handler = hass, "fixture"
    return flow


@pytest.mark.asyncio
async def test_setup_confirms_weak_choice_and_saves_strong_one(recording_hass, monkeypatch):
    hass = recording_hass
    weak = register_task(hass, anthropic_entry({"recommended": True}))
    flow = _steps(hass)
    form = await flow.async_step_init()
    labels, default = _ai_list(form)
    assert form["type"] == "form" and list(labels)[0].endswith(":recommended") and default == list(labels)[0]
    assert labels[weak].startswith("Claude Haiku") and labels[weak].endswith(" · not recommended")  # Model first.
    assert form["description_placeholders"]["guidance"] == ""  # The recommended pick needs no explaining.
    confirm = await flow.async_step_init({CONF_AI_TASK: weak})
    assert confirm["step_id"] == "confirm" and "haiku" in confirm["description_placeholders"]["warnings"].lower()
    assert confirm["type"] == "menu" and confirm["menu_options"] == ["use_anyway", "choose_again"]
    back = await flow.async_step_choose_again()
    assert back["step_id"] == "init"
    saved = await flow.async_step_use_anyway()
    assert saved["type"] == "create_entry" and saved["data"] == {CONF_AI_TASK: weak, CONF_ACCEPTED_WEAK: weak}
    monkeypatch.setattr(ai_setup, "create_ai_task", AsyncMock(return_value=(None, "provider said no.")))
    failed = await flow.async_step_init({CONF_AI_TASK: list(labels)[0]})
    assert failed["errors"] == {"base": "create_failed"} and failed["description_placeholders"]["reason"] == "provider said no."


@pytest.mark.asyncio
async def test_first_setup_gives_the_quick_steps_a_small_model(recording_hass, monkeypatch):
    from custom_components.assist_improver import settings
    from custom_components.assist_improver.const import CONF_TRIAGE_AI_TASK
    hass = recording_hass
    provider = anthropic_entry({"recommended": True})
    register_task(hass, provider, "ai_task.claude_haiku")  # The provider's defaults: a small model.
    created = []

    async def create(hass, entry_id, kind, model=None, name=None):
        created.append((kind, model))
        return ("ai_task.assist_improver", None)

    monkeypatch.setattr(ai_setup, "create_ai_task", create)
    monkeypatch.setattr(ai_setup, "assess", lambda hass, e: {"ok": True, "warnings": [], "model": {
        "ai_task.claude_haiku": "claude-haiku-5-5"}.get(e, "claude-opus-5-5")})
    flow = _steps(hass, first_setup=True)
    saved = await flow.async_step_init({CONF_AI_TASK: f"create:{provider.entry_id}:recommended"})
    # An AI Task with a small model is reused for Triage and Write up; only Opus is added.
    assert created == [("recommended", None)]
    assert saved["data"] == {CONF_AI_TASK: "ai_task.assist_improver", CONF_TRIAGE_AI_TASK: "ai_task.claude_haiku",
                             "editor_ai_task_entity_id": "ai_task.claude_haiku"}
    # None to reuse: one is added beside the new entry, on the same provider.
    monkeypatch.setattr(ai_setup, "assess", lambda hass, e: {"ok": True, "warnings": [], "model": "claude-opus-5-5"})
    monkeypatch.setattr(settings, "small_ai_task", AsyncMock(return_value=("ai_task.assist_improver_haiku", None)))
    new = {CONF_AI_TASK: f"create:{provider.entry_id}:recommended"}
    saved = await _steps(hass, first_setup=True).async_step_init(new)
    settings.small_ai_task.assert_awaited_once_with(hass, provider.entry_id)
    assert saved["data"][CONF_TRIAGE_AI_TASK] == "ai_task.assist_improver_haiku"
    # The Repairs fix (not first setup) changes only the main model.
    saved = await _steps(hass).async_step_init({CONF_AI_TASK: f"create:{provider.entry_id}:recommended"})
    assert saved["data"] == {CONF_AI_TASK: "ai_task.assist_improver"}


@pytest.mark.asyncio
async def test_configure_links_to_the_panels_settings(recording_hass):
    from custom_components.assist_improver.config_flow import AssistantImproverOptionsFlow
    flow = AssistantImproverOptionsFlow(); flow.hass, flow.handler = recording_hass, "fixture"
    shown = await flow.async_step_init()
    assert shown["type"] == "abort" and shown["reason"] == "settings_in_panel"
    assert shown["description_placeholders"] == {"link": "/assist-improver?view=settings"}


async def _start(hass):
    from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()


def _ai_list(form):
    """The AI model list's {value: label} and default."""
    key, selector = next((k, s) for k, s in form["data_schema"].schema.items() if str(k) == "ai_task_entity_id")
    default = key.default
    return {o["value"]: o["label"] for o in selector.config["options"]}, default() if callable(default) else None


@pytest.mark.asyncio
async def test_a_good_current_ai_task_stays_selected(recording_hass, monkeypatch):
    hass = recording_hass
    register_task(hass, anthropic_entry({"recommended": True}))
    hass.states.async_set("ai_task.other", "unknown", {"friendly_name": "Other AI Task"})
    form = await _steps(hass, {CONF_AI_TASK: "ai_task.other"}).async_step_init()
    labels, default = _ai_list(form)
    assert default == "ai_task.other" and labels["ai_task.other"] == "Other AI Task"
    assert any(v.endswith(":recommended") for v in labels)


def _providers(hass, *entries):
    hass.config_entries = SimpleNamespace(async_loaded_entries=loaded_for(hass), async_get_entry={e.entry_id: e for e in entries}.get,
                                          async_entries=lambda d=None: [e for e in entries if d in (None, e.domain)])


async def _options_form(hass, monkeypatch, options=None):
    return await _steps(hass, options).async_step_init()


@pytest.mark.asyncio
async def test_no_new_ai_task_is_offered_once_a_recommended_one_exists(recording_hass, monkeypatch):
    hass = recording_hass
    strong = register_task(hass, anthropic_entry({"recommended": False, **ai_setup.RECOMMENDED}, "Assist Improver"),
                           "ai_task.assist_improver")
    form = await _options_form(hass, monkeypatch)
    labels, default = _ai_list(form)
    assert not any(v.startswith("create:") for v in labels)
    assert default == strong and labels[strong] == "Claude Opus 5.5 · ai_task.assist_improver · recommended"


@pytest.mark.asyncio
async def test_providers_offer_recommended_or_default_new_ai_tasks(recording_hass, monkeypatch):
    hass = recording_hass
    router = anthropic_entry({"model": "openai/gpt-5-mini"}, "GPT", domain="open_router")
    openai = anthropic_entry({}, domain="openai_conversation")
    google = anthropic_entry({}, domain="google_generative_ai_conversation")
    object.__setattr__(google, "state", ConfigEntryState.SETUP_ERROR)  # Not loaded: not offered.
    _providers(hass, router, openai, google)
    options = ai_setup.create_options(hass)
    assert [(k, label) for _, k, label in options] == [
        ("recommended", "Claude Opus 5.5 via OpenRouter · new · recommended"),
        ("default", "OpenAI's default model · new")]
    form = await _options_form(hass, monkeypatch)
    labels, default = _ai_list(form)
    assert default == f"create:{router.entry_id}:recommended" and "via OpenRouter" in labels[default]


@pytest.mark.asyncio
async def test_default_settings_are_preselected_without_a_recommended_provider(recording_hass, monkeypatch):
    hass = recording_hass
    openai = anthropic_entry({}, domain="openai_conversation")
    _providers(hass, openai)
    form = await _options_form(hass, monkeypatch)
    labels, default = _ai_list(form)
    assert default == f"create:{openai.entry_id}:default" and labels[default] == "OpenAI's default model · new"
    assert "Claude Opus 5.5" in form["description_placeholders"]["guidance"]
    _providers(hass)
    empty = await _options_form(hass, monkeypatch)
    assert _ai_list(empty)[0] == {} and "add an AI provider" in empty["description_placeholders"]["guidance"]


@pytest.mark.asyncio
async def test_openrouter_creation_picks_claude_opus_or_explains(recording_hass):
    from homeassistant.helpers.selector import SelectSelector, SelectSelectorConfig
    hass = recording_hass
    router = anthropic_entry({"model": "fixture/model"}, domain="open_router")
    _providers(hass, router)

    def models(*ids):
        return probatio.Schema({probatio.Required("model"): SelectSelector(SelectSelectorConfig(
            options=[{"value": i, "label": i} for i in ids]))})

    answers = []

    async def configure(flow_id, answer):
        answers.append(answer)
        router.subentries = {**router.subentries, "sub-2": SimpleNamespace(subentry_type="ai_task_data", title="Opus",
                                                                           data=answer)}
        er.async_get(hass).async_get_or_create("ai_task", "open_router", "uid-sub-2", config_entry=router,
                                               config_subentry_id="sub-2", suggested_object_id="claude_opus")
        hass.states.async_set("ai_task.claude_opus", "unknown")
        return {"type": "create_entry"}

    async def init(handler, context):
        return {"type": "form", "flow_id": "f", "data_schema": models("openai/gpt-5", "anthropic/claude-opus-5.5"),
                "errors": None}

    hass.config_entries.subentries = SimpleNamespace(async_init=init, async_configure=configure)
    entity, reason = await ai_setup.create_ai_task(hass, router.entry_id, "recommended")
    assert reason is None and entity == "ai_task.claude_opus" and answers == [{"model": "anthropic/claude-opus-5.5"}]

    async def init_without(handler, context):
        return {"type": "form", "flow_id": "f", "data_schema": models("openai/gpt-5"), "errors": None}

    closed = []
    hass.config_entries.subentries = SimpleNamespace(async_init=init_without, async_configure=configure,
                                                     async_abort=closed.append)
    entity, reason = await ai_setup.create_ai_task(hass, router.entry_id, "recommended")
    assert entity is None and "isn't available" in reason and closed == ["f"]


@pytest.mark.asyncio
async def test_openrouter_opus_counts_as_recommended(recording_hass):
    hass = recording_hass
    opus = register_task(hass, anthropic_entry({"model": "anthropic/claude-opus-5.5"}, domain="open_router"))
    assert ai_setup.is_recommended(ai_setup.assess(hass, opus))


@pytest.mark.asyncio
async def test_other_openrouter_models_work_with_a_note(recording_hass):
    hass = recording_hass
    other = ai_setup.assess(hass, register_task(hass, anthropic_entry({"model": "deepseek/deepseek-v4.1-flash"},
                                                                      domain="open_router")))
    assert other["ok"] and not ai_setup.is_recommended(other) and "Opus 5.5 is recommended" in other["warnings"][0]


@pytest.mark.asyncio
async def test_setup_raises_a_repairs_notice_until_a_good_ai_task_is_chosen(recording_hass):
    from custom_components.assist_improver import async_setup_entry, async_unload_entry
    hass = recording_hass
    entry = fake_entry(); await async_setup_entry(hass, entry)
    try:
        assert ir.async_get(hass).async_get_issue(DOMAIN, "ai_task_setup") is None  # Judged once HA has started.
        await _start(hass)
        assert ir.async_get(hass).async_get_issue(DOMAIN, "ai_task_setup") is not None
    finally:
        await async_unload_entry(hass, entry)
    hass.states.async_set("ai_task.other", "unknown")
    entry = fake_entry({CONF_AI_TASK: "ai_task.other"}); await async_setup_entry(hass, entry)
    try:
        await _start(hass)
        assert ir.async_get(hass).async_get_issue(DOMAIN, "ai_task_setup") is None
    finally:
        await async_unload_entry(hass, entry)


@pytest.mark.asyncio
async def test_repairs_notice_is_fixed_in_place_and_stops_after_an_accepted_choice(recording_hass):
    from custom_components.assist_improver import _update_setup_issue, repairs
    hass = recording_hass
    anthropic = anthropic_entry({"recommended": True})
    weak = register_task(hass, anthropic)
    improver = fake_entry()
    entries = {anthropic.entry_id: anthropic, improver.entry_id: improver}
    hass.config_entries.async_get_entry = entries.get
    hass.config_entries.async_update_entry = lambda entry, options: setattr(entry, "options", options)

    _update_setup_issue(hass, improver)
    issue = ir.async_get(hass).async_get_issue(DOMAIN, "ai_task_setup")
    assert issue.is_fixable and issue.data == {"entry_id": improver.entry_id}
    flow = await repairs.async_create_fix_flow(hass, "ai_task_setup", issue.data)
    flow.hass, flow.handler = hass, DOMAIN
    form = await flow.async_step_init(issue.data)  # HA passes the issue's data to the first step.
    assert form["step_id"] == "choose" and _ai_list(form)[1].endswith(":recommended")
    confirm = await flow.async_step_choose({CONF_AI_TASK: weak})
    assert confirm["step_id"] == "confirm"
    done = await flow.async_step_use_anyway()
    assert done["type"] == "create_entry" and improver.options == {CONF_AI_TASK: weak, CONF_ACCEPTED_WEAK: weak}
    _update_setup_issue(hass, improver)
    assert ir.async_get(hass).async_get_issue(DOMAIN, "ai_task_setup") is None

    improver.options = {CONF_AI_TASK: weak}  # Chosen elsewhere without accepting the warning.
    _update_setup_issue(hass, improver)
    assert ir.async_get(hass).async_get_issue(DOMAIN, "ai_task_setup") is not None
    flow = await repairs.async_create_fix_flow(hass, "ai_task_setup", issue.data)
    flow.hass, flow.handler = hass, DOMAIN
    nothing = await flow.async_step_choose({})  # Cleared the choice: the notice stays.
    assert nothing["type"] == "abort" and improver.options == {CONF_AI_TASK: weak}


def test_fix_flow_strings_cover_every_step():
    strings = json.load(open("custom_components/assist_improver/strings.json"))
    fix = strings["issues"]["ai_task_setup"]["fix_flow"]
    assert set(fix["step"]) == {"choose", "confirm"}
    # The options flow only aborts (its settings are in the panel); hassfest still wants its (empty) step mapping.
    assert strings["options"]["step"] == {} and set(strings["options"]) == {"abort", "step"}
    assert "{link}" in strings["options"]["abort"]["settings_in_panel"]
    assert "no_choice" in fix["abort"] and "create_failed" in fix["error"]
    # Improvements are reviewed in the panel; Repairs has setup gaps, and what a Home Assistant update broke.
    assert set(strings["issues"]) == {"ai_task_setup", "step_models", *touchpoints.ISSUES}
    assert {"{steps}", "{link}"} <= set(re.findall(r"\{\w+\}", strings["issues"]["step_models"]["description"]))
    assert strings == json.load(open("custom_components/assist_improver/translations/en.json"))


@pytest.mark.asyncio
async def test_notice_clears_when_the_chosen_ai_task_appears_after_setup(recording_hass):
    from custom_components.assist_improver import async_setup_entry, async_unload_entry
    hass = recording_hass
    unloads = []
    entry = fake_entry({CONF_AI_TASK: "ai_task.other"})
    entry.async_on_unload = unloads.append
    await async_setup_entry(hass, entry)
    try:
        await _start(hass)
        assert ir.async_get(hass).async_get_issue(DOMAIN, "ai_task_setup") is not None  # Provider not loaded yet.
        hass.states.async_set("ai_task.other", "unknown")
        await hass.async_block_till_done()
        assert ir.async_get(hass).async_get_issue(DOMAIN, "ai_task_setup") is None
    finally:
        for unsub in unloads:
            unsub()
        await async_unload_entry(hass, entry)


@pytest.mark.asyncio
async def test_an_unmarked_ai_task_call_isnt_recorded_either(recording_hass, effect_guards):
    """A household automation's AI Task call, without the role marker, isn't one of the household's Assist
    conversations, so it isn't recorded either (P8.1)."""
    from homeassistant.components import ai_task
    from custom_components.assist_improver import async_setup_entry, async_unload_entry
    hass = recording_hass
    task_entity(hass, ANALYSIS)
    entry = fake_entry({CONF_AI_TASK: TASK}); await async_setup_entry(hass, entry)
    try:
        await ai_task.async_generate_data(hass, task_name="household", entity_id=TASK,
                                          instructions="Some household automation's task", structure=analyzer.STRUCTURE)
        await hass.async_block_till_done()
        recorder = entry.runtime_data.recorder
        assert not recorder.sessions and not recorder.index and recorder.ignored and effect_guards == []
    finally:
        await async_unload_entry(hass, entry)


def test_submit_goals_keeps_the_best_answer():
    submit = analyzer.SubmitGoals(SessionView(make_recording()))
    assert submit.best() == ("no_answer_submitted", None, [])
    two_wrong = {**ANALYSIS, "goals": [{**g, "states": ["off", "pressed"], "entity_id": "switch.nope"} for g in ANALYSIS["goals"]]}
    one_wrong = {**ANALYSIS, "goals": [{**ANALYSIS["goals"][0], "states": ["off", "pressed"]}, *ANALYSIS["goals"][1:]]}
    assert submit.run(**two_wrong)["accepted"] is False and submit.run(**one_wrong)["accepted"] is False
    status, answer, found = submit.best()
    assert status == "best_rejected" and answer == one_wrong and len(found) == 1
    assert submit.run(**ANALYSIS) == {"accepted": True, "message": "Accepted. Finish with one short sentence."}
    assert submit.best() == ("accepted", ANALYSIS, [])


def test_ask_goals_are_validated_and_count_as_goals():
    view = SessionView(make_recording())
    ask = {"turn": 1, "summary": "Which relay setting?", "kind": "ask", "entity_ids": ["switch.fixture_relay"]}
    later = [{"turn": g["turn"], "summary": g["summary"], "kind": "step", "serves": 1}  # The turns after it serve it
             for g in ANALYSIS["goals"][1:]]                                                # (P0.3): steps towards it.
    answer = {"goals": [ask, *later], "not_requests": [], "question_for_user": ""}
    assert analyzer.problems(view, answer) == []
    assert analyzer.to_analysis("rec", answer, view)["extra_turns"] == [2, 3, 4]
    assert "reached" in analyzer.to_analysis("rec", answer, view)["goals"][0]
    empty = {**answer, "goals": [{**ask, "entity_ids": []}, *later]}
    assert "needs entity_ids" in analyzer.problems(view, empty)[0]
    unknown = {**answer, "goals": [{**ask, "entity_ids": ["switch.nope"]}, *later]}
    assert "no device 'switch.nope'" in analyzer.problems(view, unknown)[0]


def test_a_thanks_is_a_sub_goal_met_once_said_and_no_extra_turn():
    view = SessionView(make_recording())
    thanks = {"turn": 4, "summary": "Thanked Assist", "kind": "thanks", "serves": 1}
    answer = {**ANALYSIS, "goals": [*ANALYSIS["goals"][:3], thanks]}
    assert analyzer.problems(view, answer) == []
    analysis = analyzer.to_analysis("rec", answer, view)
    assert analysis["goals"][3] == {**thanks, "reached": True} and analysis["extra_turns"] == [2, 3]
    unserved = {**answer, "goals": [*ANALYSIS["goals"][:3], {k: v for k, v in thanks.items() if k != "serves"}]}
    assert any("a thanks is a sub-goal" in p for p in analyzer.problems(view, unserved))
    label = {"recording_id": "rec", "not_requests": [],  # A label's fact at that turn takes the thanks.
             "goals": [{"turn": 1, **PRESS}, {"turn": 4, "summary": "Thanks", "kind": "fact", "fact": "Thanks"}]}
    assert score_recording(analysis, label, 4)["passed"]


def test_a_fact_says_what_its_answer_needs_the_devices_it_is_about_and_the_form_asked_for_in_their_words():
    """DETAILS P9.7.4: a fact root names the devices its answer is about (ones the household has) and quotes the form
    the user asked for, if any, from their own words; the analysis keeps all three."""
    view = SessionView(make_recording())
    fact = {"turn": 4, "summary": "Whether the relay is on", "kind": "fact", "fact": "The relay's state",
            "entity_ids": ["switch.fixture_relay"], "quote": "off then on"}
    answer = {**ANALYSIS, "goals": [*ANALYSIS["goals"][:3], fact]}
    assert analyzer.problems(view, answer) == []
    assert analyzer.to_analysis("rec", answer, view)["goals"][3] == {**fact, "reached": None}
    unknown = {**answer, "goals": [*answer["goals"][:3], {**fact, "entity_ids": ["switch.nowhere"]}]}
    assert analyzer.problems(view, unknown) == [
        "Goal 4 (turn 4): no device 'switch.nowhere' in this household; find_devices finds every entity ID."]
    paraphrased = {**answer, "goals": [*answer["goals"][:3], {**fact, "quote": "briefly"}]}
    assert analyzer.problems(view, paraphrased) == [
        "Goal 4 (turn 4): quote must be the form they asked for in their own words from turn 4, copied exactly; leave it "
        "out when they asked for none."]


def test_an_action_names_its_outcome_and_the_devices_it_is_for():
    """An action leaves no device in a new state (a message spoken on speakers): its destination is the outcome and
    the devices it's for, each one the household has, whatever the words (DETAILS P9.7.6)."""
    view = SessionView(make_recording())
    said = {"turn": 4, "summary": "Say it on the relay", "kind": "action", "outcome": "announce",
            "entity_ids": ["switch.fixture_relay"]}
    answer = {**ANALYSIS, "goals": [*ANALYSIS["goals"][:3], said]}
    assert analyzer.problems(view, answer) == []
    assert analyzer.to_analysis("rec", answer, view)["goals"][3] == {**said, "reached": False}  # Nothing spoken.
    nowhere = {**answer, "goals": [*answer["goals"][:3], {**said, "entity_ids": []}]}
    assert analyzer.problems(view, nowhere) == ["Goal 4 (turn 4): an action goal needs entity_ids: the devices it's for."]
    unknown = {**answer, "goals": [*answer["goals"][:3], {**said, "entity_ids": ["media_player.nowhere"]}]}
    assert analyzer.problems(view, unknown) == [
        "Goal 4 (turn 4): no device 'media_player.nowhere' in this household; find_devices finds every entity ID."]
    unsaid = {**answer, "goals": [*answer["goals"][:3], {k: v for k, v in said.items() if k != "outcome"}]}
    assert analyzer.problems(view, unsaid) == ["Goal 4 (turn 4): an action goal needs outcome: announce."]
    later = {**answer, "goals": [*answer["goals"][:3], {**said, "serves": 1}]}
    assert any("an action asked at a later turn is a goal of its own" in p for p in analyzer.problems(view, later))


def test_speaking_on_a_device_wants_it():
    """What speaking leaves on a speaker in its request's turns isn't a side effect, but what Assist does to it for
    another request is."""
    said = {"turn": 1, "summary": "Say it", "kind": "action", "outcome": "announce"}
    speaker = "media_player.speaker"
    home = SimpleNamespace(source={"states": {speaker: {"state": "off"}}}, changes={speaker: [
        {"during_turn": 1, "by": "assistant", "old": "off", "new": "playing"},
        {"during_turn": 1, "by": "assistant", "old": "playing", "new": "idle"}]})
    spoken = {**said, "entity_ids": [speaker]}
    lamp = {"turn": 3, "summary": "Lamp on", "kind": "state", "entity_id": "light.lamp", "state": "on"}
    assert analyzer.side_effects_left(home, [spoken, lamp]) == []  # Left idle by what it spoke.
    home.changes[speaker].append({"during_turn": 3, "by": "assistant", "old": "idle", "new": "on"})
    assert analyzer.side_effects_left(home, [spoken, lamp]) == [
        {"entity_id": speaker, "start": "off", "end": "on", "turn": 3}]  # Turned on for the lamp's request.


def test_the_analysis_gives_each_goal_the_state_the_measures_give_it_so_the_notes_goal_is_unmet():
    view = SessionView(make_recording())
    asked = {"turn": 4, "summary": "Asked what it did", "kind": "fact", "fact": "what it did"}
    noted = {"turn": 5, "summary": "The note", "kind": "conduct", "about": "all", "quote": "Press it once", "serves": 1}
    answer = {**ANALYSIS, "goals": [*ANALYSIS["goals"][:3], asked, noted]}
    analysis = analyzer.to_analysis("rec", answer, view)
    assert [g["reached"] for g in analysis["goals"]] == [True, False, False, None, False]  # Each reaction unmet.


def test_the_quick_check_gets_all_the_context_assist_had():
    from custom_components.assist_improver import triage
    record = make_recording()
    shown = triage.payload(SessionView(record))
    first = shown["turns"][0]
    assert first["assist_instructions"] == record["turns"][0]["preparations"][0]["content"][0]["content"]
    assert any("assist_calls" in s for s in first["steps"]) and any("home_assistant_returns" in s for s in first["steps"])
    assert "switch.fixture_relay" in {d["entity_id"] for d in shown["devices_involved"]}


def test_the_quick_check_gets_assists_instructions_once_and_then_only_what_changed():
    from custom_components.assist_improver import triage
    shown = triage.payload(SessionView(make_recording()))["turns"]
    assert "assist_instructions" in shown[0] and all("assist_instructions" not in t for t in shown[1:])
    first, later = "Alex is speaking.\nThe time is 10:00.", "Alex is speaking.\nThe time is 10:01."
    assert triage._instructions(None, first) == {"assist_instructions": first}
    assert triage._instructions(first, first) == {} and triage._instructions(first, None) == {}
    assert triage._instructions(first, later) == {"assist_instructions_changed": {
        "no_longer": ["The time is 10:00."], "now": ["The time is 10:01."]}}


def test_a_revision_note_is_a_turn_after_the_others_holding_a_step_or_a_conduct_goal_under_the_root_it_revises():
    """DETAILS P12.8.1: a revision's round reads the conversation afresh, its revision note a turn after those said
    and after the note it was sent in with; the note's goals are a step or a conduct goal (quoting the note, about a
    turn or all) serving the root goal its improvement was for, and a note with none is refused."""
    view = SessionView(make_recording())  # Four turns said: the press at 1, again at 4.
    note, revision = "Press it twice", {"note": "Use the press script, not a household line", "roots": [4]}
    goals = [*ANALYSIS["goals"], {"turn": 5, "summary": "The note", "kind": "conduct", "about": "all",
                                  "quote": note, "serves": 1}]
    answer = lambda *more: {"goals": [*goals, *more], "not_requests": [], "question_for_user": ""}  # noqa: E731
    check = lambda a: analyzer.problems(view, a, note, revisions=[revision])  # noqa: E731
    objection = {"turn": 6, "summary": "Not a line", "kind": "conduct", "about": "all", "quote": "not a household line",
                 "serves": 4}
    assert check(answer(objection)) == []
    assert check(answer())[0].startswith("Turns 6 have no goal: give every turn at least one goal or sub-goal")
    wrong = "Goal 6 (turn 6): a revision note holds a step or a conduct goal, serving a goal its improvement was for: turn 4."
    assert wrong in check(answer({**objection, "serves": 1}))  # Under the root it didn't fix.
    assert wrong in check(answer({**objection, "kind": "wrap-up"}))
    step = {"turn": 6, "summary": "Use the press script", "kind": "step", "serves": 4}
    assert "already done by turn 6" in " ".join(check(answer(step)))  # As any step: the press was reached by then.
    noted = {**objection, "about": 5}  # The note it was sent in with: no reply of Assist's to object to.
    assert any("said to the assistant" in p for p in check(answer(noted)))
    moved = lambda roots: analyzer.problems(view, answer({**objection, "serves": 1}), note,  # noqa: E731
                                            revisions=[{**revision, "roots": roots}])
    assert moved([7]) == [] and moved([4]) != []  # None of its roots is one now: under any root, not refused.
    fixing = {**answer(), "about_the_fix": [{"turn": 6, "what": "A script, not a household line"}]}
    assert check(fixing) == []  # Only about the improvement itself: no goal, listed apart (P12.8.1).
    assert ("Turn 5 isn't a revision note: about_the_fix is for a revision note only about the improvement itself."
            in check({**fixing, "about_the_fix": [{"turn": 5, "what": "Press it twice"}]}))
    assert check({**answer(objection), "about_the_fix": fixing["about_the_fix"]}) == [
        "Turn 6 is in about_the_fix but has goals: a note only about the improvement holds none."]
    assert check({**answer(objection), "about_the_fix": None}) == []
    assert check(answer())[-1].endswith("a revision note only about the improvement itself goes in about_the_fix "
                                        "instead.")
    kept = analyzer.to_analysis("rec", fixing, view, [{"sent": True}, {"revision": 2}])
    from custom_components.assist_improver.hypotheses import scenario
    assert kept["about_the_fix"] == fixing["about_the_fix"] and scenario(kept, view)["revision_notes"] == [
        {"round": 2, "about_the_fix": "A script, not a household line"}]  # The proposer reads it.
    assert view.read_turn(6) == {"error": "There is no turn 6; turns are 1 to 4."}  # The analyzer's tools hear it
    from custom_components.assist_improver.analysis import analyzer_input  # once it's in their input.
    instructions, payload, *_ = analyzer_input(None, "rec", view, {"note": note, "how": "filed"}, [
        {"round": 2, **revision, "title": "Press it once"}])
    assert analyzer.REVISION in instructions and payload["conversation"][-1] == view.read_turn(6) == {
        "turn": 6, "user_says": revision["note"], "revision_of": {"improvement": "Press it once", "changes": [],
                                                                  "for_goals_at_turns": [4]}}


def test_a_revision_recorded_before_its_note_was_analysed_still_names_its_improvement_and_the_roots_it_fixed():
    """A revision record from an earlier build ({improvement, note, goal}) is read as any: the improvement's title as
    the household reads it, its changes, and the root goals its test helped (its fixes), from the improvement."""
    from custom_components.assist_improver.flow import Flow
    item = {"id": "imp", "title": "Press it once", "copy": {"headline": "One press"}, "changes": [{"kind": "script"}],
            "fixes": ["friction:4"]}
    stub = SimpleNamespace(runtime=SimpleNamespace(improvements=SimpleNamespace(items={"imp": item})),
                           _latest_analysis=lambda conv: ANALYSIS)
    stub._roots_fixed = lambda conv, found: Flow._roots_fixed(stub, conv, found)
    conv = {"rounds": {"1": {"records": {"Your Assist": {}, "Analyze": {"analysis": ANALYSIS}}},
                       "2": {"records": {"Propose": {"revision": {"improvement": "imp", "note": "Twice",
                                                                  "goal": {"kind": "conduct"}}}}}}}
    assert Flow.revisions(stub, conv) == [{"round": 2, "note": "Twice", "title": "One press",
                                           "changes": [{"kind": "script"}], "roots": [4]}]
