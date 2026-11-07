"""The real panel behind a stand-in Assist Improver, served locally, for trying the whole loop by hand: find
conversations, send them in, run, review, accept, go again (tools/ui/loop_sim.js does the rest in the page).

  .venv/bin/python tools/ui/loop.py seed > "$(.venv/bin/python tools/private.py)/ui/loop-seed.json"
                                       the household's own conversations, from the private folder (tools/private.py)
  .venv/bin/python tools/ui/loop.py serve [--seed FILE] [--port 8765] [--light]  http://127.0.0.1:8765

Without a seed file it serves made-up conversations. Nothing reaches Home Assistant or a model.
"""

from datetime import UTC, datetime, timedelta
import argparse
import http.server
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
WWW = ROOT / "custom_components/assist_improver/www"
STATIC = "/assist_improver_static/"
THEMES = {
    "dark": """body { margin: 0; background: #111111; font-family: Roboto, -apple-system, sans-serif;
  --primary-color: #009ac7; --primary-text-color: #e1e1e1; --secondary-text-color: #9b9b9b; --text-primary-color: #ffffff;
  --card-background-color: #1c1c1c; --primary-background-color: #111111; --secondary-background-color: #282828;
  --divider-color: rgba(225, 225, 225, 0.12); --app-header-background-color: #1c1c1c; --app-header-text-color: #e1e1e1; color: #e1e1e1; }""",
    "light": """body { margin: 0; background: #fafafa; font-family: Roboto, -apple-system, sans-serif;
  --primary-color: #009ac7; --primary-text-color: #212121; --secondary-text-color: #727272; --text-primary-color: #ffffff;
  --card-background-color: #ffffff; --primary-background-color: #fafafa; --secondary-background-color: #e5e5e5;
  --divider-color: rgba(0, 0, 0, 0.12); --app-header-background-color: #009ac7; --app-header-text-color: #ffffff; color: #212121; }"""}

REVIEW = {  # The review page's shape (panel.review), filled per improvement by the stand-in.
    "safety": ["Tested on your own conversations, replayed in a sandboxed copy of your home: nothing in your home moved.",
               "Only settings change: 1 instruction change. Nothing runs when you accept.",
               "Checked against your Home Assistant first, applied all or nothing, and one click to undo."],
    "enabled": 0, "removed": 1, "spared": 0, "followed": 0, "still_open": [], "recent_outcome": None, "recent_outcome_ok": False, "revises": None,
    "conflicts": [],
    "changes": [{"name": "Household knowledge", "kind": "instructions", "added": 1, "removed": 0,
                 "rows": [{"sign": " ", "old": 1, "new": 1, "text": "Keep answers short."},
                          {"sign": "+", "old": None, "new": 2, "text": "When a name matches one device, act on it."}]}],
    "tests": [{"summary": "The same request, replayed", "improved": True, "result": ["2 → 1 turns"]}],
    "choices": [{"value": "accept", "label": "Accept changes"}, {"value": "revise", "label": "Ask for a revision"},
                {"value": "decline", "label": "Decline", "reasons": ["not_an_issue"]}]}
DECIDED = {"result": "Applied. Assist uses it from your next request; no script was run.", "status": "applied", "ok": True}
SETTINGS = {"developer": False, "schedule": "off", "paused": [], "tests_per_goal": 3, "recommended": True, "providers": [],
            "ai_tasks": [], "roles": {}}
HOUSEHOLD = {"lines": 0, "everyone": [], "people": [], "me": {"user_id": "u", "name": "You"}, "pipelines": [],
             "assistants": []}


def made_up():
    now = datetime.now(UTC)
    talks = [("Turn on the porch light.", "Which porch light: front or back?", 2, 0, 0.1),
             ("Warm up the car.", "I couldn't find a car called that.", 3, 1, 3),
             ("Open the garage.", "Opened the garage.", 1, 0, 5),
             ("Set the bedroom to 19 degrees.", "Set the bedroom thermostat to 19 degrees.", 1, 0, 20),
             ("Play jazz in the kitchen.", "There are two speakers in the kitchen. Which one?", 2, 0, 26),
             ("Turn off everything downstairs.", "I can't do that; I couldn't match 'downstairs'.", 2, 1, 50),
             ("What's the weather tomorrow?", "Tomorrow: sunny, 18 degrees.", 1, 0, 70),
             ("Dim the living room lights to 30%.", "Done.", 1, 0, 90),
             ("Lock the front door.", "I couldn't lock it: the door reported an error.", 2, 1, 120),
             ("Is the dishwasher done?", "Yes, it finished 10 minutes ago.", 1, 0, 200)]
    conversations = []
    for n, (said, reply, turns, failed, hours) in enumerate(talks):
        transcript = [{"you": said, "assist": reply}] + [
            {"you": "No, the other one." if k % 2 else "I said it already.", "assist": "Done."} for k in range(1, turns)]
        conversations.append({"id": f"conv{n:02d}", "started": (now - timedelta(hours=hours)).isoformat(),
                              "turns": turns, "failed": failed, "status": "waiting", "transcript": transcript})
    return {"conversations": conversations, "improvements": []}


def real():
    """The household's conversations, from tools/preflight.py's copies in the private folder (tools/private.py)."""
    sys.path[:0] = [str(ROOT), str(ROOT / "tools")]
    import types
    sys.modules.setdefault("turbojpeg", types.SimpleNamespace(TurboJPEG=object))
    import private
    from custom_components.assist_improver.cases import failed_calls, transcript
    from custom_components.assist_improver.retention import unpack
    copies = private.path("preflight", "storage")  # The sessions and the index they were copied with.
    index = json.loads((copies / "assist_improver.index").read_text())["data"]["sessions"]
    runs = json.loads(private.path("stores", "assist_improver.role_runs").read_text())["data"]["runs"]
    analysed = {r.get("recording_id") for r in runs if r.get("kind") == "journey_analyzer" and r.get("analysis")}
    conversations = []
    for path in sorted(copies.glob("assist_improver.session_*")):
        if path.name == "assist_improver.session_briefs":  # A store of its own, not a conversation.
            continue
        recording = unpack(json.loads(path.read_text())["data"])
        rid = recording["id"]
        summary = index.get(rid, {})
        if not summary.get("user_turns"):
            continue
        status = "in" if rid in analysed else "waiting"
        conversations.append({"id": rid, "started": summary.get("started") or recording.get("started"),
                              "turns": summary.get("user_turns"), "failed": failed_calls(recording), "status": status,
                              "transcript": transcript(recording)})
    return {"conversations": conversations, "improvements": []}


def serve(seed, port, theme):
    seed = {**seed, "review": REVIEW, "decided": DECIDED, "settings": SETTINGS, "household": HOUSEHOLD}
    page = f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Assist Improver (stand-in)</title><style>{THEMES[theme]}</style></head><body>
<script src="/loop_sim.js"></script><script type="module">
const callWS = window.loopSim({json.dumps(seed)});
await import("{STATIC}panel.js?v=loop");
const el = document.createElement("assist-improver-panel");
document.body.appendChild(el);
el.narrow = window.innerWidth < 870;
el.hass = {{themes: {{darkMode: {"true" if theme == "dark" else "false"}}}, callWS}};
</script></body></html>"""

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split("?")[0]
            if path.startswith(STATIC):
                body, kind = (WWW / path[len(STATIC):]).read_bytes(), "text/javascript"
            elif path == "/loop_sim.js":
                body, kind = (Path(__file__).parent / "loop_sim.js").read_bytes(), "text/javascript"
            else:
                body, kind = page.encode(), "text/html"
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    print(f"Serving the panel with a stand-in improver at http://127.0.0.1:{port}/assist-improver", flush=True)
    http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["seed", "serve"])
    parser.add_argument("--seed")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--light", action="store_true")
    args = parser.parse_args()
    if args.command == "seed":
        print(json.dumps(real()))
    else:
        serve(json.loads(Path(args.seed).read_text()) if args.seed else made_up(), args.port,
              "light" if args.light else "dark")
