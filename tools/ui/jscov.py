"""Line coverage of the panel's JavaScript (custom_components/assist_improver/www/) in the two browser suites
(tools/ui/test_panel.py, tools/e2e/conftest.py), for tools/check.sh --coverage: Chromium's own (V8's precise coverage,
over a CDP session). It measures only when IMPROVER_COVERAGE names a folder and the browser is Chromium; otherwise
each call does nothing (a WebKit run is unchanged).

- launch(browser_type, **options): the session's browser.
- start(page): before the page loads anything (a script that loaded before it isn't measured).
- collect(context): before the context closes, from each of its pages still open.
- dump(suite): at the session's end, IMPROVER_COVERAGE/js/js-<suite>-<pid>.json: {"breakable": {path: lines},
  "covered": {path: lines}}, each path under www/ (every file there), lines from 1.

A line is coverable when it holds a statement or call V8 can stop at (Debugger.getPossibleBreakpoints; a function's
exit isn't one), and covered when V8 counted one of those as run.
"""

import json
import os
import re
from bisect import bisect_left
from pathlib import Path
from urllib.parse import urlparse

DIR = os.environ.get("IMPROVER_COVERAGE")
WWW = Path(__file__).resolve().parent.parent.parent / "custom_components/assist_improver/www"
STATIC = "/assist_improver_static/"  # Where Home Assistant serves www/ (panel.py's static path), and the panel suite.
ORIGIN = "http://jscov.test"  # Where read() loads the checkout's www/ (answered in the browser, never on the network).
COMMENT = ("//".encode("utf-16-le"), "/*".encode("utf-16-le"))

_sessions = {}  # Each page measured: its CDP session.
_places = {}  # Each file's coverable places, [(offset, line)] in order, and its length (both in UTF-16 units).
_covered = {}  # Each file's lines run, in any page.


def launch(browser_type, **options):
    """browser_type.launch(**options), measuring with RenderDocument off: with it on, each navigation moves the page
    to a new frame, and what ran in the document before is lost. Chromium takes only the last --disable-features,
    so RenderDocument joins the ones Playwright passes (read from the command line of a first launch)."""
    browser = browser_type.launch(**options)
    if DIR and browser_type.name == "chromium":
        line = browser.new_browser_cdp_session().send("SystemInfo.getInfo")["commandLine"]
        browser.close()
        features = re.findall(r"--disable-features=(\S+)", line)[-1:] + ["RenderDocument"]
        browser = browser_type.launch(**{**options, "args": [*options.get("args", ()),
                                                               "--disable-features=" + ",".join(features)]})
    return browser


def start(page):
    if DIR and page.context.browser.browser_type.name == "chromium":
        _sessions[page] = measure(page)


def collect(context):
    for page in [page for page in _sessions if page.context == context]:
        cdp = _sessions.pop(page)
        if page.is_closed():  # Closed by the test: what ran there is gone.
            continue
        for path, script in ours(cdp):
            if not _places:
                _places.update(read(context.browser))
            places, length = _places[path]
            if script["functions"][0]["ranges"][0]["endOffset"] != length:  # The script's own range: all of it.
                raise AssertionError(f"JS coverage: the {path} served isn't the checkout's {WWW / path}")
            _covered.setdefault(path, set()).update(covered(script["functions"], places))


def dump(suite):
    if _places:
        folder = Path(DIR) / "js"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"js-{suite}-{os.getpid()}.json").write_text(json.dumps({
            "breakable": {path: sorted({line for _, line in places}) for path, (places, _) in _places.items()},
            "covered": {path: sorted(lines) for path, lines in _covered.items()}}))


def measure(page):
    cdp = page.context.new_cdp_session(page)
    cdp.send("Profiler.enable")
    cdp.send("Profiler.startPreciseCoverage", {"detailed": True})
    return cdp


def ours(cdp):
    """www/'s scripts in V8's coverage since it was last taken, (path, script): a page may load one more than once."""
    for script in cdp.send("Profiler.takePreciseCoverage")["result"]:
        path = urlparse(script["url"]).path
        if path.startswith(STATIC):
            yield path[len(STATIC):], script


def read(browser):
    """Each www/ file's coverable places and length, from V8, with the checkout's files loaded as modules in a page of
    their own: a page measured may have left the document its scripts ran in, and V8's debugger forgets those."""
    context = browser.new_context()
    try:
        page = context.new_page()
        page.route(f"{ORIGIN}/**", serve)
        cdp = measure(page)
        page.goto(ORIGIN)
        files = sorted(str(file.relative_to(WWW)) for file in WWW.rglob("*.js"))
        page.evaluate("(urls) => Promise.allSettled(urls.map((url) => import(url)))",
                      [ORIGIN + STATIC + path for path in files])
        scripts = dict(ours(cdp))
        cdp.send("Debugger.enable")
        places = {}
        for path in files:
            text = (WWW / path).read_text()
            length = len(text.encode("utf-16-le")) // 2
            places[path] = breakable(text, locations(cdp, scripts[path]["scriptId"])), length
        return places
    finally:
        context.close()


def serve(route):
    """The checkout's www/ at Home Assistant's static path, and an empty page at any other address."""
    path = urlparse(route.request.url).path
    if path.startswith(STATIC):
        route.fulfill(content_type="text/javascript", body=(WWW / path[len(STATIC):]).read_text())
    else:
        route.fulfill(content_type="text/html", body="")


def locations(cdp, script):
    """Debugger.getPossibleBreakpoints for all of a script: V8 answers up to 1000 at a time, so ask again from just
    past the last."""
    found = []
    while True:
        at = (found[-1]["lineNumber"], found[-1]["columnNumber"] + 1) if found else (0, 0)
        more = [location for location in cdp.send("Debugger.getPossibleBreakpoints", {"start": {
            "scriptId": script, "lineNumber": at[0], "columnNumber": at[1]}})["locations"]
            if (location["lineNumber"], location["columnNumber"]) >= at]
        if not more:
            return found
        found += more


def breakable(text, locations):
    """The places V8 can stop at that make a line coverable, [(offset, line)]: statements and calls, not a function's
    exit, nor a place on a comment (a module's start can be one). Offsets in UTF-16 units, as V8 counts them."""
    units = text.encode("utf-16-le")
    starts = [0]
    for line in text.split("\n"):
        starts.append(starts[-1] + len(line.encode("utf-16-le")) // 2 + 1)
    places = []
    for location in locations:
        offset = starts[location["lineNumber"]] + location["columnNumber"]
        if location.get("type") != "return" and units[2 * offset:2 * offset + 4] not in COMMENT:
            places.append((offset, location["lineNumber"] + 1))
    return places


def covered(functions, places):
    """The lines of `places` ([(offset, line)] in order) whose place V8 counted as run: the innermost of its ranges
    holding the place decides (they nest). A declared function's own range counts from one past its start: its first
    character belongs to the code around it (a declaration's place is on the function it declares). The script's own
    range, the first, starts at its start."""
    offsets = [offset for offset, _ in places]
    counts = [0] * len(places)
    ranges = [(r["startOffset"] + (i == 0 and n > 0), r["endOffset"], r["count"])
              for n, function in enumerate(functions) for i, r in enumerate(function["ranges"])]
    for start, end, count in sorted(ranges, key=lambda r: (r[0], -r[1])):
        first, last = bisect_left(offsets, start), bisect_left(offsets, end)
        counts[first:last] = [count] * (last - first)
    return {line for (_, line), count in zip(places, counts) if count}
