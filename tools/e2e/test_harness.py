"""The harness itself, with no Home Assistant: what the benches rely on it for."""

import os
import subprocess

import pytest

import conftest


@pytest.fixture
def refusing(tmp_path, monkeypatch):
    """HA OS stood in, its Supervisor refusing every stop: its ssh answers `ha core stop` non-zero, and anything else
    (copying /config back) with an empty folder. Gives journey(name): a journey's process to stop, in a folder of its
    own."""
    stand_ins = tmp_path / "bin"
    stand_ins.mkdir()
    (stand_ins / "ssh").write_text('#!/bin/sh\ncase "$*" in *"ha core stop"*)\n'
                                   "  echo 'Error: Another job is running' >&2; exit 1;;\n"
                                   "esac\nexec tar -cf - -T /dev/null\n")
    (stand_ins / "ssh").chmod(0o755)
    monkeypatch.setenv("PATH", f"{stand_ins}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(conftest, "HAOS", str(tmp_path / "ssh_config"))
    monkeypatch.setattr(conftest, "_STUCK", [], raising=False)  # The bench's own journeys never meet these stops.

    def journey(name):
        folder = tmp_path / name
        folder.mkdir()
        process = subprocess.Popen(["sleep", "60"], start_new_session=True)  # Its log's ssh, as _start_process has it.
        process.log, process.folder = open(folder / "e2e-hass.log", "ab"), folder
        process.stop = ["ssh", "-F", conftest.HAOS, "haos", conftest._ha_core("stop")]
        conftest._RUNNING.add(process)
        return process
    return journey


def test_a_refused_stop_on_ha_os_ends_the_session(refusing):
    """On HA OS, a stop its Supervisor refuses ends the session once the harness's own process is stopped: that Home
    Assistant may still run, and no journey may start on it."""
    process = refusing("t01")
    with pytest.raises(pytest.exit.Exception, match="Another job is running"):
        conftest._stop_process(process)
    assert process.poll() is not None and process not in conftest._RUNNING


def test_no_journey_starts_after_a_refused_stop_on_ha_os(refusing, tmp_path, monkeypatch):
    """However the stops' failures are reported: here a journey that restarted Home Assistant, both its stops refused
    (pytest groups two finalizers' errors, and an Exit among them no longer ends the session). The next journey's
    start ends the session before it copies its config in."""
    monkeypatch.setattr(conftest, "_haos_push", lambda folder: pytest.fail("copied in under a Home Assistant that may "
                                                                           "still run"))
    for process in (refusing("t01-restarted"), refusing("t01")):
        with pytest.raises(Exception, match="Another job is running"):
            conftest._stop_process(process)
    (tmp_path / "t02").mkdir()
    with pytest.raises(pytest.exit.Exception, match="Another job is running"):
        conftest._start_process(tmp_path / "t02", "http://127.0.0.1:9", 8123)
