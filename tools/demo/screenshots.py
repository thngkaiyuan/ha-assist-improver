"""Getting Started screenshots from the demo Home Assistant (tools/demo/demo.py), never a real home.

  PLAYWRIGHT_BROWSERS_PATH=tools/ui/browsers tools/e2e/.venv/bin/python tools/demo/screenshots.py

Walks the README's journey on a freshly seeded demo (demo.py fresh first): the home page (what to review, then your
conversations and the Run bar), a conversation opened (a failed step, and Send this one in), the settings, a review as
it opens, and What changes in the Movie time improvement; then accepts a review and checks the next one is a click
away. Desktop, light theme, in the demo home's time zone. Images go to assets/getting-started/.
"""

import json
import sys
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

HERE = Path(__file__).resolve().parent
OUT = HERE.parent.parent / "assets/getting-started"
URL = "http://127.0.0.1:8124"
ZONE = "America/Los_Angeles"  # The demo home's (demo.py's configuration).


def panel(page):
    return page.locator("assist-improver-panel")


def quiet(page):
    """Nothing on top of the page: no toast ("Starting…", "Connection lost"), and nothing still moving."""
    expect(page.locator("ha-toast")).to_have_count(0, timeout=60_000)
    page.wait_for_timeout(600)


def shot(page, name):
    OUT.mkdir(parents=True, exist_ok=True)
    page.mouse.move(640, 20)  # Over the page's title: nothing shows as hovered.
    quiet(page)
    page.screenshot(path=str(OUT / f"{name}.png"))
    print("saved", name)


def scroll_to(element, gap=24):
    """Scroll the page (whatever scrolls it, across shadow roots) so the element's top is gap pixels below its top,
    once any smooth scrolling the panel started has ended."""
    for _ in range(20):
        off = element.evaluate("""(el, gap) => {
            let box = el;
            while ((box = box.parentElement || box.getRootNode?.()?.host))
                if (/(auto|scroll)/.test(getComputedStyle(box).overflowY) && box.scrollHeight > box.clientHeight) break;
            box = box || document.scrollingElement;
            const off = el.getBoundingClientRect().top - Math.max(0, box.getBoundingClientRect().top) - gap;
            box.scrollTo({top: box.scrollTop + off, behavior: 'instant'});
            return off;
        }""", gap)
        element.page.wait_for_timeout(250)
        if abs(off) < 1:
            break


def home(page):
    page.goto(URL + "/assist-improver")
    expect(panel(page).get_by_text("to review").first).to_be_visible(timeout=20_000)
    expect(panel(page).locator(".cs-row").first).to_be_visible(timeout=20_000)
    expect(panel(page).locator("#cs-run")).to_be_visible(timeout=20_000)


def review(page, title):
    """A review opened from the home page, as it arrives: what you get, its before and after, why it's safe."""
    home(page)
    panel(page).locator(".item", has_text=title).click()
    expect(panel(page).locator(".summary .tests details[open]").first).to_be_visible(timeout=20_000)


def main():
    login = json.loads((HERE / "config/demo-login.json").read_text())
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(viewport={"width": 1280, "height": 800}, device_scale_factor=2,
                                      service_workers="block", color_scheme="light", timezone_id=ZONE,
                                      locale="en-US")
        page = context.new_page()
        page.goto(URL)
        page.locator("input[name='username']").fill(login["username"])
        page.locator("input[name='password']").fill(login["password"])
        page.keyboard.press("Enter")
        page.wait_for_function("() => document.querySelector('home-assistant')?.hass?.connected", timeout=60_000)
        # HA has finished starting (no "Starting ..." toast in the pictures).
        page.wait_for_function("() => document.querySelector('home-assistant').hass.config.state === 'RUNNING'",
                               timeout=180_000)
        page.wait_for_timeout(2000)
        if page.locator("dialog-http-pending-config").count():  # HA confirms the demo's HTTP port once.
            page.get_by_role("button", name="Confirm").click()
            page.wait_for_timeout(1000)
        # The demo's own Repairs items (a plain Python install), its example updates and its example notification
        # aren't part of the journey: out of the sidebar's counts. The improver's own notification stays.
        page.evaluate("""async () => {
            const hass = document.querySelector('home-assistant').hass;
            const {issues} = await hass.callWS({type: 'repairs/list_issues'});
            for (const i of issues.filter(i => i.domain !== 'assist_improver' && !i.ignored))
                await hass.callWS({type: 'repairs/ignore_issue', domain: i.domain, issue_id: i.issue_id, ignore: true});
            const updates = Object.values(hass.states).filter(s => s.entity_id.startsWith('update.') && s.state === 'on');
            for (const {entity_id} of updates) await hass.callService('update', 'skip', {entity_id}).catch(() => null);
            for (const n of await hass.callWS({type: 'persistent_notification/get'}))
                if (!n.notification_id.startsWith('assist_improver'))
                    await hass.callService('persistent_notification', 'dismiss', {notification_id: n.notification_id});
        }""")
        page.wait_for_timeout(1000)

        # The home page: what to review first, then your conversations, each with where it stands, and the Run bar.
        home(page)
        shot(page, "panel")

        # A conversation opened: what was said, the step that failed where it did, and Send this one in.
        panel(page).locator("[data-cs]", has_text="Turn on the bedroom fan.").click()
        expect(panel(page).locator(".cs-read .cs-step")).to_be_visible(timeout=20_000)
        expect(panel(page).locator("#cs-send")).to_be_visible()
        page.wait_for_timeout(800)  # Scrolled into view, smoothly; then from the list's title.
        scroll_to(panel(page).locator(".cs-title"), gap=16)
        shot(page, "conversation")

        # The settings: the pipeline as a track, each step's model and what waits at it, and Run.
        panel(page).locator("#open-settings").click()
        expect(panel(page).locator(".track .stn").first).to_be_visible(timeout=20_000)
        expect(panel(page).locator("#run-now")).to_be_visible(timeout=20_000)
        page.wait_for_timeout(800)
        shot(page, "settings")

        # A review as it opens: what you get, its before and after, then why it's safe.
        review(page, "Turn off only lights when you ask for lights")
        shot(page, "review")

        # What changes in the Movie time improvement: the new script as a diff, and the script made visible to Assist.
        review(page, "Add a Movie time script for the living room")
        changes = panel(page).locator("details[data-key='changes']")
        changes.locator(":scope > summary").click()  # Folded at first.
        expect(changes.locator(".file details[open]")).to_have_count(2, timeout=20_000)
        scroll_to(changes, gap=10)
        shot(page, "what-changes")

        # Then accepted, with the next review one click away.
        review(page, "Turn off only lights when you ask for lights")
        panel(page).locator("#decide").click()
        expect(panel(page).locator("#next")).to_be_visible(timeout=30_000)
        browser.close()


if __name__ == "__main__":
    sys.exit(main())
