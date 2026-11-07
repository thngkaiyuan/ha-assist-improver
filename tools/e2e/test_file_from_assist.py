"""Journey: Alex asks Assist for a device by a name it doesn't know, then files an improvement from Assist; the
conversation ends, the fast lane takes it in on its own, and the panel shows it moving to Your review with the fix up
for review, an alias for that device, and the decision bar offering accept, revise and decline.

SPEC.md P0.4 (stages), P0.6 (a round flows until an improvement waits at Your review), P0.7 (the fast lane: one sent
in, as soon as it ends), P0.11 (Your review: accept, revise or decline). DETAILS P14.3.9 (Up for review: its chip
opens the review), P14.4.1 (a conversation whose improvement goes up for review leaves the list; its improvement waits
at the top of the home page), P12.4/P12.4.1/P12.4.8 (one split button defaulting to Accept changes, with Ask for a
revision and Decline behind its arrow), P12.4.11 (nothing changes in Home Assistant until a decision applies it),
P6.2.2 (a replay runs on the assistant that served the conversation, with nothing for the household to configure).
"""

import re

import pytest
from playwright.sync_api import expect

LAMP = "light.desk_lamp"  # "Desk lamp" in the Study: nothing in the household is called the reading lamp.
ASKED = "Turn on the reading lamp"
FILED = "File an improvement: the reading lamp is the desk lamp in the study"


def file_from_assist(page, ha):
    """The journey, on whatever assistant the household has."""
    assert ha.aliases(LAMP) == [], "The desk lamp starts with no other names"
    assert ha.state(LAMP)["state"] == "off"

    # In the Assist dialog: a name Assist doesn't have, then filing an improvement.
    page.goto("/")
    expect(page.assist(ASKED)).to_contain_text("couldn't find")
    expect(page.assist(FILED)).to_contain_text("Filed")
    page.close_assist()

    # Still going, the conversation is in the panel as sent in (filed while it went on).
    panel = page.open_panel()
    row = page.conversation_row(ASKED)
    expect(page.chip(row)).to_have_text("Sent in")

    # It ends (its chat session idles out) and the fast lane runs it on its own; nobody pressed Run.
    run = ha.wait_for_run("filed")
    assert run["status"] == "done", run

    # Its improvement goes up for review: it waits at the top of the home page (and in the sidebar's count, where the
    # sidebar shows), and the conversation leaves the list.
    expect(panel.locator("h1").filter(has_text="1 improvement to review")).to_be_visible(timeout=15_000)
    improvement = panel.locator(".item").filter(has=page.page.locator(".go"))  # Up for review: it opens.
    expect(improvement).to_have_count(1)
    if not page.phone:
        expect(page.page.locator("ha-sidebar").get_by_text("Assist Improver (1)")).to_be_visible()
    expect(row).to_have_count(0)

    # Under Show archived, the conversation stands at Your review: its chip says so and opens the review.
    page.show_archived()
    expect(page.chip(row)).to_have_text("Up for review")  # Its one chip: not Improved or Declined.
    review_chip = row.locator(".cs-review")
    expect(review_chip).to_have_text("Up for review")
    review_chip.click()
    expect(page.page).to_have_url(re.compile(r"/assist-improver\?id="))

    # The review: up for review, and what changes is an alias for the desk lamp, the name Alex said.
    expect(panel.locator("h1 .chip")).to_have_text("To review")
    changes = panel.locator("details[data-key=changes]")
    expect(changes.locator(":scope > summary")).to_have_text("What changes (1)")
    change = changes.locator(".file")
    expect(change).to_have_count(1)
    expect(change.locator("summary .name")).to_have_text("Desk lamp")
    expect(change.locator("summary .kind")).to_have_text("aliases")
    expect(change.locator("tr.add .text")).to_have_text(["Reading lamp"])

    # Its decision bar: Accept changes by default; Ask for a revision and Decline behind the arrow.
    expect(panel.locator(".footer #decide")).to_have_text("Accept changes")
    panel.locator(".footer #arrow").click()
    choices = panel.locator(".footer .menu [data-choice]")
    expect(choices).to_have_text(["Accept changes", "Ask for a revision", "Decline"])
    page.shot("review")

    # Nothing has changed in Home Assistant: the alias waits for the household's decision, and testing it moved no
    # real device.
    assert ha.aliases(LAMP) == []
    assert ha.state(LAMP)["state"] == "off"
    assert ha.state("input_boolean.desk_lamp_power")["state"] == "off"
    assert page.errors == [], (page.errors, page.rejections, page.blocked)


def test_file_from_assist(page, ha):
    """On the household's assistant as set up (a model chosen explicitly)."""
    file_from_assist(page, ha)


@pytest.mark.parametrize("page", ["phone"], indirect=True)
def test_file_from_assist_on_recommended_settings(page, ha):
    """On an assistant left on Home Assistant's recommended model settings (how the anthropic integration sets up its
    assistant): the replay runs on it all the same (DETAILS P6.2.2), so the fix is tested and goes up for review."""
    ha.assistant_model(None)
    file_from_assist(page, ha)
