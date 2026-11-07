"""Journeys: what the household does with its conversations in the panel, and with its settings.

- Archive and unarchive: a conversation that has ended, with nothing of it waiting for review, is archived from its
  menu (More actions): it leaves the list and the run, and shows among the archived; Unarchive, from the same menu,
  brings it back to the list, new, for a run to take.
- Send in again: a conversation a run settled (Looked smooth) is sent in again from the panel: it's back in the list,
  a new round starts, and a run takes it at once, past the quick check this time.
- Settings: the tests per conversation, changed in Settings, are still as set after the page is loaded again, in Home
  Assistant's saved options too.

SPEC.md P0.6 (sending it in again starts a new round), P0.7 (runs: a manual full run admits every new one; the fast
lane takes one sent in), P0.12 (Your household: archived, looked smooth; any can be sent in again).
DETAILS P3.3.1 (archived: no run admits or counts it), P3.3.3 (unarchiving one archived by hand puts it back in the
pipeline), P14.1 (every screen reads the one record), P14.1.6/P14.1.7 (archiving updates the Run count at once; an
archived one is on no Run button), P14.3 (chips), P14.4.1-P14.4.4 (settled ones leave the list; Show archived;
unarchived ones stay listed; 'Nothing needs you right now.'), P14.4.13 (the opened conversation's More actions), P15.1
(settings in one place), P15.9.2-P15.9.4 (the tests per conversation, 3 by default, set on Simulate).
"""

import re

from playwright.sync_api import expect

FLOW = "assist_improver.flow"  # The flow record of each conversation (flow.py), as Home Assistant saves it.
ARCHIVED = re.compile(r"\bcs-archived\b")  # A row shown dimmed among the archived.
AT_ONCE = 10_000  # ms: the longest the screen takes to show what a click did (no run in between).


# What the household does.

def say(page, words, reply):
    """One request in the Assist dialog, from the dashboard, with Assist's reply checked; the dialog closed."""
    page.goto("/")
    expect(page.assist(words)).to_contain_text(reply)
    page.close_assist()


def open_conversation(page, words):
    """Opens the conversation in the panel's list to read it (its row's head): the row, once its turns are shown."""
    row = page.conversation_row(words)
    row.locator(".cs-head").click()
    expect(row.locator(".cs-read .cs-turn").first).to_be_visible()
    return row


def more_actions(row):
    """The opened conversation's More actions (⋮), opened: its menu."""
    row.get_by_role("button", name="More actions").click()
    menu = row.get_by_role("menu")
    expect(menu).to_be_visible()
    return menu


def run_on_one(page):
    """The home page's Run, on the one new conversation, confirmed (a manual full run)."""
    run = page.panel.locator("#cs-run")
    expect(run).to_have_text("Run on 1 new conversation")
    run.click()
    dialog = page.panel.get_by_role("dialog", name="Run on 1 new conversation?")
    expect(dialog).to_be_visible()
    dialog.locator("#start-run").click()
    expect(dialog).to_have_count(0)


def settings(page):
    """Settings, from the panel's gear."""
    page.panel.get_by_role("button", name="Settings").click()
    expect(page.page).to_have_url(re.compile(r"/assist-improver\?view=settings$"))
    expect(page.panel.locator(".flow-head")).to_be_visible()


# What Home Assistant holds.

def listed(ha, words):
    """The conversation whose first words are these, as Assist Improver lists it (archived ones too): its row (id,
    status, hidden, ...), or None."""
    rows = ha.conversations()
    return next((row for row in rows if row["said"] == words), None)


def ended(ha, words):
    """Waits for the conversation to end (Home Assistant ends its chat session once it's idle: CONVERSATION_TIMEOUT
    in the throwaway Home Assistant): its row, once it's no longer still going."""
    return ha.wait(lambda: (row := listed(ha, words)) and row["status"] != "active" and row,
                   what=f"“{words}” to end")


def record(ha, rid):
    """The conversation's flow record as Home Assistant last saved it: stage, round, outcome, archived, rounds, ..."""
    return ((ha.storage(FLOW) or {}).get("conversations") or {}).get(rid)


def admits(ha):
    """How many new conversations a manual full run would take in now (the Run bar's count)."""
    return ha.ws.call("assist_improver/run")["preview"]["admits"]


# The journeys.

def test_archive_and_unarchive(page, ha):
    """A conversation that has ended, with nothing waiting for review, archived from its menu: out of the list and the
    run, among the archived as Archived; unarchived from its menu: back in the list, new, for a run to take."""
    said = "Turn on the floor lamp"
    say(page, said, "Turned on")
    rid = ended(ha, said)["id"]

    panel = page.open_panel()
    row = page.conversation_row(said)
    expect(page.chip(row)).to_have_text("New")
    run = panel.locator("#cs-run")
    expect(run).to_have_text("Run on 1 new conversation")

    # Archived from its menu: it leaves the list, nothing else needs the household, and no run counts it.
    open_conversation(page, said)
    more_actions(row).get_by_role("menuitem", name="Archive this conversation").click()
    expect(row).to_have_count(0)
    expect(panel.locator(".cs-empty")).to_have_text("Nothing needs you right now.")
    expect(run).to_have_count(0)
    archived = ha.wait(lambda: (r := record(ha, rid)) and r["archived"] and r, what="the conversation archived")
    assert (archived["stage"], archived["outcome"]) == ("Your household", "archived"), archived
    assert admits(ha) == 0

    # Among the archived: dimmed, as Archived.
    expect(panel.locator(".cs-show-archived")).to_have_text("Show archived (1)")
    page.show_archived()
    expect(page.chip(row)).to_have_text("Archived")
    expect(row).to_have_class(ARCHIVED)
    page.shot("archived")

    # Unarchived from its menu: back in the list, nothing archived any more, a run counts it again, and its chip says
    # it's new, as its record does (one source for every screen: DETAILS P14.1).
    open_conversation(page, said)
    more_actions(row).get_by_role("menuitem", name="Unarchive").click()
    expect(panel.locator(".cs-show-archived")).to_have_count(0)
    expect(row).not_to_have_class(ARCHIVED)
    expect(run).to_have_text("Run on 1 new conversation")
    back = ha.wait(lambda: (r := record(ha, rid)) and not r["archived"] and r, what="the conversation unarchived")
    assert back["stage"] == "Your Assist", back
    assert listed(ha, said)["status"] == "waiting" and not listed(ha, said)["hidden"]
    assert admits(ha) == 1
    expect(page.chip(row)).to_have_text("New", timeout=AT_ONCE)

    # And so it stays when the page is loaded again.
    page.page.reload()
    expect(page.chip(row)).to_have_text("New")
    expect(row).not_to_have_class(ARCHIVED)
    expect(panel.locator(".cs-show-archived")).to_have_count(0)
    assert page.errors == [], (page.errors, page.rejections, page.blocked)


def test_send_in_again(page, ha, fake_model):
    """A conversation a run settled (Looked smooth) sent in again from the panel: back in the list, a new round, and a
    run takes it at once, past the quick check; the page follows it to where it ends."""
    said = "Turn on the desk lamp"
    say(page, said, "Turned on")
    assert ha.state("light.desk_lamp")["state"] == "on"
    rid = ended(ha, said)["id"]

    # Run on it: the quick check finds it went smoothly, so it settles in Your household and leaves the list.
    panel = page.open_panel()
    row = page.conversation_row(said)
    expect(page.chip(row)).to_have_text("New")
    run_on_one(page)
    first = ha.wait_for_run("manual")
    assert first["status"] == "done", first
    assert first["results"][rid]["outcome"] == "looked smooth", first["results"]
    expect(row).to_have_count(0)
    page.show_archived()
    expect(page.chip(row)).to_have_text("Looked smooth")
    expect(row).to_have_class(ARCHIVED)

    # Sent in again from the panel: back in the list, and the improver looks at it now. The panel says so until the
    # improver has taken it, which a fast run does at once: its chip then says where it stands.
    open_conversation(page, said)
    row.get_by_role("button", name="Send this one in").click()
    expect(row.locator(".cs-done", has_text="Sent in. The improver is looking at it now.").or_(
        page.chip(row).filter(has_not_text="Looked smooth")).first).to_be_visible()
    expect(row).not_to_have_class(ARCHIVED)
    page.shot("sent-again")

    # A run of its own takes it (the fast lane), for a new round: one fresh full analysis, past the quick check this
    # time (DETAILS P4.5, P9.3.1), that finds no trouble (the lamp came on as asked, and the helper the lamp's own
    # action switched is no side effect left: DETAILS P6.1.10), so it ends in Your household with nothing waiting; the
    # earlier round's outcome stays.
    second = ha.wait_for_run("filed")
    assert second["status"] == "done", second
    assert second["picked"] == [rid] and second["admitted"] == [rid], second
    settled = record(ha, rid)
    assert settled["round"] == 2, settled
    assert settled["rounds"]["1"]["records"]["Your household"]["outcome"] == "looked smooth", settled["rounds"]["1"]
    assert "analysis" in settled["rounds"]["2"]["records"]["Analyze"], settled["rounds"]["2"]["path"]
    assert fake_model.roles(ha.since).count("triage") == 1, fake_model.roles(ha.since)  # Round 1's only.
    assert settled["stage"] == "Your household" and settled["outcome"] == "no trouble found", settled
    assert second["results"][rid]["outcome"] == settled["outcome"], second["results"]

    # The page follows the run: the conversation, still open, now says where its new round ended.
    expect(page.chip(row)).to_have_text("No trouble found")
    expect(panel.locator(".cs-show-archived")).to_have_text("Show archived (1)")
    assert page.errors == [], (page.errors, page.rejections, page.blocked)


def test_settings_persist(page, ha):
    """The tests per conversation raised in Settings: still so after the page is loaded again, as Home Assistant saved
    them."""
    page.goto("/")
    panel = page.open_panel()
    settings(page)
    tests = panel.locator('[data-knob="test"]')  # Simulate's "Your assistant", with the tests per conversation.
    count = panel.locator("#pop .t-n")
    expect(tests).to_have_attribute("data-tip", "At most 3 tests of a conversation each round, on your assistant.")

    # 5 tests per conversation instead of 3.
    tests.click()
    expect(count).to_have_text("3")
    for n in ("4", "5"):
        panel.locator("#t-plus").click()
        expect(count).to_have_text(n)
    page.page.keyboard.press("Escape")
    expect(panel.locator("#pop")).to_have_count(0)
    expect(tests).to_have_attribute("data-tip", "At most 5 tests of a conversation each round, on your assistant.")
    page.shot("settings")
    ha.wait(lambda: (ha.options() or {}).get("tests_per_goal") == 5, what="the settings saved")

    # Loaded again, Settings shows them as set.
    page.page.reload()
    expect(panel.locator(".flow-head")).to_be_visible()
    expect(tests).to_have_attribute("data-tip", "At most 5 tests of a conversation each round, on your assistant.")
    tests.click()
    expect(count).to_have_text("5")
    page.page.keyboard.press("Escape")
    assert page.errors == [], (page.errors, page.rejections, page.blocked)
