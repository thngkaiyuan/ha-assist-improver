"""The developer view's commands, over a real run: every conversation's stage, any ID's lifecycle, runs, traces, the
journal and health. They answer admins only, and only in developer mode."""

import json
from types import SimpleNamespace

import pytest

from recordings import before, make_recording
from test_changeset import _ws
from kit import FIRST, candidate_turn, improver, no_turns, variant  # noqa: F401 - improver is a fixture.
from custom_components.assist_improver import devview
from custom_components.assist_improver.const import CONF_DEVELOPER


async def ask(hass, handler, **msg):
    sent = []
    connection = SimpleNamespace(user=SimpleNamespace(id="alex", is_admin=True),
                                 send_result=lambda i, r: sent.append(("ok", r)),
                                 send_error=lambda i, code, text: sent.append((code, text)))
    await _ws(handler, hass, connection, {"id": 1, **msg})
    return sent[-1]


async def one_run(improver):
    """One of each kind of conversation (friction, smooth, without turns, unreadable, interrupted, still going), and
    one run with Write up blocked."""
    base = make_recording()
    for record in (improver.friction,
                   variant(base, "smooth0001", "Turn the fixture relay off.", turns=1),
                   no_turns("noturns001"),
                   {**variant(base, "malformed1", "Turn the relay."), "turns": base["turns"][:3]},
                   variant(base, "interrupt1", "Switch the fixture relay off.", turns=2, status="interrupted"),
                   variant(base, "active0001", "Fixture relay off, please.", turns=1, status="active"),
                   variant(base, "activeb002", "Relay off.", turns=1, status="active")):
        await improver.arrives(record)
    improver.model.fail_once.add("Switch the fixture relay off.")
    improver.entry.options = {**improver.entry.options, "paused_stages": ["write"]}
    improver.client.blocks = before(improver.friction) + candidate_turn()
    run = await improver.run()
    assert run["status"] == "done", run["error"]
    return run


@pytest.mark.asyncio
async def test_every_command_needs_developer_mode(improver):
    hass = improver.hass
    for handler in devview.COMMANDS:
        msg = {"query": "x"} if handler is devview.ws_lookup else {"trace_id": "x"} if handler is devview.ws_trace \
            else {}
        assert (await ask(hass, handler, **msg))[0] == "developer_mode_off"


@pytest.mark.asyncio
async def test_every_conversation_shows_where_its_flow_record_says_it_stands(improver):
    hass = improver.hass
    run = await one_run(improver)
    improver.entry.options = {**improver.entry.options, CONF_DEVELOPER: True}
    status, reply = await ask(hass, devview.ws_conversations)
    assert status == "ok"
    rows = {row["id"]: row for row in reply["rows"]}
    assert {rid: (row["stage"], row["round"], row["outcome"], (row["held"] or {}).get("cause"))
            for rid, row in rows.items()} == {
        "friction01": ("Write up", 1, None, "blocked"),  # Its improvement waits at the blocked Write up.
        "smooth0001": ("Your household", 1, "looked smooth", None),
        "malformed1": ("Your household", 1, "couldn't be read", None),
        "interrupt1": ("Your household", 1, "no trouble found", None),
        "noturns001": ("Your Assist", 0, None, None), "active0001": ("Your Assist", 0, None, None),
        "activeb002": ("Your Assist", 0, None, None)}
    assert rows["friction01"]["held"]["why"] == "Write up is blocked."
    friction = rows["friction01"]
    assert friction["short"] == "friction" and friction["friction"] == 1 and friction["improvements"]
    assert friction["replays"] and friction["runs"] == [run["id"]] and friction["bytes"] > 0
    # Its analysis failed once on the provider's error, and the retry went through: the failed attempt shows.
    assert rows["interrupt1"]["failed"] == 1 and rows["smooth0001"]["failed"] == 0
    assert "waiting_at" not in friction and "strikes" not in friction


@pytest.mark.asyncio
async def test_any_id_or_the_start_of_one_shows_its_lifecycle_and_log(improver):
    hass, recorder = improver.hass, improver.recorder
    run = await one_run(improver)
    improver.entry.options = {**improver.entry.options, CONF_DEVELOPER: True}

    # A conversation, by its 8-character short ID: recorded, closed, analysed, proposed from, in an improvement.
    _, found = await ask(hass, devview.ws_lookup, query="friction")
    assert found["match"] == {**found["match"], "kind": "recording", "id": "friction01"}
    kinds = [e["kind"] for e in found["timeline"]]
    assert {"recorded", "closed", "analysis", "proposal", "replay", "improvement", "stage", "held"} <= set(kinds)
    assert all(e["ids"].get("run") == run["id"] for e in found["timeline"] if e["kind"] == "analysis")
    # A replay without the change that reproduced: its devices did as recorded, all its status says.
    assert "Replay without the change (try 1) of friction: its devices did as recorded" in [
        e["what"] for e in found["timeline"] if e["kind"] == "replay"]
    # Its flow record, round by round: each stage it moved to, and where it's held, with why.
    assert [e["what"] for e in found["timeline"] if e["kind"] == "stage"] == [
        f"Round 1: {stage}" for stage in ("Your Assist", "Triage", "Analyze", "Propose", "Write up")]
    assert [e["what"] for e in found["timeline"] if e["kind"] == "held"] == ["Held at Write up: Write up is blocked."]
    _, failing = await ask(hass, devview.ws_lookup, query="interrupt1")  # Its failed attempt, with why.
    [problem] = [e for e in failing["timeline"] if e["kind"] == "problem"]
    assert problem["what"].startswith("Round 1, Analyze: failed: ") and problem["ids"]["run"] == run["id"]
    # Home Assistant's conversation ID finds the same recording.
    recorder.keys["01JCONVERSATION"] = "friction01"
    _, by_conversation = await ask(hass, devview.ws_lookup, query="01JCONVERSATION")
    assert by_conversation["match"]["recording"] == "friction01" and by_conversation["timeline"]

    # A run: its AI calls, each conversation's attempt, with its trace for the timing chart.
    _, found = await ask(hass, devview.ws_lookup, query=run["id"])
    assert found["match"]["kind"] == "run" and found["trace_id"] == run["id"]
    calls = [e for e in found["timeline"] if e["kind"] == "role_call"]
    assert {e["ids"]["recording"] for e in calls if "recording" in e["ids"]} >= {"friction01", "interrupt1"}
    assert found["journal"] and found["record"]["progress"]["done"] == len(run["moved"])

    # An improvement: the test that validated its idea, then proposed from it.
    [item] = improver.runtime.improvements.items.values()
    _, found = await ask(hass, devview.ws_lookup, query=item["id"])
    assert [e["kind"] for e in found["timeline"]][:2] == ["test", "created"]

    # The start of several IDs offers them to pick from; too short a start matches nothing.
    _, found = await ask(hass, devview.ws_lookup, query="acti")
    assert sorted(m["id"] for m in found["matches"]) == ["active0001", "activeb002"]
    assert (await ask(hass, devview.ws_lookup, query="ac"))[1] == {"matches": []}


@pytest.mark.asyncio
async def test_runs_traces_and_the_journal_show_where_time_went_and_what_happened(improver):
    hass = improver.hass
    run = await one_run(improver)
    improver.entry.options = {**improver.entry.options, CONF_DEVELOPER: True}
    _, reply = await ask(hass, devview.ws_runs)
    [listed] = reply["runs"]
    assert listed["id"] == run["id"] and listed["trace_available"] and "timings" not in listed
    assert listed["counts"]["analyzed"] == run["progress"]["analyzed"]

    _, reply = await ask(hass, devview.ws_trace, trace_id=listed["trace_id"])
    spans = reply["trace"]["spans"]
    assert [s["at"] for s in spans] == sorted(s["at"] for s in spans)  # In the order they started.
    assert {"run", "conversation", "ai.journey_analyzer"} <= {s["name"] for s in spans}
    # Each conversation's work is in a span of its own, which what happens inside it inherits its ID from.
    conversation = {s["id"]: s["tags"]["recording_id"] for s in spans if s["name"] == "conversation"}
    assert set(conversation.values()) == set(run["moved"])
    assert {s["tags"].get("recording_id") for s in spans if s["name"] == "ai.journey_analyzer"} <= set(run["moved"])
    journal = reply["trace"]["journal"]
    assert [e["at"] for e in journal] == sorted((e["at"] for e in journal), reverse=True)  # Newest first.
    assert (await ask(hass, devview.ws_trace, trace_id="nothing_here"))[0] == "not_found"

    _, page = await ask(hass, devview.ws_journal, run_id=run["id"], limit=1)
    [newest] = page["entries"]
    assert page["more"] and newest["tags"]["run_id"] == run["id"] and newest["trace_id"] == run["id"]
    _, older = await ask(hass, devview.ws_journal, run_id=run["id"], limit=1, before=newest["at"])
    assert older["entries"][0]["at"] < newest["at"]  # The next page.
    _, warnings = await ask(hass, devview.ws_journal, level="warning", limit=500)
    assert all(e["level"] in ("warning", "error", "critical") for e in warnings["entries"])


@pytest.mark.asyncio
async def test_health_gives_sizes_times_and_ai_calls_but_no_conversation_text(improver):
    hass = improver.hass
    await one_run(improver)
    improver.entry.options = {**improver.entry.options, CONF_DEVELOPER: True}
    status, reply = await ask(hass, devview.ws_health)
    assert status == "ok"
    assert reply["disk"]["recordings"]["count"] >= 5 and reply["disk"]["traces"]["count"] >= 1
    assert reply["memory"]["run log"]["bytes"] > 0 and reply["memory"]["improvements"]["items"] == 1
    assert {"conversation", "ai.journey_analyzer"} <= {row["name"] for row in reply["hot"]}
    assert reply["ai_calls"]["journey_analyzer"]["calls"] >= 2 and reply["ai_calls"]["journey_analyzer"]["failed"] == 1
    assert reply["now"]["run"]["running"] is False and reply["process"]["max_rss_bytes"] > 0
    text = json.dumps(reply)
    assert not any(said in text for said in (FIRST, "Turn the fixture relay off.", "Switch the fixture relay off."))


@pytest.mark.asyncio
async def test_the_settings_view_says_when_developer_mode_is_on(improver):
    from custom_components.assist_improver.settings import view
    assert view(improver.hass, improver.entry)["developer"] is False
    improver.entry.options = {**improver.entry.options, CONF_DEVELOPER: True}
    assert view(improver.hass, improver.entry)["developer"] is True
