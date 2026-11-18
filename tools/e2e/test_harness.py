"""The harness itself, with no Home Assistant: what the journeys and the benches rely on it for."""

import os
import subprocess
import types

import pytest

import conftest


class Clock:
    """The harness's clock, stood in: its waits sleep on it, so minutes pass at once."""

    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class Unfolding(conftest.HomeAssistant):
    """A Home Assistant whose run unfolds on the clock: runs, [(from when, the run then)] (none before the first);
    logged, when its replay workers log a line; it stops at `stops` (None: it doesn't)."""

    def __init__(self, folder, clock, runs, logged=(), stops=None):
        self.folder, self.clock, self.runs, self.logged, self.stops = folder, clock, runs, logged, stops
        self.process = types.SimpleNamespace(poll=lambda: None if self.up() else 0)

    def up(self):
        return self.stops is None or self.clock.now < self.stops

    def run(self):
        if not self.up():
            raise ConnectionError("Home Assistant closed the websocket")
        lines = sum(at <= self.clock.now for at in self.logged)  # Its output so far, as the harness keeps it.
        (self.folder / "e2e-hass.log").write_text("DEBUG (MainThread) [...worker] Replay worker: ...\n" * lines)
        return next((run for at, run in reversed(self.runs) if at <= self.clock.now), None)


def filed(at, stage, status="running"):
    return at, {"id": "r1", "trigger": "filed", "status": status, "stage": stage, "progress": {"total": 1}}


@pytest.fixture
def clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(conftest, "time", clock)
    return clock


def test_a_run_is_waited_on_to_its_end_while_it_moves(tmp_path, clock):
    """Five minutes in all, as on a busy machine: its stage moves 50 s apart at the most, and then for three minutes
    only its replay workers do, a line every 25 s (its sandboxes starting, its replays going)."""
    runs = [filed(2, "triage"), filed(30, "analyze"), filed(80, "propose"), filed(280, "write_up"),
            filed(300, "write_up", status="done")]
    ha = Unfolding(tmp_path, clock, runs, logged=range(85, 280, 25))
    assert ha.wait_for_run("filed") == runs[-1][1]
    assert 300 <= clock.now < 301


@pytest.mark.parametrize("runs", [[filed(2, "triage"), filed(40, "analyze")],
                                  [(0, {"id": "m1", "trigger": "manual", "status": "done"})]],
                         ids=["stuck", "never-started"])
def test_a_run_that_stops_moving_fails_a_minute_after_it_last_moved(tmp_path, clock, runs):
    """Stuck at Analyze (from 40 s in), or never started (the last run is another, ended), it says so a minute on."""
    with pytest.raises(AssertionError, match="^Nothing moved for 60 s waiting for a filed run to end") as failed:
        Unfolding(tmp_path, clock, runs).wait_for_run("filed")
    assert str(runs[-1][1]) in str(failed.value)
    assert runs[-1][0] + 60 <= clock.now < runs[-1][0] + 61


def test_a_wait_on_a_run_fails_at_once_when_home_assistant_stops(tmp_path, clock):
    with pytest.raises(AssertionError, match="^Home Assistant stopped while waiting for a filed run to end$"):
        Unfolding(tmp_path, clock, [filed(2, "triage")], stops=10).wait_for_run("filed")
    assert 10 <= clock.now < 11


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
    monkeypatch.setattr(conftest, "_STUCK", [])  # The bench's own journeys never meet these stops.

    def journey(name):
        folder = tmp_path / name
        folder.mkdir()
        process = subprocess.Popen(["sleep", "60"], start_new_session=True)  # Its log's ssh, as _start_process has it.
        process.log, process.folder = open(folder / "e2e-hass.log", "ab"), folder
        process.stop = ["ssh", "-F", conftest.HAOS, "haos", conftest._ha_core("stop")]
        conftest._RUNNING.add(process)
        return process
    return journey


def test_a_refused_stop_on_ha_os_fails_loudly(refusing):
    """On HA OS, a stop its Supervisor refuses raises, saying why, once the harness's own process is stopped."""
    process = refusing("t01")
    with pytest.raises(RuntimeError, match="didn't stop: Error: Another job is running$"):
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
