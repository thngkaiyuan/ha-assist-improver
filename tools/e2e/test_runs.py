"""Journeys: runs the household starts, holds and schedules from the panel.

1. Run now on the home page takes in what nobody sent in: a conversation Assist couldn't fulfil goes all the way to
   Your review, and one Assist fulfilled ends as looked smooth, both out of the list.
2. A stage blocked in Settings holds a conversation there, with why, in the list, on its page and in Settings; once
   unblocked, Settings' Run now releases it and it moves on to Your review.
3. The daily schedule, set from Run now's menu in Settings, holds across a reload and shows on Run now's control
   (without waiting for 03:00).

Two more: a conversation Assist got right only when asked again by the lamp's own name reaches Your review too (the
household's lamps are template lights that switch a helper), and Settings, where runs are blocked and scheduled, shows
every stage's title in full on a phone.
And the home page, left open, follows a run that starts after it loaded (a conversation filed from Assist on another
screen) without a reload.

SPEC.md P0.4 (stages), P0.6 (a round flows until an improvement waits at Your review, or it ends in Your household),
P0.7 (a manual full run admits every new conversation and releases every held one; the fast lane, one sent in, as
soon as it ends), P0.8 (held at a stage for the stage being blocked, with why; only a manual full run, its stage's run
or a picked run releases it), P0.12 (looked smooth is in Your household). DETAILS P3.6 (blocking a stage),
P5.2/P5.2.1 (the home page's Run with nothing picked is Settings' manual full run), P5.3.1/P5.3.2 (the schedule: off
by default, set from the Run button's menu, named there and in its clock's tooltip; closing the menu changes nothing),
P5.8.2/P5.8.6/P14.7.2/P14.7.5 (the confirmation: which conversations, blocked stages named, the cost line), P5.9.2 (a
run refreshes itself), P14.3.4/P14.3.9/P14.3.10 (To analyze, Up for review, Looked smooth), P14.4.1/P14.4.2/P14.4.4
(both leave the list; Show archived; no 'Nothing needs you right now.' while one is up for review), P14.6.6 (the
menu's clock icons), P14.9.3 (a stage's count shows what's held there, why on demand), P14.10.3 (History says what
each run did).
"""

import re

import pytest
from playwright.sync_api import expect

LAMP = "light.desk_lamp"  # "Desk lamp" in the Study: nothing in the household is called the reading lamp.
CEILING = "light.ceiling_light"  # "Ceiling light" in the Living room: Assist knows it by that name.
UNKNOWN = "Turn on the reading lamp"  # A name Assist doesn't know: Assist can't fulfil it.
EXPLAINED = "The reading lamp is the desk lamp in the study"  # Said next, to Assist: which lamp that is.
RENAMED = "Turn on the desk lamp"  # Or asked again by the lamp's own name, which Assist knows.
KNOWN = "Turn on the ceiling light"  # A name Assist knows: Assist turns the light on.
SETTLED = {"fine": "No trouble found", "nofix": "No fix found"}  # A round that ends with nothing to review: chips.
SCHEDULES = ["Only when you run it", "Every day at 3:00, on new conversations",
             "Every Sunday at 3:00, on new conversations"]  # The Run menu's words (DETAILS P5.3.2).


# Home Assistant's side: where each conversation stands, and the runs (the same websocket commands the panel reads).

def stands(ha, said):
    """Where the conversation that began with `said` stands (assist_improver/conversations' row: status, stage,
    held, ...), archived ones included; None until it's recorded."""
    rows = ha.conversations()
    return next((r for r in rows if r["said"] == said), None)


def wait_ended(ha, *said):
    """Each of these conversations has ended (its chat session idled out) and is new: closed, waiting for a run."""
    return ha.wait(lambda: all((r := stands(ha, s)) and r["status"] == "waiting" for s in said),
                   what=f"{', '.join(said)} to end")


def last_run_id(ha):
    run = ha.run()
    return run and run.get("id")


def wait_next_run(ha, before):
    """The run started after the one with id `before` (None: the first run), once it has finished."""
    return ha.wait(lambda: (run := ha.run()) and run.get("id") != before and run.get("status") != "running" and run,
                   what="the run to finish")


# The screen.

def ask_by_unknown_name(page, then=EXPLAINED):
    """A conversation Assist couldn't fulfil: the reading lamp it can't find, then which lamp that is (Assist can't
    help with that either), or, with then=RENAMED, the lamp by its own name, which Assist turns on. Nobody files it;
    the dialog is closed after."""
    expect(page.assist(UNKNOWN)).to_contain_text("couldn't find")
    if then == RENAMED:
        expect(page.assist(RENAMED)).to_contain_text("Turned on the Desk lamp")
    else:
        expect(page.assist(then)).to_contain_text("can't help")
    page.close_assist()


def open_settings(page, panel):
    panel.locator("#open-settings").click()
    expect(page.page).to_have_url(re.compile(r"/assist-improver\?view=settings"))
    expect(panel.locator(".toolbar")).to_contain_text("Settings")
    expect(panel.locator(".flow-card")).to_be_visible()


def back_home(page, panel):
    panel.locator("#back").click()
    expect(page.page).not_to_have_url(re.compile(r"view=settings"))
    expect(panel.locator("section.cs")).to_be_visible()


def confirm_run(panel, title):
    """The run's confirmation (a modal), titled as its button said: its body's locator, once shown."""
    modal = panel.locator(".confirm-modal")
    expect(modal).to_be_visible()
    expect(modal.locator(".modal-head b")).to_have_text(title)
    expect(modal.locator(".cost").last).to_contain_text("Runs use your AI models, so they cost money.")
    return modal


def test_run_now_takes_in_what_nobody_sent_in(page, ha, fake_model):
    """Two conversations nobody sent in: one Assist couldn't fulfil (the reading lamp, then which lamp that is), one it
    did at once. Run now on the home page takes in both: the first goes all the way to Your review, with an improvement
    for it; the second looked smooth at Triage and is in Your household. Both leave the list."""
    assert ha.aliases(LAMP) == [] and ha.state(LAMP)["state"] == "off"
    assert ha.state(CEILING)["state"] == "off"

    # Two conversations with Assist, each in a dialog of its own; neither is filed.
    page.goto("/")
    ask_by_unknown_name(page)
    expect(page.assist(KNOWN)).to_contain_text("Turned on the Ceiling light")
    page.close_assist()
    ha.wait(lambda: ha.state(CEILING)["state"] == "on", what="Assist to turn the ceiling light on")

    # Both end (their chat sessions idle out); nothing runs on its own: neither was sent in.
    wait_ended(ha, UNKNOWN, KNOWN)
    assert ha.run() is None, "No run starts for a conversation nobody sent in (P0.7)"

    # The panel lists both as new, and its Run bar offers a run on both (the manual full run: nothing is picked).
    panel = page.open_panel()
    unknown, known = page.conversation_row(UNKNOWN), page.conversation_row(KNOWN)
    expect(page.chip(unknown)).to_have_text("New")
    expect(page.chip(known)).to_have_text("New")
    run_button = panel.locator(".cs-footer #cs-run")
    expect(run_button).to_have_text("Run on 2 new conversations")
    page.shot("before-run")

    # Its confirmation names both by their first words, with the cost line, and Run starts it.
    before = last_run_id(ha)
    run_button.click()
    modal = confirm_run(panel, "Run on 2 new conversations?")
    which = modal.locator(".c-which")
    expect(which).to_contain_text(f"“{UNKNOWN}”")
    expect(which).to_contain_text(f"“{KNOWN}”")
    modal.locator("#start-run").click()
    expect(modal).to_be_hidden()

    run = wait_next_run(ha, before)
    assert run["trigger"] == "manual" and not run.get("picked") and not run.get("only"), run
    assert run["status"] == "done", run
    assert run["progress"].get("new_improvements") == 1 and run["progress"].get("analyzed") == 1, run["progress"]

    # Home: the improvement waits at the top; the last run line counts it; both conversations left the list.
    expect(panel.locator("h1").filter(has_text="1 improvement to review")).to_be_visible()
    improvement = panel.locator(".item").filter(has=page.page.locator(".go"))  # Up for review: it opens.
    expect(improvement).to_have_count(1)
    expect(improvement.locator(".title")).to_contain_text("Call the desk lamp the reading lamp")
    expect(panel.locator(".cs-last")).to_contain_text("1 new improvement")
    expect(unknown).to_have_count(0)
    expect(known).to_have_count(0)
    expect(panel.locator(".cs-empty")).to_have_count(0)  # The improvement needs Alex: no line says otherwise.
    expect(panel.locator(".cs-show-archived")).to_have_text("Show archived (2)")
    expect(run_button).to_have_count(0)  # Nothing left to run.

    # Under Show archived: the first is up for review (its chip opens the review), the second looked smooth.
    page.show_archived()
    expect(unknown.locator(".cs-review")).to_have_text("Up for review")
    expect(page.chip(known)).to_have_text("Looked smooth")
    expect(page.chip(known)).to_have_attribute(
        "title", "Nothing to improve, at a quick look. Send it in for a full analysis.")
    page.shot("after-run")
    unknown.locator(".cs-review").click()
    expect(page.page).to_have_url(re.compile(r"/assist-improver\?id="))
    expect(panel.locator("h1 .chip")).to_have_text("To review")
    change = panel.locator("details[data-key=changes] .file")
    expect(change.locator("summary .name")).to_have_text("Desk lamp")
    expect(change.locator("tr.add .text")).to_have_text(["Reading lamp"])

    # Home Assistant agrees: one at Your review, one looked smooth in Your household, and only the first was analyzed.
    assert (stands(ha, UNKNOWN)["status"], stands(ha, UNKNOWN)["stage"]) == ("review", "Your review")
    assert (stands(ha, KNOWN)["status"], stands(ha, KNOWN)["stage"]) == ("smooth", "Your household")
    analyzed = [r["request"] for r in fake_model.requests[ha.since:] if r["role"] == "journey_analyzer"]
    assert analyzed and all(UNKNOWN in str(r["messages"][0]) and KNOWN not in str(r["messages"][0])
                            for r in analyzed), "Only the conversation Assist couldn't fulfil is analyzed"
    # Nothing changed in the home before a decision: the alias waits for review, and testing it (the replay turns the
    # desk lamp on) moved no real device.
    assert ha.aliases(LAMP) == []
    assert ha.state(LAMP)["state"] == "off" and ha.state("input_boolean.desk_lamp_power")["state"] == "off"
    assert page.errors == [], (page.errors, page.rejections, page.blocked)


@pytest.mark.parametrize("page", ["phone"], indirect=True)
def test_run_now_takes_in_one_assist_got_right_the_second_time(page, ha):
    """A conversation Assist fulfilled only when asked again by the lamp's own name: the reading lamp it couldn't find,
    then the desk lamp, which it turned on. The turn an alias would save is real friction, so Run now carries the
    conversation to Your review with that alias, validated in a replay of the conversation (SPEC P0.6, P0.9; DETAILS
    P6.6.1: the baseline replay reproduces when the devices the recorded Assist changed get the effects recorded;
    P11.1: the sandbox runs Home Assistant's own logic and emulates only the physical devices). The household's lights
    are template lights, each switching a helper as it turns on, as many households' are. Phone size only: nothing
    in it depends on the screen."""
    page.goto("/")
    ask_by_unknown_name(page, then=RENAMED)
    ha.wait(lambda: ha.state(LAMP)["state"] == "on", what="Assist to turn the desk lamp on")
    wait_ended(ha, UNKNOWN)

    panel = page.open_panel()
    row = page.conversation_row(UNKNOWN)
    expect(page.chip(row)).to_have_text("New")
    run_button = panel.locator(".cs-footer #cs-run")
    expect(run_button).to_have_text("Run on 1 new conversation")
    before = last_run_id(ha)
    run_button.click()
    modal = confirm_run(panel, "Run on 1 new conversation?")
    modal.locator("#start-run").click()
    expect(modal).to_be_hidden()
    run = wait_next_run(ha, before)
    assert run["status"] == "done", run

    # Up for review, with the alias the user's first words needed; nothing changed in the home yet.
    expect(row).to_have_count(0)
    page.show_archived()
    expect(page.chip(row).or_(row.locator(".cs-review"))).to_have_text("Up for review", timeout=15_000)  # Run done.
    expect(panel.locator("h1").filter(has_text="1 improvement to review")).to_be_visible()
    assert run["progress"].get("new_improvements") == 1, run["progress"]
    assert (stands(ha, UNKNOWN)["status"], stands(ha, UNKNOWN)["stage"]) == ("review", "Your review")
    assert ha.aliases(LAMP) == []
    assert page.errors == [], (page.errors, page.rejections, page.blocked)


def test_blocked_stage_holds_until_released(page, ha):
    """Analyze blocked in Settings: a full run takes in a conversation Assist couldn't fulfil and holds it at Analyze,
    and the list, its page and Settings say so and why. Unblocked, it stays held until Settings' Run now releases it;
    then it moves on to Your review."""
    page.goto("/")
    ask_by_unknown_name(page)

    # Settings: block Analyze.
    panel = page.open_panel()
    open_settings(page, panel)
    block = panel.get_by_role("button", name="Block Analyze")
    expect(block).to_have_attribute("aria-pressed", "false")
    block.click()
    unblock = panel.get_by_role("button", name="Unblock Analyze")
    expect(unblock).to_have_attribute("aria-pressed", "true")
    ha.wait(lambda: (ha.options() or {}).get("paused_stages") == ["analyze"], what="Analyze to be blocked")

    # The conversation ends; back home, Run (the manual full run) offers to take it in, and says Analyze is blocked.
    wait_ended(ha, UNKNOWN)
    back_home(page, panel)
    row = page.conversation_row(UNKNOWN)
    expect(page.chip(row)).to_have_text("New")
    run_button = panel.locator(".cs-footer #cs-run")
    expect(run_button).to_have_text("Run on 1 new conversation")
    before = last_run_id(ha)
    run_button.click()
    modal = confirm_run(panel, "Run on 1 new conversation?")
    expect(modal.locator(".c-which")).to_have_text(f"“{UNKNOWN}”")
    expect(modal).to_contain_text("Analyze is blocked.")
    modal.locator("#start-run").click()
    expect(modal).to_be_hidden()
    run = wait_next_run(ha, before)
    assert run["status"] == "done", run

    # Held at Analyze, because Analyze is blocked: in Home Assistant, ...
    held = stands(ha, UNKNOWN)
    assert held["stage"] == "Analyze" and held["held"] == {"cause": "blocked", "why": "Analyze is blocked."}, held

    # ... in the list (its chip names the stage; its tooltip says why it's held) ...
    expect(page.chip(row)).to_have_text("To analyze")
    expect(page.chip(row)).to_have_attribute("title", "Held: Analyze is blocked.")
    # ... and on its page: opened, it reads as it went, still held, with why; held, it can't be sent in on its own.
    row.locator(".cs-head").click()
    expect(row.locator(".cs-read .cs-you .cs-says")).to_have_text([UNKNOWN, EXPLAINED])
    expect(row.locator(".cs-read .cs-assist .cs-says").first).to_contain_text("couldn't find")
    expect(page.chip(row)).to_have_text("To analyze")
    expect(page.chip(row)).to_have_attribute("title", "Held: Analyze is blocked.")
    expect(row.locator("#cs-send")).to_have_count(0)
    expect(run_button).to_have_count(0)  # Analyze is still blocked: no run would move it.
    page.shot("held")

    # Settings: Analyze's count shows it held while blocked (why on hover), and History says what the run did.
    open_settings(page, panel)
    badge = panel.locator(".stn[data-step=analyze] .badge")
    expect(badge).to_have_text("1")
    expect(badge).to_have_class(re.compile(r"\bheld\b"))
    expect(badge).to_have_attribute("data-tip", "1 held while blocked")
    badge.hover()
    expect(panel.locator("#tip")).to_have_text("1 held while blocked")
    expect(panel.locator(".stn[data-step=requests] .badge")).to_have_count(0)
    latest = panel.locator(".card.history .h-row").first
    expect(latest).to_contain_text("Run finished")
    expect(latest).to_contain_text("1 held: Analyze is blocked.")

    # Unblocked, nothing moves on its own: it stays held at Analyze until a run releases it.
    unblock = panel.get_by_role("button", name="Unblock Analyze")
    unblock.click()
    expect(panel.get_by_role("button", name="Block Analyze")).to_have_attribute("aria-pressed", "false")
    ha.wait(lambda: (ha.options() or {}).get("paused_stages") == [], what="Analyze to be unblocked")
    assert last_run_id(ha) == run["id"] and stands(ha, UNKNOWN)["stage"] == "Analyze"

    # Settings' Run now releases it (no new conversation, so it runs what's waiting): its confirmation says it's
    # waiting at Analyze.
    run_now = panel.locator("#run-now")
    expect(run_now).to_have_text("Run what's waiting")
    run_now.click()
    modal = confirm_run(panel, "Run Assist Improver?")
    expect(modal.locator(".c-what")).to_have_text("1 conversation to analyze is waiting.")
    expect(modal).not_to_contain_text("blocked")
    before = run["id"]
    modal.locator("#start-run").click()
    expect(modal).to_be_hidden()
    run = wait_next_run(ha, before)
    assert run["trigger"] == "manual" and run["status"] == "done", run
    assert run["progress"].get("new_improvements") == 1, run["progress"]

    # It moved on: up for review, no longer held, and Settings' History says what this run did.
    moved = stands(ha, UNKNOWN)
    assert (moved["status"], moved["stage"], moved["held"]) == ("review", "Your review", None), moved
    expect(latest).to_contain_text("1 new improvement")
    expect(panel.locator(".stn[data-step=analyze] .badge")).to_have_count(0)
    back_home(page, panel)
    expect(panel.locator("h1").filter(has_text="1 improvement to review")).to_be_visible()
    expect(row).to_have_count(0)
    page.show_archived()
    expect(row.locator(".cs-review")).to_have_text("Up for review")
    assert ha.aliases(LAMP) == []
    assert page.errors == [], (page.errors, page.rejections, page.blocked)


def test_daily_schedule_holds_across_a_reload(page, ha):
    """Run now's menu in Settings: off by default; the daily schedule, once chosen, is saved, shows on Run now's
    control (its clock, and the clock's tooltip names it), and is still chosen after a reload."""
    page.goto("/")
    panel = page.open_panel()
    open_settings(page, panel)

    # Off by default: the menu arrow has no clock, and its tooltip says runs happen only when you run one.
    arrow = panel.get_by_role("button", name="When it runs, or run one step")
    expect(arrow).to_have_attribute("data-tip", SCHEDULES[0])
    expect(arrow.locator("svg")).to_have_count(0)
    assert (ha.options() or {}).get("schedule", "off") == "off"

    # The menu: the three schedules (a clock beside each scheduled one), 'Only when you run it' chosen.
    arrow.click()
    menu = panel.locator(".split .menu")
    options = menu.locator("[data-schedule]")
    expect(options).to_have_text([re.compile(re.escape(s)) for s in SCHEDULES])
    expect(menu.locator("[data-schedule=off]")).to_have_class(re.compile(r"\bon\b"))
    expect(menu.locator("[data-schedule=off] svg")).to_have_count(0)
    expect(menu.locator("[data-schedule=daily] svg")).to_have_count(1)
    expect(menu.locator("[data-schedule=weekly] svg")).to_have_count(1)

    # Every day at 3:00: the menu closes, and Run now's control shows the clock, its tooltip naming the schedule.
    menu.locator("[data-schedule=daily]").click()
    expect(menu).to_have_count(0)
    expect(arrow).to_have_attribute("data-tip", SCHEDULES[1])
    expect(arrow.locator("svg")).to_have_count(1)
    ha.wait(lambda: (ha.options() or {}).get("schedule") == "daily", what="the daily schedule to be saved")
    page.page.mouse.move(1, 1)
    arrow.hover()
    expect(panel.locator("#tip")).to_be_visible()
    expect(panel.locator("#tip")).to_have_text(SCHEDULES[1])
    page.shot("schedule")

    # A reload: still daily, on the control and in its menu; closing the menu changes nothing.
    page.page.reload()
    panel = page.panel
    expect(panel.locator(".flow-card")).to_be_visible()
    arrow = panel.get_by_role("button", name="When it runs, or run one step")
    expect(arrow).to_have_attribute("data-tip", SCHEDULES[1])
    expect(arrow.locator("svg")).to_have_count(1)
    page.page.mouse.move(1, 1)
    arrow.hover()
    expect(panel.locator("#tip")).to_have_text(SCHEDULES[1])
    arrow.click()
    menu = panel.locator(".split .menu")
    expect(menu.locator("[data-schedule=daily]")).to_have_class(re.compile(r"\bon\b"))
    expect(menu.locator("[data-schedule=daily] .count")).to_have_text("✓")
    expect(menu.locator("[data-schedule=off]")).not_to_have_class(re.compile(r"\bon\b"))
    page.page.keyboard.press("Escape")
    expect(menu).to_have_count(0)
    expect(arrow).to_have_attribute("data-tip", SCHEDULES[1])
    assert ha.options()["schedule"] == "daily"
    assert ha.run() is None, "Choosing a schedule starts no run"
    assert page.errors == [], (page.errors, page.rejections, page.blocked)


def test_settings_shows_each_stage_in_full(page, ha):
    """Settings, where runs are blocked, released and scheduled: its track names every stage in full, at phone and
    desktop size (DETAILS P14.9: cards are sized to their content, never overlap or get cut off, and fit any window
    width)."""
    page.goto("/")
    panel = page.open_panel()
    open_settings(page, panel)
    titles = panel.locator(".track .stn .s-title")
    expect(titles).to_have_text(["Your Assist", "Triage", "Analyze", "Propose", "Review", "Simulate", "Write up",
                                 "Your review", "Your household"])

    def cut():  # The titles shown cut short (an ellipsis), once the track has its layout.
        return titles.evaluate_all("(els) => els.filter((el) => el.scrollWidth > el.clientWidth + 1)"
                                   ".map((el) => el.textContent)")
    try:
        ha.wait(lambda: not cut(), timeout=5, what="every stage's title to show in full")
    except AssertionError:
        page.shot("cut")
        raise AssertionError(f"Settings cuts these stages' titles short at {page.size} size: {cut()}") from None
    assert page.errors == [], (page.errors, page.rejections, page.blocked)


def test_an_open_page_follows_a_run_that_starts_after_it_loaded(page, ha):
    """The home page left open on one screen while, on another (a phone, say), the household asks Assist for a lamp and
    files the conversation: the run the fast lane starts on its own shows on the open page without a reload, with
    where the conversation ended. Nothing goes up for review, so the sidebar's count doesn't change and Home Assistant
    doesn't make the panel again: the same page notices by itself (SPEC P0.7, the fast lane; DETAILS P5.9.2, a run
    refreshes itself)."""
    said = "Turn on the floor lamp"
    page.goto("/")
    panel = page.open_panel()
    expect(panel.locator(".cs-empty")).to_have_text(
        "Use Assist as usual: your conversations show up here, ready to send in.")
    panel.evaluate("(el) => { el.dataset.e2e = 'open before'; }")  # This panel, as opposed to one made again.
    same = page.page.locator("assist-improver-panel[data-e2e='open before']")

    # Elsewhere, the lamp from Assist, and the conversation filed; it ends, and the fast lane runs it.
    phone = page.elsewhere()
    phone.goto("/")
    expect(phone.assist(said)).to_contain_text("Turned on the Floor lamp")
    expect(phone.assist("File an improvement: it should say which room")).to_contain_text("Filed")
    phone.close_assist()
    run = ha.wait_for_run("filed")
    assert run["status"] == "done", run
    settled = stands(ha, said)
    assert settled["stage"] == "Your household" and settled["status"] in SETTLED, settled
    assert ha.ws.call("assist_improver/improvements")["improvements"] == []  # Nothing up for review.

    # The page open all along: the last run, and the conversation it settled, among the archived.
    expect(same.locator(".cs-last")).to_contain_text("Last run")
    expect(same.locator(".cs-show-archived")).to_have_text("Show archived (1)")
    page.show_archived()
    expect(page.chip(same.locator(".cs-row").filter(has_text=said))).to_have_text(SETTLED[settled["status"]])
    if not page.phone:
        expect(page.page.locator("ha-sidebar").get_by_text("Assist Improver", exact=True)).to_be_visible()
    assert page.errors == [], (page.errors, page.rejections, page.blocked)
