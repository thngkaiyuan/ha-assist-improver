"""Journeys: Alex decides on an improvement at Your review, from its decision bar, and the panel and Home Assistant
show what each decision did.

The improvement gets there as in test_file_from_assist.py: Alex asks Assist for the "reading lamp" (nothing is called
that), files an improvement from the Assist dialog, the conversation ends and the fast lane takes it in, and an alias
for the desk lamp goes up for review. Then:

- Accept: the alias is in Home Assistant's entity registry, nothing ran, the conversation is Improved (in Your
  household, out of the list), and the footer offers Undo; Undo takes the alias out again and the improvement is back
  up for review, its conversation at Your review again.
- Accept a script (the note asks for a "reading lamp script"): the panel saves it through Home Assistant's script
  editor, where it then is, loaded, exposed and never run; Undo deletes it there.
- Accept a scene and an automation setting it (the note asks for a "reading lamp scene" set at 21:00): the review says
  when the automation runs and what its simulated week did; the panel saves the scene, then the automation, through
  Home Assistant's own editors, and nothing runs; Undo deletes the automation first, then the scene.
- Ask for a revision, with a note: the conversation goes straight back in for a round of its own, read afresh with the
  note as its last turn (one only about the fix, with no goal), and a revised version comes up for review that says
  what it revises and answers the note, with no verdict on it; the earlier version is replaced and closed.
- Decline, ticking Not an issue or with a note of its own: the improvement is declined, the conversation is declined and
  archived, the history keeps the reason, and nothing in Home Assistant changed.
- Accept after the desk lamp's entity ID was changed in Home Assistant: a conflict, so nothing is written; the review
  says why and asks at once to send it back to resolve it, and nothing goes back in until Alex agrees. Agreed, the
  conversation goes in at Propose for round 2 on a run of its own, and its new version, on the desk lamp's new entity
  ID, comes up for review.

SPEC.md P0.6 (a revision, or a conflict sent back, starts a new round), P0.7 (a revision goes straight back in at
Propose; an accept that meets a conflict says so and asks before it goes back in), P0.11 (accept, revise or decline;
decline declines and archives, nothing more), P0.12 (Your household: improved, declined). DETAILS P12.4.3-5 (the note
box and its placeholders, Not an issue), P12.4.8 (the decisions open follow the status), P12.4.10 (history names the
decision and its reason), P12.4.11-13 (decline and revise change nothing in Home Assistant; after Decline: "Thanks."),
P12.4.16 (after an accept, Undo one click away; after the last decision, back to the list), P12.5/P12.5.1 (accept
installs the change, nothing runs; "Applied. Assist uses it from your next request."), P12.6.1-3 (a conflict says it
can't apply now and why; agreed, it goes in at Propose on a run of its own), P12.7/P12.7.4 (undo puts back what was
there; an undone improvement is up for review again, its conversation at Your review), P12.8/P12.8.4/P12.8.5 (a
revision runs at once; the revised version carries a Revision tag and the note it answers; the one it replaces shows
"Replaced by a revision" and is closed), P14.3.9/P14.3.12/P14.3.13 (Up for review, Improved, Declined), P14.4.1/P14.4.2
(settled conversations leave the list, shown dimmed under Show archived), P14.11.5 (the sidebar's count).
"""

import re

import pytest
from playwright.sync_api import expect

LAMP = "light.desk_lamp"  # "Desk lamp" in the Study: nothing in the household is called the reading lamp.
HELPER = "input_boolean.desk_lamp_power"  # What the simulated lamp switches.
ASKED = "Turn on the reading lamp"
FILED = "File an improvement: the reading lamp is the desk lamp in the study"
TITLE = "Call the desk lamp the reading lamp"  # The improvement's headline.
NOTE = 'Also call it "study lamp"'  # The revision note: one more name for it.
REVISED = "Call the desk lamp the reading lamp or the study lamp"
MOVED = "light.study_desk_lamp"  # The desk lamp's entity ID, as someone changes it in Home Assistant's settings.
SCRIPTED = "File an improvement: a reading lamp script should turn on the desk lamp in the study"
SCRIPT = "script.reading_lamp"  # The script the stand-in proposer adds for that note: "Reading lamp".
SCENED = ("File an improvement: a reading lamp scene should turn on the desk lamp in the study, set every evening at "
          "21:00")
SCENE = "scene.reading_lamp"  # What the stand-in proposer adds for that note: the scene "Reading lamp", and
AUTOMATION = "automation.reading_lamp_at_21_00"  # the automation "Reading lamp at 21:00" setting it.


def improvement_id(page):
    """The improvement the review on screen is about, from its address (/assist-improver?id=...)."""
    found = re.search(r"[?&]id=([^&#]+)", page.page.url)
    assert found, page.page.url
    return found.group(1)


def sidebar_count(page, n):
    """Wait until Home Assistant's sidebar names Assist Improver with n improvements waiting (P14.11.5): the panel is
    made again when that count changes, so a journey waits for it before acting on the new panel. The sidebar is
    in the page at both sizes (behind its toggle on the phone)."""
    item = page.page.locator("ha-sidebar").get_by_text(re.compile(r"^\s*Assist Improver(\s+\(\d+\))?\s*$")).first
    count = rf"\s+\({n}\)" if n else ""
    expect(item).to_have_text(re.compile(rf"^\s*Assist Improver{count}\s*$"))


def up_for_review(page, ha):
    """A conversation whose improvement waits at Your review, reached as Alex would (test_file_from_assist.py): asked
    for in the Assist dialog by a name Assist doesn't know, filed from there, taken in by the fast lane once it ends.
    Then the improvement is opened from the top of the panel's home page. The panel, on that review."""
    assert ha.aliases(LAMP) == [], "The desk lamp starts with no other names"
    page.goto("/")
    expect(page.assist(ASKED)).to_contain_text("couldn't find")
    expect(page.assist(FILED)).to_contain_text("Filed")
    page.close_assist()
    run = ha.wait_for_run("filed")
    assert run["status"] == "done", run
    sidebar_count(page, 1)
    panel = page.open_panel()
    expect(panel.locator("h1").filter(has_text="1 improvement to review")).to_be_visible()
    panel.locator(".item").filter(has_text=TITLE).click()
    expect(page.page).to_have_url(re.compile(r"/assist-improver\?id="))
    expect(panel.locator("h1")).to_contain_text(TITLE)
    expect(panel.locator("h1 .chip")).to_have_text("To review")
    expect(panel.locator(".footer #decide")).to_have_text("Accept changes")
    return panel


def choose(panel, decision):
    """Pick a decision from the decision bar's arrow (Ask for a revision, Decline): it becomes the main button."""
    panel.locator(".footer #arrow").click()
    panel.locator(f".footer .menu [data-choice={decision}]").click()
    expect(panel.locator(".footer .menu")).to_have_count(0)


def open_earlier(page, panel, title):
    """From the home page: Earlier improvements, unfolded, and the one with this title opened."""
    history = panel.locator("details[data-key=history]")
    if history.get_attribute("open") is None:
        history.locator(":scope > summary").click()
    expect(history).to_have_attribute("open", "")
    history.locator(".item").filter(has_text=title).click()
    expect(page.page).to_have_url(re.compile(r"/assist-improver\?id="))


def back_home(page, panel):
    """Back from a review to the panel's home page (the toolbar's back arrow)."""
    panel.locator(".toolbar #back").click()
    expect(page.page).to_have_url(re.compile(r"/assist-improver/?$"))
    expect(panel.locator(".cs-title")).to_have_text("Your conversations")


def history_rows(page, panel):
    """Settings › History, from the home page's gear: its rows, newest first."""
    panel.locator("#open-settings").click()
    expect(page.page).to_have_url(re.compile(r"view=settings"))
    rows = panel.locator(".history .h-row")
    expect(rows.first).to_be_visible()
    return rows


def stands(ha, what, **fields):
    """The conversation's one record of where it stands (SPEC P0.2), as Home Assistant saved it, once it has these
    fields (stage, outcome, archived, round). The household's only conversation in these journeys."""
    def record():
        conversations = ((ha.storage("assist_improver.flow") or {}).get("conversations") or {}).values()
        conv = next(iter(conversations), None)
        return conv if conv and all(conv.get(k) == v for k, v in fields.items()) else None
    return ha.wait(record, what=what)


def unchanged(ha, page):
    """Nothing of the improvement is in Home Assistant, and no device moved."""
    assert ha.aliases(LAMP) == []
    assert ha.state(LAMP)["state"] == "off"
    assert ha.state(HELPER)["state"] == "off"
    assert page.errors == [], (page.errors, page.rejections, page.blocked)


def test_accept_then_undo(page, ha):
    panel = up_for_review(page, ha)

    # Accept changes, the default: the alias is written, nothing runs, and the footer says so, with Undo beside it.
    panel.locator(".footer #decide").click()
    done = panel.locator(".footer .done-msg")
    expect(done).to_contain_text("Applied. Assist uses it from your next request.")
    expect(done.locator("#undo-link")).to_have_text("Undo")
    expect(panel.locator("h1 .chip")).to_have_text("Applied")
    ha.wait(lambda: ha.aliases(LAMP) == ["Reading lamp"], what="the desk lamp's new alias")
    assert ha.state(LAMP)["state"] == "off" and ha.state(HELPER)["state"] == "off", "Accepting runs nothing"
    sidebar_count(page, 0)  # Nothing waits any more (the panel is made again: the footer still says what happened).
    expect(done).to_contain_text("Applied.")
    page.shot("accepted")

    # Caught up: back to the list. The improvement is under Earlier improvements, applied; the conversation is out of
    # the list, Improved, in Your household.
    expect(panel.locator("#back-list")).to_have_text("All caught up · back to the list")
    panel.locator("#back-list").click()
    expect(panel.locator("h1").filter(has_text="to review")).to_have_count(0)
    history = panel.locator("details[data-key=history]")
    expect(history.locator(":scope > summary")).to_have_text("Earlier improvements (1)")
    row = page.conversation_row(ASKED)
    expect(row).to_have_count(0)
    page.show_archived()
    expect(page.chip(row)).to_have_text("Improved")
    expect(row).to_have_class(re.compile(r"\bcs-archived\b"))
    stands(ha, "the conversation improved, in Your household", stage="Your household", outcome="improved")
    page.shot("improved")

    # Back to its review: Undo is one click away. Undone, the alias is gone and the improvement is up for review
    # again, to accept again, revise or decline.
    open_earlier(page, panel, TITLE)
    expect(panel.locator("h1 .chip")).to_have_text("Applied")
    panel.locator(".footer #undo-link").click()
    expect(panel.locator(".footer .done-msg")).to_contain_text("Undone: what was there before is back.")
    expect(panel.locator("h1 .chip")).to_have_text("To review")
    ha.wait(lambda: ha.aliases(LAMP) == [], what="the alias to be taken out")
    sidebar_count(page, 1)
    # No decision pops up where Undo was: Decide again, beside what happened, brings the decision bar back.
    expect(panel.locator(".footer #decide")).to_have_count(0)
    expect(panel.locator(".footer #undo-link")).to_have_count(0)
    panel.locator(".footer #again-link").click()
    expect(panel.locator(".footer #decide")).to_have_text("Accept changes")
    panel.locator(".footer #arrow").click()
    expect(panel.locator(".footer .menu [data-choice]")).to_have_text(["Accept changes", "Ask for a revision", "Decline"])
    panel.locator(".footer #arrow").click()
    page.shot("undone")

    # Its conversation stands at Your review again: Up for review, not Improved or Declined.
    back_home(page, panel)
    expect(panel.locator("h1").filter(has_text="1 improvement to review")).to_be_visible()
    page.show_archived()
    expect(page.chip(row)).to_have_text("Up for review")  # Its one chip: not Improved or Declined.
    expect(row.locator(".cs-review")).to_have_text("Up for review")
    stands(ha, "the conversation back at Your review", stage="Your review", outcome=None, archived=False)
    unchanged(ha, page)


def in_script_editor(ha):
    """The script as Home Assistant's script editor opens it (GET /api/config/script/config/<id>), or None."""
    found = ha.rest("GET", f"/api/config/script/config/{SCRIPT.split('.', 1)[1]}", ok=(200, 404))
    return found if "sequence" in (found or {}) else None


def test_accept_then_undo_a_script(page, ha):
    """A script, which the panel saves through Home Assistant's own script editor as the signed-in admin (the improver
    never writes a configuration file itself): accepted, it's in HA's script editor and its list, loaded, exposed to
    Assist and never run; undone, it's gone from all of them."""
    assert in_script_editor(ha) is None
    page.goto("/")
    expect(page.assist(ASKED)).to_contain_text("couldn't find")
    expect(page.assist(SCRIPTED)).to_contain_text("Filed")
    page.close_assist()
    run = ha.wait_for_run("filed")
    assert run["status"] == "done", run
    sidebar_count(page, 1)
    panel = page.open_panel()
    panel.locator(".item").filter(has_text="reading lamp script").click()
    expect(page.page).to_have_url(re.compile(r"/assist-improver\?id="))
    review = improvement_id(page)
    expect(panel.locator(".footer #decide")).to_have_text("Accept changes")
    saves = []  # What the page asked Home Assistant's script editor to save or delete.
    page.page.on("request", lambda r: r.method in ("POST", "DELETE") and "/api/config/script/config/" in r.url
                 and saves.append((r.method, r.url.rsplit("/", 1)[1])))

    panel.locator(".footer #decide").click()
    done = panel.locator(".footer .done-msg")
    expect(done).to_contain_text("Applied. Assist uses it from your next request; no script was run.")
    assert in_script_editor(ha) == {"alias": "Reading lamp", "description": f"Turns on {LAMP}.",
                                    "sequence": [{"action": "light.turn_on", "target": {"entity_id": LAMP}}]}
    assert saves == [("POST", "reading_lamp")]  # Saved from the page, as the signed-in admin.
    assert ha.state(SCRIPT)["state"] == "off" and ha.exposed(SCRIPT)  # Loaded and exposed, never run.
    assert ha.state(LAMP)["state"] == "off" and ha.state(HELPER)["state"] == "off"
    sidebar_count(page, 0)
    page.goto("/config/script/dashboard")  # Home Assistant's own list of scripts.
    expect(page.page.get_by_text("Reading lamp", exact=True).first).to_be_visible()
    page.shot("script-accepted")

    page.goto(f"/assist-improver?id={review}")
    expect(page.panel.locator("h1 .chip")).to_have_text("Applied")
    # The socket drops and comes back under the open panel (as across a restart): the panel subscribes again to undo.
    page.page.evaluate("""() => new Promise((ready) => {
      const connection = document.querySelector("home-assistant").hass.connection;
      connection.addEventListener("ready", ready);
      connection.reconnect(true);
    })""")
    page.panel.locator(".footer #undo-link").click()
    expect(page.panel.locator(".footer .done-msg")).to_contain_text("Undone: what was there before is back.")
    ha.wait(lambda: in_script_editor(ha) is None and "state" not in (ha.state(SCRIPT) or {}),
            what="the script to be gone")
    assert saves == [("POST", "reading_lamp"), ("DELETE", "reading_lamp")]
    page.goto("/config/script/dashboard")
    expect(page.page.locator("ha-data-table, hass-tabs-subpage-data-table").first).to_be_attached()
    expect(page.page.get_by_text("Reading lamp", exact=True)).to_have_count(0)
    assert ha.state(LAMP)["state"] == "off"
    # Home Assistant's own frontend, after the reconnect, ends one of its subscriptions by the ID it had before.
    stale = '{"code":"not_found","message":"Subscription not found."}'
    assert all(stale in r for r in page.rejections) and len(page.errors) == len(page.rejections), (
        page.errors, page.rejections, page.blocked)


def in_editor(ha, kind, item_id):
    """An item as Home Assistant's editor for its kind opens it (GET /api/config/<kind>/config/<id>), or None."""
    found = ha.rest("GET", f"/api/config/{kind}/config/{item_id}", ok=(200, 404))
    return found if isinstance(found, dict) and "message" not in found else None


def test_accept_then_undo_a_scene_and_an_automation(page, ha):
    """Kai's words: "An automation's review says when it would run and what it would have done over a simulated week."
    The stand-in proposes a scene and an automation setting it at 21:00: the review says when it runs and its week,
    counts and times first, each run on demand. Accepted, the panel saves the scene, then the automation, through Home
    Assistant's own editors (DETAILS P12.5.3, P12.5.9), where both are, loaded, and nothing ran; undone, the automation
    goes first, then the scene, and neither is left."""
    assert in_editor(ha, "scene", "reading_lamp") is None and in_editor(ha, "automation", "reading_lamp_at_21") is None
    page.goto("/")
    expect(page.assist(ASKED)).to_contain_text("couldn't find")
    expect(page.assist(SCENED)).to_contain_text("Filed")
    page.close_assist()
    run = ha.wait_for_run("filed", timeout=180)
    assert run["status"] == "done", run
    sidebar_count(page, 1)
    panel = page.open_panel()
    panel.locator(".item").filter(has_text="reading lamp scene").click()
    review = improvement_id(page)
    week = panel.locator(".autos > li")
    expect(week).to_have_count(1)
    expect(week.locator(".what .name")).to_have_text("Reading lamp at 21:00")
    expect(week.locator(".kind")).to_have_text("new")
    expect(week.locator(".when")).to_have_text("At 21:00")
    expect(week.locator(".week")).to_have_text("7 runs at 21:00")
    week.locator("summary").click()
    expect(week.locator(".week-run")).to_have_count(7)
    expect(week.locator(".week-run").first).to_contain_text("Desk lamp: off → on")
    page.shot("scene-automation-review")
    saves = []  # What the page asked Home Assistant's editors to save or delete.
    page.page.on("request", lambda r: r.method in ("POST", "DELETE") and "/api/config/" in r.url
                 and "/config/" in r.url.split("/api/config/", 1)[1] and saves.append(
                     (r.method, *r.url.split("/api/config/", 1)[1].split("/config/"))))

    panel.locator(".footer #decide").click()
    expect(panel.locator(".footer .done-msg")).to_contain_text(
        "Applied. The automation runs when it's triggered.")
    assert saves == [("POST", "scene", "reading_lamp"), ("POST", "automation", "reading_lamp_at_21")]
    assert in_editor(ha, "scene", "reading_lamp")["entities"] == {LAMP: {"state": "on"}}
    assert in_editor(ha, "automation", "reading_lamp_at_21")["triggers"] == [{"trigger": "time", "at": "21:00:00"}]
    assert ha.state(SCENE)["state"] == "unknown" and ha.exposed(SCENE)  # Loaded and exposed, never set.
    assert ha.state(AUTOMATION)["state"] == "on"  # Live: it runs at 21:00.
    assert ha.state(LAMP)["state"] == "off"

    page.goto(f"/assist-improver?id={review}")
    expect(page.panel.locator("h1 .chip")).to_have_text("Applied")
    page.panel.locator(".footer #undo-link").click()
    expect(page.panel.locator(".footer .done-msg")).to_contain_text("Undone: what was there before is back.")
    ha.wait(lambda: "state" not in (ha.state(AUTOMATION) or {}) and "state" not in (ha.state(SCENE) or {}),
            what="the automation and the scene to be gone")
    assert saves[2:] == [("DELETE", "automation", "reading_lamp_at_21"), ("DELETE", "scene", "reading_lamp")]
    assert in_editor(ha, "scene", "reading_lamp") is None and in_editor(ha, "automation", "reading_lamp_at_21") is None
    assert ha.state(LAMP)["state"] == "off"


def test_revise_with_note(page, ha):
    panel = up_for_review(page, ha)
    earlier = improvement_id(page)

    # Ask for a revision: it needs a note on what should change, and the main button says what it will do.
    choose(panel, "revise")
    note = panel.locator(".footer #note")
    expect(note).to_have_attribute("placeholder", "What should change? (needed for the revision)")
    expect(panel.locator(".footer #decide")).to_have_text("Ask for a revision")
    note.fill(NOTE)
    panel.locator(".footer #decide").click()
    expect(panel.locator(".footer .done-msg")).to_contain_text("Thanks.")
    # Where Ask for a revision was, no decision: this version is being revised (or replaced already, if its round
    # was quicker), with nothing to decide on it meanwhile.
    expect(panel.locator(".footer #decide")).to_have_count(0)
    expect(panel.locator("h1 .chip")).to_have_text(re.compile(r"^(Being revised|Replaced by a revision)$"))
    page.shot("revision-asked")

    # The conversation goes straight back in, on a run of its own (nobody pressed Run), and a revised version is
    # proposed, tested and written up. Nothing changed in Home Assistant.
    run = ha.wait_for_run("revision")
    assert run["status"] == "done", run
    assert (run.get("progress") or {}).get("new_improvements") == 1, run
    assert ha.aliases(LAMP) == []

    # It was a new round, admitted straight at Propose with Alex's note and the version it revises, whose first act read
    # the conversation afresh, the note its last turn, with its goal (DETAILS P12.8.1).
    conv = stands(ha, "the conversation's second round, at Your review", round=2, stage="Your review")
    records = conv["rounds"]["2"]["records"]
    assert list(records)[0] == "Propose" and conv["rounds"]["2"]["path"][0] == "Propose", conv["rounds"]["2"]
    assert "Triage" not in records, list(records)
    analysis = records["Analyze"]["analysis"]
    last = analysis["turns"] + len(analysis["notes"])  # A note only about the fix: listed apart, with no goal.
    assert analysis["notes"][-1] == {"revision": 2} and analysis["about_the_fix"] == [{"turn": last, "what": NOTE}]
    assert not [g for g in analysis["goals"] if g["turn"] == last], analysis
    assert records["Propose"]["revision"]["note"] == NOTE, records["Propose"]
    assert records["Propose"]["revision"]["improvement"] == earlier, records["Propose"]

    # Back home: the revised version waits for review with its Revision tag; the earlier one is replaced.
    back_home(page, panel)
    expect(panel.locator("h1").filter(has_text="1 improvement to review")).to_be_visible()
    pending = panel.locator(".item").filter(has=page.page.locator(".go"))  # Up for review: it opens.
    expect(pending).to_have_count(1)
    expect(pending.locator(".title")).to_contain_text(REVISED)
    expect(pending.locator(".tag")).to_have_text("Revision")
    history = panel.locator("details[data-key=history]")
    expect(history.locator(":scope > summary")).to_have_text("Earlier improvements (1)")
    history.locator(":scope > summary").click()
    expect(history.locator(".item").filter(has_text=TITLE).locator(".chip")).to_have_text("Replaced by a revision")
    sidebar_count(page, 1)

    # The conversation stands at Your review with the revised version: its chip opens that one.
    row = page.conversation_row(ASKED)
    page.show_archived()
    expect(row.locator(".cs-review")).to_have_text("Up for review")
    row.locator(".cs-review").click()
    expect(page.page).to_have_url(re.compile(r"/assist-improver\?id="))
    assert improvement_id(page) != earlier

    # The revised version says what it revises and the note it answers, and its change answers the note: the name
    # Alex said, and the one his note asked for.
    expect(panel.locator("h1")).to_contain_text(REVISED)
    expect(panel.locator("h1 .chip")).to_have_text("To review")
    expect(panel.locator(".revises")).to_have_text(f"Revised after your note on “{TITLE}”: “{NOTE}”")
    change = panel.locator("details[data-key=changes] .file")
    expect(change).to_have_count(1)
    expect(change.locator("summary .name")).to_have_text("Desk lamp")
    expect(change.locator("tr.add .text")).to_have_text(["Reading lamp", "Study lamp"])
    # The note, only about the fix, is shown with the version that answers it, with no verdict (DETAILS P12.8.1).
    expect(panel.locator(".rp-open .line").filter(has_text=NOTE)).to_have_count(0)
    expect(panel.locator(".outcome, .tests .result")).not_to_contain_text(["follows your feedback"] * 2)
    expect(panel.locator(".footer #decide")).to_have_text("Accept changes")
    page.shot("revised")

    # The earlier version is closed: replaced by its revision, with no decision bar.
    back_home(page, panel)
    open_earlier(page, panel, TITLE)
    assert improvement_id(page) == earlier
    expect(panel.locator("h1 .chip")).to_have_text("Replaced by a revision")
    expect(panel.locator(".footer #decide")).to_have_count(0)
    expect(panel.locator(".revises")).to_have_count(0)

    # The history says what happened: the revision asked for, and its run.
    back_home(page, panel)
    rows = history_rows(page, panel)
    expect(rows.filter(has_text="Asked for a revision")).to_have_count(1)
    expect(rows.filter(has_text="Run finished · revision")).to_have_count(1)
    unchanged(ha, page)


@pytest.mark.parametrize("why", ["not-an-issue", "with-a-note"])
def test_decline(page, ha, why):
    panel = up_for_review(page, ha)
    declined = improvement_id(page)

    # Decline: its note is optional, with Not an issue to tick above it; ticked, the note asks only for anything to add.
    choose(panel, "decline")
    expect(panel.locator(".footer #decide")).to_have_text("Decline")
    note = panel.locator(".footer #note")
    expect(note).to_have_attribute("placeholder", "Why not? (optional)")
    reason = panel.locator(".footer label.reason")
    expect(reason.locator(".reason-label")).to_have_text("Not an issue")
    expect(reason.locator(".reason-hint")).to_have_text("It went fine, or you were only trying Assist out.")
    if why == "not-an-issue":
        reason.locator("input[data-reason=not_an_issue]").check()
        expect(note).to_have_attribute("placeholder", "Anything to add? (optional)")
        said = ""
    else:
        said = "I'd rather rename it myself"
        note.fill(said)
    page.shot("declining")
    panel.locator(".footer #decide").click()

    # Thanks; the improvement is declined, and closed: no decision bar, no Undo.
    expect(panel.locator(".footer .done-msg")).to_contain_text("Thanks.")
    expect(panel.locator("h1 .chip")).to_have_text("Declined")
    expect(panel.locator(".footer #decide")).to_have_count(0)
    expect(panel.locator(".footer #undo-link")).to_have_count(0)
    sidebar_count(page, 0)
    page.shot("declined")

    # The decision is kept with its reason or note (the record Your review hands on).
    def decision():
        item = ((ha.storage("assist_improver.improvements") or {}).get("improvements") or {}).get(declined) or {}
        return (item.get("decisions") or [None])[-1]
    last = ha.wait(lambda: (d := decision()) and d.get("decision") == "decline" and d, what="the decline, saved")
    assert last.get("reason") == ("not_an_issue" if why == "not-an-issue" else None), last
    assert last.get("note") == said, last

    # Back to the list: nothing to review; the improvement is under Earlier improvements, declined; the conversation
    # is out of the list, Declined and archived.
    panel.locator("#back-list").click()
    expect(panel.locator("h1").filter(has_text="to review")).to_have_count(0)
    history = panel.locator("details[data-key=history]")
    expect(history.locator(":scope > summary")).to_have_text("Earlier improvements (1)")
    history.locator(":scope > summary").click()
    expect(history.locator(".item").filter(has_text=TITLE).locator(".chip")).to_have_text("Declined")
    row = page.conversation_row(ASKED)
    expect(row).to_have_count(0)
    page.show_archived()
    expect(page.chip(row)).to_have_text("Declined")
    expect(row).to_have_class(re.compile(r"\bcs-archived\b"))
    expect(row.locator(".cs-review")).to_have_count(0)
    # Archived, not only settled (the list shows both alike, P14.4.2): its record says so.
    stands(ha, "the conversation declined and archived", stage="Your household", outcome="declined", archived=True)

    # The history names the decision by its reason, if it was given one.
    rows = history_rows(page, panel)
    expect(rows.first).to_contain_text("Declined: not an issue" if why == "not-an-issue" else "Declined")
    expect(rows.first.locator(".h-sub")).to_contain_text(TITLE)
    if why != "not-an-issue":
        expect(rows.filter(has_text="not an issue")).to_have_count(0)

    # Nothing in Home Assistant changed.
    unchanged(ha, page)


def test_accept_meets_a_conflict_and_is_sent_back_to_resolve(page, ha):
    panel = up_for_review(page, ha)
    earlier = improvement_id(page)
    rid = stands(ha, "the conversation at Your review", stage="Your review", round=1)["id"]

    # Before Alex accepts, someone changes the desk lamp's entity ID in Home Assistant's settings: the device the
    # improvement names is no longer there, so accepting meets a conflict (changeset.plan_steps).
    ha.ws.call("config/entity_registry/update", entity_id=LAMP, new_entity_id=MOVED)
    ha.wait(lambda: (ha.state(MOVED) or {}).get("state") == "off", what="the desk lamp under its new entity ID")

    # Accept changes: nothing is written, and the review says it can't apply it now and why, and asks at once, under
    # what happened: Send it back to resolve, with no note.
    why = "light.desk_lamp: The device is no longer in Home Assistant."
    panel.locator(".footer #decide").click()
    expect(panel.locator(".footer .done-msg")).to_contain_text("Nothing was changed.")  # Why once, above.
    expect(panel.locator(".footer .done-msg")).not_to_contain_text(why)
    expect(panel.locator("h1 .chip")).to_have_text("Changed since tested")
    expect(panel.locator("div.conflict")).to_have_text(f"Can't apply it now:{why}")
    # Asked beside what happened, never where Accept was (a second tap there would send it back).
    expect(panel.locator(".footer #decide")).to_have_count(0)
    expect(panel.locator(".footer .done-msg #resolve-link")).to_have_text("Send it back to resolve")
    expect(panel.locator(".footer #note")).to_have_count(0)
    assert ha.aliases(MOVED) == [], "Nothing was written"
    sidebar_count(page, 0)  # Not waiting for review any more: the panel is made again, and still asks.
    expect(panel.locator(".footer .done-msg")).to_contain_text("Nothing was changed.")
    expect(panel.locator("div.conflict")).to_have_text(f"Can't apply it now:{why}")
    expect(panel.locator(".footer .done-msg #resolve-link")).to_have_text("Send it back to resolve")
    page.shot("conflict")

    # Nothing goes back in until Alex agrees: no run of its own, and the conversation stays at Your review.
    assert ha.run()["trigger"] == "filed"
    stands(ha, "the conversation still at Your review", stage="Your review", round=1)

    # Send it back to resolve: thanks, and its conversation goes straight back in on a run of its own, admitted at
    # Propose for round 2 with what conflicted.
    panel.locator(".footer #resolve-link").click()
    expect(panel.locator(".footer .done-msg")).to_contain_text("Thanks. The improver is revising it now.")
    expect(panel.locator(".footer #decide")).to_have_count(0)
    page.shot("sent-back")
    run = ha.wait_for_run("conflict")
    assert run["status"] == "done" and run["admitted"] == [rid], run
    conv = stands(ha, "the conversation's second round, at Your review", round=2, stage="Your review")
    records = conv["rounds"]["2"]["records"]
    assert list(records)[0] == "Propose" and not {"Triage", "Analyze"} & set(records), list(records)
    assert records["Propose"]["conflict"]["improvement"] == earlier, records["Propose"]
    assert [c["why"] for c in records["Propose"]["conflict"]["conflicts"]] == [
        "The device is no longer in Home Assistant."], records["Propose"]
    # Proposed and tested on the household's setup as it is now (DETAILS P6.2.4): an alias for the desk lamp under its
    # new entity ID, the same device the conversation's goal wants.
    assert [c["entity_id"] for i in records["Propose"]["ideas"] for c in i["hypothesis"]["changes"]] == [MOVED]
    assert any(t["validated"] for t in records["Propose"]["tests"]), records["Propose"]["tests"]

    # The list follows: the new round's version waits for review as a revision; the one that met the conflict is
    # replaced; the conversation is up for review again.
    back_home(page, panel)
    expect(panel.locator("h1").filter(has_text="1 improvement to review")).to_be_visible()
    pending = panel.locator(".item").filter(has=page.page.locator(".go"))  # Up for review: it opens.
    expect(pending).to_have_count(1)
    expect(pending.locator(".tag")).to_have_text("Revision")
    history = panel.locator("details[data-key=history]")
    expect(history.locator(":scope > summary")).to_have_text("Earlier improvements (1)")
    history.locator(":scope > summary").click()
    expect(history.locator(".item").filter(has_text=TITLE).locator(".chip")).to_have_text("Replaced by a revision")
    sidebar_count(page, 1)
    row = page.conversation_row(ASKED)
    page.show_archived()
    expect(row.locator(".cs-review")).to_have_text("Up for review")
    page.shot("resolving")
    # Its chip opens the new round's version, which says it came of the conflict, not of a note.
    row.locator(".cs-review").click()
    expect(page.page).to_have_url(re.compile(r"/assist-improver\?id="))
    assert improvement_id(page) != earlier
    expect(panel.locator(".revises")).to_have_text(f"Revised after a conflict on “{TITLE}”")
    back_home(page, panel)
    rows = history_rows(page, panel)
    expect(rows.filter(has_text="Run finished · conflict")).to_have_count(1)

    # Nothing in Home Assistant changed meanwhile.
    assert ha.aliases(MOVED) == []
    assert ha.state(MOVED)["state"] == "off" and ha.state(HELPER)["state"] == "off"
    assert page.errors == [], (page.errors, page.rejections, page.blocked)
