"""Journey: the notification that tells the household what waits for them.

A conversation filed from Assist whose fix is a new name for the desk lamp (as in test_file_from_assist.py) waits in
Your review, and one notification in Home Assistant lists what waits ("1 improvement to review", by headline, each
linking to its review); a second conversation's improvement joins the same notification ("2 improvements to
review"); it follows each decision the household makes, and clears once nothing waits.

SPEC.md P0.11 (Your review: the household accepts, revises or declines), P0.12 (Your household: improved, declined).
DETAILS P12.10/P12.10.1/P12.10.2/P12.10.4 (one summary notification of what waits, its words, updated and cleared as
the list changes; no "nothing found" notification), P12.10.5 (the sidebar's count).
"""

import re
import threading

import pytest
from playwright.sync_api import expect

from conftest import WebSocket

LAMP = "light.desk_lamp"  # "Desk lamp" in the Study: nothing in the household is called the reading lamp.
ASKED = "Turn on the reading lamp"
FILED = "File an improvement: the reading lamp is the desk lamp in the study"
TITLE = "Call the desk lamp the reading lamp"  # The improvement's headline.
CEILING = "light.ceiling_light"  # "Ceiling light" in the Living room: nothing is called the big light.
ASKED_TOO = "Turn on the big light"
FILED_TOO = "File an improvement: the big light is the ceiling light in the living room"
TITLE_TOO = "Call the ceiling light the big light"
SUMMARY = "assist_improver_improvements"  # The summary notification's id in Home Assistant (P12.10).


# Home Assistant's side.

class Notifications(WebSocket):
    """Home Assistant's persistent notifications as they come and go, from a websocket of its own subscribed to them
    (persistent_notification/subscribe): now() is those there now, by id, each {notification_id, title, message};
    ours() the ids of Assist Improver's among them; summary(title) waits for the summary notification to have this
    title (None: to be gone)."""

    def __init__(self, ha):
        super().__init__(ha.port, ha.tokens["access_token"])
        self.ha, self._guard, self._now = ha, threading.Lock(), {}
        self._send({"id": next(self.ids), "type": "persistent_notification/subscribe"})
        threading.Thread(target=self._listen, name="e2e-notifications", daemon=True).start()

    def _listen(self):
        try:
            while True:
                message = self._recv()
                if message.get("type") != "event":
                    continue
                event = message["event"]
                with self._guard:
                    for nid, note in (event.get("notifications") or {}).items():
                        if event["type"] == "removed":
                            self._now.pop(nid, None)
                        else:
                            self._now[nid] = note
        except (ConnectionError, OSError, ValueError):
            pass  # Closed at the end of the test.

    def now(self):
        with self._guard:
            return dict(self._now)

    def ours(self):
        return sorted(nid for nid in self.now() if nid.startswith("assist_improver"))

    def wait(self, check, what):
        return self.ha.wait(check, what=what)

    def summary(self, title):
        def check():
            note = self.now().get(SUMMARY)
            return note is None if title is None else note if note and note["title"] == title else None
        return self.wait(check, what=f"the summary notification titled {title!r}")


@pytest.fixture
def notifications(ha):
    """Home Assistant's notifications, followed from before the journey starts (Notifications)."""
    followed = Notifications(ha)
    yield followed
    followed.close()


def improvements(ha):
    """Assist Improver's improvements as Home Assistant saved them: {id: improvement}, oldest first."""
    return (ha.storage("assist_improver.improvements") or {}).get("improvements") or {}


def improvement(ha, title, **fields):
    """The improvement with this headline, once it has these fields (status, ...)."""
    def found():
        item = next((i for i in improvements(ha).values() if (i.get("copy") or {}).get("headline") == title), None)
        return item if item and all(item.get(k) == v for k, v in fields.items()) else None
    return ha.wait(found, what=f"“{title}” with {fields}")


def stands(ha, said, **fields):
    """The flow record of the conversation that began with `said` (SPEC P0.2), once it has these fields."""
    def record():
        rows = ha.conversations()
        rid = next((r["id"] for r in rows if r["said"] == said), None)
        conv = ((ha.storage("assist_improver.flow") or {}).get("conversations") or {}).get(rid)
        return conv if conv and all(conv.get(k) == v for k, v in fields.items()) else None
    return ha.wait(record, what=f"“{said}” with {fields}")


def wait_next_run(ha, before):
    """The run started after the one with id `before` (None: the first), once it has finished."""
    return ha.wait(lambda: (run := ha.run()) and run.get("id") != before and run.get("status") != "running" and run,
                   what="the run to finish")


# The screen.

def file_from_assist(page, asked, filed):
    """Asked for a device in the Assist dialog by a name Assist doesn't know, then filed from there; dialog closed."""
    page.goto("/")
    expect(page.assist(asked)).to_contain_text("couldn't find")
    expect(page.assist(filed)).to_contain_text("Filed")
    page.close_assist()


def sidebar_count(page, n):
    """Home Assistant's sidebar names Assist Improver with n improvements waiting (P12.10.5): the panel is made again
    when that count changes, so a journey waits for it before acting on the new panel."""
    item = page.page.locator("ha-sidebar").get_by_text(re.compile(r"^\s*Assist Improver(\s+\(\d+\))?\s*$")).first
    count = rf"\s+\({n}\)" if n else ""
    expect(item).to_have_text(re.compile(rf"^\s*Assist Improver{count}\s*$"))


def open_notifications(page):
    """Home Assistant's notifications, from the dashboard's sidebar (on the phone, behind the sidebar toggle): the
    drawer."""
    page.goto("/")
    if page.phone:
        page.page.get_by_role("button", name="Sidebar toggle").click()
    page.page.locator("ha-sidebar #sidebar-notifications").click()
    drawer = page.page.locator("notification-drawer")
    expect(drawer.locator("ha-header-bar")).to_be_in_viewport(ratio=1)  # Slid all the way in.
    return drawer


def close_notifications(page):
    page.page.keyboard.press("Escape")
    closed(page)


def closed(page):
    """The notifications drawer has closed (it closes as a link in it is followed)."""
    expect(page.page.locator("notification-drawer ha-header-bar")).to_be_hidden()


def notice(drawer, title):
    """The notification in the drawer with this title."""
    return drawer.locator("persistent-notification-item").filter(
        has=drawer.page.locator("[slot=header]", has_text=title))


def review_url(iid):
    return f"/assist-improver?id={iid}"


# The journey.

def test_review_notification(page, ha, notifications):
    assert SUMMARY not in notifications.now()

    # The fix waits in Your review: nothing is applied, and no notice says it was.
    file_from_assist(page, ASKED, FILED)
    first = ha.wait_for_run("filed")
    assert first["status"] == "done", first
    a = improvement(ha, TITLE, status="awaiting_review")["id"]
    stands(ha, ASKED, stage="Your review", outcome=None)
    assert ha.aliases(LAMP) == []
    notifications.summary("1 improvement to review")
    assert notifications.ours() == [SUMMARY], notifications.now()

    # In Home Assistant's notifications: one improvement to review, by its headline, linking to its review; then
    # where to review it.
    drawer = open_notifications(page)
    summary = notice(drawer, "to review")
    expect(summary).to_have_count(1)
    expect(summary.locator("[slot=header]")).to_have_text("1 improvement to review")
    expect(summary.locator("ha-markdown li")).to_have_text([TITLE])
    expect(summary.locator("ha-markdown li a")).to_have_attribute("href", review_url(a))
    expect(summary.locator("ha-markdown p").last).to_have_text("Review this improvement in Assist Improver.")
    expect(summary.get_by_role("link", name="Assist Improver")).to_have_attribute("href", "/assist-improver")
    page.shot("notification-one")
    close_notifications(page)

    # A second conversation, filed the same way: its improvement joins the same notification, not one of its own.
    before = first["id"]
    file_from_assist(page, ASKED_TOO, FILED_TOO)
    second = wait_next_run(ha, before)
    assert second["status"] == "done", second
    b = improvement(ha, TITLE_TOO, status="awaiting_review")["id"]
    notifications.summary("2 improvements to review")
    assert notifications.ours() == [SUMMARY], notifications.now()
    assert ha.aliases(CEILING) == []

    drawer = open_notifications(page)
    summary = notice(drawer, "to review")
    expect(summary).to_have_count(1)
    expect(summary.locator("[slot=header]")).to_have_text("2 improvements to review")
    expect(summary.locator("ha-markdown li")).to_have_text([TITLE, TITLE_TOO])
    expect(summary.locator("ha-markdown li a").nth(0)).to_have_attribute("href", review_url(a))
    expect(summary.locator("ha-markdown li a").nth(1)).to_have_attribute("href", review_url(b))
    expect(summary.locator("ha-markdown p").last).to_have_text("Review these improvements in Assist Improver.")
    page.shot("notification-two")

    # Its second headline opens that improvement's review; accepted, the notification lists only the first.
    summary.get_by_role("link", name=TITLE_TOO).click()
    expect(page.page).to_have_url(re.compile(re.escape(review_url(b)) + "$"))
    closed(page)
    panel = page.panel
    expect(panel.locator("h1")).to_contain_text(TITLE_TOO)
    expect(panel.locator("h1 .chip")).to_have_text("To review")
    panel.locator(".footer #decide").click()
    expect(panel.locator(".footer .done-msg")).to_contain_text("Applied.")
    ha.wait(lambda: ha.aliases(CEILING) == ["Big light"], what="the ceiling light's new name")
    note = notifications.summary("1 improvement to review")
    assert TITLE_TOO not in note["message"], note
    sidebar_count(page, 1)

    drawer = open_notifications(page)
    summary = notice(drawer, "to review")
    expect(summary.locator("[slot=header]")).to_have_text("1 improvement to review")
    expect(summary.locator("ha-markdown li")).to_have_text([TITLE])
    expect(summary.locator("ha-markdown p").last).to_have_text("Review this improvement in Assist Improver.")

    # Its Assist Improver link opens the panel, where the first waits; declined, nothing waits, and the notification
    # clears. Nothing else is said: no notice for what the household decided, no "nothing found".
    summary.get_by_role("link", name="Assist Improver").click()
    expect(page.page).to_have_url(re.compile(r"/assist-improver/?$"))
    closed(page)
    panel = page.panel
    expect(panel.locator("h1").filter(has_text="1 improvement to review")).to_be_visible()
    panel.locator(".item").filter(has_text=TITLE).click()
    expect(page.page).to_have_url(re.compile(re.escape(review_url(a)) + "$"))
    panel.locator(".footer #arrow").click()
    panel.locator(".footer .menu [data-choice=decline]").click()
    panel.locator(".footer input[data-reason=not_an_issue]").check()
    panel.locator(".footer #decide").click()
    expect(panel.locator(".footer .done-msg")).to_contain_text("Thanks.")
    notifications.summary(None)
    assert notifications.ours() == [], notifications.now()
    sidebar_count(page, 0)

    drawer = open_notifications(page)
    expect(notice(drawer, "to review")).to_have_count(0)
    expect(notice(drawer, "Assist Improver")).to_have_count(0)
    page.shot("notification-cleared")
    close_notifications(page)
    assert ha.aliases(LAMP) == []
    assert page.errors == [], (page.errors, page.rejections, page.blocked)
