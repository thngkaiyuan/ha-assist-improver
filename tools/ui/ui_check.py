"""Scripted UI checks of the improver's screens on the Home Assistant at HA_URL, driven by Playwright.

  tools/ui-check login             opens a window; you sign in yourself, the session is saved
  tools/ui-check repairs           Repairs lists the AI model notice and opens its fix in place
  tools/ui-check panel             the panel: list, settings, summary, details, decision menu, light and dark (decides nothing)
  tools/ui-check diagnostics       downloads the improver's diagnostics into the private folder's diagnostics/
  tools/ui-check developer on|off  shows or hides the developer services (the integration reloads)

Home Assistant's address is HA_URL in the private folder's home-assistant.env (tools/private.py).
The saved session lives outside the repo and can be revoked in HA under
Profile -> Security -> Refresh tokens. Screenshots go to the private folder's ui/
because they show household data. Checks only open the improver's own screens
and close dialogs without submitting a change.

Setup: tools/e2e/.venv, with Playwright's Chromium in tools/ui/browsers (CONTRIBUTING.md).
"""

import json
import os
import sys
import time
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import private  # noqa: E402 - tools/private.py

HA = private.home_assistant("HA_URL")["HA_URL"]
STATE = Path.home() / ".config" / "assist-improver" / "ha-browser-state.json"
SHOTS = private.path() / "ui"
INTEGRATION = f"{HA}/config/integrations/integration/assist_improver"


def _shot(page, name):
    SHOTS.mkdir(parents=True, exist_ok=True)
    path = SHOTS / f"{time.strftime('%Y%m%d-%H%M%S')}-{name}.png"
    try:
        page.screenshot(path=str(path))
        print(f"screenshot: {path}")
    except Exception as err:  # Never hide the real failure behind a screenshot error.
        print(f"screenshot failed: {err}")


def login(p):
    browser = p.chromium.launch(headless=False)
    context = browser.new_context(ignore_https_errors=True)
    page = context.new_page()
    page.goto(HA)
    print("Sign in to Home Assistant in the window that opened (tick 'Keep me logged in').", flush=True)
    page.wait_for_url(lambda url: "/auth/" not in url, timeout=1_800_000)
    # Only "Keep me logged in" puts the refresh token in localStorage, which the saved state keeps.
    try:
        page.wait_for_function("() => localStorage.getItem('hassTokens')", timeout=30_000)
    except Exception:
        browser.close()
        sys.exit("Signed in, but no saved tokens: run login again and tick 'Keep me logged in'.")
    STATE.parent.mkdir(parents=True, exist_ok=True)
    context.storage_state(path=str(STATE))
    os.chmod(STATE, 0o600)
    print(f"session saved: {STATE}")
    browser.close()


def _session(p):
    if not STATE.exists():
        sys.exit("No saved session; run: tools/ui-check login")
    browser = p.chromium.launch()
    # No service worker: HA's frontend reloads the page when its worker takes over, a few seconds into a fresh
    # session, which would cut off a long action (an AI call) midway.
    context = browser.new_context(ignore_https_errors=True, storage_state=str(STATE), service_workers="block",
                                  viewport={"width": 1280, "height": 900})
    page = context.new_page()
    return browser, context, page


def _open(page, url):
    page.goto(url)
    page.wait_for_load_state("networkidle")
    if "/auth/" in page.url:
        sys.exit("The saved session has expired; run: tools/ui-check login")


def repairs(page):
    _open(page, f"{HA}/config/repairs")
    page.wait_for_timeout(2000)
    item = page.get_by_text("Assist Improver needs an AI model")
    if not item.count():
        if page.get_by_text("There are currently no repairs pending").count() == 0:
            expect(page.get_by_role("list", name="Repairs")).to_be_visible()  # Other integrations' repairs.
        _shot(page, "repairs-none")
        print("PASS: no AI model notice (a suitable AI Task is chosen)")
        return True
    expect(item).to_be_visible(timeout=20_000)
    _shot(page, "repairs")
    print("PASS: Repairs lists the improver's notice")
    item.click()
    expect(page.get_by_text("Which AI should study your conversations?")).to_be_visible(timeout=20_000)
    _shot(page, "repairs-fix")
    print("PASS: the notice opens the AI Task setup in place")
    page.keyboard.press("Escape")  # Closes without fixing; the notice stays.
    return True


def panel(page):
    """The review panel: the list, then the first improvement as it opens (its summary), then with its sections and
    first change open, in light and dark. Changes nothing (no decision is made)."""
    for scheme in ("light", "dark"):
        page.emulate_media(color_scheme=scheme)
        _open(page, f"{HA}/assist-improver")
        expect(page.locator("assist-improver-panel #open-settings")).to_be_visible(timeout=30_000)
        page.wait_for_timeout(1000)
        _shot(page, f"panel-home-{scheme}")
        page.locator("assist-improver-panel #open-settings").click()  # Settings: the pipeline track.
        expect(page.locator("assist-improver-panel .track .stn").first).to_be_visible(timeout=30_000)
        page.wait_for_timeout(500)
        _shot(page, f"panel-settings-{scheme}")
        page.go_back()
        expect(page.locator("assist-improver-panel #open-settings")).to_be_visible(timeout=30_000)
        page.wait_for_timeout(1000)
        if not page.locator("assist-improver-panel .item:visible").count():  # Earlier ones are folded away.
            print(f"PASS: panel home and settings ({scheme}); nothing up for review, so no review to open")
            continue
        page.locator("assist-improver-panel .item:visible").first.click()
        expect(page.get_by_text("What changes (").first).to_be_visible(timeout=30_000)  # Its title, folded.
        expect(page.get_by_text("Why it's safe", exact=False).first).to_be_visible()  # The summary, open on arrival.
        _shot(page, f"panel-summary-{scheme}")
        for title in ("What went wrong", "What changes ("):  # Folded on arrival.
            if page.get_by_text(title).count():
                page.get_by_text(title).first.click()
        page.wait_for_timeout(500)
        _shot(page, f"panel-review-{scheme}")
        page.get_by_text("What went wrong").first.scroll_into_view_if_needed()  # The details, at the end.
        _shot(page, f"panel-details-{scheme}")
        page.locator("assist-improver-panel #arrow").click()
        page.locator("assist-improver-panel [data-choice='revise']").click()
        expect(page.locator("assist-improver-panel textarea")).to_be_visible()
        _shot(page, f"panel-decision-{scheme}")
        print(f"PASS: panel list, review and decision menu ({scheme})")
    return True


CHECKS = {"repairs": repairs, "panel": panel}
# Actions that spend money take an argument and are never part of a check run.
def _service(page, arg):
    """service FILE.json - one improver service call, {"service": ..., "data": {...}}; the response is printed and
    saved beside it as FILE.result.json. It spends money only if the data allows paid inference."""
    request = json.loads(Path(arg).read_text())
    _open(page, INTEGRATION)
    page.wait_for_function("() => document.querySelector('home-assistant')?.hass?.connected", timeout=60_000)
    result = page.evaluate("""async ([service, data]) => {
        const hass = document.querySelector('home-assistant').hass;
        try {
            const r = await hass.callWS({type: 'call_service', domain: 'assist_improver', service,
                service_data: data, return_response: true});
            return {ok: true, response: r.response};
        } catch (e) { return {ok: false, error: e.message || String(e)}; }
    }""", [request["service"], request["data"]])
    Path(arg).with_suffix(".result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False))
    print(json.dumps(result, ensure_ascii=False)[:4000])
    return result["ok"]


def _get(page, path):
    """get API_PATH - one read-only GET of HA's REST API (for example history), printed as JSON."""
    _open(page, INTEGRATION)
    page.wait_for_function("() => document.querySelector('home-assistant')?.hass?.connected", timeout=60_000)
    result = page.evaluate("""async (path) => {
        const hass = document.querySelector('home-assistant').hass;
        try { return {ok: true, response: await hass.callApi('GET', path)}; }
        catch (e) { return {ok: false, error: e.message || String(e)}; }
    }""", path)
    print(json.dumps(result, ensure_ascii=False))
    return result["ok"]


def diagnostics(page):
    """The diagnostics HA's Download diagnostics gives, saved where household data belongs; a short summary printed."""
    _open(page, INTEGRATION)
    page.wait_for_function("() => document.querySelector('home-assistant')?.hass?.connected", timeout=60_000)
    result = page.evaluate("""async () => {
        const hass = document.querySelector('home-assistant').hass;
        const [entry] = await hass.callWS({type: 'config_entries/get', domain: 'assist_improver'});
        return await hass.callApi('GET', `diagnostics/config_entry/${entry.entry_id}`);
    }""")
    data = result.get("data", result)
    path = private.path() / "diagnostics" / f"{time.strftime('%Y%m%d-%H%M%S')}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    print(f"saved: {path}")
    print(json.dumps({k: data.get(k) for k in ("versions", "recordings", "storage", "sandboxes")}, ensure_ascii=False))
    for run in data.get("runs", [])[-3:]:
        print(run.get("started"), run.get("status"), json.dumps((run.get("progress") or {}).get("timings")))
    return True


def _developer(page, arg):
    """developer on|off - shows or hides the developer services; the integration reloads."""
    if arg not in ("on", "off"):
        sys.exit(__doc__)
    _open(page, INTEGRATION)
    page.wait_for_function("() => document.querySelector('home-assistant')?.hass?.connected", timeout=60_000)
    page.evaluate("""(on) => document.querySelector('home-assistant').hass.callWS(
        {type: 'assist_improver/settings/set', developer_services: on})""", arg == "on")
    print(f"developer services {arg}")
    return True


CHECKS["diagnostics"] = diagnostics  # Read-only, so it runs like a check.
ACTIONS = {"service": _service, "get": _get, "developer": _developer}


def main(argv):
    if not argv or argv[0] not in ("login", *CHECKS, *ACTIONS) or (argv[0] in ACTIONS and len(argv) != 2):
        sys.exit(__doc__)
    with sync_playwright() as p:
        if argv[0] == "login":
            return login(p)
        browser, context, page = _session(p)
        try:
            ok = ACTIONS[argv[0]](page, argv[1]) if argv[0] in ACTIONS else CHECKS[argv[0]](page)
        except Exception as err:
            _shot(page, f"{argv[0]}-failed")
            raise SystemExit(f"FAIL: {argv[0]}: {err}")
        finally:
            context.storage_state(path=str(STATE))  # Keep refreshed tokens.
            browser.close()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main(sys.argv[1:])
