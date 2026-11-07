"""Testable specs for the panel (custom_components/assist_improver/www/panel.js and the modules it loads from panel/):
each test renders the panel in a real browser from canned websocket replies, with no Home Assistant, the way
tools/ui/panel_harness.py does, and checks one promise the panel makes (SPEC.md, DETAILS.md, README.md,
docs/DESIGN.md, the panel's TEXT and comments). Set up and run as CONTRIBUTING.md says (from a worktree, the main
checkout's venv and browsers):

  PLAYWRIGHT_BROWSERS_PATH=tools/ui/browsers tools/e2e/.venv/bin/python -m pytest tools/ui/test_panel.py -q \
      -p no:cacheprovider -n 4

PANEL_BROWSER=webkit runs them in WebKit (Safari, and Home Assistant's iPhone and macOS apps), once it's installed
there: PLAYWRIGHT_BROWSERS_PATH=<main checkout>/tools/ui/browsers <main checkout>/tools/e2e/.venv/bin/python \
-m playwright install webkit. Chromium otherwise, measuring the panel's JavaScript when IMPROVER_COVERAGE is set
(tools/check.sh --coverage, tools/ui/jscov.py).

The page is served from a stand-in origin (http://panel.test, answered in the browser, never on the network) so the
panel's own addresses (?id=..., ?view=settings) work, and the clock is fixed so times read the same every run. Every
reply is synthetic; replies follow the shapes panel.py, settings.py and pipeline.py send.
"""

import itertools
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import pytest
from playwright.sync_api import expect, sync_playwright

import jscov

ROOT = Path(__file__).resolve().parent.parent.parent
WWW = ROOT / "custom_components/assist_improver/www"  # panel.js, and the modules it loads from panel/.
STATIC = "/assist_improver_static/"  # Where Home Assistant serves www/ (panel.py's static path).
ORIGIN = "http://panel.test"
PREFER = "assist_pipeline/pipeline/set_preferred"  # Home Assistant's own: the assistant Assist uses by default.
NOW = datetime(2026, 10, 25, 2, 0, tzinfo=timezone.utc)
PHONE = {"width": 402, "height": 874}  # The design target.
SMALL_PHONE = {"width": 360, "height": 780}
TABLET = {"width": 900, "height": 1000}  # Near where the settings track switches between its two layouts.
DESKTOP = {"width": 1280, "height": 800}
SIZES = {"small-phone": SMALL_PHONE, "phone": PHONE, "desktop": DESKTOP}
PLAY = "M8 5.14v13.72L19 12z"  # The play icon's path.
CLOCK = ("M12 20a8 8 0 1 0 0-16 8 8 0 0 0 0 16zm0-18a10 10 0 1 1 0 20 10 10 0 0 1 0-20zm.5 5v5.25l4.5 2.67-.75 "
         "1.23L11 13V7z")  # The clock icon's path: Run now's menu when it also runs on a schedule.
# HTML in every dynamic string: shown as text, never run. It closes an attribute too, and an entity reads literally.
XSS = '"><img src=x onerror="window.__xss=1">&amp;<b>x'

# Home Assistant's default dark theme, as the panel's CSS variables read it (as in tools/ui/panel_harness.py).
DARK = """body { margin: 0; background: #111111; font-family: Roboto, -apple-system, sans-serif;
  --primary-color: #009ac7; --primary-text-color: #e1e1e1; --secondary-text-color: #9b9b9b;
  --text-primary-color: #ffffff; --card-background-color: #1c1c1c; --primary-background-color: #111111;
  --secondary-background-color: #282828; --divider-color: rgba(225, 225, 225, 0.12);
  --app-header-background-color: #1c1c1c; --app-header-text-color: #e1e1e1; color: #e1e1e1; }"""
# And its default light theme.
LIGHT = """body { margin: 0; background: #fafafa; font-family: Roboto, -apple-system, sans-serif;
  --primary-color: #009ac7; --primary-text-color: #212121; --secondary-text-color: #727272;
  --text-primary-color: #ffffff; --card-background-color: #ffffff; --primary-background-color: #fafafa;
  --secondary-background-color: #e5e5e5; --divider-color: rgba(0, 0, 0, 0.12);
  --app-header-background-color: #009ac7; --app-header-text-color: #ffffff; color: #212121; }"""


def ago(minutes):
    return (NOW - timedelta(minutes=minutes)).isoformat()


# Replies, shaped as the websocket commands send them.

def item(id, title, status="awaiting_review", enabled=0, removed=0):
    return {"id": id, "title": title, "status": status, "pending": status in ("awaiting_review", "needs_attention"),
            "enabled": enabled, "removed": removed, "spared": 0, "followed": 0}


GARAGE = "Open or close garage doors by pressing their buttons"
CAR = "Warm up the car straight away"
PENDING = [item("imp1", GARAGE, enabled=1, removed=2), item("imp2", CAR, removed=1)]
EARLIER = [item("imp0", "Say good night to turn off the downstairs lights", status="applied", enabled=1)]
STEP_STAGES = {"triage": "Triage", "analyze": "Analyze", "propose": "Propose", "write": "Write up"}  # flow.KEYS


def preview(admits=0, held=None, paused=(), review=0, applied=0, causes=None, quick=None, analyses=None,
            blocked_models=None, spend=None, which=None):
    """As Pipeline.preview sends it. admits: the new ones a manual full run takes in (those that would stop at a
    blocked step left out); held: {step: n} held at each step (for being blocked where the step is, else cut off;
    causes: as given), which a manual full run releases unless the step is blocked; quick, analyses: the cost line's
    counts (by default each new one gets the quick check and reaches analysis, as do those released at Triage, and
    those released at Analyze reach analysis)."""
    held = {step: n for step, n in (held or {}).items() if n}
    releases = {step: n for step, n in held.items() if step not in paused}
    checked = admits + releases.get("triage", 0)
    return {"stages": {"Your Assist": admits, **{stage: held.get(step, 0) for step, stage in STEP_STAGES.items()},
                       "Your review": review, "Your household": 0},
            "held": causes or {step: {"blocked" if step in paused else "cut off": n} for step, n in held.items()},
            "admits": admits, "releases": releases, "which": list(which or []),
            "quick": checked if quick is None else quick,
            "analyses": (0 if "analyze" in paused else checked + releases.get("analyze", 0)) if analyses is None
            else analyses, "paused": sorted(paused), "blocked_models": blocked_models or {}, "spend": spend or {},
            "running": False, "applied": applied}


def run(status="done", stage="write", progress=None, error=None, trigger="manual", only=None, minutes=5):
    return {"id": "r1", "kind": "pipeline", "trigger": trigger, "status": status, "started": ago(minutes + 2),
            "finished": None if status == "running" else ago(minutes), "stage": stage, "progress": progress or {},
            "error": error, "only": only}


def status(last=None, **kw):
    return {"run": last, "preview": preview(**kw)}


def household(everyone=(), people=(), pipelines=(), me=("u-alex", "Alex")):
    people = [dict(p) for p in people]
    return {"lines": len(everyone) + sum(len(p["lines"]) for p in people), "everyone": list(everyone),
            "people": people, "me": {"user_id": me[0], "name": me[1]}, "pipelines": list(pipelines)}


OPUS = {"entity_id": "ai_task.claude", "label": "Opus 5.5", "title": "Claude AI Task", "provider": "anthropic",
        "warnings": [], "strong": True, "weak": False, "small": False, "known": True}
HAIKU = {"entity_id": "ai_task.claude_haiku", "label": "Haiku 4.5", "title": "Assist Improver (Claude Haiku 4.5)",
         "provider": "anthropic", "warnings": ["It runs a small model, which may miss what you meant."],
         "strong": False, "weak": True, "small": True, "known": True}
LOCAL = {"entity_id": "ai_task.local", "label": "Local LLM", "title": "Local LLM", "provider": None, "warnings": [],
         "strong": False, "weak": False, "small": False, "known": False}
ANTHROPIC = {"entry_id": "p1", "title": "Anthropic", "domain": "anthropic"}


def role(entity_id, size, optional=False, problem=None):
    return {"entity_id": entity_id, "size": size, "optional": optional, "problem": problem}


def settings(roles=None, tasks=None, providers=(), recommended=True, tests=3, schedule="off", paused=()):
    base = {"triage": role(HAIKU["entity_id"], "small", optional=True), "analyzer": role(OPUS["entity_id"], "large"),
            "generator": role(OPUS["entity_id"], "large"), "reviewer": role(OPUS["entity_id"], "large"),
            "feedback_check": role(HAIKU["entity_id"], "small", optional=True),
            "editor": role(HAIKU["entity_id"], "small")}
    return {"roles": {**base, **(roles or {})}, "ai_tasks": [OPUS, HAIKU, LOCAL] if tasks is None else tasks,
            "providers": list(providers), "recommended": recommended, "tests_per_goal": tests, "schedule": schedule,
            "paused": sorted(paused)}


CHOICES = [{"value": "accept", "label": "Accept changes"}, {"value": "revise", "label": "Ask for a revision"},
           {"value": "decline", "label": "Decline", "reasons": ["not_an_issue"]}]
SCRIPT_ROWS = [{"sign": "+", "old": None, "new": n + 1, "text": text} for n, text in enumerate(
    ["alias: Garage doors", "sequence:", "- action: button.press", "  target:",
     "    entity_id: button.garage_left_relay"])]
INSTRUCTION_ROWS = [{"sign": " ", "old": 1, "new": 1, "text": "Keep answers short."},
                    {"sign": "-", "old": 2, "new": None, "text": "Ask which garage door."},
                    {"sign": "+", "old": None, "new": 2, "text": "Both garage doors open together."},
                    {"sign": "gap"}, {"sign": " ", "old": 9, "new": 9, "text": "Use metric units."}]
# Why it's safe as panel.safety gives it for the changes below (a new script, an instruction change).
SAFETY = ["Tested on your own conversations, replayed in a sandboxed copy of your home: nothing in your home moved.",
          "Only settings change: 1 new script, 1 instruction change. Nothing runs when you accept.",
          "Checked against your Home Assistant first, applied all or nothing, and one click to undo."]


def open_all(panel):
    """Every folded section opened, as a reader would, one after another (what's open stays open)."""
    for _ in range(30):
        closed = panel.find(".page details:not([open]) > summary")
        if not closed.count():
            return
        closed.first.click()


# Each test's conversation as it went (the turns it covers) and its replay with the change, as panel.review sends them.
WHICH_DOOR = {"step": "Turn on · Garage door", "error": "Multiple entities matched 'Garage door'"}
OPENED = {"before": [{"you": "Open the garage.", "assist": "Which garage door?", "failed": [WHICH_DOOR]},
                     {"you": "Both.", "assist": "Which one first?"},
                     {"you": "Both doors, at once.", "assist": "Done: both garage doors are opening."}],
          "after": [{"you": "Open the garage.", "assist": "Done: both garage doors are opening."}]}
CLOSED = {"before": [{"you": "Close the garage, it's late.", "assist": "Which garage door?"}],
          "after": [{"you": "Close the garage, it's late.", "assist": "Which garage door?"}]}


def review(**over):
    base = {"id": "imp1", "title": GARAGE, "benefit": "Say “open the garage” and both doors open, first time.",
            "safety": SAFETY, "status": "awaiting_review", "enabled": 1, "removed": 2, "spared": 0, "followed": 0,
            "still_open": [], "recent_outcome": None,
            "recent_outcome_ok": False, "revises": None, "unrevised": None, "conflicts": [],
            "changes": [{"name": "Garage doors", "kind": "script", "rows": SCRIPT_ROWS, "added": 5, "removed": 0},
                        {"name": "Household knowledge", "kind": "instructions", "rows": INSTRUCTION_ROWS, "added": 1,
                         "removed": 1}],
            "cause": "Assist couldn't press both garage relays at once, so it asked which door.", "physical": True,
            "automations": [],
            "tests": [{"summary": "Open the garage", "improved": True, "result": "3 → 1 turns", **OPENED},
                      {"summary": "Close the garage at night", "improved": False, "result": "not improved", **CLOSED}],
            "choices": CHOICES}
    return {**base, **over}


DECIDED = {"result": "Applied. Assist uses it from your next request; no script was run.", "status": "applied",
           "ok": True}


def event_run(status="done", progress=None, error=None, trigger="manual", only=None, minutes=5):
    return {"at": ago(minutes), "kind": "run", "status": status, "trigger": trigger, "only": only,
            "progress": progress or {}, "error": error}


def event_posted(id="imp1", title=GARAGE, minutes=10):
    return {"at": ago(minutes), "kind": "posted", "id": id, "title": title}


def event_decision(decision, by="Alex", id="imp1", title=GARAGE, minutes=3, reason=None):
    return {"at": ago(minutes), "kind": "decision", "id": id, "title": title, "decision": decision, "reason": reason,
            "outcome": None, "by": by}


CHATS = [{"id": "c1", "started": ago(5), "turns": 2, "said": "Turn on the porch light.", "status": "waiting", "note": None,
          "failed": 1},
         {"id": "c2", "started": ago(90), "turns": 1, "said": "Open the garage.", "status": "working", "note": None,
          "failed": 0},
         {"id": "c3", "started": ago(3000), "turns": 1, "said": "What's the weather?", "status": "sent",
          "note": "It should know the town.", "failed": 0},
         {"id": "c4", "started": ago(4000), "turns": 3, "said": "Warm up the car.", "status": "waiting", "note": None,
          "failed": 0}]


# What each standing can do, as conversations.row says for a past conversation: be sent in (from Your household, or at
# Your Assist), and be taken by a picked run (new, sent in, looked smooth, or held).
SENDABLE = ("waiting", "sent", "active", "smooth", "fine", "nofix", "duplicate", "improved", "declined", "test",
            "unreadable", "archived")
RUNNABLE = ("waiting", "sent", "smooth")
# The flow stage each chip stands at (one held at Triage shows New or Sent in, as one at Your Assist does).
AT = {"active": "Your Assist", "waiting": "Your Assist", "sent": "Your Assist", "queued": "Analyze",
      "working": "Propose", "writing": "Write up", "review": "Your review", "unclear": "Your review",
      "cannot": "Your review"}


def can(row):
    """A row with what it can do (sendable, runnable, archivable: not while its improvement waits for the household)
    and its stage, from where it stands, unless it says."""
    stage = "Triage" if row.get("held") and row["status"] in ("waiting", "sent") else AT.get(row["status"], "Your household")
    return {"sendable": row["status"] in SENDABLE, "runnable": row["status"] in RUNNABLE or bool(row.get("held")),
            "archivable": row["status"] != "review", "stage": stage, **row}


def listing(rows=None, hidden=0, by_status=None, stands=()):
    """The conversations reply: a page, how many match, where they all stand and what a run can take of them, how many
    are left out, how many stand each way (by_status: as the rows do, unless given), and where those asked for (ids)
    stand, whatever the filters (stands)."""
    rows = [can(r) for r in (CHATS if rows is None else rows)]
    counts = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return {"conversations": rows, "total": len(rows), "hidden": hidden,
            "every": [{"id": r["id"], "status": r["status"], "stage": r["stage"], "hidden": bool(r.get("hidden")),
                       "runnable": r["runnable"], "archivable": r["archivable"]} for r in rows],
            "by_status": counts if by_status is None else by_status, "stands": [can(r) for r in stands]}


def chat(status="waiting", note=None, id="c1"):
    return can({"id": id, "started": ago(5), "status": status, "note": note,
                "turns": [{"you": "Turn on the porch light.", "assist": "Which porch light?"},
                          {"you": "The front one.", "assist": "Done."}]})


NEW = {"admits": 3}  # CHATS as a manual full run admits them: c1, c4 and c3 (sent in).


def replies(**over):
    """Every command the panel may send, with a plain answer; keyword names drop the assist_improver/ prefix and use
    __ for / (run__start is assist_improver/run/start)."""
    base = {"improvements": {"improvements": []}, "household": household(), "run": status(run()),
            "settings": settings(), "log": {"events": []}, "improvement": review(),
            "run/start": {"run": run("running", stage="analyze")}, "run/cancel": {"cancelled": True},
            "decide": DECIDED, "settings/set": settings(),
            "household/edit": {"lines": 1}, "settings/recommended": {**settings(), "notes": []},
            "conversations": listing(),
            "conversation": chat(), "conversation/send": {"status": "sent", "hidden": False}, "answer": {},
            "conversation/hide": {"hidden": True}, "conversation/delete": {"deleted": "c1"},
            "conversations/hide": {"changed": 2}, "conversations/delete": {"deleted": ["c1", "c2"]},
            "changeset/finish": {}, "editors/saved": {}}
    base.update({key.replace("__", "/"): value for key, value in over.items()})
    # Home Assistant's own command for the assistant Assist uses: it answers nothing.
    return {f"assist_improver/{key}": value for key, value in base.items()} | {PREFER: None}


def fail(message, code=None):
    """A refusal: as Home Assistant's callWS rejects, {code, message} with a code."""
    return {"__error": message, **({"__code": code} if code else {})}


def asking(*saves):
    """What the improver asks the panel to save while a command is answered (panel.EditorChannel): a script's (id,
    config), or (kind, id, config) for any editor's item; config None to delete it."""
    return [{"kind": s[0], "id": s[1], "config": s[2]} if len(s) == 3 else {"kind": "script", "id": s[0], "config": s[1]}
            for s in saves]


# The browser side: a stand-in hass whose callWS answers from the replies (and records each call), and the panel.

STUB = """(replies) => {
  window.__calls = []; window.__replies = replies; window.__holds = {}; window.__inflight = 0; window.__changes = {};
  // The panel's saver: Home Assistant's editor endpoints (callApi), recorded, refusing as told (__apiFail:
  // {"POST config/<kind>/config/<id>": HA's refusal}) and holding a call as told (__apiHolds); and the improver's asks
  // (__asks: {command type: {saves: [{kind, id, config}], wait}}), each sent to the subscriber while that command is
  // answered, waiting for its editors/saved (unless not wait: the improver gave up on it). The connection keeps a
  // subscription until a reconnect (__reconnect: its "ready", with the next subscriptions refused as told).
  window.__api = []; window.__apiFail = {}; window.__apiHolds = {}; window.__asks = {}; window.__subscribers = [];
  window.__waiting = {}; window.__listeners = {}; window.__subscribeOptions = []; window.__refuseSubscribe = 0;
  window.__callApi = async (method, path, data) => {
    window.__api.push({method, path, data: data === undefined ? null : JSON.parse(JSON.stringify(data))});
    const hold = window.__apiHolds[method + " " + path];
    if (hold) await hold.done;
    const refused = window.__apiFail[method + " " + path];
    if (refused) throw JSON.parse(JSON.stringify(refused));
    return {result: "ok"};
  };
  window.__connection = {
    subscribeMessage: async (callback, msg, options) => {
      window.__calls.push(JSON.parse(JSON.stringify(msg)));
      window.__subscribeOptions.push(options === undefined ? null : JSON.parse(JSON.stringify(options)));
      if (window.__refuseSubscribe > 0) {  // The improver isn't set up yet: Home Assistant doesn't know the command.
        window.__refuseSubscribe--;
        throw {code: "unknown_command", message: "Unknown command."};
      }
      window.__subscribers.push(callback);
      return () => { window.__subscribers = window.__subscribers.filter((c) => c !== callback); };
    },
    addEventListener: (type, listener) => { (window.__listeners[type] ||= []).push(listener); },
  };
  window.__reconnect = (refused) => {  // As home-assistant-js-websocket reconnects: subscriptions gone, then "ready".
    window.__subscribers = []; window.__refuseSubscribe = refused;
    for (const listener of window.__listeners.ready || []) listener(window.__connection);
  };
  let keys = 0;
  const ask = (saves) => saves.reduce((before, save) => before.then(() => new Promise((saved) => {
    const key = "k" + (++keys);
    window.__waiting[key] = saved;
    for (const callback of window.__subscribers) callback({key, ...save});
  })), Promise.resolve());
  window.__hold = (type) => { let release; const done = new Promise((r) => { release = r; });
                              window.__holds[type] = {done, release}; };
  window.__release = (type) => { const hold = window.__holds[type]; delete window.__holds[type]; if (hold) hold.release(); };
  window.__callWS = async (msg) => {
    window.__calls.push(JSON.parse(JSON.stringify(msg)));
    // Home Assistant changed just as it took this one (__changes: {command type: replies}): it, and each one after,
    // answers from the changed replies; those it took before answered as they were. It takes them in the order sent.
    const change = window.__changes[msg.type];
    if (change) { delete window.__changes[msg.type]; Object.assign(window.__replies, change); }
    window.__inflight++;
    try {
      // The archived ones' list (below Show archived) is held, and can be answered, on its own: conversations?archived.
      const hold = window.__holds[msg.archived ? msg.type + "?archived" : msg.type];
      if (hold) await hold.done;
      if (msg.type === "assist_improver/editors/saved" && window.__waiting[msg.key]) {
        window.__waiting[msg.key](); delete window.__waiting[msg.key];
      }
      const asks = window.__asks[msg.type];
      if (asks && asks.skip) asks.skip--;
      else if (asks) {
        delete window.__asks[msg.type];
        const asked = ask(asks.saves);
        if (asks.wait) await asked;
      }
      const type = msg.archived && (msg.type + "?archived") in window.__replies ? msg.type + "?archived" : msg.type;
      if (!(type in window.__replies)) throw new Error("No stub for " + type);
      const reply = window.__replies[type];
      if (reply && reply.__error) throw reply.__code ? {code: reply.__code, message: reply.__error}
                                                    : new Error(reply.__error);
      return JSON.parse(JSON.stringify(reply));
    } finally { window.__inflight--; }
  };
}"""
MOUNT = """(dark) => {
  const el = document.createElement("assist-improver-panel");
  document.body.appendChild(el);
  el.narrow = window.innerWidth < 870;
  el.hass = {themes: {darkMode: dark}, callWS: (msg) => window.__callWS(msg), connection: window.__connection,
             callApi: (method, path, data) => window.__callApi(method, path, data)};
}"""
# The contrast ratio (WCAG) of an element's text against what's behind it, blending translucent layers.
CONTRAST = """(css) => {
  const el = document.querySelector("assist-improver-panel").shadowRoot.querySelector(css);
  if (!el) return null;
  // rgb(r, g, b[, a]) in 0-255, or color(srgb r g b[ / a]) in 0-1, as a color-mix() computes.
  const parse = (c) => { const v = c.match(/[\\d.]+/g).map(Number), k = c.startsWith("color(") ? 255 : 1;
    return [v[0] * k, v[1] * k, v[2] * k, v.length > 3 ? v[3] : 1]; };
  const over = (top, under) => [0, 1, 2].map((i) => top[i] * top[3] + under[i] * (1 - top[3])).concat(1);
  const layers = [];
  for (let n = el; n; n = n.parentElement || (n.getRootNode() !== document ? n.getRootNode().host : null)) {
    const bg = parse(getComputedStyle(n).backgroundColor);
    if (bg[3] > 0) layers.push(bg);
    if (bg[3] >= 1) break;
  }
  let bg = [255, 255, 255, 1];
  for (const layer of layers.reverse()) bg = over(layer, bg);
  const color = parse(getComputedStyle(el).color);  // Faded as the element and what holds it are, once faded in.
  for (let n = el; n && n.nodeType === 1; n = n.parentElement) {
    if (!n.classList.contains("fresh")) color[3] *= parseFloat(getComputedStyle(n).opacity);
  }
  const fg = over(color, bg);
  const lum = (c) => { const [r, g, b] = c.slice(0, 3).map((v) => { v /= 255;
    return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; }); return 0.2126 * r + 0.7152 * g + 0.0722 * b; };
  const [hi, lo] = [lum(fg), lum(bg)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}"""


class Panel:
    def __init__(self, page, errors, dialogs, dark):
        self.page, self.errors, self.dialogs, self.dark = page, errors, dialogs, dark
        self.host = page.locator("assist-improver-panel")

    def mount(self):
        self.page.evaluate(MOUNT, self.dark)

    def settle(self):
        """Wait until no reply is outstanding and the panel has drawn (and laid out) what came back."""
        for _ in range(2):
            self.page.wait_for_function("window.__inflight === 0")
            self.page.evaluate("() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)))")

    def find(self, css):
        return self.page.locator(f"assist-improver-panel {css}")

    def text(self, text, exact=False):
        return self.host.get_by_text(text, exact=exact)

    def count(self, css):
        return self.page.evaluate(
            "(c) => document.querySelector('assist-improver-panel').shadowRoot.querySelectorAll(c).length", css)

    def calls(self, kind=None):
        calls = self.page.evaluate("window.__calls")
        return [c for c in calls if kind is None or c["type"] == f"assist_improver/{kind}"]

    def answer(self, kind, value):
        self.page.evaluate("([t, v]) => { window.__replies[t] = v; }", [f"assist_improver/{kind}", value])

    def change_at(self, kind, answers):
        """Home Assistant changes just as it takes the next kind command (a run ends, say): that command and every
        later one answer as answers says ({kind: value}, kinds as answer takes them); those it took before stay."""
        self.page.evaluate("([t, v]) => { window.__changes[t] = v; }", [
            f"assist_improver/{kind}", {f"assist_improver/{key}": value for key, value in answers.items()}])

    def asks(self, kind, saves, wait=True, skip=0):
        """While the next kind command is answered (after skip more), the improver asks the panel to save these scripts
        (asking), each waiting for its answer; not wait: it answers without waiting for the first (it gave up on it)."""
        self.page.evaluate("([t, v]) => { window.__asks[t] = v; }", [f"assist_improver/{kind}",
                                                                     {"saves": saves, "wait": wait, "skip": skip}])

    def hold_api(self, method, object_id):
        """Home Assistant's script editor takes its time over this save, until released (release_api)."""
        self.page.evaluate("(k) => { let release; const done = new Promise((r) => { release = r; }); "
                           "window.__apiHolds[k] = {done, release}; }", f"{method} {EDITOR}{object_id}")

    def release_api(self, method, object_id):
        self.page.evaluate("(k) => { window.__apiHolds[k].release(); delete window.__apiHolds[k]; }",
                           f"{method} {EDITOR}{object_id}")
        self.settle()

    def reconnect(self, refused=0):
        """Home Assistant restarted under the open panel: its websocket connects again (the same connection object),
        without the subscriptions it had; the next refused ones are refused (the improver isn't set up yet)."""
        self.page.evaluate("(n) => window.__reconnect(n)", refused)

    def refuse(self, method, object_id, message, kind="script"):
        """Home Assistant's editor refuses this save, as hass.callApi rejects (a 400 with HA's message)."""
        self.page.evaluate("([k, v]) => { window.__apiFail[k] = v; }", [f"{method} config/{kind}/config/{object_id}", {
            "error": f"400: {message}", "status_code": 400, "body": {"message": message}}])

    def api(self):
        """The calls to Home Assistant's REST API, in order: (method, path, data)."""
        return [(c["method"], c["path"], c["data"]) for c in self.page.evaluate("window.__api")]

    def hold(self, kind):
        self.page.evaluate("(t) => window.__hold(t)", f"assist_improver/{kind}")

    def release(self, kind):
        self.page.evaluate("(t) => window.__release(t)", f"assist_improver/{kind}")
        self.settle()

    def click(self, css):
        self.find(css).first.click()
        self.settle()

    def path(self):
        return self.page.evaluate("location.pathname + location.search")

    def rebuild(self):
        """Home Assistant makes the panel's element again (as after a decision changes the sidebar count)."""
        self.page.evaluate("() => document.querySelector('assist-improver-panel').remove()")
        self.mount()
        self.settle()

    def contrast(self, css):
        return self.page.evaluate(CONTRAST, css)


LOADS = itertools.count(1)


def serve(theme):
    """The stand-in origin: www/'s files at Home Assistant's static path, and a page in the theme for every address."""
    def answer(route):
        path = urlparse(route.request.url).path
        if path.startswith(STATIC):
            file = (WWW / path[len(STATIC):]).resolve()
            assert file.is_relative_to(WWW) and file.suffix == ".js", path
            route.fulfill(content_type="text/javascript", body=file.read_text())
        else:
            route.fulfill(content_type="text/html",
                          body=f"<!doctype html><html><head><style>{theme}</style></head><body></body></html>")
    return answer


def load_panel(page):
    """panel.js as Home Assistant loads it: a module at its module_url (panel.js?v=<version>), defining the panel's
    element, and loading the rest of the panel under the same version (each load here a new one, as after an update)."""
    page.evaluate("(url) => import(url).then(() => true)", f"{ORIGIN}{STATIC}panel.js?v=test{next(LOADS)}")
    page.wait_for_function("!!customElements.get('assist-improver-panel')")


@pytest.fixture(scope="session")
def browser():
    with sync_playwright() as p:
        browser = jscov.launch(getattr(p, os.environ.get("PANEL_BROWSER", "chromium")))
        yield browser
        browser.close()
        jscov.dump("panel")


@pytest.fixture
def open_panel(browser):
    opened = []

    def open_(answers=None, path="/assist-improver", size=PHONE, dark=True, held=(), touch=False):
        context = browser.new_context(viewport=size, color_scheme="dark" if dark else "light", has_touch=touch)
        page = context.new_page()
        jscov.start(page)
        errors, dialogs = [], []
        page.on("pageerror", lambda err: errors.append(str(err)))
        # With the clock fixed, an error thrown in a timer or an animation frame reaches the console, not pageerror.
        page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
        page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
        requests = []
        page.on("request", lambda request: requests.append(request.url))
        page.clock.set_fixed_time(NOW)
        page.route(f"{ORIGIN}/**", serve(DARK if dark else LIGHT))
        page.goto(ORIGIN + path)
        page.evaluate(STUB, answers or replies())
        load_panel(page)
        panel = Panel(page, errors, dialogs, dark)
        panel.requests = requests  # Every address the page asked for.
        opened.append((context, panel))
        for kind in held:  # Replies that wait until released.
            panel.hold(kind)
        panel.mount()
        if not held:
            panel.settle()
        return panel

    yield open_
    for context, panel in opened:
        # Let what's already scheduled run (a frame, then a timer), so an error it throws is counted too.
        panel.page.evaluate("() => new Promise((r) => requestAnimationFrame(() => setTimeout(r)))")
        errors = list(panel.errors)
        jscov.collect(context)
        context.close()
        assert not errors, f"The panel threw: {errors}"  # It never throws, whatever it's shown.


def classes(locator):
    return set((locator.get_attribute("class") or "").split()) - {"fresh"}  # Fading in for a moment: not a state.


def assert_escaped(panel):
    assert panel.count("img") == 0, "markup from a reply was parsed"
    panel.page.wait_for_timeout(100)
    assert panel.page.evaluate("window.__xss") is None, "a reply's markup ran"


def assert_no_horizontal_scroll(panel):
    page = panel.page
    width, scroll = page.evaluate("[window.innerWidth, document.documentElement.scrollWidth]")
    assert scroll <= width, f"the page scrolls sideways: {scroll}px wide in a {width}px window"
    for css in (".modal", ".pop", ".menu", ".footer", ".tip:not([hidden])"):  # Fixed layers can't scroll into view.
        for box in page.locator(f"assist-improver-panel {css}").all():
            if box.is_visible():
                b = box.bounding_box()
                assert b["x"] >= -1 and b["x"] + b["width"] <= width + 1, f"{css} runs off the screen: {b}"


# Home: the one next thing.

def test_home_shows_improvements_to_review_first_each_with_what_it_gets_you(open_panel):
    panel = open_panel(replies(improvements={"improvements": PENDING + EARLIER},
                               run=status(run(progress={"new_improvements": 2}))))
    expect(panel.find("h1")).to_have_text("2 improvements to review")
    expect(panel.find("h1 + .lead")).to_have_count(0)  # The heading says it all: no sentence explaining the list.
    rows = panel.find(".page > .card .item")
    expect(rows).to_have_count(2)
    expect(rows.nth(0).locator(".title")).to_contain_text(GARAGE)
    expect(rows.nth(0).locator(".gains")).to_have_text("1 new thing Assist can do · 2 fewer turns")
    expect(rows.nth(1).locator(".gains")).to_have_text("1 fewer turn")
    # Under that heading, no chip says each is to review: a chevron says it opens. An earlier one keeps its status chip.
    expect(rows.locator(".chip")).to_have_count(0)
    expect(rows.locator(".go")).to_have_count(2)
    panel.click(".history > summary")
    expect(panel.find(".history .item .chip")).to_have_text(["Applied"])
    expect(panel.find(".history .item .go")).to_have_count(0)
    expect(panel.find(".cs-title")).to_have_text("Your conversations")  # Then the conversations, to send more in.
    expect(panel.find(".cs-last")).to_have_text("Last run 5 min ago · 2 new improvements")


def test_one_improvement_to_review_reads_in_the_singular(open_panel):
    panel = open_panel(replies(improvements={"improvements": PENDING[:1]}))
    expect(panel.find("h1")).to_have_text("1 improvement to review")


QUESTION = {"id": "c9", "question": "Which relay did you mean: the one by the front door, or the garage's?",
            "said": "Do the relay thing."}


@pytest.mark.parametrize("dark", [True, False], ids=["dark", "light"])
@pytest.mark.parametrize("size", [PHONE, DESKTOP], ids=["phone", "desktop"])
def test_a_question_waits_first_with_what_was_said_a_box_and_one_button_and_the_answer_runs_at_once(
        open_panel, size, dark):
    """A question for the household (P0.11) is answered where it waits, first on the home page: what it asks, what
    was said in its conversation, a box and Answer. The answer goes back with its conversation on a run of its own
    (P0.7): the page is read again, the question gone and that run under way. An empty box sends nothing."""
    panel = open_panel(replies(improvements={"improvements": PENDING, "questions": [QUESTION]}), size=size, dark=dark)
    expect(panel.find("h1")).to_have_text(["1 question for you", "2 improvements to review"])
    q = panel.find(".q")
    expect(q.locator(".q-ask")).to_have_text(QUESTION["question"])
    expect(q.locator(".q-said")).to_have_text("“Do the relay thing.”")
    expect(q.locator("input")).to_have_attribute("aria-label", QUESTION["question"])
    expect(q.locator("button")).to_have_text("Answer")
    assert_no_horizontal_scroll(panel)
    view = f"{'phone' if size == PHONE else 'desktop'}-{'dark' if dark else 'light'}"
    if shots := os.environ.get("UI_SHOTS"):  # A visual check: UI_SHOTS=<folder> saves each view there.
        q.locator("input").fill("The one by the front door")
        panel.page.screenshot(path=f"{shots}/question-{view}.png")
        q.locator("input").fill("")
    panel.click(".q button")
    assert panel.calls("answer") == []
    q.locator("input").fill("The one by the front door")
    panel.answer("improvements", {"improvements": PENDING, "questions": []})
    panel.answer("run", status(run("running", stage="analyze", trigger="answer", progress={"total": 1})))
    panel.click(".q button")
    assert panel.calls("answer") == [{"type": "assist_improver/answer", "recording_id": "c9",
                                      "answer": "The one by the front door"}]
    expect(panel.find(".q")).to_have_count(0)
    expect(panel.find(".run .title")).to_have_text("Running on your answer")
    if shots:
        panel.page.screenshot(path=f"{shots}/answered-{view}.png")


def test_an_answer_refused_says_why_under_its_box_and_keeps_what_was_typed(open_panel):
    panel = open_panel(replies(improvements={"improvements": [], "questions": [QUESTION]},
                               answer=fail("That conversation has no question waiting.", "not_asking")))
    panel.find(".q input").fill("The garage's")
    panel.click(".q button")
    expect(panel.find(".q .conflict")).to_have_text("That conversation has no question waiting.")
    expect(panel.find(".q input")).to_have_value("The garage's")


def test_the_run_bar_shows_only_with_something_to_run(open_panel):
    everything_in = [{**c, "status": "queued"} for c in CHATS]
    panel = open_panel(replies(conversations=listing(everything_in), run=status(run())))
    expect(panel.find(".cs-row").first).to_be_visible()
    expect(panel.find(".cs-footer")).to_have_count(0)  # Nothing picked, nothing sent in, nothing waiting.
    panel = open_panel(replies(conversations=listing(everything_in), run=status(run(), held={"triage": 16})))
    expect(panel.find(".cs-footer")).to_have_text("Run what's waiting")  # Just the button: the rest is in its confirmation.
    panel = open_panel(replies(conversations=listing(everything_in), run=status(run(), held={"triage": 16},
                                                                                 paused=["triage"])))
    expect(panel.find(".cs-row").first).to_be_visible()
    expect(panel.find(".cs-footer")).to_have_count(0)  # Only what a blocked step holds: a Run wouldn't touch it.


@pytest.mark.parametrize("picks, waiting, button", [
    (["c1"], NEW, "Run on 1 conversation"),  # The one picked: c3, sent in earlier, waits (P0.7).
    (["c1", "c4"], {**NEW, "held": {"triage": 3, "write": 1}}, "Run on 2 conversations"),  # Not the 4 held: they wait.
    ([], {**NEW, "held": {"triage": 3, "propose": 1}}, "Run on 3 new conversations"),  # None picked: the new ones.
], ids=["picked", "picked-with-what-waits", "none-picked"])
def test_the_run_bar_is_just_run_on_how_many_conversations(open_panel, picks, waiting, button):
    """Kai: Run means what it says. With some picked, those (P0.7); with none, the new ones a manual full run admits
    (the held ones it releases are its confirmation's), as Settings' Run."""
    panel = open_panel(replies(run=status(run(), **waiting)))
    for pick in picks:
        panel.click(f"[data-cs-pick='{pick}']")
    expect(panel.find("#cs-run")).to_have_text(button)
    expect(panel.find(".cs-footer .cs-what")).to_have_count(0)  # No line beside it: what it does is in the confirmation.
    panel.click("#cs-run")
    expect(panel.find(".modal[role='dialog']")).to_have_attribute("aria-label", button + "?")  # The same count.


def test_with_no_new_conversation_but_held_work_the_run_bar_runs_what_s_waiting(open_panel):
    """Kai, 2026-10-26: "Run what's waiting" when no new conversation would go in but other work waits; its
    confirmation, taking no new conversation, is "Run Assist Improver?" (DETAILS P14.6.1, P17.17.3, P17.17.4)."""
    nothing_new = [{**c, "status": "queued"} for c in CHATS]
    panel = open_panel(replies(conversations=listing(nothing_new), run=status(run(), held={"write": 2})))
    expect(panel.find(".cs-footer")).to_have_text("Run what's waiting")
    panel.click("#cs-run")
    expect(panel.find(".modal[role='dialog']")).to_have_attribute("aria-label", "Run Assist Improver?")
    expect(panel.find(".modal .c-what")).to_have_text("2 write-ups are waiting.")
    panel.click("#start-run")  # Nothing picked: a full run, as Settings' Run.
    assert panel.calls("run/start") == [{"type": "assist_improver/run/start", "confirmed": True}]


@pytest.mark.parametrize("where", ["home", "settings"])
def test_a_run_that_admits_new_ones_and_releases_held_ones_counts_only_the_new(open_panel, where):
    """Kai, 2026-10-25: 'Use "Run on {n} new conversations"', '"new" says which'; the held ones it releases are its
    confirmation's "Also: …" (DETAILS P14.6.1, P14.6.4, P14.7.1)."""
    waiting = status(run(), admits=1, held={"propose": 1}, which=["Turn on the porch light."])
    if where == "home":
        panel, button = open_panel(replies(run=waiting)), "#cs-run"
    else:
        panel, button = open_settings(open_panel, run=waiting), "#run-now"
    expect(panel.find(button)).to_have_text("Run on 1 new conversation")
    panel.click(button)
    expect(panel.find(".modal-head b")).to_have_text("Run on 1 new conversation?")
    expect(panel.find(".modal .c-which")).to_have_text("“Turn on the porch light.”")
    expect(panel.find(".modal .c-also")).to_have_text("Also: 1 conversation to propose fixes for")


def test_before_any_run_there_is_no_last_run_line(open_panel):
    panel = open_panel(replies(run=status(None)))
    expect(panel.find(".cs-title")).to_be_visible()
    expect(panel.find(".cs-last")).to_have_count(0)


def test_once_everything_is_reviewed_the_last_run_line_leaves_out_what_it_found(open_panel):
    panel = open_panel(replies(improvements={"improvements": EARLIER}, run=status(run(progress={"new_improvements": 2}))))
    expect(panel.find(".cs-last")).to_have_text("Last run 5 min ago")
    panel.click("#open-settings")  # The settings still say what it found.
    expect(panel.find("#run-now")).to_have_attribute("data-tip", "Last run 5\u00a0min ago · 2 new improvements")


@pytest.mark.parametrize("minutes, when", [(5, "5\u00a0min ago"), (90, "2\u00a0h ago")], ids=["minutes", "hours"])
def test_times_never_break_between_the_number_and_its_unit(open_panel, minutes, when):
    panel = open_panel(replies(improvements={"improvements": PENDING}, run=status(run(minutes=minutes))))
    line = panel.find(".cs-last").text_content()
    assert when in line, line  # A no-break space between the number and its unit.


def test_earlier_improvements_are_folded_away_until_opened(open_panel):
    panel = open_panel(replies(improvements={"improvements": PENDING + EARLIER}))
    history = panel.find("details.history")
    expect(history.locator("summary")).to_have_text("Earlier improvements (1)")
    expect(history.locator(".item")).to_be_hidden()
    history.locator("summary").click()
    expect(history.locator(".item")).to_be_visible()
    expect(history.locator(".chip")).to_have_text("Applied")


def test_a_revision_is_tagged_and_the_improvement_it_replaced_says_so(open_panel):
    replaced = item("imp0", GARAGE, status="revised")
    panel = open_panel(replies(improvements={"improvements": [{**PENDING[0], "revision": True},
                                                              {**PENDING[1], "revision": False},
                                                              {**replaced, "revision": False}]}))
    rows = panel.find(".page > .card .item")
    expect(rows.nth(0).locator(".title .tag")).to_have_text("Revision")
    assert rows.nth(0).locator(".tag").evaluate("(el) => getComputedStyle(el).borderTopStyle") == "solid"
    expect(rows.nth(1).locator(".tag")).to_have_count(0)
    panel.click("details.history summary")
    expect(panel.find("details.history .chip")).to_have_text("Replaced by a revision")


def test_opening_an_improvement_goes_to_its_own_review_address(open_panel):
    panel = open_panel(replies(improvements={"improvements": PENDING}))
    panel.click(".item[data-id='imp1']")
    assert panel.path() == "/assist-improver?id=imp1"
    assert panel.calls("improvement")[-1]["improvement_id"] == "imp1"
    expect(panel.find("h1")).to_contain_text(GARAGE)


def test_an_improvement_id_survives_its_trip_through_the_address(open_panel):
    odd = 'imp "1" & <2>?x=y'
    panel = open_panel(replies(improvements={"improvements": [item(odd, "Odd one")]}))
    panel.find(".item").first.click()
    panel.settle()
    assert panel.calls("improvement")[-1]["improvement_id"] == odd


def test_a_review_link_opens_that_review_directly(open_panel):
    panel = open_panel(replies(improvement=review(id="imp2", title=CAR)), path="/assist-improver?id=imp2")
    assert [c["improvement_id"] for c in panel.calls("improvement")] == ["imp2"]
    expect(panel.find("h1")).to_contain_text(CAR)
    assert not panel.calls("improvements")


def test_picking_conversations_offers_run_with_a_play_icon(open_panel):
    panel = open_panel(replies(run=status(run(), **NEW)))
    expect(panel.find("#cs-run")).to_have_text("Run on 3 new conversations")  # None picked: every new one.
    panel.click("[data-cs-pick='c1']")
    expect(panel.find("#cs-run")).to_have_text("Run on 1 conversation")
    expect(panel.find("#cs-run svg path")).to_have_attribute("d", PLAY)
    panel.click("[data-cs-pick='c4']")
    expect(panel.find("#cs-run")).to_have_text("Run on 2 conversations")
    expect(panel.find("#cs-clear")).to_have_count(0)  # No Clear picks: unticking Select all clears them.


@pytest.mark.parametrize("size", [PHONE, DESKTOP], ids=["phone", "desktop"])
def test_search_filters_and_select_all_stay_on_top_while_the_list_scrolls(open_panel, size):
    """Pinned under where the toolbar was, on the card's background; an opened conversation's send row and the Run
    bar stay at the bottom, and nothing overlaps."""
    many = [{**CHATS[n % 4], "id": f"m{n}", "started": ago(5 + n)} for n in range(24)]
    long = {**chat(id="m0"), "turns": [{"you": f"Turn on the porch light, take {n}.",
                                       "assist": "Which porch light do you mean? There are several. " * 3} for n in range(12)]}
    panel = open_panel(replies(conversations=listing(many), conversation=long, run=status(run(), **NEW)), size=size)
    panel.click("[data-cs='m0']")
    panel.page.wait_for_timeout(600)  # The smooth scroll to it.
    panel.page.evaluate("() => document.scrollingElement.scrollBy(0, 600)")
    panel.settle()
    controls, send, footer = (panel.find(css).bounding_box() for css in (".cs-controls", ".cs-send", ".cs-footer"))
    assert abs(controls["y"]) <= 1, controls  # Pinned at the top.
    assert controls["y"] + controls["height"] <= send["y"] and send["y"] + send["height"] <= footer["y"] + 0.5, (
        controls, send, footer)
    expect(panel.find("#cs-search")).to_be_visible()
    background = "(el) => getComputedStyle(el).backgroundColor"
    assert panel.find(".cs-controls").evaluate(background) == panel.find(".cs-list").evaluate(background)
    assert_no_horizontal_scroll(panel)


@pytest.mark.parametrize("size", [PHONE, DESKTOP], ids=["phone", "desktop"])
@pytest.mark.parametrize("count", [1, 4], ids=["no-select-all", "select-all"])
def test_the_pinned_controls_have_even_space_above_and_below(open_panel, size, count):
    panel = open_panel(replies(conversations=listing(CHATS[:count]), run=status(run(), **NEW)), size=size)
    expect(panel.find(".cs-controls .cs-all")).to_have_count(1 if count > 1 else 0)
    box = panel.find(".cs-controls").bounding_box()
    first, last = panel.find("#cs-search").bounding_box(), panel.find(
        ".cs-controls .cs-all" if count > 1 else ".cs-controls .cs-bar").bounding_box()
    above, below = first["y"] - box["y"], box["y"] + box["height"] - (last["y"] + last["height"])
    assert abs(above - below) <= 1 and above >= 6, (above, below)


@pytest.mark.parametrize("width, rows", [(320, 3), (360, 3), (402, 3), (620, 3), (700, 3), (760, 3), (820, 3), (830, 2),
                                         (900, 2), (1280, 2)],
                         ids=["smallest-phone", "small-phone", "phone", "narrow-window", "window-beside-sidebar", "wider-beside-sidebar",
                              "just-too-narrow", "just-wide-enough", "wider-window", "desktop"])
@pytest.mark.parametrize("chosen", ["nothing", "order", "order-and-status"])
def test_the_pinned_controls_are_compact_and_as_wide_as_the_list(open_panel, width, rows, chosen):
    """On a phone, or a window narrow beside Home Assistant's sidebar: the search and the order on one row, the day
    filter and Status on the next, then Select all (it took four rows, a fifth of a phone's screen). On a desktop all of
    them on one row, then Select all. Whichever order's and status's names they show, they take as many rows, and the
    day filter's words stay whole on one line (a long status's name gives way on the smallest phones). The band is as
    wide as the list under it, Select all's box in line with each row's, and the order's menu opens on the screen."""
    counts = {**listing()["by_status"], "fine": 1}  # One with no trouble found, the longest status's name.
    panel = open_panel(replies(conversations=listing(by_status=counts), run=status(run(), **NEW)),
                       size={"width": width, "height": 874})
    if chosen != "nothing":
        panel.click("#cs-sort")
        panel.click("[data-order='effort']")  # The longest order's name.
    if chosen == "order-and-status":
        panel.click("#cs-status")
        panel.click("#cs-status-all")
        panel.click("[data-cs-status='fine']")
        panel.page.keyboard.press("Escape")
        expect(panel.find("#cs-status")).to_have_text("No trouble found▾")
    middles = sorted(b["y"] + b["height"] / 2 for b in (box(panel, css) for css in (
        "#cs-search", "#cs-sort", ".cs-filters .seg", "#cs-status", "#cs-all")))
    assert 1 + sum(b - a > 8 for a, b in zip(middles, middles[1:])) == rows, middles
    band, list_ = box(panel, ".cs-controls"), box(panel, ".cs-list")
    assert abs(band["x"] - list_["x"]) < 1 and abs(band["width"] - list_["width"]) < 1, (band, list_)
    every, first = box(panel, "#cs-all"), box(panel, "[data-cs-pick]")
    assert abs((every["x"] + every["width"] / 2) - (first["x"] + first["width"] / 2)) < 1.5, (every, first)
    if rows == 3:
        assert band["height"] <= 130, band  # Was about 160 on a phone.
    lines = "(el) => { const r = document.createRange(); r.selectNodeContents(el); return r.getClientRects().length; }"
    days = panel.find(".cs-filters .seg button").all()
    assert [d.evaluate(lines) for d in days] == [1] * 4, [d.bounding_box() for d in days]  # Each on one line…
    assert panel.find(".cs-filters .seg").evaluate("(el) => el.scrollWidth <= el.clientWidth")  # …none cut off.
    if width >= 360:  # The status's name whole: cut short only on the smallest phones.
        assert panel.find("#cs-status .cs-status-label").evaluate("(el) => el.scrollWidth <= el.clientWidth")
    panel.click("#cs-sort")
    menu = box(panel, ".cs-sort-menu")
    assert menu["x"] >= 0 and menu["x"] + menu["width"] <= width, menu
    assert_no_horizontal_scroll(panel)


@pytest.mark.parametrize("width", [402, 830, 1280], ids=["phone", "window-beside-sidebar", "desktop"])
def test_choosing_an_order_or_a_status_moves_no_other_control(open_panel, width):
    """Nothing moves after a tap: the order's and Status's buttons are as wide as their longest choice, as a select
    is, so choosing the longest leaves the day filter, both buttons and the search where they were. On a phone the
    search shares the order's row and takes up the change in its name; nothing else moves."""
    counts = {**listing()["by_status"], "fine": 1}  # One with no trouble found, the longest status's name.
    panel = open_panel(replies(conversations=listing(by_status=counts), run=status(run(), **NEW)),
                       size={"width": width, "height": 874})
    phone = width < 600
    still = [".cs-filters .seg", "#cs-status"] + ([] if phone else ["#cs-sort", "#cs-search"])
    where = lambda: {css: box(panel, css) for css in still} | {"sort's right": box(panel, "#cs-sort")}  # noqa: E731
    before = where()
    panel.click("#cs-sort")
    panel.click("[data-order='effort']")
    panel.click("#cs-status")
    panel.click("#cs-status-all")
    panel.click("[data-cs-status='fine']")
    panel.page.keyboard.press("Escape")
    expect(panel.find("#cs-status")).to_have_text("No trouble found▾")
    expect(panel.find("#cs-sort")).to_have_text("Most back-and-forth▾")
    after = where()
    for css in still:
        assert all(abs(after[css][k] - before[css][k]) < 1 for k in ("x", "y", "width")), (css, before[css], after[css])
    right = lambda b: b["x"] + b["width"]  # noqa: E731
    assert abs(right(after["sort's right"]) - right(before["sort's right"])) < 1


@pytest.mark.parametrize("close", ["choose", "outside", "escape"])
def test_the_order_is_a_menu_that_closes_on_a_choice_a_click_outside_or_escape(open_panel, close):
    panel = open_panel(replies())
    panel.click("#cs-sort")
    expect(panel.find(".cs-sort-menu [data-order]")).to_have_text(["Newest", "Most back-and-forth", "Oldest"])
    expect(panel.find(".cs-sort-menu .on")).to_have_text("Newest")
    if close == "choose":
        panel.click("[data-order='oldest']")
        expect(panel.find("#cs-sort")).to_have_text("Oldest▾")
    elif close == "outside":
        panel.find("#cs-sort-backdrop").click(position={"x": 5, "y": 5})
        panel.settle()
        expect(panel.find("#cs-sort")).to_have_text("Newest▾")
    else:
        panel.page.keyboard.press("Escape")
        expect(panel.find("#cs-sort")).to_have_text("Newest▾")
    expect(panel.find(".cs-sort-menu")).to_have_count(0)


@pytest.mark.parametrize("size", [PHONE, DESKTOP], ids=["phone", "desktop"])
def test_delete_asks_in_the_send_rows_place_where_the_more_actions_were_tapped(open_panel, size):
    """Delete's question takes the pinned Send row's place: in view above the Run bar, where the ⋮ was tapped, its
    answer the one strong button (no Send beside it to tap by mistake). Cancel brings back the row as it was, note
    and all."""
    panel = open_panel(replies(conversations=listing(MANY), conversation={**chat(id="m3"), "turns": LONG_TALK},
                       run=status(run(), **NEW)), size=size)
    panel.click("[data-cs='m3']")
    panel.page.wait_for_timeout(700)
    panel.find("#cs-note").fill("The front one, always")
    row = box(panel, ".cs-send")
    panel.click("#cs-actions")
    panel.click("#cs-delete-ask")
    expect(panel.find("#cs-send")).to_have_count(0)
    ask = panel.find(".cs-send.cs-confirm")
    expect(ask.locator("#cs-delete")).to_be_in_viewport(ratio=1)
    expect(ask.locator("#cs-delete-cancel")).to_be_in_viewport(ratio=1)
    asked, bar = ask.bounding_box(), box(panel, ".cs-footer")
    assert asked["y"] + asked["height"] <= bar["y"] + 0.5 and abs(asked["y"] + asked["height"] - (row["y"] + row["height"])) < 1
    panel.click("#cs-delete-cancel")
    expect(panel.find("#cs-send")).to_be_enabled()
    expect(panel.find("#cs-note")).to_have_value("The front one, always")
    assert box(panel, ".cs-send") == row  # As it was.


@pytest.mark.parametrize("answer", ["cancel", "escape"])
def test_delete_asks_with_cancel_in_focus_and_gives_it_back_to_the_more_actions(open_panel, answer):
    """From the keyboard: the question's Cancel takes the focus, and once it's answered (Cancel, or Esc), the ⋮ has it
    back, so Tab carries on from where the household was."""
    panel = open_panel(replies(conversations=listing(), conversation=chat(id="c1"), run=status(run(), **NEW)))
    panel.click("[data-cs='c1']")
    panel.click("#cs-actions")
    panel.click("#cs-delete-ask")
    expect(panel.find("#cs-delete-cancel")).to_be_focused()
    if answer == "cancel":
        panel.page.keyboard.press("Enter")
    else:
        panel.page.keyboard.press("Escape")
    expect(panel.find(".cs-confirm")).to_have_count(0)
    expect(panel.find("#cs-actions")).to_be_focused()


def test_escape_closes_an_opened_conversations_more_actions_menu(open_panel):
    panel = open_panel(replies(run=status(run(), **NEW)))
    panel.click("[data-cs='c1']")
    panel.click("#cs-actions")
    expect(panel.find(".cs-menu")).to_have_count(1)
    panel.page.keyboard.press("Escape")
    expect(panel.find(".cs-menu")).to_have_count(0)
    expect(panel.find("#cs-send")).to_be_visible()  # Still open to send in.


def test_escape_closes_the_status_menu_too(open_panel):
    panel = open_panel(replies())
    panel.click("#cs-status")
    expect(panel.find(".cs-status-menu")).to_have_count(1)
    panel.page.keyboard.press("Escape")
    expect(panel.find(".cs-status-menu")).to_have_count(0)


def test_the_order_chosen_shows_at_once_while_the_list_is_read_again(open_panel):
    panel = open_panel(replies())
    panel.hold("conversations")
    panel.click("#cs-sort")
    panel.find("[data-order='oldest']").click()
    expect(panel.find(".cs-sort-menu")).to_have_count(0)
    expect(panel.find("#cs-sort")).to_have_text("Oldest▾")
    panel.release("conversations")


@pytest.mark.parametrize("size", [PHONE, DESKTOP], ids=["phone", "desktop"])
def test_an_open_conversation_to_send_in_is_the_one_primary_action_and_run_steps_back_in_place(open_panel, size):
    """Two full-width blue buttons, Send this one in and Run, asked which one: while one is open to send in, Run is
    quiet, the same size in the same place; closed, or sent in, it's the primary action again."""
    panel = open_panel(replies(run=status(run(), **NEW)), size=size)
    run_ = panel.find("#cs-run")
    expect(run_).not_to_have_class(re.compile(r"\bquiet\b"))
    before = run_.bounding_box()
    panel.click("[data-cs='c1']")
    expect(panel.find("#cs-send")).to_be_visible()
    expect(run_).to_have_class(re.compile(r"\bquiet\b"))
    assert run_.bounding_box() == before  # Nothing moves.
    panel.click("#cs-send")  # Sent in: its Send row goes, and Run is the primary action again.
    expect(panel.find("#cs-send")).to_have_count(0)
    expect(run_).not_to_have_class(re.compile(r"\bquiet\b"))


def test_with_nothing_recorded_yet_it_says_to_use_assist_as_usual(open_panel):
    panel = open_panel(replies(conversations=listing([]), run=status(None)))
    expect(panel.find(".cs-empty")).to_have_text("Use Assist as usual: your conversations show up here, ready to send in.")
    expect(panel.find("#cs-search")).to_have_count(0)  # Nothing to search yet.
    expect(panel.find(".cs-footer")).to_have_count(0)


def test_select_all_selects_every_conversation_listed_and_unticked_clears_the_selection(open_panel):
    panel = open_panel(replies(run=status(run(), **NEW)))
    box, picks = panel.find("#cs-all"), panel.find("[data-cs-pick]")
    mixed = lambda: box.evaluate("(el) => el.indeterminate")  # noqa: E731
    expect(panel.find(".cs-controls .cs-all")).to_have_text("Select all 4")
    panel.click("[data-cs-pick='c1']")  # One by hand: mixed.
    assert mixed() and not box.is_checked()
    panel.click("#cs-all")  # From mixed: every one.
    for n in range(4):
        expect(picks.nth(n)).to_be_checked()
    assert box.is_checked() and not mixed()
    expect(panel.find("#cs-run")).to_have_text("Run on 3 conversations")  # c1 and c4 can still go in, and c3 sent in.
    panel.click("#cs-all")  # Unticked: nothing is selected.
    for n in range(4):
        expect(picks.nth(n)).not_to_be_checked()
    expect(panel.find("#cs-run")).to_have_text("Run on 3 new conversations")  # None picked: every new one.
    for rid in ("c1", "c2", "c3", "c4"):  # All by hand: ticked, and unticking clears those too.
        panel.click(f"[data-cs-pick='{rid}']")
    assert box.is_checked() and not mixed()
    panel.click("#cs-all")
    expect(panel.find("[data-cs-pick]:checked")).to_have_count(0)


def test_a_selection_is_archived_in_one_go_and_the_archived_view_offers_unarchive(open_panel):
    panel = open_panel(replies(run=status(run(), **NEW)))
    panel.click("[data-cs-pick='c1']")
    panel.click("[data-cs-pick='c2']")  # One that can still go in, and one the improver has.
    expect(panel.find(".cs-footer .btn")).to_have_text(["Run on 1 conversation", "Archive (2)", "Delete (2)"])
    for css in ("#cs-many-archive", "#cs-many-ask"):
        expect(panel.find(f"{css} svg.ic")).to_have_count(1)  # Each with its icon.
    assert_no_horizontal_scroll(panel)  # Three buttons on a phone: they wrap.
    panel.answer("conversations", listing(CHATS[2:], hidden=2))
    panel.click("#cs-many-archive")
    assert panel.calls("conversations/hide") == [{"type": "assist_improver/conversations/hide",
                                                  "recording_ids": ["c1", "c2"], "hidden": True}]
    expect(panel.find(".cs-row")).to_have_count(2)  # The list, read again.
    expect(panel.find("#cs-many-archive")).to_have_count(0)  # Done: out of the selection.
    expect(panel.find(".cs-show-archived")).to_have_text("Show archived (2)")
    panel.answer("conversations?archived", listing([{**c, "hidden": True} for c in CHATS[:2]], hidden=2))
    panel.click("#cs-archived")
    panel.click("#cs-all")
    panel.click("#cs-all-away")  # The archived ones' own, below Show archived.
    # Picked both ways: each action counts those it applies to.
    expect(panel.find(".cs-footer .btn")).to_have_text(["Run on 3 conversations", "Archive (2)", "Unarchive (2)",
                                                        "Delete (4)"])
    assert_no_horizontal_scroll(panel)
    panel.click("#cs-many-unarchive")
    assert panel.calls("conversations/hide")[-1] == {"type": "assist_improver/conversations/hide",
                                                     "recording_ids": ["c1", "c2"], "hidden": False}


@pytest.mark.parametrize("size", [PHONE, DESKTOP], ids=["phone", "desktop"])
def test_ticking_show_archived_leaves_the_bottom_bar_as_it_was(open_panel, size):
    """Kai: with Show archived ticked, a new conversation picked lost its Archive. The bar offers what applies to
    what's picked, whether archived ones are shown or not: ticking Show archived moves nothing in it."""
    panel = open_panel(replies(conversations=listing(hidden=2), run=status(run(), **NEW)), size=size)
    panel.click("[data-cs-pick='c1']")
    buttons = panel.find(".cs-footer .btn")
    expect(buttons).to_have_text(["Run on 1 conversation", "Archive (1)", "Delete (1)"])
    where = [b.bounding_box() for b in buttons.all()]
    panel.answer("conversations?archived", listing([{**c, "id": f"a{n}", "hidden": True} for n, c in enumerate(CHATS[2:])],
                                                   hidden=2))
    panel.click("#cs-archived")
    expect(panel.find(".cs-away .cs-row")).to_have_count(2)
    expect(buttons).to_have_text(["Run on 1 conversation", "Archive (1)", "Delete (1)"])
    assert [b.bounding_box() for b in buttons.all()] == where  # Nothing moved.
    panel.click("#cs-archived")  # And back.
    expect(buttons).to_have_text(["Run on 1 conversation", "Archive (1)", "Delete (1)"])
    assert [b.bounding_box() for b in buttons.all()] == where


def test_a_selection_is_deleted_in_one_go_after_one_question_naming_how_many(open_panel):
    panel = open_panel(replies())
    panel.click("#cs-all")
    panel.click("#cs-many-ask")
    expect(panel.find(".cs-ask span")).to_have_text(
        "Delete 4 conversations from Assist Improver? The improver can no longer check changes against them.")
    expect(panel.find("#cs-run")).to_have_count(0)  # Only the question, while it's asked.
    panel.click("#cs-many-cancel")
    expect(panel.find(".cs-ask")).to_have_count(0)
    assert not panel.calls("conversations/delete")
    panel.click("#cs-many-ask")
    panel.answer("conversations", listing([]))
    panel.click("#cs-many-delete")
    assert panel.calls("conversations/delete") == [{"type": "assist_improver/conversations/delete",
                                                    "recording_ids": ["c1", "c2", "c3", "c4"]}]
    expect(panel.find(".cs-row")).to_have_count(0)
    expect(panel.find(".cs-footer")).to_have_count(0)  # Nothing selected, nothing to run.


def test_the_status_filter_shows_only_the_statuses_chosen_and_select_all_takes_them(open_panel):
    """Status ▾ beside the day filter: All, then a checkbox for each status some conversation has, with how many."""
    panel = open_panel(replies())
    button = panel.find("#cs-status")
    expect(button).to_have_text("Status▾")
    panel.click("#cs-status")
    expect(panel.find(".cs-status-opt .cs-status-name")).to_have_text(["All", "New", "Sent in", "Looking for a fix"])
    expect(panel.find(".cs-status-opt .cs-status-n")).to_have_text(["2", "1", "1"])
    counts = listing()["by_status"]  # What each would show, whatever is chosen.
    panel.answer("conversations", listing([], by_status=counts))
    panel.click("#cs-status-all")  # None: then one.
    assert panel.calls("conversations")[-1]["statuses"] == []
    expect(button).to_have_text("Status: 0▾")
    panel.answer("conversations", listing([CHATS[0], CHATS[3]], by_status=counts))
    panel.click("[data-cs-status='waiting']")
    assert panel.calls("conversations")[-1]["statuses"] == ["waiting"]
    expect(button).to_have_text("New▾")
    expect(button).to_have_class(re.compile("on"))
    expect(panel.find("#cs-status-all")).to_have_js_property("indeterminate", True)
    panel.answer("conversations", listing([CHATS[0], CHATS[1], CHATS[3]], by_status=counts))
    panel.click("[data-cs-status='working']")
    assert sorted(panel.calls("conversations")[-1]["statuses"]) == ["waiting", "working"]
    expect(button).to_have_text("Status: 2▾")
    panel.find("#cs-status-backdrop").click(position={"x": 5, "y": 5})  # A click outside: closed; the filter stays.
    panel.settle()
    expect(panel.find(".cs-status-menu")).to_have_count(0)
    panel.click("#cs-all")  # Every one the filter shows.
    expect(panel.find("#cs-many-ask")).to_have_text("Delete (3)")
    panel.answer("conversations", listing())
    panel.click("#cs-status")
    panel.click("[data-cs-status='sent']")  # All of them again: no filter.
    assert "statuses" not in panel.calls("conversations")[-1]
    expect(button).to_have_text("Status▾")


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
@pytest.mark.parametrize("size", [PHONE, DESKTOP], ids=["phone", "desktop"])
def test_the_status_filter_is_one_compact_control_beside_the_day_filter(open_panel, size, dark):
    panel = open_panel(replies(), size=size, dark=dark)
    days, status = panel.find(".cs-filters .seg").bounding_box(), panel.find("#cs-status").bounding_box()
    assert abs((days["y"] + days["height"] / 2) - (status["y"] + status["height"] / 2)) <= 1, (days, status)  # One row.
    assert status["height"] <= days["height"] + 2
    panel.click("#cs-status")
    menu = panel.find(".cs-status-menu").bounding_box()
    assert menu["x"] >= 0 and menu["x"] + menu["width"] <= size["width"], menu
    assert not low_contrast(panel, ["#cs-status", ".cs-status-opt .cs-status-name"], 4.5)
    assert_no_horizontal_scroll(panel)


def test_the_selection_lasts_through_a_search_and_the_day_filter(open_panel):
    panel = open_panel(replies())
    panel.click("[data-cs-pick='c1']")
    panel.answer("conversations", listing([CHATS[1]]))
    panel.find("#cs-search").fill("garage")
    panel.page.wait_for_timeout(500)
    panel.settle()
    expect(panel.find(".cs-row")).to_have_count(1)
    expect(panel.find("#cs-many-ask")).to_have_text("Delete (1)")  # c1, out of sight, is still selected.
    panel.click("[data-cs-pick='c2']")
    expect(panel.find("#cs-many-ask")).to_have_text("Delete (2)")
    panel.answer("conversations", listing())
    panel.click("[data-days='7']")
    expect(panel.find("[data-cs-pick='c1']")).to_be_checked()
    expect(panel.find("[data-cs-pick='c2']")).to_be_checked()


def test_work_the_improver_has_waiting_runs_from_the_bar(open_panel):
    everything_in = [{**c, "status": "queued"} for c in CHATS]
    panel = open_panel(replies(conversations=listing(everything_in), run=status(run(), held={"triage": 16, "write": 2})))
    expect(panel.find(".cs-footer")).to_have_text("Run what's waiting")
    panel.click("#cs-run")
    expect(panel.find(".modal-head b")).to_have_text("Run Assist Improver?")  # No new one: those an earlier run held.
    expect(panel.find(".modal .c-what")).to_have_text("16 conversations to triage and 2 write-ups are waiting.")
    expect(panel.find(".modal .c-also")).to_have_count(0)  # No new ones: what it releases is the sentence.
    panel.click("#start-run")  # Nothing picked: a full run, as Settings' Run.
    assert panel.calls("run/start") == [{"type": "assist_improver/run/start", "confirmed": True}]


def test_a_blocked_write_up_says_so_in_the_confirmation(open_panel):
    everything_in = [{**c, "status": "queued"} for c in CHATS]
    panel = open_panel(replies(conversations=listing(everything_in), run=status(run(), held={"write": 1, "triage": 2},
                                                                                paused=["write"])))
    panel.click("#cs-run")
    expect(panel.find(".modal .cost").first).to_have_text("Write up is blocked.")


@pytest.mark.parametrize("stage, progress, detail", [
    ("triage", {"total": 10, "done": 3, "conversation": "c4"}, "Conversation 4 of 10, at Triage. "),
    ("analyze", {"total": 1, "done": 0, "conversation": "c1"}, "Conversation 1 of 1, at Analyze. "),
    ("propose", {"total": 3, "done": 2, "conversation": "c3"}, "Conversation 3 of 3, at Propose. "),
    ("write", {"total": 2, "done": 1, "conversation": "c2"}, "Conversation 2 of 2, at Write up. "),
    (None, {}, ""),  # Before it has admitted anything: no stage at work yet.
], ids=["triage", "analyze", "propose", "write", "starting"])
def test_a_run_in_progress_shows_where_it_is_and_can_be_left(open_panel, stage, progress, detail):
    """The conversation it's on, of all it moves, and that one's stage, marked on the stepper (Simulate is Propose's
    stamp, not a step of its own)."""
    panel = open_panel(replies(run=status(run("running", stage=stage, progress=progress))))
    expect(panel.find(".run .sub")).to_have_text(f"{detail}You can leave this page; it keeps going.")
    steps = panel.find(".stepper .step")
    expect(steps).to_have_text(["Triage", "Analyze", "Propose", "Write up"])
    at = ["triage", "analyze", "propose", "write"].index(stage) if stage else -1
    for n in range(4):
        assert classes(steps.nth(n)) == {"step"} | ({"done"} if n < at else {"now"} if n == at else set())
        bar, step = steps.nth(n).locator(".bar").bounding_box(), steps.nth(n).bounding_box()
        assert bar["width"] == step["width"] and bar["height"] == 4, (bar, step)  # Each step's bar shows, full width.


@pytest.mark.parametrize("over, title", [
    ({}, "Improving your assistant"),  # Before it has taken anything in.
    ({"progress": {"total": 14, "taken_in": 14}}, "Running on 14 new conversations"),
    ({"progress": {"total": 3, "taken_in": 2}}, "Running on 2 new conversations"),  # And 1 held one it released.
    ({"progress": {"total": 1, "taken_in": 0}}, "Improving your assistant"),  # Only a held one: no new one.
    ({"trigger": "scheduled", "progress": {"total": 1, "taken_in": 1}}, "Running on 1 new conversation"),
    ({"picked": ["c1", "c4"], "progress": {"total": 2}}, "Running on the conversations you picked"),
    ({"trigger": "filed", "picked": ["c3"], "progress": {"total": 1}}, "Running on what you sent in"),
    ({"only": "analyze", "progress": {"total": 2}}, "Running Analyze only"),
    ({"trigger": "revision", "conversation": "c2", "progress": {"total": 1}}, "Revising an improvement"),
    ({"trigger": "conflict", "conversation": "c2", "progress": {"total": 1}}, "Resolving a conflict"),
    ({"trigger": "answer", "conversation": "c2", "progress": {"total": 1}}, "Running on your answer"),
], ids=["starting", "full", "full-new-and-held", "full-held-only", "scheduled-one", "picked", "on-demand", "one-step",
        "revision", "conflict", "answer"])
def test_a_run_in_progress_says_which_run_it_is(open_panel, over, title):
    panel = open_panel(replies(run=status({**run("running", stage="analyze"), **over})))
    expect(panel.find(".run .title")).to_have_text(title)


def test_a_run_in_progress_sits_above_the_reviews_without_a_second_run_button(open_panel):
    panel = open_panel(replies(improvements={"improvements": PENDING},
                               run=status(run("running", stage="propose"))))
    expect(panel.find(".run")).to_be_visible()
    expect(panel.find("h1")).to_have_text("2 improvements to review")
    expect(panel.find("#run-now")).to_have_count(0)


def test_a_run_in_progress_can_be_cancelled(open_panel):
    panel = open_panel(replies(run=status(run("running", stage="analyze"))))
    panel.answer("run", status(run("cancelled")))
    panel.hold("run/cancel")
    panel.find("#cancel-run").click()
    expect(panel.find("#cancel-run")).to_have_text("Cancelling…")
    expect(panel.find("#cancel-run")).to_be_disabled()
    panel.release("run/cancel")
    assert len(panel.calls("run/cancel")) == 1
    expect(panel.find(".run")).to_have_count(0, timeout=1500)  # Straight away, not at the next refresh (3 s).
    expect(panel.find(".cs-last")).to_contain_text("Last run cancelled 5 min ago")


def test_a_run_in_progress_refreshes_itself_and_shows_what_it_found(open_panel):
    panel = open_panel(replies(run=status(run("running", stage="write", progress={"total": 1, "done": 0}))))
    panel.answer("run", status(run(progress={"new_improvements": 1})))
    panel.answer("improvements", {"improvements": PENDING[:1]})
    expect(panel.find("h1")).to_have_text("1 improvement to review", timeout=8000)  # Refreshed every few seconds.
    expect(panel.find(".run")).to_have_count(0)


@pytest.mark.parametrize("before", [None, run()], ids=["no-run-yet", "after-a-run"])
def test_an_open_home_page_notices_a_run_that_starts_after_it_loaded(open_panel, before):
    """A conversation filed from Assist (on another screen, say) starts a run of its own while the page is open: the
    page notices within a few seconds, as it refreshes itself, and reads again what that run found, without a reload
    (DETAILS P5.9.2)."""
    panel = open_panel(replies(run=status(before)))
    expect(panel.find("h1")).to_have_count(0)
    panel.answer("run", status({**run(progress={"new_improvements": 1}, trigger="filed"), "id": "r2"}))
    panel.answer("improvements", {"improvements": PENDING[:1]})
    expect(panel.find("h1")).to_have_text("1 improvement to review", timeout=8000)
    expect(panel.find(".cs-last")).to_have_text("Last run 5 min ago · 1 new improvement")


def test_a_run_that_ends_before_a_send_reads_it_is_still_followed_to_where_it_settles(open_panel):
    """test_send_in_again (tools/e2e) on a slow machine: one that looked smooth, opened among the archived and sent in
    again, gets a run of its own at once (the fast lane), a quick one: the lists read again after sending find it at
    Analyze, and the run, read after them, has ended. The page still comes to show where that run left it."""
    smooth, fine = ({**CHATS[0], "status": standing, "hidden": True} for standing in ("smooth", "fine"))
    panel = open_panel(replies(conversations=listing(CHATS[1:], hidden=1),
                               conversation={**chat("smooth"), "hidden": True}))
    panel.answer("conversations?archived", listing([smooth], hidden=1))
    panel.click("#cs-archived")
    panel.click("[data-cs='c1']")
    chip = panel.find(".cs-row:has([data-cs='c1']) .cs-head .chip")
    expect(chip).to_have_text("Looked smooth")
    # While its run goes: back in the list, at Analyze. Once it's over: no trouble found, and archived again.
    panel.answer("conversations", listing([{**CHATS[0], "status": "queued"}, *CHATS[1:]]))
    panel.answer("conversations?archived", listing([]))
    panel.change_at("run", {"run": status({**run(trigger="filed", minutes=0), "id": "r2"}),
                            "conversations": listing(CHATS[1:], hidden=1, stands=[fine]),
                            "conversations?archived": listing([fine], hidden=1)})
    panel.click("#cs-send")
    expect(chip).to_have_text("No trouble found", timeout=8000)  # At its next refresh.


def test_settings_follow_a_run_that_ended_before_a_change_there_read_it(open_panel):
    """A stage blocked while a run goes: Settings reads the run again for what it holds, and the run has ended in
    between. Its next refresh still reads what the run did, as for a run that ends untouched."""
    panel = open_settings(open_panel, run=status(run("running", stage="analyze")),
                          settings__set=settings(paused=["propose"]))
    expect(panel.find(".history .h-empty")).to_be_visible()
    panel.change_at("run", {"run": status(run(progress={"new_improvements": 1})),
                            "log": {"events": [event_run(progress={"new_improvements": 1})]}})
    panel.click("[data-gate='propose']")
    expect(panel.find(".h-row .h-text > div").first).to_have_text("Run finished", timeout=8000)


def test_a_run_started_here_that_ends_as_the_page_reads_it_shows_what_it_found(open_panel):
    """A quick run (on one conversation, say) can end while the page reads what to show after starting it, between
    any two of its reads (Home Assistant takes them in the order sent). What the page shows is never older than the
    run its next refresh compares with: here the run ends just as the page reads it."""
    panel = open_panel(replies(run=status(run(), **NEW)))
    panel.click("[data-cs-pick='c1']")
    panel.click("#cs-run")
    panel.change_at("run", {"run": status({**run(progress={"new_improvements": 1}, minutes=0), "id": "r2"}),
                            "improvements": {"improvements": PENDING[:1]}})
    panel.click("#start-run")
    expect(panel.find("h1")).to_have_text("1 improvement to review", timeout=8000)


def test_a_refresh_redraws_only_the_runs_progress(open_panel):
    problems = [{"stage": "analyze", "error": "RuntimeError: provider down"}]
    progress = lambda n: {"total": 10, "done": n, "problems": problems}  # noqa: E731
    panel = open_panel(replies(improvements={"improvements": PENDING},
                               run=status(run("running", stage="analyze", progress=progress(3)))))
    panel.page.evaluate("""() => { window.__kept = [...document.querySelector('assist-improver-panel').shadowRoot
        .querySelectorAll('.toolbar, .run .sub, .stepper .step, h1, .item')]; }""")
    panel.answer("run", status(run("running", stage="analyze", progress=progress(7))))
    expect(panel.find(".run .sub")).to_contain_text("Conversation 8 of 10, at Analyze.", timeout=5000)
    assert panel.page.evaluate("window.__kept.length > 7 && window.__kept.every((el) => el.isConnected)")  # Patched, not rebuilt.


def test_a_refresh_keeps_focus_its_bubble_and_the_scroll(open_panel):
    panel = open_settings(open_panel, size={"width": 1280, "height": 500}, run=status(run("running", stage="analyze")),
                          log={"events": [event_run(minutes=n + 1) for n in range(8)]})
    gate = panel.find("[data-gate='propose']")
    gate.focus()
    expect(panel.find("#tip")).to_have_text("Stop this stage.")
    panel.page.evaluate("() => document.scrollingElement.scrollTo(0, 120)")
    runs = len(panel.calls("run"))
    panel.answer("run", status(run("running", stage="propose")))
    expect(panel.find(".stn[data-step='propose']")).to_have_class(re.compile(r"\bnow\b"), timeout=5000)
    assert len(panel.calls("run")) > runs
    expect(gate).to_be_focused()
    expect(panel.find("#tip")).to_be_visible()
    assert panel.page.evaluate("document.scrollingElement.scrollTop") == 120


def test_a_refresh_leaves_an_open_dialog_as_it_was(open_panel):
    panel = open_settings(open_panel, run=status(run("running", stage="analyze")))
    panel.click("#uninstall")
    panel.find(".u-foot .u-cancel").focus()
    panel.answer("run", status(run("running", stage="propose")))
    expect(panel.find(".stn[data-step='test']")).to_have_class(re.compile(r"\bnow\b"), timeout=5000)
    expect(panel.find(".modal[role='dialog']")).to_have_attribute("aria-label", "Uninstall Assist Improver?")
    expect(panel.find(".u-foot .u-cancel")).to_be_focused()


def test_a_run_that_stopped_early_leaves_home_calm_and_its_details_wait_in_the_history(open_panel):
    """Nothing for the household to do (what it didn't finish is held for the next run), so no notice on the home page
    (Kai); the history says what happened, with the details folded away for a bug report."""
    error = "CaseError: User turns and source snapshots do not align"
    panel = open_panel(replies(run=status(run("failed", error=error))))
    expect(panel.find(".notice")).to_have_count(0)
    expect(panel.find(".cs-last")).to_contain_text("Last run stopped early 5 min ago")
    panel = open_settings(open_panel, run=status(run("failed", error=error)), log={"events": [event_run("failed", error=error)]})
    row = panel.find(".h-row").first
    expect(row.locator(".h-sub")).to_have_text("Nothing was lost: what it didn't finish is held for your next run.")
    expect(row.locator(".h-details summary")).to_have_text("Details")
    expect(row.locator(".h-details code")).to_be_hidden()
    row.locator(".h-details summary").click()
    expect(row.locator(".h-details code")).to_have_text(error)


def test_a_step_without_its_model_names_it_and_opens_the_settings(open_panel):
    error = "Analyze can't run: its AI model isn't available. Choose another in the settings."
    panel = open_panel(replies(run=status(run("failed", error=error, trigger="scheduled"))))
    notice = panel.find(".notice")
    expect(notice).to_contain_text(error)
    expect(notice.locator("details")).to_have_count(0)
    panel.click(".notice .go-settings")
    assert panel.path() == "/assist-improver?view=settings"
    assert panel.calls("settings")
    expect(panel.find(".track")).to_be_visible()


@pytest.mark.parametrize("stages, line", [
    (["propose"], "Propose couldn't finish"),
    (["propose", "write"], "Propose and Write up couldn't finish"),
    (["analyze", "propose", "write"], "Analyze, Propose and Write up couldn't finish"),
], ids=["one", "two", "three"])
def test_a_step_that_couldnt_finish_is_named_in_the_history_and_not_on_home(open_panel, stages, line):
    problems = [{"stage": s, "error": "RuntimeError: provider down"} for s in stages]
    panel = open_panel(replies(run=status(run(progress={"taken_in": 4, "problems": problems}))))
    expect(panel.find(".notice")).to_have_count(0)  # The next run tries again: nothing to do.
    panel = open_settings(open_panel, log={"events": [event_run(progress={"taken_in": 4, "problems": problems})]})
    row = panel.find(".h-row").first
    expect(row.locator(".h-sub")).to_contain_text(line)
    row.locator(".h-details summary").click()
    expect(row.locator(".h-details code")).to_contain_text("Propose: RuntimeError: provider down")


def test_one_items_failure_is_listed_under_details_without_saying_its_step_couldnt_finish(open_panel):
    problems = [{"stage": "propose", "item": "1a2b3c4d", "error": "capability probe: WorkerError: API not found"},
                {"stage": "write", "item": "9f8e7d6c5b4a3210", "error": "KeyError: 'benefit'"}]
    panel = open_settings(open_panel, log={"events": [event_run(progress={"taken_in": 4, "problems": problems})]})
    row = panel.find(".h-row").first
    expect(row.locator(".h-sub")).to_have_text("4 taken in")  # Each step finished: the others went on.
    row.locator(".h-details summary").click()
    expect(row.locator(".h-details code")).to_have_text(
        "Propose · 1a2b3c4d: capability probe: WorkerError: API not found\nWrite up · 9f8e7d6c5b4a3210: KeyError: 'benefit'")
    # Every item of a step failing: that step couldn't finish.
    problems += [{"stage": "write", "error": "Nothing done: its 1 item failed"}]
    panel = open_settings(open_panel, log={"events": [event_run(progress={"taken_in": 4, "problems": problems})]})
    expect(panel.find(".h-row").first.locator(".h-sub")).to_have_text("4 taken in · Write up couldn't finish")


def test_a_run_that_went_fine_shows_no_notice(open_panel):
    panel = open_panel(replies(run=status(run(progress={"taken_in": 4, "analyzed": 4}))))
    expect(panel.find(".notice")).to_have_count(0)


def test_a_runs_details_in_the_history_are_escaped(open_panel):
    problems = [{"stage": XSS, "error": XSS}]
    panel = open_settings(open_panel, log={"events": [event_run("failed", error=XSS), event_run(progress={"problems": problems})]})
    for summary in panel.find(".h-details summary").all():
        summary.click()
    expect(panel.find(".h-details code").first).to_have_text(XSS)
    expect(panel.find(".h-details code").nth(1)).to_contain_text(XSS)
    assert_escaped(panel)


def test_a_run_that_stopped_early_shows_no_notice_beside_the_reviews_either(open_panel):
    panel = open_panel(replies(improvements={"improvements": PENDING},
                               run=status(run("failed", error="RuntimeError: boom"))))
    expect(panel.find(".cs-last")).to_contain_text("Last run stopped early 5 min ago")
    expect(panel.find(".notice")).to_have_count(0)


def test_home_offers_nothing_about_household_knowledge_every_assistant_reads_it_already(open_panel):
    panel = open_panel(replies(household=household(everyone=["Quiet hours start at 22:00."] * 6),
                               run=status(run())))
    expect(panel.find(".cs-title")).to_have_text("Your conversations")
    expect(panel.find(".offer")).to_have_count(0)
    assert panel.calls("household") == []  # Home doesn't even ask for it.


def test_the_gear_opens_the_settings_and_back_returns_home(open_panel):
    panel = open_panel(replies(run=status(run())))
    gear = panel.find("#open-settings")
    expect(gear).to_have_attribute("aria-label", "Settings")
    panel.click("#open-settings")
    assert panel.path() == "/assist-improver?view=settings"
    assert {c["type"].split("/", 1)[1] for c in panel.calls()} >= {"settings", "run", "household", "improvements", "log"}
    expect(panel.find(".toolbar")).to_contain_text("Settings")
    panel.click("#back")
    assert panel.path() == "/assist-improver"
    expect(panel.find(".cs-title")).to_have_text("Your conversations")


def test_a_failure_to_load_says_why_with_a_way_back(open_panel):
    panel = open_panel(replies(improvements=fail("Unknown command.")))
    expect(panel.find(".conflict")).to_have_text("Unknown command.")
    expect(panel.find("#back")).to_be_visible()


def test_while_loading_it_says_so(open_panel):
    panel = open_panel(held=["improvements"])
    expect(panel.text("Loading…")).to_be_visible(timeout=1000)
    panel.release("improvements")
    expect(panel.text("Loading…")).to_have_count(0)


def test_every_dynamic_string_on_home_is_escaped(open_panel):
    problems = [{"stage": XSS, "error": XSS}]
    panel = open_panel(replies(
        improvements={"improvements": [item(XSS, XSS, enabled=1), item("imp0", XSS, status=XSS)],
                      "questions": [{"id": XSS, "question": XSS, "said": XSS}]},
        run=status(run("failed", error=XSS, progress={"problems": problems}))))
    expect(panel.find(".item .title").first).to_contain_text(XSS)
    expect(panel.find(".q-ask")).to_have_text(XSS)
    panel.find("details.history summary").click()
    expect(panel.find("details.history .chip")).to_have_text(XSS)
    assert_escaped(panel)
    panel.find(".item").first.click()
    panel.settle()
    assert panel.calls("improvement")[-1]["improvement_id"] == XSS  # The id round-trips through its attribute.


def test_an_error_loading_is_escaped(open_panel):
    panel = open_panel(replies(improvements=fail(XSS)))
    expect(panel.find(".conflict")).to_have_text(XSS)
    assert_escaped(panel)


def test_every_module_loads_under_the_panels_version(open_panel):
    # Home Assistant loads panel.js?v=<version>: each module it brings in carries the same version, so after an
    # upgrade a browser never keeps an old one.
    panel = open_panel(replies(run=status(run())))
    urls = [url for url in panel.requests if urlparse(url).path.startswith(STATIC)]
    assert len({urlparse(url).query for url in urls}) == 1 and urlparse(urls[0]).query.startswith("v=test"), urls
    # Each module, once over: with requests intercepted (page.route), WebKit fetches a module again for each import
    # made while it's still loading. Each still runs once.
    assert sorted({urlparse(url).path.removeprefix(STATIC) for url in urls}) == sorted(
        ["panel.js", *(f"panel/{module.name}" for module in (WWW / "panel").glob("*.js"))])


def test_the_panel_is_registered_once_even_when_its_script_loads_again(open_panel):
    panel = open_panel(replies(run=status(run())))
    load_panel(panel.page)  # An update loads it again under a new address.
    panel.rebuild()
    expect(panel.find(".cs-title")).to_have_text("Your conversations")


def test_the_theme_follows_home_assistant_live_without_reloading(open_panel):
    panel = open_panel(replies(run=status(run())), dark=True)
    assert "dark" in classes(panel.host)
    calls = len(panel.calls())
    panel.page.evaluate("() => { document.querySelector('assist-improver-panel').hass = "
                        "{themes: {darkMode: false}, callWS: (msg) => window.__callWS(msg)}; }")
    assert "dark" not in classes(panel.host)
    assert len(panel.calls()) == calls


# Run now: what it will do, and that it costs money, before it starts.

def open_confirmation(open_panel, size=PHONE, **kw):
    """Run now in the settings: a manual full run (the home page runs on what's picked)."""
    panel = open_panel(replies(run=status(run(), **kw)), path="/assist-improver?view=settings", size=size)
    panel.click("#run-now")
    return panel


def test_run_now_asks_first_saying_what_waits_and_that_it_costs_money(open_panel):
    panel = open_confirmation(open_panel, admits=3, held={"analyze": 2},
                              which=["Turn on the porch light.", "Open the garage."])
    dialog = panel.find(".modal[role='dialog']")
    expect(dialog).to_have_attribute("aria-modal", "true")
    expect(dialog).to_have_attribute("aria-label", "Run on 3 new conversations?")
    expect(dialog.locator(".modal-head")).to_contain_text("Run on 3 new conversations?")
    expect(dialog.locator(".modal-head svg path")).to_have_attribute("d", PLAY)
    expect(dialog.locator(".c-what")).to_have_count(0)  # The title says how many; under it, which.
    expect(dialog.locator(".c-which")).to_have_text("“Turn on the porch light.”, “Open the garage.” and 1 more")
    expect(dialog.locator(".c-also")).to_have_text("Also: 2 conversations to analyze")  # The held ones it releases.
    expect(dialog.locator(".cost")).to_have_text("Runs use your AI models, so they cost money.")
    expect(dialog.locator(".actions button")).to_have_text(["Not now", "Run"])
    assert not panel.calls("run/start")


@pytest.mark.parametrize("held, sentence", [
    ({"propose": 1, "write": 2}, "1 conversation to propose fixes for and 2 write-ups are waiting."),
    ({"analyze": 1}, "1 conversation to analyze is waiting."),
    ({"triage": 2, "analyze": 1, "write": 1}, "2 conversations to triage, 1 conversation to analyze and more are waiting."),
    ({}, "Nothing is waiting right now."),
], ids=["propose-and-write-ups", "one", "more", "nothing"])
def test_a_run_with_no_new_conversations_says_in_one_sentence_what_it_releases(open_panel, held, sentence):
    panel = open_confirmation(open_panel, held=held)
    expect(panel.find(".modal .c-what")).to_have_text(sentence)
    expect(panel.find(".modal .c-also")).to_have_count(0)


@pytest.mark.parametrize("waiting, title, also", [
    ({"admits": 1}, "Run on 1 new conversation?", None),
    ({"admits": 1, "held": {"propose": 1}}, "Run on 1 new conversation?", "Also: 1 conversation to propose fixes for"),
    ({"admits": 2, "held": {"analyze": 1, "write": 1}}, "Run on 2 new conversations?",
     "Also: 1 conversation to analyze · 1 write-up"),
], ids=["one", "propose", "everything-else"])
def test_a_run_with_new_conversations_says_how_many_in_its_title_and_the_held_ones_under_it(
        open_panel, waiting, title, also):
    panel = open_confirmation(open_panel, **waiting)
    expect(panel.find(".modal-head b")).to_have_text(title)
    expect(panel.find(".modal .c-what")).to_have_count(0)
    if also:
        expect(panel.find(".modal .c-also")).to_have_text(also)
    else:
        expect(panel.find(".modal .c-also")).to_have_count(0)


OPUS_SPEND = {"tokens": 21_000, "model": "Claude Opus 5.5"}
HAIKU_SPEND = {"tokens": 4_000, "model": "Claude Haiku 4.5"}
COST = "Runs use your AI models, so they cost money."
LEAD = "Runs use your AI models, so they cost money. Going by your past runs, about:"
MORE = "More if it finds fixes to propose and test."


def expect_estimate(panel, lines):
    """The cost line, and the rough estimate under it as a list (none: just the line, as before)."""
    if not lines:
        expect(panel.find(".modal .c-spend")).to_have_count(0)
        expect(panel.find(".modal .cost").last).to_have_text(COST)
        return
    expect(panel.find(".modal .c-cost > p")).to_have_text([LEAD, MORE])
    expect(panel.find(".modal .c-spend li")).to_have_text(lines)


@pytest.mark.parametrize("triage, paused, admits, lines", [
    (HAIKU_SPEND, (), 5, ["110,000 tokens on Claude Opus 5.5, to analyse 5 conversations",
                          "20,000 tokens on Claude Haiku 4.5, for the quick check"]),
    ({"tokens": 4_000, "model": "Claude Opus 5.5"}, (), 5,  # Both steps on one model: one line.
     ["130,000 tokens on Claude Opus 5.5, to analyse 5 conversations, quick check included"]),
    (None, (), 5, ["110,000 tokens on Claude Opus 5.5, to analyse 5 conversations"]),
    (HAIKU_SPEND, ("triage",), 0, []),  # They'd all stop at the blocked quick check: the run admits none.
    (HAIKU_SPEND, ("analyze",), 5, []),  # Nothing gets analysed.
], ids=["two-models", "one-model", "no-quick-check-yet", "quick-check-blocked", "analyze-blocked"])
def test_run_now_estimates_the_tokens_on_each_model_from_past_runs(open_panel, triage, paused, admits, lines):
    spend = {"analyze": OPUS_SPEND, **({"triage": triage} if triage else {})}
    panel = open_confirmation(open_panel, admits=admits, spend=spend, paused=paused)
    expect_estimate(panel, lines)


def test_with_the_quick_check_blocked_only_those_sent_in_past_it_are_estimated(open_panel):
    # 5 new, 2 sent in with a note: the run admits those 2, past the quick check; the other 3 would stop at it.
    panel = open_confirmation(open_panel, admits=2, quick=0,
                              spend={"analyze": OPUS_SPEND, "triage": HAIKU_SPEND}, paused=("triage",))
    expect_estimate(panel, ["42,000 tokens on Claude Opus 5.5, to analyse 2 conversations"])


def test_without_past_runs_the_cost_line_is_as_it_was(open_panel):
    panel = open_confirmation(open_panel, admits=5)
    expect_estimate(panel, [])


@pytest.mark.parametrize("only, held, lines", [
    ("analyze", {"analyze": 1}, ["21,000 tokens on Claude Opus 5.5, to analyse 1 conversation"]),
    ("analyze", {"analyze": 3}, ["63,000 tokens on Claude Opus 5.5, to analyse 3 conversations"]),
    ("triage", {"triage": 3}, []),  # Triage alone analyses nothing.
    ("write", {"write": 2}, []),
], ids=["analyze-one", "analyze", "triage", "write"])
def test_run_one_step_estimates_only_what_analyze_takes(open_panel, only, held, lines):
    panel = open_settings(open_panel, run=status(run(), admits=5, held=held,
                                                 spend={"analyze": OPUS_SPEND, "triage": HAIKU_SPEND}))
    panel.click("#run-menu")
    panel.click(f".menu [data-only='{only}']")
    expect_estimate(panel, lines)


def test_run_now_leaves_those_sent_in_past_the_quick_check_out_of_its_estimate(open_panel):
    # 5 new, 2 of them sent in with a note (past it), and 1 held at Triage: the quick check for 4, analysis for 6.
    panel = open_confirmation(open_panel, admits=5, held={"triage": 1}, quick=4,
                              spend={"analyze": OPUS_SPEND, "triage": HAIKU_SPEND})
    expect_estimate(panel, ["130,000 tokens on Claude Opus 5.5, to analyse 6 conversations",
                            "16,000 tokens on Claude Haiku 4.5, for the quick check"])


@pytest.mark.parametrize("picks, lines", [
    (["c3"], ["21,000 tokens on Claude Opus 5.5, to analyse 1 conversation"]),
    (["c1", "c4"], ["42,000 tokens on Claude Opus 5.5, to analyse 2 conversations",
                    "8,000 tokens on Claude Haiku 4.5, for the quick check"]),
], ids=["one-sent-in-with-a-note", "every-one-checked"])
def test_a_picked_run_estimates_the_quick_check_for_the_picks_without_a_sign(open_panel, picks, lines):
    """Kai, 2026-10-25: picked conversations get the quick check, as a full run's do, unless sent in with a sign: c3
    was sent in earlier with a note, so it's past the quick check (DETAILS P9.3.1)."""
    panel = open_panel(replies(run=status(run(), **NEW, spend={"analyze": OPUS_SPEND, "triage": HAIKU_SPEND})))
    for pick in picks:
        panel.click(f"[data-cs-pick='{pick}']")
    panel.click("#cs-run")
    expect_estimate(panel, lines)


def test_a_picked_run_gives_no_quick_check_to_one_it_found_smooth_before(open_panel):
    # Picked by hand, one that looked smooth is sent in again, straight to analysis.
    rows = [CHATS[0], CHATS[1], CHATS[2], {**CHATS[3], "status": "smooth"}]
    panel = open_panel(replies(conversations=listing(rows), run=status(
        run(), admits=2, spend={"analyze": OPUS_SPEND, "triage": HAIKU_SPEND})))
    panel.click("[data-cs-pick='c4']")
    panel.click("#cs-run")
    expect_estimate(panel, ["21,000 tokens on Claude Opus 5.5, to analyse 1 conversation"])
    panel.click("#cancel-confirm")
    panel.click("[data-cs-pick='c1']")  # A new one with it: the quick check for that one.
    panel.click("#cs-run")
    expect_estimate(panel, ["42,000 tokens on Claude Opus 5.5, to analyse 2 conversations",
                            "4,000 tokens on Claude Haiku 4.5, for the quick check"])


def test_on_a_phone_the_estimate_reads_as_a_list_inside_the_dialog(open_panel):
    panel = open_confirmation(open_panel, size=PHONE, admits=128,
                              spend={"analyze": OPUS_SPEND, "triage": HAIKU_SPEND})
    items = [li.bounding_box() for li in panel.find(".modal .c-spend li").all()]
    dialog = panel.find(".modal").bounding_box()
    assert len(items) == 2 and items[0]["y"] < items[1]["y"]  # One line each, one under the other.
    assert all(dialog["x"] <= b["x"] and b["x"] + b["width"] <= dialog["x"] + dialog["width"] for b in items)
    assert_no_horizontal_scroll(panel)


def test_a_blocked_step_is_left_out_of_the_run_and_named(open_panel):
    panel = open_confirmation(open_panel, admits=3, held={"analyze": 2}, paused=["analyze"])
    expect(panel.find(".modal-head b")).to_have_text("Run on 3 new conversations?")  # Not those held at Analyze.
    expect(panel.find(".modal .cost").first).to_have_text("Analyze is blocked.")


@pytest.mark.parametrize("blocked, warning", [
    ({"analyze": ["Analyze"]}, "Analyze can't run: its AI model isn't available. Choose another first."),
    ({"propose": ["Propose", "Review"]}, "Propose and Review can't run: their AI models aren't available. Choose another first."),
], ids=["one", "two"])
def test_a_step_whose_model_cant_run_stops_the_run_before_it_spends_anything(open_panel, blocked, warning):
    panel = open_confirmation(open_panel, admits=3, blocked_models=blocked)
    expect(panel.find(".modal .c-warn")).to_have_text(f"⚠ {warning}")
    expect(panel.find("#start-run")).to_be_disabled()


def test_a_blocked_steps_missing_model_doesnt_hold_up_the_run(open_panel):
    panel = open_confirmation(open_panel, admits=3, paused=["analyze"],
                              blocked_models={"analyze": ["Analyze"]})
    expect(panel.find(".modal .c-warn")).to_have_count(0)
    expect(panel.find("#start-run")).to_be_enabled()


@pytest.mark.parametrize("close", ["#cancel-confirm-2", "#cancel-confirm", "#confirm-shade", "Escape"],
                         ids=["not-now", "close", "outside", "escape"])
def test_not_now_closes_the_confirmation_without_running(open_panel, close):
    panel = open_confirmation(open_panel, size=DESKTOP)
    if close == "Escape":
        panel.page.keyboard.press("Escape")
    elif close == "#confirm-shade":
        panel.find(close).click(position={"x": 5, "y": 5})
    else:
        panel.find(close).click()
    panel.settle()
    expect(panel.find(".modal")).to_have_count(0)
    assert not panel.calls("run/start")
    expect(panel.find(".track")).to_be_visible()  # The page underneath stayed put.


def test_run_starts_only_after_confirming_and_then_shows_its_progress(open_panel):
    panel = open_confirmation(open_panel, admits=3)
    panel.answer("run", status(run("running", stage="analyze")))
    panel.hold("run/start")
    panel.find("#start-run").click()
    expect(panel.find("#start-run")).to_have_text("Starting…")
    expect(panel.find("#start-run")).to_be_disabled()
    panel.release("run/start")
    assert panel.calls("run/start") == [{"type": "assist_improver/run/start", "confirmed": True}]
    expect(panel.find(".modal")).to_have_count(0)
    expect(panel.find("#cancel-run")).to_be_visible()


def test_a_run_that_cant_start_says_why(open_panel):
    panel = open_confirmation(open_panel)
    panel.answer("run/start", fail("A run is already in progress"))
    panel.click("#start-run")
    assert panel.dialogs == ["A run is already in progress"]
    expect(panel.find(".modal")).to_have_count(0)


# The review page: a summary on top, details folded, one decision at a time.

def open_review(open_panel, size=PHONE, dark=True, **over):
    return open_panel(replies(improvement=review(**over)), path="/assist-improver?id=imp1", size=size, dark=dark)



# A phone's safe areas as Home Assistant's app gives them (its theme's variables): the status bar on top, the home
# indicator below; and a phone on its side, its rounded corners left and right.
INSETS = {"portrait": {"top": 47, "bottom": 34, "left": 0, "right": 0},
          "landscape": {"top": 0, "bottom": 21, "left": 59, "right": 59}}
SAFE = """(insets) => { for (const [side, px] of Object.entries(insets))
  document.documentElement.style.setProperty(`--safe-area-inset-${side}`, `${px}px`); }"""


def box(panel, css):
    return panel.find(css).first.bounding_box()


@pytest.mark.parametrize("width", [320, 402, 900], ids=["small-phone", "phone", "tablet"])
def test_the_decision_bar_keeps_clear_of_a_phones_home_indicator_and_stays_put_to_the_end_of_a_scroll(open_panel,
                                                                                                       width):
    """On an iPhone the footer sat down in the home indicator's strip. The header runs up under the status bar and the
    footer down to the bottom edge, with what's in them clear of both; scrolled to the end, the footer hasn't moved."""
    size = {"width": width, "height": 874}
    panel = open_review(open_panel, size=size)
    panel.page.evaluate(SAFE, INSETS["portrait"])
    panel.settle()
    assert box(panel, "#back")["y"] >= 47  # Below the status bar.
    strip = box(panel, ".status-strip")  # Its colour runs up under it.
    assert (strip["y"], strip["height"], box(panel, ".toolbar")["y"]) == (0, 47, 47), strip
    footer, button = box(panel, ".footer"), box(panel, "#decide")
    assert footer["y"] + footer["height"] == 874  # Down to the edge…
    assert button["y"] + button["height"] <= 874 - 34  # …with the decision clear of the home indicator.
    panel.page.evaluate("() => document.scrollingElement.scrollTo(0, 1e6)")
    panel.settle()
    assert abs(box(panel, "#decide")["y"] - button["y"]) < 1  # Nothing shifts at the end of a scroll.
    assert_no_horizontal_scroll(panel)


@pytest.mark.parametrize("running", [False, True], ids=["run-bar", "no-run-bar"])
def test_an_opened_conversations_send_row_keeps_clear_of_the_home_indicator_with_or_without_the_run_bar(open_panel,
                                                                                                       running):
    """Its note and Send stay in view above the Run bar; with no Run bar (a run going), above the home indicator."""
    many = [{**CHATS[n % 4], "id": f"m{n}", "started": ago(5 + n)} for n in range(24)]
    long = {**chat(id="m0"), "turns": [{"you": f"Turn on the porch light, take {n}.",
                                       "assist": "Which porch light do you mean? There are several. " * 3} for n in range(12)]}
    last = run("running", stage="analyze", progress={"total": 1}) if running else run()
    panel = open_panel(replies(conversations=listing(many), conversation=long, run=status(last, **NEW)))
    panel.page.evaluate(SAFE, INSETS["portrait"])
    panel.click("[data-cs='m0']")
    panel.page.wait_for_timeout(600)  # The smooth scroll to it.
    send = box(panel, "#cs-send")
    assert send["y"] + send["height"] <= 874 - 34, send
    expect(panel.find(".cs-footer")).to_have_count(0 if running else 1)
    if not running:
        assert send["y"] + send["height"] <= box(panel, ".cs-footer")["y"] + 0.5


def test_one_opened_low_with_no_send_row_and_no_run_bar_brings_its_first_turn_clear_of_the_home_indicator(open_panel):
    """Being looked at (no Send row) while a run goes (no Run bar): once it's in, its first turn shows above the home
    indicator, not under it."""
    going = run("running", stage="analyze", progress={"total": 1})
    panel = open_panel(replies(conversations=listing(MANY), conversation={**chat(id="m12", status="working"), "turns": LONG_TALK},
                               run=status(going, **NEW)))
    panel.page.evaluate(SAFE, INSETS["portrait"])
    row = panel.find("[data-cs='m12']")
    row.evaluate("(el) => document.scrollingElement.scrollBy(0, el.getBoundingClientRect().top - 760)")  # Low down.
    panel.settle()
    row.click()
    panel.page.wait_for_timeout(700)  # The smooth scroll to it.
    expect(panel.find("#cs-send")).to_have_count(0)
    expect(panel.find(".cs-footer")).to_have_count(0)
    first = box(panel, ".cs-turn")
    assert first["y"] + first["height"] <= 874 - 34, first


def test_a_lower_conversation_opened_shows_its_head_below_the_pinned_controls_under_the_status_bar(open_panel):
    """Opened, a long conversation is brought into view from its head: below the pinned controls, which stick below
    the status bar."""
    many = [{**CHATS[n % 4], "id": f"m{n}", "started": ago(5 + n)} for n in range(24)]
    long = {**chat(id="m8"), "turns": [{"you": f"Turn on the porch light, take {n}.",
                                       "assist": "Which porch light do you mean? There are several. " * 3} for n in range(12)]}
    panel = open_panel(replies(conversations=listing(many), conversation=long, run=status(run(), **NEW)))
    panel.page.evaluate(SAFE, INSETS["portrait"])
    panel.page.evaluate("() => document.scrollingElement.scrollTo(0, 400)")
    panel.settle()
    target = panel.find("[data-cs='m8']")
    target.click()
    panel.page.wait_for_timeout(700)  # The smooth scroll to it.
    head, controls = target.bounding_box(), box(panel, ".cs-controls")
    assert head["y"] >= controls["y"] + controls["height"] - 0.5, (head, controls)


def test_nothing_shows_under_a_phones_status_bar(open_panel):
    """In Home Assistant's app on a phone: the status bar's strip is the toolbar's colour, so at the top it reads as the
    toolbar's own, and scrolled, the page passes under it, never under the clock."""
    panel = open_panel(replies(conversations=listing(MANY), run=status(run(), **NEW)))
    panel.page.evaluate(SAFE, INSETS["portrait"])
    panel.settle()
    strip, bar = box(panel, ".status-strip"), box(panel, ".toolbar")
    assert (strip["y"], strip["height"], bar["y"], bar["height"]) == (0, 47, 47, 56), (strip, bar)
    colour = "(el) => getComputedStyle(el).backgroundColor"
    assert panel.find(".status-strip").evaluate(colour) == panel.find(".toolbar").evaluate(colour)
    panel.page.evaluate("() => document.scrollingElement.scrollTo(0, 600)")
    panel.settle()
    for x in (10, 200, 390):
        assert panel.page.evaluate("""(x) => document.querySelector('assist-improver-panel').shadowRoot
            .elementFromPoint(x, 46).className""", x) == "status-strip"
    controls = box(panel, ".cs-controls")
    assert controls["y"] == 47, controls  # Pinned just below it.
    panel.page.evaluate(SAFE, {"top": 0})  # Off the app, no strip.
    assert box(panel, ".status-strip")["height"] == 0


def test_a_bubble_near_the_top_opens_below_what_it_explains_clear_of_the_status_bar(open_panel):
    tests = [{"summary": "Close the garage", "improved": True, "result": "3 → 1 turns", "open": 1, "happened": 1, **OPENED},
             *[{"summary": f"Open the garage, take {n}", "improved": True, "result": "3 → 1 turns", **OPENED}
               for n in range(3)]]  # Room under it to scroll it up.
    panel = open_review(open_panel, still_open=STILL_OPEN, tests=tests)
    panel.page.evaluate(SAFE, INSETS["portrait"])
    line = panel.find(".rp-open .line.open").first
    line.evaluate("(el) => document.scrollingElement.scrollBy(0, el.getBoundingClientRect().top - 70)")
    panel.settle()
    line.focus()
    tip, at = panel.find("#tip").bounding_box(), line.bounding_box()
    assert abs(at["y"] - 70) < 1, at
    assert tip["y"] >= at["y"] + at["height"], (tip, at)  # No room above it below the status bar: below it.


MANY = [{**CHATS[n % 4], "id": f"m{n}", "started": ago(5 + n)} for n in range(24)]
LONG_TALK = [{"you": f"Turn on the porch light, take {n}.",
              "assist": "Which porch light do you mean? There are several. " * 3} for n in range(12)]
TOP = "document.scrollingElement.scrollTop"


def test_sending_one_in_keeps_the_buttons_words_and_size_and_its_arrow_turns_into_a_spinner(open_panel):
    panel = open_panel(replies(run=status(run(), **NEW)))
    panel.click("[data-cs='c1']")
    send = panel.find("#cs-send")
    before = send.bounding_box()
    panel.hold("conversation/send")
    send.click()
    expect(send).to_be_disabled()
    expect(send).to_have_text("Send this one in")
    expect(send.locator(".spinner")).to_be_visible()
    assert send.bounding_box() == before  # Nothing moves under the finger.
    panel.release("conversation/send")
    expect(panel.find(".cs-done")).to_contain_text("Sent in.")


def test_opening_a_conversation_whose_start_is_in_view_moves_nothing(open_panel):
    panel = open_panel(replies(conversations=listing(MANY), conversation={**chat(id="m1"), "turns": LONG_TALK},
                               run=status(run(), **NEW)))
    panel.click("[data-cs='m1']")
    panel.page.wait_for_timeout(700)  # As long as a smooth scroll would take.
    expect(panel.find(".cs-read .cs-turn").first).to_be_visible()
    assert panel.page.evaluate(TOP) == 0


@pytest.mark.parametrize("insets", [None, "portrait"], ids=["no-insets", "phone-insets"])
def test_one_opened_low_on_the_screen_scrolls_once_it_is_in_just_enough_to_show_its_start(open_panel, insets):
    """Nothing moves at the tap; once the conversation is in, the page scrolls just enough for its first turn to show
    above its Send row and the Run bar (8 px clear), the row's head still below the pinned controls."""
    panel = open_panel(replies(conversations=listing(MANY), conversation={**chat(id="m0"), "turns": LONG_TALK},
                               run=status(run(), **NEW)))
    if insets:
        panel.page.evaluate(SAFE, INSETS[insets])
        panel.settle()
    footer = box(panel, ".cs-footer")
    low = [row for row in panel.find("[data-cs]").all()
           if row.bounding_box()["y"] + row.bounding_box()["height"] <= footer["y"]][-1]  # The lowest whole one.
    head = low.bounding_box()["y"]
    panel.hold("conversation")
    low.click()
    panel.page.wait_for_timeout(700)
    assert low.bounding_box()["y"] == head  # Not while it loads.
    panel.release("conversation")
    panel.page.wait_for_timeout(700)  # The smooth scroll.
    first, send = box(panel, ".cs-read .cs-turn"), box(panel, ".cs-send")
    assert 6 <= send["y"] - (first["y"] + first["height"]) <= 10, (first, send)  # Just enough.
    assert low.bounding_box()["y"] >= box(panel, ".cs-controls")["y"] + box(panel, ".cs-controls")["height"] - 0.5


def test_opening_one_just_below_an_open_conversation_keeps_it_under_the_finger(open_panel):
    """The one open above closes as this one opens, and what loads may shift the page: the row tapped stays where it
    was on the screen, at the tap and once it's in."""
    panel = open_panel(replies(conversations=listing(MANY), conversation={**chat(id="m6"), "turns": LONG_TALK},
                               run=status(run(), **NEW)))
    panel.click("[data-cs='m6']")
    panel.page.wait_for_timeout(700)
    below = panel.find("[data-cs='m7']")
    below.evaluate("(el) => el.scrollIntoView({block: 'center'})")  # m7's head mid-screen, m6 open above it.
    panel.settle()
    head = below.bounding_box()["y"]
    panel.hold("conversation")
    below.click()
    assert abs(below.bounding_box()["y"] - head) < 1  # m6 closed above it: it hasn't moved.
    panel.release("conversation")
    panel.page.wait_for_timeout(700)
    assert abs(below.bounding_box()["y"] - head) < 1  # Its first turn already in view: nothing more moves.
    expect(panel.find(".cs-row.open [data-cs]")).to_have_attribute("data-cs", "m7")  # The one open now.
    # As long again as it was: the page holds no length of its own, and shortens as ever when the list does.
    assert panel.page.evaluate("document.querySelector('assist-improver-panel').style.minHeight") == ""


@pytest.mark.parametrize("tap", ["the-one-above", "itself"])
def test_at_the_pages_end_what_closes_leaves_the_row_tapped_under_the_finger(open_panel, tap):
    """With the last conversation open and the page scrolled to its end, opening the one above it, or closing it,
    shortens the page: the row tapped stays where it was on the screen all the same, and the room left at the end goes
    as the page is scrolled back up."""
    panel = open_panel(replies(conversations=listing(MANY), conversation=chat(id="m23"), run=status(run(), **NEW)))
    end = f"{TOP} = document.scrollingElement.scrollHeight"
    length = "document.scrollingElement.scrollHeight"
    natural = panel.page.evaluate(length)
    panel.page.evaluate(end)
    panel.click("[data-cs='m23']")
    panel.page.wait_for_timeout(700)
    panel.page.evaluate(end)
    panel.settle()
    row = panel.find("[data-cs='m22']" if tap == "the-one-above" else "[data-cs='m23']")
    head = row.bounding_box()["y"]
    if tap == "the-one-above":
        panel.hold("conversation")
    row.click()
    assert abs(row.bounding_box()["y"] - head) < 1, (row.bounding_box(), head)  # m23 closed under it: it hasn't moved.
    held = "document.querySelector('assist-improver-panel').style.minHeight"
    if tap == "the-one-above":
        panel.release("conversation")
        panel.page.wait_for_timeout(700)
        assert row.bounding_box()["y"] <= head + 1  # Once it's in, scrolled only on, never pulled down.
        expect(panel.find(".cs-row.open [data-cs]")).to_have_attribute("data-cs", "m22")
        assert panel.page.evaluate(held) == ""  # As long again as it was: let go.
        panel.click("[data-cs='m22']")  # Closed again: the list as it was.
    assert panel.page.evaluate(held)  # The room it left at the page's end, still there.
    panel.page.evaluate(f"{TOP} = 0")
    panel.settle()
    assert panel.page.evaluate(held) == ""  # Scrolled back up: let go.
    panel.page.evaluate(end)
    panel.settle()
    assert panel.page.evaluate(length) == natural  # Then down: the page ends where it did.


@pytest.mark.parametrize("then", ["search", "day-filter"])
@pytest.mark.parametrize("where", ["mid-list", "at-the-end"])
def test_a_search_after_closing_one_shows_the_list_it_finds(open_panel, where, then):
    """Closing one mid-list keeps the page no longer than the screen needs, and a new list (a search, a filter) is as
    long as it is: the search and what it found stay in view, never a blank screen with the search scrolled off."""
    rows = MANY if where == "mid-list" else MANY[:12]
    talk = {**chat(id="m10"), "turns": LONG_TALK} if where == "mid-list" else chat(id="m11")
    panel = open_panel(replies(conversations=listing(rows), conversation=talk, run=status(run(), **NEW)))
    row = panel.find("[data-cs='m10']" if where == "mid-list" else "[data-cs='m11']")
    end = f"{TOP} = document.scrollingElement.scrollHeight"
    if where == "mid-list":
        row.evaluate("(el) => el.scrollIntoView({block: 'center'})")
    else:
        panel.page.evaluate(end)
    panel.settle()
    row.click()
    panel.page.wait_for_timeout(800)
    if where == "at-the-end":
        panel.page.evaluate(end)
    panel.settle()
    head = row.bounding_box()["y"]
    row.click()  # Closed by its head.
    assert abs(row.bounding_box()["y"] - head) < 1
    held = panel.page.evaluate("document.querySelector('assist-improver-panel').style.minHeight")
    assert (held == "") if where == "mid-list" else held, held  # Mid-list: let go at once; at the end: the room kept.
    panel.answer("conversations", listing(MANY[:2]))
    if then == "search":
        panel.find("#cs-search").click()
        panel.page.keyboard.type("porch")
        panel.page.wait_for_timeout(500)
    else:
        panel.click("[data-days='1']")
    panel.settle()
    assert panel.page.evaluate("document.querySelector('assist-improver-panel').style.minHeight") == ""
    expect(panel.find("#cs-search")).to_be_in_viewport()
    expect(panel.find("[data-cs='m1']")).to_be_in_viewport()


@pytest.mark.parametrize("change", ["a-run-starts", "a-run-ends-with-one-more-to-review", "a-run-starts-at-the-top"])
def test_what_a_run_brings_above_the_list_leaves_what_is_read_where_it_is(open_panel, change):
    """Kai: "Things shouldn't move randomly". A run starting or ending changes the page above the list on its own (its
    progress shows or goes, what it found joins the reviews): the conversation being read stays where it is on the
    screen. At the very top of the page, what's new shows as it comes."""
    going = {**run("running", stage="analyze", progress={"total": 3, "done": 1}), "id": "r2"}
    panel = open_panel(replies(improvements={"improvements": PENDING[:1]}, conversations=listing(MANY),
                               run=status(going if change == "a-run-ends-with-one-more-to-review" else run(), **NEW)))
    row = panel.find("[data-cs='m10']")
    if change != "a-run-starts-at-the-top":
        row.evaluate("(el) => el.scrollIntoView({block: 'center'})")
    panel.settle()
    y = row.bounding_box()["y"]
    if change == "a-run-ends-with-one-more-to-review":
        panel.answer("run", status({**run(progress={"new_improvements": 1}), "id": "r2"}))
        panel.answer("improvements", {"improvements": PENDING})
        expect(panel.find("h1")).to_have_text("2 improvements to review", timeout=8000)  # Refreshed on its own.
        expect(panel.find(".run")).to_have_count(0)
    else:
        panel.answer("run", status(going))
        expect(panel.find(".run")).to_have_count(1, timeout=8000)
    panel.settle()
    if change == "a-run-starts-at-the-top":
        assert panel.page.evaluate(TOP) == 0 and row.bounding_box()["y"] > y  # Its progress shows, on top.
        expect(panel.find(".run")).to_be_in_viewport()
    else:
        assert abs(row.bounding_box()["y"] - y) < 1, (row.bounding_box(), y)


@pytest.mark.parametrize("menu", ["status", "order"])
def test_a_run_ending_with_a_menu_open_leaves_what_is_read_where_it_is(open_panel, menu):
    """The menu (and the backdrop that closes it) is pinned: what's read is the conversation under it."""
    going = {**run("running", stage="analyze", progress={"total": 3, "done": 1}), "id": "r2"}
    panel = open_panel(replies(improvements={"improvements": PENDING[:1]}, conversations=listing(MANY),
                               run=status(going, **NEW)))
    row = panel.find("[data-cs='m10']")
    row.evaluate("(el) => el.scrollIntoView({block: 'center'})")
    panel.settle()
    panel.click({"status": "#cs-status", "order": "#cs-sort"}[menu])
    y = row.bounding_box()["y"]
    panel.answer("run", status({**run(progress={"new_improvements": 1}), "id": "r2"}))
    panel.answer("improvements", {"improvements": PENDING})
    expect(panel.find(".run")).to_have_count(0, timeout=8000)
    panel.settle()
    assert abs(row.bounding_box()["y"] - y) < 1, (row.bounding_box(), y)


def test_a_run_ending_while_earlier_improvements_are_read_leaves_them_where_they_are(open_panel):
    """Above the conversations, with their controls not yet pinned: the earlier improvement read stays put."""
    going = {**run("running", stage="analyze", progress={"total": 3, "done": 1}), "id": "r2"}
    early = [item(f"e{n}", f"Earlier improvement number {n}", status="applied") for n in range(15)]
    panel = open_panel(replies(improvements={"improvements": PENDING[:1] + early}, conversations=listing(MANY),
                               run=status(going, **NEW)))
    panel.click("details.history > summary")
    panel.page.evaluate(f"{TOP} = 400")
    panel.settle()
    row = panel.find("[data-id='e6']")
    y = row.bounding_box()["y"]
    panel.answer("run", status({**run(progress={"new_improvements": 1}), "id": "r2"}))
    panel.answer("improvements", {"improvements": PENDING + early})
    expect(panel.find(".run")).to_have_count(0, timeout=8000)
    panel.settle()
    assert abs(row.bounding_box()["y"] - y) < 1, (row.bounding_box(), y)


def test_show_more_brings_the_next_rows_in_where_it_was_with_archived_ones_shown_below(open_panel):
    """A tap's own change isn't held in place as a run's is: the next page of rows comes in where Show more was."""
    archived = [{**CHATS[n % 4], "id": f"a{n}", "started": ago(9000 + n), "hidden": True, "status": "fine"}
                for n in range(20)]
    panel = open_panel(replies(conversations={**listing(MANY[:12], hidden=20), "total": 24}, run=status(run(), **NEW)))
    panel.answer("conversations?archived", listing(archived, hidden=20))
    panel.click("#cs-archived")
    expect(panel.find(".cs-away .cs-row")).to_have_count(20)
    more = panel.find("#cs-more")
    more.evaluate(f"(el) => document.scrollingElement.scrollBy(0, el.getBoundingClientRect().top - 220)")
    panel.settle()
    y = more.bounding_box()["y"]
    panel.answer("conversations", {**listing(MANY[12:], hidden=20), "total": 24})
    more.click()
    expect(panel.find("[data-cs='m12']")).to_be_in_viewport()
    assert abs(panel.find("[data-cs='m12']").bounding_box()["y"] - y) < 40


@pytest.mark.parametrize("size", [PHONE, DESKTOP], ids=["phone", "desktop"])
def test_sending_one_in_near_the_top_leaves_it_where_it_is(open_panel, size):
    panel = open_panel(replies(conversations=listing(MANY), conversation=chat(id="m6"), run=status(run(), **NEW)),
                       size=size)
    panel.click("[data-cs='m6']")
    panel.page.wait_for_timeout(700)
    below = panel.find("[data-cs='m7']")
    below.evaluate("(el) => document.scrollingElement.scrollBy(0, el.getBoundingClientRect().top - 250)")
    panel.settle()
    head = panel.find("[data-cs='m6']")
    y = head.bounding_box()["y"]
    panel.find("#cs-send").click()
    expect(panel.find(".cs-done")).to_have_count(1)
    panel.settle()
    assert abs(head.bounding_box()["y"] - y) < 1, (head.bounding_box(), y)


def test_a_conversation_that_cant_be_read_says_why_above_the_run_bar(open_panel):
    panel = open_panel(replies(conversations=listing(MANY), conversation=fail("That conversation isn't kept."),
                               run=status(run(), **NEW)))
    footer = box(panel, ".cs-footer")
    low = [row for row in panel.find("[data-cs]").all()
           if row.bounding_box()["y"] + row.bounding_box()["height"] <= footer["y"]][-1]
    low.click()
    panel.page.wait_for_timeout(700)
    why = box(panel, ".cs-read .conflict")
    assert why["y"] + why["height"] <= box(panel, ".cs-footer")["y"], why


@pytest.mark.parametrize("insets, width", [("portrait", w) for w in (320, 360, 402, 600)]  # A phone upright…
                         + [("landscape", w) for w in (568, 874, 1280)])  # …and on its side, the narrowest an SE.
def test_no_footer_control_runs_past_the_screens_edge_at_any_width(open_panel, insets, width):
    """Every control in the review's decision bar, its done row, and the home page's Run bar sits inside the screen and
    clear of its rounded corners, at any width."""
    size = {"width": width, "height": 874}
    side = INSETS[insets]
    long_title = "Open or close both garage doors together by pressing their buttons in the right order"
    panel = open_review(open_panel, size=size, next_pending={"id": "imp2", "title": long_title})
    panel.page.evaluate(SAFE, side)
    panel.settle()

    def inside(css):
        for control in panel.find(css).all():
            b = control.bounding_box()
            assert b["x"] >= side["left"] - 0.5 and b["x"] + b["width"] <= width - side["right"] + 0.5, (css, b, width)

    inside(".footer button")
    panel.click("#arrow")
    panel.click(".footer .menu [data-choice='revise']")
    inside(".footer button, .footer textarea")
    panel.answer("improvement", review(status="applied", choices=[{"value": "undo", "label": "Undo changes"}],
                                       next_pending={"id": "imp2", "title": long_title}))
    panel.answer("decide", {"result": "Thanks. The improver is revising it now.", "status": "awaiting_review",
                            "ok": True})
    panel.find("#note").fill("Both at once")
    panel.click("#decide")
    inside(".footer button")  # The done row: the way on, however long the next one's headline.
    home = open_panel(replies(conversations=listing(), run=status(admits=3)), size=size)
    home.page.evaluate(SAFE, side)
    home.settle()
    for row in home.find("[data-cs-pick]").all()[:2]:
        row.check()
    home.settle()
    panel = home  # inside() reads the panel in view.
    inside(".cs-footer button")
    assert box(home, ".cs-controls")["y"] >= side["top"]  # The pinned controls, clear of the status bar too.
    assert_no_horizontal_scroll(home)


@pytest.mark.parametrize("title", [GARAGE, "Call the desk lamp the reading lamp"], ids=["chip-beside", "chip-wraps"])
def test_a_review_title_s_chip_stays_clear_of_the_line_under_it(open_panel, title):
    # On a phone the status chip may wrap onto a line of its own under the title: it still sits within the title, clear
    # of the outcome line under it.
    panel = open_review(open_panel, title=title, status="conflict", choices=[])
    chip, outcome = (panel.find(css).bounding_box() for css in ("h1 .chip", ".outcome"))
    assert chip["y"] + chip["height"] <= outcome["y"], (chip, outcome)

def test_a_review_leads_with_its_headline_status_and_what_it_gets_you(open_panel):
    panel = open_review(open_panel)
    expect(panel.find("h1")).to_have_text(f"{GARAGE} To review")
    expect(panel.find("h1 .chip")).to_have_text("To review")
    expect(panel.find(".outcome")).to_have_text("1 new thing Assist can do · 2 fewer turns")
    expect(panel.find(".toolbar")).to_contain_text("Improvements")


def open_changes(panel):
    """What changes, opened, as a reader would (it's folded at first)."""
    panel.click("details[data-key='changes'] > summary")


def test_the_summary_is_open_on_top_what_you_get_its_before_and_after_and_why_its_safe(open_panel):
    """Kai's order: what you get, then the evidence (each test's before and after), then why it's safe."""
    panel = open_review(open_panel)
    summary = panel.find(".card.summary")
    expect(summary.locator(".label")).to_have_text(["What you get", "Before & after", "Why it's safe"])
    expect(summary.locator(".benefit")).to_have_text("Say “open the garage” and both doors open, first time.")
    expect(summary.locator(".safe li")).to_have_text([f"✓{fact}" for fact in SAFETY])
    expect(summary.locator(".safe")).not_to_contain_text("Assist will be able to control")  # P12.3.3 dropped it.
    tests = summary.locator(".tests li")
    expect(tests.nth(0)).to_have_class("ok")
    expect(tests.nth(0).locator(".row > span")).to_have_text(["✓", "Open the garage", "3 → 1 turns"])
    expect(tests.nth(1)).to_have_class("no")
    expect(tests.nth(1).locator(".row > span")).to_have_text(["–", "Close the garage at night", "not improved"])
    assert summary.evaluate("(el) => el.nextElementSibling.matches('.card') && !el.previousElementSibling?.matches('.card')")


def test_a_review_without_a_benefit_leaves_that_line_out(open_panel):
    panel = open_review(open_panel, benefit=None)
    expect(panel.find(".summary .label")).to_have_text(["Before & after", "Why it's safe"])
    expect(panel.find(".summary .lead")).to_have_count(0)


@pytest.mark.parametrize("size", [PHONE, DESKTOP], ids=["phone", "desktop"])
def test_each_test_shows_its_before_and_after_open(open_panel, size):
    panel = open_review(open_panel, size=size)
    tests = panel.find(".tests > li")
    expect(tests.locator(".rp")).to_have_count(2)
    replay = tests.nth(0).locator(".rp")
    expect(replay).to_be_visible()  # The evidence for what you get, open at first.
    expect(replay.locator(".rp-head")).to_have_text(["Before", "After"])
    before, after = replay.locator(".rp-before"), replay.locator(".rp-after")
    # Read as the home page reads a conversation: you on the right, Assist on the left, each failed step where it
    # happened.
    expect(before.locator(".cs-turn")).to_have_count(3)
    expect(before.locator(".cs-you .cs-says")).to_have_text(["Open the garage.", "Both.", "Both doors, at once."])
    expect(before.locator(".cs-turn").first.locator(".cs-step")).to_have_text(
        "! Failed step: Turn on · Garage door Multiple entities matched 'Garage door'")
    expect(before.locator(".cs-step")).to_have_count(1)
    expect(after.locator(".cs-turn")).to_have_count(1)
    expect(after.locator(".cs-assist .cs-says")).to_have_text("Done: both garage doors are opening.")
    expect(tests.nth(1).locator(".rp")).to_be_visible()
    panel.click(".tests > li:nth-child(2) summary")  # Each folds on its own.
    expect(tests.nth(1).locator(".rp")).to_be_hidden()
    expect(replay).to_be_visible()
    b, a = before.bounding_box(), after.bounding_box()
    if size == DESKTOP:  # Side by side where there's room, as it went on the left.
        assert a["x"] >= b["x"] + b["width"] and abs(a["y"] - b["y"]) < 1
    else:  # On a phone, as it went, then with the change under it.
        assert a["y"] >= b["y"] + b["height"] and abs(a["x"] - b["x"]) < 1
    assert_no_horizontal_scroll(panel)



def test_an_opened_replay_stays_open_when_the_panel_is_made_again(open_panel):
    panel = open_review(open_panel)
    panel.click(".tests > li:nth-child(1) summary")  # Folded by hand.
    panel.rebuild()  # As after a decision changes the sidebar count.
    expect(panel.find(".tests > li").nth(1).locator(".rp")).to_be_visible()
    expect(panel.find(".tests > li").nth(0).locator(".rp")).to_be_hidden()


def test_a_test_whose_conversation_is_gone_shows_its_replay_alone(open_panel):
    panel = open_review(open_panel, tests=[{"summary": "Open the garage", "improved": True, "result": "3 → 1 turns",
                                            "before": [], "after": OPENED["after"]}])
    expect(panel.find(".rp-head")).to_have_text(["After"])
    expect(panel.find(".rp-after .cs-turn")).to_have_count(1)


@pytest.mark.parametrize("size", [PHONE, DESKTOP], ids=["phone", "desktop"])
@pytest.mark.parametrize("dark", [True, False], ids=["dark", "light"])
def test_a_test_whose_replay_made_assist_wordier_says_so(open_panel, size, dark):
    """Counted by code: its row says Assist says more (the words a reply in its tooltip), and a line under the tests
    says how many did; a test without it shows neither."""
    panel = open_review(open_panel, size=size, dark=dark, wordier=1, tests=[
        {"summary": "Open the garage", "improved": True, "result": "3 → 1 turns", **OPENED,
         "more_words": {"before": 4, "after": 19}},
        {"summary": "Close the garage at night", "improved": False, "result": "not improved", **CLOSED}])
    chip = panel.find(".tests > li").nth(0).locator(".result.more-words")
    expect(chip).to_have_text("Says more")
    expect(chip).to_have_attribute("title", "Assist's replies: about 19 words each, 4 before.")
    expect(panel.find(".tests > li").nth(1).locator(".more-words")).to_have_count(0)
    expect(panel.find(".more-words-line")).to_have_text("With this change, Assist's replies got longer in 1 of these.")
    assert_no_horizontal_scroll(panel)
    if shots := os.environ.get("UI_SHOTS"):  # A visual check: UI_SHOTS=<folder> saves each view there.
        panel.find(".tests").scroll_into_view_if_needed()
        panel.page.screenshot(path=f"{shots}/review-{'phone' if size == PHONE else 'desktop'}-{'dark' if dark else 'light'}.png")


# An automation's review (weeks.reviewed): when it runs, in Home Assistant's own terms put in words by code, and its
# simulated week, counts and times first; each run opened.

DUSK_WEEK = {"name": "Porch at dusk", "change": "new", "when": "At 18:00", "if": "If Porch light is off",
             "week": {"days": 7, "result": "7 runs at 18:00", "more": 0, "runs": [
                 {"sign": " ", "at": f"{day} 18:00", "changed": "Porch light: off → on"}
                 for day in ("Fri", "Sat", "Sun", "Mon", "Tue", "Wed", "Thu")]}}
HALL_WEEK = {"name": "Hall at night", "change": "edited", "when": "At 23:00 or when Hall motion changes to off for 10 minutes",
             "if": None, "week": {"days": 7, "result": "2 runs added at 23:00, 1 dropped at 22:00", "more": 0, "runs": [
                 {"sign": "+", "at": "Fri 23:00", "changed": "Hall light: on → off"},
                 {"sign": "+", "at": "Sat 23:00", "changed": "Hall light: on → off"},
                 {"sign": "-", "at": "Fri 22:00", "changed": "Hall light: on → off"}]}}
NIGHT_OFF = {"name": "Night lights", "change": "off", "when": "At 22:00", "if": None, "failed": True}
# What changes and Why it's safe as panel.change_file and panel.safety give them for these changes.
TESTED, CHECKED = SAFETY[0], SAFETY[-1]
DUSK_ROWS = [{"sign": "+", "old": None, "new": n + 1, "text": t} for n, t in enumerate(
    ["id: porch_at_dusk", "alias: Porch at dusk", "triggers:", "- trigger: time", "  at: '18:00:00'", "conditions:",
     "- condition: state", "  entity_id: light.porch", "  state: 'off'", "actions:", "- action: light.turn_on",
     "  target:", "    entity_id: light.porch"])]
HALL_ROWS = [{"sign": " ", "old": 3, "new": 3, "text": "triggers:"},
             {"sign": " ", "old": 4, "new": 4, "text": "- trigger: time"},
             {"sign": "-", "old": 5, "new": None, "text": "  at: '22:00:00'"}] + [
    {"sign": "+", "old": None, "new": n, "text": t} for n, t in enumerate(
        ["  at: '23:00:00'", "- trigger: state", "  entity_id: binary_sensor.hall_motion", "  to: 'off'", "  for:",
         "    minutes: 10"], 5)] + [{"sign": " ", "old": 6, "new": 11, "text": "actions:"},
                                   {"sign": " ", "old": 7, "new": 12, "text": "- action: light.turn_off"}]
WEEK_CHANGES = [{"name": "Porch at dusk", "kind": "automation", "rows": DUSK_ROWS, "added": 13, "removed": 0},
                {"name": "Hall at night", "kind": "automation", "rows": HALL_ROWS, "added": 6, "removed": 1},
                {"name": "Night lights", "kind": "automation", "rows": [{"sign": "-", "old": None, "new": None,
                                                                        "text": "on"}], "added": 0, "removed": 1}]
WEEK_SAFETY = [TESTED, "Changes: 1 new automation, 1 automation updated, 1 automation turned off. The automations "
               "run when they're triggered.", CHECKED]
SCENE_ROWS = [{"sign": "+", "old": None, "new": n + 1, "text": t} for n, t in enumerate(
    ["name: Reading", "entities:", "  light.desk:", "    state: 'on'", "    brightness: 200"])]
RECOMMENDED = {"name": "For Kitchen, consider: web_search: true", "kind": "assistant", "rows": [], "added": 0,
               "removed": 0, "recommended": True}
MIXED = [{"name": "Porch at dusk", "kind": "automation", "rows": DUSK_ROWS, "added": 13, "removed": 0},
         {"name": "Reading", "kind": "scene", "rows": SCENE_ROWS, "added": 5, "removed": 0},
         {"name": "Desk lamp", "kind": "device", "rows": [{"sign": "-", "old": 1, "new": None, "text": "area: Hall"},
                                                         {"sign": "+", "old": None, "new": 1, "text": "area: Study"}],
          "added": 1, "removed": 1},
         {"name": "Study", "kind": "area", "rows": [{"sign": "+", "old": None, "new": 1, "text": "- the den"}],
          "added": 1, "removed": 0}, RECOMMENDED]
MIXED_SAFETY = [TESTED, "Changes: 1 new automation, 1 new scene, 1 device setting changed, 1 area setting changed. "
                "The automation runs when it's triggered.", CHECKED]


@pytest.mark.parametrize("size", [PHONE, DESKTOP], ids=["phone", "desktop"])
@pytest.mark.parametrize("dark", [True, False], ids=["dark", "light"])
def test_an_automations_review_says_when_it_runs_and_what_its_week_did_each_run_on_demand(open_panel, size, dark):
    """Kai's words: "An automation's review says when it would run and what it would have done over a simulated week;
    for a change to one you have, the runs it adds and drops." Counts and times first, each run opened; one whose week
    couldn't run says so."""
    panel = open_review(open_panel, size=size, dark=dark, automations=[DUSK_WEEK, HALL_WEEK, NIGHT_OFF],
                        changes=WEEK_CHANGES, safety=WEEK_SAFETY)
    expect(panel.find(".summary .label")).to_have_text(["What you get", "Before & after", "When it runs", "Why it's safe"])
    rows = panel.find(".autos > li")
    expect(rows.locator(".what .name")).to_have_text(["Porch at dusk", "Hall at night", "Night lights"])
    expect(rows.locator(".kind")).to_have_text(["new", "edited", "turned off"])
    expect(rows.nth(0).locator(".when")).to_have_text("At 18:00 · If Porch light is off")
    expect(rows.locator(".result")).to_have_text(["7 runs at 18:00", "2 runs added at 23:00, 1 dropped at 22:00",
                                                  "week not simulated"])
    expect(rows.nth(0).locator(".result")).to_have_attribute("title", "Over a simulated week after your conversation")
    expect(rows.nth(0).locator(".week-runs")).to_be_hidden()  # Each run on demand.
    panel.click(".autos > li:nth-child(2) summary")
    expect(rows.nth(1).locator(".week-run")).to_have_text([re.compile(r"\+\s*Fri 23:00\s*Hall light: on → off"),
                                                     re.compile(r"\+\s*Sat 23:00"), re.compile(r"−\s*Fri 22:00")])
    expect(rows.nth(1).locator(".week-run.add")).to_have_count(2)
    expect(rows.nth(1).locator(".week-run.del")).to_have_count(1)
    expect(rows.nth(2).locator("details")).to_have_count(0)  # Nothing to open.
    assert_no_horizontal_scroll(panel)
    if shots := os.environ.get("UI_SHOTS"):  # A visual check: UI_SHOTS=<folder> saves each view there.
        panel.find(".autos").scroll_into_view_if_needed()
        panel.page.screenshot(path=f"{shots}/review-week-{'phone' if size == PHONE else 'desktop'}-{'dark' if dark else 'light'}.png")


def test_a_week_that_stopped_early_says_how_many_days_ran_and_a_long_one_how_many_runs_are_past_those_shown(open_panel):
    looping = {**DUSK_WEEK, "week": {**DUSK_WEEK["week"], "days": 2, "more": 1180}}
    panel = open_review(open_panel, automations=[looping])
    expect(panel.find(".autos .result")).to_have_text("7 runs at 18:00 · 2 of 7 days simulated")
    panel.click(".autos summary")
    expect(panel.find(".week-run.more")).to_have_text("and 1180 more")


def test_a_review_without_automations_has_no_week(open_panel):
    panel = open_review(open_panel)
    expect(panel.find(".autos")).to_have_count(0)
    expect(panel.find(".summary .label")).to_have_text(["What you get", "Before & after", "Why it's safe"])


@pytest.mark.parametrize("size", [PHONE, DESKTOP], ids=["phone", "desktop"])
@pytest.mark.parametrize("dark", [True, False], ids=["dark", "light"])
def test_what_changes_tints_each_kind_and_names_a_better_assistant_setting_never_applied(open_panel, size, dark):
    """An automation, a scene, a device's and an area's settings, each kind its tint (a device's and an area's settings
    share one); a better setting for an assistant is named for you to change yourself, never applied or tested (Kai's
    words: "named for you to change yourself, never applied or tested")."""
    panel = open_review(open_panel, size=size, dark=dark, changes=MIXED, safety=MIXED_SAFETY)
    expect(panel.find(".safe li").nth(1)).to_contain_text("The automation runs when it's triggered.")
    expect(panel.find(".safe li").nth(1)).not_to_contain_text("Nothing runs")  # Its save may set it off.
    open_changes(panel)
    files = panel.find(".file")
    expect(files.locator(".kind")).to_have_text(["automation", "scene", "device", "area", "assistant"])
    colors = files.locator(".kind").evaluate_all("(chips) => chips.map((c) => getComputedStyle(c).color)")
    assert len(set(colors[:4])) == 3  # A device's and an area's settings share theirs.
    files.nth(4).locator("summary").click()
    expect(files.nth(4).locator(".empty")).to_have_text("Not applied or tested: change it yourself if you want it.")
    assert_no_horizontal_scroll(panel)
    if shots := os.environ.get("UI_SHOTS"):  # A visual check: UI_SHOTS=<folder> saves each view there.
        files.nth(0).scroll_into_view_if_needed()
        panel.page.screenshot(path=f"{shots}/review-changes-{'phone' if size == PHONE else 'desktop'}-{'dark' if dark else 'light'}.png")


@pytest.mark.parametrize("size", [PHONE, DESKTOP], ids=["phone", "desktop"])
@pytest.mark.parametrize("dark", [True, False], ids=["dark", "light"])
def test_what_went_wrong_notes_motion_isnt_simulated_in_every_view(open_panel, size, dark):
    panel = open_review(open_panel, size=size, dark=dark)
    panel.find("details[data-key='details'] > summary").click()
    expect(panel.find(".details-body .note")).to_be_visible()
    assert_no_horizontal_scroll(panel)
    if shots := os.environ.get("UI_SHOTS"):  # A visual check: UI_SHOTS=<folder> saves each view there.
        panel.find(".details-body .note").evaluate("(note) => note.scrollIntoView({block: 'center'})")
        panel.page.wait_for_timeout(500)  # The section's opening fade.
        panel.page.screenshot(path=f"{shots}/review-motion-{'phone' if size == PHONE else 'desktop'}-{'dark' if dark else 'light'}.png")


def test_a_review_without_a_wordier_test_has_no_line_about_it(open_panel):
    panel = open_review(open_panel)
    expect(panel.find(".more-words, .more-words-line")).to_have_count(0)


def test_a_replay_that_stopped_before_assist_replied_says_so(open_panel):
    panel = open_review(open_panel, tests=[{"summary": "Open the garage", "improved": False,
                                            "result": "not improved", "before": OPENED["before"], "after": []}])
    expect(panel.find(".rp-after .rp-empty")).to_have_text("The replay stopped before Assist replied.")
    expect(panel.find(".rp-before .cs-turn")).to_have_count(3)


def test_a_test_without_a_conversation_to_read_doesnt_open(open_panel):
    panel = open_review(open_panel, tests=[{"summary": "Open the garage", "improved": True, "result": "3 → 1 turns",
                                            "before": [], "after": []}])
    expect(panel.find(".tests summary")).to_have_count(0)
    expect(panel.find(".tests .row > span")).to_have_text(["✓", "Open the garage", "3 → 1 turns"])
    # In line with tests that open.
    panel = open_review(open_panel, tests=[*review()["tests"], {"summary": "Open the gate", "improved": True,
                                                                "result": "improved"}])
    marks = [panel.find(f".tests > li:nth-child({n}) .mark").bounding_box()["x"] for n in (1, 3)]
    assert abs(marks[0] - marks[1]) < 1


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_a_replay_reads_clearly_in_light_and_dark(open_panel, dark):
    panel = open_review(open_panel, dark=dark)
    assert not low_contrast(panel, [".rp-before .rp-head", ".rp-after .rp-head", ".rp .cs-you .cs-says p", ".rp .cs-assist .cs-says p",
                                    ".rp .cs-step-what"], 4.5)


def test_a_replay_is_escaped(open_panel):
    turn = {"you": XSS, "assist": XSS, "failed": [{"step": XSS, "error": XSS}]}
    panel = open_review(open_panel, tests=[{"summary": XSS, "improved": True, "result": XSS, "before": [turn],
                                            "after": [turn]}])
    expect(panel.find(".rp-after .cs-says")).to_have_text([XSS, XSS])
    expect(panel.find(".rp-before .cs-step-error")).to_have_text(XSS)
    assert_escaped(panel)


def test_below_the_summary_what_went_wrong_then_what_changes_both_folded(open_panel):
    """Kai: the diagnosis first, collapsed, then the proposed changes, the same; nothing else."""
    panel = open_review(open_panel)
    sections = panel.find(".page > .card:not(.summary) > details")
    expect(sections.locator("> summary")).to_have_text(["What went wrong", "What changes (2)"])
    assert sections.evaluate_all("(all) => all.map((d) => d.open)") == [False, False]
    expect(panel.find(".file table.diff").first).to_be_hidden()
    open_changes(panel)
    expect(panel.find(".file table.diff")).to_have_count(2)
    assert panel.find(".file > details").evaluate_all("(all) => all.every((d) => d.open)")  # Short: open to read.


def test_a_long_file_stays_folded_until_opened(open_panel):
    rows = [{"sign": "+", "old": None, "new": n, "text": f"line {n}"} for n in range(1, 61)]
    panel = open_review(open_panel, changes=[{"name": "Garage doors", "kind": "script", "rows": rows, "added": 60,
                                              "removed": 0}, *review()["changes"][1:]])
    open_changes(panel)
    assert panel.find(".file > details").evaluate_all("(all) => all.map((d) => d.open)") == [False, True]


def test_what_it_gets_you_is_counted_under_the_headline_with_no_section_repeating_it(open_panel):
    panel = open_review(open_panel)
    expect(panel.find(".outcome")).to_have_text("1 new thing Assist can do · 2 fewer turns")
    expect(panel.find("details[data-key='enabled'], details[data-key='removed']")).to_have_count(0)
    panel = open_review(open_panel, enabled=0, removed=0)
    expect(panel.find(".outcome")).to_have_text("")


def test_the_changeset_shows_each_change_as_a_file_with_a_line_numbered_diff(open_panel):
    panel = open_review(open_panel, size=DESKTOP)
    open_changes(panel)
    files = panel.find(".file")
    expect(files).to_have_count(2)
    script = files.nth(0)
    expect(script.locator(".name")).to_have_text("Garage doors")
    expect(script.locator(".kind")).to_have_text("script")
    expect(script.locator(".kind")).to_have_class("kind script")
    expect(script.locator(".count-add")).to_have_text("+5")
    expect(script.locator(".count-del")).to_have_count(0)
    expect(script.locator("table.diff")).to_be_visible()  # Short: open at first.
    rows = script.locator("tr")
    expect(rows).to_have_count(5)
    expect(rows.nth(2)).to_have_class("add")
    expect(rows.nth(2).locator("td")).to_have_text(["", "3", "+", "- action: button.press"])
    instructions = files.nth(1)
    expect(instructions.locator(".count-add")).to_have_text("+1")
    expect(instructions.locator(".count-del")).to_have_text("−1")
    rows = instructions.locator("tr")
    expect(rows.nth(0).locator("td")).to_have_text(["1", "1", "", "Keep answers short."])
    expect(rows.nth(1)).to_have_class("del")
    expect(rows.nth(1).locator("td")).to_have_text(["2", "", "−", "Ask which garage door."])
    expect(rows.nth(3)).to_have_class("gap")
    expect(rows.nth(3)).to_have_text("⋯")


@pytest.mark.parametrize("size, shown", [(PHONE, False), (DESKTOP, True)], ids=["phone", "desktop"])
def test_line_numbers_give_way_to_the_text_on_a_phone(open_panel, size, shown):
    panel = open_review(open_panel, size=size)
    open_changes(panel)
    number = panel.find(".file td.num").first
    expect(number).to_be_visible() if shown else expect(number).to_be_hidden()
    expect(panel.find(".file td.text").first).to_be_visible()
    # A diff with a gap in it reads as wide as one without: the text takes the file's width, less its sign.
    for file in panel.find(".file").all():
        text, box = file.locator("td.text").first.bounding_box(), file.bounding_box()
        assert text["width"] >= box["width"] - (140 if shown else 40), (text, box)


@pytest.mark.parametrize("size", [SMALL_PHONE, DESKTOP], ids=["small-phone", "desktop"])
def test_a_long_diff_line_wraps_instead_of_being_cut_off(open_panel, size):
    long = "    entity_id: button.garage_door_left_relay_switch_for_the_big_door_on_the_north_side_of_the_house"
    rows = [*SCRIPT_ROWS, {"sign": "+", "old": None, "new": 6, "text": long}]
    panel = open_review(open_panel, size=size, changes=[{"name": "Garage doors", "kind": "script", "rows": rows,
                                                         "added": 6, "removed": 0}])
    open_changes(panel)
    cell = panel.find("td.text").last
    assert cell.evaluate("(el) => el.scrollWidth <= el.clientWidth")
    file = panel.find(".file").bounding_box()
    box = cell.bounding_box()
    assert box["x"] + box["width"] <= file["x"] + file["width"] + 1


@pytest.mark.parametrize("flag, text", [("already", "Already in place; nothing changes."),
                                        ("proposed", "As proposed; not applied.")])
def test_a_change_without_a_diff_says_why(open_panel, flag, text):
    change = {"name": "Garage doors", "kind": "aliases", "rows": [], "added": 0, "removed": 0, flag: True}
    panel = open_review(open_panel, changes=[change])
    open_changes(panel)
    panel.find(".file summary").click()
    expect(panel.find(".file .empty")).to_have_text(text)


# An edit of one assistant's own instructions, as panel.review sends it: that assistant's instructions as a diff.
SPEAKER = "{% if user_name %}The person speaking is {{ user_name }}.{% endif %}"
ASSISTANT_ROWS = [{"sign": " ", "old": 1, "new": 1, "text": "You are a voice assistant for Home Assistant."},
                  {"sign": " ", "old": 2, "new": 2, "text": "Answer in one short sentence."},
                  {"sign": "+", "old": None, "new": 3, "text": SPEAKER}]
ASSISTANT_EDIT = {"name": "Kitchen assistant's instructions", "kind": "instructions", "rows": ASSISTANT_ROWS,
                  "added": 1, "removed": 0}


def test_an_edit_of_one_assistants_instructions_is_that_assistants_file_with_its_template_line_shown_as_written(
        open_panel):
    panel = open_review(open_panel, size=DESKTOP, changes=[ASSISTANT_EDIT])
    open_changes(panel)
    file = panel.find(".file")
    expect(file.locator(".name")).to_have_text("Kitchen assistant's instructions")
    expect(file.locator(".kind")).to_have_text("instructions")
    expect(file.locator(".count-add")).to_have_text("+1")
    expect(file.locator(".count-del")).to_have_count(0)
    rows = file.locator("tr")
    expect(rows).to_have_count(3)
    expect(rows.nth(2)).to_have_class("add")
    expect(rows.nth(2).locator("td")).to_have_text(["", "3", "+", SPEAKER])  # Template code, as text.


def test_a_decided_edit_of_an_assistants_instructions_says_what_it_would_have_done(open_panel):
    added = {"name": f"Add to Kitchen assistant's instructions: “{SPEAKER}”", "kind": "instructions", "rows": [],
             "added": 0, "removed": 0, "proposed": True}
    removed = {**added, "name": "Remove from Kitchen assistant's instructions: “Answer in one short sentence.”"}
    panel = open_review(open_panel, status="declined", choices=[], changes=[added, removed])
    open_changes(panel)
    expect(panel.find(".file .name")).to_have_text([added["name"], removed["name"]])
    assert_no_horizontal_scroll(panel)  # A long template line wraps on a phone.


def test_what_went_wrong_leaves_out_the_door_note_where_no_door_or_valve_is_involved(open_panel):
    panel = open_review(open_panel, physical=False)
    panel.find("details[data-key='details'] > summary").click()
    expect(panel.find(".details-body p")).to_have_count(1)
    expect(panel.find(".details-body .note")).to_have_count(0)


def test_what_went_wrong_gives_the_cause_and_what_the_sandbox_doesnt_simulate(open_panel):
    panel = open_review(open_panel)
    panel.find("details[data-key='details'] > summary").click()
    body = panel.find(".details-body")
    expect(body.locator("p")).to_have_text("Assist couldn't press both garage relays at once, so it asked which door.")
    expect(body.locator(".note")).to_contain_text("Motion isn't simulated: a door, valve, lock or alarm counts as done "
                                                  "once its command succeeds.")


def test_a_conflict_says_it_cant_apply_now_and_why(open_panel):
    panel = open_review(open_panel, conflicts=[{"what": "Garage doors", "why": "changed since it was tested"},
                                               {"what": "Kitchen light", "why": "removed"}])
    expect(panel.find(".conflict")).to_have_text(
        "Can't apply it now:Garage doors: changed since it was testedKitchen light: removed")


def test_a_revision_links_to_the_note_it_answers(open_panel):
    panel = open_review(open_panel, revises={"id": "imp0", "title": "Garage", "note": "Open both doors together"})
    expect(panel.find(".revises")).to_have_text("Revised after your note on “Garage”: “Open both doors together”")


def test_a_revision_without_a_note_names_only_what_it_revises(open_panel):
    panel = open_review(open_panel, revises={"id": "imp0", "title": "Garage", "note": ""})
    expect(panel.find(".revises")).to_have_text("Revised after your note on “Garage”")


def test_a_version_after_a_conflict_says_so_not_a_note(open_panel):
    panel = open_review(open_panel, revises={"id": "imp0", "title": "Garage", "note": "", "conflict": True})
    expect(panel.find(".revises")).to_have_text("Revised after a conflict on “Garage”")


UNREVISED = {"note": "make it actually broadcast on my speakers",
             "why": "No test of a script speaking on the speakers validated."}


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
@pytest.mark.parametrize("size", [PHONE, DESKTOP], ids=["phone", "desktop"])
def test_a_revision_that_brought_no_new_version_says_so_with_why_above_the_version_still_up(open_panel, size, dark):
    """A real run, 2026-10-30: a revision round that ends with no new version says so on the version still up for
    review, with the proposer's reason, rather than looking as if nothing happened; amber, read at a glance."""
    panel = open_review(open_panel, size=size, dark=dark, unrevised=UNREVISED)
    line = panel.find(".revises.unrevised")
    expect(line).to_have_text("No new version for your note “make it actually broadcast on my speakers”: No test of a "
                              "script speaking on the speakers validated.")
    assert panel.contrast(".revises.unrevised") >= 4.5
    box, card = line.bounding_box(), panel.find(".card").first.bounding_box()
    assert box["y"] + box["height"] <= card["y"], (box, card)  # Above the version, read first.
    page = panel.find(".page").bounding_box()
    assert box["x"] >= page["x"] - 0.5 and box["x"] + box["width"] <= page["x"] + page["width"] + 0.5, (box, page)
    assert_no_horizontal_scroll(panel)


def test_a_revision_that_brought_no_new_version_without_a_reason_names_the_note(open_panel):
    panel = open_review(open_panel, unrevised={"note": "only the left door", "why": ""})
    expect(panel.find(".revises.unrevised")).to_have_text("No new version for your note “only the left door”.")


def test_one_decision_at_a_time_with_accept_ready(open_panel):
    panel = open_review(open_panel)
    footer = panel.find(".footer")
    expect(footer.locator("#decide")).to_have_text("Accept changes")
    expect(footer.locator("textarea")).to_have_count(0)  # Accepting needs no note.
    expect(footer.locator("#arrow")).to_have_attribute("aria-label", "Other decisions")
    expect(footer.locator(".menu")).to_have_count(0)
    expect(footer.locator("button.main")).to_have_count(1)


def test_the_other_decisions_are_one_menu_away(open_panel):
    panel = open_review(open_panel)
    panel.click("#arrow")
    options = panel.find(".footer .menu [data-choice]")
    expect(options).to_have_text([c["label"] for c in CHOICES])
    expect(panel.find(".footer .menu .on")).to_have_text("Accept changes")
    panel.click(".footer .menu [data-choice='revise']")
    expect(panel.find(".footer .menu")).to_have_count(0)
    expect(panel.find("#decide")).to_have_text("Ask for a revision")


@pytest.mark.parametrize("decision, prompt", [
    ("revise", "What should change? (needed for the revision)"),
    ("decline", "Why not? (optional)"),
    ("accept", None),
])
def test_a_decision_asks_for_a_note_only_where_it_helps(open_panel, decision, prompt):
    panel = open_review(open_panel)
    panel.click("#arrow")
    panel.click(f".footer .menu [data-choice='{decision}']")
    if prompt:
        expect(panel.find("#note")).to_have_attribute("placeholder", prompt)
        expect(panel.find("#note")).to_be_focused()
    else:
        expect(panel.find("#note")).to_have_count(0)


def test_three_decisions_and_decline_can_be_given_as_not_an_issue(open_panel):
    panel = open_review(open_panel)
    panel.click("#arrow")
    expect(panel.find(".footer .menu [data-choice]")).to_have_text(["Accept changes", "Ask for a revision", "Decline"])
    panel.click(".footer .menu [data-choice='decline']")
    reason = panel.find(".footer .reason")
    expect(reason).to_have_text("Not an issue It went fine, or you were only trying Assist out.")
    expect(reason.locator("input[type=checkbox]")).not_to_be_checked()
    reason.locator("input").check()
    panel.settle()
    # Ticked: the note asks only for anything to add.
    expect(panel.find("#note")).to_have_attribute("placeholder", "Anything to add? (optional)")
    panel.find("#note").fill("I was showing a friend")
    panel.click("#decide")
    assert panel.calls("decide")[-1] == {"type": "assist_improver/decide", "improvement_id": "imp1",
                                         "decision": "decline", "note": "I was showing a friend",
                                         "reason": "not_an_issue"}


def test_a_plain_decline_sends_no_reason(open_panel):
    panel = open_review(open_panel)
    panel.click("#arrow")
    panel.click(".footer .menu [data-choice='decline']")
    panel.click("#decide")
    assert panel.calls("decide")[-1] == {"type": "assist_improver/decide", "improvement_id": "imp1",
                                         "decision": "decline", "note": ""}


def test_not_an_issue_shows_only_with_decline_and_is_never_sent_with_another_decision(open_panel):
    panel = open_review(open_panel)
    expect(panel.find(".footer .reason")).to_have_count(0)  # Accept, the default: no reason to give.
    panel.click("#arrow")
    panel.click(".footer .menu [data-choice='decline']")
    panel.find(".footer .reason input").check()
    panel.settle()
    panel.click("#arrow")
    panel.click(".footer .menu [data-choice='revise']")
    expect(panel.find(".footer .reason")).to_have_count(0)
    expect(panel.find("#note")).to_have_attribute("placeholder", "What should change? (needed for the revision)")
    panel.find("#note").fill("Both doors")
    panel.click("#decide")
    assert panel.calls("decide")[-1] == {"type": "assist_improver/decide", "improvement_id": "imp1",
                                         "decision": "revise", "note": "Both doors"}


@pytest.mark.parametrize("size", [SMALL_PHONE, DESKTOP], ids=["small-phone", "desktop"])
def test_declining_as_not_an_issue_fits_the_footer(open_panel, size):
    panel = open_review(open_panel, size=size)
    panel.click("#arrow")
    panel.click(".footer .menu [data-choice='decline']")
    box, note = panel.find(".footer .reason input").bounding_box(), panel.find("#note").bounding_box()
    assert box["y"] + box["height"] <= note["y"] and abs(box["x"] - note["x"]) < 2  # Above the note, in line with it.
    assert_no_horizontal_scroll(panel)


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_not_an_issue_reads_clearly_in_light_and_dark(open_panel, dark):
    panel = open_review(open_panel, dark=dark)
    panel.click("#arrow")
    panel.click(".footer .menu [data-choice='decline']")
    assert not low_contrast(panel, [".reason-label", ".reason-hint"], 4.5)


def test_the_menu_closes_on_a_click_outside(open_panel):
    panel = open_review(open_panel, size=DESKTOP)
    panel.click("#arrow")
    panel.find("#menu-backdrop").click(position={"x": 5, "y": 5})
    panel.settle()
    expect(panel.find(".footer .menu")).to_have_count(0)
    expect(panel.find("#decide")).to_have_text("Accept changes")


def test_accepting_says_what_happened_with_undo_and_the_next_review_one_click_away(open_panel):
    panel = open_review(open_panel)
    panel.answer("improvement", review(status="applied", choices=[{"value": "undo", "label": "Undo changes"}],
                                       next_pending={"id": "imp2", "title": CAR}))
    color = "(el) => getComputedStyle(el).color"  # The arrow's mark reads as the button's words do.
    assert panel.find("#arrow .caret").evaluate(color) == panel.find("#decide").evaluate(color)
    panel.hold("decide")
    size = panel.find(".split").bounding_box()
    panel.find("#decide").click()
    # On its way: the button keeps its words and size, and the arrow turns into a spinner; neither takes a click.
    expect(panel.find("#decide")).to_have_text("Accept changes")
    expect(panel.find("#decide")).to_be_disabled()
    expect(panel.find("#arrow")).to_be_disabled()
    expect(panel.find("#arrow .spinner")).to_be_visible()
    assert panel.find(".split").bounding_box() == size
    panel.release("decide")
    assert panel.calls("decide") == [{"type": "assist_improver/decide", "improvement_id": "imp1", "decision": "accept",
                                      "note": ""}]
    message = panel.find(".footer .done-msg")
    assert classes(message) == {"done-msg", "ok"}
    expect(message.locator(".mark")).to_have_text("✓")
    expect(message).to_contain_text(DECIDED["result"])
    expect(message.locator("#undo-link")).to_have_text("Undo")
    expect(panel.find("#next")).to_have_text(f"Next: {CAR} →")


def test_a_revision_sends_its_note(open_panel):
    panel = open_review(open_panel)
    panel.click("#arrow")
    panel.click(".footer .menu [data-choice='revise']")
    panel.find("#note").fill("Open both doors together")
    panel.click("#decide")
    assert panel.calls("decide")[-1] == {"type": "assist_improver/decide", "improvement_id": "imp1",
                                         "decision": "revise", "note": "Open both doors together"}


def test_accepting_never_sends_a_note_typed_for_another_decision(open_panel):
    panel = open_review(open_panel)
    panel.click("#arrow")
    panel.click(".footer .menu [data-choice='decline']")
    panel.find("#note").fill("Not now")
    panel.click("#arrow")
    panel.click(".footer .menu [data-choice='accept']")
    panel.click("#decide")
    assert panel.calls("decide")[-1]["note"] == ""


def test_a_note_survives_switching_between_decisions_that_take_one(open_panel):
    panel = open_review(open_panel)
    panel.click("#arrow")
    panel.click(".footer .menu [data-choice='decline']")
    panel.find("#note").fill("Both doors, please")
    panel.click("#arrow")
    panel.click(".footer .menu [data-choice='revise']")
    expect(panel.find("#note")).to_have_value("Both doors, please")


def test_next_goes_straight_to_the_next_review(open_panel):
    panel = open_review(open_panel)
    panel.answer("improvement", review(status="applied", choices=[{"value": "undo", "label": "Undo changes"}],
                                       next_pending={"id": "imp2", "title": CAR}))
    panel.click("#decide")
    panel.answer("improvement", review(id="imp2", title=CAR))
    panel.click("#next")
    assert panel.path() == "/assist-improver?id=imp2"
    assert panel.calls("improvement")[-1]["improvement_id"] == "imp2"
    expect(panel.find("h1")).to_contain_text(CAR)
    expect(panel.find("#decide")).to_have_text("Accept changes")  # The last review's outcome doesn't follow.


def test_after_the_last_review_it_leads_back_to_the_list(open_panel):
    panel = open_review(open_panel)
    panel.answer("improvement", review(status="applied", choices=[{"value": "undo", "label": "Undo changes"}],
                                       next_pending=None))
    panel.click("#decide")
    expect(panel.find("#back-list")).to_have_text("All caught up · back to the list")
    panel.click("#back-list")
    assert panel.path() == "/assist-improver"
    expect(panel.find(".cs-title")).to_have_text("Your conversations")


def test_with_a_question_waiting_the_last_review_leads_back_to_the_list_not_all_caught_up(open_panel):
    panel = open_review(open_panel)
    panel.answer("improvement", review(status="applied", choices=[{"value": "undo", "label": "Undo changes"}],
                                       next_pending=None, questions=1))
    panel.click("#decide")
    expect(panel.find("#back-list")).to_have_text("Back to the list")


def test_undo_is_one_click_from_what_happened(open_panel):
    panel = open_review(open_panel)
    panel.answer("improvement", review(status="applied", choices=[{"value": "undo", "label": "Undo changes"}],
                                       next_pending=None))
    panel.click("#decide")
    panel.answer("decide", {"result": "Undone: what was there before is back.", "status": "awaiting_review",
                            "ok": True})
    panel.answer("improvement", review(next_pending=None))  # Up for review again, with every choice.
    panel.click("#undo-link")
    assert panel.calls("decide")[-1] == {"type": "assist_improver/decide", "improvement_id": "imp1",
                                         "decision": "undo", "note": ""}
    expect(panel.find(".done-msg")).to_contain_text("Undone: what was there before is back.")
    # No decision pops up where Undo was: deciding again is a step of its own, beside what happened.
    expect(panel.find("#decide")).to_have_count(0)
    expect(panel.find(".done-msg #again-link")).to_have_text("Decide again")
    expect(panel.find("#back-list")).to_have_text("Back to the list")  # Not all caught up: this one waits again.
    panel.click("#again-link")
    expect(panel.find(".done-msg")).to_contain_text("Undone: what was there before is back.")
    expect(panel.find(".done-msg #again-link")).to_have_count(0)
    expect(panel.find("#decide")).to_have_text("Accept changes")
    panel.click("#arrow")
    expect(panel.find(".menu [data-choice]")).to_have_count(len(CHOICES))
    panel.click("#menu-backdrop")
    panel.answer("decide", DECIDED)
    panel.answer("improvement", review(status="applied", choices=[{"value": "undo", "label": "Undo changes"}],
                                       next_pending=None))
    panel.click("#decide")
    assert panel.calls("decide")[-1]["decision"] == "accept"
    expect(panel.find("#undo-link")).to_be_visible()


def test_a_decision_that_changed_nothing_says_so_with_a_warning_mark(open_panel):
    panel = open_review(open_panel)
    panel.answer("decide", {"result": "Nothing was changed: Garage doors: changed since it was tested",
                            "status": "conflict", "ok": False})
    panel.answer("improvement", review(status="conflict", choices=[], next_pending=None))
    panel.click("#decide")
    message = panel.find(".done-msg")
    assert classes(message) == {"done-msg", "bad"}
    expect(message.locator(".mark")).to_have_text("!")
    expect(message).to_contain_text("Nothing was changed: Garage doors: changed since it was tested")


MET = [{"kind": "script", "target": "garage_doors", "what": "Garage doors", "why": "changed since it was tested"}]
RESOLVE = [{"value": "resolve", "label": "Send it back to resolve"}]
THANKS = "Thanks. The improver is revising it now."


def test_an_accept_that_meets_a_conflict_asks_at_once_to_send_it_back_to_resolve_it(open_panel):
    """SPEC P0.7: an accept that meets a conflict says so and asks the household before it goes back in at Propose:
    under what happened, Send it back to resolve, with why above; it takes no note, and sends it back."""
    panel = open_review(open_panel)
    panel.answer("decide", {"result": "Nothing was changed: Garage doors: changed since it was tested",
                            "status": "conflict", "ok": False,
                            "asks": {"about": "conflict", "improvement_id": "imp1", "conflicts": MET}})
    panel.answer("improvement", review(status="conflict", conflicts=MET, choices=RESOLVE, next_pending=None))
    panel.click("#decide")
    # Why, once: above, under "Can't apply it now:"; under what happened, only that nothing changed.
    expect(panel.find(".done-msg.bad")).to_have_text("!Nothing was changed. Send it back to resolve")
    expect(panel.find("div.conflict")).to_have_text("Can't apply it now:Garage doors: changed since it was tested")
    # Asked beside what happened, never where Accept was (a second tap there would send it back).
    expect(panel.find(".footer #decide")).to_have_count(0)
    expect(panel.find(".done-msg #resolve-link")).to_have_text("Send it back to resolve")
    expect(panel.find(".footer #note")).to_have_count(0)
    panel.answer("conflict/resolve", {"sent_back": True, "result": THANKS})
    panel.answer("improvement", review(status="conflict", conflicts=MET, choices=[], next_pending=None))
    panel.click("#resolve-link")
    assert panel.calls("conflict/resolve") == [{"type": "assist_improver/conflict/resolve", "improvement_id": "imp1"}]
    assert [c["decision"] for c in panel.calls("decide")] == ["accept"]
    expect(panel.find(".done-msg.ok")).to_contain_text(THANKS)
    expect(panel.find(".footer #decide")).to_have_count(0)
    expect(panel.find("#back-list")).to_be_visible()


def test_a_review_whose_accept_met_a_conflict_asks_to_send_it_back_and_says_why_it_couldnt(open_panel):
    panel = open_review(open_panel, status="conflict", conflicts=MET, choices=RESOLVE, next_pending=None)
    expect(panel.find("div.conflict")).to_have_text("Can't apply it now:Garage doors: changed since it was tested")
    expect(panel.find(".footer #decide")).to_have_text("Send it back to resolve")  # Opened afresh: its one choice.
    expect(panel.find(".footer #arrow")).to_have_count(0)
    panel.answer("conflict/resolve", fail("That improvement didn't meet a conflict."))
    panel.click("#decide")
    assert classes(panel.find(".done-msg")) == {"done-msg", "bad"}
    expect(panel.find(".done-msg")).to_contain_text("That improvement didn't meet a conflict.")


def test_a_decision_that_couldnt_be_sent_says_why(open_panel):
    panel = open_review(open_panel)
    panel.answer("decide", fail("Improvement imp1 was already decided"))
    panel.click("#decide")
    assert classes(panel.find(".done-msg")) == {"done-msg", "bad"}
    expect(panel.find(".done-msg")).to_contain_text("Improvement imp1 was already decided")


# Editor items: the improver never writes a configuration file itself, so the panel saves a change's scripts,
# automations and scenes through Home Assistant's own editors, as the signed-in admin (panel.py's EditorChannel; app.js
# saveItem).

CLOSE = {"alias": "Close garage", "sequence": [{"action": "cover.close_cover", "target": {"entity_id": "cover.garage"}}]}
PRESS = {"alias": "Press garage", "sequence": [{"action": "switch.turn_on", "target": {"entity_id": "switch.relay"}}]}
EDITOR = "config/script/config/"  # Home Assistant's script editor endpoint, as hass.callApi takes it.


def test_the_panel_saves_a_changes_scripts_through_home_assistants_script_editor_in_the_order_asked(open_panel):
    """Each save the improver asks for while a decision is made goes to HA's script editor (POST, or DELETE to remove
    one), one at a time, in order, each answered as done. The panel subscribes once, kept while HA makes it again."""
    panel = open_review(open_panel)
    panel.rebuild()
    panel.asks("decide", asking(("close_garage", CLOSE), ("press_garage", PRESS), ("close_garage", None)))
    panel.click("#decide")
    assert panel.api() == [("POST", EDITOR + "close_garage", CLOSE), ("POST", EDITOR + "press_garage", PRESS),
                           ("DELETE", EDITOR + "close_garage", None)]
    types = [c["type"] for c in panel.calls()]
    assert types.count("assist_improver/editors") == 1
    assert types.index("assist_improver/editors") < types.index("assist_improver/decide")
    assert [sorted(c) for c in panel.calls("editors/saved")] == [["key", "type"]] * 3  # None refused.
    assert len({c["key"] for c in panel.calls("editors/saved")}) == 3
    expect(panel.find(".footer .done-msg.ok")).to_contain_text(DECIDED["result"])


SCENE = {"name": "Reading", "entities": {"light.desk": {"state": "on", "brightness": 200}}}
DUSK = {"alias": "Porch at dusk", "triggers": [{"trigger": "time", "at": "18:00:00"}],
        "actions": [{"action": "light.turn_on", "target": {"entity_id": "light.porch"}}]}


def test_the_panel_saves_a_changes_automations_and_scenes_through_their_own_editors(open_panel):
    """A scene to HA's scene editor, an automation to its automation editor (DELETE to remove a new one again), each
    answered as done."""
    panel = open_review(open_panel)
    panel.asks("decide", asking(("scene", "reading", SCENE), ("automation", "porch_at_dusk", DUSK),
                                ("automation", "porch_at_dusk", None)))
    panel.click("#decide")
    assert panel.api() == [("POST", "config/scene/config/reading", SCENE),
                           ("POST", "config/automation/config/porch_at_dusk", DUSK),
                           ("DELETE", "config/automation/config/porch_at_dusk", None)]
    assert [sorted(c) for c in panel.calls("editors/saved")] == [["key", "type"]] * 3
    expect(panel.find(".footer .done-msg.ok")).to_contain_text(DECIDED["result"])


def test_the_panel_saves_an_item_only_at_its_own_editors_address(open_panel):
    """Each part of the address is encoded: an id with a slash or a query in it never reaches another of Home
    Assistant's endpoints (the improver refuses such an id before it asks: changes.ITEM_ID)."""
    panel = open_review(open_panel)
    panel.asks("decide", asking(("automation", "../../services/script/x?y=1", DUSK)))
    panel.click("#decide")
    assert panel.api() == [("POST", "config/automation/config/..%2F..%2Fservices%2Fscript%2Fx%3Fy%3D1", DUSK)]


def test_a_script_home_assistant_refuses_to_save_is_answered_with_its_reason_and_the_review_says_nothing_changed(
        open_panel):
    """HA's script editor validates what it saves: a refusal goes back to the improver in HA's words, the improver
    undoes what was done (the panel deleting the script it saved), and the review says nothing was changed."""
    panel = open_review(open_panel)
    reason = "Message malformed: invalid action"
    panel.refuse("POST", "press_garage", reason)
    result = (f"Nothing was changed. Press garage failed: {reason}. The steps already done were undone, so you can "
              "try again.")
    panel.answer("decide", {"result": result, "status": "awaiting_review", "ok": False})
    panel.asks("decide", asking(("close_garage", CLOSE), ("press_garage", PRESS), ("close_garage", None)))
    panel.click("#decide")
    assert [c.get("error") for c in panel.calls("editors/saved")] == [None, reason, None]
    assert [(method, path) for method, path, _ in panel.api()] == [
        ("POST", EDITOR + "close_garage"), ("POST", EDITOR + "press_garage"), ("DELETE", EDITOR + "close_garage")]
    expect(panel.find(".done-msg.bad")).to_contain_text(result)


def test_a_change_left_open_is_finished_before_the_panel_reads_anything(open_panel):
    """HA stopped in the middle of a change, or a panel went away: the next panel opened finishes it first, saving its
    scripts, before any other read."""
    panel = open_panel(held=("changeset/finish",))
    panel.asks("changeset/finish", asking(("press_garage", PRESS), ("close_garage", None)))
    panel.release("changeset/finish")
    types = [c["type"] for c in panel.calls()]
    assert types[:4] == ["assist_improver/editors", "assist_improver/changeset/finish",
                         "assist_improver/editors/saved", "assist_improver/editors/saved"], types
    assert "assist_improver/improvements" in types[4:]
    assert panel.api() == [("POST", EDITOR + "press_garage", PRESS), ("DELETE", EDITOR + "close_garage", None)]


def test_a_decision_interrupted_midway_is_finished_at_once_and_says_what_that_did(open_panel):
    """The panel making a change stopped answering (another tab took over, or it was slow): the change is finished
    from where it stands, and the review says what that did, not that it was applied, under its title (not again)."""
    panel = open_review(open_panel)
    panel.answer("decide", fail("It was interrupted before it finished.", "interrupted"))
    message = "It was interrupted. Its changes were undone, so nothing of it is in place."
    panel.answer("changeset/finish", {"result": message})
    panel.asks("changeset/finish", asking(("close_garage", None)), skip=1)  # Not the finish before the decision.
    panel.click("#decide")
    expect(panel.find(".done-msg.bad")).to_contain_text(message)
    assert panel.api() == [("DELETE", EDITOR + "close_garage", None)]
    types = [c["type"] for c in panel.calls()]
    assert types.index("assist_improver/decide") < len(types) - 1 - types[::-1].index("assist_improver/changeset/finish")
    expect(panel.find(".footer #decide")).to_have_text("Accept changes")


def test_an_interrupted_decision_with_nothing_left_to_finish_says_it_was_interrupted(open_panel):
    panel = open_review(open_panel)
    panel.answer("decide", fail("It was interrupted before it finished.", "interrupted"))
    panel.click("#decide")
    expect(panel.find(".done-msg.bad")).to_contain_text("It was interrupted before it finished.")


def test_an_interrupted_decision_is_finished_only_once_this_panels_own_save_has_landed(open_panel):
    """A phone's tab put away mid-change, then back: the improver gave up waiting, but the panel still saves what it
    was asked. It finishes the change only once that save is answered, so the improver undoes what it saved too."""
    panel = open_review(open_panel)
    panel.hold_api("POST", "close_garage")
    panel.asks("decide", asking(("close_garage", CLOSE)), wait=False)
    panel.answer("decide", fail("It was interrupted before it finished.", "interrupted"))
    panel.click("#decide")
    decided = len(panel.calls())
    assert panel.calls()[decided - 1]["type"] == "assist_improver/decide"  # Nothing after, while the save is held.
    panel.release_api("POST", "close_garage")
    expect(panel.find(".done-msg.bad")).to_contain_text("It was interrupted before it finished.")
    after = [c["type"] for c in panel.calls()[decided:]]
    assert after[:2] == ["assist_improver/editors/saved", "assist_improver/changeset/finish"], after


def test_after_home_assistant_restarts_under_the_panel_its_next_decision_subscribes_again_first(open_panel):
    """Home Assistant restarted while the panel stayed open: its subscription went with the old socket, and isn't
    renewed by itself (the improver may not be set up again when the socket is back, and a refused renewal would go
    unnoticed). The next decision subscribes again before it's sent, so its scripts are saved."""
    panel = open_review(open_panel)
    panel.reconnect()
    panel.asks("decide", asking(("close_garage", CLOSE)))
    panel.click("#decide")
    assert panel.api() == [("POST", EDITOR + "close_garage", CLOSE)]
    types = [c["type"] for c in panel.calls()]
    assert types.count("assist_improver/editors") == 2
    assert len(types) - 1 - types[::-1].index("assist_improver/editors") < types.index("assist_improver/decide")
    assert panel.page.evaluate("window.__subscribeOptions") == [{"resubscribe": False}] * 2
    expect(panel.find(".footer .done-msg.ok")).to_contain_text(DECIDED["result"])


def test_a_subscription_refused_after_a_reconnect_is_tried_again_at_the_next_load(open_panel):
    panel = open_panel(replies(improvements={"improvements": PENDING}))
    panel.reconnect(refused=1)  # The improver isn't set up again yet.
    panel.click(".item[data-id='imp1']")
    expect(panel.find(".footer #decide")).to_be_visible()  # Nothing else is held up.
    panel.click("#back")
    assert [c["type"] for c in panel.calls()].count("assist_improver/editors") == 3
    assert panel.page.evaluate("window.__subscribers.length") == 1


def test_a_rebuilt_panel_still_shows_the_recent_outcome(open_panel):
    panel = open_review(open_panel, status="applied", recent_outcome=DECIDED["result"], recent_outcome_ok=True,
                        choices=[{"value": "undo", "label": "Undo changes"}], next_pending=None)
    expect(panel.find(".done-msg.ok")).to_contain_text(DECIDED["result"])
    expect(panel.find("#undo-link")).to_be_visible()


def test_an_applied_one_opened_afresh_offers_undo_alone_without_an_arrow(open_panel):
    panel = open_review(open_panel, status="applied", choices=[{"value": "undo", "label": "Undo changes"}],
                        next_pending=None)
    expect(panel.find(".footer #decide")).to_have_text("Undo changes")
    expect(panel.find(".footer #arrow")).to_have_count(0)


REVISING = {"note": "Open both doors together"}
REVISE_CHOICES = [c for c in CHOICES if c["value"] != "revise"]  # While its round runs (improvements.choices).


@pytest.mark.parametrize("size", [PHONE, DESKTOP], ids=["phone", "desktop"])
@pytest.mark.parametrize("decision, result, after", [
    ("accept", DECIDED["result"], review(status="applied", choices=[{"value": "undo", "label": "Undo changes"}],
                                         next_pending={"id": "imp2", "title": CAR})),
    ("revise", THANKS, review(choices=REVISE_CHOICES, revising=REVISING, next_pending={"id": "imp2", "title": CAR})),
    ("decline", "Thanks. Declined, and its conversation is archived.",
     review(status="declined", choices=[], next_pending={"id": "imp2", "title": CAR})),
], ids=["accept", "revise", "decline"])
def test_a_decision_never_turns_into_another_under_the_finger(open_panel, size, decision, result, after):
    """Kai: after asking for a revision, the button that popped up underneath was Accept changes, one accidental click
    from going through. After any decision, where its button was is the way on (the next review), never a decision."""
    panel = open_review(open_panel, size=size)
    if decision != "accept":
        panel.click("#arrow")
        panel.click(f".footer .menu [data-choice='{decision}']")
    if decision == "revise":
        panel.find("#note").fill(REVISING["note"])
    box = panel.find("#decide").bounding_box()
    panel.answer("decide", {"result": result, "status": after["status"], "ok": True})
    panel.answer("improvement", after)
    panel.click("#decide")
    expect(panel.find(".done-msg")).to_contain_text(result)
    expect(panel.find(".footer #decide, .footer #arrow, .footer .split")).to_have_count(0)
    if decision == "revise":  # This version's decision, a link beside what happened.
        expect(panel.find(".done-msg #again-link")).to_have_text("Decide on this version")
    under = panel.page.evaluate("""([x, y]) => {
        const el = document.querySelector("assist-improver-panel").shadowRoot.elementFromPoint(x, y);
        return el && (el.closest("button")?.id || el.closest("button")?.className || el.className);
    }""", [box["x"] + box["width"] / 2, box["y"] + box["height"] / 2])
    assert under == "next", under  # Where the decision's button was: the way on.
    expect(panel.find("#next")).to_have_text(f"Next: {CAR} →")


def test_a_decision_that_couldnt_be_made_keeps_its_bar_with_why_and_the_note_as_typed(open_panel):
    panel = open_review(open_panel)
    panel.click("#arrow")
    panel.click(".footer .menu [data-choice='revise']")
    panel.find("#note").fill("  ")
    panel.answer("decide", fail("Say what to change in the note"))
    panel.click("#decide")
    expect(panel.find(".done-msg.bad")).to_contain_text("Say what to change in the note")
    expect(panel.find("#decide")).to_have_text("Ask for a revision")
    expect(panel.find("#note")).to_have_value("  ")


def test_one_that_couldnt_be_made_because_it_was_decided_elsewhere_puts_no_decision_where_it_was(open_panel):
    """Undone, then Decide again and Accept, while someone else accepted it meanwhile: refused, and the review read
    again is applied. Undo changes isn't put where Accept changes was."""
    panel = open_review(open_panel, recent_outcome="Undone: what was there before is back.", recent_outcome_ok=True,
                        next_pending=None)
    panel.click("#again-link")
    panel.answer("decide", fail("Improvement imp1 was already decided"))
    panel.answer("improvement", review(status="applied", choices=[{"value": "undo", "label": "Undo changes"}],
                                       next_pending=None))
    panel.click("#decide")
    expect(panel.find(".done-msg.bad")).to_contain_text("Improvement imp1 was already decided")
    expect(panel.find(".footer #decide")).to_have_count(0)
    expect(panel.find(".done-msg #undo-link")).to_have_text("Undo")


@pytest.mark.parametrize("size", [PHONE, DESKTOP], ids=["phone", "desktop"])
def test_while_its_revision_is_worked_on_a_review_says_so_and_its_decision_is_a_quiet_link_away(open_panel, size):
    """Asked for a revision, and opened again: it's being revised, for the note given. Where the decision's button
    would be is the way on; accepting or declining this version (the household's only way out while the round runs,
    SPEC P0.11) is a step of its own, behind Decide on this version, beside it."""
    panel = open_review(open_panel, size=size, choices=REVISE_CHOICES, revising=REVISING,
                        next_pending={"id": "imp2", "title": CAR})
    expect(panel.find("h1 .chip")).to_have_text("Being revised")
    expect(panel.find(".revises")).to_have_text("Being revised for your note “Open both doors together”")
    expect(panel.find(".footer #decide")).to_have_count(0)
    expect(panel.find(".footer #next")).to_have_text(f"Next: {CAR} →")
    link, way_on = panel.find(".footer #again-link"), panel.find(".footer #next").bounding_box()
    expect(link).to_have_text("Decide on this version")
    box = link.bounding_box()
    assert box["x"] + box["width"] <= way_on["x"] or box["y"] + box["height"] <= way_on["y"], (box, way_on)  # Apart.
    panel.click("#again-link")
    expect(panel.find("#decide")).to_have_text("Accept changes")
    panel.click("#arrow")
    expect(panel.find(".footer .menu [data-choice]")).to_have_text(["Accept changes", "Decline"])  # Not revise again.


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_being_revised_reads_quietly_in_light_and_dark(open_panel, dark):
    panel = open_review(open_panel, dark=dark, choices=REVISE_CHOICES, revising=REVISING)
    assert not low_contrast(panel, ["h1 .chip", ".revises"], 4.5)


def test_one_being_revised_waits_on_the_improver_and_isnt_counted_as_to_review(open_panel):
    revising = {**PENDING[0], "revising": True}
    panel = open_panel(replies(improvements={"improvements": [revising, PENDING[1]] + EARLIER}))
    expect(panel.find("h1")).to_have_text("1 improvement to review")
    rows = panel.find(".page > .card .item")
    expect(rows.nth(0).locator(".title")).to_contain_text(CAR)  # Being revised, after the one to review.
    expect(rows.nth(1).locator(".title")).to_contain_text(GARAGE)
    expect(rows.nth(0).locator(".go")).to_have_count(1)
    expect(rows.nth(1).locator(".chip")).to_have_text("Being revised")
    panel = open_panel(replies(improvements={"improvements": [revising]}))
    expect(panel.find("h1")).to_have_count(0)  # Nothing waits on the household.
    expect(panel.find(".page > .card .item .chip")).to_have_text("Being revised")


def test_one_needing_attention_keeps_its_chip_among_those_to_review(open_panel):
    attention = item("imp3", "Turn the porch light off at night", status="needs_attention")
    panel = open_panel(replies(improvements={"improvements": [PENDING[0], attention]}))
    expect(panel.find("h1")).to_have_text("2 improvements to review")
    rows = panel.find(".page > .card .item")
    expect(rows.nth(0).locator(".go")).to_have_count(1)
    expect(rows.nth(1).locator(".chip")).to_have_text("Needs attention")
    expect(rows.nth(1).locator(".go")).to_have_count(0)


def test_a_decided_improvement_without_choices_has_no_decision_bar(open_panel):
    panel = open_review(open_panel, status="declined", choices=[], next_pending=None)
    expect(panel.find("h1 .chip")).to_have_text("Declined")
    expect(panel.find(".footer")).to_have_count(0)


@pytest.mark.parametrize("status, label", [("applied", "Applied"), ("undone", "Undone"), ("declined", "Declined"),
                                           ("revised", "Replaced by a revision"), ("conflict", "Changed since tested"),
                                           ("needs_attention", "Needs attention")])
def test_each_status_reads_in_plain_words(open_panel, status, label):
    panel = open_review(open_panel, status=status, choices=[])
    expect(panel.find("h1 .chip")).to_have_text(label)


def test_the_page_stays_where_it_was_after_a_decision(open_panel):
    panel = open_review(open_panel)
    open_changes(panel)
    panel.page.evaluate("() => document.scrollingElement.scrollTo(0, 300)")
    panel.answer("improvement", review(status="applied", recent_outcome=DECIDED["result"], recent_outcome_ok=True,
                                       choices=[{"value": "undo", "label": "Undo changes"}], next_pending=None))
    panel.click("#decide")
    expect(panel.find("details[data-key='changes']")).to_have_attribute("open", "")
    assert abs(panel.page.evaluate("document.scrollingElement.scrollTop") - 300) <= 2
    panel.rebuild()  # The sidebar count changed, so Home Assistant makes the panel again: nothing moves.
    expect(panel.find("details[data-key='changes']")).to_have_attribute("open", "")
    expect(panel.find(".done-msg.ok")).to_contain_text(DECIDED["result"])
    assert abs(panel.page.evaluate("document.scrollingElement.scrollTop") - 300) <= 2


# Every fade that starts in the panel from now on, as each element names itself (its id, else its classes); the
# loading row's own delayed fade aside.
FADES = """() => { window.__fades = []; document.querySelector("assist-improver-panel").shadowRoot.addEventListener(
  "animationstart", (ev) => { if (ev.animationName === "appear" && !ev.target.matches(".loading"))
    window.__fades.push(ev.target.id || ev.target.className); }, true); }"""


# Home Assistant makes the panel again, every fade in the new one recorded from its making.
REMADE = """(dark) => { document.querySelector("assist-improver-panel").remove(); window.__fades = [];
  const el = document.createElement("assist-improver-panel");
  el.shadowRoot.addEventListener("animationstart", (ev) => { if (ev.animationName === "appear" &&
    !ev.target.matches(".loading")) window.__fades.push(ev.target.id || ev.target.className); }, true);
  document.body.appendChild(el);
  el.narrow = window.innerWidth < 870;
  el.hass = {themes: {darkMode: dark}, callWS: (msg) => window.__callWS(msg)}; }"""


def faded(panel):
    panel.page.wait_for_timeout(50)  # An animation starts on the frame after its style.
    return panel.page.evaluate("window.__fades")


def test_what_a_tap_reveals_fades_in_and_nothing_else_does(open_panel):
    """Kai: things shouldn't move randomly, jitter or fly about; things should be smooth. What a tap reveals on the page
    fades in where it stays (opacity only): a section opened from its summary, what a decision did. A page arriving
    (Next), or a panel Home Assistant makes again after a decision, shows at once."""
    panel = open_panel(replies(improvements={"improvements": PENDING}), path="/assist-improver?id=imp1")
    panel.page.evaluate(FADES)
    panel.click("details[data-key='changes'] > summary")
    assert any("files" in name for name in faded(panel)), faded(panel)
    panel.page.evaluate(FADES)
    panel.answer("improvement", review(status="applied", recent_outcome=DECIDED["result"], recent_outcome_ok=True,
                                       choices=[{"value": "undo", "label": "Undo changes"}],
                                       next_pending={"id": "imp2", "title": CAR}))
    panel.click("#decide")
    names = faded(panel)
    assert any("done-msg" in name for name in names) and "next" in names, names
    panel.page.evaluate(REMADE, panel.dark)
    panel.settle()
    expect(panel.find(".done-msg")).to_contain_text(DECIDED["result"])
    assert faded(panel) == []  # Made again: shown at once, the outcome too.
    panel.answer("improvement", review(id="imp2", title=CAR))
    panel.click("#next")
    expect(panel.find("h1")).to_contain_text(CAR)
    assert faded(panel) == []  # Another page: its sections open at once, no fade.
    panel.click("#back")
    expect(panel.find(".cs-title")).to_be_visible()
    panel.answer("improvement", review())
    panel.click(".page > .card .item")  # Home, then a review from its list: each page arrives whole.
    expect(panel.find("h1")).to_contain_text(GARAGE)
    assert faded(panel) == []


def test_back_home_an_opened_conversation_shows_at_once(open_panel):
    panel = open_panel(replies(improvements={"improvements": PENDING}))
    panel.click("[data-cs='c1']")
    expect(panel.find(".cs-read .cs-turn").first).to_be_visible()
    panel.click(".page > .card .item")
    expect(panel.find("h1")).to_contain_text(GARAGE)
    panel.page.evaluate(FADES)
    panel.click("#back")
    expect(panel.find(".cs-read .cs-turn").first).to_be_visible()  # Still open, as it was left.
    assert faded(panel) == []


def test_a_run_finishing_moves_nothing_on_screen_into_a_fade(open_panel):
    """The run's progress leaves the top of home as the run ends (the poll draws the page again): what's open below it,
    opened by a tap a while ago, doesn't fade in again as it moves up."""
    panel = open_panel(replies(improvements={"improvements": PENDING + EARLIER},
                               run=status(run("running", stage="write", progress={"total": 1, "done": 0}))))
    panel.click(".history > summary")
    panel.click("[data-cs='c3']")
    expect(panel.find(".cs-read .cs-turn").first).to_be_visible()
    panel.page.wait_for_timeout(300)  # Their fades done.
    panel.page.evaluate(FADES)
    # The run ends, and the list read again no longer has the one above the opened one: it moves up a row.
    panel.answer("conversations", listing([c for c in CHATS if c["id"] != "c1"]))
    panel.answer("run", status(run(progress={"new_improvements": 1})))
    expect(panel.find(".run")).to_have_count(0, timeout=8000)  # Refreshed every few seconds.
    expect(panel.find("[data-cs='c1']")).to_have_count(0)
    expect(panel.find(".cs-read .cs-turn").first).to_be_visible()
    assert faded(panel) == []


def test_with_reduced_motion_nothing_fades(open_panel):
    panel = open_review(open_panel)
    panel.page.emulate_media(reduced_motion="reduce")
    panel.page.evaluate(FADES)
    panel.click("details[data-key='changes'] > summary")
    panel.click("#arrow")
    assert faded(panel) == []


def test_the_next_review_opens_at_its_top(open_panel):
    panel = open_review(open_panel)
    panel.page.evaluate("() => document.scrollingElement.scrollTo(0, 300)")
    panel.answer("improvement", review(status="applied", choices=[{"value": "undo", "label": "Undo changes"}],
                                       next_pending={"id": "imp2", "title": CAR}))
    panel.click("#decide")
    assert abs(panel.page.evaluate("document.scrollingElement.scrollTop") - 300) <= 2  # This one stays put.
    panel.answer("improvement", review(id="imp2", title=CAR))
    panel.click("#next")
    expect(panel.find("h1")).to_contain_text(CAR)
    assert panel.page.evaluate("document.scrollingElement.scrollTop") == 0


def test_back_returns_to_the_list(open_panel):
    panel = open_panel(replies(improvements={"improvements": PENDING}))
    panel.click(".item[data-id='imp1']")
    panel.click("#back")
    assert panel.path() == "/assist-improver"
    expect(panel.find("h1")).to_have_text("2 improvements to review")


def test_the_words_beside_the_back_arrow_go_back_too(open_panel):
    panel = open_panel(replies(improvements={"improvements": PENDING}))
    panel.click(".item[data-id='imp1']")
    expect(panel.find("#back")).to_have_text("←Improvements")
    panel.find("#back span").last.click()  # The word, not the arrow.
    panel.settle()
    assert panel.path() == "/assist-improver"


def test_back_from_a_review_lands_at_the_top_of_home(open_panel):
    panel = open_panel(replies(improvements={"improvements": PENDING}, conversations=listing(CHATS * 8)), size=PHONE)
    panel.click(".item[data-id='imp1']")
    panel.page.evaluate("() => document.scrollingElement.scrollTo(0, 400)")
    panel.click("#back")
    expect(panel.find("h1")).to_have_text("2 improvements to review")
    assert panel.page.evaluate("document.scrollingElement.scrollTop") == 0


def test_the_browsers_back_button_returns_to_the_list(open_panel):
    panel = open_panel(replies(improvements={"improvements": PENDING}))
    panel.click(".item[data-id='imp1']")
    panel.page.go_back()
    panel.settle()
    assert panel.path() == "/assist-improver"
    expect(panel.find("h1")).to_have_text("2 improvements to review")


def test_a_review_that_cant_be_found_says_why(open_panel):
    panel = open_panel(replies(improvement=fail("No improvement nope")), path="/assist-improver?id=nope")
    expect(panel.find(".conflict")).to_have_text("No improvement nope")
    panel.click("#back")
    assert panel.path() == "/assist-improver"


def test_every_dynamic_string_on_a_review_is_escaped(open_panel):
    change = {"name": XSS, "kind": XSS, "rows": [{"sign": "+", "old": None, "new": 1, "text": XSS}], "added": 1,
              "removed": 0}
    panel = open_review(open_panel, title=XSS, status=XSS, benefit=XSS, safety=[XSS],
                        changes=[change], cause=XSS, tests=[{"summary": XSS, "improved": True, "result": XSS}],
                        conflicts=[{"what": XSS, "why": XSS}], revises={"title": XSS, "note": XSS},
                        choices=[{"value": XSS, "label": XSS}, CHOICES[0]])
    open_all(panel)
    for css in ("h1", ".benefit", ".safe li", ".tests li", ".conflict", ".revises", ".file .name", ".file .kind",
                "td.text", ".details-body p", "#decide"):
        expect(panel.find(css).first).to_contain_text(XSS)
    panel.click("#arrow")
    expect(panel.find(".menu [data-choice]")).to_have_text([XSS, "Accept changes"])
    assert_escaped(panel)
    panel.answer("decide", {"result": XSS, "status": "applied", "ok": False})
    panel.answer("improvement", review(title=XSS, choices=[], next_pending={"id": XSS, "title": XSS}))
    panel.click(".menu [data-choice]")
    panel.click("#decide")
    assert panel.calls("decide")[-1]["decision"] == XSS
    panel = open_review(open_panel, revising={"note": XSS})  # Being revised: the note it's revised for.
    expect(panel.find(".revises")).to_contain_text(XSS)
    assert_escaped(panel)
    panel = open_review(open_panel, title=XSS, choices=[{"value": XSS, "label": XSS}])
    panel.answer("decide", {"result": XSS, "status": "applied", "ok": False})
    panel.answer("improvement", review(title=XSS, choices=[], next_pending={"id": XSS, "title": XSS}))
    panel.click("#decide")
    expect(panel.find(".done-msg")).to_contain_text(XSS)
    expect(panel.find("#next")).to_contain_text(XSS)
    assert_escaped(panel)
    panel.click("#next")
    assert panel.calls("improvement")[-1]["improvement_id"] == XSS


# Settings: the pipeline as a track, each AI step's model a click away.

def open_settings(open_panel, size=DESKTOP, dark=True, **over):
    over.setdefault("run", status(run(progress={"new_improvements": 2})))
    return open_panel(replies(**over), path="/assist-improver?view=settings", size=size, dark=dark)


STEP_TITLES = ["Your Assist", "Triage", "Analyze", "Propose", "Review", "Simulate", "Write up", "Your review",
               "Your household"]


@pytest.mark.parametrize("size, vertical", [(PHONE, True), (DESKTOP, False)], ids=["phone", "desktop"])
def test_the_settings_draw_every_step_of_the_pipeline_down_a_phone_and_across_a_desktop(open_panel, size, vertical):
    panel = open_settings(open_panel, size=size)
    expect(panel.find(".stn .s-title")).to_have_text(STEP_TITLES)
    assert classes(panel.find(".track")) == ({"track", "vertical"} if vertical else {"track"})
    expect(panel.find(".stream-label")).to_have_text("Revisions")
    expect(panel.find(".stn[data-step='you'] button")).to_have_count(0)  # Your review is where you review, not a link.


@pytest.mark.parametrize("size", [PHONE, TABLET, DESKTOP], ids=["phone", "tablet", "desktop"])
def test_step_cards_never_overlap(open_panel, size):
    panel = open_settings(open_panel, size=size, run=status(run(), admits=3, held={"analyze": 12}, review=2))
    boxes = [b.bounding_box() for b in panel.find(".stn").all()]
    for n, a in enumerate(boxes):
        for b in boxes[n + 1:]:
            apart = (a["x"] + a["width"] <= b["x"] + 1 or b["x"] + b["width"] <= a["x"] + 1
                     or a["y"] + a["height"] <= b["y"] + 1 or b["y"] + b["height"] <= a["y"] + 1)
            assert apart, (a, b)


def test_across_a_desktop_each_card_fits_its_title_and_model(open_panel):
    panel = open_settings(open_panel, size=DESKTOP)
    cut = panel.page.evaluate("""() => [...document.querySelector('assist-improver-panel').shadowRoot
        .querySelectorAll('.stn .s-title, .stn .chip-model')].filter((el) => el.scrollWidth > el.clientWidth + 1)
        .map((el) => el.textContent)""")
    assert not cut, f"cut off: {cut}"


@pytest.mark.parametrize("size", [PHONE, SMALL_PHONE], ids=["phone", "small-phone"])
def test_down_a_phone_each_card_shows_its_title_in_full_with_its_controls_beside_or_below_it(open_panel, size):
    """DETAILS P14.9: cards are sized to their content and never cut off, at any window width. A card whose controls
    don't fit beside its title takes them on a line below and grows to hold them (Your household, with its lines and
    what's been applied); the cards stay apart."""
    panel = open_settings(open_panel, size=size, household=household(everyone=[f"Line {n}" for n in range(12)]),
                          run=status(run(), applied=3))
    expect(panel.find(".stn[data-step='household'] .chip-model")).to_have_text("12 lines")
    shown = panel.page.evaluate("""() => [...document.querySelector('assist-improver-panel').shadowRoot
        .querySelectorAll('.track.vertical > .stn')].map((card) => {
          const box = card.getBoundingClientRect();
          const out = [...card.querySelectorAll('.s-head, .chip-model, .chip-row, .gate')].filter((el) => {
            const r = el.getBoundingClientRect();
            return r.left < box.left || r.right > box.right || r.top < box.top || r.bottom > box.bottom; });
          const title = card.querySelector('.s-title');
          return {step: card.dataset.step, cut: title.scrollWidth > title.clientWidth + 1, out: out.length,
                  box: [box.top, box.bottom]}; })""")
    assert len(shown) == len(STEP_TITLES)
    assert not [s["step"] for s in shown if s["cut"]], f"titles cut off: {shown}"
    assert not [s["step"] for s in shown if s["out"]], f"controls outside their card: {shown}"
    for above, below in zip(shown, shown[1:]):  # Down the track, each card clear of the next.
        assert above["box"][1] <= below["box"][0] + 0.5, (above, below)


def test_each_ai_step_names_its_model_without_a_bubble_when_all_is_well(open_panel):
    panel = open_settings(open_panel)
    for step, label in [("triage", "Haiku 4.5"), ("analyze", "Opus 5.5"), ("propose", "Opus 5.5"),
                        ("review", "Opus 5.5"), ("write", "Haiku 4.5")]:
        chip = panel.find(f".stn[data-step='{step}'] .chip-model")
        expect(chip).to_have_text(f"{label} ▾")
        expect(chip).not_to_have_attribute("data-tip", "")
        assert chip.get_attribute("data-tip") is None  # No tooltips naming AI Tasks.
        assert "warn" not in classes(chip)
    expect(panel.find(".stn[data-step='analyze'] .chip-model")).to_have_attribute("aria-label", "Analyze: Opus 5.5")


@pytest.mark.parametrize("problem, entity, text, tip", [
    ("missing", "ai_task.gone", "⚠ Not available ▾", "This AI model is gone from Home Assistant. Choose another."),
    ("unavailable", OPUS["entity_id"], "⚠ Opus 5.5 ▾", "This AI model is unavailable right now."),
    ("none", None, "⚠ Choose… ▾", "This step needs an AI model."),
])
def test_a_step_whose_model_cant_run_shows_a_warning_mark_and_why(open_panel, problem, entity, text, tip):
    panel = open_settings(open_panel, settings=settings(roles={"analyzer": role(entity, "large", problem=problem)}))
    chip = panel.find(".stn[data-step='analyze'] .chip-model")
    expect(chip).to_have_text(text)
    expect(chip).to_have_attribute("data-tip", tip)
    assert "warn" in classes(chip)


def test_a_weak_model_on_a_step_that_needs_judgement_is_marked_not_recommended(open_panel):
    panel = open_settings(open_panel, settings=settings(roles={"analyzer": role(HAIKU["entity_id"], "large")}))
    chip = panel.find(".stn[data-step='analyze'] .chip-model")
    expect(chip).to_have_text("⚠ Haiku 4.5 ▾")
    expect(chip).to_have_attribute("data-tip", "Not recommended for this step")
    expect(panel.find(".stn[data-step='triage'] .chip-model")).to_have_text("Haiku 4.5 ▾")  # Small is right there.


def test_triage_without_a_model_passes_conversations_straight_through(open_panel):
    panel = open_settings(open_panel, settings=settings(roles={"triage": role(None, "small", optional=True)}))
    chip = panel.find(".stn[data-step='triage'] .chip-model")
    expect(chip).to_have_text("Pass through ▾")
    expect(chip).to_have_attribute("data-tip", "No triage model: conversations pass straight on to analysis.")
    assert "off" in classes(chip)


def test_a_models_menu_ranks_by_fit_and_marks_recommended_not_recommended_and_fit_unknown(open_panel):
    panel = open_settings(open_panel)
    panel.click(".stn[data-step='analyze'] .chip-model")
    pop = panel.find("#pop")
    expect(pop.locator(".pop-head").first).to_have_text("Analyze uses")
    options = pop.locator(".opt[data-pick]")
    expect(options.locator(".opt-label")).to_have_text(["Opus 5.5", "Local LLM", "Haiku 4.5"])
    expect(options.locator(".tag")).to_have_text(["Recommended", "Fit unknown", "Not recommended"])
    expect(options.nth(0).locator(".check")).to_have_text("✓")
    expect(options.nth(1).locator(".check")).to_have_text("")
    expect(options.nth(1)).to_have_attribute(
        "data-tip", "Assist Improver can't tell which model this is. A strong model, such as Claude Opus 5.5, works best.")
    expect(options.nth(2)).to_have_attribute("data-tip", HAIKU["warnings"][0])
    expect(options.nth(0).locator(".opt-sub")).to_have_text("Claude AI Task")  # Its own name, when the label differs.
    expect(options.nth(1).locator(".opt-sub")).to_have_count(0)
    expect(pop.locator(".pop-head").first).to_have_attribute(
        "data-tip", "Reads each conversation to identify your intents, and where Assist could've done better. "
                    "Needs judgement: a strong model works best.")


def test_a_quick_steps_menu_marks_small_models_a_good_fit_and_offers_pass_through(open_panel):
    panel = open_settings(open_panel)
    panel.click(".stn[data-step='triage'] .chip-model")
    options = panel.find("#pop .opt[data-pick]")
    expect(options.locator(".opt-label")).to_have_text(["Haiku 4.5", "Opus 5.5", "Local LLM", "Pass through"])
    expect(panel.find("#pop .tag")).to_have_text(["Good fit"])
    expect(options.last).to_have_attribute("data-tip", "No triage model: conversations pass straight on to analysis.")


@pytest.mark.parametrize("step, role_name, pick, entity", [
    ("analyze", "analyzer", "Local LLM", LOCAL["entity_id"]), ("triage", "triage", "Pass through", None)])
def test_picking_a_model_saves_it_right_away(open_panel, step, role_name, pick, entity):
    panel = open_settings(open_panel)
    panel.click(f".stn[data-step='{step}'] .chip-model")
    panel.find("#pop .opt[data-pick]").filter(has_text=pick).click()
    panel.settle()
    assert panel.calls("settings/set") == [{"type": "assist_improver/settings/set", "role": role_name, "entity_id": entity}]
    expect(panel.find("#pop")).to_have_count(0)


def test_a_setting_that_cant_be_saved_says_why(open_panel):
    panel = open_settings(open_panel, settings__set=fail("ai_task.local isn't an AI Task in this Home Assistant."))
    panel.click(".stn[data-step='analyze'] .chip-model")
    panel.find("#pop .opt[data-pick]").filter(has_text="Local LLM").click()
    panel.settle()
    assert panel.dialogs == ["ai_task.local isn't an AI Task in this Home Assistant."]
    expect(panel.find(".stn[data-step='analyze'] .chip-model")).to_have_text("Opus 5.5 ▾")


@pytest.mark.parametrize("recommended, offered", [(False, True), (True, False), (None, False)],
                         ids=["differs", "already", "cant-be-offered"])
def test_use_recommended_setup_is_offered_in_every_models_menu_only_when_the_setup_differs(open_panel, recommended,
                                                                                         offered):
    panel = open_settings(open_panel, settings=settings(recommended=recommended))
    for step in ("analyze", "write"):
        panel.click(f".stn[data-step='{step}'] .chip-model")
        option = panel.find("#use-recommended")
        if offered:
            expect(option).to_have_text("✦ Use recommended setup")
            expect(option).to_have_attribute(
                "data-tip", "Claude Opus 5.5 for the steps that need judgement, a small model for Triage and Write up. "
                            "AI Tasks that fit are reused; the rest are added to your AI provider.")
        else:
            expect(option).to_have_count(0)
        panel.page.keyboard.press("Escape")
        panel.settle()


def test_use_recommended_setup_sets_everything_up_in_one_click(open_panel):
    panel = open_settings(open_panel, settings=settings(recommended=False,
                                                        roles={"analyzer": role(HAIKU["entity_id"], "large")}),
                          settings__recommended={**settings(recommended=True), "notes": []})
    panel.click(".stn[data-step='analyze'] .chip-model")
    panel.hold("settings/recommended")
    panel.find("#use-recommended").click()
    expect(panel.find("#use-recommended")).to_have_text("Setting up…")
    panel.release("settings/recommended")
    expect(panel.find("#pop")).to_have_count(0)
    expect(panel.find(".stn[data-step='analyze'] .chip-model")).to_have_text("Opus 5.5 ▾")
    panel.click(".stn[data-step='analyze'] .chip-model")
    expect(panel.find("#use-recommended")).to_have_count(0)


@pytest.mark.parametrize("answer, shown", [
    ({**settings(recommended=False), "notes": ["A small model couldn't be added: The provider doesn't offer a small model."]},
     "A small model couldn't be added: The provider doesn't offer a small model."),
    (fail("Add an AI provider, such as Anthropic, in Settings → Devices & services first."),
     "Add an AI provider, such as Anthropic, in Settings → Devices & services first."),
], ids=["partly", "not-at-all"])
def test_use_recommended_setup_says_what_it_couldnt_do(open_panel, answer, shown):
    panel = open_settings(open_panel, settings=settings(recommended=False), settings__recommended=answer)
    panel.click(".stn[data-step='analyze'] .chip-model")
    panel.click("#use-recommended")
    expect(panel.find("#pop .error-text")).to_have_text(shown)
    expect(panel.find("#use-recommended")).to_have_text("✦ Use recommended setup")  # Ready to try again.


def test_a_new_ai_task_needs_an_ai_provider_first(open_panel):
    panel = open_settings(open_panel)
    panel.click(".stn[data-step='analyze'] .chip-model")
    panel.click("#new-task")
    expect(panel.find("#pop")).to_contain_text("New AI Task for Analyze")
    expect(panel.find("#pop .pop-lead")).to_have_text(
        "Add an AI provider (such as Anthropic or OpenRouter) in Settings → Devices & services first.")


def test_a_new_ai_task_suggests_the_model_for_the_step_and_is_used_right_away(open_panel):
    models = {"models": [{"value": "claude-haiku-4-5", "label": "Claude Haiku 4.5"},
                         {"value": "claude-opus-5-5", "label": "Claude Opus 5.5"}],
              "default": "claude-haiku-4-5", "named": True,
              "suggested": {"large": "claude-opus-5-5", "small": "claude-haiku-4-5"}}
    panel = open_settings(open_panel, settings=settings(providers=[ANTHROPIC]), settings__models=models,
                          settings__create=settings(providers=[ANTHROPIC]))
    panel.click(".stn[data-step='analyze'] .chip-model")
    panel.click("#new-task")
    assert panel.calls("settings/models") == [{"type": "assist_improver/settings/models", "entry_id": "p1"}]
    expect(panel.find("#c-model")).to_have_value("claude-opus-5-5")
    expect(panel.find("#c-name")).to_have_value("Assist Improver (Claude Opus 5.5)")
    panel.click("#c-create")
    assert panel.calls("settings/create") == [{"type": "assist_improver/settings/create", "entry_id": "p1",
                                               "model": "claude-opus-5-5", "role": "analyzer",
                                               "name": "Assist Improver (Claude Opus 5.5)"}]
    expect(panel.find("#pop")).to_have_count(0)


def test_a_new_ai_task_that_cant_be_added_says_why(open_panel):
    models = {"models": [], "default": None, "named": False, "suggested": {"large": None, "small": None}}
    panel = open_settings(open_panel, settings=settings(providers=[ANTHROPIC]), settings__models=models,
                          settings__create=fail("The provider refused: invalid model"))
    panel.click(".stn[data-step='analyze'] .chip-model")
    panel.click("#new-task")
    panel.click("#c-create")
    expect(panel.find("#pop .error-text")).to_have_text("Choose a model.")
    assert not panel.calls("settings/create")
    panel.find("#c-model").fill("claude-opus-5-5")
    panel.find("#c-model").press("Tab")
    panel.settle()
    panel.click("#c-create")
    assert panel.calls("settings/create")[-1]["model"] == "claude-opus-5-5"
    expect(panel.find("#pop .error-text")).to_have_text("The provider refused: invalid model")


def test_typing_a_model_id_then_add_and_use_adds_it_in_one_click(open_panel):
    models = {"models": [], "default": None, "named": False, "suggested": {"large": None, "small": None}}
    panel = open_settings(open_panel, settings=settings(providers=[ANTHROPIC]), settings__models=models,
                          settings__create=settings(providers=[ANTHROPIC]))
    panel.click(".stn[data-step='analyze'] .chip-model")
    panel.click("#new-task")
    panel.find("#c-model").fill("claude-opus-5-5")
    panel.click("#c-create")
    assert [c["model"] for c in panel.calls("settings/create")] == ["claude-opus-5-5"]


@pytest.mark.parametrize("close", ["Escape", "backdrop"])
def test_escape_or_a_click_outside_closes_a_menu_without_changing_anything(open_panel, close):
    panel = open_settings(open_panel)
    panel.click(".stn[data-step='analyze'] .chip-model")
    if close == "Escape":
        panel.page.keyboard.press("Escape")
    else:
        panel.find("#backdrop").click(position={"x": 5, "y": 5})
    panel.settle()
    expect(panel.find("#pop")).to_have_count(0)
    assert not panel.calls("settings/set")


@pytest.mark.parametrize("size, sheet", [(PHONE, True), (DESKTOP, False)], ids=["phone", "desktop"])
def test_on_a_phone_a_menu_rises_from_the_bottom(open_panel, size, sheet):
    panel = open_settings(open_panel, size=size)
    panel.click(".stn[data-step='analyze'] .chip-model")
    pop = panel.find("#pop")
    assert ("sheet" in classes(pop)) == sheet
    box = pop.bounding_box()
    if sheet:
        assert abs(box["y"] + box["height"] - size["height"]) <= 1 and box["width"] == size["width"]
    else:  # Under the chip that opened it.
        chip = panel.find(".stn[data-step='analyze'] .chip-model").bounding_box()
        assert box["y"] >= chip["y"] + chip["height"]


def test_tooltips_explain_on_hover_and_on_focus(open_panel):
    panel = open_settings(open_panel)
    info = panel.find(".stn[data-step='test'] .info-tip")
    tip = panel.find("#tip")
    expect(tip).to_be_hidden()
    info.hover()
    expect(tip).to_be_visible()
    expect(tip).to_have_text("Replay your conversations with proposed fixes in a simulated copy of your home. Nothing in "
                             "your home moves.")
    panel.page.mouse.move(2, 2)
    expect(tip).to_be_hidden()
    panel.find(".stn[data-step='analyze'] .info-tip").focus()
    expect(tip).to_have_text("Reads each conversation to identify your intents, and where Assist could've done better.")


@pytest.mark.parametrize("size", [PHONE, DESKTOP], ids=["phone", "desktop"])
def test_a_tooltip_stays_on_screen(open_panel, size):
    panel = open_settings(open_panel, size=size)
    for css in (".stn[data-step='requests'] .info-tip", ".stn[data-step='write'] .info-tip",
                ".stn[data-step='household'] .info-tip"):
        panel.find(css).first.hover()
        box = panel.find("#tip").bounding_box()
        assert box["x"] >= 8 - 0.5 and box["x"] + box["width"] <= size["width"] - 8 + 0.5, (css, box)
        assert box["y"] >= 0 and box["y"] + box["height"] <= size["height"], (css, box)


def test_a_tall_picker_on_a_tablet_in_the_app_opens_clear_of_the_status_bar(open_panel):
    """Too tall to fit under or over its chip, a picker opens as high as it can: below the status bar's strip."""
    tasks = [OPUS, HAIKU, LOCAL] + [{**LOCAL, "entity_id": f"ai_task.extra_{n}", "label": f"Extra model {n}",
                                     "title": f"Extra AI Task {n}"} for n in range(14)]
    panel = open_settings(open_panel, size={"width": 900, "height": 700}, settings=settings(tasks=tasks))
    panel.page.evaluate(SAFE, {**INSETS["portrait"], "top": 24})
    chip = panel.find("[data-role='analyzer']")
    chip.evaluate("(el) => document.scrollingElement.scrollBy(0, el.getBoundingClientRect().top - 330)")
    panel.settle()
    chip.click()
    pop = panel.find(".pop").bounding_box()
    expect(panel.find(".pop.sheet")).to_have_count(0)
    assert pop["y"] == 24 + 8 and pop["y"] + pop["height"] <= 700, pop


def test_a_tooltip_beside_a_menu_never_covers_its_options(open_panel):
    panel = open_settings(open_panel)
    panel.click(".stn[data-step='analyze'] .chip-model")
    panel.find("#pop .opt[data-pick]").filter(has_text="Local LLM").hover()
    tip, pop = panel.find("#tip").bounding_box(), panel.find("#pop").bounding_box()
    assert tip["x"] >= pop["x"] + pop["width"] or tip["x"] + tip["width"] <= pop["x"], (tip, pop)


def test_a_click_puts_the_tooltip_aside(open_panel):
    panel = open_settings(open_panel)
    panel.find("[data-gate='analyze']").hover()
    expect(panel.find("#tip")).to_be_visible()
    panel.page.mouse.down()
    expect(panel.find("#tip")).to_be_hidden()
    panel.page.mouse.up()


def test_a_tooltip_shows_text_never_markup(open_panel):
    panel = open_settings(open_panel, settings=settings(roles={"analyzer": role(HAIKU["entity_id"], "large")},
                                                         tasks=[OPUS, {**HAIKU, "warnings": [XSS]}, LOCAL]))
    panel.click(".stn[data-step='analyze'] .chip-model")
    panel.find("#pop .opt[data-pick]").filter(has_text="Haiku 4.5").hover()
    expect(panel.find("#tip")).to_have_text(XSS)
    assert panel.count("#tip *") == 0
    assert_escaped(panel)


def test_blocking_a_step_holds_what_reaches_it(open_panel):
    panel = open_settings(open_panel, run=status(run(), held={"analyze": 2}),
                          settings__set=settings(paused=["analyze"]))
    gate = panel.find("[data-gate='analyze']")
    expect(gate).to_have_attribute("aria-label", "Block Analyze")
    expect(gate).to_have_attribute("aria-pressed", "false")
    expect(gate).to_have_attribute("data-tip", "Stop this stage.")
    expect(panel.find(".stn[data-step='analyze'] .badge")).to_have_attribute("data-tip", "2 held")
    runs = len(panel.calls("run"))
    panel.answer("run", status(run(), held={"analyze": 2}, paused=["analyze"]))
    panel.click("[data-gate='analyze']")
    assert panel.calls("settings/set") == [{"type": "assist_improver/settings/set", "stage": "analyze", "pause": True}]
    assert len(panel.calls("run")) == runs + 1  # What's held there is read again: held while blocked now.
    expect(gate).to_have_attribute("aria-label", "Unblock Analyze")
    expect(gate).to_have_attribute("aria-pressed", "true")
    expect(gate).to_have_attribute("data-tip", "Resume this stage.")
    assert "paused" in classes(gate)
    badge = panel.find(".stn[data-step='analyze'] .badge")
    expect(badge).to_have_attribute("data-tip", "2 held while blocked")
    assert "held" in classes(badge)


def test_unblocking_a_step_lets_it_run_again(open_panel):
    panel = open_settings(open_panel, settings=settings(paused=["analyze"]), settings__set=settings())
    panel.click("[data-gate='analyze']")
    assert panel.calls("settings/set")[-1] == {"type": "assist_improver/settings/set", "stage": "analyze", "pause": False}
    expect(panel.find("[data-gate='analyze']")).to_have_attribute("aria-pressed", "false")


def test_only_the_gated_steps_can_be_blocked(open_panel):
    panel = open_settings(open_panel)
    gates = panel.find("[data-gate]")
    expect(gates).to_have_count(4)  # Simulate is Propose's stamp: no gate of its own.
    assert [g.get_attribute("data-gate") for g in gates.all()] == ["triage", "analyze", "propose", "write"]


def test_each_step_shows_what_waits_at_it(open_panel):
    """How many conversations are at each step, from their flow records (P14.9.3): new at Your Assist, held at a step
    between, up for review; and what's applied in Your household."""
    panel = open_settings(open_panel, run=status(run(), admits=3, held={"analyze": 2}, review=1, applied=4))
    for step, n, tip, kind in [("requests", "3", "3 new: the next run takes them in", "in"),
                               ("analyze", "2", "2 held", ""), ("you", "1", "1 to review", "out")]:
        badge = panel.find(f".stn[data-step='{step}'] .badge")
        expect(badge).to_have_text(n)
        expect(badge).to_have_attribute("data-tip", tip)
        assert classes(badge) == {"badge"} | ({kind} if kind else set())
    applied = panel.find(".stn[data-step='household'] .done-count")
    expect(applied).to_have_text("4")
    expect(applied).to_have_attribute("data-tip", "4 applied")
    for step in ("triage", "propose", "review", "test", "write"):
        expect(panel.find(f".stn[data-step='{step}'] .badge")).to_have_count(0)


@pytest.mark.parametrize("stage, at_work, label", [
    ("triage", ["triage"], "Triage"), ("analyze", ["analyze"], "Analyze"),
    ("propose", ["propose", "review", "test"], "Propose"),  # Its reviewer and its Simulate stamp with it (P0.4).
    ("write", ["write"], "Write up")])
def test_the_step_at_work_is_marked_while_a_run_is_in_progress(open_panel, stage, at_work, label):
    panel = open_settings(open_panel, run=status(run("running", stage=stage)))
    marked = [el.get_attribute("data-step") for el in panel.find(".stn.now").all()]
    assert marked == at_work
    cancel = panel.find("#cancel-run")
    expect(cancel).to_have_text("Cancel run")
    expect(cancel).to_have_attribute("data-tip", f"Improving your assistant · {label}")
    expect(panel.find(".track .flowing")).to_have_count(1)
    expect(panel.find("#run-now")).to_have_count(0)


def test_a_run_refreshing_never_loses_what_is_being_typed(open_panel):
    panel = open_settings(open_panel, run=status(run("running", stage="analyze")),
                          household=household(everyone=LINES))
    panel.click("[data-knowledge]")
    panel.click("[data-kn-add='everyone']")
    panel.find("#kn-text").fill("Bins go out on Tuesday night.")
    runs = len(panel.calls("run"))
    # The run's card refreshes every 3 s, but not while a menu is open: two refreshes' time, so a slow machine's late
    # timer still falls inside.
    panel.page.wait_for_timeout(6500)
    expect(panel.find("#kn-text")).to_have_value("Bins go out on Tuesday night.")
    expect(panel.find("#kn-text")).to_be_focused()
    assert len(panel.calls("run")) == runs


STEP_TIPS = {
    "requests": "Your conversations with Assist, recorded as you use it. They wait here until the next run takes them "
                "in. The assistant shown is the one Assist uses by default.",
    "triage": "Recommended to use a small model to reduce downstream costs by filtering out conversations that clearly "
              "need no improvement.",
    "analyze": "Reads each conversation to identify your intents, and where Assist could've done better.",
    "propose": "Propose changes to make your Assist experience smoother.",
    "review": "Align proposals with the broader view of how Assist is used in your household.",
    "test": "Replay your conversations with proposed fixes in a simulated copy of your home. Nothing in your home moves.",
    "write": "Packages technical improvements so they're easy for you to review.",
    "you": "Accept, decline, or ask for a revision.",
    "household": "Your house, made easier.",
}


def test_every_step_explains_itself_in_a_bubble_not_on_the_page(open_panel):
    panel = open_settings(open_panel)
    for step, tip in STEP_TIPS.items():
        info = panel.find(f".stn[data-step='{step}'] .info-tip")
        expect(info).to_have_attribute("data-tip", tip)
        expect(info).to_have_attribute("tabindex", "0")  # Reachable from the keyboard too.
        expect(panel.find(f".stn[data-step='{step}']")).not_to_contain_text(tip)


def test_cancel_run_in_the_settings_cancels(open_panel):
    panel = open_settings(open_panel, run=status(run("running", stage="analyze")))
    panel.answer("run", status(run("cancelled")))
    panel.find("#cancel-run").click()
    expect(panel.find("#run-now")).to_be_visible(timeout=1500)  # Straight away, not at the next refresh (3 s).
    assert panel.calls("run/cancel")


def test_one_click_on_cancel_run_cancels_once(open_panel):
    panel = open_settings(open_panel, run=status(run("running", stage="analyze")))
    panel.answer("run", status(run("cancelled")))
    panel.find("#cancel-run").click()
    expect(panel.find("#run-now")).to_be_visible()
    panel.page.wait_for_timeout(1000)
    panel.settle()
    assert len(panel.calls("run/cancel")) == 1


def test_run_now_in_the_settings_has_a_play_icon_names_the_last_run_and_asks_first(open_panel):
    panel = open_settings(open_panel, run=status(run(progress={"new_improvements": 2}), admits=3))
    button = panel.find("#run-now")
    expect(button).to_have_text("Run on 3 new conversations")  # A manual full run: the new ones it admits.
    expect(button.locator("svg path")).to_have_attribute("d", PLAY)
    expect(button).to_have_attribute("data-tip", "Last run 5 min ago · 2 new improvements")
    panel.click("#run-now")
    expect(panel.find(".modal-head b")).to_have_text("Run on 3 new conversations?")
    expect(panel.find(".modal .c-what")).to_have_count(0)
    assert not panel.calls("run/start")


@pytest.mark.parametrize("waiting, paused, button, title", [
    ({}, (), "Run now", "Run Assist Improver?"),
    ({"held": {"write": 2}}, (), "Run what's waiting", "Run Assist Improver?"),
    ({"held": {"propose": 1}}, (), "Run what's waiting", "Run Assist Improver?"),
    ({"held": {"write": 2}}, ("write",), "Run now", "Run Assist Improver?"),
    ({"admits": 1}, (), "Run on 1 new conversation", "Run on 1 new conversation?"),
    ({"admits": 1, "held": {"write": 2}}, (), "Run on 1 new conversation", "Run on 1 new conversation?"),
    ({"admits": 3, "held": {"triage": 1, "analyze": 2}}, (), "Run on 3 new conversations", "Run on 3 new conversations?"),
    ({"admits": 3, "held": {"analyze": 2}}, ("analyze",), "Run on 3 new conversations", "Run on 3 new conversations?"),
], ids=["none", "held-at-write-up", "held-at-propose", "only-at-a-blocked-step", "one", "one-and-held",
        "new-and-held", "blocked"])
def test_run_now_says_how_many_conversations_it_runs_on(open_panel, waiting, paused, button, title):
    """The new ones a manual full run admits (flow.Flow.admitted), less those that would stop at a blocked step; with
    none, what's waiting when it releases held ones; else Run now. As the home page's Run says."""
    panel = open_settings(open_panel, run=status(run(), **waiting, paused=paused),
                          settings=settings(paused=paused))
    expect(panel.find("#run-now")).to_have_text(button)
    panel.click("#run-now")
    expect(panel.find(".modal[role='dialog']")).to_have_attribute("aria-label", title)


def test_with_no_new_conversation_but_held_work_settings_runs_what_s_waiting(open_panel):
    """Kai, 2026-10-26: "Run what's waiting" when that's what it will do; its confirmation's title matches, as the run
    takes no new conversation (DETAILS P14.6.4, P17.17.2, P17.17.4)."""
    panel = open_settings(open_panel, run=status(run(), held={"analyze": 1}))
    expect(panel.find("#run-now")).to_have_text("Run what's waiting")
    panel.click("#run-now")
    expect(panel.find(".modal-head b")).to_have_text("Run Assist Improver?")
    expect(panel.find(".modal .c-what")).to_have_text("1 conversation to analyze is waiting.")


def test_on_a_phone_run_now_has_a_row_of_its_own_above_the_track(open_panel):
    panel = open_settings(open_panel, size=PHONE, run=status(run(), admits=128))
    button, first = panel.find("#run-now").bounding_box(), panel.find(".stn").first.bounding_box()
    assert button["y"] + button["height"] <= first["y"]  # Above the track, not squeezing it.
    assert button["height"] < 44  # "Run on 128 new conversations" on one line.
    panel.click("#run-menu")
    assert_no_horizontal_scroll(panel)


def test_the_run_menu_sets_when_it_runs_on_its_own(open_panel):
    panel = open_settings(open_panel, settings__set=settings(schedule="daily"))
    arrow = panel.find("#run-menu")
    expect(arrow).to_have_attribute("data-tip", "Only when you run it")  # Off by default.
    panel.click("#run-menu")
    when = panel.find(".menu [data-schedule]")
    expect(when.locator(".lbl")).to_have_text(["Only when you run it", "Every day at 3:00, on new conversations",
                                               "Every Sunday at 3:00, on new conversations"])
    expect(panel.find(".menu [data-schedule].on .lbl")).to_have_text("Only when you run it")
    panel.click(".menu [data-schedule='daily']")
    assert panel.calls("settings/set") == [{"type": "assist_improver/settings/set", "schedule": "daily"}]
    expect(panel.find(".menu")).to_have_count(0)
    expect(arrow).to_have_attribute("data-tip", "Every day at 3:00, on new conversations")
    expect(arrow.locator("svg path")).to_have_attribute("d", CLOCK)


@pytest.mark.parametrize("close", ["Escape", "backdrop"])
def test_the_run_menu_closes_without_changing_anything(open_panel, close):
    panel = open_settings(open_panel)
    panel.click("#run-menu")
    if close == "Escape":
        panel.page.keyboard.press("Escape")
    else:
        panel.find("#menu-backdrop").click(position={"x": 5, "y": 500})
    panel.settle()
    expect(panel.find(".menu")).to_have_count(0)
    assert not panel.calls("settings/set")


@pytest.mark.parametrize("only, held, title, sentence", [
    ("analyze", {"analyze": 2}, "Run Analyze?", "Analyzes 2 conversations."),
    ("analyze", {}, "Run Analyze?", "Nothing is held at Analyze."),
    ("triage", {"triage": 1}, "Run Triage?", "Triages 1 conversation held there."),
    ("triage", {}, "Run Triage?", "Nothing is held at Triage."),  # The new ones wait for a full run.
    ("propose", {"propose": 1}, "Run Propose?", "Proposes fixes for 1 conversation."),
    ("propose", {}, "Run Propose?", "Nothing is held at Propose."),
    ("write", {"write": 2}, "Run Write up?", "Writes up improvements for 2 conversations."),
    ("write", {}, "Run Write up?", "Nothing is held at Write up."),
])
def test_one_step_runs_on_its_own_after_confirming(open_panel, only, held, title, sentence):
    """Run one step releases only what's held at that step, and takes in nothing new (P0.7)."""
    panel = open_settings(open_panel, run=status(run(), admits=2, held=held))
    panel.click("#run-menu")
    expect(panel.find(".menu .group")).to_have_text("Run one step")
    expect(panel.find(".menu [data-only] .lbl")).to_have_text(["Triage", "Analyze", "Propose", "Write up"])
    panel.click(f".menu [data-only='{only}']")
    expect(panel.find(".modal")).to_have_attribute("aria-label", title)
    expect(panel.find(".modal .c-what")).to_have_text(sentence)
    expect(panel.find(".modal .cost")).to_have_text("Runs use your AI models, so they cost money.")
    panel.click("#start-run")
    assert panel.calls("run/start") == [{"type": "assist_improver/run/start", "confirmed": True, "only": only}]


def test_the_run_menu_counts_what_is_held_at_each_step(open_panel):
    panel = open_settings(open_panel, run=status(run(), admits=5, held={"triage": 2, "analyze": 4, "write": 1}))
    panel.click("#run-menu")
    expect(panel.find(".menu [data-only='triage'] .count")).to_have_text("2")  # It takes in nothing new (P0.7).
    expect(panel.find(".menu [data-only='analyze'] .count")).to_have_text("4")
    expect(panel.find(".menu [data-only='write'] .count")).to_have_text("1")
    expect(panel.find(".menu [data-only='propose'] .count")).to_have_count(0)


def test_running_one_blocked_step_still_checks_its_model(open_panel):
    panel = open_settings(open_panel, settings=settings(paused=["analyze"]),
                          run=status(run(), held={"analyze": 2}, paused=["analyze"],
                                     blocked_models={"analyze": ["Analyze"]}))
    panel.click("#run-menu")
    panel.click(".menu [data-only='analyze']")
    expect(panel.find(".modal .c-warn")).to_have_text(
        "⚠ Analyze can't run: its AI model isn't available. Choose another first.")
    expect(panel.find("#start-run")).to_be_disabled()
    expect(panel.find(".modal p.cost")).to_have_count(1)  # Run one step ignores the block, so it isn't named.


def test_the_home_pages_intro_is_a_bubble_beside_the_lists_title(open_panel):
    """In Kai's words, on demand: the bubble beside the list's title, at a tap on a phone, not a block of text on the
    page."""
    lead = "Pick the ones that didn't go well, and run. Any you send in, or file with Assist, go straight to the improver."
    panel = open_panel(replies(), touch=True)
    expect(panel.find(".cs-lead")).to_have_count(0)
    expect(panel.page.get_by_role("heading", name="Your conversations", exact=True)).to_have_count(1)  # As it was.
    about = panel.page.get_by_role("note", name=f"About Your conversations: {lead}", exact=True)  # Read in full.
    expect(about).to_have_attribute("data-tip", lead)
    about.tap()
    tip = panel.find("#tip")
    expect(tip).to_have_text(lead)
    bubble, title = tip.bounding_box(), box(panel, ".cs-title")
    assert bubble["x"] >= 8 - 0.5 and bubble["x"] + bubble["width"] <= PHONE["width"] - 8 + 0.5, bubble
    assert bubble["y"] >= 0 and bubble["y"] + bubble["height"] <= title["y"], (bubble, title)  # Above, clear of it.
    top, last = box(panel, ".cs-top"), box(panel, ".cs-last")
    assert top["height"] == title["height"] and 2 <= last["y"] - (top["y"] + top["height"]) <= 6, (top, last)  # Snug.


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_an_info_mark_reads_at_three_to_one(open_panel, dark):
    """An ⓘ says there's more to read: like any mark that does, it reads at 3:1 or better (it was 2.7:1 on a light
    theme), on the home page and in settings."""
    panel = open_panel(replies(), dark=dark)
    assert not low_contrast(panel, [".cs-top .info-tip"], 3)
    assert not low_contrast(open_settings(open_panel, dark=dark), [".stn .info-tip"], 3)


def test_tests_per_conversation_each_round_are_a_bounded_stepper(open_panel):
    panel = open_settings(open_panel, settings__set=settings(tests=4))
    chip = panel.find(".stn[data-step='test'] .chip-model")
    expect(chip).to_have_text("Your assistant")
    expect(chip).to_have_attribute("data-tip", "At most 3 tests of a conversation each round, on your assistant.")
    panel.click(".stn[data-step='test'] .chip-model")
    expect(panel.find("#pop .t-n")).to_have_text("3")
    expect(panel.find("#pop .stepper-row + .pop-lead")).to_have_text("tests per conversation, each round")
    expect(panel.find("#pop .stepper-row")).to_have_attribute(
        "data-tip", "Each test replays one of your conversations with a fix, on your own assistant: 3 each round "
                    "costs at most about 3 times that conversation, plus up to three replays of it without the fix")
    panel.click("#t-plus")
    assert panel.calls("settings/set") == [{"type": "assist_improver/settings/set", "tests_per_goal": 4}]
    expect(panel.find("#pop .t-n")).to_have_text("4")


@pytest.mark.parametrize("n, minus, plus", [(1, False, True), (20, True, False)])
def test_tests_per_conversation_stay_between_1_and_20(open_panel, n, minus, plus):
    panel = open_settings(open_panel, settings=settings(tests=n))
    panel.click(".stn[data-step='test'] .chip-model")
    expect(panel.find("#t-minus")).to_be_enabled() if minus else expect(panel.find("#t-minus")).to_be_disabled()
    expect(panel.find("#t-plus")).to_be_enabled() if plus else expect(panel.find("#t-plus")).to_be_disabled()


def test_the_reply_check_can_be_switched_off_to_the_write_up_model(open_panel):
    panel = open_settings(open_panel)
    panel.click(".stn[data-step='test'] .chip-model")
    head = panel.find("#pop .pop-head").last
    expect(head).to_have_text("Check replies with")
    expect(head).to_have_attribute("data-tip", "Replies are checked with the write-up model when off")
    off = panel.find("#pop .opt[data-pick='']")
    expect(off).to_have_text("Off")
    expect(off).to_have_attribute("data-tip", "Replies are checked with the write-up model when off")
    off.click()
    panel.settle()
    assert panel.calls("settings/set") == [{"type": "assist_improver/settings/set", "role": "feedback_check",
                                            "entity_id": None}]


PIPELINES = [{"id": "p1", "name": "Home", "preferred": True}, {"id": "p2", "name": "Kitchen", "preferred": False},
             {"id": "p3", "name": "Home Assistant", "preferred": False}]


def preferred(panel):
    """Home Assistant's own command for the assistant Assist uses, as the panel sent it."""
    return [c for c in panel.calls() if c["type"] == PREFER]


def test_your_assist_names_the_default_assistant_with_nothing_to_turn_on(open_panel):
    knowledge = household(everyone=["Quiet hours start at 22:00."] * 6, pipelines=PIPELINES)
    panel = open_settings(open_panel, household=knowledge)
    expect(panel.find("[data-assist]")).to_have_text("Home ▾")
    expect(panel.find("[data-assist]")).to_have_attribute("data-tip", "The assistant Assist uses by default")
    expect(panel.find(".stn[data-step='requests'] .chip-row > *")).to_have_count(1)  # The assistant, nothing else.


def test_your_assist_menu_switches_the_default_assistant(open_panel):
    knowledge = household(everyone=["Quiet hours start at 22:00."] * 6, pipelines=PIPELINES)
    panel = open_settings(open_panel, household=knowledge)
    panel.click("[data-assist]")
    pop = panel.find("#pop")
    expect(pop.locator(".pop-head")).to_have_text("Your Assist uses")
    expect(pop.locator(".opt .opt-label")).to_have_text(["Home", "Kitchen", "Home Assistant"])
    expect(pop.locator(".opt").nth(0).locator(".check")).to_have_text("✓")
    expect(pop.locator(".opt button")).to_have_count(0)  # Nothing to turn on for one assistant.
    expect(pop).not_to_contain_text("household knowledge")
    panel.answer("household", household(everyone=["Quiet hours start at 22:00."] * 6, pipelines=[
        {**PIPELINES[0], "preferred": False}, {**PIPELINES[1], "preferred": True}, PIPELINES[2]]))
    panel.click("#pop [data-prefer='p2']")
    expect(panel.find("#pop")).to_have_count(0)
    assert preferred(panel) == [{"type": PREFER, "pipeline_id": "p2"}]  # As Settings > Voice assistants sets it.
    expect(panel.find("[data-assist]")).to_have_text("Kitchen ▾")


def test_choosing_an_assistant_home_assistant_no_longer_has_says_so(open_panel):
    knowledge = household(everyone=["Quiet hours start at 22:00."] * 6, pipelines=PIPELINES)
    panel = open_settings(open_panel, household=knowledge)
    panel.page.evaluate("([t, v]) => { window.__replies[t] = v; }", [PREFER, fail("Unable to find pipeline p2",
                                                                                  "not_found")])
    panel.click("[data-assist]")
    panel.click("#pop [data-prefer='p2']")
    expect(panel.find("#pop")).to_have_count(0)
    assert panel.dialogs == ["That assistant isn't in Home Assistant any more."]
    expect(panel.find("[data-assist]")).to_have_text("Home ▾")  # As Home Assistant still has it.


def test_without_an_assistant_your_assist_says_to_add_one(open_panel):
    panel = open_settings(open_panel)
    expect(panel.find("[data-assist]")).to_have_text("Choose… ▾")
    panel.click("[data-assist]")
    expect(panel.find("#pop .pop-lead").first).to_have_text("Add an assistant in Settings → Voice assistants first.")


# Your household: what the household knowledge holds, editable in place.

LINES = ["Quiet hours start at 22:00.", "The garage has two doors, left and right, opened by relays."]
ALEX = {"user_id": "u-alex", "name": "Alex", "lines": ["Alex likes the bedroom at 19 degrees."]}


def open_household(open_panel, size=DESKTOP, everyone=LINES, people=(ALEX,), **over):
    panel = open_settings(open_panel, size=size, household=household(everyone=everyone, people=people), **over)
    panel.click("[data-knowledge]")
    return panel


@pytest.mark.parametrize("size, chip", [(DESKTOP, "3 lines learned"), (PHONE, "3 lines")], ids=["desktop", "phone"])
def test_your_household_opens_what_the_household_knowledge_holds(open_panel, size, chip):
    panel = open_settings(open_panel, size=size, household=household(everyone=LINES, people=[ALEX]))
    expect(panel.find("[data-knowledge]")).to_have_text(chip)  # A chip without an arrow: it opens, it doesn't pick.
    expect(panel.find("[data-knowledge]")).to_have_attribute(
        "data-tip", "What your assistants know about your household. Accepted improvements add to it.")
    panel.click("[data-knowledge]")
    dialog = panel.find(".modal[role='dialog']")
    expect(dialog).to_have_attribute("aria-label", "Household knowledge")
    expect(dialog.locator(".modal-head")).to_contain_text("Household knowledge3 lines")
    expect(dialog.locator(".kn-group")).to_have_text(["Everyone2", "Alex1"])
    expect(dialog.locator(".kn-text")).to_have_text([*LINES, *ALEX["lines"]])
    expect(dialog.locator(".kn-search")).to_have_count(0)  # Search only once the list is long.


def test_a_long_household_line_is_clamped_with_read_more_and_a_short_one_isnt(open_panel):
    long = "The garage has two doors " + "and each one has its own relay, which " * 12 + "is pressed once to move it."
    panel = open_household(open_panel, size=PHONE, everyone=[LINES[0], long], people=())
    more = panel.find(".kn-more")
    expect(more.nth(0)).to_be_hidden()
    expect(more.nth(1)).to_be_visible()
    expect(more.nth(1)).to_have_text("Read more")
    clamped = panel.find(".kn-text").nth(1).bounding_box()["height"]
    panel.click(".kn-more >> nth=1")
    expect(panel.find(".kn-more").nth(1)).to_have_text("Show less")
    assert panel.find(".kn-text").nth(1).bounding_box()["height"] > clamped * 2


def test_a_household_line_is_edited_in_place(open_panel):
    panel = open_household(open_panel)
    panel.click("[data-kn-edit='everyone:0']")
    editor = panel.find("#kn-text")
    expect(editor).to_have_value(LINES[0])
    expect(editor).to_be_focused()
    editor.fill("Quiet hours start at 22:30.")
    panel.answer("household", household(everyone=["Quiet hours start at 22:30.", LINES[1]], people=[ALEX]))
    panel.click("#kn-save")
    assert panel.calls("household/edit") == [{"type": "assist_improver/household/edit", "scope": "everyone",
                                              "action": "update", "index": 0, "old": LINES[0],
                                              "text": "Quiet hours start at 22:30."}]
    expect(panel.find("#kn-text")).to_have_count(0)
    expect(panel.find(".kn-text").first).to_have_text("Quiet hours start at 22:30.")


def test_deleting_a_household_line_asks_first(open_panel):
    panel = open_household(open_panel)
    panel.click("[data-kn-ask='u-alex:0']")
    expect(panel.find(".kn-confirm span")).to_have_text("Delete this line?")
    panel.click(".kn-confirm #kn-cancel")
    expect(panel.find(".kn-confirm")).to_have_count(0)
    assert not panel.calls("household/edit")
    panel.click("[data-kn-ask='u-alex:0']")
    panel.answer("household", household(everyone=LINES))
    panel.click("#kn-remove")
    assert panel.calls("household/edit") == [{"type": "assist_improver/household/edit", "scope": "u-alex",
                                              "action": "delete", "index": 0, "old": ALEX["lines"][0]}]
    expect(panel.find(".kn-text")).to_have_text(LINES)


def test_a_household_line_is_added_for_everyone(open_panel):
    panel = open_household(open_panel)
    panel.click("[data-kn-add='everyone']")
    editor = panel.find("#kn-text")
    expect(editor).to_have_attribute("placeholder", "One fact or preference, in a sentence")
    editor.fill("Bins go out on Tuesday night.")
    panel.answer("household", household(everyone=[*LINES, "Bins go out on Tuesday night."], people=[ALEX]))
    panel.click("#kn-save")
    assert panel.calls("household/edit") == [{"type": "assist_improver/household/edit", "scope": "everyone",
                                              "action": "add", "text": "Bins go out on Tuesday night."}]
    expect(panel.find(".modal-head .kn-count")).to_have_text("4 lines")


def test_a_line_just_for_me_can_be_added_before_i_have_any(open_panel):
    panel = open_household(open_panel, people=())
    expect(panel.find(".kn-add-me button")).to_have_text("+ Add a line just for Alex")
    panel.click(".kn-add-me button")
    expect(panel.find(".kn-group")).to_have_text(["Everyone2", "Alex"])
    panel.find("#kn-text").fill("Alex likes the bedroom at 19 degrees.")
    panel.click("#kn-save")
    assert panel.calls("household/edit")[-1] == {"type": "assist_improver/household/edit", "scope": "u-alex",
                                                 "action": "add", "text": "Alex likes the bedroom at 19 degrees."}


def test_a_household_edit_that_fails_says_why(open_panel):
    panel = open_household(open_panel, household__edit=fail(
        "That line has changed since you opened it. Close and open it again."))
    panel.click("[data-kn-add='everyone']")
    panel.find("#kn-text").fill("Bins go out on Tuesday night.")
    panel.click("#kn-save")
    expect(panel.find(".kn-editor .error-text")).to_have_text(
        "That line has changed since you opened it. Close and open it again.")
    expect(panel.find("#kn-text")).to_have_value("Bins go out on Tuesday night.")
    expect(panel.find("#kn-save")).to_be_enabled()


def test_a_failed_household_line_edit_keeps_what_was_typed(open_panel):
    panel = open_household(open_panel, household__edit=fail(
        "That line has changed since you opened it. Close and open it again."))
    panel.click("[data-kn-edit='everyone:0']")
    panel.find("#kn-text").fill("Quiet hours start at 22:30.")
    panel.click("#kn-save")
    expect(panel.find(".kn-editor .error-text")).to_be_visible()
    expect(panel.find("#kn-text")).to_have_value("Quiet hours start at 22:30.", timeout=1000)


def test_the_household_lines_open_as_they_are_now(open_panel):
    # A line changed on another screen since Settings loaded: opening the lines reads them again, so closing and
    # opening them, as a failed edit says to, shows the line as it is now, and the chip counts what's there.
    panel = open_settings(open_panel, household=household(everyone=LINES, people=[ALEX]))
    panel.answer("household", household(everyone=["Quiet hours start at 22:30."], people=[ALEX]))
    panel.click("[data-knowledge]")
    expect(panel.find(".kn-text")).to_have_text(["Quiet hours start at 22:30.", *ALEX["lines"]])
    expect(panel.find(".modal-head .kn-count")).to_have_text("2 lines")
    expect(panel.find("[data-knowledge]")).to_have_text("2 lines learned")


def test_a_household_line_edit_saves_over_the_line_as_it_opened(open_panel):
    # A line opened for editing before the lines were read again, and changed on another screen meanwhile: the save
    # names the line as the editor opened with it, so Home Assistant says it changed rather than writing over it.
    panel = open_settings(open_panel, household=household(everyone=LINES, people=[ALEX]))
    panel.answer("household", household(everyone=["Quiet hours start at 22:30.", LINES[1]], people=[ALEX]))
    panel.hold("household")
    panel.find("[data-knowledge]").click()
    panel.find("[data-kn-edit='everyone:0']").click()
    expect(panel.find("#kn-text")).to_have_value(LINES[0])
    panel.find("#kn-text").fill("Quiet hours start at 23:00.")
    panel.release("household")
    panel.click("#kn-save")
    assert panel.calls("household/edit") == [{"type": "assist_improver/household/edit", "scope": "everyone",
                                              "action": "update", "index": 0, "old": LINES[0],
                                              "text": "Quiet hours start at 23:00."}]



def test_a_household_line_delete_names_the_line_it_asked_about(open_panel):
    # The delete question opened before the lines were read again, and the line deleted on another screen meanwhile
    # (so another moved up into its place): the delete names the line it asked about, so Home Assistant says it changed
    # rather than deleting the other.
    panel = open_settings(open_panel, household=household(everyone=LINES, people=[ALEX]))
    panel.answer("household", household(everyone=[LINES[1]], people=[ALEX]))
    panel.hold("household")
    panel.find("[data-knowledge]").click()
    panel.find("[data-kn-ask='everyone:0']").click()
    expect(panel.find(".kn-confirm")).to_be_visible()
    panel.release("household")
    panel.click("#kn-remove")
    assert panel.calls("household/edit") == [{"type": "assist_improver/household/edit", "scope": "everyone",
                                              "action": "delete", "index": 0, "old": LINES[0]}]


def test_a_household_delete_that_fails_says_why(open_panel):
    panel = open_household(open_panel, household__edit=fail(
        "That line has changed since you opened it. Close and open it again."))
    panel.click("[data-kn-ask='everyone:0']")
    panel.click("#kn-remove")
    expect(panel.find(".kn-line .error-text")).to_have_text(
        "That line has changed since you opened it. Close and open it again.")
    expect(panel.find("#kn-remove")).to_be_enabled()


def test_household_search_lets_go_once_the_list_is_short_again(open_panel):
    # Searching a long list, then deleting its one match: six lines are left, so the search goes, and every line shows.
    lines = ["Line 0 about the garage.", *[f"Line {n} about the kitchen." for n in range(1, 7)]]
    panel = open_household(open_panel, everyone=lines, people=())
    panel.find("#kn-search").fill("garage")
    expect(panel.find(".kn-line:visible .kn-text")).to_have_text(["Line 0 about the garage."])
    panel.answer("household", household(everyone=lines[1:], people=()))
    panel.click("[data-kn-ask='everyone:0']")
    panel.click("#kn-remove")
    expect(panel.find("#kn-search")).to_have_count(0)
    expect(panel.find(".kn-line:visible .kn-text")).to_have_text(lines[1:])
    expect(panel.find(".kn-none")).to_be_hidden()
    # A line added makes seven again: the search is back, with what it searches for, and finds nothing.
    panel.answer("household", household(everyone=[*lines[1:], "Line 7 about the hall."], people=()))
    panel.click("[data-kn-add='everyone']")
    panel.find("#kn-text").fill("Line 7 about the hall.")
    panel.click("#kn-save")
    expect(panel.find("#kn-search")).to_have_value("garage")
    expect(panel.find(".kn-none")).to_be_visible()

def test_household_search_appears_for_a_long_list_and_filters_it(open_panel):
    lines = [f"Line {n} about the {'garage' if n % 3 == 0 else 'kitchen'}." for n in range(7)]
    panel = open_household(open_panel, everyone=lines, people=())
    search = panel.find("#kn-search")
    expect(search).to_have_attribute("placeholder", "Search")
    search.fill("garage")
    expect(panel.find(".kn-line:visible .kn-text")).to_have_text(
        ["Line 0 about the garage.", "Line 3 about the garage.", "Line 6 about the garage."])
    expect(panel.find(".kn-none")).to_be_hidden()
    search.fill("bins")
    expect(panel.find(".kn-none")).to_be_visible()
    expect(panel.find(".kn-none")).to_have_text("Nothing matches.")


def test_an_empty_household_says_nothing_is_learned_yet(open_panel):
    panel = open_household(open_panel, everyone=(), people=())
    expect(panel.find(".modal")).to_contain_text("Nothing learned yet: accepted improvements add lines here.",
                                                 timeout=1000)


@pytest.mark.parametrize("close", ["close", "outside", "escape"])
def test_the_household_modal_closes(open_panel, close):
    panel = open_household(open_panel)
    if close == "close":
        panel.find("#modal-close").click()
    elif close == "outside":
        panel.find(".modal-shade").click(position={"x": 5, "y": 5})
    else:
        panel.page.keyboard.press("Escape")
    panel.settle()
    expect(panel.find(".modal")).to_have_count(0)
    assert not panel.calls("household/edit")


def test_on_a_phone_the_household_modal_rises_from_the_bottom(open_panel):
    panel = open_household(open_panel, size=PHONE)
    box = panel.find(".modal").bounding_box()
    assert abs(box["y"] + box["height"] - PHONE["height"]) <= 1 and abs(box["width"] - PHONE["width"]) <= 1


def test_household_lines_and_names_are_escaped(open_panel):
    panel = open_household(open_panel, everyone=[XSS], people=[{"user_id": XSS, "name": XSS, "lines": [XSS]}])
    expect(panel.find(".kn-text")).to_have_text([XSS, XSS])
    expect(panel.find(".kn-group").nth(1)).to_contain_text(XSS)
    panel.click(".kn-section >> nth=1 >> [data-kn-edit]")
    expect(panel.find("#kn-text")).to_have_value(XSS)
    panel.click("#kn-save")
    assert panel.calls("household/edit")[-1]["scope"] == XSS
    assert_escaped(panel)


# Uninstall, at the end of the settings: with lines learned, they're there to keep (a copy button in the box's corner),
# and it goes once a box is ticked.

LEARNED_TEXT = "- " + "\n- ".join(LINES) + "\n\nFor Alex:\n- Alex likes the bedroom at 19 degrees."  # household.plain's.
CHECK = "M9 16.17 4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41z"  # The check mark's path: copied.
STUB_CLIPBOARD = """() => { window.__copied = [];
    Object.defineProperty(navigator, "clipboard", {value: {writeText: async (text) => { window.__copied.push(text); }}}); }"""
# Where Uninstall's copy button sits: its gaps to the box's right and top edges, how far it stays clear of where the
# lines end, its size and place, and how far the lines are scrolled.
COPY_CORNER = """() => {
  const root = document.querySelector("assist-improver-panel").shadowRoot, lines = root.querySelector(".u-text");
  const b = root.querySelector("#u-copy").getBoundingClientRect(), box = lines.getBoundingClientRect();
  const end = box.left + lines.clientLeft + lines.clientWidth - parseFloat(getComputedStyle(lines).paddingRight);
  return {right: box.right - b.right, top: b.top - box.top, clear: b.left - end, size: [b.width, b.height],
          at: [b.x, b.y], scrolled: lines.scrollTop};
}"""


def open_uninstall(open_panel, size=DESKTOP, text=LEARNED_TEXT, **over):
    """As panel.ws_household sends the household's lines as one text (empty: nothing learned)."""
    learned = {**household(everyone=LINES, people=[ALEX]), "text": text} if text else household()
    panel = open_settings(open_panel, size=size, household=learned, **{"uninstall": {"removed": True}, **over})
    panel.click("#uninstall")
    return panel


@pytest.mark.parametrize("size", [PHONE, DESKTOP], ids=["phone", "desktop"])
def test_uninstall_ends_the_settings_and_shows_the_lines_to_save_before_it_can_go(open_panel, size):
    panel = open_uninstall(open_panel, size=size)
    expect(panel.find(".page .end-row #uninstall")).to_have_text("Uninstall")  # Last on the page.
    dialog = panel.find(".modal[role='dialog']")
    expect(dialog).to_have_attribute("aria-label", "Uninstall Assist Improver?")
    expect(dialog.locator(".c-what")).to_have_text(
        "Removes what it stored and the AI Tasks it set up. Accepted improvements stay. To keep what your assistants "
        "learned, add this to their instructions:")
    assert dialog.locator(".u-text").inner_text() == LEARNED_TEXT  # Every line, as one text.
    expect(dialog.locator(".u-box").get_by_role("button", name="Copy", exact=True)).to_have_attribute("data-tip", "Copy")
    expect(dialog.locator(".u-box + .reason")).to_have_text("I've saved it, or I'll do without it")  # Right after the box.
    expect(dialog.locator("#u-saved")).not_to_be_checked()
    expect(dialog.locator("#u-confirm")).to_be_disabled()  # Until it's ticked.
    panel.click("#u-saved")
    expect(dialog.locator("#u-confirm")).to_be_enabled()
    assert panel.calls("uninstall") == []  # Nothing done yet.
    assert panel.page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


@pytest.mark.parametrize("size, dark", [(PHONE, True), (DESKTOP, False)], ids=["phone-dark", "desktop-light"])
def test_uninstalls_copy_button_sits_in_the_boxs_corner_clear_of_the_lines_as_they_scroll(open_panel, size, dark):
    panel = open_uninstall(open_panel, size=size, dark=dark, text="\n".join(f"- {line}" for line in LINES * 6))  # Scrolls.
    at = panel.page.evaluate(COPY_CORNER)
    assert 0 <= at["right"] <= 8 and 0 <= at["top"] <= 8, at  # In the top-right corner, inside the box.
    assert at["clear"] >= 0, at  # The lines end before it.
    assert min(at["size"]) >= 32, at  # Easy to hit.
    assert panel.contrast("#u-copy") >= 3  # Quiet, still plain to see.
    panel.find(".u-text").evaluate("(el) => { el.scrollTop = el.scrollHeight; }")
    scrolled = panel.page.evaluate(COPY_CORNER)
    assert scrolled["scrolled"] > 0 and scrolled["at"] == at["at"], scrolled  # The lines move; it stays.


def test_copy_puts_the_lines_on_the_clipboard_and_shows_a_check_mark_for_a_moment(open_panel):
    panel = open_uninstall(open_panel)
    panel.page.evaluate(STUB_CLIPBOARD)
    panel.host.get_by_role("button", name="Copy", exact=True).click()
    copied = panel.host.get_by_role("button", name="Copied", exact=True)
    expect(copied).to_have_attribute("data-tip", "Copied")
    expect(copied.locator("path")).to_have_attribute("d", CHECK)
    assert panel.page.evaluate("window.__copied") == [LEARNED_TEXT]  # The exact text.
    assert panel.contrast("#u-copy") >= 3
    expect(panel.find("#u-confirm")).to_be_disabled()  # Copying isn't ticking.
    again = panel.host.get_by_role("button", name="Copy", exact=True)
    expect(again).to_have_attribute("data-tip", "Copy", timeout=4000)  # Back after about two seconds.
    expect(again.locator("path")).not_to_have_attribute("d", CHECK)


def test_copying_by_keyboard_the_tip_says_copied(open_panel):
    panel = open_uninstall(open_panel)
    panel.page.evaluate(STUB_CLIPBOARD)
    panel.find("#u-copy").focus()
    expect(panel.find("#tip")).to_have_text("Copy")
    panel.page.keyboard.press("Enter")
    expect(panel.find("#tip")).to_have_text("Copied")
    assert panel.page.evaluate("window.__copied") == [LEARNED_TEXT]


def test_ticked_uninstall_removes_it_then_opens_home_assistants_integrations(open_panel):
    panel = open_uninstall(open_panel)
    panel.click("#u-saved")
    panel.click("#u-confirm")
    assert panel.calls("uninstall") == [{"type": "assist_improver/uninstall"}]
    assert panel.path() == "/config/integrations/dashboard"  # The panel goes with the integration.
    expect(panel.find(".modal")).to_have_count(0)


def test_with_nothing_learned_uninstall_asks_only_whether_to_go(open_panel):
    panel = open_uninstall(open_panel, text="")
    expect(panel.find(".modal .c-what")).to_have_text(
        "Removes what it stored and the AI Tasks it set up. Accepted improvements stay.")
    expect(panel.find(".modal .u-text, .modal #u-copy, .modal #u-saved")).to_have_count(0)
    panel.click("#u-confirm")
    assert panel.calls("uninstall") == [{"type": "assist_improver/uninstall"}]


def test_an_uninstall_that_fails_says_why_and_stays(open_panel):
    why = "Another change is still being applied."
    panel = open_uninstall(open_panel, text="", uninstall=fail(why))
    panel.click("#u-confirm")
    expect(panel.find(".modal .error-text")).to_have_text(why)
    expect(panel.find("#u-confirm")).to_be_enabled()
    assert panel.path() == "/assist-improver?view=settings"


def test_not_now_or_escape_leaves_without_uninstalling(open_panel):
    panel = open_uninstall(open_panel)
    panel.click(".u-foot .u-cancel")
    expect(panel.find(".modal")).to_have_count(0)
    panel.click("#uninstall")
    panel.page.keyboard.press("Escape")
    panel.settle()
    expect(panel.find(".modal")).to_have_count(0)
    assert panel.calls("uninstall") == []


def test_uninstalls_lines_show_as_text(open_panel):
    panel = open_uninstall(open_panel, text=XSS)
    assert panel.find(".modal .u-text").inner_text() == XSS
    assert_escaped(panel)


# History: runs, improvements going up for review, and decisions.

def open_history(open_panel, events, size=DESKTOP):
    return open_settings(open_panel, size=size, log={"events": events})


def test_history_is_empty_until_something_happens(open_panel):
    panel = open_history(open_panel, [])
    expect(panel.find(".history .h-title")).to_have_text("History")
    expect(panel.find(".history .h-empty")).to_have_text("Nothing yet: runs and your decisions show up here.")


SCHEDULED_BLOCK = "Analyze can't run: its AI model isn't available. Choose another in the settings."


@pytest.mark.parametrize("event, title, sub", [
    (event_run(progress={"taken_in": 4, "analyzed": 4, "new_improvements": 2, "duplicates": 1}),
     "Run finished", "4 taken in · 4 analyzed · 2 new improvements · 1 already addressed by an improvement"),
    (event_run("failed", error="CaseError: User turns and source snapshots do not align"),
     "Run stopped early", "Nothing was lost: what it didn't finish is held for your next run."),
    (event_run("failed", error=SCHEDULED_BLOCK, trigger="scheduled"), "Run stopped early · scheduled", SCHEDULED_BLOCK),
    (event_run(only="analyze", progress={"analyzed": 2}), "Run finished · Analyze only", "2 analyzed"),
    (event_run(trigger="filed", progress={"taken_in": 1, "analyzed": 1}), "Run finished · on demand",
     "1 taken in · 1 analyzed"),
    ({**event_run(progress={"taken_in": 3}), "picked": True}, "Run finished · picked", "3 taken in"),
    (event_run(progress={"held": 2, "why": "Analyze failed 3 times in a row.", "unreadable": 1,
                         "problems": [{"stage": "propose", "error": "RuntimeError: boom"}]}),
     "Run finished", "2 held: Analyze failed 3 times in a row. · Propose couldn't finish · 1 couldn't be read"),
    (event_run(trigger="revision", progress={"taken_in": 1, "new_improvements": 1}), "Run finished · revision",
     "1 taken in · 1 new improvement"),
    (event_run(trigger="conflict", progress={"taken_in": 1}), "Run finished · conflict", "1 taken in"),
    (event_run(trigger="answer", progress={"taken_in": 1, "analyzed": 1}), "Run finished · answer",
     "1 taken in · 1 analyzed"),
    (event_run("cancelled"), "Run cancelled", None),
    (event_run("interrupted"), "Run interrupted", None),
    (event_posted(), "Up for review", GARAGE),
    (event_decision("accept"), "Accepted", f"{GARAGE} · Alex"),
    (event_decision("decline"), "Declined", f"{GARAGE} · Alex"),
    (event_decision("revise"), "Asked for a revision", f"{GARAGE} · Alex"),
    (event_decision("decline", reason="not_an_issue"), "Declined: not an issue", f"{GARAGE} · Alex"),
    (event_decision("undo", by=None), "Undid", GARAGE),
], ids=["run", "stopped", "scheduled-block", "one-step", "on-demand", "picked", "partly", "revision", "conflict",
        "answer", "cancelled", "interrupted", "posted", "accepted", "declined", "revise", "not-an-issue", "undo"])
def test_history_says_what_happened_in_plain_words(open_panel, event, title, sub):
    panel = open_history(open_panel, [event])
    row = panel.find(".h-row")
    expect(row.locator(".h-text > div").first).to_have_text(title)
    if sub:
        expect(row.locator(".h-sub")).to_have_text(sub)
    else:
        expect(row.locator(".h-sub")).to_have_count(0)
    minutes = round((NOW - datetime.fromisoformat(event["at"])).total_seconds() / 60)
    expect(row.locator(".h-when")).to_have_text(f"{minutes} min ago")


def test_history_keeps_the_order_it_is_given_newest_first(open_panel):
    events = [event_decision("accept", minutes=1), event_posted(minutes=10), event_run(minutes=30)]
    panel = open_history(open_panel, events)
    expect(panel.find(".h-row .h-text > div:first-child")).to_have_text(["Accepted", "Up for review", "Run finished"])
    expect(panel.find(".h-row .h-when")).to_have_text(["1 min ago", "10 min ago", "30 min ago"])


def test_history_shows_eight_then_more_on_request(open_panel):
    panel = open_history(open_panel, [event_run(minutes=n + 1) for n in range(10)])
    expect(panel.find(".h-row")).to_have_count(8)
    expect(panel.find("#history-more")).to_have_text("Show more")
    panel.click("#history-more")
    expect(panel.find(".h-row")).to_have_count(10)
    expect(panel.find("#history-more")).to_have_count(0)


def test_a_history_row_about_an_improvement_opens_its_review(open_panel):
    panel = open_history(open_panel, [event_posted(id="imp2", title=CAR), event_run()])
    assert "h-link" not in classes(panel.find(".h-row").nth(1))
    panel.click(".h-row.h-link")
    assert panel.path() == "/assist-improver?id=imp2"
    assert panel.calls("improvement")[-1]["improvement_id"] == "imp2"


def test_every_dynamic_string_in_history_is_escaped(open_panel):
    events = [event_posted(id=XSS, title=XSS), event_decision("accept", by=XSS, title=XSS),
              event_decision(XSS, title=XSS), event_run("failed", error=f"{XSS} can't run: {XSS}"),
              event_run(progress={"held": 1, "why": XSS, "problems": [{"stage": XSS, "error": "x"}]})]
    panel = open_history(open_panel, events)
    expect(panel.find(".h-row .h-sub").first).to_have_text(XSS)
    expect(panel.find(".h-row").nth(2).locator(".h-text > div").first).to_have_text(XSS)
    expect(panel.find(".h-row").nth(3).locator(".h-sub")).to_have_text(f"{XSS} can't run: {XSS}")
    expect(panel.find(".h-row").nth(4).locator(".h-sub")).to_have_text(f"1 held: {XSS} · {XSS} couldn't finish")
    assert_escaped(panel)
    panel.click(".h-row.h-link")
    assert panel.calls("improvement")[-1]["improvement_id"] == XSS


def test_every_dynamic_string_in_the_settings_is_escaped(open_panel):
    task = {**LOCAL, "entity_id": XSS, "label": XSS, "title": XSS + "!"}
    knowledge = household(everyone=["A line."], pipelines=[{"id": XSS, "name": XSS, "preferred": True, "target": XSS,
                                                             "using": True}])
    panel = open_settings(open_panel, household=knowledge,
                          settings=settings(roles={"analyzer": role(XSS, "large")}, tasks=[OPUS, HAIKU, task],
                                            providers=[{"entry_id": "p1", "title": XSS, "domain": "x"}]),
                          settings__models={"models": [{"value": XSS, "label": XSS}], "default": XSS, "named": True,
                                            "suggested": {"large": XSS, "small": None}})
    expect(panel.find(".stn[data-step='analyze'] .chip-model")).to_contain_text(XSS)
    expect(panel.find("[data-assist]")).to_contain_text(XSS)
    panel.click(".stn[data-step='analyze'] .chip-model")
    expect(panel.find("#pop .opt[data-pick]").nth(1).locator(".opt-label")).to_have_text(XSS)
    expect(panel.find("#pop .opt[data-pick]").nth(1).locator(".opt-sub")).to_have_text(XSS + "!")
    panel.click("#new-task")
    expect(panel.find("#c-model option")).to_have_text([XSS])
    expect(panel.find("#c-name")).to_have_value(f"Assist Improver ({XSS})")
    assert_escaped(panel)
    panel.page.keyboard.press("Escape")
    panel.settle()
    panel.click("[data-assist]")
    expect(panel.find("#pop .opt-label")).to_have_text([XSS])
    panel.click("#pop [data-prefer]")
    expect(panel.find("#pop")).to_have_count(0)
    assert preferred(panel)[-1]["pipeline_id"] == XSS
    assert_escaped(panel)


# Light and dark, phone and desktop.

@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_dark_step_cards_lift_off_the_background_and_light_ones_sit_on_the_card(open_panel, dark):
    panel = open_settings(open_panel, dark=dark)
    colors = panel.page.evaluate("""() => { const root = document.querySelector('assist-improver-panel').shadowRoot;
        const bg = (css) => getComputedStyle(root.querySelector(css)).backgroundColor;
        return [bg('.stn'), bg('.flow-card')]; }""")
    if dark:
        assert colors[0] != colors[1], colors
        assert colors[0] == "rgb(38, 39, 42)"
    else:
        assert colors[0] == colors[1] == "rgb(255, 255, 255)"


# Contrast is measured on the panel's own colors; the toolbar and buttons take Home Assistant's theme colors.
TEXTS = {  # Body text, held to WCAG's 4.5:1.
    "home": [".cs-title", ".cs-said", ".cs-sub", ".cs-last"],
    "review": ["h1", ".summary .benefit", ".safe li span:last-child", ".summary .label", ".tests li span:nth-child(2)",
               "tr.add td.text", "tr.del td.text"],
    "settings": [".stn[data-step='analyze'] .s-title", ".stn[data-step='propose'] .chip-model", ".history .h-title",
                 ".h-row .h-sub", ".stream-label"],
    "confirmation": [".c-which", ".c-cost > p", ".c-spend li"],  # A picked run's: no "Also" (it moves nothing else).
}


def open_for_contrast(open_panel, view, dark):
    if view == "home":
        return open_panel(replies(run=status(run("failed", error="RuntimeError: boom"))), dark=dark)
    if view == "review":
        kinds = ["script", "exposure", "aliases", "instructions", "household", "assistant"]
        panel = open_review(open_panel, dark=dark, changes=[
            {"name": f"A {kind} change", "kind": kind, "rows": INSTRUCTION_ROWS, "added": 1, "removed": 1}
            for kind in kinds])
        return panel
    if view == "settings":
        return open_settings(open_panel, dark=dark, log={"events": [event_posted()]},
                             settings=settings(roles={"analyzer": role(HAIKU["entity_id"], "large")}))
    if view == "menu":
        panel = open_settings(open_panel, dark=dark)
        panel.click(".stn[data-step='analyze'] .chip-model")
        return panel
    panel = open_panel(replies(run=status(run(), blocked_models={"analyze": ["Analyze"]}, held={"write": 1},
                                          spend={"analyze": {"tokens": 21_000, "model": "Claude Opus 5.5"}})), dark=dark)
    panel.click("[data-cs-pick='c1']")
    panel.click("#cs-run")
    return panel


def low_contrast(panel, selectors, floor):
    found = {css: panel.contrast(css) for css in selectors}
    assert None not in found.values(), f"not shown: {[css for css, r in found.items() if r is None]}"
    return {css: round(ratio, 2) for css, ratio in found.items() if ratio < floor}


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
@pytest.mark.parametrize("view", list(TEXTS))
def test_text_reads_clearly_in_light_and_dark_themes(open_panel, view, dark):
    panel = open_for_contrast(open_panel, view, dark)
    assert not low_contrast(panel, TEXTS[view], 4.5)


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_each_kind_of_change_has_a_tint_that_reads_in_light_and_dark(open_panel, dark):
    panel = open_for_contrast(open_panel, "review", dark)
    kinds = [f".kind.{k}" for k in ("script", "exposure", "aliases", "instructions", "household", "assistant")]
    tints = {panel.page.evaluate("(c) => getComputedStyle(document.querySelector('assist-improver-panel').shadowRoot"
                                 ".querySelector(c)).color", css) for css in kinds}
    assert len(tints) >= 5  # Each kind its own tint (instructions and household lines share one).
    assert not low_contrast(panel, kinds, 3)  # Small text: 3:1 is the floor for anything meant to be read.


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_a_warning_reads_in_light_and_dark(open_panel, dark):
    low = low_contrast(open_for_contrast(open_panel, "settings", dark), [".stn[data-step='analyze'] .chip-model.warn"], 3)
    low |= low_contrast(open_for_contrast(open_panel, "menu", dark), [".tag.bad"], 3)
    low |= low_contrast(open_for_contrast(open_panel, "confirmation", dark), [".c-warn"], 3)
    assert not low


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_what_an_improvement_gets_you_reads_in_light_and_dark(open_panel, dark):
    low = low_contrast(open_for_contrast(open_panel, "review", dark),
                       [".outcome", ".count-add", ".count-del", ".tests .ok .result", ".safe .mark"], 3)
    low |= low_contrast(open_for_contrast(open_panel, "menu", dark), [".tag.good"], 3)
    assert not low


def act_none(panel):
    pass


def act_open_details(panel):
    panel.click(".h-details summary")


def act_open_confirmation(panel):
    panel.click("[data-cs-pick='c1']")
    panel.click("#cs-run")


def act_open_everything(panel):
    open_all(panel)
    panel.settle()


def act_revise(panel):
    panel.click("#arrow")
    panel.click(".footer .menu [data-choice='revise']")
    panel.click("#arrow")


def act_run_menu(panel):
    panel.click("#run-menu")


def act_model_menu(panel):
    panel.click(".stn[data-step='analyze'] .chip-model")


def act_household(panel):
    panel.click("[data-knowledge]")


LONG = "The left garage door relay button.garage_door_left_relay_switch_for_the_big_door_on_the_north_side_of_the_house"
LONG_REVIEW = review(title="Open or close both garage doors together by pressing each of their relay buttons in turn",
                     changes=[{"name": "script.open_both_garage_doors_by_pressing_each_relay", "kind": "script",
                               "rows": [*SCRIPT_ROWS, {"sign": "+", "old": None, "new": 6, "text": LONG}], "added": 6,
                               "removed": 0},
                              {"name": "Household knowledge", "kind": "instructions", "rows": INSTRUCTION_ROWS,
                               "added": 1, "removed": 1}],
                     conflicts=[{"what": "Garage doors", "why": "It was changed in Home Assistant after the "
                                                                "improvement was tested."}])
VIEWS = {
    "home-to-review": ("/assist-improver", dict(improvements={"improvements": PENDING + EARLIER},
                                                run=status(run(progress={"new_improvements": 2})),
                                                household=household(everyone=LINES)),
                       act_none),
    "settings-run-stopped": ("/assist-improver?view=settings", dict(run=status(run("failed", error="CaseError: " + LONG)),
                             log={"events": [event_run("failed", error="CaseError: " + LONG)]}), act_open_details),
    "home-running": ("/assist-improver", dict(run=status(run("running", stage="analyze", progress={
        "total": 120, "done": 37, "conversation": "c1"}))), act_none),
    "home-confirmation": ("/assist-improver", dict(run=status(run(), admits=12, held={"analyze": 3, "propose": 2},
                                                              paused=["write"],
                                                              blocked_models={"propose": ["Propose", "Review"]})),
                          act_open_confirmation),
    "review": ("/assist-improver?id=imp1", dict(improvement=LONG_REVIEW), act_open_everything),
    "review-decision": ("/assist-improver?id=imp1", dict(improvement=LONG_REVIEW), act_revise),
    "settings": ("/assist-improver?view=settings", dict(
        run=status(run(), admits=12, held={"analyze": 3}, review=2, applied=14),
        household=household(everyone=LINES, pipelines=PIPELINES), log={"events": [
            event_run(progress={"taken_in": 12, "analyzed": 12, "new_improvements": 2, "duplicates": 1}),
            event_decision("accept", title=LONG_REVIEW["title"])]}), act_none),
    "settings-run-menu": ("/assist-improver?view=settings", dict(run=status(run(), held={"analyze": 3})),
                          act_run_menu),
    "settings-model-menu": ("/assist-improver?view=settings", dict(settings=settings(recommended=False)), act_model_menu),
    "settings-household": ("/assist-improver?view=settings", dict(household=household(
        everyone=[*LINES, LONG] * 3, people=[ALEX])), act_household),
}


@pytest.mark.parametrize("size", list(SIZES.values()), ids=list(SIZES))
@pytest.mark.parametrize("view", list(VIEWS))
def test_no_view_scrolls_sideways(open_panel, view, size):
    path, over, act = VIEWS[view]
    panel = open_panel(replies(**over), path=path, size=size)
    act(panel)
    assert_no_horizontal_scroll(panel)


def test_a_conflict_naming_a_long_entity_id_wraps_on_a_phone(open_panel):
    panel = open_review(open_panel, size=SMALL_PHONE, conflicts=[
        {"what": "sensor.living_room_air_purifier_filter_life_remaining",
         "why": "The device is no longer in Home Assistant."}])
    assert_no_horizontal_scroll(panel)


@pytest.mark.parametrize("width", [320, 600, 700, 820, 1000, 1600])
def test_the_settings_track_fits_any_window_width(open_panel, width):
    panel = open_settings(open_panel, size={"width": width, "height": 900},
                          run=status(run(), admits=12, held={"analyze": 3}, review=2, applied=14))
    assert_no_horizontal_scroll(panel)
    track = panel.find(".track").bounding_box()
    boxes = [card.bounding_box() for card in panel.find(".stn").all()]
    for box in boxes:
        assert box["x"] >= track["x"] - 1 and box["x"] + box["width"] <= track["x"] + track["width"] + 1, box
    # Centred in the track, so a phone's side lanes run midway between the panel's edge and the cards on both sides.
    left = min(b["x"] for b in boxes) - track["x"]
    right = track["x"] + track["width"] - max(b["x"] + b["width"] for b in boxes)
    assert abs(left - right) <= 2, (left, right)


# Your conversations, on the home page: find them, read one, pick some and run, or send one in (proposed words).

def test_conversations_lead_the_home_page_newest_first_with_where_each_stands(open_panel):
    panel = open_panel(replies())
    expect(panel.find(".cs-said")).to_have_text(["Turn on the porch light.", "Open the garage.", "What's the weather?",
                                                 "Warm up the car."])
    expect(panel.find(".cs-row").nth(0).locator(".chip")).to_have_text("New")  # Not looked at yet: it can be sent in.
    expect(panel.find(".cs-row").nth(0).locator(".cs-failed")).to_have_text("1 failed step")
    expect(panel.find(".cs-row").nth(1).locator(".chip")).to_have_text("Looking for a fix")
    expect(panel.find(".cs-row").nth(1).locator("[data-cs-pick]")).to_have_count(1)  # To archive or delete, not to run.
    expect(panel.find(".cs-row").nth(2).locator(".chip")).to_have_text("Sent in")
    expect(panel.find(".cs-top .info-tip")).to_have_attribute(
        "data-tip", "Pick the ones that didn't go well, and run. Any you send in, or file with Assist, go straight to the improver.")
    assert panel.calls("conversations")[0] == {"type": "assist_improver/conversations", "offset": 0}  # Order: automatic.


def test_conversations_are_found_by_what_was_said_and_when(open_panel):
    panel = open_panel(replies())
    panel.find("#cs-search").fill("porch")
    panel.page.wait_for_timeout(500)
    assert panel.calls("conversations")[-1]["query"] == "porch"
    panel.click("[data-days='7']")
    assert panel.calls("conversations")[-1]["days"] == 7
    panel.click("#cs-sort")
    panel.click("[data-order='effort']")
    assert panel.calls("conversations")[-1]["order"] == "effort"
    expect(panel.find("#cs-sort")).to_have_text("Most back-and-forth▾")
    expect(panel.find(".cs-sort-menu")).to_have_count(0)  # Chosen: the menu closes.


def test_when_no_conversation_has_every_word_the_closest_show_and_say_so(open_panel):
    panel = open_panel(replies(conversations={**listing(CHATS[:1]), "close": True}))
    expect(panel.find(".cs-close")).to_have_text("No conversation has all those words. The closest:")


def test_a_conversation_opens_to_read_and_is_sent_in_alone_with_a_note(open_panel):
    panel = open_panel(replies(run=status(run(), **NEW)))
    panel.click("[data-cs='c1']")
    expect(panel.find(".cs-turn")).to_have_count(2)
    expect(panel.find(".cs-you").first).to_have_text("You Turn on the porch light.")
    expect(panel.find(".cs-assist").first).to_have_text("Assist Which porch light?")
    panel.find("#cs-note").fill("It should know which porch.")
    panel.click("#cs-send")
    assert panel.calls("conversation/send") == [{"type": "assist_improver/conversation/send", "recording_id": "c1",
                                                 "note": "It should know which porch."}]
    expect(panel.find(".cs-done")).to_have_text("Sent in. The improver is looking at it now.")
    expect(panel.find(".cs-row").nth(0).locator(".chip")).to_have_text("Sent in")
    expect(panel.find("#cs-send")).to_have_count(0)  # Sent: once only.
    expect(panel.find("#cs-run")).to_have_text("Run on 3 new conversations")  # None picked: every new one.


FAILED_CHAT = {**chat(), "turns": [
    {"you": "Turn on the porch light.", "assist": "Done: the porch light is on.\n\nThe garden light stayed off.",
     "failed": [{"step": "Turn on · Porch · light", "error": "No entities matched the name 'Porch'"}]},
    {"you": "Thanks.", "assist": "Anytime."}]}


@pytest.mark.parametrize("size", [PHONE, DESKTOP], ids=["phone", "desktop"])
def test_a_conversation_reads_like_assists_own_dialog_with_each_failed_step_where_it_happened(open_panel, size):
    panel = open_panel(replies(conversation=FAILED_CHAT), size=size)
    panel.click("[data-cs='c1']")
    first = panel.find(".cs-turn").first
    expect(first.locator(".cs-you .cs-who")).to_have_text("You")
    expect(first.locator(".cs-you .cs-says")).to_have_text("Turn on the porch light.")
    expect(first.locator(".cs-step")).to_have_text(
        "! Failed step: Turn on · Porch · light No entities matched the name 'Porch'")  # Between what was said and the reply.
    expect(first.locator(".cs-assist .cs-says p")).to_have_text(["Done: the porch light is on.", "The garden light stayed off."])
    expect(panel.find(".cs-turn").nth(1).locator(".cs-step")).to_have_count(0)
    you, assist = (first.locator(css).bounding_box() for css in (".cs-you .cs-says", ".cs-assist .cs-says"))
    assert you["x"] + you["width"] > assist["x"] + assist["width"] and you["x"] > assist["x"]  # You on the right.
    assert panel.page.evaluate("document.scrollingElement.scrollWidth <= document.scrollingElement.clientWidth")


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_a_conversation_and_its_failed_steps_read_clearly_in_light_and_dark(open_panel, dark):
    panel = open_panel(replies(conversation=FAILED_CHAT), dark=dark)
    panel.click("[data-cs='c1']")
    assert not low_contrast(panel, [".cs-you .cs-says p", ".cs-assist .cs-says p", ".cs-step-what"], 4.5)
    assert not low_contrast(panel, [".cs-step-label", ".cs-step-error", ".cs-who"], 3)


def test_a_failed_step_is_escaped(open_panel):
    panel = open_panel(replies(conversation={**chat(), "turns": [{"you": XSS, "assist": XSS,
                                                                  "failed": [{"step": XSS, "error": XSS}]}]}))
    panel.click("[data-cs='c1']")
    expect(panel.find(".cs-step-what")).to_contain_text(XSS)
    expect(panel.find(".cs-step-error")).to_have_text(XSS)
    assert_escaped(panel)


STAGES = [("queued", "To analyze"), ("fine", "No trouble found"), ("working", "Looking for a fix"),
          ("review", "Up for review"), ("declined", "Declined"), ("duplicate", "Already covered"), ("improved", "Improved"),
          ("test", "Marked as a test"), ("unreadable", "Couldn't be read")]
# A row for each, with the longest first words the list shows on a phone.
STAGED = [{"id": f"s{n}", "started": ago(5 + n), "turns": 4, "said": "Turn on the lights in the front porch and the garden.",
           "reply": "Which porch light do you mean?", "status": status, "note": None, "failed": 2,
           **({"review": "imp-1"} if status == "review" else {})} for n, (status, _) in enumerate(STAGES)]


def test_each_conversation_a_run_took_in_says_the_stage_its_at(open_panel):
    panel = open_panel(replies(conversations=listing(STAGED)))
    expect(panel.find(".cs-row .chip")).to_have_text([label for _, label in STAGES])
    panel.click("#cs-all")
    expect(panel.find("#cs-run")).to_have_count(0)  # None can go in again: only Archive and Delete apply.
    expect(panel.find("#cs-many-archive")).to_have_text("Archive (8)")  # Not the one up for review: decided instead.
    expect(panel.find("#cs-many-ask")).to_have_text("Delete (9)")
    panel.click("#cs-all")
    expect(panel.find("button.chip.cs-review")).to_have_count(1)
    panel.click("button.chip.cs-review")  # Up for review: the chip opens that review.
    assert "id=imp-1" in panel.path()
    expect(panel.find("h1")).to_be_visible()


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_no_trouble_found_is_quiet_like_declined_and_still_reads(open_panel, dark):
    panel = open_panel(replies(conversations=listing(STAGED)), dark=dark)
    color = lambda css: panel.find(css).evaluate("(el) => getComputedStyle(el).color + ' ' + getComputedStyle(el).borderColor")  # noqa: E731
    assert color(".chip.cs-fine") == color(".chip.cs-declined")  # Nothing for the household to do: not green.
    assert color(".chip.cs-fine") != color(".chip.cs-improved")
    assert not low_contrast(panel, [".chip.cs-fine"], 3)


@pytest.mark.parametrize("size", [PHONE, DESKTOP], ids=["phone", "desktop"])
def test_every_stage_fits_its_row_beside_what_was_said(open_panel, size):
    panel = open_panel(replies(conversations=listing(STAGED)), size=size)
    for row in panel.find(".cs-row").all():
        line, chip, said = (row.locator(css).first.bounding_box() for css in (".cs-line", ".chip", ".cs-said"))
        assert chip["x"] + chip["width"] <= line["x"] + line["width"] + 0.5, (row.inner_text(), chip, line)
        assert chip["height"] < 30, row.inner_text()  # One line.
        assert said["width"] >= 120, (row.inner_text(), said)  # What was said still reads.
    assert_no_horizontal_scroll(panel)


def test_a_conversation_the_improver_has_cant_be_sent_again(open_panel):
    panel = open_panel(replies(conversation=chat("working", id="c2")))
    panel.click("[data-cs='c2']")
    expect(panel.find(".cs-turn")).to_have_count(2)
    expect(panel.find("#cs-send")).to_have_count(0)
    panel.click("#cs-actions")  # It can still be archived, or deleted.
    expect(panel.find("#cs-archive")).to_be_visible()


def test_a_conversation_whose_improvement_waits_for_the_household_offers_delete_but_not_archive(open_panel):
    """Up for review (unarchived back into the list), or looking for a fix while the earlier version waits during its
    revision's round: deciding it is the way out (SPEC.md P0.11), so the record and Your review never part (P0.2)."""
    for status in ("review", "working"):
        waits = {"status": status, "archivable": False, **({"review": "imp-1"} if status == "review" else {})}
        panel = open_panel(replies(conversations=listing([{**CHATS[0], **waits}]), conversation={**chat(), **waits}))
        panel.click("[data-cs='c1']")
        panel.click("#cs-actions")
        expect(panel.find("#cs-delete-ask")).to_be_visible()
        expect(panel.find("#cs-archive")).to_have_count(0)
        expect(panel.find("#cs-unarchive")).to_have_count(0)


# Archive (the improver keeps using it) and Delete from Assist Improver, in an opened conversation.

def test_an_opened_conversation_is_archived_out_of_the_list_and_comes_back_with_show_archived(open_panel):
    panel = open_panel(replies())
    panel.click("[data-cs='c1']")
    expect(panel.find("#cs-archive")).to_have_count(0)  # Behind the ⋮ until asked for.
    panel.click("#cs-actions")
    expect(panel.find("#cs-archive")).to_have_text("Archive this conversation")
    expect(panel.find("#cs-delete-ask")).to_have_text("Delete from Assist Improver")
    panel.click("#cs-menu-close")  # And closed again, by a click outside.
    expect(panel.find(".cs-menu")).to_have_count(0)
    expect(panel.find(".cs-controls .cs-all")).to_have_text("Select all 4")
    expect(panel.find(".cs-show-archived")).to_have_count(0)  # Nothing archived yet.
    panel.click("#cs-actions")
    panel.answer("conversations", listing(CHATS[1:], hidden=1))  # As Home Assistant lists them once it's archived.
    panel.click("#cs-archive")
    assert panel.calls("conversation/hide") == [{"type": "assist_improver/conversation/hide", "recording_id": "c1",
                                                 "hidden": True}]
    expect(panel.find("[data-cs='c1']")).to_have_count(0)
    expect(panel.find(".cs-turn")).to_have_count(0)
    expect(panel.find(".cs-controls .cs-all")).to_have_text("Select all 3")  # The rest.
    expect(panel.find(".cs-show-archived")).to_have_text("Show archived (1)")
    panel.answer("conversations?archived", listing([{**CHATS[0], "status": "archived", "hidden": True}], hidden=1))
    panel.click("#cs-archived")
    assert panel.calls("conversations")[-1]["archived"] is True
    expect(panel.find(".cs-row.cs-archived [data-cs='c1']")).to_have_count(1)
    expect(panel.find(".cs-row:has([data-cs='c1']) .cs-head .chip")).to_have_text("Archived")
    expect(panel.find("#cs-archived")).to_be_checked()
    panel.answer("conversation", {**chat("archived"), "hidden": True})
    panel.click("[data-cs='c1']")
    panel.click("#cs-actions")
    expect(panel.find("#cs-unarchive")).to_have_text("Unarchive")
    expect(panel.find("#cs-archive")).to_have_count(0)
    # Back where its record puts it: new, at Your Assist; none archived now.
    panel.answer("conversations", listing(CHATS, stands=[CHATS[0]]))
    panel.answer("conversations?archived", listing([]))
    panel.click("#cs-unarchive")
    assert panel.calls("conversation/hide")[-1] == {"type": "assist_improver/conversation/hide", "recording_id": "c1",
                                                    "hidden": False}
    expect(panel.find(".cs-row.cs-archived")).to_have_count(0)
    expect(panel.find("[data-cs='c1']")).to_have_count(1)  # Still there, once.
    expect(panel.find(".cs-turn").first).to_be_visible()  # And still open.
    expect(panel.find(".cs-row:has([data-cs='c1']) .cs-head .chip")).to_have_text("New")  # As its record says (P0.2).
    panel.click("#cs-actions")
    expect(panel.find("#cs-archive")).to_be_visible()
    expect(panel.find(".cs-show-archived")).to_have_count(0)  # Nothing archived now.


def test_archived_ones_shown_can_be_picked_and_are_unpicked_when_out_of_view_again(open_panel):
    out_of_view = listing(CHATS[1:], hidden=1)
    panel = open_panel(replies(conversations=out_of_view, run=status(run(), **NEW)))
    expect(panel.find("[data-cs='c1']")).to_have_count(0)
    expect(panel.find("#cs-run")).to_have_text("Run on 3 new conversations")  # None picked: every new one, c1 too.
    panel.answer("conversations?archived", listing([{**CHATS[0], "hidden": True}], hidden=1))
    panel.click("#cs-archived")
    panel.click("[data-cs-pick='c1']")
    expect(panel.find("#cs-run")).to_have_text("Run on 1 conversation")
    calls = len(panel.calls("conversations"))
    panel.click("#cs-archived")
    assert len(panel.calls("conversations")) == calls  # They just go: the list isn't read again.
    expect(panel.find("[data-cs='c1']")).to_have_count(0)
    expect(panel.find("#cs-run")).to_have_text("Run on 3 new conversations")  # Unpicked out of view: a full run again.


@pytest.mark.parametrize("size", [PHONE, DESKTOP], ids=["phone", "desktop"])
@pytest.mark.parametrize("listed", [[], CHATS], ids=["nothing-needs-you", "some-listed"])
def test_show_archived_opens_them_below_it_so_ticking_it_moves_nothing(open_panel, size, listed):
    """Kai (2026-10-29): the archived ones are revealed below Show archived, so it can be ticked and unticked without
    moving where you tap, and the screen doesn't move."""
    panel = open_panel(replies(conversations=listing(listed, hidden=3)), size=size)
    panel.answer("conversations?archived", listing(SETTLED_CHATS, hidden=3))
    box = panel.find("#cs-archived")
    box.evaluate("(el) => el.scrollIntoView({block: 'center'})")
    panel.settle()
    where = lambda: [panel.find(css).bounding_box()["y"] for css in ("#cs-archived", ".cs-title")]
    before = where()
    panel.click("#cs-archived")
    expect(panel.find(".cs-away .cs-row")).to_have_count(3)
    assert where() == before
    assert panel.find(".cs-away .cs-row").first.bounding_box()["y"] > before[0]  # Below it.
    panel.click("#cs-archived")
    expect(panel.find(".cs-away")).to_have_count(0)
    assert where() == before


@pytest.mark.parametrize("size", [PHONE, DESKTOP], ids=["phone", "desktop"])
def test_ticking_show_archived_moves_nothing_with_more_shown_or_one_open(open_panel, size):
    panel = open_panel(replies(conversations={**listing(CHATS[:2], hidden=3), "total": 4}), size=size)
    panel.answer("conversations", listing(CHATS[2:], hidden=3))
    panel.click("#cs-more")  # The list paged past its first page.
    panel.answer("conversation", chat())
    panel.click("[data-cs='c1']")  # And one open.
    panel.answer("conversations?archived", listing(SETTLED_CHATS, hidden=3))
    box = panel.find("#cs-archived")
    box.evaluate("(el) => el.scrollIntoView({block: 'center'})")
    panel.settle()
    where = lambda: [panel.find(css).bounding_box()["y"] for css in ("#cs-archived", ".cs-title")]
    before, calls = where(), len(panel.calls("conversations"))
    panel.click("#cs-archived")
    expect(panel.find(".cs-away .cs-row")).to_have_count(3)
    assert where() == before
    assert [c.get("archived") for c in panel.calls("conversations")[calls:]] == [True]  # Only the archived ones read.
    expect(panel.find(".cs-list").first.locator(".cs-row")).to_have_count(4)
    expect(panel.find(".cs-turn").first).to_be_visible()  # Still open.
    panel.click("#cs-archived")
    expect(panel.find(".cs-away")).to_have_count(0)
    assert where() == before


def test_show_archived_ticked_while_the_list_pages_shows_both(open_panel):
    panel = open_panel(replies(conversations={**listing(CHATS[:2], hidden=3), "total": 4}))
    panel.answer("conversations?archived", listing(SETTLED_CHATS, hidden=3))
    panel.hold("conversations?archived")
    panel.find("#cs-archived").click()  # Its reply still on the way...
    panel.answer("conversations", listing(CHATS[2:], hidden=3))
    panel.find("#cs-more").click()  # ...when the list's next page is asked for.
    expect(panel.find(".cs-list").first.locator(".cs-row")).to_have_count(4)
    panel.release("conversations?archived")
    expect(panel.find(".cs-away .cs-row")).to_have_count(3)  # Both landed.


def test_with_every_conversation_archived_the_list_says_nothing_needs_you_and_offers_show_archived(open_panel):
    panel = open_panel(replies(conversations=listing([], hidden=4)))
    expect(panel.find(".cs-empty")).to_have_text("Nothing needs you right now.")
    expect(panel.find(".cs-show-archived")).to_have_text("Show archived (4)")
    panel.answer("conversations", listing([], hidden=4))
    panel.click("[data-days='7']")  # A filter on that matches none: as before.
    expect(panel.find(".cs-empty")).to_have_text("No conversations match.")


@pytest.mark.parametrize("dark", [True, False], ids=["dark", "light"])
@pytest.mark.parametrize("size", [PHONE, DESKTOP], ids=["phone", "desktop"])
@pytest.mark.parametrize("waiting", [{"improvements": [], "questions": [QUESTION]}, {"improvements": PENDING[:1]}],
                         ids=["question", "improvement"])
def test_with_something_waiting_at_the_top_and_every_conversation_archived_no_line_says_nothing_needs_you(
        open_panel, size, dark, waiting):
    """A question, or an improvement up for review, needs the household: under Your conversations, no line says
    otherwise; Show archived stays."""
    panel = open_panel(replies(improvements=waiting, conversations=listing([], hidden=4)), size=size, dark=dark)
    expect(panel.find("h1")).to_have_text("1 question for you" if waiting.get("questions") else "1 improvement to review")
    expect(panel.find(".cs-show-archived")).to_have_text("Show archived (4)")
    expect(panel.find(".cs-empty")).to_have_count(0)
    assert_no_horizontal_scroll(panel)
    if (shots := os.environ.get("UI_SHOTS")) and waiting.get("questions"):  # A visual check, in each view.
        view = f"{'phone' if size == PHONE else 'desktop'}-{'dark' if dark else 'light'}"
        panel.page.screenshot(path=f"{shots}/question-all-archived-{view}.png", full_page=True)


def test_none_matching_is_said_once_and_below_show_archived_only_when_the_list_has_some(open_panel):
    panel = open_panel(replies(conversations=listing([], hidden=3)))
    panel.answer("conversations?archived", listing(SETTLED_CHATS, hidden=3))
    panel.click("#cs-archived")
    panel.answer("conversations?archived", listing([], hidden=3))
    panel.click("[data-days='7']")  # Neither list has any: said once, above Show archived, for both.
    expect(panel.find(".cs-empty")).to_have_text(["No conversations match."])
    expect(panel.find(".cs-away .cs-empty")).to_have_count(0)
    panel.answer("conversations", listing(CHATS[:2], hidden=3))
    panel.click("[data-days='30']")  # The list has some, the archived ones none: said below Show archived.
    expect(panel.find(".cs-empty")).to_have_text(["No conversations match."])
    expect(panel.find(".cs-away .cs-empty")).to_have_count(1)


# Settled ones (No trouble found, Improved, No fix found...) leave the list on their own: Home Assistant lists them as
# left out, from where their flow record says they stand (conversations.row), with those archived by hand.

SETTLED_CHATS = [{"id": "c5", "started": ago(6000), "turns": 1, "said": "Close the blinds.", "status": "fine",
                  "note": None, "failed": 0, "hidden": True},
                 {"id": "c6", "started": ago(7000), "turns": 2, "said": "Good night.", "status": "improved",
                  "note": None, "failed": 0, "hidden": True},
                 {"id": "c7", "started": ago(8000), "turns": 3, "said": "Warm the seats.", "status": "nofix",
                  "note": None, "failed": 0, "hidden": True}]


def close_status_menu(panel):
    panel.find("#cs-status-backdrop").click(position={"x": 5, "y": 5})
    panel.settle()


def test_the_list_shows_those_that_may_need_you_and_settled_ones_only_with_show_archived(open_panel):
    panel = open_panel(replies(conversations=listing(CHATS, hidden=3), run=status(run(), **NEW)))
    expect(panel.find(".cs-row")).to_have_count(4)
    expect(panel.find(".cs-row.cs-archived")).to_have_count(0)
    expect(panel.find(".cs-show-archived")).to_have_text("Show archived (3)")
    expect(panel.find(".cs-controls .cs-all")).to_have_text("Select all 4")
    panel.click("#cs-status")  # The statuses of those listed only.
    expect(panel.find(".cs-status-opt .cs-status-name")).to_have_text(["All", "New", "Sent in", "Looking for a fix"])
    close_status_menu(panel)
    panel.click("#cs-all")
    expect(panel.find(".cs-footer .btn")).to_have_text(["Run on 3 conversations", "Archive (4)", "Delete (4)"])
    panel.click("#cs-all")
    panel.answer("conversations?archived", listing(SETTLED_CHATS, hidden=3))
    panel.click("#cs-archived")
    assert panel.calls("conversations")[-1]["archived"] is True
    expect(panel.find(".cs-row")).to_have_count(7)
    expect(panel.find(".cs-away .cs-row.cs-archived .cs-said")).to_have_text(["Close the blinds.", "Good night.", "Warm the seats."])
    expect(panel.find(".cs-row.cs-archived .chip")).to_have_text(["No trouble found", "Improved", "No fix found"])
    expect(panel.find(".cs-controls .cs-all")).to_have_text("Select all 4")  # The list's, as it was.
    expect(panel.find(".cs-away .cs-all")).to_have_text("Select all 3")  # The archived ones' own.
    panel.click("#cs-status")  # Now the settled statuses can be chosen too.
    expect(panel.find(".cs-status-opt .cs-status-name")).to_have_text(
        ["All", "New", "Sent in", "No trouble found", "Looking for a fix", "No fix found", "Improved"])
    expect(panel.find(".cs-status-opt .cs-status-n")).to_have_text(["2", "1", "1", "1", "1", "1"])
    counts, settled = listing(CHATS)["by_status"], listing(SETTLED_CHATS)["by_status"]
    panel.answer("conversations", listing([], hidden=3, by_status=counts))
    panel.answer("conversations?archived", listing([], hidden=3, by_status=settled))
    panel.click("#cs-status-all")  # None, then one settled status.
    panel.answer("conversations?archived", listing([SETTLED_CHATS[0]], hidden=3, by_status=settled))
    panel.click("[data-cs-status='fine']")
    assert panel.calls("conversations")[-2:] == [
        {"type": "assist_improver/conversations", "offset": 0, "statuses": ["fine"]},
        {"type": "assist_improver/conversations", "offset": 0, "archived": True, "statuses": ["fine"]}]
    expect(panel.find("#cs-status")).to_have_text("No trouble found▾")
    expect(panel.find(".cs-row")).to_have_count(1)
    panel.answer("conversations", listing(CHATS, hidden=3))
    panel.answer("conversations?archived", listing(SETTLED_CHATS, hidden=3))
    panel.click("#cs-status-all")  # All again.
    close_status_menu(panel)
    panel.click("#cs-all")  # Every one shown, both lists; a Run still takes only the new ones.
    panel.click("#cs-all-away")
    expect(panel.find(".cs-footer .btn")).to_have_text(["Run on 3 conversations", "Archive (4)", "Unarchive (3)",
                                                        "Delete (7)"])
    calls = len(panel.calls("conversations"))
    panel.click("#cs-archived")  # Out of view again: the settled ones leave the selection.
    assert len(panel.calls("conversations")) == calls  # They just go: the list isn't read again.
    expect(panel.find(".cs-row")).to_have_count(4)
    expect(panel.find(".cs-footer .btn")).to_have_text(["Run on 3 conversations", "Archive (4)", "Delete (4)"])


def test_the_last_archived_one_sent_in_again_stays_on_screen_saying_so(open_panel):
    panel = open_panel(replies(conversations=listing(CHATS, hidden=1)))
    panel.answer("conversations?archived", listing(SETTLED_CHATS[:1], hidden=1))
    panel.click("#cs-archived")
    panel.answer("conversation", {**chat("fine", id="c5"), "hidden": True})
    panel.click("[data-cs='c5']")
    sent = {**SETTLED_CHATS[0], "status": "sent", "hidden": False}
    panel.answer("conversation/send", {"status": "sent", "hidden": False})
    panel.answer("conversations", listing([*CHATS, sent], stands=[sent]))  # None archived now.
    panel.answer("conversations?archived", listing([]))
    panel.click("#cs-send")
    expect(panel.find(".cs-show-archived")).to_have_count(0)
    expect(panel.find("[data-cs='c5']")).to_have_count(1)
    expect(panel.find(".cs-done")).to_be_visible()


def test_a_settled_one_unarchived_stays_listed_and_one_sent_in_again_is_back_in_the_list(open_panel):
    panel = open_panel(replies(conversations=listing(CHATS, hidden=3), conversation__hide={"hidden": False}))
    panel.answer("conversations?archived", listing(SETTLED_CHATS, hidden=3))
    panel.click("#cs-archived")
    panel.answer("conversation", {**chat("fine", id="c5"), "hidden": True})
    panel.click("[data-cs='c5']")
    panel.click("#cs-actions")
    unarchived = {**SETTLED_CHATS[0], "hidden": False}
    panel.answer("conversations", listing([*CHATS, unarchived], hidden=2, stands=[unarchived]))
    panel.answer("conversations?archived", listing(SETTLED_CHATS[1:], hidden=2))
    panel.click("#cs-unarchive")
    assert panel.calls("conversation/hide")[-1] == {"type": "assist_improver/conversation/hide", "recording_id": "c5",
                                                    "hidden": False}
    expect(panel.find(".cs-row.cs-archived [data-cs='c5']")).to_have_count(0)
    expect(panel.find("[data-cs='c5']")).to_have_count(1)  # Kept in the list, though settled.
    expect(panel.find(".cs-away [data-cs='c5']")).to_have_count(1)  # Where it's read, below Show archived, till closed.
    expect(panel.find(".cs-more")).to_have_count(0)  # The list's own row for it waits: nothing more to show.
    expect(panel.find(".cs-show-archived")).to_have_text("Show archived (2)")
    panel.click("#cs-actions")
    expect(panel.find("#cs-archive")).to_be_visible()
    panel.click("#cs-menu-close")
    panel.answer("conversation", {**chat("nofix", id="c7"), "hidden": True})
    panel.answer("conversation/send", {"status": "sent", "hidden": False})
    panel.click("[data-cs='c7']")
    sent = {**SETTLED_CHATS[2], "status": "sent", "hidden": False}
    panel.answer("conversations", listing([*CHATS, unarchived, sent], hidden=1, stands=[sent]))
    panel.answer("conversations?archived", listing(SETTLED_CHATS[1:2], hidden=1))
    panel.click("#cs-send")
    expect(panel.find(".cs-row.cs-archived")).to_have_count(1)  # Sent in again: back in the list by itself.
    expect(panel.find(".cs-row:has([data-cs='c7']) .chip")).to_have_text("Sent in")
    expect(panel.find(".cs-show-archived")).to_have_text("Show archived (1)")
    expect(panel.find("[data-cs='c7']")).to_have_count(1)
    expect(panel.find(".cs-away .cs-done")).to_be_visible()  # Where it was sent from, saying so.
    expect(panel.find(".cs-more")).to_have_count(0)
    panel.click("#cs-actions")
    expect(panel.find("#cs-archive")).to_be_visible()


def stand(id, status, hidden):
    return {"id": id, "status": status, "hidden": hidden}


def test_a_selected_one_a_run_settles_leaves_the_selection_as_the_list_is_read_again(open_panel):
    writing = {"id": "c5", "started": ago(5000), "turns": 2, "said": "Dim the hall.", "status": "writing",
               "note": None, "failed": 0, "held": None}
    panel = open_panel(replies(conversations=listing([*CHATS, writing]), run=status(run(), **NEW)))
    panel.click("#cs-all")
    expect(panel.find(".cs-footer .btn")).to_have_text(["Run on 3 conversations", "Archive (5)", "Delete (5)"])
    # The run takes the three it can; Open the garage. ends with no fix found elsewhere: off the list, out of view.
    after = [{**CHATS[0], "status": "queued"}, {**CHATS[2], "status": "queued"}, {**CHATS[3], "status": "queued"},
             writing]
    panel.answer("conversations", listing(after, hidden=1, stands=[stand("c2", "nofix", True),
                                                                   stand("c5", "writing", False)]))
    panel.click("#cs-run")
    panel.click("#start-run")
    assert panel.calls("run/start")[-1]["conversations"] == ["c1", "c3", "c4"]  # Those a run can take.
    assert panel.calls("conversations")[-1]["ids"] == ["c2", "c5"]  # Where the selected ones stand now.
    expect(panel.find(".cs-row")).to_have_count(4)
    expect(panel.find("[data-cs-pick]:checked")).to_have_count(1)  # Still in view: still selected.
    expect(panel.find("#cs-many-archive")).to_have_text("Archive (1)")
    expect(panel.find("#cs-many-ask")).to_have_text("Delete (1)")
    panel.click("#cs-many-ask")
    panel.click("#cs-many-delete")
    assert panel.calls("conversations/delete")[-1]["recording_ids"] == ["c5"]  # Never one out of view.


def test_the_opened_one_stays_in_view_while_it_is_read_though_a_run_settled_it(open_panel):
    panel = open_panel(replies(run=status(run(), **NEW), conversation=chat("working", id="c2")))
    panel.click("[data-cs='c2']")
    panel.click("[data-cs-pick='c1']")
    after = [{**CHATS[0], "status": "queued"}, CHATS[2], CHATS[3]]
    panel.answer("conversations", {**listing(after, hidden=1, stands=[stand("c2", "fine", True)]), "total": 5})
    panel.click("#cs-run")
    panel.click("#start-run")
    assert panel.calls("conversations")[-1]["ids"] == ["c2"]  # The opened one (c1 went in with the run).
    expect(panel.find(".cs-said")).to_have_text(["Turn on the porch light.", "Open the garage.", "What's the weather?",
                                                 "Warm up the car."])  # Where it was.
    expect(panel.find(".cs-row.open.cs-archived .cs-head .chip")).to_have_text("No trouble found")
    panel.click("#cs-actions")
    expect(panel.find("#cs-unarchive")).to_be_visible()  # Archived now: its ⋮ offers Unarchive.
    panel.click("#cs-menu-close")
    panel.answer("conversations", {**listing([], hidden=1, stands=[stand("c2", "fine", True)]), "total": 5})
    panel.click("#cs-more")
    assert panel.calls("conversations")[-1]["offset"] == 3  # Past those listed, not the one kept in view.
    panel.click("[data-cs='c2']")  # Closed: it goes once the list is read again.
    panel.answer("conversations", listing(after, hidden=1))
    panel.click("[data-days='7']")
    expect(panel.find("[data-cs='c2']")).to_have_count(0)


def test_show_more_bringing_the_one_kept_in_view_lists_it_once(open_panel):
    """Kept in view after a reload took it off the first page, then brought by Show more: one row, not two."""
    panel = open_panel(replies(run=status(run(), **NEW), conversation=chat("working", id="c2")))
    panel.click("[data-cs='c2']")
    panel.click("[data-cs-pick='c1']")
    after = [{**CHATS[0], "status": "queued"}, CHATS[2], CHATS[3]]
    panel.answer("conversations", {**listing(after, stands=[stand("c2", "working", False)]), "total": 4})
    panel.click("#cs-run")
    panel.click("#start-run")
    expect(panel.find("[data-cs='c2']")).to_have_count(1)  # Kept in view while it's read.
    panel.answer("conversations", {**listing([CHATS[1]], stands=[stand("c2", "working", False)]), "total": 4})
    panel.click("#cs-more")
    expect(panel.find("[data-cs='c2']")).to_have_count(1)
    expect(panel.find(".cs-row.open")).to_have_count(1)  # Drawn open once: no duplicate ids.


def test_delete_from_assist_improver_asks_first_then_the_conversation_is_gone(open_panel):
    panel = open_panel(replies())
    panel.click("[data-cs='c1']")
    panel.click("#cs-actions")
    panel.click("#cs-delete-ask")
    expect(panel.find(".cs-confirm span")).to_have_text(
        "Delete this conversation from Assist Improver? The improver can no longer check changes against it.")
    expect(panel.find("#cs-archive")).to_have_count(0)
    panel.click("#cs-delete-cancel")
    expect(panel.find(".cs-confirm")).to_have_count(0)
    assert not panel.calls("conversation/delete")
    panel.click("#cs-actions")
    panel.click("#cs-delete-ask")
    panel.click("#cs-delete")
    assert panel.calls("conversation/delete") == [{"type": "assist_improver/conversation/delete", "recording_id": "c1"}]
    expect(panel.find("[data-cs='c1']")).to_have_count(0)
    expect(panel.find(".cs-row")).to_have_count(3)
    expect(panel.find(".cs-controls .cs-all")).to_have_text("Select all 3")  # The rest.


@pytest.mark.parametrize("talk", ["short", "long"])
def test_a_conversation_that_cant_be_deleted_says_why_and_stays(open_panel, talk):
    """Why it couldn't is said in view, with the question, however long the conversation."""
    shown = chat() if talk == "short" else {**chat(), "turns": LONG_TALK}
    panel = open_panel(replies(conversation=shown, conversation__delete=fail("That conversation isn't kept.")))
    panel.click("[data-cs='c1']")
    panel.page.wait_for_timeout(700)
    panel.click("#cs-actions")
    panel.click("#cs-delete-ask")
    panel.click("#cs-delete")
    why = panel.find(".cs-read .conflict")
    expect(why).to_have_text("That conversation isn't kept.")
    expect(why).to_be_in_viewport(ratio=1)
    expect(panel.find(".cs-turn")).to_have_count(2 if talk == "short" else 12)


def test_deleting_during_a_run_takes_the_conversation_out_of_view_at_once(open_panel):
    """Home Assistant queues it and deletes it once the run ends; nothing to wait for, nothing to say."""
    panel = open_panel(replies(run=status(run("running", stage="analyze")),
                               conversation__delete={"deleted": "c1", "queued": True}))
    panel.click("[data-cs='c1']")
    panel.click("#cs-actions")
    panel.click("#cs-delete-ask")
    panel.click("#cs-delete")
    assert panel.calls("conversation/delete") == [{"type": "assist_improver/conversation/delete", "recording_id": "c1"}]
    expect(panel.find("[data-cs='c1']")).to_have_count(0)
    expect(panel.find(".conflict")).to_have_count(0)


@pytest.mark.parametrize("size", [PHONE, DESKTOP], ids=["phone", "desktop"])
def test_send_sits_with_the_note_and_archive_and_delete_wait_behind_its_dots(open_panel, size):
    """Send this one in looks like Run (filled, with an icon); beside it, a ⋮ opens Archive and Delete (red), two rows
    with their icons. The row stays calm, and nothing scrolls sideways, on a phone or a desktop."""
    panel = open_panel(replies(), size=size)
    panel.click("[data-cs='c1']")
    send, dots, note = (panel.find(css) for css in ("#cs-send", "#cs-actions", "#cs-note"))
    expect(send).to_have_text("Send this one in")
    expect(send.locator("svg.ic")).to_have_count(1)
    expect(dots).to_have_attribute("aria-label", "More actions")
    s_box, d_box, n_box = (b.bounding_box() for b in (send, dots, note))
    assert abs((s_box["y"] + s_box["height"] / 2) - (d_box["y"] + d_box["height"] / 2)) < 2  # On Send's line.
    if size == DESKTOP:
        assert round(s_box["y"]) == round(n_box["y"]) and n_box["width"] >= 400, (s_box, n_box)
    dots.click()
    items = panel.find(".cs-menu [role=menuitem]")
    expect(items).to_have_text(["Archive this conversation", "Delete from Assist Improver"])
    for n in range(2):
        expect(items.nth(n).locator("svg.ic")).to_have_count(1)
    menu, read = panel.find(".cs-menu").bounding_box(), panel.find(".cs-read").bounding_box()
    assert menu["x"] >= read["x"] - 0.5 and menu["x"] + menu["width"] <= read["x"] + read["width"] + 0.5, (menu, read)
    assert_no_horizontal_scroll(panel)
    panel.click("#cs-menu-close")  # A click outside closes it.
    expect(panel.find(".cs-menu")).to_have_count(0)
    panel.click("#cs-actions")
    panel.click("#cs-delete-ask")
    expect(panel.find("#cs-delete")).to_have_text("Delete from Assist Improver")
    assert_no_horizontal_scroll(panel)


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_send_archive_and_delete_read_clearly_in_light_and_dark(open_panel, dark):
    panel = open_panel(replies(), dark=dark)
    panel.click("[data-cs='c1']")
    panel.click("#cs-actions")
    assert not low_contrast(panel, ["#cs-archive", "#cs-delete-ask"], 4.5)
    assert not low_contrast(panel, ["#cs-send"], 3)  # Home Assistant's own primary colour, as Run is.
    panel.click("#cs-delete-ask")
    assert not low_contrast(panel, ["#cs-delete", ".cs-confirm span"], 4.5)


def test_run_on_picked_conversations_asks_first_then_takes_them_in(open_panel):
    panel = open_panel(replies(run=status(run(), **NEW, held={"triage": 3})))
    panel.click("[data-cs-pick='c1']")
    panel.click("[data-cs-pick='c4']")
    panel.click("#cs-run")
    expect(panel.find(".modal-head b")).to_have_text("Run on 2 conversations?")  # Not the 3 held at Triage.
    expect(panel.find(".modal .c-what")).to_have_count(0)
    expect(panel.find(".modal .c-which")).to_have_text(  # Which: the picked ones (P0.7).
        "“Turn on the porch light.” and “Warm up the car.”")
    expect(panel.find(".modal .cost")).to_have_text("Runs use your AI models, so they cost money.")
    panel.answer("run", status(run("running", stage="analyze")))
    panel.click("#start-run")
    assert panel.calls("run/start") == [{"type": "assist_improver/run/start", "confirmed": True,
                                         "conversations": ["c1", "c4"]}]
    expect(panel.find(".run .title")).to_have_text("Improving your assistant")
    expect(panel.find(".cs-footer")).to_have_count(0)  # While it runs.


def test_one_the_quick_check_found_smooth_can_be_picked_or_sent_in_but_not_by_select_all(open_panel):
    smooth = {**CHATS[3], "status": "smooth"}  # “Warm up the car.”
    rows = [CHATS[0], CHATS[1], smooth, {**CHATS[0], "id": "c5", "said": "Close the blinds."}]
    panel = open_panel(replies(conversations=listing(rows), conversation=chat("smooth", id="c4")))
    chip = panel.find(".cs-row").nth(2).locator(".chip")
    expect(chip).to_have_text("Looked smooth")
    expect(chip).to_have_attribute("title", "Nothing to improve, at a quick look. Send it in for a full analysis.")
    panel.click("#cs-all")  # Selected, but not run by Select all: each would be a full analysis.
    expect(panel.find("[data-cs-pick='c4']")).to_be_checked()
    expect(panel.find("#cs-run")).to_have_text("Run on 2 conversations")
    panel.click("[data-cs-pick='c4']")
    panel.click("[data-cs-pick='c4']")  # Ticked by hand, it can.
    expect(panel.find("#cs-run")).to_have_text("Run on 3 conversations")
    panel.click("[data-cs='c4']")
    expect(panel.find("#cs-send")).to_have_text("Send this one in")


def test_one_with_no_fix_found_says_so_quietly_and_is_sent_in_again_only_opened_with_a_note(open_panel):
    nofix = {**CHATS[3], "status": "nofix"}  # “Warm up the car.”
    rows = [CHATS[0], nofix]
    panel = open_panel(replies(conversations=listing(rows), conversation=chat("nofix", id="c4"),
                               conversation__send={"status": "sent", "hidden": False}))
    chip = panel.find(".cs-row").nth(1).locator(".chip")
    expect(chip).to_have_text("No fix found")
    expect(chip).to_have_attribute("title", "No fix worked. Send it in again with a note for one more look.")
    assert "cs-nofix" in classes(chip)  # Quiet grey, as Declined.
    expect(chip).to_have_css("color", panel.find(".cs-row").nth(1).locator(".cs-sub").evaluate(
        "el => getComputedStyle(el).color"))
    panel.click("#cs-all")  # Selected (to archive or delete), but never run: it goes in opened, with a note.
    expect(panel.find("#cs-run")).to_have_text("Run on 1 conversation")
    panel.click("[data-cs-pick='c4']")
    panel.click("[data-cs-pick='c4']")
    expect(panel.find("#cs-run")).to_have_text("Run on 1 conversation")
    panel.click("[data-cs='c4']")
    panel.find("#cs-note").fill("It should warm the seats too.")
    expect(panel.find("#cs-send")).to_have_text("Send this one in")
    panel.click("#cs-send")
    panel.settle()
    assert panel.calls("conversation/send")[-1] == {"type": "assist_improver/conversation/send", "recording_id": "c4",
                                                    "note": "It should warm the seats too."}
    expect(panel.find(".cs-done")).to_have_text("Sent in. The improver is looking at it now.")


def test_already_covered_and_held_say_why_in_their_tooltips(open_panel):
    """Already covered: another conversation's improvement covers it (P0.12), and it can be sent in again. Held, a
    chip names its stage and its tooltip says why (flow.py's words), and it can be picked for a run (P0.7)."""
    covered = {**CHATS[1], "status": "duplicate", "covered_by": "imp-9"}  # “Open the garage.”
    held = {**CHATS[2], "status": "queued", "held": {"cause": "blocked", "why": "Analyze is blocked."}}
    panel = open_panel(replies(conversations=listing([CHATS[0], covered, held]),
                               conversation=chat("duplicate", id="c2")))
    chips = panel.find(".cs-row .chip")
    expect(chips.nth(1)).to_have_text("Already covered")
    expect(chips.nth(1)).to_have_attribute("title", "Another conversation's improvement covers it.")
    expect(chips.nth(2)).to_have_text("To analyze")
    expect(chips.nth(2)).to_have_attribute("title", "Held: Analyze is blocked.")
    panel.click("[data-cs-pick='c3']")
    expect(panel.find("#cs-run")).to_have_text("Run on 1 conversation")
    panel.click("[data-cs='c2']")
    expect(panel.find(".cs-turn").first).to_be_visible()
    expect(panel.find("#cs-send")).to_have_text("Send this one in")


def test_the_status_filter_lists_no_fix_found_and_already_covered_where_they_come(open_panel):
    rows = [CHATS[0], CHATS[1], {**CHATS[2], "status": "duplicate"}, {**CHATS[3], "status": "nofix"}]
    panel = open_panel(replies(conversations=listing(rows)))
    panel.click("#cs-status")
    expect(panel.find(".cs-status-opt .cs-status-name")).to_have_text(
        ["All", "New", "Looking for a fix", "No fix found", "Already covered"])
    expect(panel.find(".cs-status-opt .cs-status-n")).to_have_text(["1", "1", "1", "1"])


def test_a_picked_runs_confirmation_names_up_to_three_by_their_first_words_and_counts_the_rest(open_panel):
    said = ["Turn on the porch light and the driveway lights too, please", "Open the garage.", "Warm up the car.",
            "Close the blinds.", "Good night."]
    rows = [{**CHATS[0], "id": f"p{n}", "said": text} for n, text in enumerate(said)]
    panel = open_panel(replies(conversations=listing(rows)))
    panel.click("#cs-all")
    panel.click("#cs-run")
    expect(panel.find(".modal-head b")).to_have_text("Run on 5 conversations?")
    expect(panel.find(".modal .c-which")).to_have_text(
        "“Turn on the porch light and the driveway…”, “Open the garage.”, “Warm up the car.” and 2 more")


def test_the_confirmation_runs_on_enter(open_panel):
    panel = open_panel(replies())
    panel.click("[data-cs-pick='c1']")
    panel.click("#cs-run")
    expect(panel.find("#start-run")).to_be_focused()
    panel.answer("run", status(run("running", stage="analyze")))
    panel.page.keyboard.press("Enter")
    panel.settle()
    assert [c["conversations"] for c in panel.calls("run/start")] == [["c1"]]


def test_a_match_shows_where_the_words_are_marked(open_panel):
    found = {**CHATS[0], "reply": "Which porch light?", "match": "…then turned off the garage lights as well."}
    panel = open_panel(replies(conversations=listing([found])))
    panel.find("#cs-search").fill("garage")
    panel.page.wait_for_timeout(400)
    panel.settle()
    expect(panel.find(".cs-match")).to_have_text("…then turned off the garage lights as well.")
    expect(panel.find(".cs-match mark")).to_have_text("garage")  # Instead of the first reply, where it was found.
    expect(panel.find(".cs-row .cs-reply")).to_have_count(1)


def test_enter_in_the_note_sends_the_conversation_in(open_panel):
    panel = open_panel(replies())
    panel.click("[data-cs='c1']")
    panel.find("#cs-note").fill("It should know which porch.")
    panel.find("#cs-note").press("Enter")
    panel.settle()
    assert [c["note"] for c in panel.calls("conversation/send")] == ["It should know which porch."]
    expect(panel.find(".cs-done")).to_have_text("Sent in. The improver is looking at it now.")


def test_a_time_searched_puts_the_closest_first_and_no_order_is_shown_as_chosen(open_panel):
    panel = open_panel(replies())
    expect(panel.find("#cs-sort")).to_have_text("Newest▾")
    panel.answer("conversations", {**listing([{**CHATS[0], "timed": True}]), "closest": True})
    panel.find("#cs-search").fill("friday 10:40pm")
    panel.page.wait_for_timeout(400)
    panel.settle()
    expect(panel.find("#cs-sort")).to_have_text("Sort▾")  # The closest first: none of the three.
    panel.click("#cs-sort")
    expect(panel.find("[data-order].on")).to_have_count(0)
    panel.click("[data-order='newest']")
    assert panel.calls("conversations")[-1]["order"] == "newest"  # Chosen: newest, as asked.


def test_earlier_improvements_stay_open_on_the_way_back_from_one(open_panel):
    earlier = [{**PENDING[0], "status": "applied", "pending": False}]
    panel = open_panel(replies(improvements={"improvements": earlier}))
    panel.click("details.history > summary")
    panel.click(".item[data-id='imp1']")
    panel.click("#back")
    expect(panel.find("details.history")).to_have_attribute("open", "")


def test_a_search_can_be_cleared_with_one_click(open_panel):
    panel = open_panel(replies())
    expect(panel.find("#cs-search-clear")).to_have_count(0)  # Nothing to clear.
    panel.find("#cs-search").fill("porch")
    panel.page.wait_for_timeout(400)
    panel.settle()
    expect(panel.find("#cs-search-clear")).to_have_attribute("aria-label", "Clear search")
    panel.hold("conversations")  # Home Assistant slow to answer: the box is empty at once all the same.
    panel.find("#cs-search-clear").click()
    expect(panel.find("#cs-search")).to_have_value("")
    expect(panel.find("#cs-search")).to_be_focused()
    expect(panel.find("#cs-search-clear")).to_have_count(0)
    panel.release("conversations")
    assert panel.calls("conversations")[-1].get("query") is None


def test_a_conversation_found_by_when_has_its_time_marked(open_panel):
    panel = open_panel(replies(conversations=listing([{**CHATS[0], "timed": True}, {**CHATS[3], "timed": False}])))
    expect(panel.find(".cs-row .cs-sub mark")).to_have_count(1)
    expect(panel.find(".cs-row").nth(0).locator(".cs-sub mark")).to_be_visible()


def test_a_conversation_an_accepted_improvement_covers_says_improved(open_panel):
    panel = open_panel(replies(conversations=listing([{**CHATS[1], "status": "improved"}])))
    expect(panel.find(".cs-row .chip")).to_have_text("Improved")
    panel.click("[data-cs-pick]")
    expect(panel.find("#cs-run")).to_have_count(0)  # Nothing left to send in.


def test_the_sent_in_message_goes_once_the_improver_has_it(open_panel):
    panel = open_panel(replies(conversation=chat()))
    panel.click("[data-cs='c1']")
    panel.click("#cs-send")
    expect(panel.find(".cs-done")).to_have_text("Sent in. The improver is looking at it now.")
    panel.click("[data-cs='c1']")  # Closed, and opened again once the improver has it.
    panel.answer("conversation", chat("queued"))
    panel.click("[data-cs='c1']")
    expect(panel.find(".cs-turn")).to_have_count(2)
    expect(panel.find(".cs-done")).to_have_count(0)


def test_with_nothing_picked_the_home_pages_run_is_the_full_run_settings_offers(open_panel):
    """Kai: the same count, the same run (no picks), and the same confirmation as Settings' Run."""
    full = status(run(), admits=16, held={"triage": 2, "write": 1},
                  which=["Turn on the porch light.", "Open the garage.", "What's the weather?"],
                  spend={"analyze": OPUS_SPEND, "triage": HAIKU_SPEND})
    panel = open_panel(replies(run=full))
    expect(panel.find("#cs-run")).to_have_text("Run on 16 new conversations")
    panel.click("#cs-run")
    expect(panel.find(".modal-head b")).to_have_text("Run on 16 new conversations?")
    expect(panel.find(".modal .c-which")).to_have_text(
        "“Turn on the porch light.”, “Open the garage.”, “What's the weather?” and 13 more")
    expect(panel.find(".modal .c-also")).to_have_text("Also: 2 conversations to triage · 1 write-up")
    expect(panel.find(".modal .c-spend li")).to_have_count(2)  # A full run gives the new ones the quick check.
    panel.click("#start-run")
    assert panel.calls("run/start") == [{"type": "assist_improver/run/start", "confirmed": True}]
    settings = open_settings(open_panel, run=full)
    expect(settings.find("#run-now")).to_have_text("Run on 16 new conversations")  # As Settings says it.


def test_a_picked_run_takes_only_the_picked_and_leaves_the_rest_waiting(open_panel):
    # c1 and c4 are held at Triage; c3 is held at Analyze. Picking c1 runs on it alone; the others, not picked, wait
    # for a run that releases them (P0.7, P0.8).
    held = {"cause": "cut off", "why": "The run was cut off before this was done."}
    rows = [{**CHATS[0], "held": held}, CHATS[1], {**CHATS[2], "status": "queued", "held": held},
            {**CHATS[3], "held": held}]
    panel = open_panel(replies(conversations=listing(rows), run=status(run(), held={"triage": 2, "analyze": 1})))
    expect(panel.find("#cs-run")).to_have_text("Run what's waiting")  # None picked: every one held.
    panel.click("[data-cs-pick='c1']")
    expect(panel.find("#cs-run")).to_have_text("Run on 1 conversation")
    panel.click("#cs-run")
    expect(panel.find(".modal-head b")).to_have_text("Run on 1 conversation?")
    expect(panel.find(".modal .c-which")).to_have_text("“Turn on the porch light.”")


def test_sending_one_in_brings_the_run_bar_up_to_date(open_panel):
    panel = open_panel(replies(run=status(run(), admits=2, held={"triage": 1})))
    panel.click("[data-cs-pick='c4']")
    expect(panel.find("#cs-run")).to_have_text("Run on 1 conversation")  # c4, picked.
    panel.click("[data-cs='c1']")
    panel.answer("run", status(run(), admits=3, held={"triage": 1}))  # What Home Assistant says now.
    calls = len(panel.calls("run"))
    panel.click("#cs-send")
    assert len(panel.calls("run")) == calls + 1
    expect(panel.find("#cs-run")).to_have_text("Run on 1 conversation")  # Still just the one picked.
    panel.click("[data-cs-pick='c4']")
    expect(panel.find("#cs-run")).to_have_text("Run on 3 new conversations")  # None picked: a full run, as it is now.


def test_not_now_keeps_what_was_picked(open_panel):
    panel = open_panel(replies())
    panel.click("[data-cs-pick='c1']")
    panel.click("#cs-run")
    panel.click("#cancel-confirm-2")
    expect(panel.find(".modal")).to_have_count(0)
    expect(panel.find("[data-cs-pick='c1']")).to_be_checked()
    assert not panel.calls("run/start")


@pytest.mark.parametrize("size", [PHONE], ids=["phone"])
def test_the_home_page_fits_a_phone_with_a_conversation_open_and_some_picked(open_panel, size):
    panel = open_panel(replies(), size=size)
    panel.click("[data-cs-pick='c4']")
    panel.click("[data-cs='c1']")
    expect(panel.find(".cs-turn")).to_have_count(2)
    assert panel.page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


def test_an_opened_conversation_follows_its_row_once_a_run_takes_it_in(open_panel):
    taken = listing([{**CHATS[0], "status": "queued"}, *CHATS[1:]])
    # Sent in, then taken in by a run: what sending it in said goes.
    panel = open_panel(replies(run=status(run(), **NEW)))
    panel.click("[data-cs='c1']")
    panel.click("#cs-send")
    expect(panel.find(".cs-done")).to_have_text("Sent in. The improver is looking at it now.")
    panel.answer("conversations", taken)
    panel.click("#cs-run")
    panel.click("#start-run")
    expect(panel.find(".cs-row").nth(0).locator(".chip")).to_have_text("To analyze")
    expect(panel.find(".cs-turn")).to_have_count(2)  # Still open.
    expect(panel.find(".cs-done")).to_have_count(0)
    # Picked while open, then taken in by the run: nothing left to send in.
    panel = open_panel(replies(run=status(run(), **NEW)))
    panel.click("[data-cs='c1']")
    panel.click("[data-cs-pick='c1']")
    expect(panel.find("#cs-send")).to_be_visible()
    panel.answer("conversations", taken)
    panel.click("#cs-run")
    panel.click("#start-run")
    expect(panel.find(".cs-turn")).to_have_count(2)
    expect(panel.find("#cs-send")).to_have_count(0)
    expect(panel.find("#cs-note")).to_have_count(0)


def test_an_opened_conversations_send_row_stays_in_view_above_the_run_bar(open_panel):
    long = {**chat(), "turns": [{"you": f"Turn on the porch light, the front one, take {n}.",
                                 "assist": "Which porch light do you mean? There are several. " * 3} for n in range(20)]}
    panel = open_panel(replies(conversation=long, run=status(run(), **NEW)))
    expect(panel.find(".cs-footer")).to_be_visible()  # New ones waiting: the Run bar shows.
    panel.click("[data-cs='c1']")
    expect(panel.find(".cs-turn")).to_have_count(20)
    panel.page.wait_for_timeout(600)  # The smooth scroll to it.
    panel.page.evaluate("() => document.scrollingElement.scrollBy(0, 200)")
    panel.settle()
    send, bar = panel.find("#cs-send").bounding_box(), panel.find(".cs-footer").bounding_box()
    assert send["y"] >= 0 and send["y"] + send["height"] <= bar["y"] + 0.5, (send, bar)
    note = panel.find("#cs-note").bounding_box()
    assert note["y"] >= 0 and note["y"] + note["height"] <= bar["y"] + 0.5, (note, bar)


def test_one_held_at_propose_is_run_from_the_bar_and_a_picked_run_never_promises_it(open_panel):
    """One held at Propose (its revision's run cut off, say) goes in with a manual full run, named in its
    confirmation; a picked run moves nothing else (Kai, 2026-10-26: P0.7), so it doesn't promise it."""
    nothing_sent = [{**c, "status": "queued"} if c["status"] == "sent" else c for c in CHATS]
    panel = open_panel(replies(conversations=listing(nothing_sent), run=status(run(), held={"propose": 1})))
    expect(panel.find(".cs-footer")).to_have_text("Run what's waiting")  # Only the one held.
    panel.click("#cs-run")
    expect(panel.find(".modal .c-what")).to_have_text("1 conversation to propose fixes for is waiting.")
    panel.click("#cancel-confirm-2")
    panel.click("[data-cs-pick='c1']")
    panel.click("[data-cs-pick='c4']")
    expect(panel.find("#cs-run")).to_have_text("Run on 2 conversations")
    panel.click("#cs-run")
    expect(panel.find(".modal-head b")).to_have_text("Run on 2 conversations?")
    expect(panel.find(".modal .c-which")).to_have_text("“Turn on the porch light.” and “Warm up the car.”")
    expect(panel.find(".modal .c-also")).to_have_count(0)
    panel = open_panel(replies(run=status(run(), **NEW, held={"propose": 2})))
    expect(panel.find(".cs-footer")).to_have_text("Run on 3 new conversations")


# What counts as trouble: side effects left and the household's feedback, on the home list and the review; and a
# conversation with no trouble found, sent in again.

# What a change leaves open, as the review sends it: one a reply with it still did, one this test couldn't tell.
STILL_OPEN = [{"quote": "“Say which doors moved”", "reply": 2, "why": "It says “Done.” without naming a door."},
              {"quote": "“Don't add a disclaimer”", "set_aside": True}]


def test_no_trouble_found_says_so_quietly_and_is_sent_in_again_only_opened_with_a_note(open_panel):
    fine = {**CHATS[3], "status": "fine"}  # “Warm up the car.”
    panel = open_panel(replies(conversations=listing([CHATS[0], fine]), conversation=chat("fine", id="c4"),
                               conversation__send={"status": "sent", "hidden": False}))
    chip = panel.find(".cs-row").nth(1).locator(".chip")
    expect(chip).to_have_text("No trouble found")
    expect(chip).to_have_attribute(
        "title", "Nothing went wrong, as far as it could tell. Send it in again with a note if something did.")
    assert "cs-fine" in classes(chip)  # Quiet grey, as Declined.
    panel.click("#cs-all")  # Selected (to archive or delete), but never run: it goes in opened, with a note.
    expect(panel.find("#cs-run")).to_have_text("Run on 1 conversation")
    panel.click("#cs-all")
    panel.click("[data-cs='c4']")
    panel.find("#cs-note").fill("It should have said it was done, briefly.")
    expect(panel.find("#cs-send")).to_have_text("Send this one in")
    panel.click("#cs-send")
    panel.settle()
    assert panel.calls("conversation/send")[-1] == {"type": "assist_improver/conversation/send", "recording_id": "c4",
                                                    "note": "It should have said it was done, briefly."}
    expect(panel.find(".cs-done")).to_have_text("Sent in. The improver is looking at it now.")


def test_home_says_an_improvement_avoids_unwanted_changes_and_follows_your_feedback(open_panel):
    spared = {**item("imp3", "Switch off only the lights you name"), "spared": 2, "followed": 1}
    panel = open_panel(replies(improvements={"improvements": [spared, {**PENDING[0], "spared": 1}]}))
    rows = panel.find(".page > .card .item")
    expect(rows.nth(0).locator(".gains")).to_have_text("2 unwanted changes avoided · follows your feedback")
    expect(rows.nth(1).locator(".gains")).to_have_text(
        "1 new thing Assist can do · 2 fewer turns · 1 unwanted change avoided")


LONGEST = "3 → 1 turns · side effects gone · follows your feedback"  # All three kinds of trouble removed.


def spared_review(open_panel, size=PHONE, dark=True, longest=False, **over):
    tests = [{"summary": "Switch off the lights", "improved": True, "result": "side effect gone", **CLOSED},
             {"summary": "Close the garage", "improved": True, "result": "3 → 1 turns · follows your feedback",
              **OPENED}] + ([{"summary": "Open the garage doors", "improved": True, "result": LONGEST, **CLOSED}]
                            if longest else [])
    return open_review(open_panel, size=size, dark=dark, **{"enabled": 0, "removed": 0, "spared": 1, "followed": 1,
                                                            "tests": tests, **over})


def test_a_review_counts_what_it_no_longer_changes_and_what_it_fixed_under_its_headline(open_panel):
    """Kai: the sections repeating what you get were unnecessary; the line under the headline counts them, and each
    test's row says what it showed."""
    panel = spared_review(open_panel)
    expect(panel.find(".outcome")).to_have_text("1 unwanted change avoided · follows your feedback")
    expect(panel.find(".page > .card:not(.summary) > details > summary")).to_have_text(
        ["What went wrong", "What changes (2)"])
    expect(panel.find(".tests li .result")).to_have_text(["side effect gone", "3 → 1 turns · follows your feedback"])


def test_a_review_with_nothing_still_open_has_no_still_open_lines(open_panel):
    panel = open_review(open_panel)
    expect(panel.find(".outcome")).to_have_text("1 new thing Assist can do · 2 fewer turns")
    expect(panel.find(".rp-open, .still-open")).to_have_count(0)


def test_what_is_still_open_is_escaped(open_panel):
    panel = spared_review(open_panel, still_open=[{"quote": XSS, "reply": 1, "why": XSS}])
    expect(panel.find(".rp-open .line")).to_have_text([f"!{XSS}"])
    expect(panel.find(".rp-open .line")).to_have_attribute("data-tip", f"Reply 1: {XSS}")
    assert_escaped(panel)


@pytest.mark.parametrize("size", [PHONE, DESKTOP], ids=["phone", "desktop"])
@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_the_chips_read_and_fit_in_all_four_views(open_panel, size, dark):
    panel = spared_review(open_panel, size=size, dark=dark, longest=True)
    expect(panel.find(".tests li .result").last).to_have_text(LONGEST)
    assert not low_contrast(panel, [".outcome"], 4.5)
    assert not low_contrast(panel, [".tests li .result"], 3)  # A small chip, as each test's result always was.
    assert_no_horizontal_scroll(panel)
    for row in panel.find(".tests li .row").all():  # A longer result still fits its row, beside what was tested.
        line, result = (row.locator(css).first.bounding_box() for css in (":scope", ".result"))
        assert result["x"] + result["width"] <= line["x"] + line["width"] + 0.5, (row.inner_text(), result, line)


@pytest.mark.parametrize("size", [PHONE, DESKTOP], ids=["phone", "desktop"])
@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_what_a_change_leaves_open_is_in_their_words_with_each_reply_and_reason_in_all_four_views(open_panel, size,
                                                                                                    dark):
    """What the household pointed out that a test doesn't show fixed: in their words, closing the replay with the
    change (under its last reply), each line's bubble the reply that still did it and why (or that this test couldn't
    tell), and an amber chip on its test's row counting them, after what the test showed."""
    tests = [{"summary": "Close the garage", "improved": True, "result": LONGEST, "open": 2, "happened": 1, **OPENED}]
    panel = spared_review(open_panel, size=size, dark=dark, still_open=STILL_OPEN, tests=tests)
    section = panel.find(".rp-after .rp-open")
    expect(section.locator(".rp-open-head")).to_have_text("Still open")
    replies = panel.find(".rp-after .cs-turn").last.bounding_box()
    assert section.bounding_box()["y"] >= replies["y"] + replies["height"]
    lines = section.locator(".line.open")
    expect(lines).to_have_text(["!“Say which doors moved”", "!“Don't add a disclaimer”"])
    expect(lines.nth(0)).to_have_attribute("data-tip", "Reply 2: It says “Done.” without naming a door.")
    expect(lines.nth(1)).to_have_attribute(
        "data-tip", "It didn't happen without the change either, so this test couldn't show it.")
    chip = panel.find(".tests li .result.still-open")
    expect(chip).to_have_text("2 still open")
    expect(chip).to_have_attribute("title", "Something you pointed out still happened in a reply with this change.")
    assert not low_contrast(panel, [".rp-open .line", ".rp-open-head"], 4.5)
    assert not low_contrast(panel, [".tests li .result"], 3)  # A small chip, as each test's result always was.
    assert_no_horizontal_scroll(panel)
    row = panel.find(".tests li .row")  # Both chips fit the row, beside or under what was tested.
    line = row.bounding_box()
    for box in (r.bounding_box() for r in row.locator(".result").all()):
        assert box["x"] + box["width"] <= line["x"] + line["width"] + 0.5, (box, line)


def test_a_chip_for_what_only_couldnt_be_told_says_so_and_none_shows_when_nothing_is_open(open_panel):
    tests = [{"summary": "Close the garage", "improved": True, "result": "3 → 1 turns", "open": 1, "happened": 0,
              **OPENED}, {"summary": "Open the garage", "improved": True, "result": "3 → 1 turns", **CLOSED}]
    panel = spared_review(open_panel, still_open=STILL_OPEN[1:], tests=tests)
    expect(panel.find(".tests > li").nth(0).locator(".rp-open .line")).to_have_count(1)  # With the test that counts it.
    expect(panel.find(".tests > li").nth(1).locator(".rp-open")).to_have_count(0)
    chips = panel.find(".tests li .result.still-open")
    expect(chips).to_have_text(["1 still open"])
    expect(chips).to_have_attribute(
        "title", "It didn't happen without the change either, so this test couldn't show it.")


# Where each conversation is, by its flow record (conversations.standing): a fix waiting for its write-up, a
# conversation whose question waits for the household (at the top of the home page), one archived by hand where a
# stage would work on it.

@pytest.mark.parametrize("standing,text", [("writing", "Writing it up"),
                                           ("unclear", "Question for you"), ("archived", "Archived")])
def test_each_place_a_conversation_can_be_has_its_own_chip(open_panel, standing, text):
    panel = open_panel(replies(conversations=listing([CHATS[0], {**CHATS[3], "status": standing}])))
    chip = panel.find(".cs-row").nth(1).locator(".chip")
    expect(chip).to_have_text(text)


CANT_IMPROVE = ("Home Assistant's built-in commands answered “Turn on the porch light”, not your AI assistant, and "
                "Assist Improver can only improve your AI assistant. To let it help with requests like this, turn off "
                "Prefer handling commands locally in Nova's settings.")


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
@pytest.mark.parametrize("size", [PHONE, DESKTOP], ids=["phone", "desktop"])
def test_a_conversation_home_assistant_answered_itself_says_why_it_cant_be_improved_and_how_to_let_it(
        open_panel, size, dark):
    """DETAILS P9.9.9: it waits at Your review with an amber Can't improve chip, whose bubble (a tap on a phone, a
    hover on a desktop, or focus) says what to do; it can be archived, never sent in, since it would come back the
    same."""
    phone, told = size is PHONE, {"cant_improve": {"quote": "Turn on the porch light", "assistant": "Nova"}}
    row = {**CHATS[3], "status": "cannot", **told}
    opened = {**chat("cannot", id="c4"), **told}
    panel = open_panel(replies(conversations=listing([CHATS[0], row]), conversation=opened), size=size, dark=dark,
                       touch=phone)
    chip = panel.find(".cs-row").nth(1).locator(".chip")
    expect(chip).to_have_text("Can't improve")
    amber = panel.page.evaluate("""() => { const root = document.querySelector('assist-improver-panel').shadowRoot;
        const chip = root.querySelector('.chip.cs-cannot'), probe = document.createElement('span');
        probe.style.color = 'var(--warn-fg)'; chip.parentElement.append(probe);
        const found = [getComputedStyle(chip).color, getComputedStyle(probe).color]; probe.remove(); return found; }""")
    assert amber[0] == amber[1], amber
    assert panel.contrast(".chip.cs-cannot") >= 4.5
    chip.tap() if phone else chip.hover()
    tip = panel.find("#tip")
    expect(tip).to_have_text(CANT_IMPROVE)
    box = tip.bounding_box()
    assert box["x"] >= 8 - 0.5 and box["x"] + box["width"] <= size["width"] - 8 + 0.5, box
    expect(panel.find(".cs-turn")).to_have_count(0)  # Reading it opens nothing: it's beside the row.
    panel.click("[data-cs='c4']")
    expect(panel.find(".cs-turn")).to_have_count(2)
    expect(panel.find("#cs-send")).to_have_count(0)
    panel.click("#cs-actions")
    expect(panel.find("#cs-archive")).to_be_visible()


def test_the_status_filter_finds_the_conversations_that_cant_be_improved(open_panel):
    row = {**CHATS[3], "status": "cannot", "cant_improve": {"quote": "Turn on the porch light", "assistant": "Nova"}}
    panel = open_panel(replies(conversations=listing([CHATS[0], row])))
    panel.click("#cs-status")
    expect(panel.find(".cs-status-opt .cs-status-name")).to_have_text(["All", "New", "Can't improve"])
    panel.click("#cs-status-all")  # None: then just these.
    panel.click("[data-cs-status='cannot']")
    assert panel.calls("conversations")[-1]["statuses"] == ["cannot"]


def test_archiving_reads_again_what_a_run_would_take(open_panel):
    """The Run bar counts what a run will take in; an archived conversation is out of the pipeline, so archiving one
    reads that again, as sending one in or deleting does."""
    panel = open_panel(replies(run=status(run(), **NEW)))
    expect(panel.find("#cs-run")).to_have_text("Run on 3 new conversations")
    asked = len(panel.calls("run"))
    panel.answer("run", status(run(), admits=2))
    panel.click("[data-cs='c1']")
    panel.click("#cs-actions")
    panel.click("#cs-archive")
    expect(panel.find("#cs-run")).to_have_text("Run on 2 new conversations")
    assert len(panel.calls("run")) > asked


def test_history_counts_as_analyzed_only_the_conversations_analysed_through(open_panel):
    """History says how many the run analysed (flow.Flow.run's results), never one held for failing."""
    failed = event_run(progress={"taken_in": 1, "analyzed": 0, "held": 1, "why": "Analyze failed 3 times in a row."})
    panel = open_history(open_panel, [failed])
    sub = panel.find(".h-row .h-sub")
    expect(sub).to_have_text("1 taken in · 1 held: Analyze failed 3 times in a row.")
    done = event_run(progress={"taken_in": 2, "analyzed": 1})
    panel = open_history(open_panel, [done])
    expect(panel.find(".h-row .h-sub")).to_have_text("2 taken in · 1 analyzed")
