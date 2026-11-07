#!/bin/bash
# Home Assistant's official image, run as the Supervisor runs Core, as far as the runtime allows (README.md here).
# Usage:
#   tools/bench/ha.sh run NAME [--config DIR] [--port PORT] [--cpus N] [--memory SIZE] [-e NAME=VALUE]...
#                              [-- COMMAND...]
#       In the foreground; removed once it stops. Without COMMAND the image starts as under the Supervisor: s6's
#       /init, which runs `python3 -P -m homeassistant --config /config` from /config, DIR mounted there. With
#       COMMAND, that instead, from this checkout. The checkout is mounted read-only at its own path either way.
#   tools/bench/ha.sh stop NAME
# Environment: BENCH_IMAGE (the image, by digest), BENCH_RUNTIME (docker or container), BENCH_LANDLOCK (applied, or
# missing: Landlock's calls answer ENOSYS, as on HA OS's kernels, through enosys.py).
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
: "${BENCH_IMAGE:?}" "${BENCH_RUNTIME:?}" "${BENCH_LANDLOCK:=applied}"
action=${1:?usage: see the top of $0}; name=${2:?a name}; shift 2
if [ "$action" = stop ]; then
  exec "$BENCH_RUNTIME" stop -t 45 "$name" > /dev/null
fi
[ "$action" = run ] || { echo "ha.sh: run or stop, not $action" >&2; exit 2; }

config=; port=; cpus=4; memory=4G; env=(-e HA_DUPLICATE_LOG_FILE=1); zone=UTC
while [ $# -gt 0 ]; do
  case $1 in
    --config) config=$2; shift 2 ;;
    --port) port=$2; shift 2 ;;
    --cpus) cpus=$2; shift 2 ;;
    --memory) memory=$2; shift 2 ;;
    -e) case $2 in TZ=*) zone=${2#TZ=} ;; *) env+=(-e "$2") ;; esac; shift 2 ;;
    --) shift; break ;;
    *) echo "ha.sh: unknown $1" >&2; exit 2 ;;
  esac
done

# The checkout, and the one a worktree hangs from (its git folder is there). The private folder isn't mounted: tests on
# household data skip here (tools/private.py).
main=$(cd "$(git -C "$ROOT" rev-parse --path-format=absolute --git-common-dir)/.." && pwd)
mounts=(-v "$main:$main:ro")
case $ROOT/ in "$main"/*) ;; *) mounts+=(-v "$ROOT:$ROOT:ro") ;; esac
[ -z "$config" ] || mounts+=(-v "$config:/config")

case $BENCH_RUNTIME in
  # The Supervisor's settings for Core: privileged, no seccomp or AppArmor profile, the host's network.
  docker) flags=(--privileged --security-opt seccomp=unconfined --security-opt apparmor=unconfined --network host) ;;
  # Apple's container: each container a small VM of its own, so no host network (a port is published on 127.0.0.1
  # instead) and no --privileged (every capability instead); its kernel has no seccomp profile to lift.
  container) flags=(--cap-add ALL --cpus "$cpus" --memory "$memory" --progress none)
    [ -z "$port" ] || flags+=(-p "127.0.0.1:$port:$port") ;;
  *) echo "ha.sh: BENCH_RUNTIME is docker or container, not $BENCH_RUNTIME" >&2; exit 2 ;;
esac

if [ $# -eq 0 ]; then
  command=(/init); workdir=/config
else
  command=("$@"); workdir=$ROOT
fi
case $BENCH_LANDLOCK in
  applied) ;;
  missing) command=(python3 "$ROOT/tools/bench/enosys.py" "${command[@]}") ;;
  *) echo "ha.sh: BENCH_LANDLOCK is applied or missing, not $BENCH_LANDLOCK" >&2; exit 2 ;;
esac
# /tmp a tmpfs, as the Supervisor asks Docker for it, with Docker's options for one (container's has none of its own).
exec "$BENCH_RUNTIME" run --rm --name "$name" "${flags[@]}" --tmpfs /tmp:rw,noexec,nosuid,nodev "${mounts[@]}" \
  "${env[@]}" -e "TZ=$zone" -w "$workdir" --entrypoint "${command[0]}" "$BENCH_IMAGE" "${command[@]:1}"
