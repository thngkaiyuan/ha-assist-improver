"""Bug hunt, analyze: the quick check and the analysis, through the real pipeline (kit.py: the AI roles
scripted through their real tools, the recorder's store, the run log and its index, the panel's own commands). Each
test names what the product promises the stage does, and failed until the bug it found was fixed.
"""

import json
from types import SimpleNamespace

import pytest

from recordings import TARGET, make_recording
from test_conversations import ask
from kit import FIRST, improver, variant  # noqa: F401 - improver is a fixture.
from custom_components.assist_improver import analysis, analyzer, panel

FINE = "Switch the fixture relay off."  # Two requests, each done as asked: the analysis finds no trouble.
NOTE = "It should have said less."


def later_steps_blocked(improver):
    improver.entry.options = {**improver.entry.options, "paused_stages": ["propose", "test", "write"]}


@pytest.mark.asyncio
async def test_an_analysis_accepted_before_the_model_errs_is_kept_not_retried(improver, monkeypatch):
    """P9.15.1: the last accepted answer counts. The analyzer's answer comes from submit_goals, so when the provider
    errs after the model's answer was accepted (overloaded on its closing round), the conversation is analysed:
    Propose reads that analysis, and it isn't held at Analyze to be paid for again."""
    model, flow = improver.model, improver.runtime.flow
    later_steps_blocked(improver)
    await improver.arrives(improver.friction)
    answer = model.journey_analyzer

    async def errs_after_answering(hass, payload, tools):
        await answer(hass, payload, tools)  # submit_goals accepted the goals.
        raise RuntimeError("Overloaded")  # The provider fails on the model's closing round.
    monkeypatch.setattr(model, "journey_analyzer", errs_after_answering)
    run = await improver.run()
    assert run["status"] == "done", run["error"]
    assert flow.analysis("friction01") is not None and flow.place("friction01")["stage"] == "Propose", (
        flow.place("friction01"), run["progress"])  # Past Analyze (and waiting at Propose, which is blocked here).


HOUSEHOLD_LINE = "When Alex says my fan, they mean the desk fan."


def with_household_knowledge(record):
    """A conversation whose assistant ran with the household's knowledge and the speaker's name in its instructions,
    as Home Assistant's Assist reads them out (household.render), and the knowledge recorded with the home."""
    instructions = ("You are a voice assistant for Home Assistant. The person speaking is Alex.\n"
                    "Household knowledge (facts and preferences the household has confirmed; follow them):\n"
                    f"- {HOUSEHOLD_LINE}")
    for message in record["chat_log"]["content"]:
        if message["role"] == "system":
            message["content"] = instructions
    for turn in record["turns"]:
        for preparation in turn["preparations"]:
            for message in preparation["content"]:
                if message["role"] == "system":
                    message["content"] = instructions
        turn["source"]["household"] = {"everyone": [HOUSEHOLD_LINE], "people": {}}
    return record


def seen_by(tools, view):
    """Everything a role can read through these tools without being told what to look for: each tool that needs no
    argument, and every turn."""
    out = []
    for tool in tools:
        try:
            out.append(tool.run(turns=list(range(1, len(view.digest["turns"]) + 1))) if tool.name == "read_turn"
                       else tool.run())
        except TypeError:  # It needs an argument (a device, an area, a query, an answer).
            continue
    return json.dumps(out, ensure_ascii=False, default=str)


def test_the_analyzer_knows_the_household_as_the_other_roles_do():
    """Kai, 2026-10-23, on giving "the analyzer read access to the personalizations already applied, with no second
    store": "The analyzer needs to know me / the household as well as the other agents do. they should all have the
    data they need to understand and contextualize the tasks they're doing"; 2026-10-24: "everything that the
    Assistant could see, the analyzer should've been able to see". The quick check gets Assist's instructions (who's
    speaking, the household knowledge); the proposer and reviewer read them (read_assistant_instructions, the
    household knowledge). Before the fix, the analyzer, whose job is to judge which device the user meant, got
    neither: not in its instructions, its input, or any of its tools."""
    from custom_components.assist_improver import triage
    from custom_components.assist_improver.exploration import TOOLS, SessionView
    view = SessionView(with_household_knowledge(make_recording()))
    assert HOUSEHOLD_LINE in json.dumps(triage.payload(view), ensure_ascii=False)  # The quick check knows it.
    runtime = SimpleNamespace(recorder=SimpleNamespace(index={view.digest["recording_id"]: {}}), improvements=None)
    instructions, payload, tools, _, _ = analysis.analyzer_input(runtime, view.digest["recording_id"], view)
    visible = instructions + json.dumps(payload, ensure_ascii=False) + seen_by(
        [*tools, *(tool(view) for tool in TOOLS)], view)
    assert HOUSEHOLD_LINE in visible
    assert "The person speaking is Alex" in visible or "Alex" in json.dumps(payload, ensure_ascii=False)


def test_the_note_a_conversation_was_sent_in_with_reads_as_its_last_turn():
    """The analyzer is told the note is the conversation's last turn: read_turn reads it there, as any turn, with
    nothing Assist did; without a note there's no such turn."""
    from custom_components.assist_improver.exploration import ReadTurn, SessionView
    view = SessionView(make_recording())
    last = len(view.digest["turns"]) + 1
    runtime = SimpleNamespace(recorder=SimpleNamespace(index={view.digest["recording_id"]: {}}), improvements=None)
    _, payload, _, submit, _ = analysis.analyzer_input(runtime, view.digest["recording_id"], view,
                                                       {"note": " Say less. "})
    assert payload["conversation"][-1]["turn"] == last  # The same turn in its input, read_turn and its checks.
    assert ReadTurn(view).run(turn=last) == {"turn": last, "user_says": "Say less.", "sent_with_it": True}
    assert ReadTurn(view).run(turns=[1, last])["turns"][1]["sent_with_it"]
    assert f"turns are 1 to {last}" in view.read_turn(last + 1)["error"]
    [found] = analyzer.problems(view, {"goals": [], "not_requests": [], "question_for_user": ""}, note=submit.note)
    assert found.startswith(f"Turns {', '.join(map(str, range(1, last + 1)))} have no goal")
    assert "error" in SessionView(make_recording()).read_turn(last)


def answering(model, monkeypatch, answer, first=FIRST):
    """The analyzer answers this conversation with `answer` through submit_goals, once, whatever it's told."""
    scripted = model.journey_analyzer

    async def analyzer(hass, payload, tools):
        if payload["conversation"][0]["user_says"] != first:
            return await scripted(hass, payload, tools)
        model.calls.append(("analyzer", first))
        model.submitted = await model.use(hass, tools["submit_goals"], answer)
        return "Done."
    monkeypatch.setattr(model, "journey_analyzer", analyzer)


ASKED = {"turn": 1, "summary": "Whether the relay is on", "kind": "fact", "fact": "The relay's state, on or off",
         "entity_ids": [TARGET]}
STEPS = [{"turn": n, "summary": "Asks again", "kind": "step", "serves": 1} for n in (2, 3, 4)]


@pytest.mark.asyncio
@pytest.mark.parametrize("gives", [True, False], ids=["right", "wrong"])
async def test_what_the_user_asked_to_know_is_judged_once_on_the_reply_that_ends_its_run(improver, monkeypatch, gives):
    """DETAILS P9.7.4, P9.10: a fact root about the home is judged at analysis, once, on the reply that ends its run of
    steps (turn 4 here: the replies before it are unmet by the steps that followed them), with the home as it was as
    that turn started (the device it's about, as recorded). The judge's verdict is the root's state; a wrong answer is
    trouble the proposer is told about, with the judge's reason."""
    model = improver.model
    later_steps_blocked(improver)
    await improver.arrives(improver.friction)
    answering(model, monkeypatch, {"goals": [ASKED, *STEPS], "not_requests": [], "question_for_user": ""})
    judge = model.feedback_check

    async def verdict(hass, payload, tools):
        found = await judge(hass, payload, tools)
        return {"answers": [{**a, "gives": gives, "why": "It says." if gives else "It says off; it was on."}
                            for a in found["answers"]]} if "questions" in payload else found
    monkeypatch.setattr(model, "feedback_check", verdict)
    run = await improver.run()
    assert run["status"] == "done", run["error"]
    [asked] = model.fact_checks
    [question] = asked["questions"]
    assert question["turn"] == 4 and question["asked"] == [{"turn": 1}, {"user_said": FIRST}]
    assert question["needs"] == ASKED["fact"] and "form" not in question
    [device] = question["home"]
    from custom_components.assist_improver.exploration import SessionView
    as_recorded = SessionView(improver.friction).sources[3]["states"][TARGET]  # As turn 4 started.
    assert (device["entity_id"], device["state"], device["attributes"]) == (
        TARGET, as_recorded["state"], as_recorded["attributes"])
    assert [turn[0] for turn in asked["turns"]] == [{"turn": 4}]
    analysis = improver.runtime.flow.analysis("friction01")
    root = analysis["goals"][0]
    assert root["reached"] is gives and root["answered"]["turn"] == 4
    from custom_components.assist_improver.hypotheses.goals import friction
    unmet = [f for f in friction(analysis) if f["kind"] == "unmet"]
    assert unmet == ([] if gives else [{"turn": 1, "kind": "unmet", "summary": ASKED["summary"],
                                        "why": "It says off; it was on."}])
    from custom_components.assist_improver import contracts, records  # The judge's call, kept with the analysis run.
    assert contracts.problems({"fact_check": {"status": "completed", "answers": []}}, records.Analysis,
                              required=False) == []


@pytest.mark.asyncio
async def test_a_question_beyond_the_home_is_judged_only_on_the_form_asked_for_and_without_one_not_at_all():
    """DETAILS P9.10.4: the improver holds no truth beyond the home and what Assist's tools returned; only the form
    the user asked for is checkable. One naming no device, in a turn whose tools returned nothing, with no form isn't
    judged: it can't be told."""
    from custom_components.assist_improver import simulate
    from custom_components.assist_improver.exploration import SessionView
    view = SessionView(make_recording())
    asked = []

    async def check(hass, entity_id, questions, turns):
        asked.append(questions)
        return {"status": "completed", "usage": {}, "answers": [
            {"goal": q["goal"], "turn": q["turn"], "gives": True, "why": "One sentence."} for q in questions]}
    sky = {"turn": 2, "summary": "Why the sky is blue", "kind": "fact", "fact": "Why the sky is blue"}  # No tool call.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(simulate, "check_facts", check)
        assert await simulate.answered(None, "ai_task.judge", view, [sky]) == ({}, None) and not asked
        unrecorded = {**sky, "entity_ids": ["switch.registered_only"]}  # In the registry, with no state recorded.
        assert await simulate.answered(None, "ai_task.judge", view, [unrecorded]) == ({}, None) and not asked
        found, _ = await simulate.answered(None, "ai_task.judge", view, [{**sky, "quote": "the fixture relay"}])
    assert found == {0: {"turn": 2, "gives": True, "why": "One sentence."}}
    [[question]] = asked
    assert question["form"] == "the fixture relay" and "home" not in question


@pytest.mark.asyncio
async def test_a_question_answered_from_a_tools_result_is_judged_with_what_that_turn_returned(monkeypatch):
    """DETAILS P9.10.4: a question is judged against the home and what Assist's tools returned in its turn, so one
    about anything a tool reported is judgeable though it names no device and asks no form; the judge reads the
    result in that turn."""
    from custom_components.assist_improver import simulate
    from custom_components.assist_improver.exploration import SessionView
    record = make_recording()
    content = record["chat_log"]["content"]
    at = next(i for i, m in enumerate(content) if m.get("content") == "Recorded response 2.")
    call = {"tool_name": "HassTimerStatus", "tool_args": {}, "id": "call-2-timer", "external": False}
    result = {"data": {"timers": [{"name": "Pasta", "seconds_left": 240}]}, "error": False}
    content[at:at] = [{"role": "assistant", "agent_id": "conversation.fixture", "tool_calls": [call],
                       "created": content[at]["created"]},
                      {"role": "tool_result", "agent_id": "conversation.fixture", "tool_call_id": call["id"],
                       "tool_name": call["tool_name"], "result": result, "created": content[at]["created"]}]
    seen = []

    async def check(hass, entity_id, questions, turns):
        seen.append((questions, turns))
        return {"status": "completed", "usage": {}, "answers": [
            {"goal": q["goal"], "turn": q["turn"], "gives": True, "why": "."} for q in questions]}
    monkeypatch.setattr(simulate, "check_facts", check)
    timer = {"turn": 2, "summary": "Time left on the pasta timer", "kind": "fact", "fact": "Minutes left"}
    found, _ = await simulate.answered(None, "ai_task.judge", SessionView(record), [timer])
    assert found == {0: {"turn": 2, "gives": True, "why": "."}}
    [([question], [turn])] = seen
    assert "home" not in question and "form" not in question
    [did] = next(part["assistant_did"] for part in turn if "assistant_did" in part)
    assert did["called"] == "HassTimerStatus" and "Pasta" in did["result"] and "240" in did["result"]


@pytest.mark.parametrize("result, judgeable", [
    (None, False),
    ({"data": {"speech": {}, "response_type": "action_done", "data": {"success": [{"id": "light.lamp"}]}}}, False),
    ({"data": {"result": {}}}, False),
    ({"error": True, "data": {"error": "HomeAssistantError", "error_text": "Lamp unreachable"}}, False),
    ({"data": {"timers": [{"name": "Pasta", "seconds_left": 240}]}}, True),
])
def test_a_question_is_judged_on_what_a_tool_reported_not_on_an_actions_report(result, judgeable):
    """DETAILS P9.10.4: a question beyond the home with no form asked is judgeable when a tool in its turns reported
    something; an action's result reports only what it did (an intent's report, a script returning nothing, an
    error), so a lamp switched on beside the question doesn't make it judgeable."""
    from custom_components.assist_improver import simulate
    question = {"turn": 1, "kind": "fact", "fact": "How far away the moon is", "key": "1:0", "label": 1}
    calls = [{"tool": "HassTurnOn", "args": {"name": "Lamp"}, "result": result}] if result else []
    view = SimpleNamespace(source={"states": {"light.lamp": {"state": "off"}}}, calls={1: calls})
    assert simulate.judgeable(question, view, [question]) is judgeable


def judging_facts(gives=True):
    """A stand-in check_facts: every question answered (gives) on its turn; what it was asked: each call's questions."""
    asked = []

    async def check(hass, entity_id, questions, turns):
        asked.append(questions)
        return {"status": "completed", "usage": {}, "answers": [
            {"goal": q["goal"], "turn": q["turn"], "gives": gives, "why": "."} for q in questions]}
    return asked, check


@pytest.mark.asyncio
async def test_a_question_is_judged_on_a_conversation_the_analysis_reads_though_a_replay_couldnt(monkeypatch):
    """The judge reads the recording as the analysis does: a turn Home Assistant handled itself, or a compacted
    recording, doesn't stop it, and nothing the judge meets fails the analysis."""
    from custom_components.assist_improver import simulate
    from custom_components.assist_improver.exploration import SessionView
    record = make_recording()
    content = record["chat_log"]["content"]
    record["events"] = ([{"type": "created", "data": {"chat_log": {"content": content[:1]}}}]
                        + [{"type": "content_added", "data": {"content": m}} for m in content[1:]])
    record["chat_log"] = {**record["chat_log"], "content": content[:1]}
    for turn in record["turns"]:
        turn.pop("preparations", None), turn.pop("prepared", None)
    _, check = judging_facts()
    monkeypatch.setattr(simulate, "check_facts", check)
    for recorded in (record, {**make_recording(), "compact": True}):
        found, _ = await simulate.answered(None, "ai_task.judge", SessionView(recorded), [ASKED])
        assert found == {0: {"turn": 1, "gives": True, "why": "."}}


def test_the_judge_sees_the_home_as_assist_saw_it_before_its_reply_and_only_devices_the_recording_holds():
    """What changed after the reply isn't part of the answer; a device the recording holds no state of isn't shown as
    if it were known."""
    from custom_components.assist_improver import simulate
    from custom_components.assist_improver.exploration import SessionView
    view = SessionView(make_recording())
    question = {**ASKED, "turn": 4, "entity_ids": [TARGET, "switch.registered_only"]}
    [device] = simulate.question_of(question, view, 4, "Again, off then on.")["home"]  # Turn 4: replied at :24.
    assert device["during_the_turn"] == [{"old": "on", "new": "off", "by": "assistant"},
                                         {"old": "off", "new": "on", "by": "assistant"}]
    view.replies[3] = "2026-01-01T00:00:20+00:00"  # Had it replied before the second change, that one is left out.
    [device] = simulate.question_of(question, view, 4, "Again, off then on.")["home"]
    assert device["during_the_turn"] == [{"old": "on", "new": "off", "by": "assistant"}]


def test_the_judge_sees_a_devices_readings_in_the_turn_as_one_line():
    """A sensor the question is about, reporting through the turn: its readings before the reply are one line, from
    the first to the last, however many there are."""
    from custom_components.assist_improver import simulate
    from custom_components.assist_improver.exploration import NOBODY, SessionView
    record = make_recording()
    for turn in record["turns"]:
        turn["source"]["states"]["sensor.power"] = {"state": "100", "attributes": {"unit_of_measurement": "W"}}
    record["state_changes"] += [{"entity_id": "sensor.power", "at": f"2026-01-01T00:00:{17 + n}+00:00",
                                 "old": {"state": str(100 + n)}, "new": {"state": str(101 + n), "attributes": {
                                     "unit_of_measurement": "W"}}} for n in range(5)]
    question = {**ASKED, "turn": 4, "entity_ids": ["sensor.power"]}
    [device] = simulate.question_of(question, SessionView(record), 4, "Again, off then on.")["home"]
    assert device["during_the_turn"] == [{"old": "100", "new": "105", "by": NOBODY, "readings": 5}]


def test_in_a_replay_the_judge_sees_the_home_as_that_replay_had_it():
    """A replay's own changes before the turn decide the state (an attribute recorded for another state is left out);
    in the turn, the replay's own changes count and people's only up to the reply, never what moved between turns."""
    from custom_components.assist_improver import simulate
    from custom_components.assist_improver.exploration import SessionView
    view = SessionView(make_recording())
    world = SimpleNamespace(changes={TARGET: [
        {"at": "2026-01-01T00:00:05+00:00", "during_turn": 1, "old": "on", "new": "off", "by": "assistant"},
        {"at": "2026-01-01T00:00:08+00:00", "during_turn": 1, "old": "off", "new": "on",
         "by": "someone else, between turns"},
        {"at": "2026-01-01T00:00:09+00:00", "during_turn": 2, "old": "off", "new": "on", "by": "assistant"},
        {"at": "2026-01-01T00:00:59+00:00", "during_turn": 2, "old": "on", "new": "off", "by": "a person"}]})
    [device] = simulate.question_of({**ASKED, "turn": 2}, view, 2, "", world)["home"]
    assert device["state"] == "on" and "attributes" not in device  # Recorded "off" then: its attributes aren't these.
    assert device["during_the_turn"] == [{"old": "off", "new": "on", "by": "assistant"}]  # The person came after.


def test_a_question_asked_again_in_the_note_is_unmet_by_code_and_only_its_own_steps_are_its_run():
    """The note it was sent in with asks again, so the last reply said gave nothing either (no judge needed); a step
    continuing a repair isn't one of the question's steps."""
    goals = [ASKED, {"turn": 2, "summary": "Fridge back on", "kind": "state", "entity_id": "switch.fridge",
                     "state": "on", "serves": 1}, {"turn": 3, "summary": "Again", "kind": "step", "serves": 2},
             {"turn": 4, "summary": "Asks again", "kind": "step", "serves": 1},
             {"turn": 5, "summary": "Asks once more", "kind": "step", "serves": 4}]
    assert analyzer.question_run(goals, ASKED) == [1, 4, 5]
    other = [ASKED, {"turn": 2, "summary": "Lamp on", "kind": "state", "entity_id": "light.lamp", "state": "on"},
             {"turn": 2, "summary": "Asks again", "kind": "step", "serves": 1},
             {"turn": 3, "summary": "The lamp again", "kind": "step", "serves": 2}]
    assert analyzer.question_run(other, ASKED) == [1, 2]  # Turn 3 is the lamp's.


@pytest.mark.asyncio
async def test_two_questions_asked_in_one_turn_each_get_their_own_verdict(monkeypatch):
    """A verdict belongs to its question, not its turn: two asked at once are judged each on its own."""
    from custom_components.assist_improver import simulate
    from custom_components.assist_improver.exploration import SessionView

    async def check(hass, entity_id, questions, turns):
        return {"status": "completed", "usage": {}, "answers": [
            {"goal": q["goal"], "turn": q["turn"], "gives": "form" in q, "why": q["needs"]} for q in questions]}
    monkeypatch.setattr(simulate, "check_facts", check)
    briefly = {"turn": 1, "summary": "Briefly", "kind": "fact", "fact": "Said briefly", "quote": "the fixture relay"}
    found, _ = await simulate.answered(None, "ai_task.judge", SessionView(make_recording()), [ASKED, briefly])
    assert found == {0: {"turn": 1, "gives": False, "why": ASKED["fact"]},
                     1: {"turn": 1, "gives": True, "why": "Said briefly"}}


@pytest.mark.asyncio
async def test_a_question_asked_again_in_the_note_needs_no_judge(monkeypatch):
    from custom_components.assist_improver import simulate
    from custom_components.assist_improver.exploration import SessionView
    asked, check = judging_facts()
    monkeypatch.setattr(simulate, "check_facts", check)
    noted = [ASKED, *STEPS, {"turn": 5, "summary": "Asks again in the note", "kind": "step", "serves": 1}]
    found, heard = await simulate.answered(None, "ai_task.judge", SessionView(make_recording()), noted)
    assert found == {0: {"turn": 4, "gives": False, "why": simulate.ASKED_IN_NOTE}} and heard is None and not asked
    in_note = [{**ASKED, "turn": 5}]  # A question at the note's turn (an answer never accepted): no reply to judge.
    assert await simulate.answered(None, "ai_task.judge", SessionView(make_recording()), in_note) == ({}, None)


@pytest.mark.asyncio
async def test_a_key_the_judge_adds_to_an_answer_is_dropped_not_held_against_it(monkeypatch):
    from custom_components.assist_improver import reflective, simulate

    async def run_role(hass, **kwargs):
        return {"status": "completed", "usage": {}, "error": None, "output": {"answers": [
            {"goal": "1", "turn": 1, "gives": True, "why": ".", "confidence": 0.9}]}}
    monkeypatch.setattr(reflective, "run_role", run_role)
    found = await simulate.check_facts(None, "ai_task.judge", [{"goal": "1", "turn": 1}], [])
    assert found["status"] == "completed" and found["answers"] == [{"goal": "1", "turn": 1, "gives": True, "why": "."}]


@pytest.mark.asyncio
@pytest.mark.parametrize("output", [{"answers": [{"goal": "1", "turn": 1, "gives": "false", "why": "."}]},
                                    {"answers": [{"goal": "1", "turn": 1, "gives": False}]}, "not json"],
                         ids=["a-string", "no-why", "not-an-object"])
async def test_an_answer_the_judge_didnt_give_in_the_shape_asked_counts_as_none(monkeypatch, output):
    """A provider may not enforce the structure: an ill-formed answer is no answer, never a verdict."""
    from custom_components.assist_improver import reflective, simulate

    async def run_role(hass, **kwargs):
        return {"status": "completed", "output": output, "usage": {}, "error": None}
    monkeypatch.setattr(reflective, "run_role", run_role)
    question = {"goal": "1", "turn": 1}
    assert (await simulate.check_facts(None, "ai_task.judge", [question], []))["status"] == "failed"
    objection = {"goal": "1", "judge": [1]}
    conduct = {"answers": [{"goal": "1", "turn": 1, "shows": "no", "why": "."}]} if isinstance(output, dict) else output

    async def conduct_role(hass, **kwargs):
        return {"status": "completed", "output": conduct, "usage": {}, "error": None}
    monkeypatch.setattr(reflective, "run_role", conduct_role)
    assert (await simulate.check_conduct(None, "ai_task.judge", [objection], [], []))["status"] == "failed"


@pytest.mark.asyncio
async def test_an_impossible_answer_never_moves_on_from_the_analysis(improver, monkeypatch):
    """docs/DESIGN.md 'Models judge; code derives': "Models answer through tools that validate their answer against the
    household and reject it with reasons until it is valid, so a malformed or impossible answer never moves on." DETAILS
    P1.11.2: "A conversation's goals are settled before anything is tested against them." Before the fix, when every
    submission was rejected, the best rejected one was kept as the analysis: here a goal for the relay to be "open", a
    state a switch doesn't have. It read as unmet friction, Propose was asked to fix it, and the test stage replayed
    toward a state the relay can never reach, using up the goal's tests."""
    model = improver.model
    later_steps_blocked(improver)
    await improver.arrives(improver.friction)
    answering(model, monkeypatch, {"goals": [{"turn": 1, "summary": "Relay open", "kind": "state",
                                              "entity_id": TARGET, "state": "open"}],
                                   "not_requests": [], "question_for_user": ""})
    run = await improver.run()
    assert run["status"] == "done", run["error"]
    assert model.submitted["accepted"] is False  # Rejected: a switch has no state "open". The model gave up.
    kept = improver.runtime.flow.analysis("friction01")
    impossible = [g for g in (kept or {}).get("goals", []) if g.get("state") == "open"]
    assert not impossible, (kept or {}).get("problems")


def test_a_persons_own_change_after_assists_is_never_counted_as_assists():
    """exploration.SessionView._who: "Assist, a signed-in person, or something else; recordings before identity capture
    use Assist's results". The quick check is told to treat "someone changes the same device soon after Assist did" as
    friction (triage.INSTRUCTIONS); the analyzer reads who made each change (read_turn, device_details) to tell a
    user undoing what Assist did (a sub-goal). Here the recording has identity: Assist turned the relay off with its
    request's context; 24 seconds later Alex turned it back on by hand in the app (his own fresh context, no parent).
    Before the fix, because the relay is named in Assist's tool result for that turn, the fallback meant for older
    recordings still ran and Alex's change was labelled the assistant's: both roles saw Assist flip-flopping, and no
    one stepping in."""
    from custom_components.assist_improver import triage
    from custom_components.assist_improver.exploration import SessionView
    record = variant(make_recording(), "byhand01", "Toggle the fixture relay.", turns=1)
    off = record["state_changes"][0]
    assert off["new"]["context"]["id"] == "fixture-context-1"  # Assist's, with the request's context.
    record["state_changes"].append({"entity_id": TARGET, "at": "2026-01-01T00:00:30+00:00", "old": off["new"], "new": {
        **off["old"], "context": {"id": "alex-in-the-app", "parent_id": None, "user_id": "alex"}}})
    view = SessionView(record)
    assert [c["by"] for c in view.changes[TARGET]] == ["assistant", "a person (signed in to Home Assistant)"]
    shown = triage.payload(view)["turns"][0]["home_changes"]
    assert [c["by"] for c in shown] == ["assistant", "a person (signed in to Home Assistant)"]


def test_in_a_recording_from_before_identity_capture_assists_own_changes_are_assists():
    """exploration.SessionView._who: "recordings before identity capture use Assist's results". The first versions
    kept no request context (it reads {"omitted_type": "Context"}), and there Assist's own changes carry the signed-in
    person's context (theirs, no parent), as a change by hand in the app does. The rule for a person's own change ran
    first, so Assist's changes read as the person's: in one such "Turn off the fan." Assist turned the fan off, and code
    derived the goal as not reached (its label says reached)."""
    from custom_components.assist_improver.analyzer import reached
    from custom_components.assist_improver.exploration import SessionView
    record = variant(make_recording(), "noident1", "Toggle the fixture relay.", turns=1)
    record["turns"][0]["prepared"]["context"]["context"] = {"omitted_type": "Context"}  # As the first versions kept it.
    record["state_changes"][0]["new"]["context"] = {"id": "alex-asks", "parent_id": None, "user_id": "alex"}
    view = SessionView(record)
    assert [c["by"] for c in view.changes[TARGET]] == ["assistant"]  # Its result for the turn names the relay.
    assert reached(view, {"turn": 1, "kind": "state", "entity_id": TARGET, "state": "off"}) is True


@pytest.mark.asyncio
async def test_a_note_survives_sending_the_conversation_in_again_without_one(improver, monkeypatch):
    """DETAILS P8.9.4: "An earlier note is kept when the conversation is sent in again without one"; P8.8.2: sent in
    again, it gets one fresh full analysis that reads the note. Alex sent this one in with a note; no fix was found for
    it; he sends it in again for a fresh look, without typing the note again. Its mark on the flow record keeps the
    note, and the fresh analysis hears it, as the conversation's last turn too (P8.9.1)."""
    hass, model, flow = improver.hass, improver.model, improver.runtime.flow
    improver.entry.options = {**improver.entry.options, "paused_stages": ["write"]}
    await improver.arrives(variant(make_recording(), "fine0001", FINE, turns=2))
    heard, run_role = [], analysis.run_role

    async def listening(hass_, **kwargs):
        if kwargs["role"] == "journey_analyzer":
            heard.append((kwargs["payload"].get("filed_by_user"), kwargs["payload"]["conversation"][-1]["user_says"]))
        return await run_role(hass_, **kwargs)
    monkeypatch.setattr(analysis, "run_role", listening)
    model.proposer_unfixable.add(FINE)
    assert (await ask(hass, panel.ws_conversation_send, recording_id="fine0001", note=NOTE))[0] == "ok"
    await improver.runtime.pipeline._task  # Its own run, at once.
    assert flow.place("fine0001")["outcome"] == "no fix found"

    model.proposer_unfixable.discard(FINE)
    assert (await ask(hass, panel.ws_conversation_send, recording_id="fine0001"))[0] == "ok"  # Again, no note.
    mark = flow.record("fine0001")["sent"]
    assert mark["note"] == NOTE and mark["again"] is True
    await improver.runtime.pipeline._task
    assert heard == [({"note": NOTE}, NOTE)] * 2  # The fresh analysis heard it too.


def test_a_persons_brightness_correction_after_assist_reaches_the_quick_check_and_the_analyzer():
    """triage.INSTRUCTIONS: it's not smooth "when someone changes the same device soon after Assist did"; the quick
    check gets "what changed in the home" (triage.payload) and the analyzer reads it with read_turn. Assist turned the
    desk light on at 30%; a few seconds later Alex turned it up to 80% by hand. The light stays "on", so before the fix
    read_turn filed Alex's correction under attribute_only_updates_elsewhere (a key since gone), as an entity ID without
    who, what or when, and the quick check's home_changes left it out entirely: it saw Assist do as asked and nobody
    step in."""
    from custom_components.assist_improver import triage
    from custom_components.assist_improver.exploration import SessionView
    record = variant(make_recording(), "dimmer01", "Toggle the fixture relay.", turns=1)
    light = "light.desk"
    first = record["turns"][0]["source"]
    first["states"][light] = {"entity_id": light, "state": "off", "attributes": {
        "friendly_name": "Desk light", "supported_color_modes": ["brightness"], "brightness": None}}
    first["entities"][light] = {"name": "Desk light", "aliases": [], "area_id": "fixture_area"}
    first["exposure"][light] = True
    assist = record["state_changes"][0]["new"]["context"]
    on = {"entity_id": light, "state": "on", "attributes": {"friendly_name": "Desk light",
                                                            "supported_color_modes": ["brightness"], "brightness": 77}}
    record["state_changes"] += [
        {"entity_id": light, "at": "2026-01-01T00:00:04.500000+00:00", "old": first["states"][light],
         "new": {**on, "context": dict(assist)}},  # Assist: on, at 30%.
        {"entity_id": light, "at": "2026-01-01T00:00:09+00:00", "old": on, "new": {
            **on, "attributes": {**on["attributes"], "brightness": 204},
            "context": {"id": "alex-dims-it-up", "parent_id": None, "user_id": "alex"}}}]  # Alex: 80%, by hand.
    view = SessionView(record)
    assert [c["by"] for c in view.changes[light]] == ["assistant", "a person (signed in to Home Assistant)"]
    shown = [c for c in triage.payload(view)["turns"][0]["home_changes"] if c["device"] == "Desk light"]
    assert len(shown) == 2, shown  # Assist's change, and Alex's correction with who made it.
    read = [c for c in view.read_turn(1)["state_changes_during_turn"] if c["entity_id"] == light]
    assert len(read) == 2, read


def test_a_signed_in_persons_quick_correction_is_never_a_follow_on_of_assists_change():
    """exploration.SessionView._mark_follow_ons once took any change right after an assistant change on the same
    physical device for a follow-on, not someone else (meant for a device's own reaction: a fan's climate turning off
    with it). reached() counts follow-ons as Assist's. Here Assist set the desk light to 30% and Alex, signed in,
    turned it up to 80% by hand five seconds later: the same entity, his own fresh context. Before the fix it was
    labelled a follow-on of Assist's change, so code read the goal "desk light at 80%" as reached by Assist: the unmet
    goal, Alex's correction, disappeared as friction."""
    from custom_components.assist_improver.analyzer import reached
    from custom_components.assist_improver.exploration import SessionView
    record = variant(make_recording(), "dimmer02", "Toggle the fixture relay.", turns=1)
    light = "light.desk"
    first = record["turns"][0]["source"]
    first["states"][light] = {"entity_id": light, "state": "off", "attributes": {
        "friendly_name": "Desk light", "supported_color_modes": ["brightness"], "brightness": None}}
    first["entities"][light] = {"name": "Desk light", "aliases": [], "area_id": "fixture_area", "device_id": "desk-lamp"}
    first["devices"]["desk-lamp"] = {"name": "Desk lamp"}
    first["exposure"][light] = True
    assist = record["state_changes"][0]["new"]["context"]
    on = {"entity_id": light, "state": "on", "attributes": {"friendly_name": "Desk light",
                                                            "supported_color_modes": ["brightness"], "brightness": 77}}
    record["state_changes"] += [
        {"entity_id": light, "at": "2026-01-01T00:00:04.500000+00:00", "old": first["states"][light],
         "new": {**on, "context": dict(assist)}},  # Assist: on, at 30%.
        {"entity_id": light, "at": "2026-01-01T00:00:09.500000+00:00", "old": on, "new": {
            **on, "attributes": {**on["attributes"], "brightness": 204},
            "context": {"id": "alex-dims-it-up", "parent_id": None, "user_id": "alex"}}}]  # Alex: 80%, by hand.
    view = SessionView(record)
    assert [c["by"] for c in view.changes[light]] == ["assistant", "a person (signed in to Home Assistant)"]
    goal = {"turn": 1, "kind": "state", "entity_id": light, "state": "on", "brightness_pct": 80}
    assert reached(view, goal) is False  # Alex got it to 80%, not Assist.


def test_a_goal_the_checks_threw_out_takes_only_its_own_turns_out_of_the_trouble():
    """DESIGN.md: an impossible answer never moves on; the run keeps only the goals the checks found nothing wrong
    with (analyzer.valid_part). A turn left with no goal at all isn't an extra turn (that would invent trouble): it's
    set aside, with why. A turn whose sub-goal alone was thrown out still served its goal: it stays an extra turn,
    never erased as trouble. A sub-goal of a goal thrown out serves nothing: it goes with it."""
    from custom_components.assist_improver.analyzer import extra_turns, valid_part
    relay = {"kind": "state", "entity_id": "switch.fixture_relay", "state": "on"}
    nope = {"kind": "state", "entity_id": "switch.nope", "state": "on"}
    problem = "there is no device 'switch.nope' in this household"

    def kept(goals, wrong, turns=3):
        part = valid_part({"goals": goals, "not_requests": [], "question_for_user": ""},
                          [f"Goal {n} (turn {goals[n - 1]['turn']}): {problem}" for n in wrong])
        return [g["turn"] for g in part["goals"]], [r["turn"] for r in part["not_requests"]], extra_turns(
            turns, part["goals"], part["not_requests"])
    # A sub-goal thrown out: its turn still served goal 1, an extra turn as before, its trouble on that goal (only what
    # it served stays, not the device the household lacks).
    assert kept([{"turn": 1, "summary": "On", **relay}, {"turn": 2, "summary": "Again", **nope, "serves": 1}],
                [2], turns=2) == ([1, 2], [], [2])
    from custom_components.assist_improver.hypotheses import friction_by_goal
    lamp = {"kind": "state", "entity_id": "light.fixture_lamp", "state": "on"}
    two = valid_part({"goals": [{"turn": 1, "summary": "On", **relay}, {"turn": 2, "summary": "Lamp", **lamp},
                                {"turn": 3, "summary": "Again", **nope, "serves": 1},
                                {"turn": 3, "summary": "Pick the right one", "kind": "conduct", "about": 1,
                                 "quote": "the right one", "serves": 1}],
                      "not_requests": [], "question_for_user": ""}, [f"Goal 3 (turn 3): {problem}"])
    assert two["goals"][2] == {"turn": 3, "summary": "Again", "kind": "state", "serves": 1}
    assert extra_turns(3, two["goals"], two["not_requests"]) == [3]  # An objection there too.
    assert [f["turn"] for f in friction_by_goal({**two, "extra_turns": [3]}).get(1, []) if f["kind"] == "extra turn"] == [3]
    # A goal thrown out, with its sub-goal: both turns set aside, no trouble invented.
    assert kept([{"turn": 1, "summary": "On", **relay}, {"turn": 2, "summary": "Nope", **nope},
                 {"turn": 3, "summary": "Nope again", **relay, "serves": 2}], [2]) == ([1], [2, 3], [])
    # Thrown out beside another goal at its turn: the turn keeps that goal; nothing set aside.
    assert kept([{"turn": 1, "summary": "On", **relay}, {"turn": 1, "summary": "Nope", **nope}], [2], turns=1) == (
        [1], [], [])


def test_someone_putting_back_what_assist_just_changed_is_never_its_follow_on():
    """exploration.SessionView._mark_follow_ons: a change right after Assist's on the same physical device is a
    follow-on (a blind reporting it got there), counted as Assist's own (reached()); one that puts the entity Assist
    changed back where it was is a correction, by whoever did it, signed in or not."""
    from custom_components.assist_improver.analyzer import reached
    from custom_components.assist_improver.exploration import SessionView

    def light_after(*later):
        record = variant(make_recording(), "flipback1", "Toggle the fixture relay.", turns=1)
        light = "light.desk"
        first = record["turns"][0]["source"]
        first["states"][light] = {"entity_id": light, "state": "off", "attributes": {"friendly_name": "Desk light"}}
        first["entities"][light] = {"name": "Desk light", "aliases": [], "area_id": "fixture_area",
                                    "device_id": "desk-lamp"}
        first["devices"]["desk-lamp"] = {"name": "Desk lamp"}
        assist = record["state_changes"][0]["new"]["context"]
        on = {"entity_id": light, "state": "on", "attributes": {"friendly_name": "Desk light"}}
        record["state_changes"] += [
            {"entity_id": light, "at": "2026-01-01T00:00:04.500000+00:00", "old": first["states"][light],
             "new": {**on, "context": dict(assist)}}, *[
                {"entity_id": light, "at": "2026-01-01T00:00:08+00:00", "old": on,
                 "new": {"entity_id": light, "state": state, "attributes": {"friendly_name": "Desk light", **extra},
                         "context": {"id": "at-the-wall", "parent_id": None, "user_id": None}}}
                for state, extra in later]]
        return SessionView(record), light
    view, light = light_after(("off", {}))  # Flipped back at the wall switch.
    assert view.changes[light][1]["by"].startswith("not the assistant")
    assert reached(view, {"turn": 1, "kind": "state", "entity_id": light, "state": "on"}) is False
    view, light = light_after(("on", {"brightness": 255}))  # The lamp's own report as it came on: its follow-on.
    assert view.changes[light][1]["by"].startswith("follow-on")


def test_only_carrying_assists_change_through_is_its_follow_on_whatever_else_it_changed_on_the_device():
    """exploration.SessionView._mark_follow_ons: on the entity Assist changed, a follow-on is the state it set, reported
    again or settling (a blind opening, then open), with each attribute it set kept; turning it elsewhere (the wall
    dimmer down to 30%, the blind sent back, the volume or the thermostat set back) is someone's correction (P9.1.7),
    though Assist also changed another entity of the same device just before. What the device reports on its own (a
    reading) and what Assist never set may move with a follow-on."""
    from custom_components.assist_improver.exploration import SessionView

    def after(entity, start, assist, later, sibling=None):
        record = variant(make_recording(), "carried01", "Toggle the fixture relay.", turns=1)
        first = record["turns"][0]["source"]
        context = record["state_changes"][0]["new"]["context"]
        for e in (entity, sibling) if sibling else (entity,):
            first["states"][e] = {"entity_id": e, **start}
            first["entities"][e] = {"name": e, "aliases": [], "area_id": "fixture_area", "device_id": "one-device"}
        first["devices"]["one-device"] = {"name": "One device"}
        if sibling:  # Assist changed the device's other entity too.
            record["state_changes"].append({"entity_id": sibling, "at": "2026-01-01T00:00:04.400000+00:00",
                                            "old": first["states"][sibling],
                                            "new": {"entity_id": sibling, **assist, "context": dict(context)}})
        record["state_changes"] += [
            {"entity_id": entity, "at": "2026-01-01T00:00:04.500000+00:00", "old": first["states"][entity],
             "new": {"entity_id": entity, **assist, "context": dict(context)}},
            {"entity_id": entity, "at": "2026-01-01T00:00:08+00:00", "old": {"entity_id": entity, **assist},
             "new": {"entity_id": entity, **later, "context": {"id": "not-assist", "parent_id": None, "user_id": None}}}]
        return SessionView(record).changes[entity][1]["by"]
    off = {"state": "off", "attributes": {"brightness": None}}
    full = {"state": "on", "attributes": {"brightness": 255}}
    assert after("light.desk", off, full, {"state": "on", "attributes": {"brightness": 77}}).startswith("not the")
    assert after("light.desk", off, full, {"state": "on", "attributes": {"brightness": 250}}).startswith("follow-on")
    closed, opening = {"state": "closed", "attributes": {}}, {"state": "opening", "attributes": {}}
    assert after("cover.blind", closed, opening, {"state": "open", "attributes": {}}).startswith("follow-on")
    assert after("cover.blind", closed, opening, {"state": "closing", "attributes": {}}).startswith("not the")
    assert after("switch.outlet_1", {"state": "on", "attributes": {}}, {"state": "off", "attributes": {}},
                 {"state": "on", "attributes": {}}, sibling="switch.outlet_2").startswith("not the")
    tv = lambda volume: {"state": "playing", "attributes": {"volume_level": volume}}  # noqa: E731
    assert after("media_player.tv", tv(0.3), tv(0.1), tv(0.3)).startswith("not the")  # Set back at the remote.
    heat = lambda temperature, now, preset="none": {"state": "heat", "attributes": {  # noqa: E731
        "temperature": temperature, "current_temperature": now, "preset_mode": preset}}
    assert after("climate.hall", heat(19, 19.5), heat(22, 19.6), heat(20, 19.6)).startswith("not the")
    assert after("climate.hall", heat(19, 19.5), heat(22, 19.6), heat(22, 19.7, "manual")).startswith("follow-on")
    # A device's own rounding is no one setting it back: nearer where Assist left it than where it was.
    assert after("climate.hall", heat(68, 70), heat(72, 70), heat(71.6, 70)).startswith("follow-on")
    lamp = lambda kelvin, hs: {"state": "on", "attributes": {  # noqa: E731
        "brightness": 128, "color_temp_kelvin": kelvin, "hs_color": hs}}  # HA derives the hue from the kelvin.
    assert after("light.desk", lamp(4000, [33.1, 53.5]), lamp(2700, [28.395, 65.723]),
                 lamp(2702, [28.391, 65.659])).startswith("follow-on")
    assert after("light.desk", lamp(4000, [33.1, 53.5]), lamp(2700, [28.395, 65.723]),
                 lamp(4000, [33.1, 53.5])).startswith("not the")
    blind = lambda state, position: {"state": state, "attributes": {"current_position": position}}  # noqa: E731
    assert after("cover.blind", blind("closed", 0), blind("opening", 10), blind("opening", 40)).startswith("follow-on")
    assert after("climate.hall", heat(19, 19.5), heat(22, 19.6, "eco"), heat(22, 19.6, "none")).startswith("not the")


def test_someone_setting_back_in_a_later_turn_what_assist_set_reaches_the_quick_check_and_the_analyzer():
    """P9.1.7: Assist turned the TV down in the first turn; in the third, someone set it back up at the remote, without
    signing in. The TV kept playing, so only its volume moved: read_turn and the quick check show who changed what and
    when, not one line among the readings elsewhere (once a bare entity ID under attribute_only_updates_elsewhere)."""
    from custom_components.assist_improver import triage
    from custom_components.assist_improver.exploration import SessionView
    record = variant(make_recording(), "volume01", "Toggle the fixture relay.")
    tv = "media_player.tv"

    def playing(volume):
        return {"entity_id": tv, "state": "playing", "attributes": {"friendly_name": "TV", "volume_level": volume}}
    first = record["turns"][0]["source"]
    first["states"][tv], first["exposure"][tv] = playing(0.3), True
    first["entities"][tv] = {"name": "TV", "aliases": [], "area_id": "fixture_area"}
    assist = record["state_changes"][0]["new"]["context"]
    record["state_changes"] += [
        {"entity_id": tv, "at": "2026-01-01T00:00:04.500000+00:00", "old": playing(0.3),
         "new": {**playing(0.1), "context": dict(assist)}},  # Assist, in turn 1.
        {"entity_id": tv, "at": "2026-01-01T00:00:15+00:00", "old": playing(0.1),
         "new": {**playing(0.3), "context": {"id": "remote", "parent_id": None, "user_id": None}}}]  # Turn 3.
    view = SessionView(record)
    read = view.read_turn(3)
    assert [c["by"][:17] for c in read["state_changes_during_turn"] if c["entity_id"] == tv] == ["not the assistant"]
    assert tv not in [r["entity_id"] for r in read.get("readings_elsewhere", [])]
    shown = [c for c in triage.payload(view)["turns"][2]["home_changes"] if c["device"] == "TV"]
    assert [c["attributes_changed"]["volume_level"] for c in shown] == [[0.1, 0.3]]


def test_a_quick_correction_of_a_device_assists_results_name_is_still_a_correction():
    """P9.1.7: Assist turned the TV down, its result naming the TV; the TV echoed it, then someone set it back at the
    remote. After Assist's own change came through under its request, a change only Assist's results name is held to
    carrying that change through like any other: the echo does, setting the volume back doesn't."""
    from custom_components.assist_improver.exploration import SessionView
    record = variant(make_recording(), "volume02", "Toggle the fixture relay.", turns=1)
    tv = "media_player.tv"

    def playing(volume, **more):
        return {"entity_id": tv, "state": "playing", "attributes": {"friendly_name": "TV", "volume_level": volume,
                                                                    **more}}
    first = record["turns"][0]["source"]
    first["states"][tv], first["exposure"][tv] = playing(0.3), True
    first["entities"][tv] = {"name": "TV", "aliases": [], "area_id": "fixture_area"}
    result = next(m for m in record["chat_log"]["content"] if m["role"] == "tool_result")
    result["result"]["data"]["data"]["success"].append({"name": "TV", "type": "entity", "id": tv})
    assist = record["state_changes"][0]["new"]["context"]
    record["state_changes"] += [
        {"entity_id": tv, "at": "2026-01-01T00:00:04.500000+00:00", "old": playing(0.3),
         "new": {**playing(0.1), "context": dict(assist)}},
        {"entity_id": tv, "at": "2026-01-01T00:00:05+00:00", "old": playing(0.1),
         "new": {**playing(0.1, source="TV"), "context": {"id": "echo", "parent_id": None, "user_id": None}}},
        {"entity_id": tv, "at": "2026-01-01T00:00:05.500000+00:00", "old": playing(0.1, source="TV"),
         "new": {**playing(0.3, source="TV"), "context": {"id": "remote", "parent_id": None, "user_id": None}}}]
    assert [c["by"][:17] for c in SessionView(record).changes[tv]] == ["assistant", "assistant", "not the assistant"]


def test_what_assist_did_in_a_later_turn_on_a_slow_device_stays_assists():
    """Assist turned the light on in the first turn and dimmed it to 30% in the third; the light reported that only
    after Home Assistant stopped tying it to the request, so only Assist's results name it. It's Assist's: only
    Assist's own change in the same turn is one a change only its results name must carry through."""
    from custom_components.assist_improver.exploration import SessionView
    record = variant(make_recording(), "slowdim1", "Toggle the fixture relay.")
    light = "light.desk"

    def on(brightness):
        return {"entity_id": light, "state": "on", "attributes": {"friendly_name": "Desk light",
                                                                   "brightness": brightness}}
    first = record["turns"][0]["source"]
    first["states"][light] = {"entity_id": light, "state": "off", "attributes": {"friendly_name": "Desk light"}}
    first["entities"][light] = {"name": "Desk light", "aliases": [], "area_id": "fixture_area"}
    third = next(m for m in record["chat_log"]["content"]
                 if m["role"] == "tool_result" and m["created"] > "2026-01-01T00:00:10")  # Turn 3's result.
    third["result"]["data"]["data"]["success"].append({"name": "Desk light", "type": "entity", "id": light})
    assist = record["state_changes"][0]["new"]["context"]
    record["state_changes"] += [
        {"entity_id": light, "at": "2026-01-01T00:00:04.500000+00:00", "old": first["states"][light],
         "new": {**on(255), "context": dict(assist)}},  # Turn 1, under its request.
        {"entity_id": light, "at": "2026-01-01T00:00:15+00:00", "old": on(255),
         "new": {**on(77), "context": {"id": "late-report", "parent_id": None, "user_id": None}}}]  # Turn 3.
    assert [c["by"] for c in SessionView(record).changes[light]] == ["assistant", "assistant"]
