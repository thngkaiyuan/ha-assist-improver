"""The CI bench's own logic (tools/bench), with stand-ins for what it reads: the watcher starts a channel's tests once
for each new set of Home Assistant versions, stable's tests cover every supported release, the journeys' log check
finds the guard's Landlock line and anything of the improver's (an error, or a blocking call on Home Assistant's event
loop), and the badges say what a run tested."""

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def tool(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"tools/bench/{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


watch, logcheck, badges = tool("watch"), tool("logcheck"), tool("badges")


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


def test_stable_means_the_minimum_and_each_months_newest_patch_since_up_to_the_newest_stable():
    tags = ["2026.10.4", "2026.11", "2026.11.0", "2026.11.1", "2026.11.2", "2026.12.0b0", "2026.12.0", "2026.12.1",
            "2026.12.2", "2027.1.0b1", "2027.1.0.dev202612090215", "stable", "beta", "dev", "latest"]
    # The minimum itself too, once its month has patches; 2026.12.2 is tagged, but the newest stable is 2026.12.1.
    assert watch.supported("2026.11.0", "2026.12.1", tags) == ["2026.11.0", "2026.11.2", "2026.12.1"]
    assert watch.supported("2026.11.0", "2026.11.0", ["2026.11.0"]) == ["2026.11.0"]
    assert watch.supported("2026.9.0", "2027.1.1", ["2026.9.4", "2026.10.4", "2026.12.3", "2027.1.0"]) == [
        "2026.9.0", "2026.9.4", "2026.10.4", "2026.12.3", "2027.1.1"]  # By number; the newest stable before its tag.
    assert watch.supported("2026.12.0", "2026.11.2", tags) == []  # A minimum not out yet: nothing, and the job fails.


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


BLOCKING = ("2026-11-06 20:08:17.738 WARNING (MainThread) [homeassistant.util.loop] Detected blocking call to open "
            "with args ('/usr/src/homeassistant/homeassistant/components/fan/services.yaml',) inside the event loop by "
            "custom integration 'assist_improver' at custom_components/assist_improver/targets.py, line 43: found = "
            "load_yaml_dict(path) (offender: /usr/local/lib/python3.14/site-packages/annotatedyaml/loader.py, line "
            "166: with open(fname, encoding=\"utf-8\") as conf_file:), please create a bug report at "
            "https://github.com/thngkaiyuan/ha-assist-improver/issues\n"
            "Traceback (most recent call last):\n"
            '  File "/config/custom_components/assist_improver/targets.py", line 43, in _described\n')
BLOCKING_THEIRS = ("2026-11-06 20:08:18.000 WARNING (MainThread) [homeassistant.util.loop] Detected blocking call to "
                   "open with args ('/config/x',) inside the event loop by custom integration 'other' at "
                   "custom_components/other/__init__.py, line 9: open(path), please create a bug report\n")


def test_a_blocking_call_home_assistant_caught_in_the_improvers_code_is_the_improvers_to_fix():
    """Home Assistant's warning of blocking I/O on its event loop, pointing into custom_components/assist_improver,
    fails a journey (its own log) and the HA OS run, as an error does; another integration's doesn't."""
    assert logcheck.improvers(GUARD + BLOCKING + BLOCKING_THEIRS + THEIRS) == [BLOCKING.strip()]
    assert logcheck.improvers(GUARD + TRACEBACK) == [TRACEBACK.strip()]
    assert logcheck.problems([GUARD + BLOCKING], "Landlock skipped: Function not implemented") == [BLOCKING.strip()]


def test_ci_runs_nothing_in_a_private_repository():
    """GitHub bills a private repository's runs (a public one's are free): there each job of the tests skips, as their
    events tell, and so does each of HACS's checks and the leak guard; the watcher, whose scheduled runs can't tell,
    polls only once the repository's WATCH_HOME_ASSISTANT variable is on, and HACS's scheduled checks run only then
    too."""
    import yaml
    workflows = ROOT / ".github/workflows"
    for name in ("tests.yml", "badges.yml", "validate.yml", "leak-guard.yml"):
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


def needs(**results):
    """A stand-in for toJSON(needs) in tests.yml's verdict job: a push's run where everything passed, but the results
    given."""
    found = {"versions": {"result": "success", "outputs": {"stable": '["2026.11.0","2026.11.2","2026.12.1"]',
                                                           "beta": "2027.1.0b1", "dev": "2027.1.0.dev202612090215"}},
             "gate": {"result": "success", "outputs": {}}, "beta": {"result": "success", "outputs": {}},
             "dev": {"result": "success", "outputs": {}},
             "coverage": {"result": "success",
                          "outputs": {"totals": '{"python": [9999, 12421], "js": [1000, 1579]}'}}}
    for job, result in results.items():
        found[job] = {"result": result, "outputs": {} if result != "success" else found[job]["outputs"]}
    return found


def test_the_badges_say_what_a_run_tested_and_how_it_went():
    def said(files):
        return {name: (file["label"], file["message"], file["color"]) for name, file in files.items()}
    assert said(badges.verdict(needs(), 42)) == {
        "stable.json": ("Home Assistant", "2026.11 – 2026.12 passing", "brightgreen"),
        "beta.json": ("Home Assistant beta", "2027.1.0b1 passing", "brightgreen"),
        "dev.json": ("Home Assistant dev", "2027.1.0.dev202612090215 passing", "brightgreen"),
        "coverage.json": ("coverage", "78%", "yellow")}  # 10999 of 14000 lines: 78.6%, rounded down.
    assert all(file["schemaVersion"] == 1 and file["run"] == 42 for file in badges.verdict(needs(), 42).values())
    assert said(badges.verdict(needs(gate="failure", dev="failure", coverage="failure"), 42)) == {
        "stable.json": ("Home Assistant", "2026.11 – 2026.12 failing", "red"),
        "beta.json": ("Home Assistant beta", "2027.1.0b1 passing", "brightgreen"),
        "dev.json": ("Home Assistant dev", "2027.1.0.dev202612090215 failing", "red")}  # No coverage: no number.
    one = needs()
    one["versions"]["outputs"]["stable"] = '["2026.11.0"]'
    assert said(badges.verdict(one, 42))["stable.json"] == ("Home Assistant", "2026.11 passing", "brightgreen")
    # No versions found: the Stable check fails, and the channels that need them don't run.
    assert said(badges.verdict(needs(versions="failure", gate="failure", beta="skipped", dev="skipped"), 42)) == {
        "stable.json": ("Home Assistant", "failing", "red"), "coverage.json": ("coverage", "78%", "yellow")}
    # The watcher's run for a new beta: only beta's badge changes.
    assert said(badges.verdict(needs(gate="skipped", beta="failure", dev="skipped", coverage="skipped"), 42)) == {
        "beta.json": ("Home Assistant beta", "2027.1.0b1 failing", "red")}


def test_a_badge_never_goes_back_to_an_older_runs():
    ours = badges.endpoint("Home Assistant beta", "2027.1.0b1 passing", "brightgreen", 42)
    assert not badges.kept(None, ours)  # None yet.
    assert not badges.kept({**ours, "run": 41}, ours)
    assert badges.kept({**ours, "run": 43}, ours)  # A newer run's, written before this one finished.
    assert badges.kept(ours, ours)  # Written already.
    assert not badges.kept({**ours, "message": "2027.1.0b1 failing", "color": "red"}, ours)  # The run tried again.
