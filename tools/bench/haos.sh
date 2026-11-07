#!/bin/bash
# The journeys against HA OS: its current release for the channel, booted in QEMU/KVM as HA OS's own CI does, set up
# by its Supervisor with the given Home Assistant (the channel's by default), then each journey's Home Assistant is
# HA OS's own (README.md).
# Usage: tools/bench/haos.sh [stable|beta|dev] [VERSION]
# Needs an x64 Linux machine with KVM, qemu-system-x86, OVMF, curl, jq and e2fsprogs (a GitHub runner), and
# tools/e2e/.venv with Playwright's Chromium in tools/ui/browsers. BENCH_WORK: where the VM and logs go (a temp folder).
set -euo pipefail
cd "$(dirname "$0")/../.."
channel=${1:-stable}
work=${BENCH_WORK:-$(mktemp -d)}
mkdir -p "$work"
start=$SECONDS

# The channel's versions: HA OS's (its generic x86-64 VM image), the Supervisor's and Home Assistant's.
versions=$(curl -fsSL -A assist-improver-bench "https://version.home-assistant.io/$channel.json")
os=$(jq -r .hassos.ova <<< "$versions")
supervisor=$(jq -r .supervisor <<< "$versions")
core=${2:-$(jq -r '.homeassistant["qemux86-64"]' <<< "$versions")}
curl -fsSL "https://os-artifacts.home-assistant.io/$os/haos_ova-$os.qcow2.xz" | xz -dc > "$work/haos.qcow2"

# HA OS's debug access: at boot it imports authorized_keys from a drive labeled CONFIG and starts SSH on 22222.
rm -f "$work/key" "$work/key.pub"
ssh-keygen -q -t ed25519 -N '' -f "$work/key"
mkdir -p "$work/config"
cp "$work/key.pub" "$work/config/authorized_keys"
rm -f "$work/config.img"
truncate -s 16M "$work/config.img"
mkfs.ext4 -q -L CONFIG -d "$work/config" "$work/config.img"
cat > "$work/ssh_config" << EOF
Host haos
  HostName 127.0.0.1
  Port 22222
  User root
  IdentityFile $work/key
  StrictHostKeyChecking no
  UserKnownHostsFile /dev/null
  LogLevel ERROR
  ConnectTimeout 10
EOF
export BENCH_HAOS=$work/ssh_config
on() { ssh -F "$BENCH_HAOS" haos "$@"; }

# Booted with UEFI, its web port and SSH forwarded from 127.0.0.1 only; it reaches this machine at 10.0.2.2.
cp /usr/share/OVMF/OVMF_VARS_4M.fd "$work/vars.fd"
qemu-system-x86_64 -accel kvm -cpu host -machine q35 -smp 4 -m 8G \
  -drive if=pflash,format=raw,readonly=on,file=/usr/share/OVMF/OVMF_CODE_4M.fd \
  -drive if=pflash,format=raw,file="$work/vars.fd" \
  -drive file="$work/haos.qcow2",if=virtio -drive file="$work/config.img",if=virtio,format=raw \
  -netdev user,id=net,hostfwd=tcp:127.0.0.1:8123-:8123,hostfwd=tcp:127.0.0.1:22222-:22222 \
  -device virtio-net-pci,netdev=net -display none -serial file:"$work/serial.log" \
  -daemonize -pidfile "$work/qemu.pid"
trap 'kill "$(cat "$work/qemu.pid")" 2> /dev/null || true' EXIT

wait_for() {  # wait_for SECONDS WHAT COMMAND...: until COMMAND succeeds.
  local deadline=$((SECONDS + $1)) what=$2
  shift 2
  until "$@" > /dev/null 2>&1; do
    [ $SECONDS -lt $deadline ] || { echo "haos.sh: timed out waiting for $what" >&2; exit 1; }
    sleep 5
  done
}
installed() { [ "$(on "ha $1 info --raw-json" | jq -r .data.version)" = "$2" ]; }
known() {  # Whether the Supervisor's latest is the channel's Supervisor, once it has read its channel's versions again.
  on "ha supervisor reload" && [ "$(on "ha supervisor info --raw-json" | jq -r .data.version_latest)" = "$supervisor" ]
}
core() {  # core ARGS...: `ha core ARGS`. While the Supervisor runs a job on Home Assistant (its install, which ends by
  # starting it, or an update), it refuses another: tried again every 5 s, for up to 15 minutes.
  local deadline=$((SECONDS + 900)) out
  until out=$(on "ha core $*" 2>&1); do
    [[ $out == *"Another job is running"* && $SECONDS -lt $deadline ]] || { echo "$out" >&2; return 1; }
    sleep 5
  done
}
wait_for 600 "SSH" on true
wait_for 900 "the Supervisor" on "ha supervisor info"

# The channel's Supervisor, once the Supervisor has read the channel's versions (it updates to the latest it knows,
# whatever version is asked for), then the Home Assistant asked for (the landing page first, while the Supervisor
# installs the channel's; an older release replaces it).
on "ha supervisor options --channel $channel" > /dev/null
wait_for 300 "the $channel channel's versions" known
installed supervisor "$supervisor" || on "ha supervisor update" > /dev/null || true
wait_for 900 "Supervisor $supervisor" installed supervisor "$supervisor"
wait_for 1800 "Home Assistant to be installed" sh -c \
  "ssh -F '$BENCH_HAOS' haos 'ha core info --raw-json' | jq -e '.data.version != \"landingpage\"'"
installed core "$core" || core update --version "$core" || true
wait_for 1800 "Home Assistant $core" installed core "$core"
booted=$((SECONDS - start))
echo "== HA OS $(on "ha os info --raw-json" | jq -r .data.version) (Linux $(on "uname -r")), Supervisor" \
  "$(on "ha supervisor info --raw-json" | jq -r .data.version), Home Assistant $core ($channel; ready in $booted s)"
core stop  # Each journey starts it on a config of its own (tools/e2e/conftest.py).

# The journeys, one Home Assistant at a time, at phone size (the screen's sizes are the image runs' to cover); then
# what Home Assistant logged: the replay guard's Landlock line, and no error or blocking call of Assist Improver's.
runs=$work/e2e-runs
mkdir -p "$runs"
runner=
for base in "$PWD" "$(git rev-parse --path-format=absolute --git-common-dir)/.."; do
  [ -x "$base/tools/e2e/.venv/bin/python" ] && { runner=$base/tools/e2e/.venv/bin/python; break; }
done
[ -n "$runner" ] || { echo "haos.sh: no tools/e2e/.venv (tools/e2e/README.md)" >&2; exit 1; }
status=0
E2E_KEEP=1 E2E_RUNS_DIR=$runs "$runner" -m pytest tools/e2e -q -p no:cacheprovider -k "not desktop" || status=$?
"$runner" tools/bench/logcheck.py "Landlock skipped: Function not implemented" "$runs"/*/*/e2e-hass.log || status=1
echo "== HA OS bench: $((SECONDS - start)) s"
exit "$status"
