"""People want Assist brief: the proposer is told to add speech only where Assist would misreport, the reviewer gets the
speech a change adds listed by code, and a test whose replay made Assist noticeably wordier is flagged (review page,
write-up)."""

import pytest

from test_changeset import tested
from test_evidence import TODAY, PRESS, hypothesis, sessions
from custom_components.assist_improver import editor, panel
from custom_components.assist_improver.evidence import Evidence
from custom_components.assist_improver.hypotheses import prompts
from custom_components.assist_improver.hypotheses.checks import speech
from custom_components.assist_improver.improvements import reply_words, wordier


def test_the_prompts_ask_for_brief_replies_and_keep_home_assistants_guidance_as_it_is():
    primer = prompts.HA_PRIMER
    assert "what to tell the user afterwards" in primer  # Home Assistant's own words, still quoted as theirs.
    assert primer.index("What Assist says (the household's side") > primer.index("Home Assistant's own published")
    assert "never adds caveats or disclaimers the household didn't ask for" in primer
    assert "stating them firmly" not in prompts.GENERATOR_INSTRUCTIONS
    assert "no caveats about what it can't sense" in prompts.GENERATOR_INSTRUCTIONS
    assert "Weigh what the change would have Assist say" in prompts.REVIEWER_INSTRUCTIONS
    assert primer in prompts.GENERATOR_INSTRUCTIONS and primer in prompts.REVIEWER_INSTRUCTIONS


def test_the_speech_a_change_adds_is_listed_by_code_with_its_words():
    garage = {"kind": "script", "id": "garage_press", "config": {"alias": "Garage press", "description": (
        "Presses the far garage door. After running, say the door was pressed and that its position can't be read."),
        "sequence": [{"action": "switch.turn_off"}, {"if": [], "then": [
            {"stop": "Pressed. The door should be moving; its position can't be read."}]}]}}
    trimmed = {"kind": "preference", "for": "everyone", "text": "The garage doors have no position sensor.",
               "replaces": ["When a garage door is pressed, tell the user its position can't be read."]}
    assert speech({"changes": [garage, trimmed]}) == [
        {"change": "garage_press", "kind": "stop message",
         "text": "Pressed. The door should be moving; its position can't be read.", "words": 11},
        {"change": "garage_press", "kind": "tells Assist what to say",
         "text": "After running, say the door was pressed and that its position can't be read.", "words": 14},
        {"change": "preference", "kind": "removes",
         "text": "When a garage door is pressed, tell the user its position can't be read.", "words": 14}]
    assert speech({"changes": [{"kind": "registry", "entity_id": "light.front", "aliases": ["Lamp"]}]}) == []


def test_the_households_own_words_and_self_checks_arent_talk():
    lines = ["Only touch the Den TV when the user says den.",
             "When the user says 'my TV' without a room, they mean the Lounge TV.",
             "If they don't say which fan, use the one that is running.",
             "If the user doesn't say which door, ask once.",
             "Check the device state to confirm that the change took effect before replying."]
    assert speech({"changes": [{"kind": "preference", "for": "everyone", "text": " ".join(lines)}]}) == []


def test_an_edit_that_trims_a_scripts_wordy_speech_shows_what_it_removes():
    scripts = {"garage_press": {"alias": "Garage press", "description": (
        "Presses the garage door. Tell the user the door was pressed and that its position can't be read."),
        "sequence": [{"action": "switch.turn_on"}, {"stop": "Pressed. Its position can't be read."}]}}
    edit = {"kind": "script", "id": "garage_press", "config": {
        "alias": "Garage press", "description": "Presses the garage door.",
        "sequence": [{"action": "switch.turn_on"}, {"stop": "Pressed."}]}}
    assert speech({"changes": [edit]}, scripts) == [
        {"change": "garage_press", "kind": "stop message", "text": "Pressed.", "words": 1},
        {"change": "garage_press", "kind": "removes", "text": "Pressed. Its position can't be read.", "words": 6},
        {"change": "garage_press", "kind": "removes",
         "text": "Tell the user the door was pressed and that its position can't be read.", "words": 14}]
    unchanged = {**edit, "config": {**scripts["garage_press"], "description": "Presses the garage door, once."}}
    assert [t["kind"] for t in speech({"changes": [unchanged]}, scripts)] == ["removes"]  # Its steps stay as they are.


@pytest.mark.asyncio
async def test_a_drafts_checks_carry_the_speech_it_adds_for_the_reviewer(hass, effect_guards):
    found, _ = sessions()
    checks = await Evidence(found, TODAY).check(hypothesis(PRESS))
    assert checks["speech"] == [{"change": "press_the_relay", "kind": "stop message", "text": "Pressed.", "words": 1}]
    quiet = await Evidence(found, TODAY).check(hypothesis({"kind": "registry", "entity_id": PRESS["config"][
        "sequence"][0]["target"]["entity_id"], "aliases": ["Garage door"]}))
    assert "speech" not in quiet and effect_guards == []


def said(*replies):
    return [{"you": "…", "assistant": reply} for reply in replies]


TERSE = said("Which porch light?", "Done.")  # 2 words a reply, on average.
CHATTY = said("Done. I turned on the front porch light for you; it should be on now, though I can't see it from here.")


def test_replies_are_counted_a_reply_on_average_and_flagged_when_noticeably_wordier():
    assert reply_words({"before": TERSE, "after": CHATTY}) == {"before": 2, "after": 22, "wordier": True}
    assert reply_words({"before": TERSE, "after": said("Done: the front porch light is on.")}) == {
        "before": 2, "after": 7, "wordier": False}  # Longer, but not by 10 words.
    long = said(" ".join(["word"] * 40))
    assert not wordier({"before": long, "after": said(" ".join(["word"] * 55))})  # 15 more, but not 1.5 times.
    assert reply_words({"before": [], "after": CHATTY}) is None  # Its recording is gone: nothing to compare.


def test_a_wordier_improvements_write_up_is_told_and_its_review_says_so():
    line = {"kind": "preference", "for": "everyone", "text": "The porch light is the front one."}
    def test(*after):
        return tested("Turn on the porch light", said=[t["you"] for t in TERSE],
                      replies=[t["assistant"] for t in TERSE], after=after)
    item = {"id": "imp1", "title": "Porch light", "cause": "", "changes": [line], "test": test(CHATTY[0]["assistant"]),
            "provider": {"title": "Kitchen"}, "status": "awaiting_review", "decisions": []}
    assert editor.payload(item)["tested"][0]["assist_says_more"] is True
    assert "assist_says_more" in editor.INSTRUCTIONS
    shown = panel.review(item, ([], [], []))
    assert shown["tests"][0]["more_words"] == {"before": 2, "after": 22} and shown["wordier"] == 1
    calm = panel.review({**item, "test": test("Done.")}, ([], [], []))
    assert "more_words" not in calm["tests"][0] and calm["wordier"] == 0
