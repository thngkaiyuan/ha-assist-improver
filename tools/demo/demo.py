"""A local demo Home Assistant for Assist Improver's docs: made-up devices and conversations, never a real home.

  tools/demo/.venv/bin/python tools/demo/demo.py setup     config folder, integrations linked (first time)
  tools/demo/.venv/bin/python tools/demo/demo.py start     start HA at http://localhost:8124 (background; log in config/)
  tools/demo/.venv/bin/python tools/demo/demo.py onboard   demo owner, then the demo, Demo AI and Assist Improver
  tools/demo/.venv/bin/python tools/demo/demo.py seed      made-up conversations and improvements on the demo devices
                                                          (demo_conversations.py; restarts HA twice)
  tools/demo/.venv/bin/python tools/demo/demo.py stop
  tools/demo/.venv/bin/python tools/demo/demo.py fresh     all of the above from scratch (wipes config/)

Set up the environment with: uv venv tools/demo/.venv --python 3.14 --managed-python, then
uv pip install --python tools/demo/.venv/bin/python homeassistant==2026.11.0 home-assistant-frontend==20261028.4.
The demo owner's login is generated on onboarding and kept in config/demo-login.json (a local test account).
"""

import json
import os
import secrets
import signal
import subprocess
import sys
import time
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
CONFIG = HERE / "config"
URL = "http://127.0.0.1:8124"
LOGIN = CONFIG / "demo-login.json"
CLIENT = URL + "/"

CONFIGURATION = """homeassistant:
  name: Demo Home
  unit_system: metric
  time_zone: America/Los_Angeles
  country: US
  currency: USD
http:
  server_port: 8124
  server_host: 127.0.0.1
frontend:
config:
onboarding:
history:
logbook:
person:
conversation:
assist_pipeline:
intent:
analytics:
demo:
script: !include scripts.yaml
"""
SCRIPTS = """garage_open:
  alias: Open garage door
  sequence:
  - action: cover.open_cover
    target:
      entity_id: cover.garage_door
"""


def setup():
    CONFIG.mkdir(exist_ok=True)
    (CONFIG / "configuration.yaml").write_text(CONFIGURATION)
    (CONFIG / "scripts.yaml").write_text(SCRIPTS)
    custom = CONFIG / "custom_components"
    custom.mkdir(exist_ok=True)
    for name, target in (("assist_improver", ROOT / "custom_components/assist_improver"), ("demo_ai", HERE / "demo_ai")):
        link = custom / name
        if not link.exists():
            link.symlink_to(target)
    print("config ready:", CONFIG)


def _request(method, path, body=None, token=None, form=False):
    data = None
    headers = {}
    if body is not None:
        if form:
            data = "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in body.items()).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(URL + path, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        text = resp.read().decode()
        return json.loads(text) if text else None


def _wait(timeout=180):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(URL + "/manifest.json", timeout=5)
            return True
        except Exception:
            time.sleep(2)
    raise SystemExit("HA did not come up; see config/home-assistant.log")


def start():
    if (CONFIG / "hass.pid").exists():
        print("already started (pid file present)")
        return
    log = open(CONFIG / "demo-run.log", "a")
    proc = subprocess.Popen([str(HERE / ".venv/bin/hass"), "-c", str(CONFIG)], stdout=log, stderr=log,
                            start_new_session=True)
    (CONFIG / "hass.pid").write_text(str(proc.pid))
    _wait()
    print("HA up at", URL)


def stop():
    pid_file = CONFIG / "hass.pid"
    if not pid_file.exists():
        return
    try:
        os.kill(int(pid_file.read_text()), signal.SIGTERM)
        for _ in range(60):
            os.kill(int(pid_file.read_text()), 0)
            time.sleep(1)
    except ProcessLookupError:
        pass
    pid_file.unlink(missing_ok=True)
    print("HA stopped")


def _token():
    login = json.loads(LOGIN.read_text())
    flow = _request("POST", "/auth/login_flow", {"client_id": CLIENT, "handler": ["homeassistant", None],
                                                  "redirect_uri": CLIENT})
    done = _request("POST", f"/auth/login_flow/{flow['flow_id']}",
                    {"client_id": CLIENT, "username": login["username"], "password": login["password"]})
    tokens = _request("POST", "/auth/token", {"grant_type": "authorization_code", "code": done["result"],
                                              "client_id": CLIENT}, form=True)
    return tokens["access_token"]


def _flow(token, handler, *steps):
    result = _request("POST", "/api/config/config_entries/flow", {"handler": handler}, token)
    for step in steps:
        if result.get("type") != "form":
            break
        result = _request("POST", f"/api/config/config_entries/flow/{result['flow_id']}", step(result), token)
    return result


def onboard():
    import urllib.parse  # noqa: F401  (used by _request for forms)
    if not LOGIN.exists():
        login = {"username": "demo", "password": "demo-" + secrets.token_urlsafe(12), "name": "Alex"}
        LOGIN.write_text(json.dumps(login))
        LOGIN.chmod(0o600)
    login = json.loads(LOGIN.read_text())
    status = _request("GET", "/api/onboarding")
    if not next(s for s in status if s["step"] == "user")["done"]:
        code = _request("POST", "/api/onboarding/users", {"client_id": CLIENT, "name": login["name"],
                                                          "username": login["username"], "password": login["password"],
                                                          "language": "en"})["auth_code"]
        token = _request("POST", "/auth/token", {"grant_type": "authorization_code", "code": code,
                                                 "client_id": CLIENT}, form=True)["access_token"]
        _request("POST", "/api/onboarding/core_config", {}, token)
        _request("POST", "/api/onboarding/analytics", {}, token)
        _request("POST", "/api/onboarding/integration", {"client_id": CLIENT, "redirect_uri": CLIENT}, token)
    token = _token()
    entries = {e["domain"] for e in _request("GET", "/api/config/config_entries/entry", token=token)}
    if "demo_ai" not in entries:
        print("demo_ai:", _flow(token, "demo_ai").get("type"))
    if "assist_improver" not in entries:
        print("assist_improver:", _flow(token, "assist_improver",
                                        lambda r: {"ai_task_entity_id": "ai_task.demo_large"},
                                        lambda r: {"use_anyway": True}).get("type"))
    print("onboarded; login in", LOGIN)


def _storage(key):
    path = CONFIG / ".storage" / key
    return json.loads(path.read_text())["data"] if path.exists() else None


def _write(key, data, private=True):
    """A store as Home Assistant writes it (private ones readable by HA's user only)."""
    path = CONFIG / ".storage" / key
    path.write_text(json.dumps({"version": 1, "minor_version": 1, "key": key, "data": data}, indent=1))
    if private:
        path.chmod(0o600)


# The stages the last run took each standing's conversation through (flow.STAGES): where it ended, and how.
PATHS = {"fine": ["Your Assist", "Triage", "Analyze", "Your household"],
         "nofix": ["Your Assist", "Triage", "Analyze", "Propose", "Your household"],
         "review": ["Your Assist", "Triage", "Analyze", "Propose", "Write up", "Your review"]}
OUTCOMES = {"fine": "no trouble found", "nofix": "no fix found"}
MOVIE_TIME = {"alias": "Movie time", "description": "Gets the living room ready for a film: dims the lights, closes the "
              "blinds and turns on the TV.", "sequence": [
                  {"action": "light.turn_on", "target": {"entity_id": "light.living_room_rgbww_lights"},
                   "data": {"brightness_pct": 10}},
                  {"action": "cover.close_cover", "target": {"entity_id": "cover.living_room_window"}},
                  {"action": "media_player.turn_on", "target": {"entity_id": "media_player.living_room"}}]}


def _flow_record(spec, record):
    """A conversation's flow record (flow.Flow.record) where the last run ended it: no trouble found, no fix found, or
    up for review."""
    path = PATHS[spec["standing"]]
    return {"id": record["id"], "stage": path[-1], "round": 1, "held": None,
            "outcome": OUTCOMES.get(spec["standing"]), "archived": False, "covered_by": None, "sent": None,
            "rounds": {"1": {"path": path, "records": {}, "handoffs": []}}, "trips": [], "trace": []}


def _clear():
    """The improver's stores of conversations and what came of them (the household lines stay)."""
    for path in (CONFIG / ".storage").glob("assist_improver.*"):
        if path.name != "assist_improver.household":
            path.unlink()


def _turn(you, assistant):
    return {"you": you, "assistant": assistant}


def _templates(assistants):
    """One real recording of each demo assistant (Home Assistant's own snapshot of the demo home, Assist's tools and
    prompt), which the made-up conversations are built on: a request to each, recorded by the improver, read once HA
    has stopped and then dropped."""
    token = _token()
    deadline = time.time() + 180
    while True:  # HA answers before its integrations are set up: until the assistants are there.
        try:
            if (_request("GET", "/api/config", token=token)["state"] == "RUNNING"
                    and all(_request("GET", f"/api/states/{agent}", token=token) for agent in assistants)):
                break
        except urllib.error.HTTPError:
            pass
        if time.time() > deadline:
            raise SystemExit("The demo assistants didn't come up; see config/home-assistant.log")
        time.sleep(2)
    asked = {agent: _request("POST", "/api/conversation/process",
                             {"text": "Hello", "agent_id": agent, "language": "en"}, token)["conversation_id"]
             for agent in assistants}
    time.sleep(3)  # The recorder saves a turn within a second or two.
    stop()
    out = {}
    for key in (_storage("assist_improver.index") or {}).get("sessions", {}):
        record = _storage(f"assist_improver.session_{key}")
        agent = next((a for a, cid in asked.items() if record and record["conversation_id"] == cid), None)
        if agent is None:
            continue
        turn = record["turns"][0]
        if "prepared" not in turn:
            raise SystemExit(f"{agent} prepared no tools; see config/home-assistant.log")
        out[agent] = {"source": turn["source"], "prepared": turn["prepared"], "core_version": record["core_version"],
                      "system": turn["preparations"][-1]["content"][0]["content"],
                      "subentries": turn["prepared"]["provider_config"]}  # Each assistant's, until one answers.
    if set(out) != set(assistants):
        raise SystemExit("A demo assistant's request wasn't recorded; see config/home-assistant.log")
    return out


def seed():
    """Made-up conversations and improvements on the demo's devices, written while HA is stopped (HA is started in
    between to record one request to each assistant, which they're built on), then HA is started again."""
    from zoneinfo import ZoneInfo
    import demo_conversations as made_up
    stop()  # HA writes its storage on the way down; the entries made just now are read from it.
    entries = _storage("core.config_entries")["entries"]
    demo_ai = next(e for e in entries if e["domain"] == "demo_ai")
    living = next(s for s in demo_ai["subentries"] if s["title"] == "Living room assistant")
    user_id = next(u["id"] for u in _storage("auth")["users"] if u.get("name") == json.loads(LOGIN.read_text())["name"])
    provider = {"config_entry_id": demo_ai["entry_id"], "subentry_id": living["subentry_id"],
                "title": living["title"]}
    _clear()
    # The quick check on.
    config_entries = json.loads((CONFIG / ".storage" / "core.config_entries").read_text())
    for entry in config_entries["data"]["entries"]:
        if entry["domain"] == "assist_improver":
            entry["options"].update(triage_ai_task_entity_id="ai_task.demo_small")
        if entry["domain"] == "demo_ai":  # Both assistants on Assist, which brings the household knowledge.
            for sub in entry["subentries"]:
                if sub["subentry_type"] == "conversation":
                    sub["data"]["llm_hass_api"] = ["assist"]
    (CONFIG / ".storage" / "core.config_entries").write_text(json.dumps(config_entries))
    # Two assistants in Assist besides Home Assistant's own, the living room one preferred; and a few household lines.
    pipelines = _storage("assist_pipeline.pipelines")
    base = {k: v for k, v in pipelines["items"][0].items() if k not in ("id", "name", "conversation_engine")}
    pipelines["items"] = [pipelines["items"][0],
                          {**base, "id": "demo-living-room", "name": "Living room", "conversation_engine": "conversation.living_room_assistant"},
                          {**base, "id": "demo-kitchen", "name": "Kitchen", "conversation_engine": "conversation.kitchen_assistant"}]
    pipelines["preferred_item"] = "demo-living-room"
    (CONFIG / ".storage" / "assist_pipeline.pipelines").write_text(json.dumps(
        {"version": 1, "minor_version": 2, "key": "assist_pipeline.pipelines", "data": pipelines}))
    _write("assist_improver.household", {"everyone": ["The garage door is a single momentary button.",
                                                      "Movie time means the living room.", "Quiet hours start at 22:00."],
                                         "people": {}}, private=False)

    # The conversations: built on a real recording of each assistant, as the recorder keeps them once ended.
    start()
    templates = _templates((made_up.LIVING, made_up.KITCHEN))
    _clear()
    zone = ZoneInfo(templates[made_up.LIVING]["source"]["time_zone"])  # The demo home's, as HA recorded it.
    now = datetime.now(UTC)
    made, index = [], {}
    for spec in made_up.CONVERSATIONS:
        template = dict(templates[spec["assistant"]])
        title = "Living room assistant" if spec["assistant"] == made_up.LIVING else "Kitchen assistant"
        mine = [c for c in template["subentries"] if c["title"] == title]  # The one that answered.
        template["prepared"] = {**template["prepared"], "provider_config": mine}
        record, stored, summary = made_up.recording(spec, now, template, user_id, zone)
        _write(f"assist_improver.session_{record['id']}", stored)
        index[record["id"]] = summary
        made.append((spec, record))
    _write("assist_improver.index", {"sessions": index})
    # Where each stands, as the improver's flow keeps it (flow.py): new ones have no record yet.
    _write("assist_improver.flow", {"conversations": {record["id"]: _flow_record(spec, record) for spec, record in made
                                                      if spec["standing"] != "new"}})
    by_key = {spec["key"]: record for spec, record in made}
    short = {key: record["id"][:8] for key, record in by_key.items()}

    # The runs: each took in its conversations, analysed them, and proposed its improvements (made_up.RUNS).
    runs, created = [], {}
    for name, run in made_up.RUNS.items():
        run_id = made_up._id("demo-run", name)
        run_started = (now - run["ago"]).replace(microsecond=0)
        finished = run_started + timedelta(minutes=3, seconds=12)
        taken = [(spec, record) for spec, record in made if spec.get("run") == name]
        runs += [{"id": run_id, "kind": "pipeline", "trigger": "manual", "status": "done",
                  "started": run_started.isoformat(), "finished": finished.isoformat(), "stage": "write",
                  "progress": {"taken_in": len(taken), "analyzed": len(taken),
                               "new_improvements": len(run["improvements"])},
                  "error": None, "only": None, "trace_id": run_id,
                  **({"picked": [record["id"] for _, record in taken]} if run["picked"] else {})},
                 *made_up.analyses(made, name, run_id, finished),
                 {"id": made_up._id("demo-proposal", name), "kind": "hypothesis_generator",
                  "role": "hypothesis_generator", "status": "completed", "entity_id": "ai_task.demo_large",
                  "started": (finished - timedelta(seconds=50)).isoformat(),
                  "finished": (finished - timedelta(seconds=20)).isoformat(), "tested": True,
                  "output": {"hypotheses": []}, "sessions": [short[s["key"]] for s, _ in taken], "run_id": run_id,
                  "trace_id": run_id}]
        created.update(dict.fromkeys(run["improvements"], finished.isoformat()))
    _write("assist_improver.role_runs", {"runs": runs})

    def item(id_, title, cause, changes, setup, goal, test, tested=None):
        """An improvement as the pipeline leaves it (improvements.build_for): for the one conversation it came from
        (goal), tested on it."""
        record, hypothesis = by_key[goal], {"title": title, "cause": cause, "changes": changes}
        setup = {"scripts": {}, "exposure": {}, "aliases": {}, "alias_removals": {}, **setup}
        tested = tested or {"scripts": {}, "exposure": {}}
        return {"id": id_, "title": title, "cause": cause, "hypothesis": hypothesis, "changes": changes,
                "test": {**test, "idea": title, "hypothesis": hypothesis, "changes": made_up.digest(changes),
                         "setup": setup,
                         "tested": tested, "provider": provider},
                "round": 1, "conversation": record["id"], "fixes": [f"{record['id'][:8]}:1"], "devices": [],
                "setup": setup, "tested": tested, "provider": provider, "user_ids": [user_id], "checked_on": [],
                "status": "awaiting_review", "created": created[id_], "decisions": [], "application": None,
                "history": []}

    def test(goal, summary, after, *, met_before=True):
        """Its test, as the simulator keeps one (records.Test): the conversation as it went, and its replay with the
        change (after: Assist's replies), one root goal said in each turn; met with the change, and before unless
        not met_before."""
        record, root = by_key[goal], "1:0"
        said = made_up.transcript(record)

        def replay(status, replies, met):
            numbers = list(range(1, len(replies) + 1))
            return {"plan": status, "status": status, "said": numbers,
                    "replies": [{"turn": n, **reply} for n, reply in zip(numbers, replies)],
                    "goals": {root: "met" if met else "unmet"}, "goals_met": [root] if met else [],
                    "turns_per_root_goal": {root: len(numbers)}, "counted": {root: numbers}, "side_effects_left": [],
                    "started_from": {"at": record["started"], "home": "home"},
                    "config": {"scripts": {}, "exposed": []}, "conduct": {}}
        return {"at": record["started"], "judged": {root: {"summary": summary, "turn": 1, "root": root,
                                                           "devices": []}},
                "before": replay("reproduced", [_turn(t["you"], t["assist"]) for t in said], met_before),
                "after": replay("done", after, True), "gains": ["turns per root goal"] if met_before else ["goals met"],
                "validated": True, "user_id": user_id, "notes": [],
                "recorded": [{"you": t["you"], "assist": t["assist"], **({"failed": t["failed"]} if t.get("failed")
                                                                         else {})} for t in said]}

    lights_line = ("When asked to turn lights on or off in a room or the whole home, act only on lights: never "
                   "switch off other devices in those rooms, such as the AC, TVs or blinds.")
    items = [
        item("demo-lights-only", "Lights only, when you ask about lights",
             "Asked to turn off the lights except in the bedroom, the assistant switched off whole rooms: the AC and "
             "the living room TV went off too, and they had to be turned back on.",
             [{"kind": "preference", "for": "everyone", "text": lights_line}],
             {"household": {"everyone": [lights_line], "people": {}}}, "lights-except-bedroom",
             test("lights-except-bedroom", "Turn off every light except in the bedroom, and nothing else",
                  [_turn("Turn off all the lights except the bedroom.", "Done: the living room, kitchen, office and "
                         "entrance lights are off; the bedroom light is on.")], met_before=False)),
        item("demo-garage", "Open the garage door by its everyday name",
             "The garage door is called “Garage Door” in Home Assistant, and “the garage” didn't "
             "match it, so the assistant asked which device was meant.",
             [{"kind": "registry", "entity_id": "cover.garage_door", "aliases": ["Garage"]}],
             {"aliases": {"cover.garage_door": ["Garage"]}}, "garage",
             test("garage", "Open the garage door", [_turn("Open the garage.", "The garage door is opening.")])),
        item("demo-movie", "Movie time in one request",
             "Getting the living room ready for a film took three requests: the lights, the blinds, then the TV.",
             [{"kind": "script", "id": "movie_time", "config": MOVIE_TIME}],
             {"scripts": {"movie_time": MOVIE_TIME}, "exposure": {"script.movie_time": True}}, "movie",
             test("movie", "Get the living room ready for a movie",
                  [_turn("It's movie time in the living room.", "Movie time: lights dimmed, blinds closing and the "
                         "TV is on.")]),
             tested={"exposure": {"script.movie_time": False}}),
    ]
    copy = {"demo-lights-only": ("Turn off only lights when you ask for lights",
                                 "Say “turn off all the lights except the bedroom” and only the lights go off: the AC and "
                                 "the TV stay on, so you don't have to turn them back on."),
            "demo-garage": ("Let “the garage” open the garage door",
                            "Say “open the garage” and the garage door opens, without Assist asking which device you mean."),
            "demo-movie": ("Add a Movie time script for the living room",
                           "Say “it's movie time” and the lights dim, the blinds close and the TV comes on, in one request "
                           "instead of three.")}
    for i in items:  # As the editor writes them, for the demo.
        headline, benefit = copy[i["id"]]
        i["copy"] = {"status": "written", "entity_id": "ai_task.demo_small", "headline": headline, "benefit": benefit}
    _write("assist_improver.improvements", {"improvements": {i["id"]: i for i in items}})
    start()
    print("seeded", len(made), "conversations and", len(items), "improvements")


def fresh():
    """A brand-new demo: wipe the config folder, then set up, start, onboard and seed."""
    import shutil
    stop()
    shutil.rmtree(CONFIG, ignore_errors=True)
    setup()
    start()
    onboard()
    seed()


if __name__ == "__main__":
    import urllib.parse  # noqa: F401
    {"setup": setup, "start": start, "stop": stop, "onboard": onboard, "seed": seed, "fresh": fresh}[sys.argv[1]]()
