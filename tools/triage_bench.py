"""A small benchmark for the quick check (triage): a fixed set of real conversations with known answers, run through
the Claude Code CLI with the model settings you pick, scored. Run with the repo's Python, from the repo root (or with
full paths, from anywhere):

  .venv/bin/python tools/triage_bench.py --model haiku                       every case once, on Haiku
  .venv/bin/python tools/triage_bench.py --model sonnet --effort low -n 3 -j 8   three tries per case, 8 at a time
  .venv/bin/python tools/triage_bench.py --model haiku --prompt-file p.txt --cases 1a2b,3c4d
  .venv/bin/python tools/triage_bench.py --model haiku --no-web              without the web tools
  .venv/bin/python tools/triage_bench.py freeze                              rebuild the cases (after code changes)

The right decision: skip the analysis for a smooth conversation, analyse one with friction (only a sure "smooth"
skips, as in production). A skipped friction case hides a problem, so any one fails the run. A call that fails or
gives no valid answer counts as wrong and as an error (production retries the check a few times in a run, then holds
the conversation). Answers come only from the household's own judgement, never a model's: the labelled sessions
(extra turns or a goal unmet = friction), and conversations with an improvement the household accepted (friction).
freeze reads local copies of the improvements store and those recordings in triage-bench/{stores,recordings} in the
private folder (tools/private.py; copied read-only from HA over SSH), and writes each case's input as the quick check
sees it today, so the benchmark stays fixed while code changes. Cases, runs (triage-bench/runs/) and copies stay there,
out of the checkout.

Each call is laid out as production's (Home Assistant's Anthropic integration, called by reflective.run_role). The
system prompt is Home Assistant's own for an AI Task: its default prompt and its date/time line, imported from the
installed homeassistant package and dated at the case's last turn in the household's time zone (the earliest a check
can run). The one user message is run_role's own text (reflective.task_text): the quick check's instructions, then
the case's input as JSON; --prompt-file puts other instructions in their place. The answer is forced as production
forces it, by constrained decoding (output_config.format with the answer's schema as Home Assistant builds it, passed
through CLAUDE_CODE_EXTRA_BODY), and read as Home Assistant reads it: the reply's text, parsed as JSON. The same extra
body sends production's web search and fetch tools (5 uses each, without the household's location; --no-web leaves
them out); where production carries on after a long search (pause_turn), the CLI ends the call, an error here.
Output is capped at production's 3000 tokens, thinking included. Where production stops at the cap and the check
fails, the CLI asks the model to go on, so a call that took more than one turn counts as that failure. Each result
keeps its output tokens and web requests.

The CLI: --claude PATH, else $CLAUDE_BIN, else `claude` on PATH, else the newest copy the Claude app bundles. It
needs a signed-in CLI (`<cli> auth login` once; the Claude app's own sign-in doesn't reach a terminal), and costs
are its own estimates. Each call runs in an empty temp dir, without the calling session's CLAUDE* env, with
--safe-mode, --setting-sources "" and --tools "": no CLAUDE.md, auto-memory, skills, hooks, plugins, MCP servers,
settings files or Claude Code tools reach the model. What the CLI still adds (measured on CLI 2.1.309; no flag or
setting takes them out short of --bare, which drops the claude.ai sign-in): its billing header and a one-line SDK
identity before the system prompt; a note with the signed-in account's email before the user message; and after it
a note on its environment (temp dir, OS, shell, model name, token budget, today's real date).
"""

import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import anthropic
import jinja2
import probatio

sys.path.insert(0, str(Path(__file__).resolve().parent))
from triage_check import LABELS, PRIVATE, friction, labelled, view  # noqa: E402  (also sets up the import path)
from custom_components.assist_improver import triage  # noqa: E402
from custom_components.assist_improver.reflective import task_text  # noqa: E402
from homeassistant.components.ai_task.const import DEFAULT_SYSTEM_PROMPT  # noqa: E402
from homeassistant.helpers import llm  # noqa: E402
from custom_components.assist_improver.exploration import SessionView  # noqa: E402
from custom_components.assist_improver.retention import unpack  # noqa: E402

BENCH = PRIVATE / "triage-bench"
TIMEOUT = 180
MAX_TOKENS = 3000  # Production's (the AI Task's max_tokens).
# The answer's schema as Home Assistant's Anthropic integration builds it from the structure.
SCHEMA = anthropic.transform_schema(probatio.to_openapi(triage.STRUCTURE, custom_serializer=llm.selector_serializer,
                                                        openapi_version="3.1.0"))
# Production's server tools (its AI Task model turns both on, with code execution, which picks these types),
# without the household's location.
TOOLS = [{"name": "web_search", "type": "web_search_20260209", "max_uses": 5},
         {"name": "web_fetch", "type": "web_fetch_20260209", "max_uses": 5}]


def answer_env(web):
    """The answer forced as production forces it (constrained decoding), its tools (unless web is off), and output
    capped as production's."""
    body = {"output_config": {"format": {"type": "json_schema", "schema": SCHEMA}}, **({"tools": TOOLS} if web else {})}
    return {"CLAUDE_CODE_EXTRA_BODY": json.dumps(body), "CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(MAX_TOKENS)}


# --- freeze ---------------------------------------------------------------------------------------------------------

def freeze():
    cases = []
    for short, label in labelled().items():
        rid = label["recording_id"]
        f = friction(label)
        unmet = sorted({g["turn"] for g in label["goals"] if g["kind"] == "ask" or g.get("reached") is False})
        says = ([f"extra turn at {turns(f['extra_turns'])}"] * bool(f["extra_turns"])
                + [f"goal left unmet at {turns(unmet)}"] * bool(unmet)) or ["labelled smooth"]
        folder = next(d for d in LABELS if (PRIVATE / d / f"{rid}.json").exists())
        cases.append(case(short, rid, f"label ({folder})", "friction" if f["any"] else "smooth", "; ".join(says),
                          view(label)))
    seen = {c["recording_id"] for c in cases}
    store = json.loads((BENCH / "stores/assist_improver.improvements").read_text())["data"]["improvements"]
    accepted = {}
    for item in store.values():
        rid = item["conversation"]
        if any(d.get("decision") == "accept" and not d.get("auto") for d in item.get("decisions") or []):
            accepted.setdefault(rid, set()).update(f.split(":")[1] for f in item.get("fixes") or []
                                                   if f.startswith(rid[:8]))
    for rid, fixed in sorted(accepted.items()):
        if rid in seen:
            continue
        path = next((p for p in (BENCH / "recordings" / f"{rid}.json",
                                 PRIVATE / "recordings" / f"{rid}.json") if p.exists()), None)
        if path is None:
            sys.exit(f"No recording for {rid}: copy it into {BENCH / 'recordings'} first")
        cases.append(case(rid[:8], rid, "accepted fix", "friction",
                          f"accepted a fix for it ({turns(sorted(fixed, key=int))})",
                          SessionView(unpack(json.loads(path.read_text())["data"]))))
    out = BENCH / "cases"
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("*.json"):
        old.unlink()
    for c in cases:
        (out / f"{c['id']}.json").write_text(json.dumps(c, indent=1, ensure_ascii=False))
    kinds = Counter(c["expected"] for c in cases)
    print(f"Froze {len(cases)} cases ({kinds['friction']} friction, {kinds['smooth']} smooth) into {out}")


def turns(numbers):
    return f"turn{'s' * (len(numbers) > 1)} {', '.join(map(str, numbers))}"


def case(short, rid, source, expected, says, session):
    data = triage.payload(session)
    return {"id": short, "recording_id": rid, "source": source, "expected": expected, "label_says": says,
            "turns": len(data["turns"]), "time_zone": session.source.get("time_zone") or "UTC", "input": data}


# --- run ------------------------------------------------------------------------------------------------------------

def find_claude(given):
    found = given or os.environ.get("CLAUDE_BIN") or shutil.which("claude")
    if not found:
        bundled = glob.glob(os.path.expanduser(
            "~/Library/Application Support/Claude/claude-code/*/*/claude.app/Contents/MacOS/claude"))
        version = lambda p: tuple(int(x) for x in re.findall(r"\d+", Path(p).parents[4].name))  # noqa: E731
        found = max(bundled, key=version, default=None)
    if not found:
        sys.exit("No Claude Code CLI found: pass --claude PATH or set CLAUDE_BIN")
    return found


def clean_env():
    """The caller's environment without the CLAUDE* variables a calling Claude session sets (its session, effort,
    host sockets), plus switches that keep CLAUDE.md and auto-memory out (a safeguard: the flags already do)."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("CLAUDE") or k in ("CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN")}
    return {**env, "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1", "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "CLAUDE_CODE_DISABLE_BUNDLED_SKILLS": "1"}


def system_prompt(case):
    """Home Assistant's system prompt for an AI Task (its default prompt, then its date/time line, each rendered and
    stripped, joined by a line break), at the case's last turn in the household's time zone."""
    if "time_zone" not in case:
        sys.exit("The cases predate the household's time zone: run freeze again")
    at = datetime.fromisoformat(case["input"]["turns"][-1]["at"]).astimezone(ZoneInfo(case["time_zone"]))
    return f"{DEFAULT_SYSTEM_PROMPT}\n{jinja2.Template(llm.DATE_TIME_PROMPT).render(now=lambda: at).strip()}"


def command(claude, model, effort, system):
    return [claude, "-p", "--model", model, *(["--effort", effort] if effort else []),
            "--system-prompt", system, "--tools", "", "--strict-mcp-config", "--disable-slash-commands",
            "--safe-mode", "--setting-sources", "", "--no-session-persistence", "--output-format", "json"]


def signed_in(claude):
    """Exits with what to do if the CLI isn't signed in (no model call): the calls would all fail at once."""
    try:
        status = json.loads(subprocess.run([claude, "auth", "status"], capture_output=True, text=True,
                                           env=clean_env(), timeout=30).stdout)
    except (ValueError, subprocess.SubprocessError):
        return
    if status.get("authMethod") == "none":
        sys.exit(f"The Claude Code CLI isn't signed in. Sign in once, then run this again:\n  \"{claude}\" auth login")


def cli(cmd, message, web):
    """One CLI call in a fresh empty dir: {text, error, cost, output_tokens, web_requests, seconds}."""
    started, out = time.monotonic(), {"text": None, "error": None, "cost": 0.0, "output_tokens": None,
                                      "web_requests": None}
    with tempfile.TemporaryDirectory() as cwd:
        try:
            p = subprocess.run(cmd, input=message, capture_output=True, text=True, cwd=cwd,
                               env={**clean_env(), **answer_env(web)}, timeout=TIMEOUT)
        except subprocess.TimeoutExpired:
            return {**out, "error": f"timed out after {TIMEOUT} s", "seconds": TIMEOUT}
    out["seconds"] = round(time.monotonic() - started, 1)
    try:
        result = json.loads(p.stdout)
    except ValueError:
        return {**out, "error": f"exit {p.returncode}: {(p.stderr or p.stdout).strip()[-300:]}"}
    if not isinstance(result, dict):
        return {**out, "error": f"exit {p.returncode}: unexpected output {p.stdout.strip()[:300]}"}
    out["cost"] = result.get("total_cost_usd") or 0.0
    usage = result.get("usage") or {}
    server = usage.get("server_tool_use") or {}
    out["output_tokens"] = usage.get("output_tokens")
    out["web_requests"] = (server.get("web_search_requests") or 0) + (server.get("web_fetch_requests") or 0)
    if result.get("is_error") or p.returncode or not isinstance(result.get("result"), str):
        return {**out, "error": f"{result.get('terminal_reason') or result.get('subtype')}: "
                                f"{str(result.get('result'))[:300]}"}
    if (turns := result.get("num_turns") or 1) > 1:  # Production raises on the first reply here.
        return {**out, "error": f"took {turns} turns: the first reply hit the {MAX_TOKENS}-token cap or had no text"}
    return {**out, "text": result["result"]}


def ask(cmd, message, web):
    """One case: {answer, error, cost, output_tokens, web_requests, seconds}. The reply's text must parse as JSON into
    {smooth, sure, why}, as Home Assistant reads it; no valid answer is an error."""
    got = cli(cmd, message, web)
    text, got["answer"] = got.pop("text"), None
    if text is not None:
        try:
            got["answer"] = triage.STRUCTURE(json.loads(text))
        except Exception as err:  # noqa: BLE001 - any reply that isn't {smooth, sure, why} is no answer.
            got["error"] = f"no valid answer ({err}): {text[:200]}"
    return got


def run(args):
    cases = [json.loads(p.read_text()) for p in sorted((BENCH / "cases").glob("*.json"))]
    if args.cases:
        cases = [c for c in cases if any(c["id"].startswith(x) for x in args.cases.split(","))]
    if not cases:
        sys.exit("No cases: run freeze first, or check --cases")
    instructions = Path(args.prompt_file).read_text().rstrip("\n") if args.prompt_file else triage.INSTRUCTIONS
    claude = find_claude(args.claude)
    signed_in(claude)
    jobs = [(c, k) for c in cases for k in range(args.n)]
    web = "on" if args.web else "off"
    print(f"{len(cases)} cases x {args.n} on {args.model} (effort {args.effort or 'default'}, web tools {web}), "
          f"{args.j} at a time", file=sys.stderr)
    started, results = time.monotonic(), []
    with ThreadPoolExecutor(args.j) as pool:
        futures = {pool.submit(ask, command(claude, args.model, args.effort, system_prompt(c)),
                               task_text(instructions, c["input"]), args.web): (c, k) for c, k in jobs}
        for done, future in enumerate(as_completed(futures), 1):
            c, k = futures[future]
            got = future.result()
            decision = "skip" if triage.skips_analysis(got["answer"]) else "analyse"
            results.append({"id": c["id"], "try": k + 1, "expected": c["expected"], "label_says": c["label_says"],
                            "decision": decision, "web": args.web,
                            "right": not got["error"] and (decision == "skip") == (c["expected"] == "smooth"),
                            **got})
            print(f"  {done}/{len(jobs)}", end="\r", file=sys.stderr)
    print(file=sys.stderr)
    seconds = time.monotonic() - started
    results.sort(key=lambda r: (r["expected"], r["id"], r["try"]))
    for r in results:
        a = r["answer"]
        how = f"{'smooth' if a['smooth'] else 'not smooth'}, {'sure' if a['sure'] else 'unsure'}: {a['why']}" if a \
            else f"ERROR {r['error']}"
        print(f"{'right' if r['right'] else 'WRONG'}  {r['id']}{'#%d' % r['try'] if args.n > 1 else ''}  "
              f"{r['expected']:8}  {r['decision']:7}  ({how})")
    friction_runs = [r for r in results if r["expected"] == "friction"]
    smooth_runs = [r for r in results if r["expected"] == "smooth"]
    score = {"right": sum(r["right"] for r in results), "of": len(results),
             "friction_skipped": sum(r["decision"] == "skip" for r in friction_runs), "friction": len(friction_runs),
             "smooth_skipped": sum(r["decision"] == "skip" for r in smooth_runs), "smooth": len(smooth_runs),
             "errors": sum(bool(r["error"]) for r in results),
             "web_requests": sum(r["web_requests"] or 0 for r in results),
             "cost_usd": round(sum(r["cost"] for r in results), 4), "seconds": round(seconds)}
    varied = {}
    if args.n > 1:
        for c in cases:
            got = Counter(r["decision"] for r in results if r["id"] == c["id"])
            if len(got) > 1:
                varied[c["id"]] = dict(got)
        print(f"Decision varied on {len(varied)}/{len(cases)} cases"
              + "".join(f"\n  {i} ({c['expected']}): " + ", ".join(f"{d} {n}/{args.n}" for d, n in v.items())
                        for i, v in varied.items() for c in cases if c["id"] == i))
    print(f"Score: {score['right']}/{score['of']} right ({100 * score['right'] / score['of']:.0f}%) · "
          f"friction skipped: {score['friction_skipped']}/{score['friction']} (must be 0) "
          f"{'PASS' if not score['friction_skipped'] else 'FAIL'} · "
          f"smooth skipped: {score['smooth_skipped']}/{score['smooth']} · errors {score['errors']} · "
          f"web tools {web}, {score['web_requests']} requests · "
          f"cost ${score['cost_usd']:.2f} · time {int(seconds // 60)}m{int(seconds % 60):02d}s")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    name = f"{stamp}-{args.model}-{args.effort or 'default'}{'-no-web' * (not args.web)}.json"
    path = BENCH / "runs" / re.sub(r"[^\w.-]", "_", name)
    path.parent.mkdir(parents=True, exist_ok=True)
    version = subprocess.run([claude, "--version"], capture_output=True, text=True, env=clean_env(),
                             timeout=30).stdout.strip()
    path.write_text(json.dumps({
        "model": args.model, "effort": args.effort, "web": args.web, "repeats": args.n, "parallel": args.j,
        "cases": len(cases), "claude": claude, "claude_version": version,
        "command": command("claude", args.model, args.effort, "<system_prompt>")[1:], "env": answer_env(args.web),
        "system_prompt": f"{DEFAULT_SYSTEM_PROMPT}\n{llm.DATE_TIME_PROMPT.strip()}",  # Dated per case.
        "prompt_file": args.prompt_file, "instructions": instructions,
        "score": score, "varied": varied, "results": results}, indent=1, ensure_ascii=False))
    print(f"Saved {path}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", nargs="?", choices=("run", "freeze"), default="run")
    parser.add_argument("--model", default="haiku", help="haiku, sonnet, opus or a full model id (default haiku)")
    parser.add_argument("--effort", help="low, medium, high, xhigh or max (default: the CLI's, medium on haiku today)")
    parser.add_argument("--prompt-file", help="instructions to test in place of the quick check's own (in the user "
                                              "message, before the input, where production puts them)")
    parser.add_argument("-n", type=int, default=1, help="tries per case (default 1)")
    parser.add_argument("-j", type=int, default=4, help="calls at a time (default 4)")
    parser.add_argument("--cases", help="only these cases: comma-separated id prefixes")
    parser.add_argument("--no-web", dest="web", action="store_false",
                        help="send no tools, as Home Assistant does with web search, web fetch and code execution "
                             "all off (default: the web tools, as production does today)")
    parser.add_argument("--claude", help="path to the Claude Code CLI")
    args = parser.parse_args()
    freeze() if args.command == "freeze" else run(args)


if __name__ == "__main__":
    main()
