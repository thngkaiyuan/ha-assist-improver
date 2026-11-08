"""Who gets the quick check (DETAILS P9.3): every conversation a run takes in, picked or sent in from the panel without
a note too, unless it came with a clear sign it needs a closer look (analysis.deliberate): a note written as it was sent
in, filing it from Assist, or sending in again one an earlier round looked at. Those go straight to Analyze, and only
those tell the analyzer the household sent them in (Kai, 2026-10-25). How it was sent in is one mark on its flow
record (flow.Flow.sent_in)."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from test_conversations import ask
from kit import FIRST, improver  # noqa: F401 - improver is a fixture.
from custom_components.assist_improver import analysis, analyzer, panel
from custom_components.assist_improver import llm as improver_llm
from custom_components.assist_improver.flow import deliberate

NOTE = "It should have pressed the relay straight away."


def heard(monkeypatch):
    """What the analyzer is told about each conversation it analyses: {first request: (the instruction that the user
    sent it in, filed_by_user, whether the note is the last turn it reads)}."""
    told, run_role = {}, analysis.run_role

    async def listening(hass, **kwargs):
        if kwargs["role"] == analyzer.ROLE:
            said = kwargs["payload"]["conversation"]
            told[said[0]["user_says"]] = (analyzer.FILED in kwargs["instructions"],
                                          kwargs["payload"].get("filed_by_user"), said[-1]["user_says"] == NOTE)
        return await run_role(hass, **kwargs)
    monkeypatch.setattr(analysis, "run_role", listening)
    return told


checked = lambda model: sorted(said for role, said in model.calls if role == "triage")  # noqa: E731
analysed = lambda model: sorted(said for role, said in model.calls if role == "analyzer")  # noqa: E731


def test_only_a_clear_sign_sends_a_conversation_past_the_quick_check():
    at = "2026-10-25T08:00:00+00:00"
    assert not deliberate(None) and not deliberate({})
    assert not deliberate({"at": at, "via": "panel"})  # Picked, or sent in without a note.
    assert not deliberate({"at": at, "via": "panel", "by": "alex", "again": False})
    assert deliberate({"at": at, "via": "panel", "note": "The front porch."})
    assert deliberate({"at": at, "via": "panel", "again": True})  # Sent in again after an earlier round.
    assert deliberate({"at": at, "via": "assist"})  # Filed from Assist, with or without a note.


async def picked(improver, monkeypatch):
    """Select it, Run: the run below picks it, with nothing sent in."""


async def sent(improver, monkeypatch, note=""):
    """Send this one in, on its page."""
    assert (await ask(improver.hass, panel.ws_conversation_send, recording_id="friction01", note=note))[0] == "ok"


async def noted(improver, monkeypatch):
    await sent(improver, monkeypatch, NOTE)


async def filed(improver, monkeypatch):
    """Assist's file_improvement tool, as the conversation goes on, with nothing said about what to improve."""
    from homeassistant.components.conversation.chat_log import current_chat_log
    recorder = improver.recorder
    recorder.keys["conv-1"] = "friction01"
    recorder.sessions["friction01"] = deepcopy(improver.friction)  # Still in memory as Assist files it.
    monkeypatch.setattr(recorder, "schedule", lambda *a, **k: None)
    token = current_chat_log.set(SimpleNamespace(conversation_id="conv-1"))
    try:
        answer = await improver_llm.FileImprovement().async_call(
            improver.hass, SimpleNamespace(tool_args={}), SimpleNamespace(context=SimpleNamespace(user_id="alex")))
    finally:
        current_chat_log.reset(token)
    recorder.sessions.pop("friction01")
    assert answer.data["filed"] is True


async def again(improver, monkeypatch):
    """The quick check found it smooth in an earlier round (Your household, Looked smooth); sent in again, without a
    note."""
    model, triage = improver.model, improver.model.triage

    async def smooth(hass, payload, tools):
        await triage(hass, payload, tools)
        return {"smooth": True, "sure": True, "why": "Done as asked."}
    monkeypatch.setattr(model, "triage", smooth)
    await improver.run()
    assert improver.runtime.flow.place("friction01")["outcome"] == "looked smooth"
    monkeypatch.setattr(model, "triage", triage)
    model.calls.clear()
    await sent(improver, monkeypatch)


@pytest.mark.asyncio
@pytest.mark.parametrize("way, via, skips, told", [
    (picked, None, False, (False, None, False)),
    (sent, "panel", False, (False, None, False)),
    (noted, "panel", True, (True, {"note": NOTE}, True)),  # The note is its last turn (P8.9.1).
    (filed, "assist", True, (True, {"note": ""}, False)),
    (again, "panel", True, (True, {"note": ""}, False)),
], ids=["picked", "sent-without-a-note", "sent-with-a-note", "filed-from-assist", "sent-in-again"])
async def test_only_a_clear_sign_skips_the_quick_check_and_only_it_tells_the_analyzer(improver, monkeypatch, way, via,
                                                                                      skips, told):
    """DETAILS P9.3.1 and P9.3.2: picked for a run, or sent in (its own run at once), the quick check runs unless
    there's a clear sign, and the analyzer hears the household sent it in only with one, with the note if there is one."""
    model, flow = improver.model, improver.runtime.flow
    await improver.arrives(improver.friction)
    improver.entry.options = {**improver.entry.options, "paused_stages": ["propose", "write"]}
    hearing = heard(monkeypatch)
    await way(improver, monkeypatch)
    assert (flow.sent("friction01") or {}).get("via") == via
    if way is picked:
        status, _ = await ask(improver.hass, panel.ws_run_start, confirmed=True, conversations=["friction01"])
        assert status == "ok"
    await improver.runtime.pipeline._task
    assert checked(model) == ([] if skips else [FIRST]) and analysed(model) == [FIRST]
    assert hearing == {FIRST: told}
    assert flow.round_of("friction01")["records"]["Your Assist"]["sent"] == flow.sent("friction01")  # Its round's.


@pytest.mark.asyncio
async def test_picking_one_already_sent_in_with_a_note_keeps_its_note_and_its_way_past_the_quick_check(improver,
                                                                                                    monkeypatch):
    """Filed from Assist with a note, then picked on another device where the list still shows it New: the picked run
    takes it in as it was sent in, its note kept, past the quick check (DETAILS P8.9.1, P9.3.1)."""
    hass, model, flow = improver.hass, improver.model, improver.runtime.flow
    await improver.arrives(improver.friction)
    improver.entry.options = {**improver.entry.options, "paused_stages": ["propose", "write"]}
    await flow.sent_in("friction01", NOTE, via="assist")  # Filed from Assist, with what to improve.
    hearing = heard(monkeypatch)
    status, _ = await ask(hass, panel.ws_run_start, confirmed=True, conversations=["friction01"])
    assert status == "ok"
    await improver.runtime.pipeline._task
    assert flow.sent("friction01")["note"] == NOTE, "the household's note was dropped"
    assert checked(model) == []  # Filed from Assist: past the quick check.
    assert hearing == {FIRST: (True, {"note": NOTE}, True)}


@pytest.mark.asyncio
async def test_filing_an_archived_conversation_from_assist_brings_it_back_into_the_pipeline(improver, monkeypatch):
    """Archived while it went on, then filed from Assist: filing is sending in (P0.12), so it's back at Your Assist,
    and once it ends the fast lane takes it in."""
    from homeassistant.components.conversation.chat_log import current_chat_log
    recorder, pipeline, flow = improver.recorder, improver.runtime.pipeline, improver.runtime.flow
    recorder.keys["conv-1"] = "live0001"
    recorder.sessions["live0001"] = {"id": "live0001", "status": "active", "turns": []}
    recorder.index["live0001"] = {"id": "live0001", "status": "active", "user_turns": 1}
    await flow.archive("live0001")  # Alex archived it from the list while it was still going.
    monkeypatch.setattr(recorder, "schedule", lambda *a, **k: None)
    token = current_chat_log.set(SimpleNamespace(conversation_id="conv-1"))
    try:
        answer = await improver_llm.FileImprovement().async_call(
            improver.hass, SimpleNamespace(tool_args={"what_to_improve": "Press it in one go."}),
            SimpleNamespace(context=SimpleNamespace(user_id="alex")))
    finally:
        current_chat_log.reset(token)
    assert answer.data["filed"] is True  # Assist tells him it's filed...
    recorder.index["live0001"]["status"] = "session_expired"  # ...and the conversation ends.
    assert not flow.place("live0001")["archived"], "still archived, though it was sent in"
    assert flow.waiting("live0001") and pipeline.filed("live0001")  # The fast lane takes it in.
