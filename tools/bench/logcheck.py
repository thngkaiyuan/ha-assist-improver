"""What the journeys' Home Assistants logged: nothing of Assist Improver's was an error, nor a blocking call Home
Assistant caught on its event loop in the improver's code (each journey checks its own log: tools/e2e/conftest.py);
on HA OS (tools/bench/haos.sh), also every replay's guard said what Landlock did, as expected.

  python3 tools/bench/logcheck.py EXPECTED LOG...    exit 1, saying why, unless every guard line says EXPECTED
                                                     (at least one does) and nothing is the improver's (ours)
"""

import re
import sys
from pathlib import Path

RECORD = re.compile(r"\b(DEBUG|INFO|WARNING|ERROR|CRITICAL) \([^)]*\) \[[^\]]+\] ")  # A log record's first line.
BLOCKING = "Detected blocking call"  # Home Assistant's warning of blocking I/O on its event loop (util/loop.py).
OURS = "custom_components/assist_improver/"


def records(text):
    """Each log record: its level and its lines (a traceback's with it)."""
    level, lines = None, []
    for line in text.splitlines():
        if match := RECORD.search(line):
            if level:
                yield level, "\n".join(lines).rstrip()
            level, lines = match.group(1), [line]
        elif level:
            lines.append(line)
    if level:
        yield level, "\n".join(lines).rstrip()


def ours(level, record):
    """Whether a log record is the improver's to fix: an error naming it, or a blocking call Home Assistant caught in
    its code (its warning, or its traceback, points into custom_components/assist_improver)."""
    return level in ("ERROR", "CRITICAL") and "assist_improver" in record or BLOCKING in record and OURS in record


def improvers(text):
    """The records of a log that are the improver's to fix (ours)."""
    return [record for level, record in records(text) if ours(level, record)]


def problems(texts, expected):
    """What's wrong with these logs: no guard line, a guard line that isn't the expected one, the improver's (ours)."""
    found, wrong = 0, []
    for text in texts:
        for level, record in records(text):
            if "Replay guard: " in record:
                found += 1
                if f"Replay guard: {expected}" not in record:
                    wrong.append(record)
                    continue
            if ours(level, record):
                wrong.append(record)
    return wrong if found else ["No replay's guard said what Landlock did", *wrong]


def main(args):
    if len(args) < 2:
        sys.exit(__doc__)
    expected, paths = args[0], [Path(arg) for arg in args[1:]]
    if found := problems((path.read_text(errors="replace") for path in paths), expected):
        print(*found, sep="\n\n")
        sys.exit(1)
    print(f"== logs: every replay's guard said {expected}; no error or blocking call of Assist Improver's "
          f"({len(paths)} logs)")


if __name__ == "__main__":
    main(sys.argv[1:])
