"""Journeys: the household knowledge, Your household's lines: what every assistant on Assist reads about the household.

- Lines by hand: from Settings, Your household's lines chip opens the Household knowledge modal; the household adds a
  line for everyone, edits it, adds one just for Alex and deletes it (it asks first, and Alex drops out of the view), the
  count on the chip follows each change, and the lines are as left after the page is loaded again, as Home Assistant
  saved them.
- Changed elsewhere: a line edited on another screen while the lines are open here can't be saved over: the edit says
  why and keeps what was typed, and the lines, closed and opened again as it says, are as they are now.
- What they're for: the lines reach the household's assistant at its next request (the request the stand-in model got
  carries them under the household knowledge heading: everyone's, then the speaking person's, never another person's),
  so a name only a line explains now works.
- From an improvement: a conversation sent in with a note asking Assist to remember a fact gets that fact proposed as a
  line for everyone; accepted, the line is in the modal and Assist follows it at its next request; Undo takes it out.

SPEC.md P0.11 (Your review: accept), P0.12 (Your household: improved). DETAILS P10.2.3 (every saved or accepted line
reaches every assistant on Assist at its next request), P10.2.4/P10.2.5 (lines for everyone, then the speaking
person's; another person's never), P10.4 (an improvement's line goes to everyone or the speaking person),
P10.5.5-P10.5.7 (the modal: everyone's lines, then each person's by name;
empty, it says nothing is learned yet; it closes with its close button or Esc; one line at a time added, edited in
place, or deleted after asking; text trimmed; a person with no lines left drops out), P10.5.8 (a failed edit says why
and keeps what was typed), P12.5.1 (accept applies it: "Applied. Assist uses it from your next request."), P12.7 (undo
puts back what was there).
"""

import re

from playwright.sync_api import expect

STORE = "assist_improver.household"  # The household knowledge as Home Assistant saves it (household.Household).
HEADING = "Household knowledge (facts and preferences the household has confirmed; follow them):"  # What Assist reads.
LAMP = "light.desk_lamp"  # "Desk lamp" in the Study: nothing in the household is called the reading lamp.
FLOOR = "light.floor_lamp"  # "Floor lamp" in the Living room: nothing is called the big lamp either.


# What the household does.

def plural(n, word):
    return f"{n} {word}" + ("" if n == 1 else "s")


def open_settings(page):
    """Settings, from the panel's home page (its gear)."""
    page.goto("/")
    page.open_panel()
    page.panel.get_by_role("button", name="Settings").click()
    expect(page.page).to_have_url(re.compile(r"/assist-improver\?view=settings$"))
    expect(page.panel.locator(".flow-head")).to_be_visible()


def lines_chip(page):
    """Your household's lines chip on the settings diagram."""
    return page.panel.locator('.stn[data-step="household"] [data-knowledge]')


def chip_says(page, n):
    """What the lines chip says with n lines: "n lines learned", or just "n lines" where the diagram stands upright
    (the phone)."""
    return plural(n, "line") if page.phone else f"{plural(n, 'line')} learned"


def open_lines(page):
    """The Household knowledge modal, opened from the lines chip: its locator."""
    lines_chip(page).click()
    modal = page.panel.get_by_role("dialog", name="Household knowledge")
    expect(modal).to_be_visible()
    return modal


def section(modal, name):
    """One section of the modal: Everyone, or one person's by name (its heading: the name, then how many lines)."""
    heading = modal.page.locator(".kn-group", has_text=re.compile(rf"^{name}\d*$"))
    return modal.locator(".kn-section").filter(has=heading)


def write(modal, text):
    """Types in the open line editor and saves; waits until the editor has closed (the save came back)."""
    editor = modal.locator("#kn-text")
    expect(editor).to_be_focused()
    editor.fill(text)
    modal.locator("#kn-save").click()
    expect(editor).to_have_count(0)


def add(modal, where, text):
    """Adds a line: "everyone" for everyone, or "me" for the person signed in ("Add a line just for <name>")."""
    if where == "everyone":
        section(modal, "Everyone").get_by_role("button", name="+ Add a line").click()
    else:
        modal.locator(".kn-add-me button").click()
    expect(modal.locator("#kn-text")).to_have_attribute("placeholder", "One fact or preference, in a sentence")
    write(modal, text)


def close(modal):
    modal.locator("#modal-close").click()
    expect(modal).to_have_count(0)


def say(page, words, reply):
    """One request in the Assist dialog, from the dashboard, with Assist's reply checked."""
    page.goto("/")
    expect(page.assist(words)).to_contain_text(reply)


# What Home Assistant holds, and what the household's assistant was given.

def saved(ha, what, everyone, people=None):
    """Waits until Home Assistant has saved exactly these lines: everyone's, and each person's {user id: lines}."""
    want = {"everyone": everyone, "people": people or {}}
    return ha.wait(lambda: ha.storage(STORE) == want, what=what)


def read_by_assistant(fake_model, ha, said):
    """What the household's assistant was told with its last request answering `said` (the stand-in's log): its
    system prompt, where Assist's instructions are."""
    for record in reversed(fake_model.requests[ha.since:]):
        request = record["request"]
        if record["role"] != "household":
            continue
        users = [m for m in request.get("messages") or [] if m.get("role") == "user"]
        content = users[-1].get("content") if users else None
        words = content if isinstance(content, str) else "".join(
            b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text")
        if words.strip() == said:
            system = request.get("system") or ""
            return system if isinstance(system, str) else "\n".join(b.get("text", "") for b in system)
    raise AssertionError(f"The household's assistant was never asked {said!r}")


def knowledge_read(system):
    """The household knowledge lines Assist was given (after its heading), or None without the heading."""
    if HEADING not in system:
        return None
    lines = []
    for line in system.split(HEADING, 1)[1].splitlines()[1:]:
        if not line.startswith("- "):
            break
        lines.append(line[2:])
    return lines


def sidebar_count(page, n):
    """Waits until Home Assistant's sidebar names Assist Improver with n improvements waiting (the panel is made again
    when that count changes, so a journey waits for it before acting on the new panel)."""
    item = page.page.locator("ha-sidebar").get_by_text(re.compile(r"^\s*Assist Improver(\s+\(\d+\))?\s*$")).first
    expect(item).to_have_text(re.compile(rf"^\s*Assist Improver{rf'\s+\({n}\)' if n else ''}\s*$"))


# The journeys.

def test_lines_by_hand(page, ha):
    """Added, edited and deleted in the modal, one line at a time; the chip's count follows; kept as left."""
    me = ha.ws.call("auth/current_user")["id"]
    open_settings(page)

    # The lines' chip says nothing is learned yet.
    expect(lines_chip(page)).to_have_text(chip_says(page, 0))
    modal = open_lines(page)
    expect(modal.locator(".kn-count")).to_have_text("0 lines")
    expect(modal.locator(".kn-empty")).to_have_text("Nothing learned yet: accepted improvements add lines here.")
    expect(modal.locator(".kn-add-me button")).to_have_text("+ Add a line just for Alex")

    # A line for everyone, typed with spaces around it: kept trimmed, counted on the modal and on the chip.
    add(modal, "everyone", "   The study is Alex's office.  ")
    everyone = section(modal, "Everyone")
    expect(everyone.locator(".kn-text")).to_have_text(["The study is Alex's office."])
    expect(modal.locator(".kn-empty")).to_have_count(0)
    expect(modal.locator(".kn-count")).to_have_text("1 line")
    expect(lines_chip(page)).to_have_text(chip_says(page, 1))
    saved(ha, "the line saved", ["The study is Alex's office."])

    # Edited in place: the editor opens with the line as it is.
    everyone.get_by_role("button", name="Edit").click()
    expect(modal.locator("#kn-text")).to_have_value("The study is Alex's office.")
    write(modal, "The study is Alex's office, upstairs.")
    expect(everyone.locator(".kn-text")).to_have_text(["The study is Alex's office, upstairs."])
    expect(modal.locator(".kn-count")).to_have_text("1 line")
    saved(ha, "the line edited", ["The study is Alex's office, upstairs."])

    # A line just for Alex: his own section, by name, after everyone's.
    add(modal, "me", "Alex likes the desk lamp warm.")
    mine = section(modal, "Alex")
    expect(modal.locator(".kn-group")).to_have_text(["Everyone1", "Alex1"])
    expect(mine.locator(".kn-text")).to_have_text(["Alex likes the desk lamp warm."])
    expect(modal.locator(".kn-add-me")).to_have_count(0)
    expect(modal.locator(".kn-count")).to_have_text("2 lines")
    expect(lines_chip(page)).to_have_text(chip_says(page, 2))
    saved(ha, "Alex's line saved", ["The study is Alex's office, upstairs."], {me: ["Alex likes the desk lamp warm."]})
    page.shot("knowledge-lines")

    # Deleted after asking: Cancel keeps it; Delete takes it, and Alex, with no lines left, drops out of the view.
    mine.get_by_role("button", name="Delete").click()
    confirm = mine.locator(".kn-confirm")
    expect(confirm).to_contain_text("Delete this line?")
    confirm.get_by_role("button", name="Cancel").click()
    expect(confirm).to_have_count(0)
    expect(mine.locator(".kn-text")).to_have_text(["Alex likes the desk lamp warm."])
    mine.get_by_role("button", name="Delete").click()
    confirm.get_by_role("button", name="Delete").click()
    expect(mine).to_have_count(0)
    expect(modal.locator(".kn-add-me button")).to_have_text("+ Add a line just for Alex")
    expect(modal.locator(".kn-count")).to_have_text("1 line")
    expect(lines_chip(page)).to_have_text(chip_says(page, 1))
    saved(ha, "Alex's line deleted", ["The study is Alex's office, upstairs."])
    close(modal)

    # Loaded again: the chip and the modal show the lines as left.
    page.page.reload()
    expect(lines_chip(page)).to_have_text(chip_says(page, 1))
    modal = open_lines(page)
    expect(modal.locator(".kn-count")).to_have_text("1 line")
    expect(modal.locator(".kn-group")).to_have_text(["Everyone1"])
    expect(section(modal, "Everyone").locator(".kn-text")).to_have_text(["The study is Alex's office, upstairs."])
    page.page.keyboard.press("Escape")
    expect(modal).to_have_count(0)
    assert page.errors == [], (page.errors, page.rejections, page.blocked)


def test_lines_reach_the_assistant(page, ha, fake_model):
    """Everyone's lines, then the speaking person's, reach the household's assistant at its next request, and it acts
    on them; another person's never do."""
    me = ha.ws.call("auth/current_user")["id"]
    say(page, "Turn on the reading lamp", "couldn't find")
    assert knowledge_read(read_by_assistant(fake_model, ha, "Turn on the reading lamp")) is None, "Nothing learned yet"

    # Jordan, another person in the household, has a line of her own (saved with the command the panel saves lines with).
    jordan = ha.ws.call("config/auth/create", name="Jordan", group_ids=["system-users"])["user"]["id"]
    ha.ws.call("assist_improver/household/edit", scope=jordan, action="add", text="The big lamp is the desk lamp.")

    # Alex adds a line for everyone and one just for himself.
    open_settings(page)
    modal = open_lines(page)
    expect(modal.locator(".kn-group")).to_have_text(["Everyone", "Jordan1"])
    add(modal, "everyone", "The reading lamp is the desk lamp in the study.")
    add(modal, "me", "The big lamp is the floor lamp.")
    expect(modal.locator(".kn-group")).to_have_count(3)  # Everyone's first, then each person's, by name.
    expect(modal.locator(".kn-group").first).to_have_text("Everyone1")
    expect(section(modal, "Alex").locator(".kn-text")).to_have_text(["The big lamp is the floor lamp."])
    expect(section(modal, "Jordan").locator(".kn-text")).to_have_text(["The big lamp is the desk lamp."])
    saved(ha, "the lines saved", ["The reading lamp is the desk lamp in the study."],
          {me: ["The big lamp is the floor lamp."], jordan: ["The big lamp is the desk lamp."]})
    close(modal)

    # At Alex's next request, Assist reads everyone's line, then his own, and not Jordan's: so it knows which lamp he means.
    say(page, "Turn on the reading lamp", "Turned on the Desk lamp")
    told = read_by_assistant(fake_model, ha, "Turn on the reading lamp")
    assert knowledge_read(told) == ["The reading lamp is the desk lamp in the study.",
                                    "The big lamp is the floor lamp."], told
    assert "The big lamp is the desk lamp." not in told
    expect(page.assist("Turn on the big lamp")).to_contain_text("Turned on the Floor lamp")
    page.close_assist()
    ha.wait(lambda: ha.state(LAMP)["state"] == "on" and ha.state(FLOOR)["state"] == "on", what="both lamps on")
    assert page.errors == [], (page.errors, page.rejections, page.blocked)


def test_line_changed_elsewhere(page, ha):
    """A line changed on another screen while the lines are open here: saving an edit here says why it failed and
    keeps what was typed; closed and opened again, as it says, the lines are as they are now, and the edit saves."""
    ha.ws.call("assist_improver/household/edit", scope="everyone", action="add", text="The kettle is in the kitchen.")
    open_settings(page)
    modal = open_lines(page)
    everyone = section(modal, "Everyone")
    expect(everyone.locator(".kn-text")).to_have_text(["The kettle is in the kitchen."])
    everyone.get_by_role("button", name="Edit").click()
    expect(modal.locator("#kn-text")).to_have_value("The kettle is in the kitchen.")

    # Meanwhile, on Alex's phone, the same line is edited.
    phone = page.elsewhere()
    open_settings(phone)
    there = open_lines(phone)
    section(there, "Everyone").get_by_role("button", name="Edit").click()
    write(there, "The kettle is on the kitchen counter.")
    saved(ha, "the line edited on the phone", ["The kettle is on the kitchen counter."])
    phone.page.close()

    # Here, the edit can't save over it: it says why and keeps what was typed; nothing changed in Home Assistant.
    modal.locator("#kn-text").fill("The kettle is in the kitchen, by the window.")
    modal.locator("#kn-save").click()
    expect(modal.locator(".kn-editor .error-text")).to_have_text(
        "That line has changed since you opened it. Close and open it again.")
    expect(modal.locator("#kn-text")).to_have_value("The kettle is in the kitchen, by the window.")
    assert ha.storage(STORE) == {"everyone": ["The kettle is on the kitchen counter."], "people": {}}

    # Closed and opened again: the line as it is now, and editing it saves.
    close(modal)
    modal = open_lines(page)
    everyone = section(modal, "Everyone")
    expect(everyone.locator(".kn-text")).to_have_text(["The kettle is on the kitchen counter."])
    everyone.get_by_role("button", name="Edit").click()
    expect(modal.locator("#kn-text")).to_have_value("The kettle is on the kitchen counter.")
    write(modal, "The kettle is on the kitchen counter, by the window.")
    expect(everyone.locator(".kn-text")).to_have_text(["The kettle is on the kitchen counter, by the window."])
    saved(ha, "the edit saved", ["The kettle is on the kitchen counter, by the window."])
    assert page.errors == [], (page.errors, page.rejections, page.blocked)


ASKED = "Turn on the reading lamp"
REMEMBER = "File an improvement: remember that the reading lamp is the desk lamp"
LINE = "The reading lamp is the desk lamp."  # The fact the note asked Assist to remember, as a line for everyone.
TITLE = "Remember that the reading lamp is the desk lamp"


def open_review(page):
    """The improvement's review, from the panel's home page, where it waits up for review: the panel."""
    page.goto("/")
    panel = page.open_panel()
    expect(panel.locator("h1").filter(has_text="1 improvement to review")).to_be_visible()
    panel.locator(".item").filter(has_text=TITLE).click()
    expect(page.page).to_have_url(re.compile(r"/assist-improver\?id="))
    expect(panel.locator("h1")).to_contain_text(TITLE)
    return panel


def test_accepted_knowledge(page, ha):
    """A household line an accepted improvement adds is in the modal and Assist follows it; Undo takes it out."""
    say(page, ASKED, "couldn't find")
    expect(page.assist(REMEMBER)).to_contain_text("Filed")
    page.close_assist()
    run = ha.wait_for_run("filed")
    assert run["status"] == "done", run

    # Up for review: what changes is the household knowledge, one line for everyone; nothing is applied yet.
    sidebar_count(page, 1)
    panel = open_review(page)
    change = panel.locator("details[data-key=changes] .file")
    expect(change).to_have_count(1)
    expect(change.locator("summary .name")).to_have_text("Household knowledge")
    expect(change.locator("tr.add .text")).to_have_text([f"- {LINE}"])
    expect(panel.locator(".conflict")).to_have_count(0)
    assert ha.storage(STORE) in (None, {"everyone": [], "people": {}}), ha.storage(STORE)

    # Accepted: the line is in the household knowledge, for everyone, and nothing else changed.
    panel.locator(".footer #decide").click()
    expect(panel.locator(".footer .done-msg")).to_contain_text("Applied. Assist uses it from your next request.")
    saved(ha, "the accepted line", [LINE])
    assert ha.aliases(LAMP) == [] and ha.state(LAMP)["state"] == "off"
    sidebar_count(page, 0)
    page.shot("knowledge-accepted")

    # Settings: the chip counts it, and the modal shows it under Everyone.
    open_settings(page)
    expect(lines_chip(page)).to_have_text(chip_says(page, 1))
    modal = open_lines(page)
    expect(section(modal, "Everyone").locator(".kn-text")).to_have_text([LINE])
    close(modal)

    # Assist follows it from the next request: the reading lamp is the desk lamp.
    say(page, ASKED, "Turned on the Desk lamp")
    page.close_assist()
    ha.wait(lambda: ha.state(LAMP)["state"] == "on", what="the desk lamp on")

    # Undone from its review: the line is gone from the household knowledge, the chip and the modal.
    page.open_panel()
    history = page.panel.locator("details[data-key=history]")
    history.locator(":scope > summary").click()
    history.locator(".item").filter(has_text=TITLE).click()
    expect(page.panel.locator("h1 .chip")).to_have_text("Applied")
    page.panel.locator(".footer #undo-link").click()
    expect(page.panel.locator(".footer .done-msg")).to_contain_text("Undone: what was there before is back.")
    saved(ha, "the line taken out", [])
    sidebar_count(page, 1)
    open_settings(page)
    expect(lines_chip(page)).to_have_text(chip_says(page, 0))
    modal = open_lines(page)
    expect(modal.locator(".kn-empty")).to_have_text("Nothing learned yet: accepted improvements add lines here.")
    close(modal)
    assert page.errors == [], (page.errors, page.rejections, page.blocked)
