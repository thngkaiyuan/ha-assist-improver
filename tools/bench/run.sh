#!/bin/bash
# The Linux bench: every suite against Home Assistant's official image (README.md here), with tools/check.sh's verdict,
# on Docker (a GitHub runner) or Apple's container (a Mac).
# Usage: tools/bench/run.sh [stable|beta|dev|VERSION] [--landlock applied|missing]
# BENCH_RUNTIME=docker|container picks the runtime (Docker when it answers).
set -u
cd "$(dirname "$0")/../.."
channel=stable; landlock=applied
while [ $# -gt 0 ]; do
  case $1 in
    --landlock) landlock=$2; shift 2 ;;
    -*) sed -n '4p' "$0"; exit 2 ;;
    *) channel=$1; shift ;;
  esac
done
case $landlock in applied|missing) ;; *) echo "run.sh: --landlock applied or missing" >&2; exit 2 ;; esac
runtime=${BENCH_RUNTIME:-$(docker info > /dev/null 2>&1 && echo docker || echo container)}
case $(uname -m) in arm64|aarch64) arch=aarch64 ;; x86_64|amd64) arch=amd64 ;; *) arch=$(uname -m) ;; esac
start=$SECONDS

# The image, by digest: the channel's tag may move during the run.
image=ghcr.io/home-assistant/home-assistant:$channel
case $runtime in
  docker) docker pull -q "$image" > /dev/null || exit 1
    ref=$(docker image inspect --format '{{index .RepoDigests 0}}' "$image") ;;
  container) container image pull --progress none "$image" > /dev/null || exit 1
    digest=$(container image inspect "$image" | sed -n 's/.*"digest" : "\(sha256:[0-9a-f]*\)".*/\1/p' | head -1)
    ref=${image%:*}@$digest ;;
  *) echo "run.sh: BENCH_RUNTIME is docker or container" >&2; exit 2 ;;
esac
export BENCH_IMAGE=$ref BENCH_RUNTIME=$runtime BENCH_LANDLOCK=$landlock BENCH_HOST=127.0.0.1
pulled=$((SECONDS - start))

# What the image runs on, and what the guard's Landlock does there.
probe=$(tools/bench/ha.sh run "assist-improver-probe-$$" -- python3 -c '
import importlib.util, platform, sys, tempfile
import homeassistant.const as ha
spec = importlib.util.spec_from_file_location("guard", "custom_components/assist_improver/guard.py")
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)
print(f"Home Assistant {ha.__version__}, Python {platform.python_version()}, Linux {platform.release()} "
      f"{platform.machine()}: {guard.landlock(tempfile.mkdtemp())}")' 2> /dev/null | tail -1)
echo "== $ref ($runtime, $arch, Landlock $landlock; pulled in $pulled s)"
echo "== $probe"

anchor=
if [ "$runtime" = container ]; then
  # Each container is a VM on the container network; the journeys' fake model listens on this Mac's address there,
  # which exists while a container runs: one runs for the whole run.
  anchor=assist-improver-anchor-$$
  container run -d --rm --progress none --name "$anchor" --entrypoint sleep "$ref" 86400 > /dev/null || exit 1
  trap 'container stop -t 1 "$anchor" > /dev/null 2>&1' EXIT
  BENCH_HOST=$(container network inspect default | sed -n 's/.*"ipv4Gateway" : "\(.*\)".*/\1/p')
fi

tools/check.sh
status=$?
echo "== bench: $((SECONDS - start)) s"
exit "$status"
