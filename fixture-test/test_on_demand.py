"""The fast lane: Assist can file the conversation it's in when the user asks, and each filed
conversation gets a run of its own as soon as it ends, without the quick check (filed from Assist: a clear sign,
test_sent_in.py). Who each run admits is the graph's (test_p0_7)."""

from types import SimpleNamespace

import pytest

from kit import improver, variant  # noqa: F401 - improver is a fixture.
from custom_components.assist_improver import llm as improver_llm
from custom_components.assist_improver.reflective import role_marker


async def file(improver, rid, note="It should have pressed the relay straight away."):
    """As Assist's file_improvement tool sends one in (Recorder.file: flow.Flow.sent_in)."""
    await improver.runtime.flow.sent_in(rid, note, via="assist")


@pytest.mark.asyncio
async def test_assist_gets_the_tool(improver):
    from homeassistant.helpers import llm
    hass, context = improver.hass, SimpleNamespace(context=None)
    [tool] = improver_llm.async_get_tools(hass, context, llm.LLM_API_ASSIST).tools
    assert tool.name == "assist_improver__file_improvement"
    assert improver_llm.async_get_tools(hass, context, "another_api") is None
    with role_marker("journey_analyzer"):  # The improver's own roles and replays never file anything.
        assert improver_llm.async_get_tools(hass, context, llm.LLM_API_ASSIST) is None


@pytest.mark.asyncio
async def test_filing_marks_the_conversation_assist_is_in(improver, monkeypatch):
    from homeassistant.components.conversation.chat_log import current_chat_log
    recorder = improver.recorder
    recorder.keys["conv-1"] = "live0001"
    recorder.sessions["live0001"] = {"id": "live0001", "status": "active", "turns": []}
    recorder.index["live0001"] = {"id": "live0001", "status": "active", "user_turns": 1}
    monkeypatch.setattr(recorder, "schedule", lambda *a, **k: None)
    token = current_chat_log.set(SimpleNamespace(conversation_id="conv-1"))
    try:
        answer = await improver_llm.FileImprovement().async_call(
            improver.hass, SimpleNamespace(tool_args={"what_to_improve": "  Press it in one go.  "}),
            SimpleNamespace(context=SimpleNamespace(user_id="alex")))
        unknown = SimpleNamespace(conversation_id="not-recorded")
        current_chat_log.set(unknown)
        refused = await improver_llm.FileImprovement().async_call(improver.hass, SimpleNamespace(tool_args={}), None)
    finally:
        current_chat_log.reset(token)
    assert answer.data["filed"] is True and refused.data["filed"] is False  # A ToolResult, as HA asks of tools.
    filed = improver.runtime.flow.sent("live0001")  # One mark, on its flow record.
    assert filed["note"] == "Press it in one go." and filed["by"] == "alex" and filed["via"] == "assist"


@pytest.mark.asyncio
async def test_a_filed_conversation_runs_as_soon_as_it_ends_or_right_after_the_run_in_progress(improver):
    recorder, pipeline = improver.recorder, improver.runtime.pipeline
    improver.entry.options = {**improver.entry.options, "paused_stages": ["propose", "test", "write"]}
    await improver.arrives(improver.friction)
    await file(improver, "friction01")
    recorder._closed("friction01")
    assert pipeline.running and pipeline.current["trigger"] == "filed"
    await improver.arrives(variant(improver.friction, "second001", "Could you toggle the fixture relay?"))
    await file(improver, "second001")
    recorder._closed("second001")  # A run is in progress: it runs again right after.
    first = pipeline._task
    await first
    assert pipeline.running and pipeline._task is not first
    await pipeline._task
    assert all(improver.runtime.flow.analysis(rid) for rid in ("friction01", "second001"))


@pytest.mark.asyncio
async def test_home_assistants_own_tool_gathering_offers_it_to_assist(improver):
    from homeassistant.components import llm as ha_llm
    from homeassistant.helpers import llm
    from homeassistant.setup import async_setup_component
    hass = improver.hass
    if "llm" not in hass.config.components:
        assert await async_setup_component(hass, "llm", {})
    hass.config.components.add("assist_improver")  # Loaded, as on a real Home Assistant.
    context = SimpleNamespace(context=None, device_id=None, assistant="conversation", language="en",
                              platform="conversation")
    names = lambda result: {t.name for t in result.tools}  # noqa: E731
    assert "assist_improver__file_improvement" in names(await ha_llm.async_get_tools(hass, context,
                                                                                     llm.LLM_API_ASSIST))
