"""Each assistant's own instructions: the proposer and reviewer see them per conversation, a prompt change edits one
assistant's (checked by code, replayed only on that assistant's conversations, applied there with conflicts and
undo), and the recommended facts are offered only where an assistant's instructions lack them, never twice."""

import json
from copy import deepcopy
from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.helpers import llm
from homeassistant.util import dt as dt_util

from conftest import assist_home, household_user
from ha_task import ENTITIES, serve, use
from recordings import (PROFILE, REPLAY_ENTITIES, TARGET, as_kept, before, bind_agent, make_recording,
                        prepared_recording, recorded_today)
from test_ai_roles import ANALYSIS
from test_changeset import tested
from custom_components.assist_improver import changeset, hypotheses, instructions, panel
from custom_components.assist_improver.analyzer import to_analysis
from custom_components.assist_improver.cases import CaseError, digest
from custom_components.assist_improver.changes import setup_from_changes
from custom_components.assist_improver.evidence import Evidence
from custom_components.assist_improver.exploration import SessionView

ENTRY = "fixture-entry"
PLAIN = "You are a voice assistant for Home Assistant.\nAnswer in one short sentence.\n"  # Never says who's speaking.
KNOWS = PLAIN + "{% if user_name %}You're talking with {{ user_name }}.{% endif %}\n"
SPEAKER = instructions.FACTS["speaker"].line
NAME = "Tester"  # The stand-in person speaking.
SMOOTH = {"goals": [{"turn": 1, "summary": "Relay off", "kind": "state", "entity_id": TARGET, "state": "off"}],
          "not_requests": [], "question_for_user": ""}


def on_assistant(record, rid, subentry, title, prompt, options=None):
    """A recording of a conversation with one assistant (its config subentry), under its own id."""
    record = deepcopy(record)
    record["id"] = rid
    for turn in record["turns"]:
        turn["prepared"]["provider_config"] = [{
            "config_entry_id": ENTRY, "subentry_id": subentry, "title": title,
            "options": {**(options or {"chat_model": "fixture-no-model", "llm_hass_api": ["assist"]}), "prompt": prompt}}]
    return record


def in_the_study(record):
    """The recording's asking device is a speaker in an area."""
    for turn in record["turns"]:
        turn["source"]["devices"] = {"fixture-phone": {"name": "Study speaker", "name_by_user": None,
                                                       "area_id": "fixture_area"}}
    return record


def sessions_over(*recordings, analyses=None):
    views = [SessionView(r) for r in recordings]
    found = [to_analysis(r["id"], (analyses or {}).get(r["id"], ANALYSIS), v) for r, v in zip(recordings, views)]
    return hypotheses.Sessions(found, views)


def two_assistants():
    """A conversation with each of two assistants; the first one's instructions don't say who's speaking, and a
    third conversation with it went smoothly."""
    plain = on_assistant(make_recording(), "plainone-0001", "plain", "Plain assistant", PLAIN)
    knows = on_assistant(make_recording(), "knowsone-0001", "knows", "Knowing assistant", KNOWS)
    from kit import variant
    smooth = on_assistant(variant(make_recording(), "plaintwo-0001", "Fixture relay off, please.", turns=1),
                          "plaintwo-0001", "plain", "Plain assistant", PLAIN)
    return sessions_over(plain, knows, smooth, analyses={"plaintwo-0001": SMOOTH})


def set_prompt(home, subentry, prompt):
    """An assistant's instructions changed in Home Assistant."""
    entry = home.provider
    home.hass.config_entries.async_update_subentry(entry, entry.subentries[subentry],
                                                   data={**entry.subentries[subentry].data, "prompt": prompt})


def idea(*changes, session="plainone", title="Say who's speaking"):
    return {"title": title, "cause": "Assist didn't know who was speaking", "goals": [{"session": session,
                                                                                         "goal_turn": 1}],
            "changes": list(changes), "expected": "One turn", "conflicts": [],
            "support": [{"session": session, "turn": 1, "note": "the user meant their own relay"}]}


def say_who(assistant="Plain assistant", **extra):
    return {"kind": "assistant", "assistant": assistant, "text": SPEAKER, **extra}


def edit_problems(found):
    return [p for p in found if "goals still have friction" not in p]


def provider(prompts=None):
    """Home Assistant's config entry holding both assistants, as a provider keeps them (subentries)."""
    prompts = {"plain": PLAIN, "knows": KNOWS, **(prompts or {})}
    titles = {"plain": "Plain assistant", "knows": "Knowing assistant"}
    return ConfigEntry(domain="anthropic", title="Provider", data={"api_key": "sk-fixture"}, source="user", version=2,
                       minor_version=4, options={}, unique_id=None, discovery_keys=MappingProxyType({}),
                       state=ConfigEntryState.NOT_LOADED, entry_id=ENTRY, subentries_data=[
                           {"data": {"prompt": prompts[sub], "chat_model": "fixture", "llm_hass_api": ["assist"]},
                            "subentry_type": "conversation", "title": titles[sub], "unique_id": None,
                            "subentry_id": sub} for sub in ("plain", "knows")])


@pytest_asyncio.fixture
async def home(hass, effect_guards):
    await assist_home(hass, REPLAY_ENTITIES)
    hass.auth = SimpleNamespace(async_get_user=AsyncMock(return_value=household_user(NAME)))
    entry = provider()
    hass.config_entries._entries[entry.entry_id] = entry
    return SimpleNamespace(hass=hass, provider=entry)


# The recommended facts.

def test_the_recommended_facts_are_a_short_list_in_one_place_without_what_home_assistant_says():
    # The date and time too: Home Assistant leaves them out when Assist has its date-and-time tool, as by default.
    assert [f.key for f in instructions.RECOMMENDED] == ["speaker", "device", "date_time", "time_zone"]
    for fact in instructions.RECOMMENDED:
        assert fact.line and fact.why and fact.markers and fact.what
        assert "area" not in fact.line  # Assist's own part says the asking device's area.
        assert fact.key in instructions.carried(fact.line)  # Each line is found again once it's there.
    listed = hypotheses.GENERATOR_INSTRUCTIONS
    assert all(f.line in listed and f.line in hypotheses.REVIEWER_INSTRUCTIONS for f in instructions.RECOMMENDED)


def test_a_fact_counts_as_given_by_its_template_code_and_one_written_in_words_is_a_hint_for_the_roles():
    keys = lambda found: {f["fact"] for f in found}  # noqa: E731
    assert keys(instructions.missing(PLAIN)) == {"speaker", "device", "date_time", "time_zone"}
    assert "speaker" not in keys(instructions.missing(KNOWS))  # {{ user_name }}.
    by_user = PLAIN + "{% if llm_context.context.user_id == 'abc' %}It's the owner.{% endif %}"
    assert "speaker" not in keys(instructions.missing(by_user))  # Who's speaking, told by their user ID.
    # Written out in words: maybe given already, which the roles judge; not taken off the list by code.
    written = PLAIN + f"You're talking with {NAME}.\n"
    [speaker] = [f for f in instructions.missing(written, values={"speaker": NAME}) if f["fact"] == "speaker"]
    assert speaker["maybe_there"].startswith(f"it says {NAME}")
    [speaker] = [f for f in instructions.missing(PLAIN, rendered=f"Hello {NAME}.", values={"speaker": NAME})
                 if f["fact"] == "speaker"]
    assert "maybe_there" in speaker
    assert all("maybe_there" not in f for f in instructions.missing(PLAIN, values={"speaker": NAME}))
    # Prose that merely mentions a time zone isn't the fact's template code.
    assert "time_zone" in keys(instructions.missing(PLAIN + "Keep the timezone in mind when it's late.\n"))
    assert "time_zone" not in keys(instructions.missing(PLAIN + "Times are in {{ now().tzinfo }}."))
    assert "date_time" not in keys(instructions.missing(PLAIN + instructions.FACTS["date_time"].line))
    device = PLAIN + "They use {{ device_attr(llm_context.device_id, 'name') }}."
    assert "device" not in keys(instructions.missing(device))
    assert keys(instructions.missing("")) == keys(instructions.missing(PLAIN))  # Home Assistant's default ones.


def elsewhere():
    """A time zone other than this test process's."""
    return "Pacific/Auckland" if str(dt_util.get_default_time_zone()) != "Pacific/Auckland" else "America/New_York"


@pytest.mark.asyncio
async def test_each_recommended_fact_renders_in_the_sandbox(hass):
    record = in_the_study(on_assistant(make_recording(), "plainone-0001", "plain", "Plain assistant", PLAIN))
    zone = elsewhere()
    for turn in record["turns"]:
        turn["source"]["time_zone"] = zone  # The household's, as Home Assistant has it.
    s = sessions_over(record)
    s.evidence = Evidence(s, recorded_today(record))
    found = await s.evidence.render(s.views["plainone"], [f.line for f in instructions.RECOMMENDED], NAME)
    rendered = [r.get("rendered") for r in found]
    assert rendered[:2] == [f"The person speaking is {NAME}.", "The request came from Study speaker."]
    assert rendered[2].startswith("It's ") and rendered[3] == f"The household's time zone is {zone}."
    # The asking device's area is Assist's own to say: no fact repeats it. A replay with the time zone fact reads the
    # household's zone too.
    from custom_components.assist_improver.cases import recording_turns
    from custom_components.assist_improver.replay import SandboxHome
    turn = recording_turns(record)[0]
    sandbox = SandboxHome(hass, record, turn, "area", recorded_today(record),
                          change={"prompt": PLAIN + instructions.FACTS["time_zone"].line})
    try:
        prompt = (await sandbox.assist(turn))["prompt"]
    finally:
        await sandbox.stop()
    assert "You are in area Fixture room" in prompt
    assert f"The household's time zone is {zone}." in prompt
    # Without a speaker or an asking device, the lines say nothing rather than something wrong.
    record["turns"][0]["prepared"]["context"]["device_id"] = None
    quiet = await s.evidence.render(SessionView(record), [f.line for f in instructions.RECOMMENDED[:2]])
    assert [r.get("rendered") for r in quiet] == ["", ""]


def test_a_fact_is_proposed_only_where_its_assistants_instructions_lack_it():
    s = two_assistants()
    rows = {r["assistant"]: r for r in s.assistant_rows()}
    assert "who's speaking" in rows["Plain assistant"]["recommended_facts_missing"]
    assert "who's speaking" not in rows["Knowing assistant"]["recommended_facts_missing"]
    assert edit_problems(hypotheses.problems(s, {"hypotheses": [idea(say_who())]})) == []
    found = hypotheses.problems(s, {"hypotheses": [idea(say_who("Knowing assistant"), session="knowsone")]})
    assert any("Knowing assistant's instructions already give who's speaking (its template uses user_name)" in p
               for p in found), found


def test_a_fact_is_never_proposed_twice():
    s = two_assistants()
    twice = hypotheses.problems(s, {"hypotheses": [idea(say_who()), idea(say_who(), title="Again")]})
    assert any("adds who's speaking to Plain assistant's instructions too" in p for p in twice), twice
    # An improvement waiting for review (or applied) adds it already.
    s.existing = [{"improvement": "i1", "title": "Know who's speaking", "status": "waiting for review",
                   "changes": [say_who(target=f"{ENTRY}/plain")], "goals": [], "devices": []}]
    found = hypotheses.problems(s, {"hypotheses": [idea(say_who())]})
    assert any("Know who's speaking" in p and "already adds who's speaking" in p for p in found), found
    # Replacing the line that says it, with a line that says it better, is fine.
    s.existing = []
    better = say_who("Knowing assistant", replaces=["{% if user_name %}You're talking with {{ user_name }}.{% endif %}"])
    assert edit_problems(hypotheses.problems(s, {"hypotheses": [idea(better, session="knowsone")]})) == []


# Each assistant's instructions, as the proposer and reviewer see them.

def test_each_conversations_assistant_and_its_instructions_reach_the_proposer_and_reviewer():
    s = two_assistants()
    overview = s.overview()
    assert {r["assistant"]: r["conversations"] for r in overview["assistants"]} == {
        "Plain assistant": 2, "Knowing assistant": 1}
    assert {f["session"]: f["assistant"] for f in overview["friction_scenarios"]} == {
        "plainone": "Plain assistant", "knowsone": "Knowing assistant"}
    assert {r["assistant"] for r in s.summary()["assistants"]} == {"Plain assistant", "Knowing assistant"}
    reader = hypotheses.ReadAssistantInstructions(s)
    read = reader.run(session="plainone")
    assert read["assistant"] == "Plain assistant" and read["instructions_it_ran_with"] == PLAIN
    assert read["current_instructions"] == PLAIN and read["same_as_now"] is True
    assert SPEAKER in [f["line"] for f in read["recommended_facts_missing"]]
    assert reader.run(assistant="Knowing assistant")["current_instructions"] == KNOWS
    assert {a["assistant"] for a in reader.run()["assistants"]} == {"Plain assistant", "Knowing assistant"}
    assert "error" in reader.run(assistant="Nobody") and "error" in reader.run(session="nope")
    tools = {t.name for t in hypotheses.reviewer_tools(s, hypotheses.SubmitReview(s))}
    assert "read_assistant_instructions" in tools  # The reviewer reads them the same way.
    # A draft on the plain assistant applies to its other conversations: listed where it would apply.
    applies = s.where_it_applies_to(idea(say_who()))
    assert [(r["session"], r["touches"]) for r in applies["sessions"]] == [("plaintwo", "its assistant's instructions")]


@pytest.mark.asyncio
async def test_the_current_instructions_come_from_home_assistant(home):
    s = two_assistants()
    set_prompt(home, "plain", PLAIN + "Be kind.\n")
    await s.load_assistants(home.hass)  # Each conversation's assistant, as Home Assistant has it now.
    read = hypotheses.ReadAssistantInstructions(s).run(session="plainone")
    assert read["instructions_it_ran_with"] == PLAIN and read["current_instructions"] == PLAIN + "Be kind.\n"
    assert read["same_as_now"] is False
    # Its other settings now, and as the session recorded them with the tools Assist had.
    assert read["settings_now"] == {"chat_model": "fixture", "llm_hass_api": ["assist"]}
    assert read["as_recorded"]["options"]["chat_model"] == "fixture-no-model" and read["as_recorded"]["tools"]
    assert s.assistants()[f"{ENTRY}/plain"]["values"]["speaker"] == NAME
    gone = two_assistants()
    del home.hass.config_entries._entries[ENTRY]
    await gone.load_assistants(home.hass)
    assert "no_longer_set_up" in hypotheses.ReadAssistantInstructions(gone).run(assistant="Plain assistant")


# A prompt change, checked by code before testing.

def test_a_prompt_change_names_an_assistant_and_lines_that_are_there():
    s = two_assistants()
    check = lambda *changes, session="plainone": edit_problems(
        hypotheses.problems(s, {"hypotheses": [idea(*changes, session=session)]}))
    assert "name it in assistant" in check({"kind": "assistant", "text": "Be brief."})[0]
    assert "there's no assistant 'Nobody'" in check(say_who("Nobody"))[0]
    assert "isn't in Plain assistant's current instructions" in check(say_who(replaces=["Never said."]))[0]
    assert "an assistant change gives text or replaces" in check({"kind": "assistant", "assistant": "Plain assistant"})[0]
    assert "read for everyone who uses it" in check(say_who(**{"for": "this_user"}))[0]
    # A goal on another assistant can't gain from an edit of this one's instructions.
    found = check(say_who(), session="knowsone")
    assert any("was a conversation with Knowing assistant, whose instructions this doesn't change" in p
               for p in found), found
    # Removing a line is a prompt change too.
    assert check({"kind": "assistant", "assistant": "Plain assistant", "replaces": ["Answer in one short sentence."]}) == []


@pytest.mark.asyncio
async def test_a_prompt_change_must_render_and_is_kept_with_where_home_assistant_keeps_it(home):
    s = two_assistants()
    s.evidence = Evidence(s, recorded_today(s.household.recording))
    await s.load_assistants(home.hass)  # The speakers' names, as a run reads them.
    submit = hypotheses.SubmitHypotheses(s)

    async def send(*hypotheses_):
        result = await submit.async_call(home.hass, SimpleNamespace(tool_args={"hypotheses": list(hypotheses_)}),
                                         None)
        return result.data
    broken = await send(idea(say_who(text="{{ no_such_function() }}")))
    assert broken["accepted"] is False
    assert any("Home Assistant can't render Plain assistant's instructions with this change" in p
               for p in broken["problems"]), broken
    both = idea(say_who(), {"kind": "registry", "entity_id": "script.fixture_press", "expose": True})  # One idea fixes both.
    accepted = await send({**both, "goals": [*both["goals"], {"session": "knowsone", "goal_turn": 1}]})
    assert accepted["accepted"], accepted
    _, answer, _ = submit.best()
    assert answer["hypotheses"][0]["changes"][0]["target"] == f"{ENTRY}/plain"
    rendered = s.renders[(f"{ENTRY}/plain", PLAIN + SPEAKER + "\n")]
    assert rendered == {"rendered": PLAIN + f"The person speaking is {NAME}."}  # Stripped, as HA renders it.
    # A draft's checks show what Assist would read.
    checks = await s.evidence.check(idea(say_who()), home.hass)
    [fact] = checks["instructions"]
    assert fact["assistant"] == "Plain assistant" and fact["renders"] is True
    assert fact["assist_reads"].endswith(f"The person speaking is {NAME}.")


FIRST_NAME = "Address them as {{ user_name.split()[0] }}."  # Fails without a signed-in person.
QUIET = "for a request without a signed-in person or an asking device"


@pytest.mark.asyncio
async def test_a_line_that_renders_only_for_a_signed_in_person_is_a_problem_before_testing(home):
    """Home Assistant renders an assistant's instructions for every request, a voice satellite's (no signed-in person,
    no asking device) too, and a template that fails there fails the request: it's checked that way as well."""
    s = two_assistants()
    s.evidence = Evidence(s, recorded_today(s.household.recording))
    await s.load_assistants(home.hass)
    submit = hypotheses.SubmitHypotheses(s)
    sent = await submit.async_call(home.hass, SimpleNamespace(tool_args={"hypotheses": [idea(say_who(
        text=FIRST_NAME))]}), None)
    assert sent.data["accepted"] is False
    assert any(f"Home Assistant can't render Plain assistant's instructions with this change: {QUIET}: " in p
               for p in sent.data["problems"]), sent.data
    [fact] = (await s.evidence.check(idea(say_who(text=FIRST_NAME)), home.hass))["instructions"]
    assert fact["renders"] is False and fact["error"].startswith(QUIET)  # The reviewer sees it too.
    guarded = "{% if user_name %}" + FIRST_NAME + "{% endif %}"
    [fact] = (await s.evidence.check(idea(say_who(text=guarded)), home.hass))["instructions"]
    assert fact["renders"] is True and fact["assist_reads"].endswith(f"Address them as {NAME}.")
    # An assistant without a conversation of its own to seed a sandbox: checked in the household's, quietly.
    s.assistants()[f"{ENTRY}/plain"]["replayable"] = None
    assert (await s.render(home.hass, f"{ENTRY}/plain", PLAIN + FIRST_NAME))["error"].startswith(QUIET)
    assert await s.render(home.hass, f"{ENTRY}/plain", PLAIN + guarded) == {"renders_quietly": True}
    [fact] = (await s.evidence.check(idea(say_who()), home.hass))["instructions"]
    assert fact["renders"] is True and "assist_reads" not in fact and QUIET in fact["note"]


@pytest.mark.asyncio
async def test_a_target_without_an_assistant_is_a_problem_to_fix_never_a_failed_tool(home):
    """A model may send target (it's in the change's fields): only an assistant it names, or a target of one of
    theirs, is kept, and the change is answered with what to fix."""
    s = two_assistants()
    s.evidence = Evidence(s, recorded_today(s.household.recording))
    await s.load_assistants(home.hass)
    unknown = {"kind": "assistant", "target": "Plain assistant", "text": SPEAKER}
    known = {"kind": "assistant", "target": f"{ENTRY}/plain", "text": SPEAKER}
    assert s.with_targets([unknown]) == [{"kind": "assistant", "text": SPEAKER}]
    assert s.with_targets([known]) == [known]
    assert s.with_targets([{**known, "assistant": "Nobody"}]) == [{"kind": "assistant", "assistant": "Nobody",
                                                                  "text": SPEAKER}]
    submit, check = hypotheses.SubmitHypotheses(s), hypotheses.CheckHypothesis(s)
    for change in (unknown, known):
        sent = await submit.async_call(home.hass, SimpleNamespace(tool_args={"hypotheses": [idea(change)]}), None)
        assert sent.data["accepted"] is False
        assert any("name it in assistant" in p for p in sent.data["problems"]), sent.data
        checked = await check.async_call(home.hass, SimpleNamespace(tool_args=idea(change)), None)
        assert "sessions_where_it_would_apply" in checked.data


def test_a_conversation_whose_answering_assistant_wasnt_known_is_no_assistants():
    """Several assistants recorded for a turn (which one answered wasn't known): none is told it was theirs."""
    plain = on_assistant(make_recording(), "plainone-0001", "plain", "Plain assistant", PLAIN)
    either = on_assistant(make_recording(), "eitherone-001", "knows", "Knowing assistant", KNOWS)
    for turn in either["turns"]:
        turn["prepared"]["provider_config"] = (deepcopy(plain["turns"][0]["prepared"]["provider_config"])
                                               + turn["prepared"]["provider_config"])
    assert instructions.ran_with(either) is None
    s = sessions_over(plain, either)
    assert s.briefs["eitheron"]["assistant_target"] is None and s.assistant_of("eitheron") is None
    assert {r["assistant"]: r["conversations"] for r in s.assistant_rows()} == {"Plain assistant": 1}
    assert "error" in hypotheses.ReadAssistantInstructions(s).run(session="eitheron")


# Simulated only on that assistant's conversations.

def test_a_prompt_change_edits_only_the_instructions_of_conversations_with_its_assistant():
    record = make_recording()
    changes = [say_who(target=f"{ENTRY}/plain")]
    mine = setup_from_changes(changes, source=record["turns"][0]["source"], prompt=PLAIN, scripts={},
                              assistant=f"{ENTRY}/plain")
    assert mine["prompt"] == PLAIN + SPEAKER + "\n" and mine["prompt_changed"]
    other = setup_from_changes(changes, source=record["turns"][0]["source"], prompt=KNOWS, scripts={},
                               assistant=f"{ENTRY}/knows")
    assert other["prompt"] is None and not other["prompt_changed"] and "household" not in other
    replaced = [{"kind": "assistant", "target": f"{ENTRY}/plain", "text": "Answer in a word.",
                 "replaces": ["Answer in one short sentence."]}]
    assert setup_from_changes(replaced, source=record["turns"][0]["source"], prompt=PLAIN, scripts={},
                              assistant=f"{ENTRY}/plain")["prompt"] == PLAIN.replace("one short sentence", "a word")
    with pytest.raises(CaseError, match="aren't in the instructions this conversation's assistant has"):
        setup_from_changes(replaced, source=record["turns"][0]["source"], prompt=KNOWS.replace("one short", "a"),
                           scripts={}, assistant=f"{ENTRY}/plain")
    # With no instructions of its own, Home Assistant's default ones are what it edits.
    default = setup_from_changes(changes, source=record["turns"][0]["source"], prompt="", scripts={},
                                 assistant=f"{ENTRY}/plain")
    assert default["prompt"] == llm.DEFAULT_INSTRUCTIONS_PROMPT + SPEAKER + "\n"


def test_a_conversation_on_another_assistant_is_never_replayed_with_it():
    from custom_components.assist_improver.simulate import reaches
    plain = on_assistant(make_recording(), "plainone-0001", "plain", "Plain", PLAIN)
    knows = on_assistant(make_recording(), "knowsone-0001", "knows", "Knowing", KNOWS)
    hypothesis = idea(say_who(target=f"{ENTRY}/plain"))
    assert reaches(hypothesis, plain) and not reaches(hypothesis, knows)
    alias = {"kind": "registry", "entity_id": TARGET, "aliases": ["relay"]}
    assert reaches(idea(say_who(target=f"{ENTRY}/plain"), alias), knows)


# Applied to that assistant, with conflicts and undo.

def accepted_edit(*changes, tested=PLAIN):
    return {"id": "imp1", "title": "Say who's speaking", "changes": list(changes) or [say_who(target=f"{ENTRY}/plain")],
            "setup": {}, "tested": {"scripts": {}, "exposure": {}, "prompts": {f"{ENTRY}/plain": tested}},
            "provider": {"config_entry_id": ENTRY, "subentry_id": "plain", "title": "Plain assistant"}}


@pytest.mark.asyncio
async def test_a_prompt_change_applies_to_its_assistant_only_and_undoes(home):
    hass = home.hass
    steps, already, conflicts = await changeset.plan_steps(hass, accepted_edit())
    assert not conflicts and not already
    [step] = steps
    assert (step["kind"], step["target"], step["what"]) == ("prompt", f"{ENTRY}/plain", "Plain assistant's instructions")
    assert step["before"] == PLAIN and step["after"] == PLAIN + SPEAKER + "\n"
    shown = panel.review({**accepted_edit(), "test": tested(), "cause": "", "status": "awaiting_review"},
                         (steps, already, conflicts))
    [change] = shown["changes"]
    assert change["name"] == "Plain assistant's instructions" and change["kind"] == "instructions"
    assert [r["text"] for r in change["rows"] if r["sign"] == "+"] == [SPEAKER] and change["removed"] == 0
    result = await changeset.Changesets(hass).run("imp1", "apply", steps)
    assert result["status"] == "done"
    assert home.provider.subentries["plain"].data["prompt"] == PLAIN + SPEAKER + "\n"
    assert home.provider.subentries["knows"].data["prompt"] == KNOWS  # The other assistant is untouched.
    live = {(s["kind"], s["target"]): await changeset.read(hass, s["kind"], s["target"]) for s in result["steps"]}
    undo, left = changeset.undo_steps(result["steps"], live)
    assert not left and (await changeset.Changesets(hass).run("imp1", "undo", undo))["status"] == "done"
    assert home.provider.subentries["plain"].data["prompt"] == PLAIN
    # Accepted again once it's there: nothing to change.
    set_prompt(home, "plain", PLAIN + SPEAKER + "\n")
    steps, already, conflicts = await changeset.plan_steps(hass, accepted_edit())
    assert not steps and not conflicts and already[0]["what"] == "Plain assistant's instructions"


@pytest.mark.asyncio
async def test_a_prompt_change_on_instructions_changed_since_its_test_is_a_conflict(home):
    hass = home.hass
    set_prompt(home, "plain", PLAIN + "Be kind.\n")
    _, _, conflicts = await changeset.plan_steps(hass, accepted_edit())
    assert [(c["what"], c["why"]) for c in conflicts] == [
        ("Plain assistant's instructions", "It was changed in Home Assistant after the improvement was tested.")]
    replacing = say_who(target=f"{ENTRY}/plain", replaces=["Answer in one short sentence."])
    set_prompt(home, "plain", "Be kind.\n")
    _, _, conflicts = await changeset.plan_steps(hass, accepted_edit(replacing))
    assert conflicts[0]["why"] == "It was changed in Home Assistant after the improvement was tested."
    del hass.config_entries._entries[ENTRY]
    _, _, conflicts = await changeset.plan_steps(hass, accepted_edit())
    assert conflicts[0]["why"] == "The assistant it was tested on is no longer set up."


def test_a_prompt_change_reads_as_what_it_does_to_its_assistants_instructions():
    assert panel._change_text(say_who(), "Assist") == f"Add to Plain assistant's instructions: “{SPEAKER}”"
    assert panel._change_text(say_who(replaces=["Old."]), "Assist") == \
        f"Add to Plain assistant's instructions: “{SPEAKER}”, replacing “Old.”"
    assert panel._change_text({"kind": "assistant", "assistant": "Plain assistant", "replaces": ["Old."]}, "Assist") \
        == "Remove from Plain assistant's instructions: “Old.”"


def test_instruction_edits_replace_in_place_remove_whole_lines_and_never_add_twice():
    text, problems = instructions.edit(PLAIN, [{"text": "Answer in a word.", "replaces": ["Answer in one short sentence."]}])
    assert text == "You are a voice assistant for Home Assistant.\nAnswer in a word.\n" and not problems
    text, _ = instructions.edit(PLAIN, [{"replaces": ["Answer in one short sentence."]}])
    assert text == "You are a voice assistant for Home Assistant.\n"
    assert instructions.edit(PLAIN + SPEAKER + "\n", [{"text": SPEAKER}])[0] == PLAIN + SPEAKER + "\n"
    _, problems = instructions.edit(PLAIN, [{"text": "New.", "replaces": ["Gone."]}])
    assert problems == ["The instruction line it replaces has changed since it was tested: 'Gone.'"]


# End to end: a conversation on an assistant whose instructions don't say who's speaking.

FIRST = "Toggle the fixture relay."


def one_turn():
    """The household's assistant, once it knows who's speaking: off then on, in one turn."""
    args = {"name": "Fixture relay", "domain": ["switch"]}
    return [[{"type": "tool_use", "id": "off", "name": "intent__HassTurnOff", "input": args},
             {"type": "tool_use", "id": "on", "name": "intent__HassTurnOn", "input": args}],
            [{"type": "text", "text": "Done: pressed."}]]


def knowing_the_speaker(record):
    """The conversation replayed with the edit: the press in one turn, then the later press (turn 4) as it went.
    Turns 2 and 3 finished the first press by hand, so they aren't said (DETAILS P6.4)."""
    return one_turn() + before(record)[-2:]


@pytest_asyncio.fixture
async def household(hass, effect_guards, monkeypatch):
    """The integration installed with two assistants; the AI roles and the household's assistant scripted."""
    from homeassistant.core import ServiceRegistry
    from homeassistant import config as conf_util
    from homeassistant.setup import async_setup_component
    from test_changeset import _original_async_call
    from kit import Model, variant
    from test_milestone1 import fake_entry
    from custom_components.assist_improver import async_setup_entry, async_unload_entry
    from custom_components.assist_improver.const import CONF_AI_TASK, CONF_TRIAGE_AI_TASK
    guarded, original = ServiceRegistry.async_call, _original_async_call()

    async def allow_script_reload(registry, domain, service, *args, **kwargs):
        if (domain, service) == ("script", "reload"):
            return await original(registry, domain, service, *args, **kwargs)
        return await guarded(registry, domain, service, *args, **kwargs)
    monkeypatch.setattr(ServiceRegistry, "async_call", allow_script_reload)
    await assist_home(hass, REPLAY_ENTITIES)
    hass.auth = SimpleNamespace(async_get_user=AsyncMock(return_value=household_user(NAME)))
    open(hass.config.path("configuration.yaml"), "w").write("script: !include scripts.yaml\n")
    open(hass.config.path("scripts.yaml"), "w").write("{}\n")
    assert await async_setup_component(hass, "script", await conf_util.async_hass_config_yaml(hass))

    async def no_platforms(*_args):
        return True
    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", no_platforms)
    monkeypatch.setattr(hass.config_entries, "async_unload_platforms", no_platforms)
    for entity_id in ENTITIES:
        hass.states.async_set(entity_id, "unknown")
    entry = provider()
    hass.config_entries._entries[entry.entry_id] = entry

    class Roles(Model):
        """The proposer reads the friction conversation's assistant's instructions and adds the missing fact."""
        reviewed, checks, read = [], [], None

        async def hypothesis_generator(self, hass, payload, tools):
            self.calls.append(("proposer", None))
            self.proposer_input = payload
            [scenario] = payload["friction_scenarios"]
            self.read = await self.use(hass, tools["read_assistant_instructions"], {"session": scenario["session"]})
            [speaker] = [f for f in self.read["recommended_facts_missing"] if f["fact"] == "speaker"]
            hypothesis = idea({"kind": "assistant", "assistant": scenario["assistant"], "text": speaker["line"]},
                              session=scenario["session"])
            review = await self.use(hass, tools["ask_reviewer"], hypothesis)
            self.drafts.append(review["outcome"])
            self.checks.append(review["checks"])
            self.tests.append(await self.use(hass, tools["test_idea"], hypothesis))  # Simulate's stamp too (P0.4).
            answer = {"hypotheses": [hypothesis]} if self.tests[-1].get("validated") else {
                "hypotheses": [], "not_fixed": [{**hypothesis["goals"][0], "why": "No idea for it was validated."}]}
            accepted = await self.use(hass, tools["submit_hypotheses"], answer)
            assert accepted["accepted"], accepted
            return "Submitted."

        async def hypothesis_reviewer(self, hass, payload, tools):
            self.reviewer_input = payload
            while "draft" in (got := await self.use(hass, tools["wait_for_draft"])):
                self.reviewed.append(got["draft"])
                comment = {"label": "praise", "decorations": ["non-blocking"], "subject": "Says who's speaking"}
                assert (await self.use(hass, tools["submit_review"], {"comments": [comment]}))["accepted"]
            return "Finished."

    model = Roles()
    slips = serve(hass, model)  # The AI roles answer through Home Assistant's real AI Task path.
    friction = on_assistant(variant(prepared_recording(), "friction01", FIRST), "friction01", "plain",
                            "Plain assistant", PLAIN, options=PROFILE)
    other = on_assistant(variant(make_recording(), "otherone01", "Switch the fixture relay off.", turns=2),
                         "otherone01", "knows", "Knowing assistant", KNOWS)
    assistant, client = await bind_agent(hass, friction)
    improver = fake_entry({CONF_AI_TASK: "ai_task.opus", CONF_TRIAGE_AI_TASK: "ai_task.haiku"})
    await async_setup_entry(hass, improver)
    runtime = improver.runtime_data
    for record in (friction, other):
        await runtime.recorder.store(record["id"]).async_save(as_kept(hass, record))
        runtime.recorder.index[record["id"]] = {"status": record["status"], "user_turns": len(record["turns"])}
    try:
        yield SimpleNamespace(hass=hass, runtime=runtime, model=model, client=client, friction=friction,
                              provider=entry)
    finally:
        await async_unload_entry(hass, improver)
    assert slips == [], f"The scripted roles called tools off their schemas: {slips}"


@pytest.mark.asyncio
async def test_a_missing_fact_in_one_assistants_instructions_is_proposed_tested_written_up_and_accepted(household):
    hass, runtime, model, client = household.hass, household.runtime, household.model, household.client
    client.blocks = before(household.friction) + knowing_the_speaker(household.friction)  # Without, then with it.
    pipeline = runtime.pipeline
    pipeline.start(trigger="manual")
    await pipeline._task
    run = pipeline.current
    assert run["status"] == "done", run["error"]
    assert model.slips == [] and not client.blocks  # The other assistant's conversation was never replayed.
    # Propose: the conversation's assistant, its instructions as it ran with them and now, and the missing fact.
    assert model.proposer_input["friction_scenarios"][0]["assistant"] == "Plain assistant"
    assert {r["assistant"] for r in model.proposer_input["assistants"]} == {"Plain assistant", "Knowing assistant"}
    assert model.read["instructions_it_ran_with"] == PLAIN and model.read["current_instructions"] == PLAIN
    assert {r["assistant"] for r in model.reviewer_input["household"]["assistants"]} == {
        "Plain assistant", "Knowing assistant"}
    # Review: the draft came with what Assist would read once it's edited, rendered by Home Assistant.
    [[edited]] = [c["instructions"] for c in model.checks]
    assert edited["renders"] and edited["assist_reads"].endswith(f"The person speaking is {NAME}.")
    assert model.drafts == ["approve"] and model.reviewed[0]["changes"][0]["assistant"] == "Plain assistant"
    # Simulate: the conversation replayed without the change (its 4 turns, 7 requests), then with the edited
    # instructions: the model read who's speaking, and pressed in one turn instead of three.
    speaker = f"The person speaking is {NAME}."
    assert [speaker in json.dumps(r) for r in client.requests] == [False] * 7 + [True] * 4
    improvements = runtime.improvements
    [item] = improvements.pending()
    assert item["test"]["validated"] and item["test"]["after"]["turns_per_root_goal"] == {"1:0": 1, "4:3": 1}
    assert item["copy"]["headline"] and item["tested"]["prompts"] == {f"{ENTRY}/plain": PLAIN}
    # What changes: the plain assistant's instructions, as a diff.
    from custom_components.assist_improver.changeset import plan_steps
    shown = panel.review(item, await plan_steps(hass, item))
    assert [(c["name"], c["kind"]) for c in shown["changes"]] == [("Plain assistant's instructions", "instructions")]
    # Accepted: only that assistant's instructions change; undone, they're back.
    accepted = await improvements.decide(item["id"], "accept", user_id="tester")
    assert accepted["status"] == "applied", accepted["outcome"]
    assert household.provider.subentries["plain"].data["prompt"] == PLAIN + SPEAKER + "\n"
    assert household.provider.subentries["knows"].data["prompt"] == KNOWS
    await improvements.decide(item["id"], "undo", user_id="tester")
    assert household.provider.subentries["plain"].data["prompt"] == PLAIN
    assert household.provider.subentries["knows"].data["prompt"] == KNOWS


@pytest.mark.asyncio
async def test_a_prompt_change_is_reviewed_tested_and_recorded_as_the_same_changes(household):
    """P0.4 with P0.2: a prompt change goes through ask_reviewer, test_idea and submit_hypotheses as one set of changes,
    each kept with where Home Assistant keeps the instructions it edits (target). The kept review, the test and the
    improvement share one digest, and the Propose record gives the idea both stamps."""
    runtime, client = household.runtime, household.client
    client.blocks = before(household.friction) + knowing_the_speaker(household.friction)
    pipeline = runtime.pipeline
    pipeline.start(trigger="manual")
    await pipeline._task
    assert pipeline.current["status"] == "done", pipeline.current["error"]
    flow, rid = runtime.flow, household.friction["id"]
    conv = flow.record(rid)
    record = conv["rounds"][str(conv["round"])]["records"]["Propose"]
    [test] = flow.tests(rid)
    [item] = runtime.improvements.items.values()
    [review] = record["reviews"]
    assert all(change.get("target") for change in review["hypothesis"]["changes"])
    assert digest(review["hypothesis"]["changes"]) == test["changes"] == digest(item["changes"])
    [idea] = [i for i in record["ideas"] if digest(i["hypothesis"]["changes"]) == test["changes"]]
    assert idea["stamps"] == {"Review": True, "Simulate": True} and idea["sent_on"]


async def said(hass, handler, **msg):
    """A panel command, as the household's admin sends it: ("ok", result) or ("error", code, text)."""
    from test_changeset import _ws
    sent = []
    connection = SimpleNamespace(user=SimpleNamespace(id="tester", is_admin=True),
                                 send_result=lambda i, r: sent.append(("ok", r)),
                                 send_error=lambda i, code, text: sent.append(("error", code, text)))
    await _ws(handler, hass, connection, {"id": 1, **msg})
    return sent[-1]


async def conflicted(household):
    """A first run puts the edit up for review; Alex changes the assistant's instructions after its test, so accepting
    it meets a conflict, which says so and asks before the conversation goes back in (P0.7). The improvement."""
    runtime, client = household.runtime, household.client
    client.blocks = before(household.friction) + knowing_the_speaker(household.friction)
    runtime.pipeline.start(trigger="manual")
    await runtime.pipeline._task
    assert runtime.pipeline.current["status"] == "done", runtime.pipeline.current["error"]
    [item] = runtime.improvements.pending()
    set_prompt(household, "plain", PLAIN + "Be kind.\n")  # Alex's own edit, after the test.
    status, decided = await said(household.hass, panel.ws_decide, improvement_id=item["id"], decision="accept",
                                    note="")
    assert status == "ok" and decided["status"] == "conflict" and not decided["ok"]
    assert decided["asks"]["about"] == "conflict" and decided["asks"]["improvement_id"] == item["id"]
    assert household.provider.subentries["plain"].data["prompt"] == PLAIN + "Be kind.\n"  # Nothing overwritten.
    return item


async def agreed(household, item):
    """The household agrees to resolve the conflict: its conversation goes straight back in at Propose, on a run of
    its own (P0.7). That run."""
    pipeline = household.runtime.pipeline
    assert await said(household.hass, panel.ws_conflict_resolve, improvement_id=item["id"]) == (
        "ok", {"sent_back": True, "result": "Thanks. The improver is revising it now."})
    await pipeline._task
    run = pipeline.current
    assert run["trigger"] == "conflict" and run["status"] == "done", run["error"]
    assert run["moved"] == ["friction01"]
    return run


@pytest.mark.asyncio
async def test_a_conflicted_edit_shown_passing_on_the_instructions_as_they_are_now_goes_back_up_for_review(household):
    """DETAILS P12.6: an accept that met a conflict says so and asks; agreed, its conversation goes back in at Propose,
    and a fix shown passing again goes back up for review. Alex changed the assistant's instructions after the edit
    was tested, so accepting it conflicts; the re-proposal words the same edit anew, and its test passes on the
    instructions as they are now: his edit changed the setup the replays run on, so both run again on it (DETAILS
    P6.7.1). Nothing stands in its way any more, judged against what it was tested on now: it's up for review again."""
    runtime, model, client = household.runtime, household.model, household.client
    item = await conflicted(household)
    client.blocks = before(household.friction) + knowing_the_speaker(household.friction)

    async def worded_anew(hass, tool, args=None):  # The same edit, a new idea (so a new test, on the home as it is):
        if tool.name in ("ask_reviewer", "test_idea", "submit_hypotheses"):  # each idea it asks about, tests and
            for hypothesis in args["hypotheses"] if "hypotheses" in args else [args]:  # sends on, under its new title.
                hypothesis["title"] = "Tell Assist who is speaking"
        return await use(hass, tool, args)
    model.use = worded_anew
    asked = len(client.requests)
    model.calls.clear()
    await agreed(household, item)
    assert [role for role, _ in model.calls if role in ("triage", "analyzer", "proposer")] == ["proposer"]
    assert not client.blocks and all("Be kind." in r["system"][0]["text"] for r in client.requests[asked:])
    [now] = runtime.improvements.pending()  # The re-proposal, up for review.
    assert now["title"] == "Tell Assist who is speaking" and now["status"] == "awaiting_review"
    assert now["tested"]["prompts"] == {f"{ENTRY}/plain": PLAIN + "Be kind.\n"}
    assert runtime.flow.place("friction01")["stage"] == "Your review"


@pytest.mark.asyncio
async def test_the_same_edit_proposed_again_after_its_conflict_is_up_for_review_once_and_then_applies(household):
    """DETAILS P12.6.5: agreed after the conflict, the re-proposal proposes the very same edit; its test runs again on
    the instructions as they are now (Alex's own line in them: a setup of its own, DETAILS P6.7.1), and the fix shown
    passing again goes back up for review, once: no run after that pays for it again, and accepting it now adds its
    line to his."""
    hass, runtime, model = household.hass, household.runtime, household.model
    item = await conflicted(household)
    household.client.blocks = before(household.friction) + knowing_the_speaker(household.friction)
    model.calls.clear()
    await agreed(household, item)
    assert model.calls.count(("proposer", None)) == 1  # The conflict went back to the proposer, once.
    [now] = runtime.improvements.pending()
    assert now["status"] == "awaiting_review" and now["tested"]["prompts"] == {f"{ENTRY}/plain": PLAIN + "Be kind.\n"}
    assert now["revises"] == {"id": item["id"], "title": item["title"], "note": "", "conflict": True}  # Not a note.
    model.calls.clear()
    runtime.pipeline.start(trigger="manual")
    await runtime.pipeline._task
    assert ("proposer", None) not in model.calls
    status, decided = await said(hass, panel.ws_decide, improvement_id=now["id"], decision="accept", note="")
    assert status == "ok" and decided["status"] == "applied", decided
    assert household.provider.subentries["plain"].data["prompt"] == PLAIN + "Be kind.\n" + SPEAKER + "\n"
    assert runtime.flow.place("friction01")["outcome"] == "improved"


@pytest.mark.asyncio
async def test_a_review_whose_accept_met_a_conflict_says_why_and_asks_to_send_it_back_until_it_is_sent_back(household):
    """P0.7 with DETAILS P12.6.1: the review of an improvement whose accept met a conflict says why it can't apply
    now (the conflicts its accept met) and offers one choice: send it back to resolve them. Sent back, its
    conversation is in at Propose already: nothing is left to ask."""
    hass, runtime = household.hass, household.runtime
    item = await conflicted(household)
    met = runtime.improvements.get(item["id"])["conflicts"]
    status, review = await said(hass, panel.ws_get, improvement_id=item["id"])
    assert status == "ok" and review["status"] == "conflict" and met
    assert review["conflicts"] == met
    assert review["choices"] == [{"value": "resolve", "label": "Send it back to resolve"}]
    assert await said(hass, panel.ws_conflict_resolve, improvement_id=item["id"]) == (
        "ok", {"sent_back": True, "result": "Thanks. The improver is revising it now."})
    status, review = await said(hass, panel.ws_get, improvement_id=item["id"])
    assert runtime.flow.place("friction01")["stage"] != "Your review"
    assert review["status"] == "conflict" and review["conflicts"] == met and review["choices"] == []
    await runtime.pipeline._task


def test_an_edit_that_only_removes_a_line_from_the_default_instructions_edits_them():
    first = llm.DEFAULT_INSTRUCTIONS_PROMPT.splitlines()[0]
    text, problems = instructions.edit("", [{"kind": "assistant", "assistant": "Plain", "replaces": [first]}])
    assert problems == [] and first not in text and text.strip()
