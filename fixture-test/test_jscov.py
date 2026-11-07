"""The panel's JS coverage (tools/ui/jscov.py), from synthetic answers in the shapes Chromium's V8 gives: which lines
are coverable, which ran, and the file each browser suite's worker leaves."""

import importlib.util
import json
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("jscov", ROOT / "tools/ui/jscov.py")
jscov = importlib.util.module_from_spec(spec)
spec.loader.exec_module(jscov)

SOURCE = """// A module: V8 can put a place on its first comment.
const unused = (x) =>
  x + "🙂";
function used(flag) {
  if (flag) {
    skipped();
  }
  return flag;
}
used(false);
"""
END = len(SOURCE.encode("utf-16-le")) // 2


def at(snippet):
    """Where `snippet` starts in SOURCE, in UTF-16 units (as V8 counts; the emoji is two)."""
    return len(SOURCE[:SOURCE.index(snippet)].encode("utf-16-le")) // 2


def place(snippet, kind=None):
    """V8's location of a place: line and column from 0, the column in UTF-16 units."""
    before = SOURCE[:SOURCE.index(snippet)]
    line = before.count("\n")
    column = len(before[before.rfind("\n") + 1:].encode("utf-16-le")) // 2
    return {"scriptId": "7", "lineNumber": line, "columnNumber": column, **({"type": kind} if kind else {})}


# Debugger.getPossibleBreakpoints, as Chromium answers for SOURCE (and a place on the first comment, as it gives for
# one of the panel's modules): statements, calls, and each function's exit (type "return").
LOCATIONS = [place("// A module"), place("(x) =>"), place('+ "'), place(";\nfunction", "return"),
             place("if (flag)"), place("skipped()", "call"), place("return flag"), place("\n}\nused", "return"),
             place("used(false)", "call")]


def span(start, end, count):
    return {"startOffset": at(start), "endOffset": at(end), "count": count}


# Profiler.takePreciseCoverage's functions, as Chromium answers after used(false): the module ran, `unused` never,
# `used` once with its `if` not taken (a block range inside it).
FUNCTIONS = [{"functionName": "", "ranges": [{"startOffset": 0, "endOffset": END, "count": 1}]},
             {"functionName": "unused", "ranges": [span("(x) =>", ";\nfunction", 0)]},
             {"functionName": "used", "ranges": [span("function used", "\nused(false)", 1),
                                                 span("{\n    skipped", "\n  return", 0)]}]


def test_coverable_lines_are_v8s_statement_and_call_places():
    assert jscov.breakable(SOURCE, LOCATIONS) == [
        (at("(x) =>"), 2), (at('+ "'), 3), (at("if (flag)"), 5), (at("skipped()"), 6), (at("return flag"), 8),
        (at("used(false)"), 10)]  # Not the comment, nor a function's exit; offsets past the emoji count it as two.


def test_a_line_is_covered_when_v8_counted_its_place_as_run():
    # Line 2 declares `unused`, which never ran: its place sits on the function's first character, which belongs to
    # the module around it. Line 6 is inside `used`, which ran, but in the `if` that didn't.
    assert jscov.covered(FUNCTIONS, jscov.breakable(SOURCE, LOCATIONS)) == {2, 5, 8, 10}


class Fake:
    def __init__(self, **attributes):
        self.__dict__.update(attributes)


class CDP:
    """A page's CDP session, after the module loaded twice (two ?v=; `served`: its length), with a script of Home
    Assistant's own; and V8's debugger, answering two places at a time (it caps at 1000)."""

    def __init__(self, served=END):
        self.served = served

    def send(self, method, params=None):
        if method == "Profiler.takePreciseCoverage":
            ours = "http://127.0.0.1:8123/assist_improver_static/panel/x.js"
            again = [{**FUNCTIONS[0], "ranges": [{**FUNCTIONS[0]["ranges"][0], "endOffset": self.served}]},
                     FUNCTIONS[1], {**FUNCTIONS[2], "ranges": [{**r, "count": 1} for r in FUNCTIONS[2]["ranges"]]}]
            theirs = "http://127.0.0.1:8123/frontend_latest/app.js"
            return {"result": [{"scriptId": "7", "url": f"{ours}?v=1", "functions": FUNCTIONS},
                               {"scriptId": "8", "url": f"{ours}?v=2", "functions": again},
                               {"scriptId": "9", "url": theirs, "functions": []}]}
        if method == "Debugger.getPossibleBreakpoints":
            start = (params["start"]["lineNumber"], params["start"]["columnNumber"])
            return {"locations": [location for location in LOCATIONS
                                  if (location["lineNumber"], location["columnNumber"]) >= start][:2]}
        return {}


def test_every_place_is_read_past_v8s_cap_on_one_answer():
    assert jscov.locations(CDP(), "7") == LOCATIONS


def measure(monkeypatch, tmp_path, cdp):
    """One page measured in Chromium, then its context closed, with www/ holding SOURCE as panel/x.js."""
    monkeypatch.setattr(jscov, "DIR", str(tmp_path))
    monkeypatch.setattr(jscov, "read", lambda browser: {"panel/x.js": (jscov.breakable(SOURCE, LOCATIONS), END)})
    for state in ("_sessions", "_places", "_covered"):
        monkeypatch.setattr(jscov, state, {})
    context = Fake(browser=Fake(browser_type=Fake(name="chromium")), new_cdp_session=lambda page: cdp)
    page = Fake(context=context, is_closed=lambda: False)
    jscov.start(page)
    jscov.collect(context)


def test_each_file_is_kept_by_its_path_with_what_ran_in_any_load(monkeypatch, tmp_path):
    measure(monkeypatch, tmp_path, CDP())
    jscov.dump("panel")
    assert json.loads((tmp_path / f"js/js-panel-{os.getpid()}.json").read_text()) == {
        "breakable": {"panel/x.js": [2, 3, 5, 6, 8, 10]}, "covered": {"panel/x.js": [2, 5, 6, 8, 10]}}


def test_a_served_file_that_isnt_the_checkouts_fails(monkeypatch, tmp_path):
    with pytest.raises(AssertionError, match="panel/x.js served isn't the checkout's"):
        measure(monkeypatch, tmp_path, CDP(served=END + 1))
