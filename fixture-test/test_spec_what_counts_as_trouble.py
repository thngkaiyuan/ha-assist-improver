"""Testable specs: what counts as trouble.

Besides extra turns and settings left open, two more things are trouble, and both flow through every stage:
- a side effect left: a device Assist changed that no goal asked for and that Assist didn't leave as it started (code
  derives it; a device whose start was unknown, such as one asleep, never is one; anyone else putting it back
  doesn't clear it). It counts on the goal whose turn made the change, and a fix must reach the goal without changing
  that device, judged by code.
- what the household objected to in how Assist behaved, in a later turn or in the note they sent the conversation in
  with: a conduct sub-goal of the goal it counts on, about the Assist turn it objects to, in their own words (code
  refuses a paraphrase). A fix must still reach the goal, and Assist must no longer do what they objected to, judged
  by the small check model.
A conversation shown "No trouble found" can be sent in again, with or without a note, for one fresh full analysis that
reads the note as a turn of its own: the same mechanism as for one that looked smooth or had no fix found.

Each kind runs through a whole run of the pipeline (kit.py: the AI roles and the household's assistant
scripted, the sandbox real): analysis, Propose (the proposer's and the reviewer's inputs), Simulate (the replays without
and with the fix, the check), the write-up and the review page.
"""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from ha_task import use
from recordings import PROFILE, before
from test_conversations import ask
from kit import GOALS, improver  # noqa: F401 - improver is a fixture.
from custom_components.assist_improver import analyzer, measures, panel
from custom_components.assist_improver.hypotheses import friction_by_goal, scenario
from custom_components.assist_improver.improvements import row

RELAY, FRIDGE = "switch.fixture_relay", "switch.fixture_fridge"
NAMES = {RELAY: "Fixture relay", FRIDGE: "Fixture fridge", "script.fixture_press": "Fixture press"}
SCRIPT = "Switch the relay off"  # The fix the proposer tries: a script that switches off only the relay.
SCRIPT_TOOL = "script__switch_the_relay_off"


def home(rid, turns, *, start=None):
    """A finished conversation in the fixture home, which has a second switch (the fridge's plug, on): each turn is
    (what was said, Assist's calls as [(direction, name, devices it changed)], its reply). Recorded as the recorder
    keeps one, with the household's assistant (recordings.PROFILE)."""
    clock = datetime(2026, 1, 1, tzinfo=UTC)

    def tick():
        nonlocal clock
        clock += timedelta(seconds=1)
        return clock.isoformat()
    current = {RELAY: "on", FRIDGE: "on", **(start or {})}
    state = lambda e, s: {"entity_id": e, "state": s, "attributes": {"friendly_name": NAMES[e]}}  # noqa: E731
    schema = {"type": "object", "properties": {"name": {"type": "string"},
              "domain": {"type": "array", "items": {"type": "string"}}}, "additionalProperties": False}
    tools = [{"name": f"intent__HassTurn{d}", "description": f"Turn {d}", "integration": "intent", "parameters": schema}
             for d in ("On", "Off")] + [{"name": f"FixtureExtra{i}", "description": "Unused fixture tool",
                                         "integration": "fixture", "parameters": {"type": "object", "properties": {}}}
                                        for i in range(8)]
    messages = [{"role": "system", "content": "Frozen instructions and fixture inventory."}]
    record = {"format": 1, "id": rid, "core_version": "2026.10.2", "status": "session_expired",
              "limitations": ["provider_native_history_omitted; replay adapter must check compatibility"],
              "turns": [], "state_changes": [], "chat_log": {"content": messages}}
    for number, (text, calls, reply) in enumerate(turns, 1):
        user = {"role": "user", "content": text, "created": tick()}
        messages.append(user)
        context = {"id": f"{rid}-context-{number}", "parent_id": None, "user_id": "fixture-user"}
        source = {"at": tick(), "states": {e: state(e, current.get(e, "off")) for e in NAMES},
                  "entities": {e: {"name": n, "aliases": [None], "area_id": "fixture_area"}
                               for e, n in NAMES.items()},
                  "exposure": {RELAY: True, FRIDGE: True, "script.fixture_press": False}, "devices": {},
                  "areas": {"fixture_area": {"name": "Fixture room"}}}
        prepared = {"context": {"platform": "anthropic", "context": context, "language": "en",
                                "assistant": "conversation", "device_id": "fixture-phone"},
                    "tools": deepcopy(tools), "provider_model_capabilities": {},
                    "provider_config": [{"config_entry_id": "fixture-entry", "subentry_id": "fixture-anthropic",
                                         "options": deepcopy(PROFILE)}]}
        record["turns"].append({"user": deepcopy(user), "source": source, "prepared": prepared,
                                "preparations": [{"content": deepcopy(messages)}]})
        for n, (direction, name, changed) in enumerate(calls):
            call = {"tool_name": f"intent__HassTurn{direction}", "tool_args": {"name": name, "domain": ["switch"]},
                    "id": f"call-{number}-{n}", "external": False}
            messages.append({"role": "assistant", "agent_id": "conversation.fixture", "tool_calls": [call],
                             "created": tick()})
            for entity in changed:
                after = direction.lower()
                record["state_changes"].append({"entity_id": entity, "at": tick(), "old": state(entity, current[entity]),
                                                "new": {**state(entity, after), "context": deepcopy(context)}})
                current[entity] = after
            result = {"data": {"speech": {}, "response_type": "action_done", "data": {
                "success": [{"name": NAMES[e], "type": "entity", "id": e} for e in changed], "failed": []}},
                "error": False}
            messages.append({"role": "tool_result", "agent_id": "conversation.fixture", "tool_call_id": call["id"],
                             "tool_name": call["tool_name"], "result": result, "created": tick()})
        messages.append({"role": "assistant", "agent_id": "conversation.fixture", "content": reply, "created": tick()})
    return record


RELAY_OFF = {"turn": 1, "summary": "Relay off", "kind": "state", "entity_id": RELAY, "state": "off"}
# (a) Assist switched off the fridge's plug too: a side effect left, and nothing else.
CARELESS = "Switch off the fixture relay."
CARELESS_TURNS = [(CARELESS, [("Off", "Fixture relay", [RELAY]), ("Off", "Fixture fridge", [FRIDGE])],
                   "Done: the relay and the fridge are off.")]
# (b) Done as asked, then an objection to how Assist said it.
WORDY = "Turn the fixture relay off now."
WORDY_TURNS = [(WORDY, [("Off", "Fixture relay", [RELAY])],
                "I've turned off the fixture relay. Please note I can't confirm its physical position."),
               ("It worked, but please be less wordy and skip the disclaimers next time.", [], "Noted.")]
LESS_WORDY = "please be less wordy and skip the disclaimers next time"  # Their words, as the analyzer quotes them.
LESS_WORDY_GOAL = {"turn": 2, "summary": "Less wordy next time", "kind": "conduct", "about": 1, "quote": LESS_WORDY,
                   "serves": 1}
# (c) Done as asked in one turn; sent in with a note.
NOTED = "Relay off, thanks."
NOTE = "Keep it short, without the disclaimer."
NOTE_GOAL = {"turn": 3, "summary": "Without the disclaimer", "kind": "conduct", "about": "all", "quote": NOTE,
             "serves": 1}  # WORDY's.
QUIET_TURNS = [(NOTED, [("Off", "Fixture relay", [RELAY])],
                "The relay is off, though I can't confirm its physical position.")]
# (d) Two requests, each done as asked: no trouble found, until it's sent in again with a note.
FINE = "Switch the fixture relay off."
FINE_TURNS = [(FINE, [("Off", "Fixture relay", [RELAY])], "Done."),
              ("Is the relay off now?", [], "Yes, it's off, though I can't confirm its physical position.")]

# (a') Done as asked; then, in "Thanks.", Assist switched off the fridge's plug: a side effect after the goal.
THANKS = "Switch off the relay, please."
THANKS_TURNS = [(THANKS, [("Off", "Fixture relay", [RELAY])], "Done."),
                ("Thanks.", [("Off", "Fixture fridge", [FRIDGE])], "You're welcome.")]

GOALS.update({
    CARELESS: {"goals": [RELAY_OFF], "not_requests": [], "question_for_user": ""},
    THANKS: {"goals": [RELAY_OFF], "not_requests": [{"turn": 2, "what": "Thanks"}], "question_for_user": ""},
    WORDY: {"goals": [RELAY_OFF, LESS_WORDY_GOAL], "not_requests": [], "question_for_user": ""},
    NOTED: {"goals": [RELAY_OFF], "not_requests": [], "question_for_user": ""},
})


def fixing(model):
    """The proposer's part: one script that switches off only the relay, ending with a short reply, for every goal
    with friction it's given (after the reviewer's one round), sent on when its test validates it (P0.4), and what it
    was given kept."""
    async def propose(hass, payload, tools):
        model.calls.append(("proposer", None))
        model.proposer_input = payload
        goals = [{"session": s["session"], "goal_turn": t} for s in payload.get("friction_scenarios", [])
                 for t in s["goals_with_friction"]]
        answer = {"hypotheses": []}
        if goals:
            hypothesis = {"title": "Switch off only the relay, briefly", "goals": goals, "expected": "One turn",
                          "cause": "Assist switched off what it wasn't asked to, and said too much",
                          "support": [{"session": goals[0]["session"], "turn": 1, "note": "Only the relay was meant"}],
                          "conflicts": [], "changes": [{"kind": "script", "id": "switch_the_relay_off", "config": {
                              "alias": SCRIPT, "description": "Switches off the fixture relay, and only it.",
                              "sequence": [{"action": "switch.turn_off", "target": {"entity_id": RELAY}},
                                           {"stop": "Done."}]}}]}
            review = await model.use(hass, tools["ask_reviewer"], hypothesis)
            model.reviewer_saw.append(review)
            if review["outcome"] == "request_changes":
                responses = [{"comment": c["comment"], "resolution": "resolved", "note": "Only the relay"}
                             for c in review["comments"] if "blocking" in c["decorations"]]
                await model.use(hass, tools["ask_reviewer"], {**hypothesis, "responses": responses})
            model.tests.append(await model.use(hass, tools["test_idea"], hypothesis))
            if model.tests[-1].get("validated"):
                answer["hypotheses"] = [hypothesis]
            else:
                answer["not_fixed"] = [{**goal, "why": "No idea for it was validated."} for goal in goals]
        accepted = await model.use(hass, tools["submit_hypotheses"], answer)
        assert accepted["accepted"], accepted
        return "Submitted."
    return propose


def keeping_drafts(model):
    """Kit's tool calls, keeping each draft the reviewer was sent (what it saw of the friction)."""
    async def keeping(hass, tool, args=None):
        got = await use(hass, tool, args)
        if tool.name == "wait_for_draft" and "draft" in got:
            model.drafts_seen.append(got["draft"])
        return got
    return keeping


def the_fix():
    """The household's assistant with the fix: it runs the script, and says only that it's done."""
    return [[{"type": "tool_use", "id": "fix", "name": SCRIPT_TOOL, "input": {}}], [{"type": "text", "text": "Done."}]]


def scripted(model):
    model.hypothesis_generator = fixing(model)
    model.use = keeping_drafts(model)
    model.reviewer_saw, model.drafts_seen = [], []


def fridge_at_home(hass):
    """The fridge's plug in the household's home today, where replays run, seen by Assist."""
    from homeassistant.components.homeassistant.exposed_entities import async_expose_entity
    hass.states.async_set(FRIDGE, "on", {"friendly_name": NAMES[FRIDGE]})
    async_expose_entity(hass, "conversation", FRIDGE, True)


async def arrive(improver, rid, turns, **kwargs):
    record = home(rid, turns, **kwargs)
    await improver.arrives(record)
    return record


def analysis_of(runtime, rid):
    return runtime.flow.analysis(rid)


async def fast_lane(improver):
    """The run a conversation sent in gets at once (the fast lane), once it's done."""
    pipeline = improver.runtime.pipeline
    await pipeline._task
    return pipeline.current


async def chip(improver, rid):
    # Wherever it's listed: settled ones are archived on their own, listed below Show archived.
    rows = [c for archived in (False, True)
            for c in (await ask(improver.hass, panel.ws_conversations, archived=archived))[1]["conversations"]]
    return {c["id"]: c["status"] for c in rows}[rid]


async def the_review(improver):
    [item] = improver.runtime.improvements.pending()
    _, shown = await ask(improver.hass, panel.ws_get, improvement_id=item["id"])
    return item, shown


@pytest.mark.asyncio
async def test_a_side_effect_left_is_trouble_from_the_analysis_to_the_review(improver):
    runtime, model, client = improver.runtime, improver.model, improver.client
    scripted(model)
    fridge_at_home(improver.hass)
    record = await arrive(improver, "careless", CARELESS_TURNS)
    client.blocks = before(record) + the_fix()
    run = await improver.run()
    assert run["status"] == "done", run["error"]
    assert model.slips == [] and not client.blocks
    # Analysis: code derives the side effect, with the turn that made it; it's friction on the goal of that turn.
    analysis = analysis_of(runtime, "careless")
    assert analysis["side_effects_left"] == [{"entity_id": FRIDGE, "start": "on", "end": "off", "turn": 1}]
    assert analysis["extra_turns"] == []
    assert [f["kind"] for f in friction_by_goal(analysis)[1]] == ["side effect"]
    # Propose: the proposer gets the side effect with the goal it counts on; the reviewer, the friction it targets.
    [shown] = model.proposer_input["friction_scenarios"]
    assert shown["goals_with_friction"] == [1]
    assert shown["side_effects_left"] == [{"entity_id": FRIDGE, "start": "on", "end": "off", "changed_in_turn": 1,
                                           "for_goal_at_turn": 1}]
    targeted = model.drafts_seen[0]["friction_it_targets"]
    assert [f["kind"] for f in targeted["goals"][0]["friction"]] == ["side effect"]
    assert "side effect" in targeted["kinds"]
    # Write-up: what you get names the side effect removed.
    item, review = await the_review(improver)
    test = row(item["test"])
    assert test["side_effects"] == [{"entity_id": FRIDGE, "name": "Fixture fridge", "start": "on"}]
    assert test["side_effects_gone"] == [FRIDGE]
    [listed_for_roles] = runtime.improvements.catalog(detailed=True)  # read_improvement, for later roles.
    assert listed_for_roles["details"]["tested_on"][0]["side_effects_gone"] == [FRIDGE]
    from custom_components.assist_improver.editor import payload as editor_payload
    assert "no longer changes Fixture fridge (it stays on)" in editor_payload(item)["tested"][0]["result"]
    # The review page: the Tested on row's chip, and what it gets you.
    assert review["tests"][0]["result"] == "side effect gone"
    assert (review["spared"], review["followed"]) == (1, 0)
    _, listed = await ask(improver.hass, panel.ws_list)
    assert (listed["improvements"][0]["spared"], listed["improvements"][0]["followed"]) == (1, 0)
    assert await chip(improver, "careless") == "review"


@pytest.mark.asyncio
@pytest.mark.parametrize("fixed", [True, False], ids=["fix", "no-op"])
async def test_a_side_effect_made_after_the_goal_is_replayed_so_only_a_real_fix_removes_it(improver, fixed):
    """Assist switched off the fridge's plug in "Thanks.", a turn after the goal was met: a fix's replay goes on to
    that turn (the household says it either way), so a change that does nothing leaves the side effect, and fails."""
    runtime, model, client = improver.runtime, improver.model, improver.client
    scripted(model)
    fridge_at_home(improver.hass)
    record = await arrive(improver, "thanks01", THANKS_TURNS)
    fix = the_fix() + [[{"type": "text", "text": "You're welcome."}]]
    client.blocks = before(record) + (fix if fixed else before(record))  # Without the change, then with it.
    run = await improver.run()
    assert run["status"] == "done", run["error"]
    assert model.slips == [] and not client.blocks
    # Analysis: the side effect came in turn 2, a non-request; it counts on the goal before it.
    analysis = analysis_of(runtime, "thanks01")
    assert analysis["side_effects_left"] == [{"entity_id": FRIDGE, "start": "on", "end": "off", "turn": 2}]
    assert [f["kind"] for f in friction_by_goal(analysis)[1]] == ["side effect"]
    if fixed:
        item, review = await the_review(improver)
        assert review["tests"][0]["result"] == "side effect gone" and review["removed"] == 0
    else:  # As the conversation went: the fridge is switched off again, so nothing is better.
        assert runtime.improvements.pending() == [] and runtime.improvements.unwritten() == []


@pytest.mark.asyncio
async def test_a_note_sent_in_with_a_conversation_is_an_objection_carried_to_the_review(improver):
    runtime, model, client = improver.runtime, improver.model, improver.client
    scripted(model)
    record = await arrive(improver, "noted001", QUIET_TURNS)
    assert await ask(improver.hass, panel.ws_conversation_send, recording_id="noted001", note=NOTE) == (
        "ok", {"status": "sent", "hidden": False})
    client.blocks = before(record) + the_fix()
    run = await fast_lane(improver)
    assert run["status"] == "done", run["error"]
    assert model.slips == [] and ("triage", NOTED) not in model.calls  # Sent in: past the quick check.
    # Analysis: the note is its last turn, a conduct goal under the goal it's about (code rejects an answer that gives
    # it no goal), in the note's own words.
    analysis = analysis_of(runtime, "noted001")
    assert analysis["goals"][1] == {"turn": 2, "summary": f"The note: {NOTE}", "kind": "conduct", "about": "all",
                                    "quote": NOTE, "serves": 1, "reached": False}
    assert [f["kind"] for f in friction_by_goal(analysis)[1]] == ["conduct"]
    [shown] = model.proposer_input["friction_scenarios"]
    assert shown["note"] == {"conduct": [{"quote": NOTE, "about_assist_turn": "all", "goal_turn": 1}]}
    # Simulate, write-up, review: judged by the small model, it follows the note (the baseline's replies didn't).
    assert [feedback for feedback, _ in model.feedback_checks] == [[NOTE], [NOTE]]
    item, review = await the_review(improver)
    assert row(item["test"])["feedback"] == [NOTE] and review["tests"][0]["result"] == "follows your feedback"
    assert review["followed"] == 1 and await chip(improver, "noted001") == "review"


@pytest.mark.asyncio
async def test_an_analysis_that_leaves_out_the_note_is_rejected_and_the_conversation_held_at_analyze(improver):
    """The note a conversation was sent in with is its last turn, with a goal (SPEC.md P0.3): every answer of the
    analyzer's that drops it is rejected, so Analyze holds the conversation after its tries (P0.8), and nothing is
    proposed from what an answer kept."""
    from custom_components.assist_improver.flow import RETRIES
    runtime, model = improver.runtime, improver.model
    scripted(model)
    model.notes_dropped.add(NOTED)
    await arrive(improver, "noted001", QUIET_TURNS)
    await ask(improver.hass, panel.ws_conversation_send, recording_id="noted001", note=NOTE)
    run = await fast_lane(improver)
    assert run["status"] == "done", run["error"]
    kept = [r for r in runtime.role_runs.runs if r.get("recording_id") == "noted001" and r.get("analysis")]
    assert len(kept) == RETRIES and all(r["answer_status"] == "best_rejected" for r in kept)
    assert all(all(g["turn"] == 1 for g in r["output"]["goals"]) for r in kept)  # Each dropped the note.
    place = runtime.flow.place("noted001")
    assert (place["stage"], place["held"]["cause"]) == ("Analyze", "failures")
    assert analysis_of(runtime, "noted001") is None and ("proposer", None) not in model.calls


@pytest.mark.asyncio
async def test_no_trouble_found_sent_in_again_without_a_note_is_looked_at_afresh_too(improver):
    runtime, model = improver.runtime, improver.model
    scripted(model)
    await arrive(improver, "fine0001", FINE_TURNS)
    await improver.run()
    model.calls.clear()
    assert await ask(improver.hass, panel.ws_conversation_send, recording_id="fine0001") == (
        "ok", {"status": "sent", "hidden": False})
    assert (await fast_lane(improver))["status"] == "done"
    assert model.calls.count(("analyzer", FINE)) == 1 and await chip(improver, "fine0001") == "fine"  # Still fine.


@pytest.mark.asyncio
async def test_a_smooth_conversation_with_nothing_left_and_no_feedback_still_ends_no_trouble_found(improver):
    runtime, model = improver.runtime, improver.model
    scripted(model)
    await arrive(improver, "fine0001", FINE_TURNS)
    assert (await improver.run())["status"] == "done"
    analysis = analysis_of(runtime, "fine0001")
    assert analysis["side_effects_left"] == [] and friction_by_goal(analysis) == {}
    assert await chip(improver, "fine0001") == "fine" and runtime.pipeline.preview()["stages"]["Propose"] == 0


def view_of(record):
    from custom_components.assist_improver.exploration import SessionView
    return SessionView(record)


def test_a_device_whose_start_was_unknown_is_never_a_side_effect():
    # The fridge's plug was unavailable before Assist's call (asleep, say): its change can't be told from its start.
    record = home("asleep01", CARELESS_TURNS, start={FRIDGE: "unavailable"})
    view = view_of(record)
    assert analyzer.side_effects_left(view, [RELAY_OFF]) == []
    unknown = home("unknown1", CARELESS_TURNS, start={FRIDGE: "unknown"})
    assert analyzer.side_effects_left(view_of(unknown), [RELAY_OFF]) == []
    put_back(unknown, FRIDGE, "on", NO_ONE)  # Switched on later at the plug: still nothing to compare with.
    assert analyzer.side_effects_left(view_of(unknown), [RELAY_OFF]) == []


def test_a_setting_assist_changed_and_didnt_put_back_is_a_side_effect_and_a_buttons_last_press_never_is():
    """DETAILS P9.12: what Assist left is a device's settable state, its state and each setting an action of its sets
    (a light's brightness or colour), compared in any form and a number within tolerance; a reading isn't one. A
    button's last press, or a script running, is never a side effect left: it keeps no state to reach. A setting, as
    its state, is compared with how it started; one unknown at the start can't be told to have moved."""
    lamp, gate, fan = "light.fixture_lamp", "button.fixture_gate", "fan.fixture_fan"
    asked = {"turn": 1, "summary": "Fan on", "kind": "state", "entity_id": fan, "state": "on"}

    def left(start, *moves):
        home = SimpleNamespace(source={"states": {e: {"state": s, "attributes": a} for e, (s, a) in start.items()}},
                               changes={})
        for entity, old, new, attributes in moves:
            home.changes.setdefault(entity, []).append({"during_turn": 1, "by": "assistant", "old": old, "new": new,
                                                        "attributes_changed": attributes})
        return analyzer.side_effects_left(home, [asked])
    lit = {"color_mode": "hs", "brightness": 255, "hs_color": [30.0, 50.0], "supported_color_modes": ["hs"]}
    assert left({lamp: ("on", lit)}, (lamp, "on", "on", {"brightness": [255, 100]})) == [
        {"entity_id": lamp, "start": "on", "end": "on", "turn": 1, "settings": {"brightness": [255, 100]}}]
    assert left({lamp: ("on", lit)}, (lamp, "on", "on", {"brightness": [255, 250]})) == []  # Within tolerance.
    as_rgb = {"color_mode": ["hs", "rgb"], "hs_color": [[30.0, 50.0], [30.1, 49.8]],
              "rgb_color": [None, [255, 191, 128]]}
    assert left({lamp: ("on", lit)}, (lamp, "on", "on", as_rgb)) == []  # The same colour, now shown as rgb.
    assert left({lamp: ("on", lit)}, (lamp, "on", "on", {"hs_color": [[30.0, 50.0], [240.0, 100.0]]}))[0][
        "settings"] == {"hs_color": [[30.0, 50.0], [240.0, 100.0]]}
    unknown = {**lit, "brightness": None}
    assert left({lamp: ("on", unknown)}, (lamp, "on", "on", {"brightness": [None, 100]})) == []
    assert left({lamp: ("on", lit)}, (lamp, "on", "on", {"friendly_name": ["Lamp", "Lamp 2"]})) == []  # No setting.
    dimmed = {"during_turn": 1, "by": "a person", "old": "on", "new": "on",
              "attributes_changed": {"brightness": [255, 51]}}
    for back, found in ((255, []), (128, [{"brightness": [255, 128]}])):  # Assist sets it after a person dimmed it.
        put = {**dimmed, "by": "assistant", "during_turn": 2, "attributes_changed": {"brightness": [51, back]}}
        home = SimpleNamespace(source={"states": {lamp: {"state": "on", "attributes": lit}}},
                               changes={lamp: [dimmed, put]})
        assert [e["settings"] for e in analyzer.side_effects_left(home, [asked])] == found
    assert left({gate: ("2026-01-01T08:00:00+00:00", {})},
                (gate, "2026-01-01T08:00:00+00:00", "2026-01-01T09:00:00+00:00", {})) == []
    assert left({"script.fixture_press": ("off", {})}, ("script.fixture_press", "off", "on", {})) == []


def test_an_unmet_goal_whose_device_was_unavailable_as_asked_carries_that_the_home_couldnt_do_it():
    """The relay was unavailable when the user asked for it, so its goal is unmet whatever Assist did: the proposer
    is told so, with the friction and in the turn the goal started, and a test keeps it with the goal it judged. The
    controls: the same goal missed on a relay that was there, and on one that was unknown (asleep: a command wakes
    it)."""
    from custom_components.assist_improver import measures
    from custom_components.assist_improver.hypotheses.goals import HOME_COULDNT
    turns = [(CARELESS, [("Off", "Fixture relay", [])], "Done.")]
    answer = {"goals": [RELAY_OFF], "not_requests": [], "question_for_user": ""}
    record = home("down0001", turns, start={RELAY: "unavailable"})
    analysis = analyzer.to_analysis("down0001", answer, view_of(record))
    [goal] = analysis["goals"]
    assert goal["reached"] is False and goal["home_couldnt"] == "unavailable"
    assert friction_by_goal(analysis)[1] == [{"turn": 1, "kind": "unmet", "summary": "Relay off",
                                              "home_couldnt": HOME_COULDNT["unavailable"]}]
    assert scenario(analysis, view_of(record))["turns"][0]["goals_started"][0]["home_couldnt"] == HOME_COULDNT[
        "unavailable"]
    judged = measures.judged_of(measures.goals_of(analysis), lambda g: {g["entity_id"]})
    assert judged["1:0"]["home_couldnt"] == "unavailable"
    kept = {**analysis, "goals": [{**goal, "home_couldnt": True}]}  # An analysis from before says True: unavailable.
    assert friction_by_goal(kept)[1][0]["home_couldnt"] == HOME_COULDNT["unavailable"]
    for start in ({}, {RELAY: "unknown"}):
        missed = analyzer.to_analysis("missed01", answer, view_of(home("missed01", turns, start=start)))
        assert missed["goals"][0]["reached"] is False and "home_couldnt" not in missed["goals"][0]


def test_the_home_couldnt_do_it_only_when_the_device_was_unavailable_in_every_turn_its_goal_took():
    """The snapshot of each turn counts, as Assist saw the home then: a relay unavailable in the turn it was asked
    for but back by the retry Assist also missed was Assist's to reach; one unavailable only in a later turn was there
    when asked. A sub-goal on an unavailable device is never marked: the context is its root goal's."""
    retried = [(CARELESS, [("Off", "Fixture relay", [])], "Done."), ("The relay, off.", [], "Done.")]
    again = {"turn": 2, "summary": "Relay off, again", "kind": "step", "serves": 1}
    answer = {"goals": [RELAY_OFF, again], "not_requests": [], "question_for_user": ""}
    for down in ([1, 2], [1], [2]):
        record = home("retry001", retried)
        for n in down:
            record["turns"][n - 1]["source"]["states"][RELAY]["state"] = "unavailable"
        [root, _] = analyzer.to_analysis("retry001", answer, view_of(record))["goals"]
        assert root["reached"] is False and root.get("home_couldnt") == ("unavailable" if down == [1, 2] else None)
    fridge = {"turn": 1, "summary": "Fridge off too", "kind": "state", "entity_id": FRIDGE, "state": "off",
              "serves": 1}
    record = home("parts001", [(CARELESS, [("Off", "Fixture relay", [RELAY])], "Done.")], start={FRIDGE: "unavailable"})
    goals = analyzer.to_analysis("parts001", {"goals": [RELAY_OFF, fridge], "not_requests": [],
                                              "question_for_user": ""}, view_of(record))["goals"]
    assert goals[1]["reached"] is False and "home_couldnt" not in goals[1]


def test_a_command_that_failed_at_home_in_every_turn_its_goal_took_is_one_the_home_couldnt_do():
    """DETAILS P9.10: the home couldn't do a goal when its command failed at home (a call of the turn's call log on
    its device ended in an error, as a replay refuses it again), or its device was unavailable, in every turn its tree
    took; the analysis, the proposer and a test's record carry which. A retry that wasn't refused was Assist's to
    reach; a failure on another device says nothing of this one."""
    from custom_components.assist_improver import measures
    from custom_components.assist_improver.hypotheses.goals import HOME_COULDNT
    retried = [(CARELESS, [("Off", "Fixture relay", [])], "Done."), ("The relay, off.", [], "Done.")]
    again = {"turn": 2, "summary": "Relay off, again", "kind": "step", "serves": 1}
    answer = {"goals": [RELAY_OFF, again], "not_requests": [], "question_for_user": ""}
    refused = lambda entity: [{"action": "switch.turn_off", "data": {"entity_id": [entity]},  # noqa: E731
                               "error": "The device didn't answer"}]
    for failed, down, why in (([1, 2], [], "failed"), ([1], [2], "failed"), ([1], [], None), ([], [], None)):
        record = home("refused1", retried)
        for n, turn in enumerate(record["turns"], 1):
            turn["actions"] = refused(RELAY) if n in failed else refused(FRIDGE)
            if n in down:
                turn["source"]["states"][RELAY]["state"] = "unavailable"
        analysis = analyzer.to_analysis("refused1", answer, view_of(record))
        [root, _] = analysis["goals"]
        assert root["reached"] is False and root.get("home_couldnt") == why, (failed, down)
    record = home("refused1", retried)
    for turn in record["turns"]:
        turn["actions"] = refused(RELAY)
    analysis = analyzer.to_analysis("refused1", answer, view_of(record))
    assert friction_by_goal(analysis)[1][0]["home_couldnt"] == HOME_COULDNT["failed"]
    judged = measures.judged_of(measures.goals_of(analysis), lambda g: {g.get("entity_id")})
    assert judged["1:0"]["home_couldnt"] == "failed"
    went = measures.judge(measures.goals_of(analysis), view_of(record), [1, 2], measures.CONVERSATION)
    assert went["home_couldnt"] == {"1:0": "failed"}


SPEAKER, VOICE = "media_player.fixture_speaker", "assist_satellite.fixture_voice"  # One device: what reaches one
# reaches the other.
SPOKEN = {"turn": 1, "summary": "Hello on the speaker", "kind": "action", "outcome": "announce",
          "entity_ids": [SPEAKER]}


def broadcast(rid, reached=(), tool="HassBroadcast", more=()):
    """The household's broadcast, as Home Assistant handled it itself: "hello everyone" on their speaker, Assist's
    one call a broadcast (or another tool) whose result names the devices it reached, and its reply "Done"; then the
    turns in more, with no calls."""
    record = home(rid, [("Broadcast hello everyone on my speaker", [("Off", "Fixture relay", [])], "Done"),
                        *((said, [], "Done") for said in more)])
    for message in record["chat_log"]["content"]:
        for call in message.get("tool_calls") or []:
            call.update(tool_name=tool, tool_args={"message": "hello everyone"})
        if message.get("role") == "tool_result":
            message["tool_name"] = tool
            message["result"]["data"]["data"]["success"] = [{"name": "Speaker", "type": "entity", "id": e}
                                                            for e in reached]
    for turn in record["turns"]:
        for entity, name in ((SPEAKER, "Fixture speaker"), (VOICE, "Fixture voice")):
            turn["source"]["states"][entity] = {"entity_id": entity, "state": "idle",
                                                "attributes": {"friendly_name": name}}
            turn["source"]["entities"][entity] = {"name": name, "aliases": [], "area_id": "fixture_area",
                                                  "device_id": "fixture-speaker-device"}
    return record


def test_a_broadcast_that_reached_no_speaker_is_an_unmet_action_though_assist_said_done():
    """DETAILS P9.7.6: the household's broadcast (Home Assistant handled it itself) came back "Done", but its result
    reached no device, so the message wasn't spoken on the speaker it was for: code reads the action unmet from the
    call, and it's trouble. Reaching the speaker's device (a broadcast names its voice satellite) meets it; a script,
    whose own calls a recording doesn't keep, can't be told."""
    for reached, tool, met in (((), "HassBroadcast", False), ((VOICE,), "HassBroadcast", True),
                               ((), "script__say_it", None)):
        record = broadcast(f"bcast-{tool[:6]}-{len(reached)}", reached, tool)
        analysis = analyzer.to_analysis(record["id"], {"goals": [SPOKEN], "not_requests": [],
                                                       "question_for_user": ""}, view_of(record))
        assert analysis["goals"][0]["reached"] is met, (tool, reached)
    assert friction_by_goal(analyzer.to_analysis("bcast-none", {"goals": [SPOKEN], "not_requests": [],
                                                                "question_for_user": ""},
                                                 view_of(broadcast("bcast-none"))))[1] == [
        {"turn": 1, "kind": "unmet", "summary": "Hello on the speaker"}]


def test_a_revision_asking_again_for_a_broadcast_is_a_step_that_a_fix_speaking_it_meets_with_the_goal():
    """A household's broadcast said "Done" and reached no device; they said it didn't work and filed it, objecting to
    the misreport. A wording fix closed only that, so they asked for a revision: "make it really speak on the speaker".
    The round's analysis gives that note a step under the broadcast (DETAILS P12.8.1), which code meets once the message
    is spoken: a fix that speaks it on the speaker meets the root, the step with it, never a question for a model about
    what replies can't show."""
    from custom_components.assist_improver import measures
    record = broadcast("bcast-revised", (), more=["That didn't work"])
    view = view_of(record)
    note, asked = "Assist said it broadcast when it didn't", "make it really speak on the speaker"
    goals = [SPOKEN, {"turn": 2, "summary": "Said it didn't work", "kind": "step", "serves": 1},
             {"turn": 3, "summary": "The misreport", "kind": "conduct", "about": 1, "quote": "said it broadcast when "
              "it didn't", "serves": 1},
             {"turn": 4, "summary": "Make it really speak", "kind": "step", "serves": 1}]
    answer = {"goals": goals, "not_requests": [], "question_for_user": ""}
    assert analyzer.problems(view, answer, note, revisions=[{"note": asked, "roots": [1]}]) == []
    analysis = {**analyzer.to_analysis(record["id"], answer, view), "notes": [{"sent": True}, {"revision": 2}]}
    tree = measures.goals_of(analysis)
    assert [g["label"] for g in tree] == [1, 2, "note", "revision:2"]
    went = measures.judge(tree, view, [1, 2], measures.CONVERSATION)
    assert went["goals"]["1:0"] == "unmet" and went["goals"]["4:3"] == "unmet"  # Never spoken: the step stands open.
    fixed = SimpleNamespace(source=view.source, changes={}, calls={1: [  # The script spoke it: its call log says.
        {"tool": "script__speak_it", "args": {}, "result": {"data": {}}}]}, actions={1: [
            {"action": "script.speak_it", "data": {}}, {"action": "tts.speak", "under": 0, "data": {
                "entity_id": ["tts.speech"], "media_player_entity_id": [SPEAKER], "message": "Hello everyone"}}]})
    after = measures.judge(tree, fixed, [1], {"3:2": "met"})
    assert after["goals"]["1:0"] == after["goals"]["4:3"] == "met"  # Spoken: the root, and the step towards it.


def test_a_broadcast_whose_satellite_was_unavailable_reached_nothing_though_its_result_names_it():
    """Home Assistant's broadcast names every satellite that can announce, an unavailable one too, though nothing is
    spoken there (as its entity service skips it): the action is met only on a device available as its turn began, by
    that turn's own snapshot of the home, in the conversation as in a replay; and one whose device was unavailable
    throughout is one the home couldn't do."""
    from custom_components.assist_improver import measures
    from custom_components.assist_improver.cases import recording_turns
    for state, met in (("idle", True), ("unavailable", False)):
        record = broadcast(f"bcast-voice-{state}", (VOICE,))
        record["turns"][0]["source"]["states"][VOICE]["state"] = state
        [goal] = analyzer.to_analysis(record["id"], {"goals": [SPOKEN], "not_requests": [], "question_for_user": ""},
                                      view_of(record))["goals"]
        assert goal["reached"] is met, state
    assert goal.get("home_couldnt") is None  # The speaker beside the satellite was there: a script could speak.
    record["turns"][0]["source"]["states"][SPEAKER]["state"] = "unavailable"
    for turn in record["turns"]:  # A disabled diagnostic entity on the same device: in the registry, with no state.
        turn["source"]["entities"]["sensor.fixture_voice_wifi"] = {"name": "Wi-Fi", "aliases": [],
                                                                   "device_id": "fixture-speaker-device"}
    [goal] = analyzer.to_analysis(record["id"], {"goals": [SPOKEN], "not_requests": [], "question_for_user": ""},
                                  view_of(record))["goals"]
    assert goal["reached"] is False and goal["home_couldnt"]  # Nothing of the device was there: the home couldn't.
    both = {**SPOKEN, "entity_ids": [SPEAKER, "media_player.fixture_relay_speaker"]}  # Another speaker, there.
    record["turns"][0]["source"]["states"]["media_player.fixture_relay_speaker"] = {
        "entity_id": "media_player.fixture_relay_speaker", "state": "idle", "attributes": {}}
    [goal] = analyzer.to_analysis(record["id"], {"goals": [both], "not_requests": [], "question_for_user": ""},
                                  view_of(record))["goals"]
    assert goal["reached"] is False and "home_couldnt" not in goal  # Only when every device it's for was down.

    # Replayed, the broadcast comes a turn later, when the satellite had gone: that turn's own home says it reached
    # nothing, as the conversation would; the first turn's home would say it did.
    record = broadcast("bcast-later", more=["Say it again"])
    record["turns"][1]["source"]["states"][VOICE]["state"] = "unavailable"
    view = view_of(record)
    sent = [{"role": "assistant", "tool_calls": [{"id": "b", "tool_name": "HassBroadcast",
                                                   "tool_args": {"message": "hello everyone"}}]},
            {"role": "tool_result", "tool_call_id": "b", "tool_name": "HassBroadcast", "tool_result": {
                "data": {"success": [{"id": VOICE}]}}}]
    replayed = measures.replay_view(recording_turns(record), view, [
        {"turn": 1, "transitions": [], "output": []}, {"turn": 2, "transitions": [], "output": sent}])
    goals = measures.goals_of({"turns": 2, "goals": [SPOKEN, {"turn": 2, "summary": "Again", "kind": "step",
                                                              "serves": 1}]})
    assert measures.judge(goals, replayed, [1, 2], {})["goals"]["1:0"] == "unmet"
    record["turns"][1]["source"]["states"][VOICE]["state"] = "idle"  # Control: there as the turn began, it's met.
    replayed = measures.replay_view(recording_turns(record), view_of(record), [
        {"turn": 1, "transitions": [], "output": []}, {"turn": 2, "transitions": [], "output": sent}])
    assert measures.judge(goals, replayed, [1, 2], {})["goals"]["1:0"] == "met"


@pytest.mark.asyncio
async def test_the_judge_at_analysis_hears_what_the_recorded_broadcast_reached_and_what_the_turn_changed(
        hass, monkeypatch):
    """At analysis the judge reads each recorded turn with what the home did then, as the recording shows it: a
    broadcast's satellites reached (one unavailable as the turn began reached nothing), and each device the turn's
    request changed, its settings too (the judge must see what was spoken where)."""
    from custom_components.assist_improver import simulate
    from custom_components.assist_improver.cases import recording_turns
    shown = []

    async def check(hass, entity_id, questions, turns):
        shown.extend(turns)
        return {"status": "completed", "answers": [
            {"goal": q["goal"], "turn": q["turn"], "gives": True, "why": "."} for q in questions]}
    monkeypatch.setattr(simulate, "check_facts", check)
    record = broadcast("bcast-judged", (VOICE,))
    asked = {"turn": 1, "summary": "Did it play", "kind": "fact", "fact": "Whether it played", "entity_ids": [VOICE]}
    for state, on in (("idle", [VOICE]), ("unavailable", [])):
        record["turns"][0]["source"]["states"][VOICE]["state"] = state
        shown.clear()
        await simulate.answered(hass, "ai_task.judge", view_of(record), [asked])
        [turn] = shown
        assert turn[4] == {"home_did": {"spoke": [{"message": "hello everyone", "on": on}]}}, state
    turns = recording_turns(record, replay=False)
    context = turns[0]["prepared"]["context"]["context"]["id"]
    record["state_changes"].append({"entity_id": SPEAKER, "at": turns[0]["source"]["at"], "old": {
        "state": "idle", "attributes": {"volume_level": 0.2}}, "new": {"state": "idle", "attributes": {
            "volume_level": 0.5}, "context": {"id": context}}})
    turn = simulate._recorded_turn(record, recording_turns(record, replay=False), 1)
    assert f"{SPEAKER}: volume_level 0.2 → 0.5" in turn[4]["home_did"]["changed"]  # Its settings, though not its state.



def test_the_judge_hears_every_setting_a_change_moved_whole_and_long_call_data_cut_saying_so():
    """#114: a change lists each setting one of its device's actions sets, all of them (none cut after the first
    few), the attributes nothing sets (a colour mode) aside; a call's data and a failed call's error are cut to
    CALL_CHARS, saying how much was left out."""
    from custom_components.assist_improver import simulate
    lamp = "light.fixture_lamp"
    moved = {"brightness": [10, 200], "hs_color": [[0, 0], [30, 50]], "rgb_color": [[255, 255, 255], [255, 191, 128]],
             "xy_color": [[0.32, 0.33], [0.45, 0.38]], "color_temp_kelvin": [2700, None], "color_mode": ["color_temp", "hs"]}
    long = "word " * 100
    did = simulate._home_did([], [{"action": "light.turn_on", "data": {"entity_id": lamp, "flash": long}},
                                  {"action": "light.turn_off", "data": {}, "error": long}],
                             {"states": {}}, [{"entity_id": lamp, "old": "off", "new": "on", "attributes_changed": moved}])
    [change] = did["changed"]
    assert change.startswith(f"{lamp}: off → on, ") and "color_mode" not in change
    assert all(f"{k} {old} → {new}" in change for k, (old, new) in moved.items() if k != "color_mode")
    data, failed = did["called"]
    cut = "more characters omitted]"
    assert cut in data and len(data) < len(long) and failed.startswith("light.turn_off: failed, word") and cut in failed

def test_the_judge_at_analysis_hears_that_a_recorded_turn_ran_a_script_whose_doings_a_recording_doesnt_keep():
    """A recording keeps no more of what a script did than the changes Home Assistant puts down to the turn's request,
    so a recorded turn that ran one says so where the judge reads what the home did: nothing spoken there isn't nothing
    done. A turn that ran no script says nothing of one."""
    from custom_components.assist_improver import simulate
    from custom_components.assist_improver.cases import recording_turns
    scripted = broadcast("bcast-scripted", (), "script__say_it")
    turn = simulate._recorded_turn(scripted, recording_turns(scripted, replay=False), 1)
    assert turn[4] == {"home_did": {"ran_scripts": ["script__say_it: what it did isn't recorded, beyond any change "
                                                    "listed"]}}
    plain = broadcast("bcast-unscripted", (VOICE,))
    assert "ran_scripts" not in simulate._recorded_turn(plain, recording_turns(plain, replay=False), 1)[4]["home_did"]


def both_sides(record, goal):
    """Whether an action was done, read the same way recorded and replayed (happened.py): by the recording's analysis
    (True, False or None: can't be told), and by a replay whose turn did what the recorded one did, its tool calls and
    its call log (met, unmet or indeterminate)."""
    from custom_components.assist_improver import measures
    from custom_components.assist_improver.cases import recording_turns
    view = view_of(record)
    [analysed] = analyzer.to_analysis(record["id"], {"goals": [goal], "not_requests": [], "question_for_user": ""},
                                      view)["goals"]
    output = [m for m in record["chat_log"]["content"][2:] if m["role"] != "user"]
    output = [{**m, "tool_result": m["result"]} if m["role"] == "tool_result" else m for m in output]
    replayed = measures.replay_view(recording_turns(record), view, [{"turn": 1, "changes": [], "output": output, **(
        {"actions": record["turns"][0]["actions"]} if "actions" in record["turns"][0] else {})}])
    return analysed["reached"], measures.judge(measures.goals_of({"turns": 1, "goals": [goal]}), replayed, [1], {})[
        "goals"]["1:0"]


def test_an_announcement_by_a_script_by_tts_speak_or_by_a_satellite_meets_an_action_on_both_sides():
    """DETAILS P9.10.5: what happened is one record on both sides, each turn's tool calls with their results and its
    call log, a script's own calls in it: words played on the speaker by a script (tts.speak under it), by tts.speak
    itself, or announced on the satellite of the speaker's device each meet the action, recorded as replayed. A call
    that failed says nothing; a script whose call log spoke nothing leaves it unmet; one recorded with no call log
    can't be told."""
    speech = {"entity_id": ["tts.fixture_engine"], "media_player_entity_id": [SPEAKER], "message": "hello everyone"}
    logs = {"script": [{"action": "script.say_it", "data": {}},
                       {"action": "tts.speak", "data": speech, "under": 0}],
            "tts.speak": [{"action": "tts.speak", "data": speech}],
            "satellite": [{"action": "assist_satellite.announce", "data": {"entity_id": [VOICE],
                                                                          "message": "hello everyone"}}]}
    for name, log in logs.items():
        record = broadcast(f"said-{name}", (), "script__say_it")
        record["turns"][0]["actions"] = log
        assert both_sides(record, SPOKEN) == (True, "met"), name
        record["turns"][0]["actions"] = [*log[:-1], {**log[-1], "error": "The speaker didn't answer"}]
        assert both_sides(record, SPOKEN) == (False, "unmet"), name
    record = broadcast("said-nothing", (), "script__say_it")
    record["turns"][0]["actions"] = logs["script"][:1]
    assert both_sides(record, SPOKEN) == (False, "unmet")  # Its call log: it spoke nothing.
    assert both_sides(broadcast("said-untold", (), "script__say_it"), SPOKEN)[0] is None  # A replay always keeps one.


GATE = "button.fixture_gate"
PRESSED = {"turn": 1, "summary": "Open the gate", "kind": "action", "outcome": "command", "entity_ids": [GATE]}


def gate(rid, reached):
    """A button pressed by Assist's one call, whose result names the devices it reached."""
    record = broadcast(rid, (), "intent__HassTurnOn")
    for message in record["chat_log"]["content"]:
        if message.get("role") == "tool_result":
            message["result"]["data"]["data"]["success"] = [{"name": "Gate", "type": "entity", "id": e}
                                                            for e in reached]
    for turn in record["turns"]:
        turn["source"]["states"][GATE] = {"entity_id": GATE, "state": "2026-01-01T08:00:00+00:00",
                                          "attributes": {"friendly_name": "Gate"}}
        turn["source"]["entities"][GATE] = {"name": "Gate", "aliases": [], "area_id": "fixture_area"}
    return record


def test_a_button_press_is_an_action_met_when_the_call_reached_it_and_never_a_state_to_reach():
    """DETAILS P9.10.5: a button keeps no state but when it was last pressed, so pressing it is an action goal
    (outcome command), met by a call that reached it, recorded as replayed, by Assist's call's result or by the call
    log; a press that failed or went elsewhere leaves it unmet. A state goal on it is refused, with how to say it."""
    assert both_sides(gate("gate-pressed", (GATE,)), PRESSED) == (True, "met")
    assert both_sides(gate("gate-missed", ()), PRESSED) == (False, "unmet")
    logged = gate("gate-logged", ())
    logged["turns"][0]["actions"] = [{"action": "button.press", "data": {"entity_id": [GATE]}}]
    assert both_sides(logged, PRESSED) == (True, "met")
    logged["turns"][0]["actions"][0]["error"] = "The gate didn't answer"
    assert both_sides(logged, PRESSED) == (False, "unmet")
    record = gate("gate-state", (GATE,))
    as_state = {"turn": 1, "summary": "Open the gate", "kind": "state", "entity_id": GATE, "state": "pressed"}
    assert analyzer.problems(view_of(record), {"goals": [as_state], "not_requests": [], "question_for_user": ""}) == [
        f"Goal 1 (turn 1): {GATE} keeps no state to reach: pressing or running it is an action goal with outcome "
        "command; a scene asked for is its devices' states."]


def test_a_button_a_script_pressed_is_met_by_its_last_press_by_assist_where_no_call_log_was_kept():
    """DETAILS P9.10.5: a script's run reaches what it presses; in a recording that kept no call log, Assist's own
    change to the button (its last press, in the turn) shows the press was done, and without one it can't be told."""
    chime, at = "button.chime", "2026-01-01T00:00:00+00:00"
    source = {"states": {chime: {"state": at, "attributes": {}}}, "entities": {chime: {}}}
    press = {"at": "2026-01-01T00:00:01+00:00", "during_turn": 1, "old": at, "new": "2026-01-01T00:00:01+00:00",
             "by": "assistant"}
    ring = {"turn": 1, "summary": "Ring the chime", "kind": "action", "outcome": "command", "entity_ids": [chime]}
    judged = []
    for changes in ({chime: [press]}, {}):
        view = SimpleNamespace(source=source, sources=[source], calls={1: [{"tool": "script__ring", "args": {},
                                                                            "result": {"data": {"result": {}}}}]},
                               actions={1: None}, changes=changes)
        judged.append(measures.judge(measures.goals_of({"turns": 1, "goals": [ring]}), view, [1])["goals"]["1:0"])
    assert judged == ["met", "indeterminate"]


@pytest.mark.asyncio
async def test_home_assistants_own_turn_on_reaches_a_device_by_the_call_it_makes_in_its_domain(hass):
    """DETAILS P9.10.5: homeassistant.turn_on calls each entity's own domain's turn_on, logged under it; a button has
    none, so Home Assistant presses nothing, and the call doesn't reach it. A toggle it does switch is reached by the
    call made in its domain."""
    from homeassistant.const import EVENT_CALL_SERVICE
    from homeassistant.core import Context
    from homeassistant.setup import async_setup_component
    from conftest import load_registries
    from custom_components.assist_improver.recorder import Actions
    chime, fan = "input_button.chime", "input_boolean.fan"
    hass.data["assist_improver_sandbox"] = True  # This offline Home Assistant's actions act only on itself.
    await load_registries(hass)
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(hass, "input_button", {"input_button": {"chime": {}}})
    assert await async_setup_component(hass, "input_boolean", {"input_boolean": {"fan": {}}})
    request, log, actions = Context(), Actions(), []
    log.watch("recording", request.id, actions)
    hass.bus.async_listen(EVENT_CALL_SERVICE, log.heard)
    await hass.services.async_call("homeassistant", "turn_on", {"entity_id": [chime, fan]}, blocking=True,
                                   context=request)
    await hass.async_block_till_done()
    source = {"states": {e: {"state": "off", "attributes": {}} for e in (chime, fan)}, "entities": {chime: {}, fan: {}}}
    view = SimpleNamespace(source=source, sources=[source], calls={1: []}, changes={}, actions={1: actions})
    press = {"turn": 1, "summary": "Ring the chime", "kind": "action", "outcome": "command", "entity_ids": [chime]}
    assert hass.states.get(chime).state == "unknown" and analyzer.done(view, press, [1]) is False
    assert hass.states.get(fan).state == "on" and analyzer.done(view, {**press, "entity_ids": [fan]}, [1]) is True


def test_a_scripts_run_may_be_a_sequence_goal_as_presses_standing_for_a_motion():
    """DETAILS P9.7.5, P9.7.6: a script that presses a door's relay runs on, then off; a sequence goal on it (the
    presses standing for a motion nobody can check) is accepted and met by that run, as the analysis judges it. A state
    goal on it, or a sequence on a button, whose state is only when it was last pressed, is refused."""
    script, button = "script.door_press", "button.chime"
    source = {"states": {script: {"state": "off", "attributes": {}}, button: {"state": "unknown", "attributes": {}}},
              "entities": {script: {}, button: {}}}
    changes = {script: [
        {"at": "2026-01-01T00:00:01+00:00", "during_turn": 1, "old": "off", "new": "on", "by": "assistant"},
        {"at": "2026-01-01T00:00:02+00:00", "during_turn": 1, "old": "on", "new": "off", "by": "assistant"}]}
    view = SimpleNamespace(source=source, sources=[source], calls={1: []}, actions={1: None}, changes=changes,
                           digest={"turns": [{"turn": 1, "user_says": "Open the door"}]}, notes={},
                           _entities=lambda: [script, button])
    run = {"turn": 1, "summary": "Open the door", "kind": "sequence", "entity_id": script, "states": ["on", "off"]}
    asking = lambda g: analyzer.problems(view, {"goals": [g], "not_requests": [], "question_for_user": ""})  # noqa: E731
    assert asking(run) == [] and measures.judge(measures.goals_of({"turns": 1, "goals": [run]}), view, [1])[
        "goals"]["1:0"] == "met"
    for refused in ({**run, "kind": "state", "state": "on"}, {**run, "entity_id": button}):
        assert [p for p in asking(refused) if "keeps no state to reach" in p], refused


def test_asking_again_for_a_message_already_spoken_isnt_a_step_towards_it():
    """As for a device already in the state asked for (P9.6): a turn asking again for a message its broadcast already
    spoke on the speaker isn't a step towards it; after one that reached nothing, it is."""
    again = {"turn": 2, "summary": "Asked again", "kind": "step", "serves": 1}
    for reached, refused in (((VOICE,), True), ((), False)):
        view = view_of(broadcast(f"again-{len(reached)}", reached, more=["Say it again"]))
        found = analyzer.problems(view, {"goals": [SPOKEN, again], "not_requests": [], "question_for_user": ""})
        assert any("already done by turn 2" in p for p in found) is refused, found


def test_a_question_about_a_device_doesnt_make_assists_change_to_it_wanted():
    """A fact names the devices its answer is about (P9.7.4): asking about the fridge doesn't ask for it to change,
    so Assist switching it off is still a side effect left, and a later turn putting it right is a repair."""
    view = view_of(home("asked001", CARELESS_TURNS))
    asked = {"turn": 1, "summary": "Whether the fridge is on", "kind": "fact", "fact": "The fridge's state",
             "entity_ids": [FRIDGE], "serves": 1}
    assert analyzer.side_effects_left(view, [RELAY_OFF, asked]) == [
        {"entity_id": FRIDGE, "start": "on", "end": "off", "turn": 1}]
    leaves_it_open = {"turn": 1, "summary": "The fridge", "kind": "ask", "entity_ids": [FRIDGE], "serves": 1}
    assert analyzer.side_effects_left(view, [RELAY_OFF, leaves_it_open]) == []  # A control left open is wanted.
    view = view_of(home("asked002", [("Is the fixture fridge on?", [("Off", "Fixture fridge", [FRIDGE])], "Off now."),
                                     ("Turn the fixture fridge back on.", [("On", "Fixture fridge", [FRIDGE])], "On.")]))
    question = {k: v for k, v in asked.items() if k != "serves"}  # Here the question is the root.
    repair = {"turn": 2, "summary": "Fridge back on", "kind": "state", "entity_id": FRIDGE, "state": "on", "serves": 1}
    assert analyzer.problems(view, {"goals": [question, repair], "not_requests": [], "question_for_user": ""}) == []


def test_a_side_effect_is_assists_own_change_not_a_devices_follow_on():
    # Assist switched only the relay; the fridge's plug, on the same device, followed by itself.
    record = home("followon", [(CARELESS, [("Off", "Fixture relay", [RELAY, FRIDGE])], "Done.")])
    record["state_changes"][1]["new"]["context"] = {"id": "the device itself", "parent_id": None, "user_id": None}
    for turn in record["turns"]:
        for entity in (RELAY, FRIDGE):
            turn["source"]["entities"][entity]["device_id"] = "one-device"
    record["chat_log"]["content"][3]["result"]["data"]["data"]["success"] = [
        {"name": "Fixture relay", "type": "entity", "id": RELAY}]
    view = view_of(record)
    assert view.changes[FRIDGE][0]["by"].startswith("follow-on")
    assert analyzer.side_effects_left(view, [RELAY_OFF]) == []


NO_ONE = {"id": "at-the-plug", "parent_id": None, "user_id": None}  # No one signed in to Home Assistant.
LATER = "2026-01-01T00:05:00+00:00"  # Five minutes after a one-turn conversation's last reply.


def put_back(record, entity, state, context, at=LATER):
    """Someone else's change to a device after Assist's, with the context Home Assistant gave it."""
    was = next(c["new"] for c in reversed(record["state_changes"]) if c["entity_id"] == entity)
    was = {k: v for k, v in was.items() if k != "context"}
    record["state_changes"].append({"entity_id": entity, "at": at, "old": was,
                                    "new": {**was, "state": state, "context": dict(context)}})
    return record


def with_device(record, entity, start, *changes):
    """The fixture home with one more device, in state start at every turn, and its changes: (seconds after the
    conversation began, new state, by), by "assistant" (the first turn's request) or someone else's context."""
    began = datetime(2026, 1, 1, tzinfo=UTC)
    for turn in record["turns"]:
        turn["source"]["states"][entity] = {"entity_id": entity, "state": start, "attributes": {}}
        turn["source"]["entities"][entity] = {"name": entity, "aliases": [], "area_id": "fixture_area"}
    assist, state = record["turns"][0]["prepared"]["context"]["context"], start
    for seconds, new, by in changes:
        record["state_changes"].append({
            "entity_id": entity, "at": (began + timedelta(seconds=seconds)).isoformat(),
            "old": {"entity_id": entity, "state": state, "attributes": {}},
            "new": {"entity_id": entity, "state": new, "attributes": {},
                    "context": dict(assist if by == "assistant" else by)}})
        state = new
    record["state_changes"].sort(key=lambda c: c["at"])
    return record


def test_only_how_assist_left_a_device_counts_whoever_puts_it_back_later():
    """Only how Assist left the device matters (Kai, 2026-11-02: "Any put-back counts"; DETAILS P9.12.5). Assist
    switched off the fridge's plug beside the relay; five minutes later it's switched back on, by no one signed in to
    Home Assistant (at the plug or in its own app, its own timer, an automation Assist didn't set off) or by someone
    signed in. Neither clears it: it's left as Assist left it, on the goal whose turn made it (P9.12.4). Asking Assist
    to put it back counts as that turn instead: Assist left it as it started, so nothing is left, and the turn is an
    extra turn."""
    for rid, context in (("nobody01", NO_ONE), ("signedin", {"id": "in-the-app", "parent_id": None,
                                                             "user_id": "fixture-user"})):
        view = view_of(put_back(home(rid, CARELESS_TURNS), FRIDGE, "on", context))
        assert view.changes[FRIDGE][-1]["by"] not in analyzer.ASSISTANT
        assert analyzer.side_effects_left(view, [RELAY_OFF]) == [
            {"entity_id": FRIDGE, "start": "on", "end": "off", "turn": 1}]
    asked = home("asked003", [*CARELESS_TURNS, ("Turn the fixture fridge back on.",
                                                [("On", "Fixture fridge", [FRIDGE])], "It's on.")])
    repair = {"turn": 2, "summary": "Fridge back on", "kind": "state", "entity_id": FRIDGE, "state": "on", "serves": 1}
    analysis = analyzer.to_analysis("asked003", {"goals": [RELAY_OFF, repair], "not_requests": [],
                                                 "question_for_user": ""}, view_of(asked))
    assert analysis["side_effects_left"] == [] and analysis["extra_turns"] == [2]


def test_a_put_back_home_assistant_ties_to_assist_only_loosely_is_still_someone_elses():
    """Anyone or anything else putting it back doesn't clear it (DETAILS P9.12.5), however loosely Home Assistant ties
    the put-back to Assist: the fridge's plug reports Assist's change without Assist's request on it (only Assist's
    results name it as its), and is switched back on at the plug after Assist's reply; or Assist's change sets off an
    automation that switches it back on, whose run names Assist's request only as its parent. The control: what
    Assist's request itself set off (a sentence trigger's actions, all with it only as their parent) is Assist's, all
    of it, so the plug switched off, then back on, by that is left as it started."""
    plug = {"id": "the-plug-itself", "parent_id": None, "user_id": None}
    unmarked = home("noctx001", CARELESS_TURNS)
    for change in unmarked["state_changes"]:
        if change["entity_id"] == FRIDGE:
            change["new"]["context"] = dict(plug)
    automation = {"id": "automation-run", "parent_id": "autorun1-context-1", "user_id": None}
    for record, context in ((unmarked, NO_ONE), (home("autorun1", CARELESS_TURNS), automation)):
        view = view_of(put_back(record, FRIDGE, "on", context))
        assert [c["by"] in analyzer.ASSISTANT for c in view.changes[FRIDGE]] == [True, False]
        assert analyzer.side_effects_left(view, [RELAY_OFF]) == [
            {"entity_id": FRIDGE, "start": "on", "end": "off", "turn": 1}]
    triggered = {"id": "sentence-trigger-run", "parent_id": "trigger1-context-1", "user_id": None}
    record = home("trigger1", CARELESS_TURNS)
    for change in record["state_changes"]:
        if change["entity_id"] == FRIDGE:
            change["new"]["context"] = dict(triggered)
    view = view_of(put_back(record, FRIDGE, "on", triggered))
    assert all(c["by"] == "assistant" for c in view.changes[FRIDGE])
    assert analyzer.side_effects_left(view, [RELAY_OFF]) == []


def test_a_devices_own_follow_on_is_how_assist_left_it_and_a_later_change_elsewhere_is_judged_by_that():
    """A device's own follow-on of Assist's change is how that change came through (DETAILS P9.12.2): Assist set the
    blind opening, and the blind reported it open four seconds later, so Assist left it open, and someone closing it
    later doesn't clear it. Someone else moving a device on to another state is no put-back either: it's judged by how
    Assist left it (the heater on heat, not cooling), and when Assist left it as it started (heat, then off again in its
    turn), nothing is left whatever anyone does later."""
    blind, heater = "cover.fixture_blind", "climate.fixture_heater"
    relay_only = [(CARELESS, [("Off", "Fixture relay", [RELAY])], "Done.")]  # Assist's change: 4 seconds in.
    itself = {"id": "the-blind-itself", "parent_id": None, "user_id": None}
    view = view_of(with_device(home("blind001", relay_only), blind, "closed",
                               (4.5, "opening", "assistant"), (8.5, "open", itself), (300, "closed", NO_ONE)))
    assert [c["by"].split(" ")[0] for c in view.changes[blind]] == ["assistant", "follow-on", "not"]
    assert analyzer.side_effects_left(view, [RELAY_OFF]) == [
        {"entity_id": blind, "start": "closed", "end": "open", "turn": 1}]
    view = view_of(with_device(home("heater01", relay_only), heater, "off",
                               (4.5, "heat", "assistant"), (300, "cool", NO_ONE)))
    assert analyzer.side_effects_left(view, [RELAY_OFF]) == [
        {"entity_id": heater, "start": "off", "end": "heat", "turn": 1}]
    view = view_of(with_device(home("heater02", relay_only), heater, "off",
                               (4.5, "heat", "assistant"), (4.6, "off", "assistant"), (300, "cool", NO_ONE)))
    assert analyzer.side_effects_left(view, [RELAY_OFF]) == []


def test_a_script_assist_ran_is_no_side_effect_however_long_it_runs():
    """A script is on only while it runs, and it ends its run itself: no one putting it back. What it changed counts
    on the devices it changed (with Assist's request), so the script itself is never a side effect left, finished or
    still running."""
    press, relay_only = "script.fixture_press", [(CARELESS, [("Off", "Fixture relay", [RELAY])], "Done.")]
    itself = {"id": "the-script-itself", "parent_id": None, "user_id": None}
    for rid, run in (("ran00001", [(4.5, "on", "assistant"), (20, "off", itself)]),
                     ("ran00002", [(4.5, "on", "assistant")])):
        view = view_of(with_device(home(rid, relay_only), press, "off", *run))
        assert analyzer.side_effects_left(view, [RELAY_OFF]) == []


def test_a_side_effect_counts_on_the_goal_whose_turn_made_it():
    goals = [RELAY_OFF, {"turn": 3, "summary": "Lamp on", "kind": "state", "entity_id": "light.lamp", "state": "on"},
             {"turn": 4, "summary": "Lamp brighter", "kind": "step", "serves": 3}]
    analysis = {"recording_id": "x", "goals": goals, "not_requests": [{"turn": 2, "what": "small talk"}],
                "extra_turns": [4], "side_effects_left": [
                    {"entity_id": "switch.a", "start": "on", "end": "off", "turn": 1},
                    {"entity_id": "switch.b", "start": "on", "end": "off", "turn": 2},  # Latest goal up to it.
                    {"entity_id": "switch.c", "start": "on", "end": "off", "turn": 4}]}  # The goal its sub-goal serves.
    by_goal = friction_by_goal(analysis)
    assert sorted(f["entity_id"] for f in by_goal[1] if f["kind"] == "side effect") == ["switch.a", "switch.b"]
    assert [f.get("entity_id") for f in by_goal[3]] == [None, "switch.c"]  # The extra turn, then the side effect.


def test_turns_no_longer_needed_are_those_the_goals_took_without_the_change_and_not_with_it():
    """The thanks is said in both replays, so it's a turn the goal took both ways: only the correction goes."""
    from test_changeset import tested
    from custom_components.assist_improver.improvements import gains, row
    test = tested("Relay off", said=("Relay off.", "No, the relay.", "Thanks."), replies=("Which?", "Done.", "Sure."))
    test["after"].update(said=[1, 3], counted={"1:0": [1, 3]}, turns_per_root_goal={"1:0": 2})
    assert row(test)["no_longer_needed"] == ["“No, the relay.”"]
    counted = gains({"test": test})
    assert (counted["enabled"], counted["removed"]) == (0, 1)  # As the review counts it.


def test_the_note_needs_a_goal_and_an_objection_quotes_the_households_words_about_a_turn_of_its_goal():
    view = view_of(home("wordy001", WORDY_TURNS))
    base = {"goals": [RELAY_OFF, LESS_WORDY_GOAL], "not_requests": [], "question_for_user": ""}
    assert analyzer.problems(view, base) == []
    noted = {**base, "goals": [*base["goals"], NOTE_GOAL]}  # The note is the last turn, a sub-goal (SPEC.md P0.3).
    assert analyzer.problems(view, noted, note=NOTE) == []
    assert analyzer.problems(view, base, note=NOTE)[0].startswith("Turns 3 have no goal")

    def found(**changed):
        return " ".join(analyzer.problems(view, {**base, "goals": [RELAY_OFF, {**LESS_WORDY_GOAL, **changed}]}))
    # Their words, word for word, case, punctuation and apostrophes aside; … marks words left out.
    assert found(quote="Please be LESS wordy, and skip the disclaimers") == ""
    assert found(quote="less wordy … next time") == ""
    assert "quote must be the household's own words from turn 2" in found(quote="Keep confirmations short")
    assert "quote must be" in found(quote="next time … less wordy")  # The parts in the order they were said.
    assert "needs quote" in found(quote=" ")
    # About: an Assist turn of its goal's, before the objection; "all" only for the note.
    assert "about must be a turn of its goal's" in found(about=2)
    assert "about all is for a note" in found(about="all")
    assert "needs about" in " ".join(analyzer.problems(view, {**base, "goals": [
        RELAY_OFF, {k: v for k, v in LESS_WORDY_GOAL.items() if k != "about"}]}))
    # A reaction serves the turn where its goal starts, and its turn isn't a not-request.
    assert "a conduct is a sub-goal" in " ".join(analyzer.problems(view, {**base, "goals": [
        RELAY_OFF, {k: v for k, v in LESS_WORDY_GOAL.items() if k != "serves"}]}))
    quiet = analyzer.problems(view, {**base, "not_requests": [{"turn": 2, "what": "a remark"}]})
    assert any("take it out of not_requests" in p for p in quiet)
    # A fact asked at a later turn is a goal of its own, or a step; and a step only while its goal wasn't met yet (the
    # relay was off by turn 2: checking on it is no step towards it).
    later = {"turn": 2, "summary": "Did it work?", "kind": "fact", "fact": "whether it worked", "serves": 1}
    assert "a fact asked at a later turn" in " ".join(analyzer.problems(view, {**base, "goals": [RELAY_OFF, later]}))
    checked = {"turn": 2, "summary": "Did it work?", "kind": "step", "serves": 1}
    assert "already done by turn 2" in " ".join(analyzer.problems(view, {**base, "goals": [RELAY_OFF, checked]}))


def test_a_quote_may_differ_in_its_apostrophes_and_cuts_but_must_be_their_words_in_one_tree_and_turn():
    """The household's words as speech-to-text wrote them, against a model's typography: curly or straight
    apostrophes, … or ... for a cut. An objection's about must be a turn of its own goal's (not another goal's
    reply), and a conduct goal, a wrap-up or a thanks serves the turn where its goal starts, not a step's."""
    assert analyzer.quoted("you don’t have to … in a terse way", "You don't have to tell me that. Just say it in a "
                                                                 "terse way.")
    assert analyzer.quoted("don't have to...terse", "You don’t have to be so long; be terse.")
    turns = [WORDY_TURNS[0], ("Switch off the fixture fridge too.", [("Off", "Fixture fridge", [FRIDGE])], "Done."),
             ("You said too much the first time.", [], "Sorry.")]
    view = view_of(home("three001", turns))
    fridge = {"turn": 2, "summary": "Fridge off", "kind": "state", "entity_id": FRIDGE, "state": "off"}
    objection = {"turn": 3, "summary": "Too much", "kind": "conduct", "about": 1, "quote": "You said too much",
                 "serves": 1}
    assert analyzer.problems(view, {"goals": [RELAY_OFF, fridge, objection], "not_requests": [],
                                    "question_for_user": ""}) == []
    elsewhere = analyzer.problems(view, {"goals": [RELAY_OFF, fridge, {**objection, "about": 2}], "not_requests": [],
                                         "question_for_user": ""})
    assert any("about must be a turn of its goal's" in p for p in elsewhere)  # Turn 2 is the fridge's.
    step = {"turn": 2, "summary": "Again", "kind": "step", "serves": 1}
    thanks = {"turn": 3, "summary": "Thanks", "kind": "thanks", "serves": 2}
    found = analyzer.problems(view_of(home("three002", [WORDY_TURNS[0], ("The relay, off.", [], "Done."),
                                                       ("Thanks.", [], "Sure.")])),
                              {"goals": [RELAY_OFF, step, thanks], "not_requests": [], "question_for_user": ""})
    assert any("a thanks serves the turn where its goal itself starts" in p for p in found)


def test_a_later_sub_goal_that_was_already_so_before_its_turn_is_refused_so_no_change_skips_it_for_free():
    """A turn whose sub-goal was already met when it was said would be skipped by any change, a turn saved for
    nothing: checking that the fridge is back on after it was put right, or that the relay is off when a step towards
    it is open, isn't a part, a repair or a step. The analyzer is told what it is instead."""
    fridge_back = {"turn": 2, "summary": "Fridge back on", "kind": "state", "entity_id": FRIDGE, "state": "on",
                   "serves": 1}
    checked = {"turn": 3, "summary": "Is the fridge on?", "kind": "state", "entity_id": FRIDGE, "state": "on",
               "serves": 1}
    view = view_of(home("fridge01", [CARELESS_TURNS[0], ("Turn the fixture fridge back on.",
                                                         [("On", "Fixture fridge", [FRIDGE])], "Done."),
                                     ("Is the fridge on now?", [], "Yes.")]))
    found = analyzer.problems(view, {"goals": [RELAY_OFF, fridge_back, checked], "not_requests": [],
                                     "question_for_user": ""})
    assert found == ["Goal 3 (turn 3): switch.fixture_fridge was already that way by turn 3, so nothing there was put "
                     "right: a check because a reply left them unsure is a conduct goal about that reply, and a new "
                     "request a goal of its own."]
    view = view_of(home("relay001", [(CARELESS, [], "Done."), ("The relay, off.", [], "Done."),
                                     ("Is it off?", [], "Yes.")]))
    again = {"turn": 2, "summary": "Again", "kind": "step", "serves": 1}
    off = {**RELAY_OFF, "turn": 3, "summary": "Is it off?", "serves": 2}
    found = analyzer.problems(view, {"goals": [RELAY_OFF, again, off], "not_requests": [], "question_for_user": ""})
    assert any("its goal at turn 2 already wants switch.fixture_relay" in p for p in found)  # Through the step.


def test_a_note_that_asks_again_for_what_a_goal_didnt_get_is_trouble_too():
    """A note may hold a step instead of an objection: the household sent the conversation in to ask again, so it
    has trouble and goes on to Propose, never "no trouble found"."""
    analysis = {"recording_id": "x", "turns": 1, "extra_turns": [], "side_effects_left": [], "goals": [
        {**RELAY_OFF, "reached": True}, {"turn": 2, "summary": "The fridge too", "kind": "step", "serves": 1}]}
    assert friction_by_goal(analysis) == {1: [{"turn": 2, "kind": "extra turn", "in_note": True, "served": [
        {"summary": "The fridge too", "for_goal_at_turn": 1}]}]}
    thanked = {**analysis, "goals": [{**RELAY_OFF, "reached": True}, {
        "turn": 2, "summary": "It worked", "kind": "thanks", "serves": 1}]}
    assert friction_by_goal(thanked) == {}  # A note that only says it worked: no trouble.
    objected = {**thanked, "goals": [*thanked["goals"], {"turn": 2, "summary": "Less chat", "kind": "conduct",
                                                         "serves": 1, "about": 1, "quote": "less chat"}]}
    assert [f["kind"] for f in friction_by_goal(objected)[1]] == ["conduct"]  # Only what it objects to.


def test_an_objection_names_only_an_improvement_applied_before_the_conversation():
    view = view_of(home("wordy001", WORDY_TURNS))
    started = view.digest["started"]
    applied = {"improvement": "imp1", "title": "Say the doors' position can't be confirmed", "status": "applied",
               "applied": "2025-12-31T00:00:00+00:00"}
    later = {**applied, "improvement": "imp2", "applied": "2026-02-01T00:00:00+00:00"}
    waiting = {"improvement": "imp3", "title": "Waiting", "status": "waiting for review"}
    answer = lambda name: {"goals": [RELAY_OFF, {**LESS_WORDY_GOAL, "improvement": name}],  # noqa: E731
                           "not_requests": [], "question_for_user": ""}
    assert started > applied["applied"]
    assert analyzer.problems(view, answer("imp1"), existing=[applied, later, waiting]) == []
    assert "applied after this session" in analyzer.problems(view, answer("imp2"), existing=[applied, later])[0]
    assert "no applied improvement 'imp3'" in analyzer.problems(view, answer("imp3"), existing=[waiting])[0]
    analysis = analyzer.to_analysis("wordy001", answer("imp1"), view)
    assert analysis["goals"][1]["improvement"] == "imp1"
    # The proposer reads which improvement the objection is about, on the turn it was said.
    shown = scenario(analysis, view)
    assert shown["turns"][1]["conduct"] == [{"quote": LESS_WORDY, "about_assist_turn": 1, "about_improvement": "imp1",
                                             "goal_turn": 1}]


def test_a_thanks_only_turn_isnt_an_extra_turn_but_one_with_an_objection_is():
    thanks = {"turn": 2, "summary": "Thanks", "kind": "thanks", "serves": 1}
    objection = {"turn": 3, "summary": "Shorter", "kind": "conduct", "about": 1, "quote": "shorter", "serves": 1}
    assert analyzer.extra_turns(3, [RELAY_OFF, thanks, objection], []) == [3]
    assert analyzer.extra_turns(2, [RELAY_OFF, thanks, {**objection, "turn": 2}], []) == [2]


def test_the_quick_check_treats_complaints_and_feedback_as_not_smooth():
    from custom_components.assist_improver import triage
    assert "complains or comments on how Assist behaved" in triage.INSTRUCTIONS


def test_a_proposal_can_revise_an_applied_improvements_household_line():
    """Feedback about what an applied improvement makes Assist do: a preference that replaces its household line
    passes the proposer's checks and replaces it in the sandbox, as applying would."""
    from custom_components.assist_improver import household
    from custom_components.assist_improver.changes import setup_from_changes
    line = "Say the garage doors' position can't be confirmed."
    knowledge = {"everyone": [line], "people": {}}
    change = {"kind": "preference", "for": "everyone", "text": "Say the garage doors are moving; no disclaimers.",
              "replaces": [line]}
    revised, _, problems = household.edit(knowledge, [change], "fixture-user")
    assert problems == [] and revised["everyone"] == [change["text"]]
    source = {"states": {RELAY: {"state": "on"}}, "household": knowledge}
    setup = setup_from_changes([change], source=source, prompt="Recorded template", scripts={}, user_id="alex")
    assert setup["household"]["everyone"] == [change["text"]]
