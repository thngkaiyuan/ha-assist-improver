"""Journeys: the conversations list on the panel's home page ('Your conversations'), as the household finds, picks and
deletes conversations there.

1. Delete from Assist Improver, from an opened conversation's ⋮ (More actions): it asks first, and Cancel keeps it;
   confirmed, the conversation is gone from the list and from what a run takes, and Home Assistant no longer keeps it
   (its recording, its index entry, its flow record and its words are gone, and Export recording no longer knows it).
2. Finding conversations: by what was said (in any case) and by what Assist replied, the closest when none has every
   word, by when (the search's own 'yesterday'), the day filter (All · Today · 7 days · 30 days: Today is today's, in
   Home Assistant's time zone, as the search's 'today' is, not the last 24 hours) and the order (Newest · Most
   back-and-forth · Oldest), on conversations recorded at different times.
3. Picking conversations with their checkboxes and running only those: Run on 2 conversations asks first (Not now
   starts nothing), then moves only the picked ones, and the one left unpicked stays new, untouched. Then the Status
   filter finds the picked ones by where the run left them.

Conversations recorded at different times (journey 2): each conversation is recorded for real, through Assist, now.
Home Assistant has no clock to set back, so the journey then stops Home Assistant and moves each older conversation's
recording back in time on disk, as it would be had it been recorded then: every timestamp in its recording, in its
index entry (its start, which the list reads), and in its words' and flow records, moved back by the same amount.
Started again, Home Assistant and the product read it as any older conversation. Nothing else is changed. The newest
is moved, the same way, to the moment the checks start from: they all read days, so they all fall on one day (close to
midnight, the journey waits for the new day first).

SPEC.md P0.2 (one record every screen reads), P0.7 (a picked run admits the picked ones; only what it admits moves),
P0.12 (looked smooth is in Your household). DETAILS P8.14.2 (Archive and Delete behind the opened conversation's ⋮),
P8.15.1 (Delete asks first, in those words: Cancel · Delete from Assist Improver), P8.15.2 (deleting removes the
conversation and everything kept about it), P14.1.1 (every count is what the run will move), P14.3.3/P14.3.9/P14.3.10
(New, Up for review, Looked smooth), P14.4.1/P14.4.2 (settled ones leave the list; Show archived), P14.4.5 ('No
conversations match.'), P14.4.6 (search by what was said, in any case, or by when; Clear search; 'No conversation has
all those words. The closest:'), P14.4.7 (the day filter and the sort), P14.4.9 (choose conversations by where they
stand), P14.4.13 (the ⋮ with an icon on each row, Delete in red), P14.5 (selection: Select all mixed when only some
are picked; the bottom bar's actions with their counts), P14.6.3 (with some picked, Run counts the picked ones and is
a picked run), P14.7.2 (its confirmation names them), P14.10.2 (History: 'Run finished · picked').
"""

from datetime import datetime, time, timedelta
import json
import re
from zoneinfo import ZoneInfo

import pytest
from playwright.sync_api import expect

from conftest import STARTUP_SECONDS, WebSocket, _ready, _running, _set_port, _start_process, _stop_process, wait_until

FLOW = "assist_improver.flow"  # Each conversation's flow record (flow.py), as Home Assistant saves it.
INDEX = "assist_improver.index"  # The recordings' index: each one's start, which the list reads (recorder.py).
WORDS = "assist_improver.transcripts"  # What each conversation said, kept for the list (conversations.py).
ON = re.compile(r"\bon\b")  # The chosen button of a segmented control.
ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:\d{2}|Z)$")
ONE_DAY = timedelta(minutes=5)  # The longest journey 2's checks take: from their start, all on one day.


# Home Assistant's side.

def listed(ha, said):
    """The conversation whose first words are these, as Assist Improver lists it (archived ones too): its row (id,
    status, started, hidden, ...), or None."""
    rows = ha.conversations()
    return next((row for row in rows if row["said"] == said), None)


def all_ended(ha, *said):
    """Each of these conversations has ended (its chat session idled out) and is new: {said: its row}."""
    return ha.wait(lambda: (rows := {s: listed(ha, s) for s in said})
                   and all(r and r["status"] == "waiting" for r in rows.values()) and rows,
                   what=f"{', '.join(said)} to end")


def record(ha, rid):
    """The conversation's flow record as Home Assistant last saved it (stage, round, outcome, ...), or None."""
    return ((ha.storage(FLOW) or {}).get("conversations") or {}).get(rid)


def admits(ha):
    """How many new conversations a manual full run would take in now (what the Run bar counts)."""
    return ha.ws.call("assist_improver/run")["preview"]["admits"]


def export(ha, rid):
    """Export recording, the administrators' service: the recording, or Home Assistant's error."""
    try:
        return ha.ws.call("call_service", domain="assist_improver", service="export_recording",
                          service_data={"recording_id": rid}, return_response=True)["response"]
    except RuntimeError as err:
        return str(err)


def kept_anywhere(ha, rid):
    """Where Home Assistant still keeps anything of the conversation, of its recording (.storage), its index entry,
    its flow record and its words."""
    where = []
    if ha.storage(f"assist_improver.session_{rid}") is not None:
        where.append("recording")
    if rid in ((ha.storage(INDEX) or {}).get("sessions") or {}):
        where.append("index")
    if record(ha, rid) is not None:
        where.append("flow")
    if rid in ((ha.storage(WORDS) or {}).get("transcripts") or {}):
        where.append("words")
    return where


def _earlier(value, by):
    """The value with every timestamp in it moved back by `by`."""
    if isinstance(value, dict):
        return {k: _earlier(v, by) for k, v in value.items()}
    if isinstance(value, list):
        return [_earlier(v, by) for v in value]
    if isinstance(value, str) and ISO.match(value):
        return (datetime.fromisoformat(value.replace("Z", "+00:00")) - by).isoformat()
    return value


def _rewrite(path, change):
    if path.exists():
        stored = json.loads(path.read_text())
        change(stored["data"])
        path.write_text(json.dumps(stored))


@pytest.fixture
def restart(ha, fake_model):
    """recorded_at({rid: when it started}): Home Assistant stopped, each of these conversations moved back in time on
    disk to when it says, as if it had been recorded then (every timestamp of its recording, index entry, words and
    flow record, moved by the same amount), and Home Assistant started again on the same port and logged in. Taken
    before `page`, so a failing test's screenshot is taken while Home Assistant still runs."""
    started = []

    def recorded_at(when):
        _stop_process(ha.process)  # Gently: the improver saves everything it holds as Home Assistant stops.
        storage = ha.folder / ".storage"
        index = json.loads((storage / INDEX).read_text())["data"]["sessions"]
        shifts = {rid: datetime.fromisoformat(index[rid]["started"]) - at for rid, at in when.items()}
        for rid, by in shifts.items():
            _rewrite(storage / f"assist_improver.session_{rid}", lambda data, by=by: data.update(_earlier(data, by)))

        def entries(key, kind):
            def change(data):
                for rid, by in shifts.items():
                    if rid in (data.get(kind) or {}):
                        data[kind][rid] = _earlier(data[kind][rid], by)
            _rewrite(storage / key, change)
        entries(INDEX, "sessions")
        entries(WORDS, "transcripts")
        entries(FLOW, "conversations")
        _set_port(ha.folder, ha.port)
        ha.process = _start_process(ha.folder, fake_model.url, ha.port)
        started.append(ha.process)
        ha.wait(lambda: _ready(ha.port) or ha.process.poll() is not None, timeout=STARTUP_SECONDS,
                what="Home Assistant to start again")
        assert ha.process.poll() is None, f"Home Assistant exited at startup; see {ha.folder}/e2e-hass.log"
        ha.ws.close()
        ha.ws = ha.wait(lambda: WebSocket(ha.port, ha.tokens["access_token"]), what="the websocket")
        ha.wait(lambda: _running(ha), timeout=STARTUP_SECONDS, what="Home Assistant to finish starting")
        ha.wait(lambda: all(e["state"] == "loaded" for e in ha.entries()
                            if e["domain"] in ("anthropic", "assist_improver")), what="the integrations to load")

    yield recorded_at
    for process in started:
        _stop_process(process)


# The screen.

def talk(page, *turns):
    """One conversation with Assist, in a dialog of its own: each (words, what the reply says) in turn; then closed."""
    page.goto("/")
    for words, reply in turns:
        expect(page.assist(words)).to_contain_text(reply)
    page.close_assist()


def open_conversation(page, said):
    """Opens the conversation in the list to read it: its row, once its turns are shown."""
    row = page.conversation_row(said)
    row.locator(".cs-head").click()
    expect(row.locator(".cs-read .cs-turn").first).to_be_visible()
    return row


def more_actions(row):
    """The opened conversation's More actions (⋮), opened: its menu."""
    row.get_by_role("button", name="More actions").click()
    menu = row.get_by_role("menu")
    expect(menu).to_be_visible()
    return menu


def shown(panel):
    """What each conversation listed said first, in the list's order."""
    return panel.locator(".cs-list .cs-row .cs-said")


def search(panel, words):
    """Words typed in the search box, tapped first as on a phone (the frontend's sidebar, just closed there, keeps
    the focus until a tap lands elsewhere)."""
    box = panel.locator("#cs-search")
    box.click()
    box.fill(words)


def run_dialog(panel, title):
    """The run's confirmation (a modal), titled as its button said, once shown."""
    modal = panel.locator(".confirm-modal")
    expect(modal).to_be_visible()
    expect(modal.locator(".modal-head b")).to_have_text(title)
    expect(modal.locator(".cost").last).to_contain_text("Runs use your AI models, so they cost money.")
    return modal


# The journeys.

FLOOR = "Turn on the floor lamp"
KETTLE = "Turn on the kettle"


def test_delete_from_assist_improver(page, ha):
    """A new conversation deleted from its ⋮: asked first (Cancel keeps it, still in Home Assistant and in the run);
    confirmed, it's gone from the list, the Run count and the run itself, and Home Assistant keeps nothing of it."""
    talk(page, (FLOOR, "Turned on the Floor lamp"))
    talk(page, (KETTLE, "Turned on the Kettle"))
    rows = all_ended(ha, FLOOR, KETTLE)
    gone, stays = rows[FLOOR]["id"], rows[KETTLE]["id"]
    assert export(ha, gone)["id"] == gone  # Export recording has it.

    panel = page.open_panel()
    row = page.conversation_row(FLOOR)
    expect(page.chip(row)).to_have_text("New")
    run = panel.locator(".cs-footer #cs-run")
    expect(run).to_have_text("Run on 2 new conversations")

    # Its ⋮: Archive this conversation, and Delete from Assist Improver in red, each with an icon.
    open_conversation(page, FLOOR)
    menu = more_actions(row)
    items = menu.get_by_role("menuitem")
    expect(items).to_have_text(["Archive this conversation", "Delete from Assist Improver"])
    expect(items.locator("svg")).to_have_count(2)
    delete = menu.get_by_role("menuitem", name="Delete from Assist Improver")
    expect(delete).to_have_class(re.compile(r"\bdanger\b"))

    # It asks first; Cancel keeps it: still listed and open, still in Home Assistant, still in the run.
    delete.click()
    ask = row.get_by_role("group", name="Delete from Assist Improver")
    expect(ask).to_contain_text("Delete this conversation from Assist Improver? "
                                "The improver can no longer check changes against it.")
    expect(ask.get_by_role("button")).to_have_text(["Cancel", "Delete from Assist Improver"])
    page.shot("delete-asks")
    ask.get_by_role("button", name="Cancel").click()
    expect(ask).to_have_count(0)
    expect(row.locator(".cs-read .cs-turn")).to_have_count(1)
    expect(page.chip(row)).to_have_text("New")
    expect(run).to_have_text("Run on 2 new conversations")
    assert kept_anywhere(ha, gone)[:2] == ["recording", "index"] and admits(ha) == 2

    # Confirmed: gone from the list at once, and from what a run takes; the other conversation stays.
    more_actions(row).get_by_role("menuitem", name="Delete from Assist Improver").click()
    ask.get_by_role("button", name="Delete from Assist Improver").click()
    expect(row).to_have_count(0)
    expect(shown(panel)).to_have_text([KETTLE])
    expect(run).to_have_text("Run on 1 new conversation")
    expect(panel.locator(".cs-show-archived")).to_have_count(0)  # Deleted, not archived.
    assert listed(ha, FLOOR) is None and admits(ha) == 1
    page.shot("deleted")

    # Home Assistant keeps nothing of it: its recording, index entry, flow record and words are gone (the words once
    # their store is next written), the panel can't open it, and Export recording no longer knows it.
    ha.wait(lambda: kept_anywhere(ha, gone) == [], what="nothing kept of the deleted conversation")
    with pytest.raises(RuntimeError, match="That conversation isn't kept."):
        ha.ws.call("assist_improver/conversation", recording_id=gone)
    assert "Unknown recording ID" in export(ha, gone)
    assert kept_anywhere(ha, stays)[:2] == ["recording", "index"]

    # A run takes only the other one: its confirmation names only that, and only that is admitted.
    run.click()
    modal = run_dialog(panel, "Run on 1 new conversation?")
    expect(modal.locator(".c-which")).to_have_text(f"“{KETTLE}”")
    modal.locator("#start-run").click()
    expect(modal).to_be_hidden()
    done = ha.wait_for_run("manual")
    assert done["status"] == "done" and gone not in json.dumps(done), done
    assert list(done["results"]) == [stays], done["results"]

    # Loaded again, it's still gone, also from among the archived (where the run settled the other).
    page.page.reload()
    expect(panel.locator(".cs-show-archived")).to_have_text("Show archived (1)")
    page.show_archived()
    expect(shown(panel)).to_have_text([KETTLE])
    assert kept_anywhere(ha, gone) == []
    assert page.errors == [], (page.errors, page.rejections, page.blocked)


# Journey 2's conversations, by what each said first, and when each started (moved back, but for the newest).
TODAY = "Turn on the kettle"  # Just now (the checks' start): 1 turn.
YESTERDAY = "Turn on the ceiling light"  # Yesterday at 23:59, less than a day ago: 1 turn.
DAYS_3 = "Turn on the reading lamp"  # 3 days ago: 3 turns, the most back-and-forth (the lamp it couldn't find).
DAYS_12 = "Turn on the floor lamp"  # 12 days ago: 2 turns.
DAYS_45 = "Turn off the kettle"  # 45 days ago: 1 turn.


def sort(panel, order):
    """The order, from its menu beside the search."""
    panel.locator("#cs-sort").click()
    panel.get_by_role("menuitemradio", name=order).click()


def test_find_conversations_by_words_day_and_order(restart, page, ha):
    """Five conversations recorded at different times: found by what was said (any case) and by what Assist replied,
    the closest when none has every word, by when; the day filters show those of today, of the last 7 and 30 days, or
    all; and the orders put the newest, the most back-and-forth or the oldest first."""
    talk(page, (DAYS_3, "couldn't find"), ("Turn on the desk lamp", "Turned on the Desk lamp"),
         ("Turn off the desk lamp", "Turned off the Desk lamp"))
    talk(page, (DAYS_12, "Turned on the Floor lamp"), ("Turn off the floor lamp", "Turned off the Floor lamp"))
    talk(page, (DAYS_45, "Turned off the Kettle"))
    talk(page, (YESTERDAY, "Turned on the Ceiling light"))
    talk(page, (TODAY, "Turned on the Kettle"))
    rows = all_ended(ha, TODAY, YESTERDAY, DAYS_3, DAYS_12, DAYS_45)
    ids = {said: row["id"] for said, row in rows.items()}

    # The older ones, recorded earlier, and the newest now, the checks' start (see the module's docstring), in Home
    # Assistant's time zone.
    zone = ZoneInfo(ha.ws.call("get_config")["time_zone"])
    wait_until(lambda: (datetime.now(zone) + ONE_DAY).date() == datetime.now(zone).date(),
               timeout=ONE_DAY.total_seconds() + 60, interval=1, what="the new day")
    now = datetime.now(zone)
    midnight = datetime.combine(now.date(), time(), zone)
    starts = {TODAY: now, YESTERDAY: midnight - timedelta(minutes=1), DAYS_3: now - timedelta(days=3),
              DAYS_12: now - timedelta(days=12), DAYS_45: now - timedelta(days=45)}
    page.page.goto(ha.url + "/manifest.json")  # No frontend open while Home Assistant restarts (it would notice).
    restart({ids[said]: at for said, at in starts.items()})
    for said, at in starts.items():  # Home Assistant reads them as started then.
        assert datetime.fromisoformat(listed(ha, said)["started"]) == at, (said, listed(ha, said))

    page.goto("/")
    panel = page.open_panel()
    newest = [TODAY, YESTERDAY, DAYS_3, DAYS_12, DAYS_45]
    expect(shown(panel)).to_have_text(newest)
    expect(page.conversation_row(TODAY).locator(".cs-sub")).to_contain_text("Today")
    expect(page.conversation_row(YESTERDAY).locator(".cs-sub")).to_contain_text("Yesterday, 11:59")
    expect(page.conversation_row(DAYS_3).locator(".cs-sub")).to_contain_text("3 turns")
    expect(panel.get_by_role("button", name="All", exact=True)).to_have_class(ON)
    expect(panel.locator("#cs-sort")).to_contain_text("Newest")  # The order's menu: newest first.
    page.shot("all")

    # By what was said, in any case: the words are marked.
    search(panel, "KETTLE")
    expect(shown(panel)).to_have_text([TODAY, DAYS_45])
    expect(page.conversation_row(TODAY).locator(".cs-said mark")).to_have_text(["kettle"])

    # By what Assist replied: the reply shows, with the words marked.
    search(panel, "couldn't find")
    expect(shown(panel)).to_have_text([DAYS_3])
    match = page.conversation_row(DAYS_3).locator(".cs-match")
    expect(match).to_contain_text("Sorry, I couldn't find a device called reading lamp.")
    expect(match.locator("mark")).to_have_text(["couldn't", "find"])

    # No conversation has every word: the closest show, under a line that says so.
    search(panel, "kettle reading")
    expect(panel.locator(".cs-close")).to_have_text("No conversation has all those words. The closest:")
    expect(shown(panel)).to_have_text([TODAY, DAYS_3, DAYS_45])

    # By when: yesterday's, with its time marked.
    search(panel, "yesterday")
    expect(panel.locator(".cs-close")).to_have_count(0)
    expect(shown(panel)).to_have_text([YESTERDAY])
    expect(page.conversation_row(YESTERDAY).locator(".cs-sub mark")).to_contain_text("Yesterday")
    page.shot("searched")

    # Clear search: every conversation again, and the box empty.
    panel.get_by_role("button", name="Clear search").click()
    expect(panel.locator("#cs-search")).to_have_value("")
    expect(shown(panel)).to_have_text(newest)

    # The day filter: today's only (not yesterday's, though it's less than a day old), the last 7 days', the last
    # 30 days', and all.
    for button, expected in (("Today", [TODAY]), ("7 days", [TODAY, YESTERDAY, DAYS_3]),
                             ("30 days", [TODAY, YESTERDAY, DAYS_3, DAYS_12]), ("All", newest)):
        panel.get_by_role("button", name=button, exact=True).click()
        expect(panel.get_by_role("button", name=button, exact=True)).to_have_class(ON)
        expect(shown(panel)).to_have_text(expected)
    page.shot("days")

    # The order: the most back-and-forth first (the most turns), the oldest first, and the newest again.
    for button, expected in (("Most back-and-forth", [DAYS_3, DAYS_12, TODAY, YESTERDAY, DAYS_45]),
                             ("Oldest", newest[::-1]), ("Newest", newest)):
        sort(panel, button)
        expect(panel.locator("#cs-sort")).to_contain_text(button)  # The menu's button names the order chosen.
        expect(shown(panel)).to_have_text(expected)

    # Together: the last 7 days', oldest first; and a search on top of them.
    panel.get_by_role("button", name="7 days", exact=True).click()
    sort(panel, "Oldest")
    expect(shown(panel)).to_have_text([DAYS_3, YESTERDAY, TODAY])
    search(panel, "kettle")
    expect(shown(panel)).to_have_text([TODAY])
    search(panel, "floor")
    expect(panel.locator(".cs-empty")).to_have_text("No conversations match.")
    page.shot("combined")
    assert page.errors == [], (page.errors, page.rejections, page.blocked)


# Journey 3's conversations, in the order they're had.
READING = "Turn on the reading lamp"  # Assist couldn't find it; then told which lamp that is: picked.
EXPLAINED = "The reading lamp is the desk lamp in the study"
CEILING = "Turn on the ceiling light"  # Assist turned it on: left unpicked.


def test_run_on_picked_conversations(page, ha, fake_model):
    """Two of three new conversations picked with their checkboxes: Run says it runs on those 2, asks first (Not now
    starts nothing, and they stay picked), then moves only them: one Assist couldn't fulfil goes up for review, the
    one it did looked smooth, and the unpicked one stays new, untouched, for the next run. Then the Status filter,
    among the archived, finds them by where they stand."""
    talk(page, (READING, "couldn't find"), (EXPLAINED, "can't help"))
    talk(page, (KETTLE, "Turned on the Kettle"))
    talk(page, (CEILING, "Turned on the Ceiling light"))
    rows = all_ended(ha, READING, KETTLE, CEILING)
    ids = {said: row["id"] for said, row in rows.items()}

    panel = page.open_panel()
    expect(shown(panel)).to_have_text([CEILING, KETTLE, READING])
    run = panel.locator(".cs-footer #cs-run")
    expect(run).to_have_text("Run on 3 new conversations")  # Nothing picked: the full run, on every new one.
    select_all = panel.locator("#cs-all")
    expect(panel.locator(".cs-all").first).to_have_text("Select all 3")

    # Two picked by hand: Run counts those, the bar offers what applies to them, and Select all is mixed.
    for said in (KETTLE, READING):
        box = page.conversation_row(said).get_by_role("checkbox", name=f"Select “{said}”")
        box.check()
        expect(box).to_be_checked()
    expect(run).to_have_text("Run on 2 conversations")
    expect(panel.locator(".cs-footer .btn")).to_have_text(["Run on 2 conversations", "Archive (2)", "Delete (2)"])
    expect(select_all).not_to_be_checked()
    expect(select_all).to_have_js_property("indeterminate", True)
    page.shot("picked")

    # It asks first, naming the picked ones only; Not now starts nothing, and they stay picked.
    run.click()
    modal = run_dialog(panel, "Run on 2 conversations?")
    which = modal.locator(".c-which")
    expect(which).to_contain_text(f"“{KETTLE}”")
    expect(which).to_contain_text(f"“{READING}”")
    expect(which).not_to_contain_text(CEILING)
    page.shot("confirm")
    modal.locator(".actions").get_by_role("button", name="Not now").click()
    expect(modal).to_have_count(0)
    assert ha.run() is None, "Not now starts no run"
    expect(run).to_have_text("Run on 2 conversations")
    expect(page.conversation_row(KETTLE).get_by_role("checkbox")).to_be_checked()

    # Run: a picked run, on those two only.
    run.click()
    modal = run_dialog(panel, "Run on 2 conversations?")
    modal.locator("#start-run").click()
    expect(modal).to_have_count(0)
    done = ha.wait_for_run("manual")
    assert done["status"] == "done", done
    assert sorted(done["picked"]) == sorted([ids[KETTLE], ids[READING]]), done
    assert sorted(done["results"]) == sorted([ids[KETTLE], ids[READING]]), done["results"]

    # Only they moved: one up for review with an improvement, one looked smooth, both out of the list. The unpicked
    # one is new, as it was, for the next run: nothing read it (no AI role was asked about it).
    expect(panel.locator("h1").filter(has_text="1 improvement to review")).to_be_visible()
    expect(shown(panel)).to_have_text([CEILING])
    expect(page.chip(page.conversation_row(CEILING))).to_have_text("New")
    expect(page.conversation_row(CEILING).get_by_role("checkbox")).not_to_be_checked()
    expect(run).to_have_text("Run on 1 new conversation")  # Nothing picked now: the full run, on the new one left.
    assert (listed(ha, READING)["status"], listed(ha, KETTLE)["status"]) == ("review", "smooth")
    untouched = record(ha, ids[CEILING])
    assert untouched is None or (untouched["stage"], untouched["round"]) == ("Your Assist", 0), untouched
    assert listed(ha, CEILING)["status"] == "waiting" and admits(ha) == 1
    looked_at = [json.dumps(r["request"]["messages"][0]) for r in fake_model.requests[ha.since:]
                 if r["role"] in ("triage", "journey_analyzer")]  # The roles that look at one conversation each.
    assert looked_at and not any(CEILING in one for one in looked_at), "Nothing looked at the unpicked one"
    page.shot("ran")

    # Among the archived, the Status filter finds them by where they stand: each standing with how many.
    expect(panel.locator(".cs-show-archived")).to_have_text("Show archived (2)")
    page.show_archived()
    expect(shown(panel)).to_have_text([CEILING, KETTLE, READING])
    status = panel.locator("#cs-status")
    expect(status).to_have_text(re.compile(r"^Status\W*$"))
    status.click()
    choices = panel.locator(".cs-status-menu .cs-status-opt")
    expect(choices.locator(".cs-status-name")).to_have_text(["All", "New", "Looked smooth", "Up for review"])
    expect(choices.locator(".cs-status-n")).to_have_text(["1", "1", "1"])
    panel.locator("#cs-status-all").uncheck()
    expect(panel.locator(".cs-empty")).to_have_text("No conversations match.")
    panel.locator("[data-cs-status=smooth]").check()
    expect(shown(panel)).to_have_text([KETTLE])
    expect(status).to_have_text(re.compile(r"^Looked smooth"))
    panel.locator("[data-cs-status=review]").check()
    expect(shown(panel)).to_have_text([KETTLE, READING])
    expect(status).to_have_text(re.compile(r"^Status: 2"))
    expect(page.conversation_row(READING).locator(".cs-review")).to_have_text("Up for review")
    page.shot("by-status")
    panel.locator("#cs-status-all").check()
    expect(shown(panel)).to_have_text([CEILING, KETTLE, READING])
    expect(status).to_have_text(re.compile(r"^Status\W*$"))
    outside = panel.locator("#cs-status-backdrop")  # A click outside the menu closes it, changing nothing.
    outside.click(position={"x": 4, "y": outside.bounding_box()["height"] - 4})
    expect(panel.locator(".cs-status-menu")).to_have_count(0)

    # Settings › History says the run was a picked one.
    panel.locator("#open-settings").click()
    expect(page.page).to_have_url(re.compile(r"view=settings"))
    expect(panel.locator(".history .h-row").first).to_contain_text("Run finished · picked")
    assert page.errors == [], (page.errors, page.rejections, page.blocked)
