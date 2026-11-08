"""Replay workers as real processes (DETAILS P11.8.1): each replay's sandbox, and each evidence probe's, runs in a
worker of its own, the real instance's process keeps none of them, what crosses is plain data with Home Assistant's own
errors kept, and a worker ends with its job, its folder with it. The rest of the suite serves workers inline, through
the same JSON."""

import json
import logging
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time

import pytest

from conftest import assist_home
from recordings import (PROFILE, REPLAY_ENTITIES, TARGET, at_home, bind_agent, make_recording, prepared_recording,
                        recorded_today)
from kit import improver  # noqa: F401 - a fixture.
from custom_components.assist_improver import replay
from custom_components.assist_improver import sandbox as sandbox_module
from custom_components.assist_improver import worker as worker_module
from custom_components.assist_improver.cases import CaseError, recording_turns
from custom_components.assist_improver.replay import SandboxHome

POPEN = subprocess.Popen  # Before the effect guards: this module's tests start worker processes for real.
SCRIPTS = {"fixture_press": {"alias": "Fixture press", "sequence": [
    {"action": "switch.turn_off", "target": {"entity_id": TARGET}},
    {"action": "switch.turn_on", "target": {"entity_id": TARGET}}]}}


def today(record):
    """Today's setup: the home as the recording found it, with the household's press script."""
    return recorded_today(record, scripts=SCRIPTS, automations=[])


async def built(hass, record):
    """Assist's prompt and tools as Home Assistant builds them in a replay's sandbox of its own, which ends with it."""
    turn = recording_turns(record)[0]
    home = SandboxHome(hass, record, turn, "fixture-built", today(record))
    try:
        return await home.assist(turn)
    finally:
        await home.stop()


@pytest.fixture
def real_worker(hass, monkeypatch):
    """Workers as real processes; the ones started, in order."""
    started, begin = [], worker_module.Worker.start.__func__

    async def recorded(cls, hass, providers=None, start=None):
        started.append(await begin(cls, hass, providers, start))
        return started[-1]
    monkeypatch.setattr(subprocess, "Popen", POPEN)
    monkeypatch.setattr(worker_module, "INLINE", False)
    monkeypatch.setattr(worker_module.Worker, "start", classmethod(recorded))
    yield started


class ProcessProvider:
    """conftest.ScriptedProvider for a worker in its own process: the provider is a process of its own
    (provider_server.py), its script and what it was asked kept in files."""

    def __init__(self, folder):
        self.folder, self.reply, self.loop, self.asked = folder, None, None, []
        self.blocks, self.models = [], ["fixture-no-model"]

    @property
    def blocks(self):
        return json.loads((self.folder / "script.json").read_text())

    @blocks.setter
    def blocks(self, blocks):
        (self.folder / "script.json").write_text(json.dumps(blocks))

    @property
    def models(self):
        return json.loads((self.folder / "models.json").read_text())

    @models.setter
    def models(self, models):
        (self.folder / "models.json").write_text(json.dumps(list(models)))

    @property
    def requests(self):
        log = self.folder / "requests.jsonl"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


@pytest.fixture
def provider_process(real_worker, monkeypatch, tmp_path):
    """The household's provider for the worker process: a process of its own on 127.0.0.1, which the worker reaches
    as Anthropic's API (ANTHROPIC_BASE_URL); nothing here opens a connection."""
    import conftest
    scripted = ProcessProvider(tmp_path)
    server = POPEN([sys.executable, str(Path(__file__).parent / "provider_server.py"), str(tmp_path)],
                   stdout=subprocess.PIPE, text=True)
    try:
        monkeypatch.setenv("ANTHROPIC_BASE_URL", f"http://127.0.0.1:{server.stdout.readline().strip()}")
        monkeypatch.setattr(conftest, "PROVIDER", scripted)
        yield scripted
    finally:
        server.kill()
        server.wait()


def sandbox_threads():
    return [t for t in threading.enumerate() if t.name == sandbox_module.THREAD_NAME]


def ended(hass, workers):
    """Every worker started has ended, its folder gone, and none is left running."""
    return bool(workers) and not hass.data.get(worker_module.KEY) and all(
        w.process is None and w.folder is None for w in workers)


@pytest.mark.asyncio
async def test_a_replays_sandbox_runs_in_the_worker_process_and_none_in_this_one(hass, real_worker, effect_guards):
    record = make_recording()
    turn = recording_turns(record)[0]
    filters = [list(h.filters) for h in logging.getLogger().handlers]
    home = SandboxHome(hass, record, turn, "fixture-worker", today(record))
    try:
        assist = await home.assist(turn)
        assert home.sandbox.worker.process.returncode is None
        assert not sandbox_threads()  # Nothing of the sandbox in this process...
        assert [list(h.filters) for h in logging.getLogger().handlers] == filters  # ...nor on its log handlers.
        assert "Fixture Relay" in assist["prompt"] or "fixture" in assist["prompt"].lower()
        assert "intent__HassTurnOff" in [t["name"] for t in assist["tools"]]
        result = await home.sandbox.call("tool", tool_name="intent__HassTurnOff", tool_args={"name": "Fixture Relay"})
        assert result["data"]["response_type"] == "action_done"
        world = await home.snapshot()
        assert world["transitions"] == [{"entity_id": TARGET, "old": "on", "new": "off"}]  # The worker's home.
        assert hass.states.get(TARGET) is None  # The real instance's home never moved.
    finally:
        await home.stop()
    assert ended(hass, real_worker) and effect_guards == []


@pytest.mark.asyncio
async def test_a_worker_gets_none_of_the_real_instances_supervisor_access(hass, real_worker, monkeypatch, tmp_path):
    """The Supervisor's address and tokens stay with the real instance; the rest of its environment goes to the worker
    (DETAILS P11.8.3)."""
    seen = tmp_path / "environment.json"
    for name, value in (("SUPERVISOR", "fixture-supervisor"), ("SUPERVISOR_TOKEN", "fixture-token"),
                        ("SUPERVISOR_CORE_API_SOCKET", "fixture-socket"), ("HASSIO_TOKEN", "fixture-token"),
                        ("FIXTURE_KEPT", "kept")):
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(worker_module, "BOOT", "import json, os\n"
                        f"json.dump(sorted(os.environ), open({str(seen)!r}, 'w'))\n" + worker_module.BOOT)
    worker = await worker_module.Worker.start(hass)
    try:
        await worker.call("home.start", time_zone="UTC")
    finally:
        await worker.stop()
    names = json.loads(seen.read_text())
    assert "FIXTURE_KEPT" in names and not [n for n in names if n.startswith(("SUPERVISOR", "HASSIO"))]
    assert ended(hass, real_worker)


@pytest.mark.asyncio
async def test_a_worker_starts_in_its_own_folder_imports_nothing_from_the_real_instances_and_keeps_temporary_files(
        hass, real_worker, monkeypatch, tmp_path):
    """Home Assistant starts in its config folder as python -P, so nothing there shadows an import; a worker starts so
    too, in its own folder: a module in the real instance's folder, named as one the guard imports, isn't imported,
    and its own folder, where a sandbox can write, isn't where it imports from. Where libraries and child processes
    that read TMPDIR or SQLITE_TMPDIR put temporary files is its folder too."""
    home, seen = tmp_path / "config", tmp_path / "seen.json"
    home.mkdir()
    (home / "ifaddr.py").write_text("raise RuntimeError('a module in the real instance\\'s folder')\n")
    monkeypatch.chdir(home)
    temporary = ("TMPDIR", "TEMP", "TMP", "SQLITE_TMPDIR")
    monkeypatch.setattr(worker_module, "BOOT", "import json, os, sys\n"
                        f"json.dump([[os.path.realpath(p or os.curdir) for p in sys.path], "
                        f"os.path.realpath(sys.argv[1]), os.getcwd(), *(os.path.realpath(os.environ[n]) "
                        f"for n in {temporary!r})], open({str(seen)!r}, 'w'))\n" + worker_module.BOOT)
    worker = await worker_module.Worker.start(hass)
    try:
        await worker.call("home.start", time_zone="UTC")
    finally:
        await worker.stop()
    imports_from, folder, *where = json.loads(seen.read_text())
    assert where == [folder] * (1 + len(temporary))  # Its working folder, then each temporary files' folder.
    assert folder not in imports_from  # python -P: neither its folder nor "" (the working folder) is on sys.path.
    assert ended(hass, real_worker)


@pytest.mark.asyncio
async def test_home_assistants_tool_errors_cross_with_their_name_and_text(hass, real_worker):
    """The model is told a failed tool call's error by its class name and text, as Home Assistant reports it."""
    from homeassistant.exceptions import HomeAssistantError
    record = make_recording()
    turn = recording_turns(record)[0]
    home = SandboxHome(hass, record, turn, "fixture-worker-error", today(record))
    try:
        await home.assist(turn)
        with pytest.raises(HomeAssistantError) as raised:
            await home.sandbox.call("tool", tool_name="intent__HassTurnOff",
                                    tool_args={"name": "Nothing by that name"})
        assert type(raised.value).__name__ == "MatchFailedError" and "Nothing by that name" in str(raised.value)
    finally:
        await home.stop()


def test_a_case_the_replay_cant_run_crosses_as_one():
    """A CaseError in the worker is a CaseError in the real instance, with its text: the case can't be replayed."""
    crossed = worker_module.raised(worker_module._error(CaseError("x")))
    assert type(crossed) is CaseError and str(crossed) == "x"


@pytest.mark.asyncio
async def test_the_worker_process_answers_as_a_sandbox_in_this_process_does(hass, monkeypatch):
    """The same replay setup, served inline (as the rest of the suite is) and by a real worker process."""
    inline = await built(hass, make_recording())
    monkeypatch.setattr(subprocess, "Popen", POPEN)
    monkeypatch.setattr(worker_module, "INLINE", False)
    assert await built(hass, make_recording()) == inline
    assert not hass.data[worker_module.KEY] and not sandbox_threads()


@pytest.mark.asyncio
async def test_a_turns_answer_from_the_worker_process_carries_what_a_replay_keeps_of_it_the_turns_own(
        hass, provider_process, monkeypatch):
    """Each turn's answer crosses from the worker with all a replay keeps of it (simulate.Trial._turn): the words the
    turn added and its reply, the provider's usage, the watched devices' attributes, the turn's own transitions,
    changes, commands, calls beyond the home and what wasn't simulated, its call log, and the home after it."""
    await assist_home(hass, REPLAY_ENTITIES)
    record = prepared_recording()
    agent, client = await bind_agent(hass, record, PROFILE)
    at_home(monkeypatch, record, {"notify": {"mobile_app_phone": "none"}}, automations=[  # Turn 1 sets it off.
        {"id": "tell", "alias": "Tell the phone", "triggers": [{"trigger": "state", "entity_id": TARGET, "to": "off"}],
         "actions": [{"action": "notify.mobile_app_phone", "data": {"message": "Relay off"}}]}])
    client.blocks = [[{"type": "tool_use", "id": "off", "name": "intent__HassTurnOff",
                       "input": {"name": "Fixture relay", "domain": ["switch"]}}],
                     [{"type": "text", "text": "Done."}], [{"type": "text", "text": "Ok."}]]
    one, two = recording_turns(record)[:2]
    home = SandboxHome(hass, record, one, "fixture-turns", await replay.today(hass, agent.entity_id), watch=[TARGET])
    try:
        agent_id = await home.load_assistant()
        first = await home.turn(one, agent_id, home.id)
        second = await home.turn(two, agent_id, home.id)
    finally:
        await home.stop()
    kept = {"content", "reply", "usage", "attributes", "transitions", "changes", "commands", "outside", "actions",
            "unemulated", "home"}
    assert kept <= set(first) and kept <= set(second)
    assert [m["role"] for m in first["content"]] == ["assistant", "tool_result", "assistant"]
    assert first["content"][-1]["content"] == first["reply"]["speech"]["plain"]["speech"] == "Done."
    assert first["usage"] and second["usage"]
    assert [t["attributes"][TARGET]["friendly_name"] for t in (first, second)] == ["Fixture relay"] * 2
    assert first["transitions"] == [{"entity_id": TARGET, "old": "on", "new": "off"}] == first["changes"][:1]
    assert first["commands"] == [{"entity_id": TARGET, "service": "switch.turn_off", "data": {}}]
    assert first["outside"] == [{"action": "notify.mobile_app_phone", "data": {"message": "Relay off"},
                                 "answer": "not simulated"}] and first["unemulated"] == ["notify.mobile_app_phone"]
    assert [a["action"] for a in first["actions"]] == [
        "homeassistant.turn_off", "switch.turn_off", "notify.mobile_app_phone"]
    assert first["home"][TARGET]["state"] == second["home"][TARGET]["state"] == "off"
    # Turn 2's lists are its own: nothing of turn 1's.
    assert second["content"][-1]["content"] == second["reply"]["speech"]["plain"]["speech"] == "Ok."
    assert second["transitions"] == second["changes"] == second["commands"] == second["outside"] == []
    assert second["actions"] == second["unemulated"] == []


@pytest.mark.asyncio
async def test_a_worker_that_dies_fails_its_calls_and_leaves_nothing_behind(hass, real_worker):
    import os
    record = make_recording()
    turn = recording_turns(record)[0]
    home = SandboxHome(hass, record, turn, "fixture-worker-dies", today(record))
    await home.start()
    worker = home.sandbox.worker
    process, folder = worker.process, worker.folder
    process.kill()
    await process.wait()
    with pytest.raises(worker_module.WorkerError):
        await home.snapshot()
    await home.stop()  # Its folder goes: the worker's own clean-up never ran.
    assert ended(hass, real_worker) and not os.path.exists(folder)
    await built(hass, record)  # The next replay, in a worker of its own.
    assert len(real_worker) == 2 and ended(hass, real_worker)


@pytest.mark.asyncio
async def test_each_replay_gets_a_fresh_worker_that_ends_with_it_and_unloading_kills_those_running(hass, real_worker):
    """A worker serves one job, and goes with it: its process ends and its folder, which held the sandbox's own
    config folder, goes. Unloading the improver kills the workers still running."""
    import os
    await built(hass, make_recording())
    await built(hass, make_recording())
    first, second = real_worker
    assert first is not second and ended(hass, real_worker)
    record = make_recording()
    home = SandboxHome(hass, record, recording_turns(record)[0], "fixture-worker-unload", today(record))
    await home.start()
    process, folder = home.sandbox.worker.process, home.sandbox.worker.folder
    assert any(p.name.startswith("assist-improver-sandbox-") for p in Path(folder).iterdir())  # Its config folder.
    await worker_module.shutdown(hass)  # As async_unload_entry does.
    assert process.returncode is not None and not os.path.exists(folder) and ended(hass, real_worker)
    with pytest.raises(worker_module.WorkerError):
        await home.snapshot()
    await home.stop()


def test_only_worker_folders_left_empty_a_while_ago_are_removed_at_setup(tmp_path, monkeypatch):
    """Another Home Assistant's workers may share the temporary folder: a folder that holds anything, or was just made,
    stays."""
    import os
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    left, used, new = (tmp_path / f"{worker_module.PREFIX}{name}" for name in ("left", "used", "new"))
    for folder in (left, used, new):
        folder.mkdir()
    (used / "custom_components").mkdir()
    old = time.time() - worker_module.LEFTOVER_SECONDS - 1
    for folder in (left, used):
        os.utime(folder, (old, old))
    worker_module.remove_leftovers()
    assert not left.exists() and used.exists() and new.exists()


@pytest.mark.asyncio
async def test_the_improvers_setup_removes_the_worker_folders_left_behind(hass, tmp_path, monkeypatch):
    """Home Assistant sets the improver up as it starts: a worker's folder left empty a while ago goes then."""
    import os
    from homeassistant import config_entries
    import custom_components.assist_improver as integration
    (tmp_path / "tmp").mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmp"))
    left = tmp_path / "tmp" / f"{worker_module.PREFIX}left"
    left.mkdir()
    old = time.time() - worker_module.LEFTOVER_SECONDS - 1
    os.utime(left, (old, old))
    hass.config_entries = config_entries.ConfigEntries(hass, {})
    await hass.config_entries.async_initialize()
    assert await integration.async_setup(hass, {})
    assert not left.exists()


@pytest.mark.asyncio
async def test_a_worker_has_a_session_of_its_own_and_ends_when_home_assistant_does(hass, real_worker):
    """Its process group is its own, not Home Assistant's, so a signal to that group reaches no further; and it ends
    with Home Assistant, however that stops (a restart too): Home Assistant's end of its input closes, and it exits,
    its folder removed (DETAILS P11.8.1): by the worker, or, where Landlock keeps it from removing the folder itself
    (Linux), emptied, then removed as the improver is next set up."""
    import asyncio
    import os
    worker = await worker_module.Worker.start(hass)
    process, folder = worker.process, worker.folder
    try:
        await worker.call("home.start", time_zone="UTC")
        assert os.getsid(process.pid) == os.getpgid(process.pid) == process.pid != os.getpgid(0)
        process.stdin.close()  # As when Home Assistant's process ends.
        assert await asyncio.wait_for(process.wait(), 30) == 0
        assert not os.path.exists(folder) or os.listdir(folder) == []
        if os.path.exists(folder):  # A while later, Home Assistant starts again.
            old = time.time() - worker_module.LEFTOVER_SECONDS - 1
            os.utime(folder, (old, old))
        worker_module.remove_leftovers()
        assert not os.path.exists(folder)
    finally:
        await worker.stop()
    assert ended(hass, real_worker)


@pytest.mark.asyncio
async def test_a_long_answer_is_read_off_home_assistants_loop(hass, real_worker, monkeypatch):
    """The worker's answers can run to MBs (a sandbox's states): a long one is decoded in Home Assistant's executor,
    with the same result."""
    on_loop = await built(hass, make_recording())
    jobs, add = [], hass.async_add_executor_job
    monkeypatch.setattr(hass, "async_add_executor_job", lambda target, *args: jobs.append(target) or add(target, *args))
    monkeypatch.setattr(worker_module, "LARGE", 0)  # Every answer counts as long.
    assert await built(hass, make_recording()) == on_loop and json.loads in jobs


@pytest.mark.asyncio
async def test_a_full_run_replays_through_the_worker_process(real_worker, provider_process, improver, monkeypatch):
    """Both replays of a test and the proposer's evidence: every sandbox of a run in the worker, the model called from
    there (its provider a process of its own)."""
    from recordings import before
    from kit import candidate_turn
    ops, send = [], worker_module.Worker._send

    async def sent(worker, message):
        ops.append(message["op"] + (":" + message["args"]["job"] if message["op"] == "evidence" else ""))
        return await send(worker, message)
    monkeypatch.setattr(worker_module.Worker, "_send", sent)
    await improver.arrives(improver.friction)
    improver.client.blocks = before(improver.friction) + candidate_turn()
    run = await improver.run()
    assert run["status"] == "done", run["error"]
    [item] = improver.runtime.improvements.items.values()
    assert item["test"]["validated"] and item["test"]["gains"] == ["goals met", "turns per root goal"]
    assert ended(improver.hass, real_worker) and not sandbox_threads()
    assert {"home.start", "home.seed", "home.assistant", "home.turn"} <= set(ops), ops


async def replayed_as_it_went(improver):
    """A full run on the friction conversation: the replay without the change goes as the conversation went, turn 4
    too, so the idea is tested and its improvement waits for the household."""
    from recordings import before
    from kit import candidate_turn
    await improver.arrives(improver.friction)
    improver.client.blocks = before(improver.friction) + candidate_turn()
    run = await improver.run()
    assert run["status"] == "done", run["error"]
    [replayed] = [r for r in improver.runtime.replays.items.values() if r["plan"]["kind"] == "before"]
    assert replayed["status"] == "reproduced"
    [step] = [s for s in replayed["steps"] if s["turn"] == 4]
    assert [(t["old"], t["new"]) for t in step["transitions"]] == [("on", "off"), ("off", "on")]
    assert len(improver.runtime.improvements.pending()) == 1


@pytest.mark.asyncio
async def test_every_evidence_probe_answers_the_same_from_the_worker_process(hass, monkeypatch):
    """Recorded calls replayed, capabilities, scripts run at a goal, aliases matched."""
    from recordings import SCRIPT
    from test_evidence import TODAY, PRESS, hypothesis, sessions
    from custom_components.assist_improver.evidence import Evidence, capabilities, replay_calls

    async def probes():
        found, record = sessions()
        evidence = Evidence(found, TODAY)
        alias = hypothesis({"kind": "registry", "entity_id": TARGET, "aliases": ["Garage door"]})
        return {"calls": await replay_calls(record, TODAY),
                "capabilities": await capabilities(record, [TARGET, SCRIPT], TODAY),
                "scripts": await evidence.implementation(hypothesis(PRESS), evidence.setup(hypothesis(PRESS))),
                "aliases": await evidence.implementation(alias, evidence.setup(alias))}
    inline = await probes()
    monkeypatch.setattr(subprocess, "Popen", POPEN)
    monkeypatch.setattr(worker_module, "INLINE", False)
    real = await probes()
    assert real == inline and real["calls"]
    assert real["scripts"][0]["reaches_goal"] and real["aliases"][0]["matches"] == {"Garage door": [TARGET]}


@pytest.mark.asyncio
async def test_evidence_jobs_run_at_most_concurrent_workers_at_once_and_all_finish(hass, monkeypatch):
    """However many evidence jobs are asked for at once, of every kind, the instance runs at most evidence.CONCURRENT
    of their workers at once, and every job finishes."""
    import asyncio
    from custom_components.assist_improver.evidence import CONCURRENT, JOBS
    most = 0

    async def served(server, job, n):
        nonlocal most
        most = max(most, len(hass.data[worker_module.KEY]))  # The workers alive now, this one too.
        await asyncio.sleep(0.01)
        return n
    monkeypatch.setattr(worker_module.Server, "op_evidence", served)
    asked = [(name, n) for n in range(3) for name in JOBS]
    found = await asyncio.gather(*(worker_module.job(name, n=n) for name, n in asked))
    assert found == [n for _, n in asked] and most == CONCURRENT and not hass.data[worker_module.KEY]


@pytest.mark.asyncio
@pytest.mark.parametrize("inline", [True, False], ids=["inline", "process"])
async def test_a_developer_replay_reproduces_a_recording_with_its_own_assistant(improver, monkeypatch, inline,
                                                                                request):
    """replay_recording: the recording's turns, the assistant it used, judged by what that assistant did."""
    from recordings import before
    from custom_components.assist_improver.simulate import replay_recording
    if not inline:  # The worker in its own process, the provider in one of its own.
        improver.client = request.getfixturevalue("provider_process")
    await improver.arrives(improver.friction)
    improver.client.blocks = before(improver.friction)
    found = await replay_recording(improver.hass, improver.recorder, "friction01", last_turn=3)
    assert found["status"] == "reproduced" and [s["matched"] for s in found["steps"]] == [True] * 3
    assert found["model"] and "plan" not in found and found["said"] == [1, 2, 3]
    assert not improver.runtime.replays.items  # Nothing stored.


@pytest.mark.asyncio
async def test_the_worker_process_renders_the_households_time_zone_and_this_one_keeps_its_own(hass, real_worker):
    """Templates' now() reads the process's default time zone: in the worker it's the home's, as on the real
    instance, so an assistant's instructions render there as Home Assistant renders them for the household."""
    from homeassistant.util import dt as dt_util
    from test_assistant_instructions import elsewhere, sessions_over
    from custom_components.assist_improver import instructions
    from custom_components.assist_improver.evidence import Evidence
    before, zone = dt_util.get_default_time_zone(), elsewhere()
    line = instructions.FACTS["time_zone"].line
    record = make_recording()
    for turn in record["turns"]:
        turn["source"]["time_zone"] = zone
    s = sessions_over(record)
    [found] = await Evidence(s, today(record)).render(s.views["syntheti"], [line])
    assert found == {"rendered": f"The household's time zone is {zone}."}
    turn = recording_turns(record)[0]
    home = SandboxHome(hass, record, turn, "fixture-worker-zone", today(record), change={"prompt": line})
    try:
        assist = await home.assist(turn)
    finally:
        await home.stop()
    assert f"The household's time zone is {zone}." in assist["prompt"]  # A replay reads it too.
    assert dt_util.get_default_time_zone() == before and str(before) != zone


def test_a_replays_sandbox_sees_only_home_assistants_own_integrations_and_the_improvers_code(tmp_path, monkeypatch):
    """The worker's sandboxes run Home Assistant's own code and the improver's: a custom integration of the household's
    (one a recorded tool could name) isn't there to set up, while the improver is, its modules and as an
    integration, with the sandbox's own two (standin.write): the stand-in devices and the web server that serves
    nothing, in place of Home Assistant's."""
    import json
    import sys
    monkeypatch.setattr(subprocess, "Popen", POPEN)
    vendor = tmp_path / "custom_components" / "vendor"
    vendor.mkdir(parents=True)
    (vendor / "manifest.json").write_text(json.dumps({"domain": "vendor", "name": "Vendor", "version": "1.0"}))
    (vendor / "__init__.py").write_text("")
    probe = worker_module.BOOT.replace(
        "from custom_components.assist_improver.worker import main\nmain(folder)",
        "import asyncio\nfrom homeassistant import loader\nfrom homeassistant.core import HomeAssistant\n"
        "import custom_components.assist_improver.standin\n"
        "async def found():\n    hass = HomeAssistant(sys.argv[3])\n    loader.async_setup(hass)\n"
        "    return sorted(loader._get_custom_components(hass))\n"
        "print(asyncio.run(found()))")
    own = worker_module._folder({})  # The worker's own folder, as the real instance makes it (Worker.start).
    try:
        seen = subprocess.run([sys.executable, "-c", probe, own, "", str(tmp_path)],
                              capture_output=True, text=True, timeout=120)
    finally:
        shutil.rmtree(own)
    assert seen.returncode == 0, seen.stderr[-2000:]
    assert seen.stdout.strip().splitlines()[-1] == "['assist_improver', 'assist_improver_standin', 'http']"


@pytest.mark.asyncio
async def test_household_knowledge_reaches_a_replay_in_the_worker_process(hass, real_worker):
    """The improver is an integration in the worker's sandboxes too: its llm platform adds the household's lines to
    the prompt a replay's Assist reads, as at home."""
    record = make_recording()
    for raw in record["turns"]:
        raw["source"].update({"household": {"everyone": ["Press relays off then on."], "people": {}},
                              "household_on": True})
    turn = recording_turns(record)[0]
    home = SandboxHome(hass, record, turn, "household-in-process", today(record))
    try:
        assist = await home.assist(turn)
        assert home.sandbox.worker.process is not None and "Press relays off then on." in assist["prompt"]
    finally:
        await home.stop()


CUSTOM_PROVIDER = {  # A household's assistant from a custom integration: it answers every request the same.
    "manifest.json": json.dumps({"domain": "fixture_custom_provider", "name": "Fixture custom provider",
                                 "version": "1.0.0", "dependencies": ["conversation"], "codeowners": [],
                                 "requirements": [], "iot_class": "cloud_polling",
                                 "documentation": "https://example.invalid", "config_flow": True}),
    "config_flow.py": """from homeassistant import config_entries


class Flow(config_entries.ConfigFlow, domain="fixture_custom_provider"):
    VERSION = 1
""",
    "__init__.py": """from homeassistant.const import Platform


async def async_setup_entry(hass, entry):
    await hass.config_entries.async_forward_entry_setups(entry, [Platform.CONVERSATION])
    return True


async def async_unload_entry(hass, entry):
    return await hass.config_entries.async_unload_platforms(entry, [Platform.CONVERSATION])
""",
    "conversation.py": """from homeassistant.components import conversation
from homeassistant.helpers import intent


class Agent(conversation.ConversationEntity):
    supported_languages = "*"

    def __init__(self, subentry):
        self._attr_unique_id, self._attr_name = subentry.subentry_id, subentry.title

    async def _async_handle_message(self, user_input, chat_log):
        chat_log.async_add_assistant_content_without_tools(conversation.AssistantContent(
            agent_id=user_input.agent_id, content="The custom provider answered."))
        response = intent.IntentResponse(language=user_input.language)
        response.async_set_speech("The custom provider answered.")
        return conversation.ConversationResult(response=response, conversation_id=chat_log.conversation_id)


async def async_setup_entry(hass, entry, async_add_entities):
    for subentry in entry.subentries.values():
        async_add_entities([Agent(subentry)], config_subentry_id=subentry.subentry_id)
"""}


@pytest.mark.asyncio
async def test_an_assistant_from_a_custom_integration_answers_in_the_worker_by_its_own_integration(
        hass, real_worker, monkeypatch, tmp_path):
    """Any provider replays through the household's own integration (DETAILS P6.2.3): a custom one, as the household
    has it installed, loads in the worker's sandbox as the assistant's own integration, and answers there."""
    from types import MappingProxyType
    from homeassistant import config_entries
    from homeassistant.helpers import entity_registry as er
    from conftest import assist_home
    from recordings import prepared_recording
    from custom_components.assist_improver import replay
    folder = tmp_path / "fixture_custom_provider"
    folder.mkdir()
    for name, text in CUSTOM_PROVIDER.items():
        (folder / name).write_text(text)
    monkeypatch.setattr(replay, "custom_providers", lambda hass: {"fixture_custom_provider": str(folder)})
    await assist_home(hass, {})
    record = prepared_recording()
    entry = config_entries.ConfigEntry(
        domain="fixture_custom_provider", title="Custom", data={}, source="user", version=1, minor_version=1,
        options={}, unique_id=None, discovery_keys=MappingProxyType({}), entry_id="fixture-entry",
        subentries_data=[{"data": {"prompt": "Be custom."}, "subentry_id": "fixture-anthropic",
                          "subentry_type": "conversation", "title": "Custom assistant", "unique_id": None}])
    hass.config_entries._entries[entry.entry_id] = entry
    er.async_get(hass).async_get_or_create("conversation", "fixture_custom_provider", "fixture-anthropic",
                                           suggested_object_id="fixture", config_entry=entry,
                                           config_subentry_id="fixture-anthropic")
    turn = recording_turns(record)[0]
    home = SandboxHome(hass, record, turn, "improver-replay-custom", await replay.today(hass, "conversation.fixture"))
    try:
        agent = await home.load_assistant()
        found = await home.turn(turn, agent, "improver-replay-custom")
    finally:
        await home.stop()
    assert agent == "conversation.fixture" and found["content"][-1]["content"] == "The custom provider answered."


def test_a_workers_folder_holds_the_improver_and_the_households_providers_and_nothing_else(tmp_path):
    """Each custom provider is linked where the household has it: one gone from home isn't (it can't load: can't
    replay, never a failed start), and neither the improver's own place nor the sandbox's own integrations' are ever
    taken (DETAILS P11.8)."""
    import os
    (tmp_path / "provider").mkdir()
    folder = worker_module._folder({"fixture_provider": str(tmp_path / "provider"), "gone_provider": str(
        tmp_path / "gone"), "assist_improver": str(tmp_path / "provider"), "http": str(tmp_path / "provider"),
        "assist_improver_standin": str(tmp_path / "provider")})
    try:
        own = Path(folder, "custom_components")
        assert sorted(p.name for p in own.iterdir()) == ["assist_improver", "assist_improver_standin",
                                                          "fixture_provider", "http"]
        assert not (own / "http").is_symlink() and not (own / "assist_improver_standin").is_symlink()
        assert os.readlink(own / "fixture_provider") == str(tmp_path / "provider")
        assert Path(os.readlink(own / "assist_improver")) == worker_module.IMPROVER
    finally:
        shutil.rmtree(folder)


@pytest.mark.asyncio
async def test_only_the_households_assistants_custom_providers_set_up_at_home_are_a_sandboxs_to_load(hass, monkeypatch):
    """replay.custom_providers: the providers of conversation agents, custom integrations set up at home; never a
    built-in one, a custom integration that isn't an assistant's, or a provider not set up."""
    from pathlib import Path
    from types import MappingProxyType, SimpleNamespace
    from homeassistant import config_entries, loader
    from homeassistant.helpers import entity_registry as er
    from conftest import assist_home
    from custom_components.assist_improver import replay
    await assist_home(hass, {})
    registry = er.async_get(hass)
    for domain, kind in (("custom_provider", "conversation"), ("anthropic", "conversation"),
                         ("custom_lights", "light"), ("custom_disabled", "conversation")):
        entry = config_entries.ConfigEntry(domain=domain, title=domain, data={}, source="user", version=1,
                                           minor_version=1, options={}, unique_id=None,
                                           discovery_keys=MappingProxyType({}), entry_id=f"{domain}-entry",
                                           subentries_data=None)
        hass.config_entries._entries[entry.entry_id] = entry
        registry.async_get_or_create(kind, domain, f"{domain}-agent", config_entry=entry)
        if domain != "custom_disabled":
            hass.config.components.add(domain)
    monkeypatch.setattr(loader, "async_get_loaded_integration", lambda hass, domain: SimpleNamespace(
        is_built_in=domain == "anthropic", file_path=Path(f"/config/custom_components/{domain}")))
    assert replay.custom_providers(hass) == {"custom_provider": "/config/custom_components/custom_provider"}
