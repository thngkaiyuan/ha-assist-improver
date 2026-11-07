"""Journeys: what Alex asked to know about the home, through to the review. A question is judged on the reply that
ends its run (the analysis), and again in each replay with the home as that replay had it; a wrong answer is trouble,
and a fix that has Assist answer right the first time shows it: the question met, and the turn spent asking again and
the filing after it no longer said. Here Assist answers without looking, and he has to ask again.

SPEC.md P0.1.1 (fulfilment and effort). DETAILS P9.7.4 (a question's destination), P9.10.4 (judged against the home),
P6.4 (a step under a question said only while no reply so far gave the answer).
"""

from playwright.sync_api import expect

from test_reactions import FILED, HOUSEHOLD, open_review, said_in_replays

ASKED = "Is the desk lamp on?"


def test_a_wrong_answer_about_the_home_is_trouble_and_a_fix_that_checks_answers_it_first_time(page, ha):
    """Assist says the desk lamp is off when Alex has just turned it on; he asks again and gets the same, then files it.
    The fix, a household line, has Assist check the lamp before answering: the replay with it answers right at once,
    so the question is met, and neither the turn asking again nor the filing is needed."""
    page.goto("/")
    expect(page.assist("Turn on the desk lamp")).to_contain_text("Turned on")
    expect(page.assist(ASKED)).to_contain_text("The desk lamp is off")  # It's on: a guess, wrong.
    expect(page.assist("Is the desk lamp on or not?")).to_contain_text("is off")
    expect(page.assist(FILED)).to_contain_text("Filed")
    page.close_assist()
    run = ha.wait_for_run("filed")
    assert run["status"] == "done", run

    panel = open_review(page, "Check before answering")
    change = panel.locator("details[data-key=changes] .file")
    expect(change.locator("tr.add .text")).to_have_text(["- Check a device's state before saying whether it's on or off."])
    expect(panel.locator(".tests li .result")).to_contain_text("goal now met")
    assert said_in_replays(ha) == [{"before": [1, 2, 3, 4]}, {"after": [1, 2]}]  # Asked once; no need to file it.
    assert ha.storage(HOUSEHOLD) in (None, {"everyone": [], "people": {}})  # Nothing applied before Alex decides.
    assert page.errors == [], (page.errors, page.rejections, page.blocked)
