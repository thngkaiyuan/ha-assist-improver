"""The harness itself, with no Home Assistant: what the benches rely on it for."""

import os
import subprocess

import pytest

import conftest


def test_a_refused_stop_on_ha_os_ends_the_session(tmp_path, monkeypatch):
    """On HA OS, a stop its Supervisor refuses (ssh answers non-zero) ends the session once the harness's own process
    is stopped: that Home Assistant may still run, and no journey may start on it. The stand-in ssh answers anything
    else (copying /config back) with an empty folder."""
    stand_ins, folder = tmp_path / "bin", tmp_path / "config"
    stand_ins.mkdir()
    folder.mkdir()
    (stand_ins / "ssh").write_text('#!/bin/sh\ncase "$*" in *"ha core stop"*)\n'
                                   "  echo 'Error: Another job is running' >&2; exit 1;;\n"
                                   "esac\nexec tar -cf - -T /dev/null\n")
    (stand_ins / "ssh").chmod(0o755)
    monkeypatch.setenv("PATH", f"{stand_ins}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(conftest, "HAOS", str(tmp_path / "ssh_config"))
    process = subprocess.Popen(["sleep", "60"], start_new_session=True)  # Its log's ssh, as _start_process has it.
    process.log, process.folder = open(folder / "e2e-hass.log", "ab"), folder
    process.stop = ["ssh", "-F", conftest.HAOS, "haos", conftest._ha_core("stop")]
    conftest._RUNNING.add(process)
    with pytest.raises(pytest.exit.Exception, match="Another job is running"):
        conftest._stop_process(process)
    assert process.poll() is not None and process not in conftest._RUNNING
