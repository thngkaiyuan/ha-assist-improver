#!/bin/bash
# The full test gate: the four suites (offline, panel, end-to-end, graph) side by side on this checkout, each on its
# own pytest-xdist workers. Prints the commit and whether the tree is clean before and after, each suite's result
# line (with its failed tests), and a verdict. Exit 0 only when every suite passed on a clean tree whose commit didn't
# move; 1 otherwise.
# Usage: tools/check.sh. CHECK_LOGS=<folder> keeps the logs (and a failing journey's Home Assistant folder) there;
# a new temp folder otherwise, kept and printed when something fails. On the bench (tools/bench/run.sh sets
# BENCH_IMAGE), the offline and graph suites run in Home Assistant's image, and each journey's Home Assistant too.
set -u
cd "$(dirname "$0")/.."
ROOT=$PWD

find_up() {  # A path in this checkout, or in the checkout a worktree hangs from (venvs and browsers aren't copied).
  local base=$ROOT
  while [ "$base" != / ]; do
    [ -e "$base/$1" ] && { echo "$base/$1"; return; }
    base=$(dirname "$base")
  done
  echo "check.sh: no $1 in $ROOT or above it (see CONTRIBUTING.md)" >&2
  exit 1
}
IMAGE=${BENCH_IMAGE:-}
PY=
[ -n "$IMAGE" ] || PY=$(find_up .venv/bin/python) || exit 1  # Home Assistant's: the offline and graph suites.
RUNNER=$(find_up tools/e2e/.venv/bin/python) || exit 1      # Playwright's: the panel and end-to-end suites.
BROWSERS=${PLAYWRIGHT_BROWSERS_PATH:-$(find_up tools/ui/browsers)} || exit 1
for python in $PY "$RUNNER"; do
  "$python" -c "import xdist" 2>/dev/null || {
    echo "check.sh: $python has no pytest-xdist: install its requirements again (CONTRIBUTING.md)"
    exit 1
  }
done
LOGS=${CHECK_LOGS:-$(mktemp -d "${TMPDIR:-/tmp}/assist-improver-check.XXXXXX")}
mkdir -p "$LOGS/e2e-runs"

tree() {  # The commit, and what differs from it (untracked files too: a stray test file would be collected).
  local changed
  changed=$(git status --porcelain | wc -l | tr -d ' ')
  echo "$(git rev-parse --short HEAD), $([ "$changed" = 0 ] && echo clean || echo "$changed changed files")"
}
before=$(tree)
echo "== tree $before"
start=$SECONDS

# Workers per suite, by the machine's processors: 21 on 16 (the journeys mostly wait on their Home Assistants), 5 on
# a 4-processor CI runner. The journeys take longest, about 3 minutes on 16; the others finish within 2.
CPUS=$(getconf _NPROCESSORS_ONLN)
workers() { local n=$(($1 * CPUS / 16)); echo $((n < ${2:-1} ? ${2:-1} : n)); }
SUITES=(offline panel e2e graph)
pytest_ha() {  # pytest on Home Assistant's Python: the checkout's .venv, or on the bench the image's (tools/bench).
  if [ -z "$IMAGE" ]; then exec "$PY" -m pytest "${@:2}"; fi
  exec tools/bench/ha.sh run "assist-improver-$1-$$" --cpus 8 --memory 8G -e "BENCH_LANDLOCK=${BENCH_LANDLOCK:-}" \
    -- sh tools/bench/tests.sh "${@:2}"
}
run() {
  case $1 in
    offline) pytest_ha offline fixture-test -q -p no:cacheprovider -n "$(workers 4)" ;;
    panel) PLAYWRIGHT_BROWSERS_PATH=$BROWSERS \
      exec "$RUNNER" -m pytest tools/ui/test_panel.py -q -p no:cacheprovider -n "$(workers 3)" ;;
    e2e) E2E_RUNS_DIR=$LOGS/e2e-runs exec "$RUNNER" -m pytest tools/e2e -q -p no:cacheprovider -n "$(workers 8 2)" ;;
    graph) pytest_ha graph fixture-test/graph --graph -q -p no:cacheprovider -n "$(workers 6)" ;;
  esac
}

# Each suite in a process group of its own (job control). Ctrl-C, or the script being stopped, stops each suite with
# SIGTERM (an interrupt can arrive ignored: a script started in the background ignores it), which the journeys take
# as an interrupt, stopping the Home Assistants they started.
set -m
pids=()
trap 'for pid in ${pids[@]+"${pids[@]}"}; do kill -TERM -- "-$pid" 2>/dev/null; done' INT TERM HUP
for suite in "${SUITES[@]}"; do
  run "$suite" > "$LOGS/$suite.log" 2>&1 &
  pids+=($!)
done

failed=()
for i in "${!SUITES[@]}"; do
  suite=${SUITES[$i]}
  while true; do  # A trapped signal ends a wait early (status over 128): wait again until the suite has stopped.
    wait "${pids[$i]}"; status=$?
    [ "$status" -gt 128 ] && kill -0 "${pids[$i]}" 2>/dev/null || break
  done
  echo "== $suite: $(tail -1 "$LOGS/$suite.log")"
  grep -E '^(FAILED|ERROR) ' "$LOGS/$suite.log" | sed 's/^/   /'
  [ "$status" = 0 ] || failed+=("$suite")
done
trap - INT TERM HUP

after=$(tree)
echo "== tree after $after"
took="$((SECONDS - start)) s"
if [ ${#failed[@]} -gt 0 ]; then
  echo "== FAILED: ${failed[*]} ($took; logs in $LOGS)"
elif [ "$before" != "$after" ] || [ "${before#*, }" != clean ]; then
  echo "== suites passed, but not on one clean commit ($took; logs in $LOGS): not a pass for ${before%%,*}"
else
  echo "== passed on ${before%%,*} in $took"
  [ -n "${CHECK_LOGS:-}" ] || rm -rf "$LOGS"
  exit 0
fi
exit 1
