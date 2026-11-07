"""tools/check.sh --coverage: each Python process started with this folder on PYTHONPATH measures the improver's code
(../coveragerc) when IMPROVER_COVERAGE names the folder its data goes to and coverage.py is installed (Home Assistant's
.venv has it; Playwright's hasn't, so the browser suites' runners go unmeasured). A process saves its data as it exits,
os._exit too.

A replay worker can't: its guard refuses writes outside its folder, and it's killed, its folder deleted. So before the
guard is installed (worker.BOOT runs after this), it measures in memory and opens a file of its own to append to; each
answer it gives (one write to stdout) first appends the lines run since the last snapshot, as one JSON line. The guard
sees none of it: the database is in memory, and os.write raises no audit event (Landlock judges a file as it opens)."""

import atexit
import os
import sys


def _start(folder):
    try:
        import coverage
    except ImportError:
        return
    rc = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "coveragerc")
    data = os.path.join(folder, "python")
    if sys.argv[0] == "-c" and len(sys.argv) > 1 and os.path.basename(sys.argv[1]).startswith(
            "assist_improver_worker_"):  # worker.BOOT, given its folder.
        _worker(coverage.Coverage(data_file=None, config_file=rc), data)
        return
    cov = coverage.Coverage(data_file=os.path.join(data, ".coverage"), config_file=rc)
    cov.start()
    atexit.register(cov.save)


def _worker(cov, data):
    import io
    import json
    cov.set_option("run:patch", [])  # Coverage's own save at os._exit would be refused: a snapshot instead.
    cov.start()
    os.makedirs(data, exist_ok=True)
    fd = os.open(os.path.join(data, f"worker-{os.getpid()}.jsonl"),
                 os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_CLOEXEC, 0o644)
    sent = {}

    def snapshot():
        found, new = cov.get_data(), {}
        for path in found.measured_files():
            if lines := set(found.lines(path) or ()) - sent.setdefault(path, set()):
                sent[path] |= lines
                new[path] = sorted(lines)
        if new:
            os.write(fd, (json.dumps(new) + "\n").encode())

    class Answers(io.BufferedWriter):
        def write(self, b):
            snapshot()
            return super().write(b)

    def _exit(code):
        snapshot()
        exit_(code)

    out, exit_ = sys.stdout, os._exit
    sys.stdout = io.TextIOWrapper(Answers(io.FileIO(out.fileno(), "w", closefd=False)), encoding=out.encoding,
                                  errors=out.errors, line_buffering=out.line_buffering)
    os._exit = _exit
    atexit.register(snapshot)
    snapshot()  # What a snapshot needs (its database among it) made before the guard.


if os.environ.get("IMPROVER_COVERAGE"):
    _start(os.environ["IMPROVER_COVERAGE"])
