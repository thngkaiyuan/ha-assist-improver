"""The bug hunt's findings on review and apply, fixed: write-up, decisions, applying and undoing.

Each test names the behaviour the product promises; it failed while its bug was there.
"""

import pytest

from test_changeset import home  # noqa: F401  (a fixture)
from test_spec_improvements import alias_item, home_now, line_item, reviewing, target
from custom_components.assist_improver import changeset


# Undo (DESIGN.md: "changes apply all or nothing and can be undone"; the review: "one click to undo").

@pytest.mark.asyncio
async def test_undoing_an_earlier_household_line_takes_out_only_its_line_after_a_later_one_was_accepted(home):
    # Household lines from every improvement share one value (the household knowledge). Undo "puts back what was there,
    # leaves a value the user changed since" (DETAILS P12.7.1): another improvement's line isn't the user changing it.
    quiet = "Quiet hours start at 22:00."
    breakfast = "Breakfast is at 7."
    improvements = await reviewing(home, line_item(home, "a", text=quiet), line_item(home, "b", text=breakfast))
    assert (await improvements.decide("a", "accept", user_id="alex"))["status"] == "applied"
    assert (await improvements.decide("b", "accept", user_id="alex"))["status"] == "applied"
    undone = await improvements.decide("a", "undo", user_id="alex")
    knowledge = (await home_now(home))["household"]
    assert quiet not in knowledge["everyone"], (undone["status"], undone["outcome"], knowledge)
    assert knowledge["everyone"] == [breakfast]  # The later improvement's line stays.
    assert undone["outcome"].get("left") in (None, [])  # Nothing the household changed was left behind.


@pytest.mark.parametrize("kind, before, after, later, back", [
    # A device's names: the earlier improvement's name goes, and the one it replaced comes back; the later one stays.
    ("alias", ["desk lamp"], ["front light"], ["front light", "lamp"], ["desk lamp", "lamp"]),
    # An assistant's instructions: the earlier improvement's line goes back to the one it replaced, in place.
    ("prompt", "You are Assist.\nShort answers.\nBe kind.\n", "You are Assist.\nDetailed answers.\nBe kind.\n",
     "You are Assist.\nDetailed answers.\nBe kind.\nRiley likes jazz.\n",
     "You are Assist.\nShort answers.\nBe kind.\nRiley likes jazz.\n")])
def test_undoing_an_earlier_name_or_instruction_takes_out_only_its_own_after_a_later_one(kind, before, after, later,
                                                                                         back):
    step = {"kind": kind, "target": "t", "what": "It", "before": before, "after": after}
    steps, left = changeset.undo_steps([step], {(kind, "t"): later})
    assert steps == [{**step, "before": later, "after": back}] and not left
    # Its own part changed since (the household edited it): left as it is, and said so.
    changed = [n.replace("front light", "my light") for n in later] if kind == "alias" else later.replace("Detailed",
                                                                                                         "Long")
    assert changeset.undo_steps([step], {(kind, "t"): changed}) == (
        [], [{"what": "It", "why": "It was changed again after the improvement was applied."}])


@pytest.mark.asyncio
async def test_an_undo_home_assistant_stopped_midway_says_the_improvement_is_still_in_place(home):
    from test_spec_improvements import Crash, crashing_after, improvement, notifications, saves
    from custom_components.assist_improver.changeset import Changesets
    from custom_components.assist_improver.improvements import OUTCOME_NOTIFICATION, Improvements
    improvements = await reviewing(home, improvement(home))
    assert (await improvements.decide("imp1", "accept", user_id="alex", editors=saves(home)))["status"] == "applied"
    applied = await home_now(home)
    writer = crashing_after(2)
    improvements.changesets = Changesets(home.hass, writer=writer)
    with pytest.raises(Crash):  # Home Assistant stops while the undo is half done.
        await improvements.decide("imp1", "undo", user_id="alex", editors=writer.editor(home.hass).save)
    restarted = Improvements(home.hass, None)
    await restarted.start()
    await restarted.recover()
    # Rolling the half-done undo back puts the improvement fully back in place, and it stays applied ...
    assert await home_now(home) == applied and restarted.get("imp1")["status"] == "applied"
    # ... so the household mustn't be told that nothing of it is in place (a misleading notice).
    message = notifications(home.hass)[OUTCOME_NOTIFICATION]["message"]
    assert "nothing of it is in place" not in message, message


# Found in review of the fixes above.

@pytest.mark.asyncio
async def test_undo_keeps_a_line_or_name_another_applied_improvement_adds_too_and_says_so(home):
    from custom_components.assist_improver import panel
    quiet, music = "Quiet hours start at 22:00.", "No music in quiet hours."
    # B was tested on a conversation from before A, so it brings A's line too; accepted after A, it adds only its own.
    later = {**line_item(home, "b", copy={"headline": "No music in quiet hours"}),
             "changes": [{"kind": "preference", "for": "everyone", "text": quiet},
                         {"kind": "preference", "for": "everyone", "text": music}]}
    improvements = await reviewing(home, line_item(home, "a", text=quiet), later,
                                   alias_item(home, "n1"), alias_item(home, "n2", copy={"headline": "Name it"}))
    for improvement_id in ("a", "b", "n1", "n2"):  # n2 gives the same name n1 did: already there.
        assert (await improvements.decide(improvement_id, "accept", user_id="alex"))["status"] == "applied"
    undone = await improvements.decide("a", "undo", user_id="alex")
    assert (await home_now(home))["household"]["everyone"] == [quiet, music]  # B still has all its lines ...
    assert undone["status"] == "awaiting_review" and improvements.get("b")["status"] == "applied"
    assert panel.outcome_text(undone) == ("Undone: what was there before is back. Kept, as another improvement uses "
                                          "it: “Quiet hours start at 22:00.” (No music in quiet hours)")
    undone = await improvements.decide("n1", "undo", user_id="alex")
    assert "front light" in await changeset.read(home.hass, "alias", "light.front")  # ... and n2 its name.
    assert [(left["kept"], left["used_by"]) for left in undone["outcome"]["left"]] == [("front light", "Name it")]


@pytest.mark.asyncio
async def test_a_line_or_name_kept_for_another_improvement_goes_once_that_one_is_undone_too(home):
    from custom_components.assist_improver import panel
    quiet, music = "Quiet hours start at 22:00.", "No music in quiet hours."
    later = {**line_item(home, "b", copy={"headline": "No music in quiet hours"}),
             "changes": [{"kind": "preference", "for": "everyone", "text": quiet},
                         {"kind": "preference", "for": "everyone", "text": music}]}
    improvements = await reviewing(home, line_item(home, "a", text=quiet), later,
                                   alias_item(home, "n1"), alias_item(home, "n2", copy={"headline": "Name it"}))
    before = await home_now(home)
    for improvement_id in ("a", "b", "n1", "n2"):
        assert (await improvements.decide(improvement_id, "accept", user_id="alex"))["status"] == "applied"
    await improvements.decide("a", "undo", user_id="alex")  # A's line is kept for B ...
    await improvements.decide("n1", "undo", user_id="alex")  # ... and n1's name for n2.
    # ... so it's theirs now: undoing them takes it out, and nothing of the four is left.
    for improvement_id in ("b", "n2"):
        undone = await improvements.decide(improvement_id, "undo", user_id="alex")
        assert panel.outcome_text(undone) == "Undone: what was there before is back."
    assert (await home_now(home))["household"] == before["household"]
    assert "front light" not in (await changeset.read(home.hass, "alias", "light.front") or [])


def block(user, text):
    """An assistant's own instructions for one person: a template block, readable only whole."""
    return f"{{% if llm_context.context and llm_context.context.user_id == '{user}' %}}\n{text}\n{{% endif %}}"


@pytest.mark.asyncio
async def test_undo_never_leaves_part_of_a_template_block_in_an_assistants_instructions(home):
    from test_changeset import PROMPT
    from test_spec_improvements import improvement

    def edit(id_, text):
        return improvement(home, id_, title=id_, changes=[
            {"kind": "assistant", "target": target(home), "assistant": "Assistant", "text": text}],
            setup={"scripts": {}, "exposure": {}, "aliases": {}}, tested={"scripts": {}, "exposure": {}})
    jordan, alex = block("jordan", "Jordan likes rock."), block("alex", "Alex likes jazz.")
    improvements = await reviewing(home, edit("a", jordan), edit("b", alex))
    for improvement_id in ("a", "b"):
        assert (await improvements.decide(improvement_id, "accept", user_id="alex"))["status"] == "applied"
    undone = await improvements.decide("a", "undo", user_id="alex")
    # B's text has a {% endif %} line too: A's goes all the same, with the rest of A's block.
    assert await changeset.read(home.hass, "prompt", target(home)) == PROMPT + alex + "\n"
    assert not undone["outcome"]["left"]
    await improvements.decide("b", "undo", user_id="alex")
    assert await changeset.read(home.hass, "prompt", target(home)) == PROMPT


@pytest.mark.asyncio
async def test_an_undo_that_would_leave_instructions_home_assistant_cant_read_leaves_them_as_they_are(home):
    from custom_components.assist_improver import panel
    # An undo of instructions is checked as applying is: one that would break the template isn't written.
    step = {"kind": "prompt", "target": target(home), "what": "Assistant's instructions", "before": "Be kind.",
            "after": block("jordan", "Jordan likes rock.").rsplit("\n", 1)[0]}  # Its {% endif %} gone.
    kept = {"what": "Assistant's instructions", "kept": "Be kind.", "why": "Another applied improvement adds it too.",
            "used_by": "B", "owner": "b", "kind": "prompt", "target": target(home)}
    steps, left = changeset.readable(home.hass, [step], [kept])
    assert steps == [] and [entry["what"] for entry in left] == ["Assistant's instructions"]  # Nothing kept for B.
    assert left[0]["why"].startswith("Home Assistant can't read the edited instructions: ")
    assert panel.outcome_text({"status": "awaiting_review", "outcome": {"status": "done", "left": left}}) == (
        "Undone: what was there before is back. Left as you changed it: Assistant's instructions")


@pytest.mark.asyncio
async def test_a_line_handed_to_an_improvement_whose_undo_is_in_flight_stays_handed_over(home, monkeypatch):
    import asyncio
    from test_spec_improvements import failing_writes
    from custom_components.assist_improver import improvements as module
    from custom_components.assist_improver.changeset import Changesets
    quiet, music = "Quiet hours start at 22:00.", "No music in quiet hours."
    later = {**line_item(home, "b", copy={"headline": "No music in quiet hours"}),
             "changes": [{"kind": "preference", "for": "everyone", "text": quiet},
                         {"kind": "preference", "for": "everyone", "text": music}]}
    improvements = await reviewing(home, line_item(home, "a", text=quiet), later)
    before = await home_now(home)
    for improvement_id in ("a", "b"):
        assert (await improvements.decide(improvement_id, "accept", user_id="alex"))["status"] == "applied"
    paused, go, read = asyncio.Event(), asyncio.Event(), module.read

    async def reading(*args):  # B's undo (in a second tab) waits after reading its record, before its changes.
        if not paused.is_set():
            paused.set()
            await go.wait()
        return await read(*args)
    monkeypatch.setattr(module, "read", reading)
    second_tab = asyncio.ensure_future(improvements.decide("b", "undo", user_id="alex"))
    await paused.wait()
    await improvements.decide("a", "undo", user_id="alex")  # Keeps A's line for B: B's own now.
    improvements.changesets = Changesets(home.hass, writer=failing_writes(1))
    go.set()
    assert (await second_tab)["status"] == "applied"  # B's undo failed: still applied ...
    improvements.changesets = Changesets(home.hass)
    await improvements.decide("b", "undo", user_id="alex")  # ... and undoing it now takes the line out.
    assert (await home_now(home))["household"] == before["household"]


def test_a_line_replacing_one_in_several_peoples_lines_replaces_it_in_each_of_theirs_only():
    from custom_components.assist_improver import household
    old, new = "My light means the desk lamp.", "My light means the floor lamp."
    knowledge = {"everyone": [], "people": {"alex": [old], "jordan": [old], "riley": [old]}}
    change = {"kind": "preference", "for": "this_user", "text": new, "replaces": [old]}
    applied, _, problems = household.edit(knowledge, [change], ["alex", "jordan"])  # Applying, for both it was tested for.
    assert applied["people"] == {"alex": [new], "jordan": [new], "riley": [old]} and not problems
    replayed, _, problems = household.edit(knowledge, [change], "jordan")  # Replaying Jordan's conversation.
    assert replayed["people"] == {"alex": [old], "jordan": [new], "riley": [old]} and not problems


@pytest.mark.asyncio
@pytest.mark.parametrize("entity_id, device_class, noted", [
    ("cover.garage_1", "garage", True), ("cover.front", "door", True), ("cover.drive", "gate", True),
    ("valve.water_main", None, True), ("lock.back", None, True), ("alarm_control_panel.house", None, True),
    ("cover.blind", "shade", False), ("cover.awning", None, False), ("switch.relay_3", None, False),
    ("light.garage_door", None, False)])  # Named after a door, a switch or light moves none of its own.
async def test_the_review_notes_motion_by_domain_and_a_covers_device_class(home, entity_id, device_class, noted):
    """DETAILS P11.5: a garage, door or gate cover, a valve, lock or alarm panel is taken as moved once its
    command succeeds, and its review says so; a cover's device class is read from its state, else its registry
    entry."""
    from homeassistant.helpers import entity_registry as er
    from custom_components.assist_improver import panel
    attributes = {"friendly_name": "Garage Door", **({"device_class": device_class} if device_class else {})}
    home.hass.states.async_set(entity_id, "off", attributes)
    item = alias_item(home, "r", entity_id=entity_id, alias="it", cause="")
    assert panel.review(item, ([], [], []), hass=home.hass)["physical"] is noted  # Its review notes its motion.
    if entity_id.startswith("cover."):  # Not loaded now: its registry entry's device class.
        home.hass.states.async_remove(entity_id)
        domain, object_id = entity_id.split(".")
        er.async_get(home.hass).async_get_or_create(domain, "demo", object_id, suggested_object_id=object_id,
                                                    original_device_class=device_class)
        assert panel.review(item, ([], [], []), hass=home.hass)["physical"] is noted
