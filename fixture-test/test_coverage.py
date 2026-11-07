"""tools/check.sh --coverage. A replay worker (tools/coverage/site/sitecustomize.py), killed once its job is done, its
folder deleted, writing nowhere else, still leaves the improver's lines it ran, those after its guard was installed too,
and its guard refuses nothing. The report (tools/coverage/report.py) maps every copy's paths to the checkout's, takes
what each process ran, a worker's cut-off last snapshot aside, and counts every file. Skipped where coverage.py isn't
installed, as in Home Assistant's image."""

import importlib.util
import inspect
import json
import os
from pathlib import Path

import pytest

coverage = pytest.importorskip("coverage")

from recordings import make_recording  # noqa: E402
from test_worker import real_worker, today  # noqa: E402, F401 - a fixture.
from custom_components.assist_improver import guard  # noqa: E402
from custom_components.assist_improver import worker as worker_module  # noqa: E402
from custom_components.assist_improver.cases import recording_turns  # noqa: E402
from custom_components.assist_improver.replay import SandboxHome  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "tools/coverage/site"


def lines_of(function):
    source, first = inspect.getsourcelines(function)
    return set(range(first, first + len(source)))


@pytest.mark.asyncio
async def test_a_replay_worker_leaves_the_lines_it_ran_and_its_guard_refuses_nothing(
        hass, real_worker, monkeypatch, tmp_path):
    monkeypatch.setenv("IMPROVER_COVERAGE", str(tmp_path))
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(filter(None, (str(SITE), os.environ.get("PYTHONPATH")))))
    record = make_recording()
    turn = recording_turns(record)[0]
    home = SandboxHome(hass, record, turn, "fixture-coverage", today(record))
    try:
        await home.assist(turn)
        pid = home.sandbox.worker.process.pid
        await home.sandbox.call("tool", tool_name="intent__HassTurnOff", tool_args={"name": "Fixture Relay"})
    finally:
        await home.stop()  # Killed, its folder deleted.
    ran = {}
    for line in (tmp_path / "python" / f"worker-{pid}.jsonl").read_text().splitlines():
        for path, found in json.loads(line).items():
            ran.setdefault(os.path.realpath(path), set()).update(found)
    assert ran[os.path.realpath(guard.__file__)] & lines_of(guard.install)  # Before the guard...
    assert ran[os.path.realpath(worker_module.__file__)] & lines_of(worker_module.Server.op_home_tool)  # ...and after.
    assert not [line for line in real_worker[-1].stderr if "may not" in line], real_worker[-1].stderr


def test_the_report_maps_every_copy_to_the_checkout_and_counts_every_file(tmp_path, monkeypatch, capsys):
    spec = importlib.util.spec_from_file_location("report", ROOT / "tools/coverage/report.py")
    report = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(report)
    monkeypatch.chdir(tmp_path)  # The report works from the checkout; this test's working folder comes back after.
    package = ROOT / "custom_components/assist_improver"
    unrun = coverage.Coverage(data_file=None)
    statements = {name: unrun.analysis2(str(package / name))[1] for name in ("const.py", "worker.py")}
    (tmp_path / "python").mkdir()
    # A journey's Home Assistant ran its copy's first statement of const.py; a worker, the checkout's of worker.py.
    coverage.CoverageData(str(tmp_path / "python/.coverage"), suffix="journey").add_lines(
        {"/journey/config/custom_components/assist_improver/const.py": statements["const.py"][:1]})
    worker = {str(package / "worker.py"): statements["worker.py"][:1]}
    cut_off = json.dumps({str(package / "worker.py"): statements["worker.py"][1:2]})[:-3]
    (tmp_path / "python/worker-1.jsonl").write_text(f"{json.dumps(worker)}\n{cut_off}")
    (tmp_path / "js").mkdir()
    www = package / "www"
    files = sorted(path.relative_to(www).as_posix() for path in www.rglob("*.js"))
    (tmp_path / "js/js-panel-1.json").write_text(json.dumps({"breakable": {name: [1, 2, 3] for name in files},
                                                            "covered": {name: [1] for name in files}}))
    (tmp_path / "js/js-e2e-2.json").write_text(json.dumps({"breakable": {files[0]: [1, 2, 3, 4]},
                                                          "covered": {files[0]: [2, 4]}}))
    report.main(tmp_path)
    total = sum(len(unrun.analysis2(str(path))[1]) for path in package.rglob("*.py"))
    assert json.loads((tmp_path / "totals.json").read_text()) == {"python": [2, total],
                                                                  "js": [len(files) + 2, 3 * len(files) + 1]}
    assert capsys.readouterr().out.startswith("== coverage: ")
    (tmp_path / "js/js-panel-1.json").unlink()
    with pytest.raises(SystemExit, match=f"no panel coverage recorded in .* for {files[1]}, "):
        report.js(tmp_path / "js")
