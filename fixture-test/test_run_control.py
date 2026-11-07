"""Run control (pipeline.py) over a real flow run (flow.py): a run refused while a step it needs has no model, how a
run in progress ends when something outside it stops it, and the fast lane promised to one sent in meanwhile. The AI
roles and the household's assistant are scripted (kit.py)."""

import asyncio
from types import SimpleNamespace

import pytest

from kit import improver, seat, variant  # noqa: F401 - improver is a fixture.
from test_changeset import _ws
from test_conversations import ask
from custom_components.assist_improver import panel, settings
from custom_components.assist_improver.const import CONF_AI_TASK, CONF_CHECK_AI_TASK, CONF_PAUSED

EDITOR = settings.ROLES["editor"]["key"]


@pytest.mark.asyncio
async def test_unloading_stops_a_run_in_progress_before_the_stores_close(improver):
    """Unloading (async_unload_entry) stops a run in progress before the stores close: it's cut off, not the
    household's Cancel, so the run ends interrupted, and the conversation it was working on is held where it was
    (P0.8). Both are kept by the time stop() returns, so nothing writes after the stores close."""
    runtime, pipeline = improver.runtime, improver.runtime.pipeline
    thinking = asyncio.Event()

    async def triage(hass, payload, tools):  # The quick check, still thinking when the integration unloads.
        thinking.set()
        await asyncio.Event().wait()
    improver.model.triage = triage
    await improver.arrives(improver.friction)
    pipeline.start(trigger="manual")
    await asyncio.wait_for(thinking.wait(), 10)
    await pipeline.stop()  # As async_unload_entry does, before stopping the stores.
    assert not pipeline.running and pipeline.current["status"] == "interrupted"  # Cut off, not cancelled.
    [kept] = [r for r in runtime.role_runs.runs if r["id"] == pipeline.current["id"]]
    assert kept["status"] == "interrupted"
    place = runtime.flow.place("friction01")
    assert place["stage"] == "Triage" and place["held"]["cause"] == "cut off"
    saved = (await runtime.flow.store.async_load())["conversations"]["friction01"]
    assert saved["stage"] == "Triage" and saved["held"]["cause"] == "cut off"
    await pipeline.stop()  # Nothing running: a no-op.
    assert pipeline.current["status"] == "interrupted"


@pytest.mark.asyncio
async def test_run_now_is_refused_naming_the_steps_whose_model_cant_run(improver):
    hass, entry, pipeline = improver.hass, improver.entry, improver.runtime.pipeline
    entry.options = {**entry.options, EDITOR: "ai_task.small"}
    hass.states.async_set("ai_task.small", "unavailable")
    sent = []
    connection = SimpleNamespace(user=SimpleNamespace(id="alex", name="Alex", is_admin=True),
                                 send_result=lambda i, r: sent.append(("ok", r)),
                                 send_error=lambda i, code, message: sent.append(("error", code, message)))
    await _ws(panel.ws_run_start, hass, connection, {"id": 1, "confirmed": True})
    assert sent[-1] == ("error", "busy", "Write up can't run: its AI model isn't available. Choose another in the "
                                         "settings.")
    assert pipeline.current is None  # Nothing started, nothing spent.
    entry.options = {**entry.options, CONF_PAUSED: ["write"]}
    await _ws(panel.ws_run_status, hass, connection, {"id": 2})
    assert sent[-1][1]["preview"]["blocked_models"] == {"write": ["Write up"]}  # Paused, but Run one step can tell.
    await _ws(panel.ws_run_start, hass, connection, {"id": 3, "confirmed": True, "only": "write"})
    assert sent[-1][:2] == ("error", "busy") and pipeline.current is None
    hass.states.async_set("ai_task.main", "unavailable")  # No triage model is fine: Triage passes everything on.
    entry.options = {CONF_AI_TASK: "ai_task.main"}
    await _ws(panel.ws_run_start, hass, connection, {"id": 4, "confirmed": True})
    assert sent[-1] == ("error", "busy", "Analyze, Propose, Review and Write up can't run: their AI models aren't "
                                         "available. Choose another in the settings.")


@pytest.mark.asyncio
async def test_a_run_that_reaches_propose_is_refused_while_simulates_model_cant_run(improver):
    """Simulate is Propose's stamp, not a step of its own (P0.4): its feedback check's model is one Propose needs, so a
    full run, or Run one step on Propose, is refused while it can't run; a step before Propose isn't."""
    hass, entry, pipeline = improver.hass, improver.entry, improver.runtime.pipeline
    entry.options = {**entry.options, CONF_CHECK_AI_TASK: "ai_task.check"}
    hass.states.async_set("ai_task.check", "unavailable")
    why = "Simulate can't run: its AI model isn't available. Choose another in the settings."
    assert await ask(hass, panel.ws_run_start, confirmed=True) == ("busy", why)
    assert await ask(hass, panel.ws_run_start, confirmed=True, only="propose") == ("busy", why)
    assert pipeline.current is None  # Nothing started, nothing spent.
    assert pipeline.preview()["blocked_models"] == {"propose": ["Simulate"]}
    assert (await ask(hass, panel.ws_run_start, confirmed=True, only="analyze"))[0] == "ok"
    await pipeline._task


@pytest.mark.asyncio
async def test_one_sent_in_during_a_run_the_household_cancels_still_gets_its_own_run_right_after(improver, monkeypatch):
    """The fast lane: a conversation filed or sent in gets a run of its own as soon as it ends,
    or right after the run in progress (P0.7; Kai, 2026-10-25: filed "for immediate (or as soon as possible)
    improvement analysis"). Cancelling the run in progress stops that run, not the fast lane promised to the one sent
    in meanwhile."""
    hass, pipeline, model, flow = improver.hass, improver.runtime.pipeline, improver.model, improver.runtime.flow
    improver.entry.options = {**improver.entry.options, CONF_PAUSED: ["propose", "write"]}
    await improver.arrives(improver.friction)
    hold, triage = asyncio.Event(), model.triage

    async def slow(hass, payload, tools):  # The full run's quick check takes a while.
        await hold.wait()
        return await triage(hass, payload, tools)
    monkeypatch.setattr(model, "triage", slow)
    pipeline.start(trigger="manual")
    await asyncio.sleep(0.2)
    assert pipeline.running and flow.place("friction01")["stage"] == "Triage"

    await improver.arrives(variant(improver.friction, "second001", "Please toggle the fixture relay."))
    assert (await ask(hass, panel.ws_conversation_send, recording_id="second001", note="Press it at once."))[0] == "ok"
    await ask(hass, panel.ws_run_cancel)
    with pytest.raises(asyncio.CancelledError):
        await pipeline._task
    hold.set()
    await asyncio.sleep(0.2)
    assert pipeline.current["trigger"] == "filed"  # Its own run, right after the one cancelled.
    if pipeline.running:
        await pipeline._task
    assert pipeline.current["moved"] == ["second001"]
    assert flow.round_of("second001")["records"]["Analyze"]["analysis"]  # Analysed, past the quick check.


@pytest.mark.asyncio
async def test_a_picked_run_refused_for_a_missing_model_sends_nothing_in(improver):
    """DESIGN.md: "a run that would need it waits and says which step, instead of failing midway"; a run that needs a
    missing model is refused before anything is spent. A Run on picked conversations that's refused leaves them as
    they were: one that looked smooth isn't quietly sent in, as if the household had sent it in."""
    hass, flow = improver.hass, improver.runtime.flow
    await improver.arrives(improver.friction)
    seat(improver, "smooth01", "Your household", outcome="looked smooth")
    hass.states.async_remove("ai_task.haiku")  # The quick check's model is gone from Home Assistant.
    result = await ask(hass, panel.ws_run_start, confirmed=True, conversations=["friction01", "smooth01"])
    assert result[0] == "busy" and "can't run" in result[1]  # Refused, saying which step.
    assert improver.runtime.pipeline.current is None
    assert flow.sent("friction01") is None and flow.place("smooth01")["outcome"] == "looked smooth"  # As they were.
    _, page = await ask(hass, panel.ws_conversations)
    assert {c["id"]: c["status"] for c in page["conversations"]} == {"friction01": "waiting"}
    _, page = await ask(hass, panel.ws_conversations, archived=True)  # Settled: with the archived, on its own.
    assert {c["id"]: c["status"] for c in page["conversations"]} == {"smooth01": "smooth"}

