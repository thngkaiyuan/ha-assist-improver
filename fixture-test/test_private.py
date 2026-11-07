"""Where the maintainer's private data lives (tools/private.py): ASSIST_IMPROVER_PRIVATE, else assist-improver-private
beside the main checkout, the same folder from each of its worktrees. A tool stops with one line when it isn't there;
a test on it skips, saying why, and fails on a path that moved."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

import conftest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("private", ROOT / "tools/private.py")
private = importlib.util.module_from_spec(spec)
spec.loader.exec_module(private)


def test_by_default_its_beside_the_main_checkout_from_it_and_from_each_worktree(tmp_path):
    main, worktree = tmp_path / "checkout", tmp_path / "checkout/.claude/worktrees/one"
    (main / ".git/worktrees/one").mkdir(parents=True)
    (main / ".git/worktrees/one/commondir").write_text("../..\n")  # As git writes it.
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {main}/.git/worktrees/one\n")
    (tmp_path / "export").mkdir()  # No git at all: an archive's copy.
    for checkout in (main, worktree, tmp_path / "export"):
        assert private.folder(checkout, {}) == tmp_path / "assist-improver-private"


def test_the_variable_puts_it_elsewhere(tmp_path):
    assert private.folder(tmp_path, {"ASSIST_IMPROVER_PRIVATE": "~/elsewhere"}) == Path.home() / "elsewhere"


def test_a_tool_stops_with_one_line_when_the_folder_a_file_in_it_or_a_setting_is_missing(tmp_path, monkeypatch):
    folder = tmp_path / "private"
    monkeypatch.setenv("ASSIST_IMPROVER_PRIVATE", str(folder))
    with pytest.raises(SystemExit, match="^No private folder at .*: set ASSIST_IMPROVER_PRIVATE to it"):
        private.home_assistant()
    folder.mkdir()
    with pytest.raises(SystemExit, match="^No home-assistant.env in the private folder "):
        private.home_assistant()
    (folder / "home-assistant.env").write_text("# How to reach it.\nHA_SSH=root@ha.invalid\nHA_SSH_KEY=~/.ssh/key\n"
                                               "HA_URL='https://ha.invalid'\n")
    assert private.home_assistant("HA_URL") == {
        "HA_SSH": "root@ha.invalid", "HA_SSH_KEY": str(Path.home() / ".ssh/key"), "HA_URL": "https://ha.invalid"}
    (folder / "home-assistant.env").write_text("HA_SSH=root@ha.invalid\n")
    with pytest.raises(SystemExit, match="^No HA_SSH_KEY, HA_URL in .*home-assistant.env "):
        private.home_assistant("HA_SSH", "HA_SSH_KEY", "HA_URL")


def test_a_test_on_household_data_skips_without_the_folder_and_fails_on_a_path_that_moved(tmp_path, monkeypatch):
    folder = tmp_path / "private"
    monkeypatch.setenv("ASSIST_IMPROVER_PRIVATE", str(folder))
    asked = SimpleNamespace(getoption=lambda name: name == "--private")
    with pytest.raises(pytest.skip.Exception, match="Household data stays out of the checkout: no private folder"):
        conftest.private("stores")
    folder.mkdir()
    with pytest.raises(pytest.fail.Exception, match="No stores in the private folder"):
        conftest.private("stores", on_demand=asked)
    (folder / "stores").mkdir()
    assert conftest.private("stores") == conftest.private("stores", on_demand=asked) == folder / "stores"
    with pytest.raises(pytest.skip.Exception, match="on demand: --private"):  # Not asked for.
        conftest.private("stores", on_demand=SimpleNamespace(getoption=lambda name: False))
