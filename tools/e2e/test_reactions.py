"""Journeys: what Alex's later turns reacted to, through to the review. An objection to Assist's reply is a goal of its
own, judged one replay turn at a time; once no reply so far does what he objected to, its turn and the filing that
ended the conversation aren't said, and the fix shows what it fixed in his words. A request said again after Assist
failed is a step towards it: once the fix meets the request in its first turn, the step is met and its turn isn't
said; a filing made after the request was met can't be tested, so it's set aside and said with the change too.

Two journeys: an objection to a promise, with a thanks beside it (an objection the fix answers, in a turn of its own,
and a thanks said all the same), and a request asked for again (a step the fix closes).

SPEC.md P0.1.1 (fulfilment and effort), P0.3 (every turn maps to a goal). DETAILS P2.3.2 (a reaction names what it
reacts to), P6.1.9 (an objection judged one Assist turn at a time), P6.4 (a turn said only while a goal on it is open;
set aside), P9.13 (an objection is a sub-goal in the household's own words).
"""

import re

from playwright.sync_api import expect

from fake_anthropic import EXPECT_NEXT

LAMP = "light.desk_lamp"
OBJECTED = "Just say what you did, without promising what happens next"
FILED = "File an improvement."
HOUSEHOLD = "assist_improver.household"  # The household knowledge as Home Assistant saves it.


def open_review(page, title):
    """The improvement's review, from the panel's home page, where it waits up for review: the panel."""
    page.goto("/")
    panel = page.open_panel()
    expect(panel.locator("h1").filter(has_text="1 improvement to review")).to_be_visible(timeout=15_000)
    panel.locator(".item").filter(has_text=title).click()
    expect(page.page).to_have_url(re.compile(r"/assist-improver\?id="))
    return panel


def said_in_replays(ha):
    """The replays the one test kept, each as {kind: the turns it said}."""
    return [{r["plan"]["kind"]: r["said"]} for r in ha.storage("assist_improver.replays")["replays"].values()]


def test_an_objection_the_fix_answers_closes_and_the_filing_after_it_needs_no_turn(page, ha):
    """Assist promises what happens next (its instructions ask it to); Alex thanks it, objects to the promise in a turn
    of its own, then files it. The fix, a household line, has Assist say only what it did: no reply with it promises
    anything, so the objection is met and neither its turn nor the filing is needed; the thanks is said all the same."""
    ha.assistant_instructions(EXPECT_NEXT)
    page.goto("/")
    expect(page.assist("Turn on the kettle")).to_contain_text("It will be ready in a few minutes")
    expect(page.assist("Thanks.")).to_contain_text("You're welcome")
    page.assist(OBJECTED)
    expect(page.assist(FILED)).to_contain_text("Filed")
    page.close_assist()
    run = ha.wait_for_run("filed")
    assert run["status"] == "done", run

    panel = open_review(page, "Say only what was done")
    change = panel.locator("details[data-key=changes] .file")
    expect(change.locator("summary .name")).to_have_text("Household knowledge")
    expect(change.locator("tr.add .text")).to_have_text(["- Say only what you did, without promising what happens next."])
    # What it gets you, under the headline; its before and after show it: with the change, neither the objection
    # nor the filing is said.
    expect(panel.locator(".outcome")).to_contain_text("2 fewer turns")
    expect(panel.locator(".outcome")).to_contain_text("follows your feedback")
    said = panel.locator(".tests .rp-after .cs-you .cs-says")
    expect(said).to_have_count(2)
    for turn in (OBJECTED, FILED):
        expect(panel.locator(".tests .rp-before .cs-you .cs-says").filter(has_text=turn)).to_have_count(1)
        expect(said.filter(has_text=turn)).to_have_count(0)
    expect(panel.locator(".rp-open")).to_have_count(0)  # Nothing still open.
    assert said_in_replays(ha) == [{"before": [1, 2, 3, 4]}, {"after": [1, 2]}]  # The thanks said; the rest not.
    assert ha.storage(HOUSEHOLD) in (None, {"everyone": [], "people": {}})  # Nothing applied before Alex decides.
    assert page.errors == [], (page.errors, page.rejections, page.blocked)


def test_a_request_said_again_is_a_step_the_fix_closes_and_a_filing_after_it_was_met_is_set_aside(page, ha):
    """Assist doesn't know the reading lamp, so Alex asks for the desk lamp instead (a step towards the same goal), then
    files it. The fix, the name he said, meets the request in its first turn: the step is met and its turn isn't
    said. The filing came once the lamp was on, so without the change it's met already: set aside, it's said with the
    change too and counts neither way."""
    page.goto("/")
    expect(page.assist("Turn on the reading lamp")).to_contain_text("couldn't find")
    expect(page.assist("Turn on the desk lamp")).to_contain_text("Turned on the Desk lamp")
    expect(page.assist(FILED)).to_contain_text("Filed")
    page.close_assist()
    run = ha.wait_for_run("filed")
    assert run["status"] == "done", run

    panel = open_review(page, "Reading lamp")
    expect(panel.locator("details[data-key=changes] .file tr.add .text")).to_have_text(["Reading lamp"])
    expect(panel.locator(".outcome")).to_contain_text("1 fewer turn")
    expect(panel.locator(".tests .rp-after .cs-you .cs-says").filter(has_text="Turn on the desk lamp")).to_have_count(0)
    assert said_in_replays(ha) == [{"before": [1, 2, 3]}, {"after": [1, 3]}]
    assert ha.aliases(LAMP) == []  # Nothing applied before Alex decides.
    assert page.errors == [], (page.errors, page.rejections, page.blocked)
