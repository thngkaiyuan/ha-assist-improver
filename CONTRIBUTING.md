# Contributing

## Set up

With [uv](https://docs.astral.sh/uv/) and its Python, about 1.7 GB in all (the last line downloads Chrome for Testing
from Playwright's servers):

```sh
uv venv .venv --python 3.14 --managed-python                # Home Assistant, for the offline tests
uv pip install --python .venv/bin/python -r fixture-test/requirements-lock.txt
uv venv tools/e2e/.venv --python 3.14 --managed-python      # Playwright, for the journeys and the panel
uv pip install --python tools/e2e/.venv/bin/python -r tools/e2e/requirements.txt
PLAYWRIGHT_BROWSERS_PATH=tools/ui/browsers tools/e2e/.venv/bin/python -m playwright install chromium
```

On Linux, add `--with-deps` to the last line.

## Test

`tools/check.sh` runs the four suites side by side and passes only if all pass on a clean commit: about 3 to 5 minutes
on an otherwise idle 16-processor machine. On a busy one, journeys can time out waiting for a run ("Timed out after
60 s"): run it again when it's quiet. Any file neither committed nor ignored, even a stray log, keeps it from passing.
Its logs go to a temporary folder (`CHECK_LOGS=<folder>` picks one), kept and printed when something fails.
`tools/check.sh --coverage DIR` (an empty folder outside the checkout) also prints the share of the integration's lines
the suites run: its Python in every process it runs in, and the panel's JavaScript.

Each suite alone (`-rs` prints why a test skipped):

- offline: `.venv/bin/python -m pytest fixture-test -q -n 4`
- graph, the tests that hold `SPEC.md`: `.venv/bin/python -m pytest fixture-test/graph --graph -q -n 4`
- journeys, a real Home Assistant through its frontend (`tools/e2e/README.md`), about 5 minutes:
  `tools/e2e/.venv/bin/python -m pytest tools/e2e -q -p no:cacheprovider -n 8`
- panel: `PLAYWRIGHT_BROWSERS_PATH=tools/ui/browsers tools/e2e/.venv/bin/python -m pytest tools/ui/test_panel.py -q
  -p no:cacheprovider -n 4`

All pass. No suite calls a paid model. Only tests on the maintainer's household data skip, saying why.

`tools/bench/run.sh` runs the same against Home Assistant's official image on Docker, and `tools/bench/haos.sh` the
journeys on HA OS (`tools/bench/README.md`). CI runs both on every pull request.

## Pull requests

Fork, branch, commit, and open a pull request against `main`. Keep each change small, with tests that hold it, and
`tools/check.sh` green. Use made-up homes and people in tests and docs.

`SPEC.md` says what the product does. A change to it comes by pull request, as its own, and the maintainer approves
it before anything that follows from it.
