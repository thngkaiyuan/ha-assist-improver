"""What the replay worker's containment guard (guard.py) costs: real worker processes, with the guard and without it,
each a developer replay of the suite's recorded three-turn conversation, its model the suite's stand-in (a process on
127.0.0.1). Per run: the worker's start (its process started until its sandbox answers), the whole replay, and the
worker's peak memory (RSS). Each run is a fresh pytest process, so the peak is that one worker's; the two variants
alternate, so drift on the machine hits both alike.

Usage, from the checkout: .venv/bin/python tools/measure_guard.py [runs, default 10]. Not shipped.
"""

import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
GUARD = ("sys.dont_write_bytecode = True\n",  # BOOT's lines for the guard.
         "from custom_components.assist_improver.guard import install\ninstall(folder)\n")


def main(runs):
    found = {"without": [], "with": []}
    with tempfile.TemporaryDirectory() as folder:
        for run in range(runs):
            for variant in ("without", "with") if run % 2 == 0 else ("with", "without"):
                out = Path(folder, f"{variant}-{run}.json")
                subprocess.run([sys.executable, "-m", "pytest", "-p", "conftest", "-q", "-p", "no:cacheprovider",
                                f"{__file__}::test_one_replay"], cwd=ROOT, check=True, stdout=subprocess.DEVNULL,
                               env={**os.environ, "PYTHONPATH": str(ROOT / "fixture-test"),
                                    "MEASURE_GUARD": variant, "MEASURE_OUT": str(out)})
                found[variant].append(json.loads(out.read_text()))
                print(variant, found[variant][-1], flush=True)
    print(f"\n{runs} runs each; median [min-max]")
    for key, unit in (("start", "s"), ("replay", "s"), ("peak_rss", "MB")):
        medians = {v: statistics.median(r[key] for r in rows) for v, rows in found.items()}
        spread = {v: f"[{min(r[key] for r in rows):.2f}-{max(r[key] for r in rows):.2f}]" for v, rows in found.items()}
        print(f"{key}: without {medians['without']:.2f} {unit} {spread['without']}, with {medians['with']:.2f} {unit} "
              f"{spread['with']}; added {medians['with'] - medians['without']:+.2f} {unit} "
              f"({(medians['with'] / medians['without'] - 1) * 100:+.0f}%)")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 10)
else:  # Run by pytest, in a process of its own: one replay.
    import resource
    import time

    import pytest

    from kit import improver  # noqa: F401 - a fixture.
    from recordings import before
    from test_worker import provider_process, real_worker  # noqa: F401 - fixtures.
    from custom_components.assist_improver import worker as worker_module
    from custom_components.assist_improver.simulate import replay_recording

    @pytest.mark.asyncio
    async def test_one_replay(improver, provider_process, monkeypatch):
        if os.environ["MEASURE_GUARD"] == "without":  # The worker as it was before the guard.
            boot = worker_module.BOOT
            for line in GUARD:
                assert line in boot
                boot = boot.replace(line, "")
            monkeypatch.setattr(worker_module, "BOOT", boot)
        started, start, call = {}, worker_module.Worker.start.__func__, worker_module.Worker.call

        async def timed_start(cls, hass, providers=None, start_at=None):
            began = time.monotonic()
            worker = await start(cls, hass, providers, start_at)
            started[worker] = began
            return worker

        async def timed_call(worker, op, **args):
            found = await call(worker, op, **args)
            if op == "home.start":
                started[worker] = time.monotonic() - started[worker]
            return found
        monkeypatch.setattr(worker_module.Worker, "start", classmethod(timed_start))
        monkeypatch.setattr(worker_module.Worker, "call", timed_call)
        improver.client = provider_process
        await improver.arrives(improver.friction)
        improver.client.blocks = before(improver.friction)
        began = time.monotonic()
        found = await replay_recording(improver.hass, improver.recorder, "friction01", last_turn=3)
        replay = time.monotonic() - began
        assert found["status"] == "reproduced" and len(started) == 1, found
        peak = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss  # The largest child's: the worker.
        Path(os.environ["MEASURE_OUT"]).write_text(json.dumps({
            "start": round(next(iter(started.values())), 3), "replay": round(replay, 3),
            "peak_rss": round(peak / (1 << 20 if sys.platform == "darwin" else 1 << 10), 1)}))
