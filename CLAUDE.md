# Assist Improver

A Home Assistant custom integration (domain `assist_improver`) that records a household's Assist conversations and
proposes changes that get the same result in fewer turns. `SPEC.md` is the one spec (what the product does): the
maintainer's model. Where it doesn't settle a choice, its Principles do. A change to it comes by pull request,
its exact words shown, and the maintainer approves it.
Work flows from it to the tests that hold it (fixture-test/graph, run with `--graph`), then the code; `DETAILS.md` is
detail for code and tests to settle. `ROADMAP.md` says where we are. Keep process lean.

## Layout and commands
- Code: `custom_components/assist_improver/` (shipped). Scorers for the AI roles: `evaluation/` (not shipped). Offline
  tests: `fixture-test/`. Design principles: `docs/DESIGN.md`.
- Set-up (uv, the two venvs, Playwright's browsers): `CONTRIBUTING.md`. Use uv's command-line Python, not Homebrew's:
  a crash in Homebrew's Python.app pops a "Python quit unexpectedly" dialog.
- Tests (offline; network, DNS and subprocess are blocked): `.venv/bin/python -m pytest fixture-test -q`.
- End-to-end journeys (a throwaway HA through its real frontend, only the model stood in; `tools/e2e/README.md`):
  `tools/e2e/.venv/bin/python -m pytest tools/e2e -q -p no:cacheprovider`.
- Panel (`www/` in a real browser, canned replies): `PLAYWRIGHT_BROWSERS_PATH=tools/ui/browsers tools/e2e/.venv/bin/python
  -m pytest tools/ui/test_panel.py -q -p no:cacheprovider` (from a worktree, the main checkout's browsers).
- The full gate: `tools/check.sh` runs all four suites side by side, each on pytest-xdist workers (about 3 minutes;
  one at a time it was 35). It prints each suite's result line and passes only if all pass on a clean, unmoved commit.
  Any suite alone takes `-n <workers>` too. No suite calls a paid model: stand-ins answer.
- Linux: `tools/bench/run.sh [stable|beta|dev] [--landlock applied|missing]` runs `tools/check.sh` against HA's official
  image, `tools/bench/haos.sh` the journeys on HA OS (`tools/bench/README.md`). CI (`.github/workflows`) runs both
  on each push and pull request; in a private repository (GitHub bills there), every job skips.
- Package: `python build_package.py` writes `dist/`. Install steps: `INSTALL.md`.
- Maintainer tools (`tools/deploy.sh`, `preflight.py`, `ui/ui_check.py`, the triage tools) read the household's data
  from the private folder outside the checkout (`tools/private.py`). Tests on it skip without it. `tools/deploy.sh`
  ships only a commit whose newest Stable check on GitHub passed (`tools/passed.py`).
- No household detail goes in git: shipped code names no one, and tests and docs use made-up homes and people. The
  leak guard (`tools/leak_guard.py`) checks each commit pushed to the public repository, and CI checks again.
