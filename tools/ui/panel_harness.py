"""The panel in a browser with a stubbed Home Assistant: panel.js renders from canned websocket replies, so a state
can be checked without any Home Assistant running (the seed of the panel's behavior tests).

  PLAYWRIGHT_BROWSERS_PATH=tools/ui/browsers tools/e2e/.venv/bin/python tools/ui/panel_harness.py <out dir> [scenario ...]

Scenarios are named states below; each is shown at phone size in dark mode unless it says otherwise (a light theme, a
desktop-width window), and saved as <out dir>/<scenario>.png; the developer view's are saved at full length. The
output lists page errors and any scenario whose page scrolls sideways.
"""

from datetime import UTC, datetime, timedelta
import hashlib
import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent.parent
WWW = ROOT / "custom_components/assist_improver/www"  # panel.js, and the modules it loads from panel/.
STATIC = "/assist_improver_static/"  # Where Home Assistant serves www/ (panel.py's static path).
ORIGIN = "http://panel.test"  # Served by the harness itself (page.route): nothing goes to the network.

EARLIER = [{"id": f"i{n}", "title": title, "status": "applied", "pending": False}
           for n, title in enumerate(["Open or close garage doors by pressing their buttons",
                                      "Warm up the car straight away"])]
HOUSEHOLD = {"lines": 6, "everyone": [], "people": [], "pipelines": []}
PREVIEW = {"stages": {"Your Assist": 16, "Triage": 0, "Analyze": 0, "Propose": 0, "Write up": 0, "Your review": 0,
                      "Your household": 0}, "held": {}, "admits": 16, "releases": {}, "which": [], "quick": 0,
           "analyses": 16, "paused": [], "blocked_models": {}, "spend": {}, "running": False, "applied": 0}
RUN = {"id": "r1", "kind": "pipeline", "trigger": "manual", "started": "2026-10-25T01:53:43+00:00",
       "finished": "2026-10-25T01:54:08+00:00", "stage": "analyze", "progress": {}, "error": None}

# Home Assistant's default dark theme, as the panel's CSS variables read it.
THEME = """body { margin: 0; background: #111111; font-family: Roboto, -apple-system, sans-serif;
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


# The developer view's canned replies (devview.py's shapes), timed relative to now so "ago" and live runs read right.

NOW = datetime.now(UTC)


def at(seconds_ago):
    return (NOW - timedelta(seconds=seconds_ago)).isoformat()


def hexid(seed, n=32):
    return hashlib.sha256(seed.encode()).hexdigest()[:n]


REC = [hexid(f"recording {n}", 64) for n in range(14)]
IMP = [hexid(f"improvement {n}") for n in range(3)]
PLAN = [hexid(f"plan {n}") for n in range(3)]
CALL = [hexid(f"role call {n}") for n in range(8)]
RUN_LIVE, RUN_DONE, RUN_SCHED, RUN_FAIL, RUN_BLOCKED, RUN_OLD = (hexid(f"run {n}") for n in range(6))
CONTEXT = ("run_id", "recording_id", "improvement_id", "plan_id", "role", "goal")
OPUS, HAIKU = "ai_task.claude_opus_5_5", "ai_task.claude_haiku_4_5"


class Spans:
    """A trace's spans, as trace.py records them: context tags flow down to children."""

    def __init__(self, run_id, started):
        self.run_id, self.started, self.spans, self.tags = run_id, started, [], {}

    def add(self, name, start, seconds, parent=None, span_status="ok", error=None, **tags):
        span_id = hexid(f"{self.run_id} span {len(self.spans)}", 12)
        inherited = {k: v for k, v in self.tags.get(parent, {"run_id": self.run_id}).items() if k in CONTEXT}
        own = {k: v for k, v in tags.items() if v is not None}
        self.tags[span_id] = {**inherited, **own}
        self.spans.append({"id": span_id, "parent": parent, "name": name, "tags": {**inherited, **own},
                           "at": (self.started + timedelta(seconds=start)).isoformat(), "ms": round(seconds * 1000, 3),
                           "status": span_status, "error": error})
        return span_id


def entry(seconds_ago, level, message, **extra):
    tags = extra.pop("tags", {})
    return {"at": at(seconds_ago), "level": level, "message": message, "tags": tags, "span": extra.pop("span", None),
            **extra}


TRACEBACK = """Traceback (most recent call last):
  File "/config/custom_components/assist_improver/pipeline.py", line 231, in _guarded
    await work()
  File "/config/custom_components/assist_improver/editor.py", line 88, in write_up
    reply = await run_role(hass, "editor", prompt)
RuntimeError: provider down"""


def done_trace():
    """A finished run: four stages, five conversations (one failed), two ideas simulated (a goal errored), and a
    write-up whose provider was down."""
    started = NOW - timedelta(hours=2)
    s = Spans(RUN_DONE, started)
    run = s.add("run", 0, 311.9, trigger="manual", outcome="done")
    analyze = s.add("stage.analyze", 0.05, 93.9, run)
    t = 0.1
    for i, rid in enumerate(REC[:5]):
        smooth, failed = i in (1, 3), i == 4
        triage = 2.1 + 0.3 * i
        length = triage + (0 if smooth else 13.5 + 1.6 * i) + 0.06
        conv = s.add("conversation", t, length, analyze, status="failed" if failed else "completed", recording_id=rid,
                     steps=4 + i, triage="smooth" if smooth else "friction",
                     answer_status="error" if failed else "triaged_smooth" if smooth else "answered")
        s.add("ai.triage", t + 0.02, triage, conv, role="triage", model=HAIKU, input_chars=7800 + 410 * i,
              tokens={"input_tokens": 2300 + 90 * i, "output_tokens": 84})
        if not smooth:
            ai = s.add("ai.journey_analyzer", t + triage + 0.03, length - triage - 0.06, conv,
                       "error" if failed else "ok", "TimeoutError: the AI Task didn't answer in 120 s" if failed else None,
                       role="analyzer", model=OPUS, input_chars=21400 + 1200 * i,
                       tokens=None if failed else {"input_tokens": 6100 + 300 * i, "output_tokens": 1400})
            for k, tool in enumerate(["get_snapshot", "list_entities", "get_snapshot"]):
                s.add(f"tool.{tool}", t + triage + 2.5 + 3.1 * k, 0.04 + 0.02 * k, ai, accepted=True)
        t += length + 0.2
    propose = s.add("stage.propose", 94.1, 65.9, run)
    p = s.add("propose", 94.15, 65.8, propose, sessions=3, status="completed", generator_run=CALL[3])
    s.add("evidence.load", 94.2, 0.38, p)
    gen = s.add("ai.hypothesis_generator", 94.6, 41.2, p, role="generator", model=OPUS, input_chars=48210,
                tokens={"input_tokens": 15400, "cache_read_input_tokens": 9800, "output_tokens": 3900})
    ask = s.add("tool.ask_reviewer", 110.0, 9.8, gen, accepted=True)
    s.add("evidence.check", 110.1, 0.21, ask)
    s.add("ai.hypothesis_reviewer", 136.2, 23.5, p, role="reviewer", model=OPUS, input_chars=30100,
          tokens={"input_tokens": 9900, "output_tokens": 1200})
    test = s.add("stage.test", 160.1, 129.4, run)
    t = 160.2
    for h, goals in enumerate([[(REC[0], 2, PLAN[0]), (REC[2], 1, PLAN[1])], [(REC[4], 3, PLAN[2])]]):
        length = 60.0 if h == 0 else 68.9
        hyp = s.add("test.hypothesis", t, length, test, hypothesis=f"h{h + 1}", goals=len(goals))
        g0 = t + 0.05
        for rid, turn, plan in goals:
            bad = rid == REC[2]
            glen = (length - 0.2) / len(goals) - 0.1
            goal = s.add("test.goal", g0, glen, hyp, "error" if bad else "ok",
                         "SandboxError: the sandbox didn't seed in 30 s" if bad else None,
                         goal=f"{rid[:8]}:{turn}", plan_id=plan, status="error" if bad else "improved")
            s.add("sandbox.start", g0 + 0.01, 4.2, goal)
            s.add("sandbox.seed", g0 + 4.3, 30.0 if bad else 1.9, goal, "error" if bad else "ok")
            if not bad:
                base = s.add("baseline", g0 + 6.3, 11.4, goal, status="ok")
                cand = s.add("candidate", g0 + 17.8, 13.2, goal, plan_id="p1", status="ok")
                for k in range(2):
                    for parent, start, replay in ((base, g0 + 6.4, "baseline"), (cand, g0 + 17.9, "candidate")):
                        turn_span = s.add("replay.turn", start + 5.5 * k, 5.3, parent, replay=replay, turn=k + 1)
                        s.add("replay.tool", start + 5.5 * k + 3.9, 0.12, turn_span, tool="HassTurnOn")
                s.add("ai.feedback_check", g0 + 31.1, 2.4, goal, role="feedback_check", model=HAIKU, input_chars=3100,
                      tokens={"input_tokens": 1100, "output_tokens": 40})
            s.add("sandbox.stop", g0 + glen - 1.2, 1.1, goal)
            g0 += glen + 0.1
        t += length + 0.1
    write = s.add("stage.write", 289.6, 22.3, run, "error", "RuntimeError: provider down")
    w = s.add("write", 289.65, 22.2, write, "error", "RuntimeError: provider down", improvement_id=IMP[0], status="failed")
    s.add("ai.editor", 289.7, 22.1, w, "error", "RuntimeError: provider down", role="editor", model=HAIKU, input_chars=5200)
    journal = [
        entry(7200 - 311.9, "info", "Run done", tags={"run_id": RUN_DONE}),
        entry(7200 - 311.8, "error", "Assist Improver: the write stage failed", logger="custom_components.assist_improver.pipeline",
              exception=TRACEBACK, tags={"run_id": RUN_DONE}),
        entry(7200 - 250, "warning", "test.goal blocked the event loop for 142 ms",
              tags={"run_id": RUN_DONE, "section": "test.goal", "goal": f"{REC[2][:8]}:1"}),
        entry(7200 - 200, "info", "Sandbox seed failed: the sandbox didn't seed in 30 s",
              tags={"run_id": RUN_DONE, "plan_id": PLAN[1], "goal": f"{REC[2][:8]}:1"}),
        entry(7200 - 150, "info", "Proposed 2 ideas from 3 conversations",
              tags={"run_id": RUN_DONE, "generator_run": CALL[3]}),
        entry(7200 - 80, "warning", "Analysis failed: TimeoutError: the AI Task didn't answer in 120 s",
              tags={"run_id": RUN_DONE, "recording_id": REC[4], "role": "analyzer"}),
        entry(7200 - 60, "debug", "Model said <img src=x onerror=alert(1)> & left the tag open <b>",
              tags={"run_id": RUN_DONE, "recording_id": REC[3], "role": "triage"}),
        entry(7200 - 1, "info", "Run started (manual)", tags={"run_id": RUN_DONE}),
    ]
    return {"id": RUN_DONE, "kind": "run", "started": started.isoformat(), "updated": (started + timedelta(seconds=313)).isoformat(),
            "finished": (started + timedelta(seconds=312)).isoformat(), "spans": sorted(s.spans, key=lambda x: x["at"]),
            "journal": journal, "open": [], "dropped": {"spans": 0, "journal": 0}}


def live_trace():
    """A run in progress: analysis done, the proposer still thinking."""
    started = NOW - timedelta(seconds=95)
    s = Spans(RUN_LIVE, started)
    analyze = s.add("stage.analyze", 0.05, 40.2, None)
    for i, rid in enumerate(REC[5:8]):
        conv = s.add("conversation", 0.1 + 13.3 * i, 13.1, analyze, recording_id=rid, status="completed",
                     triage="friction", answer_status="answered")
        s.add("ai.triage", 0.12 + 13.3 * i, 2.0, conv, role="triage", model=HAIKU)
        s.add("ai.journey_analyzer", 2.2 + 13.3 * i, 10.9, conv, role="analyzer", model=OPUS)
    # The run, the propose stage and the proposer are still open; their parent is the run span.
    run_id_span = hexid(f"{RUN_LIVE} open run", 12)
    for span in s.spans:
        if span["parent"] is None:
            span["parent"] = run_id_span
    open_spans = [
        {"id": run_id_span, "parent": None, "name": "run", "tags": {"run_id": RUN_LIVE, "trigger": "manual"}, "at": started.isoformat()},
        {"id": "p" * 12, "parent": run_id_span, "name": "stage.propose", "tags": {"run_id": RUN_LIVE},
         "at": (started + timedelta(seconds=40.3)).isoformat()},
        {"id": "q" * 12, "parent": "p" * 12, "name": "propose", "tags": {"run_id": RUN_LIVE, "sessions": 2},
         "at": (started + timedelta(seconds=40.4)).isoformat()},
        {"id": "r" * 12, "parent": "q" * 12, "name": "ai.hypothesis_generator",
         "tags": {"run_id": RUN_LIVE, "role": "generator", "model": OPUS}, "at": (started + timedelta(seconds=41.0)).isoformat()},
    ]
    return {"id": RUN_LIVE, "kind": "run", "started": started.isoformat(), "updated": NOW.isoformat(), "finished": None,
            "spans": sorted(s.spans, key=lambda x: x["at"]), "open": open_spans, "dropped": {"spans": 0, "journal": 0},
            "journal": [entry(54, "info", "Analysed 3 conversations", tags={"run_id": RUN_LIVE}),
                        entry(94, "info", "Run started (manual)", tags={"run_id": RUN_LIVE})]}


DONE_TRACE, LIVE_TRACE = done_trace(), live_trace()


def run_row(run_id, status, started_ago, seconds, trigger="manual", **extra):
    started = NOW - timedelta(seconds=started_ago)
    return {"id": run_id, "started": started.isoformat(),
            "finished": None if status == "running" else (started + timedelta(seconds=seconds)).isoformat(),
            "status": status, "trigger": trigger, "only": extra.pop("only", None), "stage": extra.pop("stage", None),
            "timings": extra.pop("timings", {}), "problems": extra.pop("problems", []), "counts": extra.pop("counts", {}),
            "error": extra.pop("error", None), "trace_id": run_id, "trace_available": extra.pop("trace", True)}


RUNS = [
    run_row(RUN_LIVE, "running", 95, 0, stage="propose", timings={"analyze": 40.2},
            counts={"taken_in": 3, "analyze_total": 3, "analyzed": 3, "skipped_as_smooth": 0, "sessions": 2}),
    run_row(RUN_DONE, "done", 7200, 312, timings={"analyze": 93.9, "propose": 65.9, "test": 129.4, "write": 22.3},
            problems=[{"stage": "write", "error": "RuntimeError: provider down"}],
            counts={"taken_in": 5, "analyze_total": 5, "analyzed": 4, "skipped_as_smooth": 2, "analyze_failed": 1,
                    "sessions": 3, "hypotheses": 2, "writing": 1, "new_improvements": 0}),
    run_row(RUN_SCHED, "done", 86400 + 3600, 141, trigger="scheduled",
            timings={"analyze": 61.0, "propose": 52.2, "test": 0.1, "write": 0.0},
            counts={"taken_in": 4, "analyzed": 4, "new_improvements": 1}),
    run_row(RUN_FAIL, "failed", 3 * 86400, 18, timings={"analyze": 17.9},
            error="CaseError: User turns and source snapshots do not align"),
    run_row(RUN_BLOCKED, "failed", 4 * 86400, 0, trigger="scheduled", trace=False,
            error="Analyze can't run: its AI model isn't available. Choose another in the settings."),
    run_row(RUN_OLD, "done", 12 * 86400, 233, only="analyze", trace=False, timings={"analyze": 232.8}),
]

STARTED = [3600 * (26 + 5 * n) for n in range(13)] + [300]  # Seconds ago; the last one is still going.
DAY0 = f"day_{(NOW - timedelta(seconds=STARTED[0])).astimezone():%Y%m%d}"  # Local dates, as trace.py names them.
# Where each stands in its flow record: (stage, outcome, held cause), as devview.conversation_rows reads it.
STAGE_OF = [("Your review", None, None), ("Your household", "looked smooth", None), ("Your household", "duplicate", None),
            ("Your household", "looked smooth", None), ("Analyze", None, "failures"), ("Your household", "no fix found", None),
            ("Your household", "improved", None), ("Write up", None, "blocked"), ("Your Assist", None, None),
            ("Your Assist", None, None), ("Your household", "couldn't be read", None),
            ("Your household", "marked as a test", None), ("Your Assist", None, None), ("Your Assist", None, None)]
WHY = {"failures": "Analyze failed 3 times in a row.", "blocked": "Write up is blocked."}
CONVERSATIONS = [{
    "id": rid, "short": rid[:8], "started": at(STARTED[n]), "ended": None if n == 13 else at(STARTED[n] - 90),
    "user_turns": 0 if n == 12 else 2 + n % 4, "status": "active" if n == 13 else "closed",
    "stored": "packed" if n > 9 else "full", "bytes": 4200 + 3100 * ((n * 7) % 11),
    "stage": stage, "round": 0 if stage == "Your Assist" else 1, "outcome": outcome,
    "held": {"cause": held, "why": WHY[held]} if held else None, "covered_by": IMP[1] if outcome == "duplicate" else None,
    "failed": 3 if held == "failures" else 1 if n == 0 else 0,
    "goals": 3 if outcome in (None, "duplicate", "no fix found", "improved") and stage != "Your Assist" else 0,
    "friction": 1 if outcome in (None, "duplicate", "no fix found", "improved") and stage != "Your Assist" else 0,
    "improvements": [IMP[0]] if n in (0, 6) else [], "replays": [PLAN[0]] if n == 0 else [],
    "runs": [RUN_DONE, RUN_SCHED] if n < 5 else [RUN_LIVE] if n < 8 else []}
    for n, (rid, (stage, outcome, held)) in enumerate(zip(REC, STAGE_OF))]
CONVERSATIONS.sort(key=lambda r: r["started"], reverse=True)

HEALTH = {
    "version": "0.41.0",
    "disk": {"stores": {"role_runs": 1843210, "improvements": 210443, "recordings_index": 18220, "replays": 108116,
                        "household": 3310},
             "recordings": {"count": 26, "bytes": 2914002, "largest": 318200}, "traces": {"count": 9, "bytes": 6120440},
             "recordings_by_storage": {"full": 17, "packed": 7, "compact": 2}},
    "memory": {"live recordings": {"items": 1, "bytes": 18320, "ms": 0.4}, "recordings index": {"items": 26, "bytes": 17110, "ms": 0.3},
               "run log": {"items": 412, "bytes": 1790233, "ms": 21.7}, "improvements": {"items": 9, "bytes": 205100, "ms": 2.2},
               "replays": {"items": 14, "bytes": 105900, "ms": 1.3},
               "hot spots": {"items": 31, "bytes": 3900, "ms": 0.1},
               "active traces": [{"id": f"day_{NOW.astimezone():%Y%m%d}", "kind": "day", "started": at(30000), "spans": 214, "journal": 61,
                                  "open": 0, "dropped": {"spans": 0, "journal": 0}},
                                 {"id": RUN_LIVE, "kind": "run", "started": at(95), "spans": 10, "journal": 2, "open": 4,
                                  "dropped": {"spans": 0, "journal": 0}}]},
    "now": {"run": {"running": True, "stage": "propose", "id": RUN_LIVE},
            "tasks": ["assist_improver_pipeline", "assist_improver_trace_save", "Task-212 (Recorder._flush_later)"],
            "sandboxes": {"started": 14, "start_seconds": 58.8, "slowest_start": 6.1, "assistants": 9,
                          "assistant_seconds": 12.6, "slowest_assistant": 1.9, "running": 0},
            "pending_writes": 1},
    "hot": [{"name": "ai.journey_analyzer", "count": 38, "total_ms": 612400.2, "max_ms": 41022.1, "last_at": at(60)},
            {"name": "ai.hypothesis_generator", "count": 6, "total_ms": 251990.0, "max_ms": 52010.4, "last_at": at(50)},
            {"name": "replay.turn", "count": 212, "total_ms": 1102233.9, "max_ms": 9120.0, "last_at": at(7000)},
            {"name": "ai.triage", "count": 41, "total_ms": 90110.5, "max_ms": 3900.2, "last_at": at(80)},
            {"name": "sandbox.start", "count": 14, "total_ms": 58800.0, "max_ms": 6100.0, "last_at": at(7000)},
            {"name": "store.role_runs", "count": 96, "total_ms": 4120.6, "max_ms": 188.2, "last_at": at(40)},
            {"name": "recorder.snapshot", "count": 133, "total_ms": 2210.4, "max_ms": 61.0, "last_at": at(900)},
            {"name": "tidy", "count": 1, "total_ms": 812.3, "max_ms": 812.3, "last_at": at(30000)}],
    "ai_calls": {"analyzer": {"calls": 38, "failed": 2, "seconds": 612.4, "slowest": 41.0,
                              "tokens": {"input_tokens": 251300, "output_tokens": 52100}},
                 "generator": {"calls": 6, "failed": 0, "seconds": 252.0, "slowest": 52.0,
                               "tokens": {"input_tokens": 92400, "cache_read_input_tokens": 58800, "output_tokens": 23100}},
                 "reviewer": {"calls": 6, "failed": 0, "seconds": 140.2, "slowest": 31.9, "tokens": {"input_tokens": 59400, "output_tokens": 7200}},
                 "triage": {"calls": 41, "failed": 0, "seconds": 90.1, "slowest": 3.9, "tokens": {"input_tokens": 96100, "output_tokens": 3500}},
                 "editor": {"calls": 3, "failed": 1, "seconds": 51.0, "slowest": 22.1, "tokens": {}}},
    "retention": {"deleted": 2, "packed": 3, "compacted": 1, "runs": {"deleted": 14}, "traces": {"deleted": 1, "bytes": 6120440}},
    "process": {"max_rss_bytes": 1183449088, "note": "The whole Home Assistant process. For CPU and memory profiles, use Home "
                                                     "Assistant's Profiler integration."},
}

HEALTH["hot"].sort(key=lambda r: -r["total_ms"])  # As devview.health sorts them.

LOGS = {"entries": [{**e, "trace_id": RUN_DONE} for e in DONE_TRACE["journal"]] + [
    {**entry(30000, "info", "Tidy-up: 2 recordings deleted, 3 packed, 1 compacted; 1 trace deleted"), "trace_id": f"day_{NOW.astimezone():%Y%m%d}"},
    {**entry(STARTED[0] - 90, "info", "Recording closed (4 user turns)", tags={"recording_id": REC[0]}), "trace_id": DAY0},
    {**entry(STARTED[0], "info", "Recording started", tags={"recording_id": REC[0]}), "trace_id": DAY0},
], "more": True}
LOGS["entries"].sort(key=lambda e: e["at"], reverse=True)

FIND_RECORDING = {
    "match": {"kind": "recording", "id": REC[0], "title": f"Conversation from {at(3600 * 2)}"},
    "record": {"started": at(STARTED[0]), "updated": at(STARTED[0] - 90), "user_turns": 4, "status": "closed", "stored": "full"},
    "timeline": [
        {"at": at(STARTED[0]), "kind": "recorded", "what": "Recording started (4 user turns so far)", "ids": {"recording": REC[0]}},
        {"at": at(STARTED[0] - 90), "kind": "closed", "what": "Closed: closed; stored full", "ids": {"recording": REC[0]}},
        {"at": at(86400 + 3500), "kind": "analysis", "what": "failed (error); TimeoutError: the AI Task didn't answer in 120 s",
         "ids": {"role_call": CALL[0], "run": RUN_SCHED}},
        {"at": at(7150), "kind": "analysis", "what": "completed (answered); triage: needs analysis; 3 goals, 1 with friction",
         "ids": {"role_call": CALL[1], "run": RUN_DONE}},
        {"at": at(7050), "kind": "proposal", "what": "Read by the proposer (completed)", "ids": {"role_call": CALL[3], "run": RUN_DONE}},
        {"at": at(7040), "kind": "replay", "what": f"Replay without the change (try 1) of {REC[0][:8]}: reproduced",
         "ids": {"replay": PLAN[0]}},
        {"at": at(6900), "kind": "improvement", "what": f"Improvement covers goal(s) {REC[0][:8]}:2: Open or close garage doors "
                                                          "by pressing their buttons (awaiting_review)", "ids": {"improvement": IMP[0]}}] + [
        {"at": at(seconds), "kind": "stage", "what": f"Round 1: {stage}", "ids": {"recording": REC[0]}}
        for seconds, stage in ((86400 + 3590, "Your Assist"), (86400 + 3580, "Triage"), (86400 + 3570, "Analyze"),
                               (7100, "Propose"), (6950, "Write up"), (6890, "Your review"))] + [
        {"at": at(86400 + 3500), "kind": "problem", "what": "Round 1, Analyze: failed: TimeoutError: the AI Task didn't "
                                                             "answer in 120 s", "ids": {"run": RUN_SCHED}}],
    "journal": [e for e in LOGS["entries"] if e["tags"].get("recording_id") == REC[0]] + [
        {**entry(7150, "debug", "Triage: needs analysis", tags={"run_id": RUN_DONE, "recording_id": REC[0], "role": "triage"}),
         "trace_id": RUN_DONE}],
    "trace_id": None,
}
FIND_RECORDING["journal"].sort(key=lambda e: e["at"], reverse=True)
FIND_RECORDING["timeline"].sort(key=lambda e: e["at"])  # As devview.lookup sorts it.
FIND_RUN = {
    "match": {"kind": "run", "id": RUN_DONE, "title": f"pipeline done {at(7200)}"},
    "record": {"id": RUN_DONE, "status": "done", "trigger": "manual", "only": None, "started": RUNS[1]["started"],
               "finished": RUNS[1]["finished"], "stage": "write", "error": None, "trace_id": RUN_DONE,
               "progress": {**RUNS[1]["counts"], "timings": RUNS[1]["timings"], "problems": RUNS[1]["problems"]}},
    "timeline": [{"at": RUNS[1]["started"], "kind": "run", "what": "Run started (manual)", "ids": {"run": RUN_DONE}}] + [
        {"at": at(7200 - 10 * n), "kind": "role_call", "what": what, "ids": {"role_call": CALL[n], **({"recording": rid} if rid else {})}}
        for n, (what, rid) in enumerate([("triage: completed (triaged_smooth) in 2.4 s", REC[1]),
                                         ("analyzer: completed (answered) in 15.2 s", REC[0]),
                                         ("analyzer: failed (error) in 120.0 s", REC[4]),
                                         ("generator: completed in 41.2 s", None),
                                         ("editor: failed in 22.1 s", None)], start=1)] + [
        {"at": RUNS[1]["finished"], "kind": "problem", "what": "write: RuntimeError: provider down", "ids": {"run": RUN_DONE}},
        {"at": RUNS[1]["finished"], "kind": "finished", "what": "Run done", "ids": {"run": RUN_DONE}}],
    "journal": [{**e, "trace_id": RUN_DONE} for e in DONE_TRACE["journal"]],
    "trace_id": RUN_DONE,
}
FIND_MATCHES = {"matches": [{"kind": "recording", "id": REC[3], "title": f"Conversation from {at(3600 * 17)}"},
                            {"kind": "role_call", "id": REC[3][:4] + CALL[5][4:], "title": "journey_analyzer completed"},
                            {"kind": "improvement", "id": REC[3][:4] + IMP[1][4:], "title": "Turn on the porch light at sunset"}]}

ROLE = {"large": {"entity_id": OPUS, "size": "large", "optional": False, "problem": None},
        "small": {"entity_id": HAIKU, "size": "small", "optional": False, "problem": None}}
SETTINGS = {"roles": {"triage": {**ROLE["small"], "optional": True}, "analyzer": ROLE["large"], "generator": ROLE["large"],
                      "reviewer": ROLE["large"], "feedback_check": {**ROLE["small"], "optional": True}, "editor": ROLE["small"]},
            "ai_tasks": [{"entity_id": OPUS, "label": "Opus 5.5", "title": "Claude Opus 5.5", "provider": "anthropic", "warnings": [],
                          "strong": True, "weak": False, "small": False, "known": True},
                         {"entity_id": HAIKU, "label": "Haiku 4.5", "title": "Claude Haiku 4.5", "provider": "anthropic",
                          "warnings": [], "strong": False, "weak": True, "small": True, "known": True}],
            "providers": [], "recommended": True, "tests_per_goal": 3, "schedule": "off", "paused": [], "developer": True}
DEV = {"assist_improver/dev/runs": {"runs": RUNS}, "assist_improver/dev/conversations": {"rows": CONVERSATIONS},
       "assist_improver/dev/health": HEALTH, "assist_improver/dev/journal": LOGS,
       f"assist_improver/dev/lookup {REC[0][:8]}": FIND_RECORDING, f"assist_improver/dev/lookup {RUN_DONE}": FIND_RUN,
       f"assist_improver/dev/lookup {REC[3][:4]}": FIND_MATCHES,
       f"assist_improver/dev/trace {RUN_DONE}": {"trace": DONE_TRACE}, f"assist_improver/dev/trace {RUN_LIVE}": {"trace": LIVE_TRACE},
       "assist_improver/settings": SETTINGS}
DEVELOPER = "?view=developer&tab="
# The settings as a household sees them (no developer mode), with stand-in household lines.
LEARNED = {"lines": 6, "everyone": ["Quiet hours start at 22:00.", "The garage has two doors, opened by relays.",
                                    "The lounge lamp is the one by the window."],
           "people": [{"user_id": "u1", "name": "Alex", "lines": ["Alex likes the bedroom at 19 degrees.",
                                                                 "Alex means the streaming box by the TV.",
                                                                 "Alex wants short answers."]}],
           "me": {"user_id": "u1", "name": "Alex"}, "pipelines": [{"id": "p1", "name": "Home", "preferred": True}]}


def household_view(**extra):
    return {"run": {**RUN, "status": "done"}, "search": "?view=settings", **extra, "replies": {
        "assist_improver/settings": {**SETTINGS, "developer": False},
        "assist_improver/household": LEARNED, "assist_improver/log": {"events": []}}}


# The settings' Uninstall: the household's lines as one text to copy (household.plain, as panel.ws_household sends it).
LEARNED["text"] = "\n".join(f"- {line}" for line in LEARNED["everyone"]) + "\n\nFor Alex:\n" + "\n".join(
    f"- {line}" for line in LEARNED["people"][0]["lines"])
NOTHING = {**LEARNED, "lines": 0, "everyone": [], "people": [], "text": ""}


def uninstall_view(learned=LEARNED, **extra):
    view = household_view(**extra)
    view["replies"]["assist_improver/household"] = learned
    return {**view, "clicks": ["assist-improver-panel #uninstall", *extra.get("clicks", [])]}


TICK = ["assist-improver-panel #u-saved"]
COPY = ["assist-improver-panel #u-copy"]


SCENARIOS = {
    # A run that stopped on an unexpected error.
    "failed": {"run": {**RUN, "status": "failed", "error": "CaseError: User turns and source snapshots do not align"}},
    # A step whose model is missing: something the household can fix.
    "blocked": {"run": {**RUN, "status": "failed", "trigger": "scheduled",
                        "error": "Analyze can't run: its AI model isn't available. Choose another in the settings."}},
    # A run that finished, with one stage that couldn't.
    "problems": {"run": {**RUN, "status": "done", "progress": {
        "taken_in": 4, "analyzed": 4, "unreadable": 1,
        "problems": [{"stage": "propose", "error": "RuntimeError: provider down"}]}}},
    "fine": {"run": {**RUN, "status": "done", "progress": {"taken_in": 4, "analyzed": 4}}},
    # Developer mode on: the Developer link in home's and the settings' headers.
    "home-dev": {"run": {**RUN, "status": "done", "progress": {"taken_in": 4, "analyzed": 4}}, "replies": DEV},
    "settings-dev": {"run": {**RUN, "status": "done"}, "search": "?view=settings",
                     "replies": {**DEV, "assist_improver/log": {"events": []}}},
    # The developer view, a scenario per tab.
    "dev-find": {"search": f"{DEVELOPER}find&q={REC[0][:8]}", "replies": DEV, "full": True},
    "dev-find-light": {"search": f"{DEVELOPER}find&q={REC[0][:8]}", "replies": DEV, "full": True, "theme": "light"},
    "dev-find-desktop": {"search": f"{DEVELOPER}find&q={REC[0][:8]}", "replies": DEV, "full": True, "desktop": True},
    "dev-find-desktop-light": {"search": f"{DEVELOPER}find&q={REC[0][:8]}", "replies": DEV, "full": True,
                               "desktop": True, "theme": "light"},
    "dev-find-run": {"search": f"{DEVELOPER}find&q={RUN_DONE}", "replies": DEV, "full": True},
    "dev-find-matches": {"search": f"{DEVELOPER}find&q={REC[3][:4]}", "replies": DEV, "full": True},
    "dev-conversations": {"search": f"{DEVELOPER}conversations", "replies": DEV, "full": True},
    "dev-conversations-light": {"search": f"{DEVELOPER}conversations", "replies": DEV, "full": True, "theme": "light"},
    "dev-conversations-desktop": {"search": f"{DEVELOPER}conversations", "replies": DEV, "full": True, "desktop": True},
    "dev-conversations-desktop-light": {"search": f"{DEVELOPER}conversations", "replies": DEV, "full": True,
                                        "desktop": True, "theme": "light"},
    "dev-conversations-filtered": {"search": f"{DEVELOPER}conversations", "replies": DEV, "full": True,
                                   "clicks": ["assist-improver-panel button[data-stage='Your household']"]},
    "dev-runs": {"search": f"{DEVELOPER}runs", "replies": DEV, "full": True},
    "dev-run": {"search": f"{DEVELOPER}runs&q={RUN_DONE}", "replies": DEV, "full": True,
                "clicks": ["assist-improver-panel .wf-row:has(.wf-label[title^='test.hypothesis']) .wf-tog",
                           f"assist-improver-panel .wf-row:has(.wf-label[title*='goal={REC[2][:8]}']) .wf-tog",
                           "assist-improver-panel .wf-label[title^='sandbox.seed']"]},
    "dev-run-light": {"search": f"{DEVELOPER}runs&q={RUN_DONE}", "replies": DEV, "full": True, "theme": "light"},
    "dev-run-desktop": {"search": f"{DEVELOPER}runs&q={RUN_DONE}", "replies": DEV, "full": True, "desktop": True,
                        "clicks": ["assist-improver-panel #wf-depth"]},
    "dev-run-live": {"search": f"{DEVELOPER}runs&q={RUN_LIVE}", "replies": DEV, "full": True},
    "dev-health": {"search": f"{DEVELOPER}health", "replies": DEV, "full": True},
    "dev-health-light": {"search": f"{DEVELOPER}health", "replies": DEV, "full": True, "theme": "light"},
    "dev-health-desktop": {"search": f"{DEVELOPER}health", "replies": DEV, "full": True, "desktop": True},
    "dev-logs": {"search": f"{DEVELOPER}logs", "replies": DEV, "full": True,
                 "clicks": ["assist-improver-panel .dv-log details summary"]},
    "household": household_view(),
    "household-light": household_view(theme="light"),
    "household-desktop": household_view(desktop=True),
    "household-desktop-light": household_view(desktop=True, theme="light"),
    # The end of the settings, with Uninstall; then Uninstall's step, with the lines to copy, just copied, and ticked.
    "settings-end": household_view(full=True),
    "settings-end-light": household_view(full=True, theme="light"),
    "settings-end-desktop": household_view(full=True, desktop=True),
    "settings-end-desktop-light": household_view(full=True, desktop=True, theme="light"),
    "uninstall": uninstall_view(),
    "uninstall-light": uninstall_view(theme="light"),
    "uninstall-desktop": uninstall_view(desktop=True),
    "uninstall-desktop-light": uninstall_view(desktop=True, theme="light"),
    "uninstall-copied-desktop-light": uninstall_view(clicks=COPY, desktop=True, theme="light"),
    "uninstall-ticked": uninstall_view(clicks=TICK),
    "uninstall-ticked-desktop-light": uninstall_view(clicks=TICK, desktop=True, theme="light"),
    # With no lines learned: the plain confirmation.
    "uninstall-nothing": uninstall_view(NOTHING),
    "uninstall-nothing-light": uninstall_view(NOTHING, theme="light"),
    "uninstall-nothing-desktop": uninstall_view(NOTHING, desktop=True),
    "uninstall-nothing-desktop-light": uninstall_view(NOTHING, desktop=True, theme="light"),
    "dev-off": {"search": f"{DEVELOPER}runs", "full": True, "replies": {
        "assist_improver/dev/runs": {"__error": {"code": "developer_mode_off", "message": "Developer mode is off."}}}},
}


def render(page, scenario):
    spec = SCENARIOS[scenario]
    replies = {"assist_improver/improvements": {"improvements": EARLIER},
               "assist_improver/household": HOUSEHOLD,
               "assist_improver/run": {"run": spec.get("run", RUN), "preview": PREVIEW}, **spec.get("replies", {})}
    light = spec.get("theme") == "light"
    html = f"<!doctype html><html><head><style>{LIGHT if light else THEME}</style></head><body></body></html>"
    def serve(route):  # www/'s files at the static path; the page at every other address.
        path = route.request.url.split("?")[0].removeprefix(ORIGIN)
        if path.startswith(STATIC):
            route.fulfill(status=200, content_type="text/javascript", body=(WWW / path[len(STATIC):]).read_text())
        else:
            route.fulfill(status=200, content_type="text/html", body=html)
    page.route(f"{ORIGIN}/**", serve)
    page.goto(f"{ORIGIN}/assist-improver{spec.get('search', '')}")
    page.evaluate("(url) => import(url).then(() => true)", f"{ORIGIN}{STATIC}panel.js?v=harness")  # As module_url loads it.
    page.evaluate("""([replies, dark]) => {
        const el = document.createElement("assist-improver-panel");
        document.body.appendChild(el);
        el.narrow = true;
        el.hass = {themes: {darkMode: dark}, callWS: async (msg) => {
            const key = `${msg.type} ${msg.query ?? msg.trace_id ?? ""}`.trim();
            const reply = key in replies ? replies[key] : replies[msg.type];
            if (reply === undefined) throw new Error("No stub for " + msg.type);
            if (reply && reply.__error) throw reply.__error;  // As Home Assistant rejects: {code, message}.
            return JSON.parse(JSON.stringify(reply));
        }};
    }""", [replies, not light])
    page.wait_for_timeout(400)
    for selector in spec.get("clicks", []):
        page.locator(selector).first.click(timeout=5000)
        page.wait_for_timeout(150)


def main(out, *names, desktop=False):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    errors, sideways = [], []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        contexts = {}
        for name in names or SCENARIOS:
            spec = SCENARIOS[name]
            wide, light = desktop or spec.get("desktop", False), spec.get("theme") == "light"
            if (wide, light) not in contexts:
                size = {"width": 1280, "height": 800} if wide else {"width": 402, "height": 874}
                contexts[wide, light] = browser.new_context(viewport=size, device_scale_factor=2,
                                                            color_scheme="light" if light else "dark")
            page = contexts[wide, light].new_page()  # A fresh page each: panel.js defines its globals once.
            page.on("pageerror", lambda e, name=name: errors.append(f"{name}: {e}"))
            page.on("console", lambda m, name=name: m.type == "error" and errors.append(f"{name}: {m.text}"))
            render(page, name)
            if page.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth"):
                sideways.append(name)
            page.screenshot(path=str(out / f"{name}.png"), full_page=spec.get("full", False))
            if page.locator("assist-improver-panel .notice summary").count():
                page.locator("assist-improver-panel .notice summary").first.click()
                page.wait_for_timeout(200)
                page.screenshot(path=str(out / f"{name}-details.png"))
            page.close()
        browser.close()
    print(json.dumps({"saved": sorted(p.name for p in out.glob("*.png")), "page_errors": errors,
                      "scrolls_sideways": sideways}))


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    main(*args, desktop="--desktop" in sys.argv)  # --desktop: every scenario in a desktop-width window.
