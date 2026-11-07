"""Where the maintainer's private data lives: household data, never in a checkout. It holds copies of the household's
stores and recordings, labels, the leak guard's denylist (tools/leak_guard.py), and home-assistant.env, how the
maintainer tools reach the household's Home Assistant (HA_SSH, HA_SSH_KEY, HA_URL).

The folder is ASSIST_IMPROVER_PRIVATE, else assist-improver-private beside the main checkout (the one git's common
folder belongs to, so every worktree finds the same one). Contributors have none: a tool that needs it stops with one
line, and a test that needs it skips, saying why.

  python3 tools/private.py [FILE]      the folder, or FILE in it; exit 1 with one line when it isn't there
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VARIABLE = "ASSIST_IMPROVER_PRIVATE"
NAME = "assist-improver-private"


def main_checkout(root=ROOT):
    """The checkout git's common folder belongs to: root itself, or the one a worktree hangs from. Read from .git, with
    no git to run (the offline tests allow none)."""
    dot = root / ".git"
    if not dot.is_file():
        return root
    gitdir = root / dot.read_text().removeprefix("gitdir:").strip()  # <main>/.git/worktrees/<name>
    common = gitdir / "commondir"
    return (gitdir / common.read_text().strip()).resolve().parent if common.is_file() else root


def folder(root=ROOT, environ=os.environ):
    """The private folder, whether or not it's there."""
    if setting := environ.get(VARIABLE):
        return Path(setting).expanduser()
    return main_checkout(root).parent / NAME


def missing(path):
    """Why path can't be read, in one line, or None."""
    if not folder().is_dir():
        return f"No private folder at {folder()}: set {VARIABLE} to it (tools/private.py)."
    if not path.exists():
        return f"No {path.name} in the private folder {folder()} (tools/private.py)."
    return None


def path(*parts):
    """A path in the private folder; the tool stops with one line when it isn't there."""
    found = folder().joinpath(*parts)
    if why := missing(found):
        sys.exit(why)
    return found


def home_assistant(*required):
    """How the maintainer tools reach the household's Home Assistant: home-assistant.env's settings, NAME=VALUE lines
    (as tools/deploy.sh sources them), a leading ~ expanded. The tool stops with one line when one it requires isn't
    there."""
    found, settings = path("home-assistant.env"), {}
    for line in found.read_text().splitlines():
        name, equals, value = line.strip().partition("=")
        if equals and not name.startswith("#"):
            settings[name] = os.path.expanduser(value.strip().strip("'\""))
    if lacking := [name for name in required if not settings.get(name)]:
        sys.exit(f"No {', '.join(lacking)} in {found} (tools/private.py).")
    return settings


if __name__ == "__main__":
    print(path(*sys.argv[1:2]))
