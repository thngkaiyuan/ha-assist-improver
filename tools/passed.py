"""Whether a build passed its tests: tools/deploy.sh ships only builds that did.

  python3 tools/passed.py      exit 0 if HEAD passed (saying where), 1 if not

A pass is GitHub's newest finished Stable check for the commit (.github/workflows/tests.yml), and nothing else. A
commit gets one with each push and each new Home Assistant stable, so a red one on a new stable takes an earlier pass
away. What ships is custom_components/assist_improver as committed, so the working copy must match the commit.
"""

import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COMPONENT = "custom_components/assist_improver"
REPO = "thngkaiyuan/ha-assist-improver"  # The public repository, owner/name on GitHub: read here and by leak_guard.py.


def git(*args):
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, check=True).stdout.strip()


def github_verdict(commit, fetch=None):
    """GitHub's newest finished Stable check for the commit: (whether it passed, its URL), or None while there's none,
    as for a commit GitHub doesn't know yet. A skipped or cancelled one says nothing."""
    url = f"https://api.github.com/repos/{REPO}/commits/{commit}/check-runs?check_name=Stable&per_page=100"
    try:
        answer = (fetch or _fetch)(url)
    except urllib.error.HTTPError as error:
        if error.code in (404, 422):  # No such commit there.
            return None
        raise
    runs = [run for run in answer.get("check_runs", [])
            if run.get("completed_at") and run.get("conclusion") not in ("skipped", "cancelled")]
    if not runs:
        return None
    newest = max(runs, key=lambda run: run["completed_at"])
    return newest["conclusion"] == "success", newest["html_url"]


def _fetch(url):
    request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(request, timeout=30) as answer:
        return json.load(answer)


def check(fetch=None):
    """Why HEAD may ship, or None (printing why not)."""
    if changed := git("status", "--porcelain", "--", COMPONENT):
        print(f"{COMPONENT} differs from the commit (commit it, test it, then ship):\n{changed}")
        return None
    commit = git("rev-parse", "HEAD")
    verdict = github_verdict(commit, fetch)
    if verdict is None:
        print(f"No Stable check for commit {commit[:12]} on {REPO} yet: push it and wait for the Stable check.")
        return None
    if not verdict[0]:
        print(f"Commit {commit[:12]} failed GitHub's newest Stable check: {verdict[1]}")
        return None
    return f"passed on GitHub: {verdict[1]}"


def main(args):
    if args:
        sys.exit(__doc__)
    why = check()
    if why is None:
        sys.exit(1)
    print(why)


if __name__ == "__main__":
    main(sys.argv[1:])
