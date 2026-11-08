"""The conversations view: kept conversations found by what was said and when, read turn by turn, and sent in, as
Assist's tool files one, so a run lets it in (past the quick check with a note), never twice. Each row's chip, whether
the list shows it, and whether it can be sent in or picked come from its flow record (conversations.row)."""

from types import SimpleNamespace

import pytest

from editors import Connection, Editors, command, open_panel
from kit import FIRST, HELD, improver, seat, variant  # noqa: F401 - improver is a fixture.
from test_records import problems
from recordings import make_recording
from custom_components.assist_improver import panel, records
from custom_components.assist_improver.cases import failed_calls, replayed, transcript


async def ask(hass, handler, **msg):
    """One websocket command from the panel, signed in as Alex (an admin), open and saving scripts through HA's script
    editor: ("ok", result) or (error code, text)."""
    connection = Connection()
    await open_panel(hass, connection, Editors(hass))
    try:
        reply = await command(hass, connection, handler, **msg)
    finally:
        connection.close()
    return reply if reply[0] == "ok" else reply[1:]


async def three(improver):
    """A conversation with friction an hour ago, one asking about the weather months ago, and one about the porch
    light just now."""
    from datetime import timedelta
    from homeassistant.util.dt import utcnow
    weather = variant(improver.friction, "weather01", "What's the weather like?", turns=1)
    porch = variant(improver.friction, "porch0001", "Turn on the porch light.", turns=1)
    for record, ago in ((improver.friction, timedelta(hours=1)), (weather, timedelta(days=60)), (porch, timedelta())):
        await improver.arrives(record)
        improver.recorder.index[record["id"]]["started"] = (utcnow() - ago).isoformat()


def improved_porch(improver):
    """The porch conversation, ended improved: the household accepted its improvement."""
    seat(improver, "porch0001", "Your household", outcome="improved")


def test_a_transcript_is_what_was_said_turn_by_turn():
    record = make_recording()
    said = transcript(record)
    assert [t["you"] for t in said] == [t["user"]["content"] for t in record["turns"]]
    assert all(t["assist"] for t in said)  # Assist's last reply to each.


def test_a_failed_step_shows_in_the_turn_it_happened_in_plain_words():
    call = lambda i, name, args: {"role": "assistant", "content": None, "tool_calls": [  # noqa: E731
        {"id": i, "tool_name": name, "tool_args": args, "external": False}]}
    result = lambda i, name, out: {"role": "tool_result", "tool_call_id": i, "tool_name": name, "tool_result": out}  # noqa: E731
    record = {"turns": [{}, {}], "chat_log": {"content": [
        {"role": "user", "content": "Warm up the den."},
        call("a", "homeassistant__GetLiveContext", {"name": "Den"}),
        result("a", "homeassistant__GetLiveContext", {"error": "No exposed entities matched name 'Den'"}),
        {"role": "assistant", "content": "I can't find it. Which device?"},
        {"role": "user", "content": "The den, and warm the rug."},
        call("b", "intent__HassTurnOn", {"name": "Rug heater", "domain": ["select"], "area": "Den"}),
        result("b", "intent__HassTurnOn", {"error": "IntentHandleError", "error_text": "Failed to call turn_on"}),
        call("c", "script__warm_the_den", {}),
        result("c", "script__warm_the_den", {"success": True}),
        call("d", "media_player__HassMediaPlayerMute", {"domain": "media_player"}),
        result("d", "media_player__HassMediaPlayerMute", {"error": "Not supported"}),
        {"role": "assistant", "content": "Done, except the rug."}]}}
    said = transcript(record)
    assert said[0]["failed"] == [{"step": "Look up devices · Den", "error": "No exposed entities matched name 'Den'"}]
    assert said[1]["failed"] == [
        {"step": "Turn on · Rug heater · Den · select", "error": "Failed to call turn_on"},  # The error's text.
        {"step": "Media player mute · media player", "error": "Not supported"}]  # Named from the tool's own name.
    assert said[1]["assist"] == "Done, except the rug."
    assert all(problems(t, records.Said) == [] for t in said)


LAMP_ERROR = {"error": "HomeAssistantError", "error_text": "Lamp unreachable"}


def lamp_answered(**answer):
    """Assist turning on the lamp, Home Assistant's answer to its call (answer: the tool result's fields), and its
    reply: the messages a recording's chat log and a replay step's output both hold."""
    return [{"role": "assistant", "content": None, "tool_calls": [
                {"id": "a", "tool_name": "intent__HassTurnOn", "tool_args": {"name": "Lamp"}, "external": False}]},
            {"role": "tool_result", "tool_call_id": "a", "tool_name": "intent__HassTurnOn", **answer},
            {"role": "assistant", "content": "The lamp didn't answer."}]


def failed_steps(answered):
    """(the recording's failed steps, its count of failed calls, the replay's failed steps) for one turn whose
    messages after the user's are answered."""
    record = {"turns": [{}], "chat_log": {"content": [{"role": "user", "content": "Turn on the lamp."}, *answered]}}
    return (transcript(record)[0].get("failed"), failed_calls(record),
            replayed([{"input": "Turn on the lamp.", "output": answered}])[0].get("failed"))


def test_a_failed_step_reads_alike_in_a_recording_and_a_replay_from_each_copy_home_assistant_writes():
    """DETAILS P14.4.11 and P14.4.12: a step Home Assistant answered with an error shows in its turn and in the row's
    count, the same in a recording and in a replay's steps, written as Home Assistant does today (its result and
    tool_result, the copy) or as it did before 2026.10 (tool_result alone). A result that only flags an error (an MCP
    tool's) names no error, so it shows no failed step."""
    shown = [{"step": "Turn on · Lamp", "error": "Lamp unreachable"}]
    assert failed_steps(lamp_answered(result={"data": LAMP_ERROR, "error": True}, tool_result=LAMP_ERROR)) == (
        shown, 1, shown)
    assert failed_steps(lamp_answered(tool_result=LAMP_ERROR)) == (shown, 1, shown)
    done = {"speech": {}, "response_type": "action_done", "data": {"success": [{"id": "light.lamp"}], "failed": []}}
    assert failed_steps(lamp_answered(result={"data": done, "error": False}, tool_result=done)) == (None, 0, None)
    assert failed_steps(lamp_answered(result={"data": {"content": [{"type": "text", "text": "No."}]},
                                              "error": True})) == (None, 0, None)


@pytest.mark.asyncio
async def test_kept_conversations_are_found_by_what_was_said_and_when(improver):
    hass = improver.hass
    await three(improver)
    _, page = await ask(hass, panel.ws_conversations)
    assert [c["id"] for c in page["conversations"]][-1] == "weather01" and page["total"] == 3  # Newest first.
    assert all(c["status"] == "waiting" for c in page["conversations"])
    _, found = await ask(hass, panel.ws_conversations, query="PORCH")
    assert [c["id"] for c in found["conversations"]] == ["porch0001"] and found["conversations"][0]["said"] == (
        "Turn on the porch light.")
    _, recent = await ask(hass, panel.ws_conversations, days=7)
    assert "weather01" not in [c["id"] for c in recent["conversations"]]
    _, oldest = await ask(hass, panel.ws_conversations, order="oldest")
    assert oldest["conversations"][0]["id"] == "weather01"
    _, one = await ask(hass, panel.ws_conversation, recording_id="friction01")
    assert one["turns"][0]["you"] == FIRST and one["status"] == "waiting" and len(one["turns"]) == len(improver.friction["turns"])
    assert await ask(hass, panel.ws_conversation, recording_id="nope") == ("not_found", "That conversation isn't kept.")
    kept = await improver.runtime.transcripts.all()
    assert all(problems(t, records.Transcript) == [] for t in kept.values())


@pytest.mark.asyncio
async def test_a_past_conversation_sent_in_goes_in_past_the_quick_check_with_the_note(improver, monkeypatch):
    """Sending one in starts a run for it; only what was sent in moves."""
    hass, recorder, runtime, model = improver.hass, improver.recorder, improver.runtime, improver.model
    await three(improver)
    improver.entry.options = {**improver.entry.options, "paused_stages": ["propose", "write"]}
    recorder.sessions.pop("friction01", None)  # Long over: only on disk.
    seen, analyze = [], model.journey_analyzer

    async def journey_analyzer(hass, payload, tools):
        seen.append(payload.get("filed_by_user"))
        return await analyze(hass, payload, tools)
    monkeypatch.setattr(model, "journey_analyzer", journey_analyzer)
    _, sent = await ask(hass, panel.ws_conversation_send, recording_id="friction01", note="Press it in one go.")
    assert sent == {"status": "sent", "hidden": False}
    assert runtime.flow.sent("friction01")["note"] == "Press it in one go."
    assert runtime.flow.sent("friction01")["by"] == "alex"
    saved = (await runtime.flow.store.async_load())["conversations"]["friction01"]
    assert saved["sent"]["note"] == "Press it in one go."  # On disk too.
    assert runtime.pipeline.running and runtime.pipeline.current["trigger"] == "filed"
    await runtime.pipeline._task
    assert runtime.pipeline.current["moved"] == ["friction01"] and seen == [{"note": "Press it in one go."}]
    assert [c for c in model.calls if c[0] == "triage"] == []
    assert runtime.flow.place("friction01")["stage"] == "Propose"  # Trouble found; Propose is blocked.
    _, page = await ask(hass, panel.ws_conversations)
    status = {c["id"]: c["status"] for c in page["conversations"]}
    assert status == {"friction01": "working", "porch0001": "waiting", "weather01": "waiting"}
    assert await ask(hass, panel.ws_conversation_send, recording_id="friction01") == (
        "already_in", "The improver already has that conversation.")  # Never twice.


@pytest.mark.asyncio
async def test_words_match_in_any_order_and_without_every_word_the_closest_show(improver):
    hass = improver.hass
    await three(improver)
    _, found = await ask(hass, panel.ws_conversations, query="light porch")
    assert [c["id"] for c in found["conversations"]] == ["porch0001"] and found["close"] is False
    _, close = await ask(hass, panel.ws_conversations, query="porch lamp")  # No conversation says "lamp".
    assert close["close"] is True and close["conversations"][0]["id"] == "porch0001"
    _, none = await ask(hass, panel.ws_conversations, query="zebra")
    assert none["conversations"] == [] and none["total"] == 0


@pytest.mark.asyncio
async def test_the_most_back_and_forth_comes_first_and_the_list_says_what_can_be_picked(improver):
    hass = improver.hass
    await three(improver)
    _, page = await ask(hass, panel.ws_conversations, order="effort")
    assert page["conversations"][0]["id"] == "friction01"  # The most turns.
    assert {c["id"]: (c["status"], c["runnable"], c["sendable"]) for c in page["conversations"]} == dict.fromkeys(
        ["friction01", "porch0001", "weather01"], ("waiting", True, True))  # New: picked for a run, or sent in.
    assert [{**r} for r in page["every"]] == [{"id": c["id"], "status": "waiting", "stage": "Your Assist",
                                               "hidden": False, "runnable": True, "archivable": True}
                                              for c in page["conversations"]]
    assert "counts" not in page and "waiting" not in page
    assert all("reply" in c and "failed" in c for c in page["conversations"])


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["sent", "picked", "sent-plainly"])
async def test_one_the_quick_check_found_smooth_can_be_sent_in_once_for_the_full_analysis(improver, monkeypatch, how):
    """Kai, 2026-10-25: a smooth verdict no longer keeps a conversation in for good. It ends Looked smooth in Your
    household, can be sent in or picked (by hand, never by Select all, the panel says), and then gets the full
    analysis, past the quick check, once, in a new round: sent in again, with a note or without, is a clear sign it
    needs the closer look (P0.12, DETAILS P9.3.1)."""
    hass, runtime, model = improver.hass, improver.runtime, improver.model
    pipeline, flow = runtime.pipeline, runtime.flow
    await improver.arrives(improver.friction)
    improver.entry.options = {**improver.entry.options, "paused_stages": ["propose", "write"]}
    triage, seen, analyze = model.triage, [], model.journey_analyzer

    async def smooth(hass, payload, tools):  # The quick check finds it smooth.
        await triage(hass, payload, tools)
        return {"smooth": True, "sure": True, "why": "Done as asked."}

    async def journey_analyzer(hass, payload, tools):
        seen.append(payload.get("filed_by_user"))
        return await analyze(hass, payload, tools)
    monkeypatch.setattr(model, "triage", smooth)
    monkeypatch.setattr(model, "journey_analyzer", journey_analyzer)
    await improver.run()
    assert flow.place("friction01")["outcome"] == "looked smooth" and seen == []
    _, page = await ask(hass, panel.ws_conversations)
    assert page["conversations"] == [] and page["hidden"] == 1  # In Your household: off the list on its own.
    _, page = await ask(hass, panel.ws_conversations, archived=True)
    [row] = page["conversations"]
    assert (row["status"], row["hidden"], row["runnable"], row["sendable"]) == ("smooth", True, True, True)
    await improver.run()  # A full run leaves it as it is.
    assert seen == [] and [c for c in model.calls if c[0] == "triage"] == [("triage", FIRST)]
    if how == "picked":
        _, started = await ask(hass, panel.ws_run_start, confirmed=True, conversations=["friction01"])
        assert started["run"]["picked"] == ["friction01"] and flow.sent("friction01")["again"] is True
    else:  # Sent in: its fast lane takes it in at once.
        _, sent = await ask(hass, panel.ws_conversation_send, recording_id="friction01",
                            note="" if how == "sent-plainly" else "Press it in one go.")
        assert sent["status"] == "sent" and flow.sent("friction01")["again"] is True
        assert pipeline.running
    await pipeline._task
    assert len(seen) == 1 and [c for c in model.calls if c[0] == "triage"] == [("triage", FIRST)]  # Past it.
    assert seen[0] == ({"note": "Press it in one go."} if how == "sent" else {"note": ""})
    assert flow.record("friction01")["round"] == 2 and flow.round_of("friction01")["path"][:3] == [
        "Your Assist", "Triage", "Analyze"]  # A new round, through Triage past the quick check (SPEC.md P0.4).
    _, page = await ask(hass, panel.ws_conversations)  # Back in the list on its own.
    assert page["conversations"][0]["status"] == "working"  # Trouble found; Propose is blocked.
    await improver.run()
    assert len(seen) == 1  # Never twice.
    assert await ask(hass, panel.ws_conversation_send, recording_id="friction01") == (
        "already_in", "The improver already has that conversation.")


@pytest.mark.asyncio
async def test_run_nows_confirmation_names_a_few_new_conversations_from_the_words_the_view_kept(improver, monkeypatch):
    """The new ones a manual full run admits (Pipeline.preview): sent in first, then the newest; archived ones left
    out, and those that would stop at a blocked step; none until the conversations view has read the words."""
    hass, pipeline = improver.hass, improver.runtime.pipeline
    monkeypatch.setattr(pipeline, "request", lambda *rids: None)  # Sent in, each waits, as behind a run in progress.
    await three(improver)
    assert pipeline.preview()["which"] == []  # The view hasn't read them yet: nothing is opened for this.
    await ask(hass, panel.ws_conversations)
    assert pipeline.preview()["which"] == ["Turn on the porch light.", FIRST, "What's the weather like?"]
    await ask(hass, panel.ws_conversation_send, recording_id="weather01")
    await ask(hass, panel.ws_conversation_hide, recording_id="porch0001")
    assert pipeline.preview()["which"] == ["What's the weather like?", FIRST]
    assert pipeline.preview()["admits"] == 2
    improver.entry.options = {**improver.entry.options, "paused_stages": ["triage"]}
    await ask(hass, panel.ws_conversation_send, recording_id="weather01", note="It was sunny.")  # Past the quick check.
    preview = pipeline.preview()
    assert preview["which"] == ["What's the weather like?"] and preview["admits"] == 1  # The other stops at Triage.


def test_when_understands_days_parts_of_the_day_and_times():
    """A conversation is found by roughly when it happened, in the household's time: a day, a part of the day, a
    time said any of the usual ways; small words beside them don't count as words to find."""
    from datetime import datetime
    from custom_components.assist_improver.conversations import maybe_names, when
    now = datetime(2026, 10, 25, 9, 30)  # A Sunday.
    tuesday_late = datetime(2026, 10, 20, 22, 40)
    for query in ("tuesday night", "10:40pm", "10:40 pm", "22:40", "last tuesday around 10pm", "tue 10ish", "10 o'clock"):
        test, rest = when(query, now)
        assert test and test(tuesday_late) and rest == [], query
    for query in ("monday night", "morning", "10:40am", "8pm", "yesterday"):
        test, _ = when(query, now)
        assert test and not test(tuesday_late), query
    test, rest = when("porch light this morning", now)
    assert rest == ["porch", "light"] and test(datetime(2026, 10, 25, 8, 15)) and not test(datetime(2026, 10, 24, 8, 15))
    test, _ = when("last night", now)  # Past midnight is still last night.
    assert test(datetime(2026, 10, 25, 1, 5)) and test(datetime(2026, 10, 24, 23, 0)) and not test(datetime(2026, 10, 25, 23, 0))
    assert when("set it to 10 percent", now) == (None, ["set", "it", "to", "10", "percent"])  # A bare number isn't a time.
    assert when("sun blinds", now) == (None, ["sun", "blinds"])  # The sun, not Sunday.
    late = datetime(2026, 10, 25, 1, 30)  # Past midnight, tonight is still the night that began yesterday evening.
    test, _ = when("tonight", late)
    assert test(datetime(2026, 10, 24, 23, 0)) and test(datetime(2026, 10, 25, 1, 0))
    assert not test(datetime(2026, 10, 23, 23, 0))
    test, _ = when("tonight", now.replace(hour=21))
    assert test(datetime(2026, 10, 25, 20, 0)) and not test(datetime(2026, 10, 24, 23, 0))
    assert when("night light", now, {"night light"}) == (None, ["night light"])  # A name, not a when.
    test, rest = when("last night light", now, {"night light"})
    assert rest == ["night light"] and test(datetime(2026, 10, 24, 15, 0)) and not test(datetime(2026, 10, 25, 9, 0))
    assert maybe_names("Friday night lights in the morning") == {"night lights"}  # Not "friday night", "the morning".
    assert maybe_names("good night morning routine") == {"good night", "morning routine"}


@pytest.mark.asyncio
async def test_a_search_by_when_and_words_shows_where_the_words_are(improver):
    hass = improver.hass
    from homeassistant.util import dt as dt_util
    from custom_components.assist_improver.conversations import PARTS
    await three(improver)
    index = improver.recorder.index
    started = {rid: dt_util.as_local(dt_util.parse_datetime(index[rid]["started"])) for rid in index}
    hour = started["porch0001"].hour
    # Just after midnight it's last night's, as people say it; otherwise today's part of the day.
    query = "last night" if hour < 5 else "today " + next(name for name, (low, high) in PARTS.items() if low <= hour < high)
    _, found = await ask(hass, panel.ws_conversations, query=query)
    assert "porch0001" in [c["id"] for c in found["conversations"]] and "weather01" not in [c["id"] for c in found["conversations"]]
    kept = await improver.runtime.transcripts.all()
    word = kept["friction01"]["turns"][0]["assist"].split()[-1].strip(".,!?").casefold()
    _, deep = await ask(hass, panel.ws_conversations, query=word)
    row = next(c for c in deep["conversations"] if c["id"] == "friction01")
    assert row["match"] and word in row["match"].casefold()  # Found in what Assist said: shown where.
    _, first = await ask(hass, panel.ws_conversations, query="porch")
    assert first["conversations"][0]["match"] is None  # The first thing said shows it already.
    said_word = kept["friction01"]["turns"][0]["you"].split()[0].casefold()
    _, both = await ask(hass, panel.ws_conversations, query=f"{said_word} {word}")
    row = next(c for c in both["conversations"] if c["id"] == "friction01")
    assert row["match"] and word in row["match"].casefold()  # The word the list doesn't show yet.
    _, other = await ask(hass, panel.ws_conversations, query="weather morning")
    assert other["conversations"][0]["id"] == "weather01"  # If not one morning, then "morning" as a word: the closest.
    _, sunday = await ask(hass, panel.ws_conversations, query="porch 3:33am")
    assert [c["id"] for c in sunday["conversations"]][:1] == ["porch0001"]


@pytest.mark.asyncio
async def test_a_conversation_whose_improvement_was_accepted_shows_as_improved(improver):
    """Improved, it's in Your household: off the list on its own, shown with the archived; it can be sent in again
    for a new round (P0.12), or picked by hand into a run, which sends it in first."""
    hass = improver.hass
    await three(improver)
    improved_porch(improver)
    _, page = await ask(hass, panel.ws_conversations)
    assert "porch0001" not in {c["id"] for c in page["conversations"]} and page["hidden"] == 1
    assert {c["id"]: c["status"] for c in page["conversations"]}["weather01"] == "waiting"
    _, page = await ask(hass, panel.ws_conversations, archived=True)  # Below Show archived, listed on their own.
    rows = {c["id"]: c for c in page["conversations"]}
    assert list(rows) == ["porch0001"] and rows["porch0001"]["status"] == "improved"
    assert page["by_status"] == {"improved": 1}
    assert (rows["porch0001"]["sendable"], rows["porch0001"]["runnable"]) == (True, True)


async def at_local(improver, rid, text, when):
    """A conversation that said the text, started at that local time."""
    from homeassistant.util import dt as dt_util
    await improver.arrives(variant(improver.friction, rid, text, turns=1))
    local = when.replace(tzinfo=dt_util.get_default_time_zone())
    improver.recorder.index[rid]["started"] = dt_util.as_utc(local).isoformat()


@pytest.mark.asyncio
async def test_the_day_filter_counts_whole_days_in_home_assistants_time_zone(improver):
    """Today is today's conversations, as the search's 'today' is, not the last 24 hours': one from a minute before
    midnight isn't among them, though it's less than a day old. The last 7 days are today and the six before it."""
    from datetime import timedelta
    from homeassistant.util import dt as dt_util
    hass, midnight = improver.hass, dt_util.start_of_local_day()
    for rid, at in (("today000", midnight), ("lastnite", midnight - timedelta(minutes=1)),
                    ("sixdays0", midnight - timedelta(days=6)),
                    ("tooearly", midnight - timedelta(days=6, minutes=1))):
        await at_local(improver, rid, "Turn off the lights.", at.replace(tzinfo=None))
    days = {}
    for n in (1, 7):
        _, page = await ask(hass, panel.ws_conversations, days=n)
        days[n] = sorted(c["id"] for c in page["conversations"])
    _, searched = await ask(hass, panel.ws_conversations, query="today")
    assert days[1] == [c["id"] for c in searched["conversations"]] == ["today000"]
    assert days[7] == ["lastnite", "sixdays0", "today000"]


@pytest.mark.asyncio
async def test_a_name_with_a_part_of_the_day_in_it_is_found_as_said_not_as_a_when(improver):
    from datetime import datetime
    hass = improver.hass
    await three(improver)
    await at_local(improver, "nightlt1", "Turn on the night light.", datetime(2026, 10, 21, 14, 0))  # A Wednesday.
    await at_local(improver, "desklt01", "Turn on the desk light.", datetime(2026, 10, 21, 22, 0))  # At night.
    await at_local(improver, "friday01", "Turn off the lights.", datetime(2026, 10, 23, 21, 30))  # Friday night.
    _, found = await ask(hass, panel.ws_conversations, query="Night Light")
    assert [c["id"] for c in found["conversations"]] == ["nightlt1"] and found["close"] is False
    assert found["conversations"][0]["match"] is None  # The first thing said shows it.
    _, friday = await ask(hass, panel.ws_conversations, query="friday night lights")  # No one said "night lights".
    assert [c["id"] for c in friday["conversations"]] == ["friday01"]


@pytest.mark.asyncio
async def test_tonight_is_always_a_when_even_where_someone_said_it(improver):
    from datetime import datetime, timedelta
    from homeassistant.util import dt as dt_util
    hass = improver.hass
    await three(improver)
    night = (dt_util.now() - timedelta(hours=5)).date()  # Tonight's date, past midnight too.
    await at_local(improver, "oldsaid1", "Turn off the lights tonight at 11.", datetime(2026, 10, 1, 9, 0))
    await at_local(improver, "porchlt1", "Turn on the porch lights.", datetime(night.year, night.month, night.day, 21, 0))
    _, found = await ask(hass, panel.ws_conversations, query="lights tonight")
    assert [c["id"] for c in found["conversations"]] == ["porchlt1"]


@pytest.mark.asyncio
async def test_with_a_time_said_the_closest_comes_first(improver):
    from datetime import datetime
    hass = improver.hass
    await three(improver)
    for rid, minute in (("late0003", (23, 3)), ("first039", (22, 39)), ("last0007", (23, 7))):
        await at_local(improver, rid, "Turn off all the lights.", datetime(2026, 10, 23, *minute))  # Friday.
    _, found = await ask(hass, panel.ws_conversations, query="friday 10:40 pm lights")
    assert [c["id"] for c in found["conversations"]] == ["first039", "late0003", "last0007"]
    assert all(c["timed"] for c in found["conversations"])  # Found by when: the panel marks the time.
    assert found["closest"] is True
    _, chosen = await ask(hass, panel.ws_conversations, query="friday 10:40 pm lights", order="newest")
    assert [c["id"] for c in chosen["conversations"]] == ["last0007", "late0003", "first039"]  # Newest, as chosen.
    assert chosen["closest"] is False
    _, newest = await ask(hass, panel.ws_conversations, query="friday night lights")  # No time said: newest first.
    assert [c["id"] for c in newest["conversations"]] == ["last0007", "late0003", "first039"] and not newest["closest"]
    _, words = await ask(hass, panel.ws_conversations, query="lights")
    assert not any(c["timed"] for c in words["conversations"])


@pytest.mark.asyncio
async def test_conversations_are_found_by_where_they_stand_with_how_many_stand_each_way(improver):
    """The home page's Status filter: with search and the day filter; by_status counts what the rest matches, so
    choosing a status shows that many, and Select all takes them all."""
    hass, runtime = improver.hass, improver.runtime
    await three(improver)
    improved_porch(improver)
    await runtime.flow.sent_in("friction01")  # Sent in.
    _, page = await ask(hass, panel.ws_conversations)
    assert page["by_status"] == {"waiting": 1, "sent": 1}  # Improved is off the list on its own: not to choose.
    _, page = await ask(hass, panel.ws_conversations, archived=True)  # Show archived: listed on their own, to choose too.
    assert page["by_status"] == {"improved": 1}
    _, improved = await ask(hass, panel.ws_conversations, statuses=["improved"], archived=True)
    assert [c["id"] for c in improved["conversations"]] == ["porch0001"] and improved["total"] == 1
    assert [r["id"] for r in improved["every"]] == ["porch0001"]  # Select all: just those.
    assert improved["by_status"] == page["by_status"]  # The choice doesn't change what each would show.
    _, two = await ask(hass, panel.ws_conversations, statuses=["waiting", "sent"], order="oldest")
    assert [c["id"] for c in two["conversations"]] == ["weather01", "friction01"]
    _, searched = await ask(hass, panel.ws_conversations, query="weather", statuses=["waiting", "improved"])
    assert [c["id"] for c in searched["conversations"]] == ["weather01"] and searched["by_status"] == {"waiting": 1}
    _, recent = await ask(hass, panel.ws_conversations, days=7, statuses=["waiting"])
    assert recent["conversations"] == [] and recent["by_status"] == {"sent": 1}
    _, recent = await ask(hass, panel.ws_conversations, days=7, statuses=["waiting"], archived=True)
    assert recent["conversations"] == [] and recent["by_status"] == {"improved": 1}
    _, none = await ask(hass, panel.ws_conversations, statuses=[], archived=True)
    assert none["conversations"] == [] and none["total"] == 0 and none["by_status"] == page["by_status"]


@pytest.mark.asyncio
async def test_a_runs_progress_names_the_stage_at_work_on_the_conversation_its_on_of_how_many(improver, monkeypatch):
    """The panel lights up the stage at work on the conversation the run is on (the quick check at Triage, then
    Analyze alone) and says which conversation it is of all the run moves (DETAILS P14.8); each is kept as it moves,
    so a restart shows where the run was."""
    hass, runtime, model = improver.hass, improver.runtime, improver.model
    await three(improver)
    improver.entry.options = {**improver.entry.options, "paused_stages": ["propose", "write"]}
    seen, triage, analyze = [], model.triage, model.journey_analyzer

    def where(role):
        run = runtime.pipeline.current
        [kept] = [r for r in runtime.role_runs.runs if r["id"] == run["id"]]
        seen.append((role, run["stage"], run["progress"]["done"], run["progress"]["total"],
                     run["progress"]["conversation"], kept["stage"]))

    async def checking(hass, payload, tools):
        where("triage")
        return await triage(hass, payload, tools)

    async def analysing(hass, payload, tools):
        where("analyzer")
        return await analyze(hass, payload, tools)
    monkeypatch.setattr(model, "triage", checking)
    monkeypatch.setattr(model, "journey_analyzer", analysing)
    runtime.pipeline.start(trigger="manual")
    await runtime.pipeline._task
    run = runtime.pipeline.current
    assert len(run["moved"]) == 3 and run["progress"]["total"] == 3 and run["progress"]["done"] == 3
    seen = list(dict.fromkeys(seen))  # An analysis the model fails is tried again, at the same place.
    assert [(role, stage) for role, stage, *_ in seen] == [("triage", "triage"), ("analyzer", "analyze")] * 3
    assert [(done, total, rid) for _, _, done, total, rid, _ in seen] == [
        (n, 3, rid) for n, rid in enumerate(run["moved"]) for _ in (1, 2)]  # Each conversation in turn, of three.
    assert all(kept == stage for _, stage, *_, kept in seen)  # Kept as it moved.
    assert set(run["results"]) == set(run["moved"]) and run["progress"]["analyzed"] == 1  # The fixture's only.


# Each place a conversation's flow record can be, and how the list shows it (P0.2; DETAILS P14.3, P14.4): its chip,
# whether the list leaves it out, whether it can be sent in (P0.12), whether a picked run can take it (P0.7),
# and whether it can be archived (not while its improvement waits for the household: its decision is the way out).

PLACES = [  # (where it's seated, its chip, left out of the list, sendable, runnable)
    (dict(stage="Your Assist", status="active"), "active", False, True, False),
    (dict(stage="Your Assist", status="active", sent={"via": "assist"}), "sent", False, True, False),
    (dict(stage="Your Assist"), "waiting", False, True, True),
    (dict(stage="Your Assist", sent={"note": "The front porch."}), "sent", False, True, True),
    (dict(stage="Your Assist", held="cut off"), "waiting", False, False, True),
    (dict(stage="Triage", held="blocked"), "waiting", False, False, True),
    (dict(stage="Triage", held="cut off", sent={}), "sent", False, False, True),
    (dict(stage="Analyze", held="failures"), "queued", False, False, True),
    (dict(stage="Propose", held="scope"), "working", False, False, True),
    (dict(stage="Write up", held="blocked"), "writing", False, False, True),
    (dict(stage="Your review"), "review", True, False, False),
    (dict(stage="Your review", question="Which porch light did you mean?"), "unclear", True, False, False),
    (dict(stage="Your household", outcome="looked smooth"), "smooth", True, True, True),
    (dict(stage="Your household", outcome="no trouble found"), "fine", True, True, True),
    (dict(stage="Your household", outcome="no fix found"), "nofix", True, True, True),
    (dict(stage="Your household", outcome="improved"), "improved", True, True, True),
    (dict(stage="Your household", outcome="declined", archived=True), "declined", True, True, True),
    (dict(stage="Your household", outcome="marked as a test"), "test", True, True, True),
    (dict(stage="Your household", outcome="couldn't be read"), "unreadable", True, True, True),
    (dict(stage="Your household", outcome="archived", archived=True), "archived", True, True, True),
    (dict(stage="Your household", outcome="duplicate", covered_by="imp-other"), "duplicate", True, True, True),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("seated, chip, left_out, sendable, runnable", PLACES, ids=[
    "still-going", "filed-while-going", "new", "sent-in", "held-at-your-assist", "held-at-triage",
    "sent-in-held-at-triage", "held-at-analyze", "held-at-propose", "held-at-write-up", "up-for-review", "unclear",
    "looked-smooth", "no-trouble-found", "no-fix-found", "improved", "declined", "marked-as-a-test",
    "couldnt-be-read", "archived", "duplicate"])
async def test_each_place_on_the_flow_record_has_its_chip_and_says_whether_it_is_listed(improver, seated, chip,
                                                                                        left_out, sendable, runnable):
    hass, runtime = improver.hass, improver.runtime
    seat(improver, "conv0001", **seated)
    if chip == "review":  # Its improvement, written up, waits at the top of the home page.
        runtime.improvements.items["imp-1"] = {"id": "imp-1", "title": "Press it", "status": "awaiting_review",
                                               "conversation": "conv0001", "copy": {"headline": "Press it"}}
    _, page = await ask(hass, panel.ws_conversations, archived=left_out, ids=["conv0001"])  # The list it's in.
    [row] = page["conversations"]
    assert (row["status"], row["hidden"], row["sendable"], row["runnable"]) == (chip, left_out, sendable, runnable)
    assert row["archivable"] is (chip != "review")
    held = seated.get("held")
    assert row["stage"] == seated["stage"] and row["held"] == (
        {"cause": held, "why": HELD[held].format(stage=seated["stage"])} if held else None)
    assert row["covered_by"] == seated.get("covered_by") and row.get("review") == ("imp-1" if chip == "review" else None)
    assert page["stands"] == [{"id": "conv0001", **{k: row[k] for k in (
        "status", "hidden", "sendable", "runnable", "archivable", "stage", "held", "covered_by")}}]
    assert page["every"] == [{"id": "conv0001", "status": chip, "stage": seated["stage"], "hidden": left_out,
                              "runnable": runnable, "archivable": row["archivable"]}]
    _, listed = await ask(hass, panel.ws_conversations)
    assert (listed["conversations"] == []) is left_out and listed["hidden"] == int(left_out)
    _, below = await ask(hass, panel.ws_conversations, archived=True)  # Below Show archived: only the archived ones.
    assert (below["conversations"] == []) is not left_out and below["hidden"] == int(left_out)
    _, opened = await ask(hass, panel.ws_conversation, recording_id="conv0001")
    assert (opened["status"], opened["hidden"], opened["sendable"]) == (chip, left_out, sendable)
    status, _ = await ask(hass, panel.ws_conversation_send, recording_id="conv0001")
    assert (status == "ok") is sendable  # The page offers Send this one in exactly when it's taken.
    _, archived = await ask(hass, panel.ws_conversations_hide, recording_ids=["conv0001"])
    assert archived == {"changed": int(row["archivable"])}  # The selection's Archive leaves one it can't out.


@pytest.mark.asyncio
async def test_no_fix_found_on_one_no_idea_could_be_tested_on_keeps_its_chip_and_says_so(improver):
    """DETAILS P14.3.14: the same chip, and the row says which No fix found it is, for its bubble: one whose round's
    Propose record says no idea could be tested on it (P6.6.7), not one whose tests found no fix."""
    hass = improver.hass
    seat(improver, "nofix001", "Your household", outcome="no fix found")
    untested = seat(improver, "nofix002", "Your household", outcome="no fix found")
    untested["rounds"]["1"]["records"]["Propose"] = {"attempts": [], "untestable": {
        "why": "Tried 3 times without the change: turn 1 didn't do what it did in the conversation.", "at": "now"}}
    _, page = await ask(hass, panel.ws_conversations, archived=True)
    rows = {row["id"]: row for row in page["conversations"]}
    assert rows["nofix001"]["status"] == rows["nofix002"]["status"] == "nofix"
    assert rows["nofix002"]["untestable"] is True and "untestable" not in rows["nofix001"]


@pytest.mark.asyncio
async def test_unarchived_one_stays_listed_while_it_stands_where_it_did(improver):
    """DETAILS P14.4.3: unarchiving keeps a conversation listed for as long as it stands where it did. Archived by hand,
    one is out of the pipeline, in Your household (P0.12); unarchived mid-round, it's back in the pipeline, new, for a
    new round (DETAILS P3.3.3)."""
    hass, flow = improver.hass, improver.runtime.flow
    seat(improver, "nofix001", "Your household", outcome="no fix found")
    seat(improver, "held0001", "Propose", held="blocked")

    async def listed():
        return {c["id"] for c in (await ask(hass, panel.ws_conversations))[1]["conversations"]}
    assert await listed() == {"held0001"}  # No fix found: off the list on its own.
    assert await ask(hass, panel.ws_conversation_hide, recording_id="nofix001", hidden=False) == (
        "ok", {"hidden": False})
    assert await listed() == {"nofix001", "held0001"}
    assert await ask(hass, panel.ws_conversation_hide, recording_id="held0001") == ("ok", {"hidden": True})
    place = flow.place("held0001")
    assert (place["stage"], place["outcome"], place["archived"], place["held"]) == (
        "Your household", "archived", True, None)
    assert await listed() == {"nofix001"}
    await ask(hass, panel.ws_conversation_hide, recording_id="held0001", hidden=False)
    place = flow.place("held0001")
    assert (place["stage"], place["held"], place["archived"]) == ("Your Assist", None, False)
    assert flow.waiting("held0001")
    assert await listed() == {"nofix001", "held0001"}
    await ask(hass, panel.ws_conversation_test, recording_id="nofix001")  # It stands otherwise now: off on its own.
    assert await listed() == {"held0001"}


@pytest.mark.asyncio
async def test_a_picked_run_takes_what_was_picked_and_sends_in_one_from_your_household_first(improver):
    """P0.7: a picked run admits the picked ones (new, or held: released); one picked from Your household (looked
    smooth, no fix found, ...) is sent in again first, for a new round past the quick check (DETAILS P9.3.1); those
    not kept are refused."""
    hass, flow, pipeline = improver.hass, improver.runtime.flow, improver.runtime.pipeline
    improver.entry.options = {**improver.entry.options, "paused_stages": ["triage", "analyze", "propose", "write"]}
    seat(improver, "new00001", "Your Assist")
    seat(improver, "held0001", "Analyze", held="failures")
    seat(improver, "smooth01", "Your household", outcome="looked smooth")
    seat(improver, "nofix001", "Your household", outcome="no fix found")
    assert await ask(hass, panel.ws_run_start, confirmed=True, conversations=["new00001", "nope"]) == (
        "not_found", "1 of those conversations aren't kept.")
    assert pipeline.current is None and flow.place("new00001")["round"] == 0  # Nothing started, nothing moved.
    status, _ = await ask(hass, panel.ws_run_start, confirmed=True,
                          conversations=["new00001", "held0001", "smooth01", "nofix001"])
    assert status == "ok"
    await pipeline._task
    assert sorted(pipeline.current["admitted"]) == ["new00001", "nofix001", "smooth01"]
    assert pipeline.current["released"] == ["held0001"]
    assert flow.sent("smooth01")["again"] is True and flow.sent("new00001") is None  # Picked is not sent in.
    assert [flow.place(rid)["stage"] for rid in ("new00001", "smooth01", "held0001", "nofix001")] == [
        "Triage", "Analyze", "Analyze", "Analyze"]
    assert flow.sent("nofix001")["again"] is True and flow.record("nofix001")["round"] == 2  # A new round.


def test_a_conversation_home_assistant_answered_itself_names_the_voice_assistant_that_let_it(monkeypatch):
    """DETAILS P9.9.9: the Can't improve chip quotes what the household said, cut short, and names the voice assistant
    whose Prefer handling commands locally setting let Home Assistant answer: the one the recording kept; else the one
    using its AI (any AI, when the recording names none) that answers first, if just one; else the AI's own name."""
    from custom_components.assist_improver import household
    from custom_components.assist_improver.conversations import cant_improve
    voice = lambda pid, name, first: SimpleNamespace(id=pid, name=name, prefer_local_intents=first,  # noqa: E731
                                                     conversation_engine="conversation.claude")
    kept = {"p1": voice("p1", "Nova", True), "p2": voice("p2", "Kitchen", False), "p3": SimpleNamespace(
        id="p3", name="Home Assistant", prefer_local_intents=True, conversation_engine="conversation.home_assistant")}
    # Home Assistant's assistants, through assist_pipeline's own functions.
    monkeypatch.setattr(household, "async_get_pipelines", lambda hass: list(kept.values()))
    monkeypatch.setattr(household, "async_get_pipeline", lambda hass: kept["p3"])
    states = {"conversation.claude": SimpleNamespace(name="Claude")}
    hass = SimpleNamespace(data={}, states=SimpleNamespace(get=states.get))
    said = {"turn": 2, "quote": "Turn on the porch light and the lamp in the living room, then close all the blinds",
            "agent_id": "conversation.claude", "pipeline_id": None}
    assert cant_improve(hass, said) == {"quote": "Turn on the porch light and the lamp in the living room…",
                                        "assistant": "Nova"}
    assert cant_improve(hass, {**said, "pipeline_id": "p2"})["assistant"] == "Kitchen"  # The one it ran through.
    assert cant_improve(hass, {**said, "agent_id": None})["assistant"] == "Nova"  # The one AI's answers first.
    kept["p2"].prefer_local_intents = True
    assert cant_improve(hass, said)["assistant"] == "Claude"  # Two answer first: the AI's own name.
    assert cant_improve(hass, {**said, "agent_id": None})["assistant"] == "Assist"
    assert cant_improve(hass, {**said, "quote": "Lights off"})["quote"] == "Lights off"
