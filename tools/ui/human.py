"""A browser a stand-in tester drives the way a person would: look at the page, then click, type or scroll by what
they see (a button's words, a checkbox's label, a field's placeholder), one command at a time. Each tester has their
own browser, which stays open between commands.

  PLAYWRIGHT_BROWSERS_PATH=tools/ui/browsers tools/e2e/.venv/bin/python tools/ui/human.py start NAME [--phone] [--url URL]
  ... look NAME                  a screenshot (its path) and what the page shows, as roles and names
  ... click NAME "words" [--nth N]   the visible button, link, checkbox or text with those words
  ... type NAME "field" "text"   into the field with that placeholder or label (replacing what's there)
  ... press NAME Enter | scroll NAME down|up | wait NAME SECONDS | stop NAME
"""

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

HOME = Path(os.environ.get("HUMAN_DIR", "/tmp/assist-improver-human"))
URL = "http://127.0.0.1:8765/assist-improver"


def state_file(name):
    return HOME / name / "state.json"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start(name, phone=False, url=URL):
    folder = HOME / name
    folder.mkdir(parents=True, exist_ok=True)
    port = free_port()
    with sync_playwright() as p:
        executable = p.chromium.executable_path
    width, height = (402, 874) if phone else (1280, 800)
    process = subprocess.Popen([executable, "--headless=new", f"--remote-debugging-port={port}",
                                f"--user-data-dir={folder / 'profile'}", f"--window-size={width},{height}", "--no-first-run",
                                "--force-device-scale-factor=1", url],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    state_file(name).write_text(json.dumps({"port": port, "pid": process.pid, "shots": 0}))
    for _ in range(50):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.2)
    time.sleep(2)
    print(f"Started {name} ({'phone' if phone else 'desktop'}) at {url}")


def page_of(p, name):
    state = json.loads(state_file(name).read_text())
    browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{state['port']}")
    pages = [pg for ctx in browser.contexts for pg in ctx.pages]
    return state, pages[0]


def look(name):
    with sync_playwright() as p:
        state, page = page_of(p, name)
        state["shots"] += 1
        shot = HOME / name / f"look-{state['shots']:03d}.png"
        page.screenshot(path=str(shot))
        state_file(name).write_text(json.dumps(state))
        snapshot = page.locator("body").aria_snapshot()
        print(f"Screenshot: {shot}\nURL: {page.url}\nWhat the page shows:\n{snapshot[:6000]}")


def find(page, words, nth=0):
    """What a person means by those words: a control named exactly so, else one whose name contains them."""
    for exact in (True, False):
        for locator in (page.get_by_role("button", name=words, exact=exact), page.get_by_role("checkbox", name=words, exact=exact),
                        page.get_by_role("link", name=words, exact=exact), page.get_by_label(words, exact=exact),
                        page.get_by_text(words, exact=exact)):
            visible = [locator.nth(i) for i in range(locator.count()) if locator.nth(i).is_visible()]
            if len(visible) > nth:
                return visible[nth]
    return None


def act(name, verb, words=None, text=None, nth=0):
    with sync_playwright() as p:
        _, page = page_of(p, name)
        if verb == "press":
            page.keyboard.press(words)
        elif verb == "scroll":
            page.mouse.wheel(0, 600 if words == "down" else -600)
        elif verb == "wait":
            page.wait_for_timeout(float(words) * 1000)
        else:
            target = page.get_by_placeholder(words).first if verb == "type" and page.get_by_placeholder(words).count() \
                else find(page, words, nth)
            if target is None:
                print(f"Nothing visible matches “{words}”. Look again to see what's there.")
                return
            if verb == "type":
                target.fill(text)
            else:
                target.click()
        page.wait_for_timeout(700)
        print(f"Done: {verb} {words or ''}".strip())


def stop(name):
    state = json.loads(state_file(name).read_text())
    try:
        os.kill(state["pid"], 15)
    except ProcessLookupError:
        pass
    print(f"Stopped {name}")


if __name__ == "__main__":
    args = sys.argv[1:]
    command, name = args[0], args[1]
    if command == "start":
        url = args[args.index("--url") + 1] if "--url" in args else URL
        start(name, "--phone" in args, url)
    elif command == "look":
        look(name)
    elif command == "stop":
        stop(name)
    elif command == "type":
        act(name, "type", args[2], args[3])
    else:
        nth = int(args[args.index("--nth") + 1]) if "--nth" in args else 0
        act(name, command, args[2] if len(args) > 2 else None, nth=nth)
