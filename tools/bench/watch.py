"""Home Assistant's new versions, tested once each (.github/workflows/home-assistant.yml runs this every 5 minutes).

  python3 tools/bench/watch.py      in GitHub Actions: gh, and GH_TOKEN allowed to start workflows

A channel's versions are what https://version.home-assistant.io/<channel>.json says of Home Assistant, the Supervisor
and HA OS. When they change, the channel's tests run (.github/workflows/tests.yml, dispatched with the versions as the
run's name). Those runs are the state: a channel whose versions have a run, whatever its result, isn't run again.
"""

import json
import subprocess
import sys
import urllib.request

CHANNELS = ("stable", "beta", "dev")
LISTED = 1000  # The test runs dispatched so far that are looked through, newest first.


def name(channel, versions):
    """The run's name for a channel's versions (version.home-assistant.io's answer)."""
    return (f"Home Assistant {channel}: {versions['homeassistant']['qemux86-64']}, Supervisor "
            f"{versions['supervisor']}, HA OS {versions['hassos']['ova']}")


def due(found, ran):
    """The runs to start: {channel: its run's name} for each channel's versions ({channel: answer}) with no run yet
    (ran: the names of the runs there have been)."""
    return {channel: name(channel, versions) for channel, versions in found.items()
            if name(channel, versions) not in ran}


def versions(channel):
    request = urllib.request.Request(f"https://version.home-assistant.io/{channel}.json",
                                     headers={"User-Agent": "assist-improver-ci"})  # It refuses Python's own.
    with urllib.request.urlopen(request, timeout=30) as answer:
        return json.load(answer)


def gh(*args):
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout


def main():
    ran = set(gh("run", "list", "--workflow", "tests.yml", "--event", "workflow_dispatch", "--limit", str(LISTED),
                 "--json", "displayTitle", "--jq", ".[].displayTitle").splitlines())
    for channel, title in due({channel: versions(channel) for channel in CHANNELS}, ran).items():
        gh("workflow", "run", "tests.yml", "-f", f"channel={channel}", "-f", f"versions={title}")
        print(f"Started: {title}")


if __name__ == "__main__":
    sys.exit(main())
