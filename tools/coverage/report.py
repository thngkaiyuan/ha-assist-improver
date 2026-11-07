"""tools/check.sh --coverage's figure. report.py DIR combines what every process left in DIR: in python/, coverage.py's
data and each replay worker's snapshots (site/sitecustomize.py); in js/, the panel's (tools/ui/jscov.py). It writes
DIR/totals.json, {"python": [covered, statements], "js": [covered, lines]}, and prints one line. Every .py file of the
integration counts, run or not; every www/**/*.js file must have been recorded. Exits 1, saying why, when data is
missing or doesn't map to the checkout. Runs on Home Assistant's .venv, which has coverage.py."""

from collections import defaultdict
import json
import os
from pathlib import Path
import sys
import tempfile

import coverage

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "custom_components/assist_improver"


def fail(why):
    sys.exit(f"report.py: {why}")


def snapshots(path):
    """A worker's lines run, by file: every complete line of its file (a kill can cut off its last)."""
    lines = defaultdict(set)
    for line in path.read_text().split("\n")[:-1]:
        for name, found in json.loads(line).items():
            lines[name].update(found)
    return lines


def python(folder):
    """(covered, statements) over every .py file of the package."""
    if not list(folder.glob(".coverage.*")):
        fail(f"no process saved coverage data in {folder}")
    workers = [found for path in sorted(folder.glob("worker-*.jsonl")) if (found := snapshots(path))]
    if not workers:
        fail(f"no replay worker left a snapshot in {folder}")
    os.chdir(ROOT)  # Paths are relative to the checkout (coveragerc: relative_files).
    with tempfile.TemporaryDirectory() as scratch:
        for n, lines in enumerate(workers):
            coverage.CoverageData(os.path.join(scratch, ".coverage"), suffix=f"worker{n}").add_lines(lines)
        cov = coverage.Coverage(data_file=os.path.join(scratch, ".coverage"),
                                config_file=str(Path(__file__).with_name("coveragerc")))
        cov.combine([str(folder), scratch], keep=True)  # Each copy's paths mapped to the checkout's ([paths]).
        files = sorted(PACKAGE.rglob("*.py"))
        known = {path.relative_to(ROOT).as_posix() for path in files}
        if strays := sorted(set(cov.get_data().measured_files()) - known):
            fail(f"measured outside the checkout's package (coveragerc's [paths]): {', '.join(strays[:5])}")
        covered = statements = 0
        for path in files:
            _, found, _, missing, _ = cov.analysis2(str(path))
            statements += len(found)
            covered += len(found) - len(missing)
    return covered, statements


def js(folder):
    """(covered, lines) over every .js file of the panel."""
    breakable, covered = defaultdict(set), defaultdict(set)
    for path in folder.glob("js-*.json"):
        found = json.loads(path.read_text())
        for name, lines in found["breakable"].items():
            breakable[name].update(lines)
        for name, lines in found["covered"].items():
            covered[name].update(lines)
    www = PACKAGE / "www"
    files = sorted(path.relative_to(www).as_posix() for path in www.rglob("*.js"))
    if missing := [name for name in files if name not in breakable]:
        fail(f"no panel coverage recorded in {folder} for {', '.join(missing)}")
    return sum(len(covered[name] & breakable[name]) for name in files), sum(len(breakable[name]) for name in files)


def main(folder):
    folder = Path(folder).resolve()
    (python_covered, statements), (js_covered, lines) = python(folder / "python"), js(folder / "js")
    (folder / "totals.json").write_text(json.dumps({"python": [python_covered, statements],
                                                    "js": [js_covered, lines]}) + "\n")
    print(f"== coverage: {(python_covered + js_covered) * 100 // (statements + lines)}% "
          f"(Python {python_covered}/{statements} lines, panel {js_covered}/{lines} lines)")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        fail("usage: report.py DIR")
    main(sys.argv[1])
