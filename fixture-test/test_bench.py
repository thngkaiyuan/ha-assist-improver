"""The CI bench's own logic (tools/bench), with stand-ins for what it reads: the watcher starts a channel's tests once
for each new set of Home Assistant versions, and the HA OS job's log check finds the guard's Landlock line and any
error of the improver's."""

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def tool(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"tools/bench/{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


watch, logcheck = tool("watch"), tool("logcheck")


def answer(core, supervisor, os):  # A stand-in for version.home-assistant.io's answer, in the parts read.
    return {"homeassistant": {"default": core, "qemux86-64": core}, "supervisor": supervisor, "hassos": {"ova": os}}


def test_a_channel_runs_once_for_each_new_set_of_versions():
    found = {"stable": answer("2026.11.0", "2026.10.5", "18.4"),
             "dev": answer("2026.12.0.dev202611060216", "2026.11.0.dev0604", "19.0.dev20261102")}
    assert watch.due(found, set()) == {
        "stable": "Home Assistant stable: 2026.11.0, Supervisor 2026.10.5, HA OS 18.4",
        "dev": "Home Assistant dev: 2026.12.0.dev202611060216, Supervisor 2026.11.0.dev0604, HA OS 19.0.dev20261102"}
    ran = set(watch.due(found, set()).values())
    assert watch.due(found, ran) == {}  # Run already, whatever came of it: not again.
    found["dev"] = answer("2026.12.0.dev202611070215", "2026.11.0.dev0604", "19.0.dev20261102")  # Tonight's build.
    assert list(watch.due(found, ran)) == ["dev"]
    found["stable"] = answer("2026.11.0", "2026.11.0", "18.4")  # A new Supervisor alone is new too.
    assert list(watch.due(found, ran)) == ["stable", "dev"]


GUARD = ("2026-11-06 08:20:39.759 DEBUG (MainThread) [custom_components.assist_improver.worker] Replay worker: "
         "Replay guard: Landlock skipped: Function not implemented\n")
TRACEBACK = ("2026-11-06 08:21:00.001 ERROR (MainThread) [homeassistant.core] Error doing job: Task exception\n"
             "Traceback (most recent call last):\n"
             '  File "/config/custom_components/assist_improver/worker.py", line 1, in boot\n'
             "OSError: boom\n")
THEIRS = ("2026-11-06 08:21:01.000 ERROR (MainThread) [homeassistant.components.websocket_api.http.connection] "
          "[123] Unknown recording ID\n")


def test_the_logs_show_the_guards_landlock_and_no_error_of_the_improvers():
    expected = "Landlock skipped: Function not implemented"
    assert logcheck.problems([GUARD + THEIRS, GUARD], expected) == []  # An error that isn't the improver's: fine.
    assert logcheck.problems([THEIRS], expected) == ["No replay's guard said what Landlock did"]
    assert logcheck.problems([GUARD.replace("skipped: Function not implemented", "applied (ABI 7)")], expected) == [
        GUARD.replace("skipped: Function not implemented", "applied (ABI 7)").strip()]
    assert logcheck.problems([GUARD + TRACEBACK + "\n" + THEIRS], expected) == [TRACEBACK.strip()]  # Its traceback too.


def test_ci_runs_nothing_in_a_private_repository():
    """GitHub bills a private repository's runs (a public one's are free): there each job of the tests skips, as their
    events tell, and so does each of HACS's checks and the leak guard; the watcher, whose scheduled runs can't tell,
    polls only once the repository's WATCH_HOME_ASSISTANT variable is on, and HACS's scheduled checks run only then
    too."""
    import yaml
    workflows = ROOT / ".github/workflows"
    for name in ("tests.yml", "validate.yml", "leak-guard.yml"):
        jobs = yaml.safe_load((workflows / name).read_text())["jobs"]
        assert all("!github.event.repository.private" in job["if"] for job in jobs.values()), name
    validate = yaml.safe_load((workflows / "validate.yml").read_text())["jobs"]
    assert all("vars.WATCH_HOME_ASSISTANT == 'on'" in job["if"] for job in validate.values())
    watcher = yaml.safe_load((workflows / "home-assistant.yml").read_text())["jobs"]
    assert all(job["if"] == "vars.WATCH_HOME_ASSISTANT == 'on'" for job in watcher.values())


def test_a_pull_request_from_a_fork_runs_with_no_secret_but_the_leak_guards_and_a_token_that_only_reads():
    """A fork's pull request gets no secrets and a read-only token: what it runs reads none, but the leak guard, which
    skips without its list (test_leak_guard.py), and asks for no more."""
    import yaml
    workflows = ROOT / ".github/workflows"
    runs = {"bench.yml"}  # The tests call it.
    for path in sorted(workflows.glob("*.yml")):
        workflow = yaml.safe_load(path.read_text())
        if "pull_request" in workflow[True] or path.name in runs:  # YAML reads the key "on" as True.
            assert workflow["permissions"] == {"contents": "read"}, path.name
            assert all("permissions" not in job for job in workflow["jobs"].values()), path.name
            assert ("secrets." in path.read_text()) == (path.name == "leak-guard.yml"), path.name


def test_each_workflows_job_ids_differ_without_regard_to_case():
    """GitHub rejects a workflow whose job IDs differ only in case ("stable" and "Stable"), which then never runs; the
    YAML reader used here tells them apart."""
    import yaml
    for path in sorted((ROOT / ".github/workflows").glob("*.yml")):
        jobs = list(yaml.safe_load(path.read_text())["jobs"])
        assert len({job.lower() for job in jobs}) == len(jobs), (path.name, jobs)
