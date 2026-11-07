"""Home Assistant's new versions, tested once each (.github/workflows/home-assistant.yml runs this every 5 minutes),
and the versions each test run tests (tests.yml's versions job).

  python3 tools/bench/watch.py           in GitHub Actions: gh, and GH_TOKEN allowed to start workflows
  python3 tools/bench/watch.py versions  stable=[every supported release], beta=..., dev=... (for $GITHUB_OUTPUT)

A channel's versions are what https://version.home-assistant.io/<channel>.json says of Home Assistant, the Supervisor
and HA OS. When they change, the channel's tests run (.github/workflows/tests.yml, dispatched with the versions as the
run's name). Those runs are the state: a channel whose versions have a run, whatever its result, isn't run again. A new
stable's run tests every supported stable release (supported()).
"""

import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

CHANNELS = ("stable", "beta", "dev")
LISTED = 1000  # The test runs dispatched so far that are looked through, newest first.
HACS = Path(__file__).resolve().parents[2] / "hacs.json"  # Its "homeassistant": the oldest release HACS installs on.
REGISTRY = "https://ghcr.io"
IMAGE = "home-assistant/home-assistant"  # Home Assistant's official image, as the bench pulls it.
RELEASE = re.compile(r"(\d{4})\.(\d{1,2})\.(\d+)")  # An exact stable release: not a month's tag, a beta or a dev build.


def name(channel, versions):
    """The run's name for a channel's versions (version.home-assistant.io's answer)."""
    return (f"Home Assistant {channel}: {versions['homeassistant']['qemux86-64']}, Supervisor "
            f"{versions['supervisor']}, HA OS {versions['hassos']['ova']}")


def due(found, ran):
    """The runs to start: {channel: its run's name} for each channel's versions ({channel: answer}) with no run yet
    (ran: the names of the runs there have been)."""
    return {channel: name(channel, versions) for channel, versions in found.items()
            if name(channel, versions) not in ran}


def supported(minimum, stable, tags):
    """The stable releases to test, oldest first: the minimum itself, and the newest patch of each month from the
    minimum's to the newest stable's, none newer than it (tags: the image's tags, any kind); none if the minimum is
    newer. The newest stable is its month's newest patch even before its tag is listed: its pull fails, not its test."""
    def release(tag):
        match = RELEASE.fullmatch(tag)
        return tuple(map(int, match.groups())) if match else None
    low, high = release(minimum), release(stable)
    if low > high:
        return []
    by_month = {}
    for version in filter(None, map(release, [*tags, minimum, stable])):
        if low <= version <= high:
            by_month[version[:2]] = max(by_month.get(version[:2], version), version)
    return [".".join(map(str, version)) for version in sorted({*by_month.values(), low})]


def versions(channel):
    request = urllib.request.Request(f"https://version.home-assistant.io/{channel}.json",
                                     headers={"User-Agent": "assist-improver-ci"})  # It refuses Python's own.
    with urllib.request.urlopen(request, timeout=30) as answer:
        return json.load(answer)


def tags():
    """The image's tags on ghcr.io, every page (an anonymous token reads them)."""
    with urllib.request.urlopen(f"{REGISTRY}/token?scope=repository:{IMAGE}:pull", timeout=30) as answer:
        token = json.load(answer)["token"]
    found, page = [], f"/v2/{IMAGE}/tags/list?n=1000"
    while page:
        request = urllib.request.Request(REGISTRY + page, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(request, timeout=30) as answer:
            found += json.load(answer)["tags"]
            following = re.match(r"<([^>]+)>", answer.headers.get("Link", ""))  # The next page's, if any.
            page = following and following.group(1)
    return found


def gh(*args):
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout


def tested():
    """tests.yml's versions job: each channel's Home Assistant (HA OS's x86-64 build, as the HA OS job installs it),
    and for stable every supported release; none supported fails, as when hacs.json names a release not out yet."""
    found = {channel: versions(channel)["homeassistant"]["qemux86-64"] for channel in CHANNELS}
    stable = supported(json.loads(HACS.read_text())["homeassistant"], found["stable"], tags())
    if not stable:
        sys.exit(f"watch.py: no supported stable release (hacs.json's minimum is newer than {found['stable']})")
    print(f"stable={json.dumps(stable)}\nbeta={found['beta']}\ndev={found['dev']}")


def main():
    if sys.argv[1:] == ["versions"]:
        return tested()
    ran = set(gh("run", "list", "--workflow", "tests.yml", "--event", "workflow_dispatch", "--limit", str(LISTED),
                 "--json", "displayTitle", "--jq", ".[].displayTitle").splitlines())
    for channel, title in due({channel: versions(channel) for channel in CHANNELS}, ran).items():
        gh("workflow", "run", "tests.yml", "-f", f"channel={channel}", "-f", f"versions={title}")
        print(f"Started: {title}")


if __name__ == "__main__":
    sys.exit(main())
