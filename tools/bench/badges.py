"""The README's badges: shields.io endpoint files on the repository's badges branch, read by shields' endpoint badge.

  python3 tools/bench/badges.py verdict DIR  tests.yml's verdict job: NEEDS=its toJSON(needs), RUN=the run's ID
  python3 tools/bench/badges.py publish RUN  badges.yml: the run's badges artifact to the badges branch (gh, GH_TOKEN
                                             allowed to write contents, GH_REPO)

A run writes the files of what it tested: a channel whose job skipped didn't run, and keeps its file. Each file names
its run, so a run that finishes late never covers a newer one's. The badges branch is made once, by hand.
"""

import base64
import json
import os
import random
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

BRANCH = "badges"
TRIES = 5  # Writes to one branch at once can clash: each file is read and written again, up to this many times.


def endpoint(label, message, color, run):
    return {"schemaVersion": 1, "label": label, "message": message, "color": color, "run": run}


def said(result):
    return ("passing", "brightgreen") if result == "success" else ("failing", "red")


def month(version):  # 2026.12.0.dev202611070215: 2026.12
    return ".".join(version.split(".")[:2])


def coverage_color(percent):  # Shields' endpoint badge is grey unless told.
    return "brightgreen" if percent >= 80 else "yellow" if percent >= 60 else "red"


def verdict(needs, run):
    """The endpoint files for a test run's results ({job: {"result", "outputs"}}, as toJSON(needs) gives them):
    {file name: its JSON}. Stable is the Stable check's verdict over every supported release, named by their months;
    dev names the release its build leads to (its run names the build)."""
    files = {}
    tested = needs["versions"]["outputs"]
    if needs["gate"]["result"] != "skipped":
        state, color = said(needs["gate"]["result"])
        try:
            months = [month(version) for version in json.loads(tested["stable"])]
            span = months[0] if months[0] == months[-1] else f"{months[0]} – {months[-1]}"
        except (KeyError, ValueError, IndexError):  # No list: the versions job failed, and so did the check.
            span = ""
        files["stable.json"] = endpoint("Home Assistant", f"{span} {state}".strip(), color, run)
    for channel in ("beta", "dev"):
        if needs[channel]["result"] != "skipped":
            state, color = said(needs[channel]["result"])
            version = month(tested[channel]) if channel == "dev" else tested[channel]
            files[f"{channel}.json"] = endpoint(f"Home Assistant {channel}", f"{version} {state}", color, run)
    if needs["coverage"]["result"] == "success":
        totals = json.loads(needs["coverage"]["outputs"]["totals"])
        percent = (totals["python"][0] + totals["js"][0]) * 100 // (totals["python"][1] + totals["js"][1])
        files["coverage.json"] = endpoint("coverage", f"{percent}%", coverage_color(percent), run)
    return files


def kept(stored, ours):
    """Whether the stored file stays: a newer run's, or ours already (a run tried again keeps its ID, and its new
    verdict replaces the old)."""
    return stored is not None and (stored.get("run", 0) > ours["run"] or stored == ours)


def api(method, path, body=None):
    """gh api: (200, its JSON answer) on success, else (its HTTP status, None)."""
    done = subprocess.run(["gh", "api", "-X", method, path, *(["--input", "-"] if body else [])],
                          input=json.dumps(body) if body else None, capture_output=True, text=True)
    if done.returncode == 0:
        return 200, json.loads(done.stdout or "null")
    status = re.search(r"\(HTTP (\d{3})\)", done.stderr)
    if not status:
        sys.exit(done.stderr.strip())
    return int(status.group(1)), None


def publish(name, ours):
    path = f"repos/{{owner}}/{{repo}}/contents/{name}"
    for attempt in range(TRIES):
        status, answer = api("GET", f"{path}?ref={BRANCH}")
        if status not in (200, 404):
            sys.exit(f"badges.py: reading {name} on {BRANCH}: HTTP {status}")
        stored = json.loads(base64.b64decode(answer["content"])) if answer else None
        if kept(stored, ours):
            return print(f"{name}: kept (run {stored.get('run')})")
        content = base64.b64encode(json.dumps(ours).encode() + b"\n").decode()
        status, _ = api("PUT", path, {"message": f"{name} from run {ours['run']}", "content": content,
                                      "branch": BRANCH, **({"sha": answer["sha"]} if answer else {})})
        if status == 200:
            return print(f"{name}: {ours['message']}")
        if status not in (409, 422) or attempt == TRIES - 1:
            sys.exit(f"badges.py: writing {name} on {BRANCH}: HTTP {status}")
        time.sleep(2 ** attempt + random.random())  # Another write moved the branch: read it again.


def main():
    command, where = (sys.argv[1:] + ["", ""])[:2]
    if command == "verdict" and where:
        folder = Path(where)
        folder.mkdir(parents=True, exist_ok=True)
        for name, content in verdict(json.loads(os.environ["NEEDS"]), int(os.environ["RUN"])).items():
            (folder / name).write_text(json.dumps(content) + "\n")
            print(f"{name}: {content['message']}")
    elif command == "publish" and where:
        if api("GET", f"repos/{{owner}}/{{repo}}/branches/{BRANCH}")[0] == 404:
            sys.exit(f"badges.py: no {BRANCH} branch: make it once (tools/bench/README.md)")
        with tempfile.TemporaryDirectory() as folder:
            subprocess.run(["gh", "run", "download", where, "-n", "badges", "-D", folder], check=True)
            for file in sorted(Path(folder).rglob("*.json")):
                publish(file.name, json.loads(file.read_text()))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    sys.exit(main())
