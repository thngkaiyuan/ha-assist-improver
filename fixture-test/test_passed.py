"""Only builds that passed their tests ship (tools/passed.py): a pass is GitHub's newest finished Stable check for the
commit, and tools/deploy.sh refuses a build with none before anything reaches Home Assistant. Each test of passed.py
names the repository and stands in for GitHub's answer."""

import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import urllib.error

import pytest

POPEN = subprocess.Popen  # Before the effect guards: deploy.sh runs for real here, with stand-ins for ssh and scp.
ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("passed", ROOT / "tools/passed.py")
passed = importlib.util.module_from_spec(spec)
spec.loader.exec_module(passed)
COMMIT = "c" * 40


def check_run(conclusion, completed_at, number):  # One of GitHub's check-runs, in the parts read.
    return {"conclusion": conclusion, "completed_at": completed_at, "html_url": f"https://github.com/o/r/runs/{number}"}


def unknown(code):  # A stand-in for GitHub's answer for a commit it doesn't know.
    def fetch(url):
        raise urllib.error.HTTPError(url, code, "No commit found", {}, None)
    return fetch


def test_githubs_newest_finished_stable_check_is_the_verdict(monkeypatch):
    """Each push and each new Home Assistant stable checks a commit again: the newest finished check holds, so a red
    one on a new stable takes away an earlier pass; a skipped or cancelled one says nothing."""
    monkeypatch.setattr(passed, "REPO", "o/r")
    asked = []
    runs = [check_run("failure", "2026-11-06T10:00:00Z", 1), check_run("success", "2026-11-06T11:00:00Z", 2)]

    def github(url):  # A stand-in for GitHub's answer.
        asked.append(url)
        return {"check_runs": runs}
    assert passed.github_verdict("abc", github) == (True, "https://github.com/o/r/runs/2")
    assert asked == ["https://api.github.com/repos/o/r/commits/abc/check-runs?check_name=Stable&per_page=100"]
    runs.insert(0, check_run("failure", "2026-11-07T09:00:00Z", 3))  # A new stable, newest, listed first.
    assert passed.github_verdict("abc", github) == (False, "https://github.com/o/r/runs/3")
    runs += [check_run("skipped", "2026-11-08T09:00:00Z", 4), check_run("cancelled", "2026-11-08T10:00:00Z", 5),
             check_run(None, None, 6)]  # Beta's run, a cancelled one, one still running.
    assert passed.github_verdict("abc", github) == (False, "https://github.com/o/r/runs/3")
    assert passed.github_verdict("abc", lambda url: {"check_runs": []}) is None  # Not run yet.


def test_a_commit_github_doesnt_know_has_no_verdict_yet(monkeypatch, capsys):
    """Not pushed yet, GitHub answers 404 or 422: no pass until it's pushed and its Stable check passes. Any other
    error isn't taken for a verdict."""
    monkeypatch.setattr(passed, "REPO", "o/r")
    monkeypatch.setattr(passed, "git", lambda *args: {("rev-parse", "HEAD"): COMMIT}.get(args, ""))
    for code in (404, 422):
        assert passed.github_verdict("abc", unknown(code)) is None
        assert passed.check(unknown(code)) is None
        assert capsys.readouterr().out == (f"No Stable check for commit {COMMIT[:12]} on o/r yet: push it and wait "
                                           "for the Stable check.\n")
    with pytest.raises(urllib.error.HTTPError):
        passed.check(unknown(500))


def test_only_a_commit_whose_newest_stable_check_passed_ships_and_the_working_copy_must_match_it(monkeypatch,
                                                                                                   capsys):
    monkeypatch.setattr(passed, "REPO", "o/r")
    answers = {("rev-parse", "HEAD"): COMMIT,
               ("status", "--porcelain", "--", passed.COMPONENT): " M custom_components/assist_improver/llm.py"}
    monkeypatch.setattr(passed, "git", lambda *args: answers[args])
    green = {"check_runs": [check_run("success", "2026-11-07T09:00:00Z", 4)]}
    assert passed.check(lambda url: green) is None
    assert "differs from the commit" in capsys.readouterr().out
    answers[("status", "--porcelain", "--", passed.COMPONENT)] = ""
    assert passed.check(lambda url: green) == "passed on GitHub: https://github.com/o/r/runs/4"
    assert passed.check(lambda url: {"check_runs": [check_run("failure", "2026-11-07T09:00:00Z", 3)]}) is None
    assert capsys.readouterr().out == (f"Commit {COMMIT[:12]} failed GitHub's newest Stable check: "
                                       "https://github.com/o/r/runs/3\n")


def deploy(tmp_path, verdict, private=True):
    """deploy.sh in a copy of its folders, tools/passed.py answering as given, with ssh and scp stood in for, and a
    private folder saying how to reach Home Assistant (tools/private.py; private: its home-assistant.env, if not the
    usual one), or none: (exit status, output, whether ssh or scp was reached)."""
    (tmp_path / "tools").mkdir(parents=True)
    for name in ("deploy.sh", "private.py"):
        (tmp_path / "tools" / name).write_bytes((ROOT / "tools" / name).read_bytes())
    (tmp_path / "tools/passed.py").write_text(f"import sys\nprint('stand-in verdict')\nsys.exit({verdict})\n")
    if private:
        (tmp_path / "private").mkdir()
        (tmp_path / "private/home-assistant.env").write_text(
            private if isinstance(private, str) else "HA_SSH=root@ha.invalid\nHA_SSH_KEY=~/.ssh/stand-in\n")
    (tmp_path / "custom_components/assist_improver").mkdir(parents=True)
    (tmp_path / "custom_components/assist_improver/manifest.json").write_text('{"version": "9.9.9"}')
    (tmp_path / ".venv/bin").mkdir(parents=True)
    (tmp_path / ".venv/bin/python").symlink_to(sys.executable)
    stand_ins = tmp_path / "bin"
    stand_ins.mkdir()
    for name in ("ssh", "scp"):
        (stand_ins / name).write_text(f"#!/bin/sh\necho {name} >> {tmp_path / 'reached'}\nexit 1\n")
        (stand_ins / name).chmod(0o755)
    env = {**os.environ, "PATH": f"{stand_ins}{os.pathsep}{os.environ['PATH']}", "HOME": str(tmp_path),
           "ASSIST_IMPROVER_PRIVATE": str(tmp_path / "private")}
    with POPEN(["bash", str(tmp_path / "tools/deploy.sh")], env=env, stdout=subprocess.PIPE,
               stderr=subprocess.STDOUT, text=True) as process:
        out = process.communicate(timeout=60)[0]
    return process.returncode, out, (tmp_path / "reached").exists()


@pytest.mark.parametrize("verdict, reached", [(1, False), (0, True)], ids=["no pass", "a pass"])
def test_deploy_asks_for_a_pass_before_it_reaches_home_assistant(tmp_path, verdict, reached):
    """Refused builds stop before ssh; a passed one goes on to it."""
    status, out, ssh = deploy(tmp_path, verdict)
    assert status != 0 and "stand-in verdict" in out, out
    assert ("this build hasn't passed its tests" in out) is not reached, out
    assert ssh is reached, out


def test_deploy_stops_with_one_line_when_theres_no_private_folder(tmp_path):
    """Its Home Assistant's address and key are private (tools/private.py): without them it stops first."""
    assert deploy(tmp_path, 0, private=False) == (1, f"No private folder at {tmp_path / 'private'}: set "
                                                     "ASSIST_IMPROVER_PRIVATE to it (tools/private.py).\n", False)
    assert deploy(tmp_path / "lacking", 0, private="HA_SSH=root@ha.invalid\n") == (
        1, f"No HA_SSH_KEY in {tmp_path / 'lacking/private/home-assistant.env'} (tools/private.py).\n", False)
