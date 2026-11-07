"""Traces (trace.py): spans and the journal of a run or a day, kept across restarts, bounded, and tidied away."""

import asyncio
import logging
import os
import time
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from homeassistant.util.dt import utcnow

from custom_components.assist_improver import retention, trace
from custom_components.assist_improver.const import CONF_DEVELOPER, DOMAIN
from custom_components.assist_improver.trace import for_run, journal, journal_detail, span, tag, tagged, timed, today


@pytest_asyncio.fixture
async def tracing(hass):
    """Tracing set up as the integration sets it up, for an entry whose options a test can change."""
    entry = SimpleNamespace(options={})
    await trace.async_setup(hass, entry)
    try:
        yield SimpleNamespace(hass=hass, entry=entry)
    finally:
        await trace.async_unload()


def day_id():
    return next(t for t in trace.active_ids() if t.startswith("day_"))


async def child(n, gate):
    async with span("child", n=n):
        await gate.wait()
        journal("info", f"child {n} done")


@pytest.mark.asyncio
async def test_spans_nest_also_across_parallel_tasks(tracing):
    hass = tracing.hass
    gate = asyncio.Event()
    async with for_run("run1"):
        assert trace.ids() == {"run_id": "run1", "trace_id": "run1"}
        async with span("outer", recording_id="rec1") as outer:
            tasks = [asyncio.create_task(child(1, gate)), hass.async_create_task(child(2, gate))]
            await asyncio.sleep(0)
            live = await trace.load(hass, "run1")  # Open spans show while the run goes on.
            assert {s["name"] for s in live["open"]} == {"outer", "child"} and live["spans"] == []
            gate.set()
            await asyncio.gather(*tasks)
            with span("inner", step=3):
                pass
    assert trace.ids() == {} and "run1" not in trace.active_ids()  # Saved for good, and let go.
    saved = await trace.load(hass, "run1")
    assert saved["kind"] == "run" and saved["finished"] and saved["open"] == []
    by_name = {}
    for s in saved["spans"]:
        by_name.setdefault(s["name"], []).append(s)
    [root] = by_name["outer"]
    assert root["parent"] is None and root["status"] == "ok" and root["ms"] >= 0
    assert root["tags"] == {"run_id": "run1", "recording_id": "rec1"} and root["id"] == outer.id
    # Each task's span is a child of the span it was started in, with the context's tags.
    assert sorted(s["tags"]["n"] for s in by_name["child"]) == [1, 2]
    assert all(s["parent"] == root["id"] and s["tags"]["recording_id"] == "rec1" for s in by_name["child"])
    assert by_name["inner"][0]["parent"] == root["id"]
    # Children close first, so they come first; ordered by start, the parent leads.
    assert saved["spans"][-1]["name"] == "outer" and trace.spans(saved)[0]["name"] == "outer"
    assert [s["name"] for s in trace.spans(saved, n=2)] == ["child"]
    done = [e for e in saved["journal"] if e["message"].startswith("child")]
    assert {e["message"] for e in done} == {"child 1 done", "child 2 done"}
    assert all(e["tags"] == {"run_id": "run1", "recording_id": "rec1"} for e in done)
    assert {e["span"] for e in done} == {s["id"] for s in by_name["child"]}  # Each in the span it happened in.
    assert trace.HOT["child"]["count"] >= 2 and trace.HOT["outer"]["total_ms"] >= 0


@pytest.mark.asyncio
async def test_a_span_closes_on_errors_and_cancellation_with_its_status(tracing):
    hass = tracing.hass

    async def slow():
        async with span("slow"):
            await asyncio.sleep(10)
    async with for_run("run2"):
        with pytest.raises(RuntimeError):
            async with span("boom"):
                raise RuntimeError("provider down")
        with pytest.raises(ValueError):
            with span("sync boom"):
                raise ValueError("bad input")
        async with span("handled") as section:
            section.fail("The provider said no")  # A failure the code handles: no exception.
        task = asyncio.create_task(slow())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    spans = {s["name"]: s for s in (await trace.load(hass, "run2"))["spans"]}
    assert spans["boom"]["status"] == "error" and spans["boom"]["error"] == "RuntimeError: provider down"
    assert spans["sync boom"]["status"] == "error" and spans["sync boom"]["error"] == "ValueError: bad input"
    assert spans["handled"]["status"] == "error" and spans["handled"]["error"] == "The provider said no"
    assert spans["slow"]["status"] == "cancelled" and spans["slow"]["error"] is None


@pytest.mark.asyncio
async def test_journal_entries_carry_the_context_tags(tracing):
    hass = tracing.hass
    with tagged(recording_id="rec9"):
        journal("info", "Recording closed", user_turns=2)  # Outside a run: today's trace.
    async with for_run("run3"):
        async with span("conversation", recording_id="rec9", steps="triage") as section:
            tag(plan_id="plan1")  # From here on, inside this span.
            journal("warning", "Retrying", attempt=2)
        journal("info", "After")
    [closed] = (await trace.load(hass, day_id()))["journal"]
    assert closed["tags"] == {"recording_id": "rec9", "user_turns": 2} and closed["level"] == "info"
    entries = (await trace.load(hass, "run3"))["journal"]
    retry = next(e for e in entries if e["message"] == "Retrying")
    assert retry["tags"] == {"run_id": "run3", "recording_id": "rec9", "plan_id": "plan1", "attempt": 2}
    assert retry["span"] == section.id and retry["level"] == "warning"
    after = next(e for e in entries if e["message"] == "After")
    assert after["tags"] == {"run_id": "run3"} and after["span"] is None  # The span's tags ended with it.
    [conversation] = (await trace.load(hass, "run3"))["spans"]
    assert conversation["tags"]["plan_id"] == "plan1" and conversation["tags"]["steps"] == "triage"


@pytest.mark.asyncio
async def test_the_log_handler_copies_what_passes_the_configured_level_and_changes_no_level(tracing, caplog):
    hass = tracing.hass
    package = logging.getLogger("custom_components.assist_improver")
    module = logging.getLogger("custom_components.assist_improver.pipeline")
    before = (package.level, module.level, package.getEffectiveLevel(), module.getEffectiveLevel())
    await trace.async_unload()
    await trace.async_setup(hass, tracing.entry)  # Setting up again changes no level either.
    assert (package.level, module.level, package.getEffectiveLevel(), module.getEffectiveLevel()) == before
    assert not module.isEnabledFor(logging.DEBUG)  # As configured here: no debug lines.
    module.warning("The %s stage failed", "propose")
    module.debug("Not passing the configured level")
    try:
        raise RuntimeError("provider down")
    except RuntimeError:
        module.exception("Couldn't analyse a conversation")
    entries = (await trace.load(hass, day_id()))["journal"]
    assert [(e["level"], e["message"]) for e in entries] == [
        ("warning", "The propose stage failed"), ("error", "Couldn't analyse a conversation")]
    assert entries[0]["logger"] == "custom_components.assist_improver.pipeline"
    assert "Traceback" in entries[1]["exception"] and "RuntimeError: provider down" in entries[1]["exception"]
    # With debug logging turned on (by Alex, in HA's logger settings), a journal entry is still stored once: its
    # debug mirror isn't copied back.
    caplog.set_level(logging.DEBUG, logger="custom_components.assist_improver")
    journal("info", "Stored once")
    module.debug("Now passing")
    entries = (await trace.load(hass, day_id()))["journal"]
    assert [e["message"] for e in entries].count("Stored once") == 1 and entries[-1]["message"] == "Now passing"
    assert "Stored once" in caplog.text  # HA's debug log still shows it.


@pytest.mark.asyncio
async def test_the_log_handler_reads_no_source_files_where_it_runs(tracing, monkeypatch):
    """The handler runs in the thread that logs, the event loop mostly: there, an exception is copied without its
    traceback, which reads source files. The traceback is added in the executor, as the trace is saved or read."""
    import traceback
    from test_retention import on_the_event_loop
    formatted, original = [], traceback.format_exception

    def watched(*args, **kwargs):
        formatted.append(on_the_event_loop())
        return original(*args, **kwargs)
    monkeypatch.setattr(traceback, "format_exception", watched)
    module = logging.getLogger("custom_components.assist_improver.pipeline")
    try:
        raise RuntimeError("provider down")
    except RuntimeError:
        module.exception("Couldn't analyse a conversation")
    [entry] = [e for e in trace._tracer().active[day_id()].journal if e.get("logger")]
    assert entry["exception"] == "RuntimeError: provider down\n" and formatted == []
    assert "Traceback" in (await trace.load(tracing.hass, day_id()))["journal"][-1]["exception"]  # Read: added.
    try:
        raise ValueError("bad answer")
    except ValueError:
        module.exception("Couldn't write it up")
    await trace.async_unload()  # Saved.
    await trace.async_setup(tracing.hass, tracing.entry)
    saved = (await trace.load(tracing.hass, day_id()))["journal"]
    assert "Traceback" in saved[-1]["exception"] and "ValueError: bad answer" in saved[-1]["exception"]
    assert formatted == [False, False]


@pytest.mark.asyncio
async def test_detail_entries_are_kept_only_in_developer_mode(tracing):
    journal_detail("debug", "Tool input", size=10)
    tracing.entry.options = {CONF_DEVELOPER: True}
    journal_detail("debug", "Tool input", size=20)
    entries = (await trace.load(tracing.hass, day_id()))["journal"]
    assert [e["tags"]["size"] for e in entries] == [20]


@pytest.mark.asyncio
async def test_a_trace_survives_a_reload_and_a_span_cut_off_by_a_restart_says_so(tracing):
    hass = tracing.hass
    async with for_run("run4"):
        async with span("stage.analyze"):
            journal("info", "Analysed")
    with today():
        with span("tidy"):
            pass
        cut = span("tidy").__enter__()  # Still going when Home Assistant stops.
        journal("info", "Tidying")
        await trace.async_unload()  # Saves every active trace.
        cut.__exit__(None, None, None)  # Closes after the restart: never written to the saved trace again.
    await trace.async_setup(hass, tracing.entry)
    run = await trace.load(hass, "run4")
    assert [s["name"] for s in run["spans"]] == ["stage.analyze"] and run["journal"][0]["message"] == "Analysed"
    day = await trace.load(hass, day_id())
    assert [e["message"] for e in day["journal"]] == ["Tidying"]
    assert [(s["name"], s["status"]) for s in day["spans"]] == [("tidy", "ok"), ("tidy", "cancelled")]
    assert day["spans"][1]["error"].startswith("Cut off") and day["open"] == []
    journal("info", "After the restart")  # Today's trace goes on where it left off.
    await trace.async_unload()
    await trace.async_setup(hass, tracing.entry)
    day = await trace.load(hass, day_id())
    assert [e["message"] for e in day["journal"]] == ["Tidying", "After the restart"]


@pytest.mark.asyncio
async def test_caps_count_what_they_drop(tracing, monkeypatch):
    monkeypatch.setattr(trace, "MAX_SPANS", 3)
    monkeypatch.setattr(trace, "MAX_JOURNAL", 2)
    async with for_run("run5"):
        for n in range(5):
            with span("step", n=n):
                journal("info", f"step {n}")
    saved = await trace.load(tracing.hass, "run5")
    assert len(saved["spans"]) == 3 and saved["dropped"] == {"spans": 2, "journal": 3}
    assert [e["message"] for e in saved["journal"]] == ["step 0", "step 1"]


@pytest.mark.asyncio
async def test_a_section_holding_the_event_loop_is_journaled(tracing):
    calls = []

    @timed("hot.sum")
    def add(a, b):
        calls.append((a, b))
        return a + b
    count = trace.HOT.get("hot.sum", {}).get("count", 0)
    assert add(1, 2) == 3 and trace.HOT["hot.sum"]["count"] == count + 1
    with span("recorder.snapshot", recording_id="rec1"):
        time.sleep(0.12)  # Synchronous work on the event loop.
    async with span("awaits"):
        await asyncio.sleep(0.12)  # Waiting isn't blocking.
    async with timed("store.role_runs"):
        await asyncio.sleep(0)
    [warning] = (await trace.load(tracing.hass, day_id()))["journal"]
    assert warning["level"] == "warning" and warning["message"].startswith("recorder.snapshot blocked the event loop")
    assert warning["tags"]["recording_id"] == "rec1"
    assert trace.HOT["recorder.snapshot"]["max_ms"] >= 100 and trace.HOT["store.role_runs"]["count"] >= 1


@pytest.mark.asyncio
async def test_readers_list_load_and_search_the_retained_traces(tracing):
    hass = tracing.hass
    journal("warning", "Disk nearly full")
    async with for_run("run6"):
        await asyncio.sleep(0.002)  # Entries a moment apart, so their order is certain.
        journal("info", "Run started")
        await asyncio.sleep(0.002)
        with span("conversation", recording_id="rec1"):
            journal("error", "Analysis failed", attempt=1)
    async with for_run("run7"):
        await asyncio.sleep(0.002)
        journal("info", "Other run")
    await trace.async_unload()  # Everything saved; then readers go to the stores.
    await trace.async_setup(hass, tracing.entry)
    listed = {t["id"]: t for t in await trace.traces(hass)}
    assert set(listed) == {"run6", "run7", day_id()} and listed[day_id()]["active"]
    assert listed["run6"]["kind"] == "run" and listed["run6"]["bytes"] > 0 and not listed["run6"]["active"]
    assert await trace.load(hass, "../index") is None and await trace.load(hass, "missing") is None
    newest = await trace.find_entries(hass)
    assert [e["message"] for e in newest] == ["Other run", "Analysis failed", "Run started", "Disk nearly full"]
    assert newest[0]["trace_id"] == "run7"
    assert [e["message"] for e in await trace.find_entries(hass, level="warning")] == [
        "Analysis failed", "Disk nearly full"]
    assert [e["message"] for e in await trace.find_entries(hass, run_id="run6")] == ["Analysis failed", "Run started"]
    assert [e["message"] for e in await trace.find_entries(hass, recording_id="rec1")] == ["Analysis failed"]
    assert [e["message"] for e in await trace.find_entries(hass, text="DISK")] == ["Disk nearly full"]
    page = await trace.find_entries(hass, limit=2)
    more = await trace.find_entries(hass, limit=2, before=page[-1]["at"])
    assert [e["message"] for e in page + more] == [e["message"] for e in newest]


def write_trace(hass, trace_id, size, days_old):
    path = hass.config.path(".storage")
    os.makedirs(path, exist_ok=True)
    name = os.path.join(path, f"{trace.STORE_PREFIX}{trace_id}")
    with open(name, "w") as file:
        file.write("x" * size)
    moment = (utcnow() - timedelta(days=days_old)).timestamp()
    os.utime(name, (moment, moment))


@pytest.mark.asyncio
async def test_retention_deletes_by_age_then_by_size_never_an_active_trace(tracing, monkeypatch):
    hass = tracing.hass
    for trace_id, size, days in (("old", 100, 8), ("older_active", 100, 9), ("big", 600, 3), ("mid", 300, 2),
                                 ("new", 300, 1)):
        write_trace(hass, trace_id, size, days)
    write_trace(hass, "x.corrupt", 100, 30)  # Not a trace store (a corrupt file HA set aside): left alone.
    monkeypatch.setattr(retention, "TRACE_MAX_BYTES", 700)
    done = await retention.prune_traces(hass, active={"older_active"})
    # "old" is past TRACE_DAYS; then the oldest go until the rest fit; the active one stays whatever its age.
    assert done == {"deleted": 2, "bytes": 700}
    left = {f["id"] for f in trace.stored(hass.config.path(".storage"))}
    assert left == {"older_active", "mid", "new"}
    assert os.path.exists(hass.config.path(".storage", f"{trace.STORE_PREFIX}x.corrupt"))
    assert await retention.prune_traces(hass, active={"older_active"}) == {"deleted": 0, "bytes": 700}
    # Diagnostics count them apart from the other stores: sizes only. The briefs' store isn't a recording.
    from custom_components.assist_improver.diagnostics import _storage
    for name, size in (("session_" + "a" * 64, 50), ("session_briefs", 20)):
        Path(hass.config.path(".storage", f"{DOMAIN}.{name}")).write_text("x" * size)
    storage = _storage(hass.config.path(".storage"))
    assert storage["traces"] == {"count": 4, "bytes": 800} and storage["stores"] == {"session_briefs": 20}
    assert storage["recordings"] == {"count": 1, "bytes": 50, "largest": 50}


@pytest.mark.asyncio
async def test_an_ai_roles_tool_calls_get_spans_and_in_developer_mode_their_input(tracing):
    class Tool:
        name = "submit_goals"

        async def async_call(self, hass, tool_input, llm_context):
            return SimpleNamespace(data={"accepted": tool_input.tool_args["ok"]})
    tool = trace.traced_tool(Tool())
    assert trace.traced_tool(tool) is tool  # Wrapped once, however often it's handed over.
    tracing.entry.options = {CONF_DEVELOPER: True}
    async with for_run("run8"):
        async with span("ai.journey_analyzer", role="journey_analyzer") as call:
            assert next(t for t in trace.active_summary() if t["id"] == "run8")["open"] == 1
            await tool.async_call(None, SimpleNamespace(tool_args={"ok": False}), None)
            await tool.async_call(None, SimpleNamespace(tool_args={"ok": True}), None)
    saved = await trace.load(tracing.hass, "run8")
    calls = [s for s in saved["spans"] if s["name"] == "tool.submit_goals"]
    assert [s["tags"]["accepted"] for s in calls] == [False, True]
    assert all(s["parent"] == call.id and s["tags"]["role"] == "journey_analyzer" for s in calls)
    assert [e["tags"]["args"] for e in saved["journal"]] == ['{"ok": false}', '{"ok": true}']
