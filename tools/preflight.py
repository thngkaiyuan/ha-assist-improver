"""A check before each install, with no model: every deterministic step each stage takes, on every stored conversation.

It copies the improver's stored data from the household's Home Assistant (read-only: tar over the SSH access
tools/deploy.sh uses) into preflight/storage/ in the private folder (tools/private.py: household data stays out of the
checkout), then runs this checkout's code, with its replay workers, on a scratch copy of it:

- each conversation: reading its recording, the quick check's and the analyzer's inputs, the household knowledge as
  each turn rendered it, replaying its recorded tool calls, the capability probe, and a test's replay setup (its plan,
  then the sandbox set up at the first turn and Assist's prompt and tools built in it);
- the proposer's window: its sessions (a brief of each), the scenarios, the evidence it loads (the probe on the
  household's conversation), and what the generator and the reviewer get upfront;
- the improvements, built again from their tests.

Replays run on the household's setup as it is today; with no Home Assistant here, the preflight takes it from the
newest conversation, as its first turn recorded it (its home, its registries whole as the recorder keeps them now,
Assist's tools, its assistant), and replays every conversation on that one setup, each from its own moment. That
conversation is kept in went-through.json (below), so each build replays on the same setup until the file is deleted.

No model is called, and nothing is written back to Home Assistant. A step the pipeline itself treats as "can't"
(CaseError: a conversation that can't be replayed, say) is skipped with why; anything else is a failure. So is a
"can't" on a step the conversation went through before, as the stores show it (an analysis or a quick check read it,
a replay without a change ran on it) or an earlier preflight did (preflight/went-through.json beside the copy, only
added to): the pipeline's "can't" is for good there (a conversation it can't read is never read again, one it can't
replay is never tested), so a step that stopped working is a regression, not a skip. Delete that file to accept a
deliberate change. One line per conversation; exit 1 on any failure, so tools/deploy.sh stops.

  .venv/bin/python tools/preflight.py              copy from Home Assistant, then check
  .venv/bin/python tools/preflight.py --offline    check the copy already in the private folder
  .venv/bin/python tools/preflight.py --verbose    also the integration's log (tracebacks)
"""

import argparse
import asyncio
import importlib.util
import inspect
import io
import json
import logging
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import types

ROOT = Path(__file__).resolve().parent.parent
sys.modules.setdefault("turbojpeg", types.SimpleNamespace(TurboJPEG=object))  # As in fixture-test/conftest.py.
sys.path.insert(0, str(ROOT))
sys.path.insert(1, str(ROOT / "fixture-test"))  # Its recordings.kept_home.

_spec = importlib.util.spec_from_file_location("private", ROOT / "tools" / "private.py")
private = importlib.util.module_from_spec(_spec)  # Where the copy goes, and how Home Assistant is reached.
_spec.loader.exec_module(private)

# The stores the stages read (traces and the changeset journal aren't).
STORES = ("index", "flow", "improvements", "replays", "household", "session_*")
CONCURRENT = 4  # Conversations checked at once (each starts a few sandboxes, each in a worker of its own).
STEPS = ("read", "quick check input", "analyzer input", "household knowledge", "replay", "capability probe",
         "replay setup")
OK, SKIPPED, FAILED = "ok", "skipped", "failed"
RAN = ("reproduced", "not_reproducible")  # A replay without a change that got past its setup.


def fetch(folder):
    """The improver's stores, copied from Home Assistant (read-only) into folder/storage, replacing the last copy."""
    names = " ".join(f"assist_improver.{name}" for name in STORES)
    command = f"cd /config/.storage && tar -cf - $(ls -d {names} 2>/dev/null)"
    ha = private.home_assistant("HA_SSH", "HA_SSH_KEY")
    ssh = ["ssh", "-i", ha["HA_SSH_KEY"], "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", ha["HA_SSH"]]
    found = subprocess.run([*ssh, command], capture_output=True, timeout=600, check=False)
    if found.returncode:
        raise SystemExit(f"preflight: couldn't copy the stores: {found.stderr.decode(errors='replace').strip()}")
    folder.mkdir(parents=True, exist_ok=True)
    staging, target = Path(tempfile.mkdtemp(dir=folder)), folder / "storage"
    try:
        with tarfile.open(fileobj=io.BytesIO(found.stdout)) as archive:
            archive.extractall(staging / "storage", filter="data")
        if target.exists():
            shutil.rmtree(target)
        (staging / "storage").rename(target)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return target


def _outcome(err):
    from custom_components.assist_improver.cases import CaseError
    kind = SKIPPED if isinstance(err, CaseError) else FAILED
    return kind, (str(err) if kind == SKIPPED else f"{type(err).__name__}: {err}")[:300]


async def _step(steps, name, work):
    """One step: ok, skipped (CaseError: the pipeline's own "can't") or failed (anything else). Returns its result."""
    try:
        result = work()
        if inspect.isawaitable(result):
            result = await result
    except Exception as err:  # noqa: BLE001 - what the step does is the finding.
        steps[name] = _outcome(err)
        return None
    steps[name] = (OK, "")
    return result


async def _runtime(hass, folder):
    """The improver's stores, read the way the integration reads them, from a scratch copy in hass's storage."""
    from custom_components.assist_improver.flow import Flow
    from custom_components.assist_improver.improvements import Improvements
    from custom_components.assist_improver.recorder import Recorder
    from custom_components.assist_improver.simulate import Replays
    storage = Path(hass.config.path(".storage"))
    storage.mkdir(parents=True, exist_ok=True)
    for path in Path(folder).glob("assist_improver.*"):
        shutil.copy(path, storage / path.name)
    recorder = Recorder(hass)
    recorder.index = ((await recorder.index_store.async_load()) or {}).get("sessions", {})
    replays = Replays(hass)
    await replays.start()
    runtime = types.SimpleNamespace(recorder=recorder, replays=replays)
    runtime.improvements = Improvements(hass, recorder)
    runtime.flow = Flow(hass, types.SimpleNamespace(options={}), runtime)  # Where each conversation is (flow.py).
    await runtime.flow.load()
    return runtime


def recorded_setup(recording):
    """A setup as a conversation's first turn recorded it, in place of the household's today (replay.today's; there's
    no Home Assistant here): its home, its registries whole as the recorder keeps them now (an older recording's made
    whole, recordings.kept_home), the tools Assist had, its device when one of them set timers, and the assistant that
    answered as its provider configuration kept it. None when it keeps no assistant."""
    from recordings import kept_home
    from custom_components.assist_improver.instructions import assistant_config
    turn = (recording.get("turns") or [{}])[0]
    source, prepared = turn["source"], turn["prepared"]
    ctx, tools = prepared["context"], [t["name"] for t in prepared["tools"]]
    config = assistant_config(prepared.get("provider_config"))
    if config is None or "platform" not in ctx:
        return None
    subentry = {"subentry_id": config["subentry_id"], "subentry_type": "conversation", "title": config.get("title"),
                "data": config.get("options") or {}}
    return {"home": kept_home(source), "tools": tools,
            "timers": [ctx["device_id"]] if ctx.get("device_id") and "HassStartTimer" in tools else [],
            "assistant": {"entry": {"domain": ctx["platform"], "entry_id": config["config_entry_id"],
                                    "subentries": [subentry]}},
            "provider": config}


async def _today(recorder, recording_ids, pinned=None):
    """(recording id, setup): the one setup every replay here runs on (recorded_setup), so that each build replays each
    conversation on the same one: the pinned conversation's while it gives one, else the newest conversation's. (None,
    None) when none does; a replay's steps then say why. Read one at a time."""
    async def setup_of(recording_id):
        try:
            return recorded_setup(await recorder.export(recording_id))
        except Exception:  # noqa: BLE001 - its own steps say why.
            return None
    if pinned in recording_ids and (found := await setup_of(pinned)) is not None:
        return pinned, found
    newest, out = "", (None, None)
    for recording_id in recording_ids:
        if (found := await setup_of(recording_id)) is not None and (found["home"].get("at") or "") >= newest:
            newest, out = found["home"].get("at") or "", (recording_id, found)
    return out


def _user(turn):
    return (((turn.get("prepared") or {}).get("context") or {}).get("context") or {}).get("user_id")


def _knowledge(recording):
    """The household knowledge as each turn rendered it for the person speaking."""
    from custom_components.assist_improver.household import as_text, render
    for turn in recording.get("turns") or []:
        knowledge = (turn.get("source") or {}).get("household")
        render(knowledge, _user(turn))
        as_text(knowledge)


async def _replay_setup(hass, recording, today):
    """A test's replay setup, as Simulate starts its replay without the change: its plan, then the sandbox set up as
    today's setup at the first turn, and Assist's prompt and tools built in it."""
    from custom_components.assist_improver.cases import CaseError, recording_turns
    from custom_components.assist_improver.replay import SandboxHome
    from custom_components.assist_improver.simulate import before_plan
    if today is None:
        raise CaseError("No conversation keeps the assistant that answered it, so there's no setup to replay on.")
    before_plan(recording, today)
    turn = recording_turns(recording)[0]
    home = SandboxHome(hass, recording, turn, "improver-preflight", today)
    try:
        if not (await home.assist(turn))["api_prompt"]:
            raise RuntimeError("Assist built no prompt in the sandbox")
    finally:
        await home.stop()


async def _conversation(hass, runtime, recording_id, analysis, today):
    """Every deterministic step each stage takes on one conversation: {step: (ok | skipped | failed, why)}."""
    from custom_components.assist_improver import analysis as analysis_module, evidence, triage
    from custom_components.assist_improver.exploration import SessionAPI, SessionView
    steps = {}
    recording = await _step(steps, "read", lambda: runtime.recorder.export(recording_id))
    view = recording and await _step(steps, "read", lambda: SessionView(recording))
    if view is None:
        return steps

    def analyzer_input():
        instructions, payload, tools, _, _ = analysis_module.analyzer_input(runtime, recording_id, view)
        SessionAPI(hass, view, tools)
        json.dumps(payload, ensure_ascii=False, sort_keys=True)
    await _step(steps, "quick check input",
                lambda: json.dumps(triage.payload(view), ensure_ascii=False, sort_keys=True))
    await _step(steps, "analyzer input", analyzer_input)
    await _step(steps, "household knowledge", lambda: _knowledge(recording))
    await _step(steps, "replay", lambda: evidence.replay_calls(recording, today))
    devices = set(view.devices_touched()) | {g["entity_id"] for g in (analysis or {}).get("goals", [])
                                             if g.get("entity_id")}
    await _step(steps, "capability probe", lambda: evidence.capabilities(recording, sorted(devices), today))
    await _step(steps, "replay setup", lambda: _replay_setup(hass, recording, today))
    return steps


# The steps that replay a conversation in a sandbox: a compacted one can't, by design (retention.py), so their "can't"
# then is no regression.
REPLAYED = {"replay", "capability probe", "replay setup"}


def _went_through(flow, replays, rid):
    """The steps the stores show the pipeline got past on this conversation: reading it (a round's quick check or
    analysis did, as its flow record keeps them; not once a round ended it couldn't be read: nothing an install does
    changes it then), and the replay setup, once a replay without a change ran on it."""
    conv = flow.record(rid)
    read = any((r["records"].get("Analyze") or {}).get("analysis") or (r["records"].get("Triage") or {}).get("verdict")
               for r in conv["rounds"].values())
    out = {"read"} if read and conv["outcome"] != "couldn't be read" else set()
    if any(item["recording_id"] == rid and item["plan"]["kind"] == "before" and item["status"] in RAN
           for item in replays.items.values()):
        out.add("replay setup")
    return out


def went_through(found, earlier=None):
    """{recording id: [steps]}: earlier's, with each step that went through this time added. Never taken out: a step
    that went through once and can't now is a regression however often it's checked."""
    out = {rid: set(steps) for rid, steps in (earlier or {}).items()}
    for rid, steps in found["conversations"].items():
        out.setdefault(rid, set()).update(step for step, (kind, _) in steps.items() if kind == OK)
    return {rid: sorted(steps) for rid, steps in sorted(out.items()) if steps}


async def _window(runtime, today):
    """The proposer's window, as a run loads it before the generator's first call, with every conversation's latest
    analysis (but those marked as a test) in it: (steps, problems)."""
    from custom_components.assist_improver import failures, hypotheses, proposal
    steps, problems = {}, []
    flow = runtime.flow
    with failures.collecting(problems):
        analyses = [found for rid in runtime.recorder.index
                    if (found := flow.analysis(rid)) and flow.place(rid)["outcome"] != "marked as a test"]
        improvements = runtime.improvements
        sessions = await _step(steps, "sessions", lambda: proposal.window(
            runtime, analyses, feedback=improvements.feedback(), existing=improvements.catalog(detailed=True),
            today=today))
        if sessions is not None:
            await _step(steps, "scenarios", sessions.prepare)
            await _step(steps, "evidence", sessions.load_evidence)
            await _step(steps, "generator input", lambda: json.dumps(sessions.overview(), ensure_ascii=False))
            await _step(steps, "reviewer input", lambda: json.dumps(sessions.summary(), ensure_ascii=False))
            await _step(steps, "instructions", lambda: hypotheses.known_lines(sessions))
    return steps, problems


async def check(hass, folder, *, earlier=None, setup=None, concurrent=CONCURRENT):
    """Every deterministic step on every stored conversation in folder (the stores as fetched; they're read from a
    copy in hass's own storage, so folder stays as it was): {"conversations": {recording id: {step: (outcome, why)}},
    "window": {...}, "improvements": {...}, "setup": the conversation whose setup replays ran on (_today; setup: the
    one earlier preflights pinned)}. earlier: {recording id: [steps]} that went through in earlier preflights
    (went_through, kept); a "can't" on one of those, or on one the stores show the pipeline got past, failed."""
    from custom_components.assist_improver import failures
    from custom_components.assist_improver.flow import CLOSED
    runtime = await _runtime(hass, folder)
    out = {"conversations": {}, "window": {}, "improvements": {}, "setup": None}
    problems = []
    with failures.collecting(problems):
        await _step(out["improvements"], "built from their tests", runtime.improvements.start)
    for problem in problems:
        out["improvements"][f"{problem.get('item')}"] = (FAILED, problem["error"])
    index = runtime.recorder.index
    wanted = sorted(rid for rid, summary in index.items()
                    if summary.get("status") in CLOSED and summary.get("user_turns") != 0)
    out["setup"], today = await _today(runtime.recorder, wanted, setup)
    gate, earlier = asyncio.Semaphore(concurrent), earlier or {}

    async def one(rid):
        async with gate:
            steps = await _conversation(hass, runtime, rid, runtime.flow.analysis(rid), today)
        before = set(earlier.get(rid, ())) | _went_through(runtime.flow, runtime.replays, rid)
        if index[rid].get("stored") == "compact":  # Retention's doing, on purpose: it no longer replays.
            before -= REPLAYED
        for step, (kind, why) in steps.items():
            if kind == SKIPPED and step in before:
                steps[step] = (FAILED, f"can't now, though it went through before: {why}")
        out["conversations"][rid] = steps
    await asyncio.gather(*(one(rid) for rid in wanted))
    steps, problems = await _window(runtime, today)
    out["window"] = steps
    for problem in problems:  # One conversation's, with it ("proposer"); the rest, the window's.
        owner = next((rid for rid in wanted if rid.startswith(problem.get("item") or "\0")), None)
        steps, name = (out["conversations"][owner], "proposer") if owner else (out["window"], problem.get("item"))
        why = " | ".join(filter(None, [steps.get(name, ("", ""))[1], problem["error"]]))
        steps[name] = (FAILED, why)
    return out


def _has(steps, outcome):
    return any(kind == outcome for kind, _ in steps.values())


def counts(found):
    """{conversations, ok, skipped (ok, with a step skipped), failed, proposer: ok | failed, improvements: ...}."""
    conversations = found["conversations"].values()
    failed = sum(1 for steps in conversations if _has(steps, FAILED))
    return {"conversations": len(conversations), "ok": len(conversations) - failed,
            "skipped": sum(1 for steps in conversations if _has(steps, SKIPPED) and not _has(steps, FAILED)),
            "failed": failed, "proposer": FAILED if _has(found["window"], FAILED) else OK,
            "improvements": FAILED if _has(found["improvements"], FAILED) else OK}


def failed(found):
    """Whether any step failed anywhere."""
    found = counts(found)
    return bool(found["failed"]) or FAILED in (found["proposer"], found["improvements"])


def report(found, write=print):
    """One line per conversation: ok, or each step that failed (and those skipped, with why); then the counts."""
    def line(name, steps):
        bad = [f"{step}: {why}" for step, (kind, why) in steps.items() if kind == FAILED]
        skipped = [f"{step}: {why}" for step, (kind, why) in steps.items() if kind == SKIPPED]
        write(f"FAILED   {name}  " + " | ".join(bad) if bad
              else f"ok       {name}" + (f"  (skipped {' | '.join(skipped)})" if skipped else ""))
    for rid, steps in sorted(found["conversations"].items()):
        line(rid[:8], steps)
    line("proposer", found["window"])
    line("improvements", found["improvements"])
    write(f"setup    {found['setup'][:8]}'s first turn, kept in went-through.json until it's deleted"
          if found.get("setup") else "setup    none: no conversation keeps the assistant that answered it")
    c = counts(found)
    write(f"{c['conversations']} conversations: {c['ok']} ok ({c['skipped']} with a step skipped), {c['failed']} "
          f"failed; proposer {c['proposer']}; improvements {c['improvements']}")


def kept(path):
    """What earlier preflights kept in path (run): {"conversations": {recording id: [steps that went through]},
    "setup": the conversation whose setup replays run on}."""
    try:
        found = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        found = {}
    return {"conversations": found.get("conversations") or {}, "setup": found.get("setup")}


async def run(folder):
    from homeassistant.core import HomeAssistant
    from custom_components.assist_improver.worker import shutdown
    path = Path(folder).parent / "went-through.json"
    earlier = kept(path)
    with tempfile.TemporaryDirectory() as scratch:
        hass = HomeAssistant(scratch)
        try:
            found = await check(hass, folder, earlier=earlier["conversations"], setup=earlier["setup"])
        finally:
            await shutdown(hass)
            await hass.async_stop(force=True)
    path.write_text(json.dumps({"setup": found.get("setup"),
                                "conversations": went_through(found, earlier["conversations"])}, indent=1))
    report(found)
    return 1 if failed(found) else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--offline", action="store_true", help="check the copy already fetched")
    parser.add_argument("--verbose", action="store_true", help="also the integration's log")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING if args.verbose else logging.CRITICAL, stream=sys.stderr)
    out = private.path() / "preflight"
    folder = out / "storage"
    if not args.offline:
        fetch(out)
    elif not folder.is_dir():
        raise SystemExit(f"preflight: no copy in {folder}; run it without --offline first")
    print("preflight: every deterministic step on every stored conversation, with this checkout's code "
          "(no model, nothing written back)", flush=True)
    sys.exit(asyncio.run(run(folder)))


if __name__ == "__main__":
    main()
