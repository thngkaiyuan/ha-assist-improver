#!/bin/bash
# Build the package and install it over SSH on the Home Assistant that the private folder's home-assistant.env names
# (HA_SSH, HA_SSH_KEY; tools/private.py). No copy of the old version is kept (Kai: prefer no backups; every version is
# in git).
# Usage: tools/deploy.sh [--dry-run] [--restart] [--skip-preflight] | tools/deploy.sh --verify
# Never touches .storage records.
# Only a build that passed its tests ships: before anything reaches HA, tools/passed.py checks that the commit's shipped
# tree has a pass (GitHub's newest finished Stable check for the commit) and that the working copy ships exactly that
# tree.
# First, tools/preflight.py runs this checkout's code on a read-only copy of the improver's stored data (no model);
# a failure stops the install. --skip-preflight is for emergencies only.
# After --restart, tools/ha_verify.sh checks on HA that the new version set up with no errors; a failure exits 1.
# --verify runs only that check, for HA's last start.
set -euo pipefail
cd "$(dirname "$0")/.."
DRY=0; RESTART=0; PREFLIGHT=1; VERIFY=0
for arg in "$@"; do case $arg in --dry-run) DRY=1;; --restart) RESTART=1;; --skip-preflight) PREFLIGHT=0;;
  --verify) VERIFY=1;; *) echo "unknown $arg"; exit 2;; esac; done
PY=.venv/bin/python  # uv's Python (CONTRIBUTING.md), never Homebrew's
ENV=$($PY tools/private.py home-assistant.env) || exit 1
. "$ENV"
for name in HA_SSH HA_SSH_KEY; do [ -n "${!name:-}" ] || { echo "No $name in $ENV (tools/private.py)."; exit 1; }; done
SSH=(ssh -i "$HA_SSH_KEY" -o BatchMode=yes -o IdentitiesOnly=yes "$HA_SSH")
[ $VERIFY = 1 ] && exec "${SSH[@]}" 'bash -s' < tools/ha_verify.sh

$PY tools/passed.py || { echo "Not installed: this build hasn't passed its tests"; exit 1; }
NEW=$($PY -c 'import json;print(json.load(open("custom_components/assist_improver/manifest.json"))["version"])')
OLD=$("${SSH[@]}" 'jq -r .version /config/custom_components/assist_improver/manifest.json')
[ "$OLD" = "$NEW" ] && { echo "HA already runs $NEW; bump the version first"; exit 1; }
if [ $PREFLIGHT = 1 ]; then
  $PY tools/preflight.py || { echo "Preflight failed: nothing installed (--skip-preflight for emergencies)"; exit 1; }
else
  echo "Preflight skipped (--skip-preflight)"
fi
$PY build_package.py
ZIP=dist/assist-improver-$NEW.zip
WORK=$(mktemp -d); trap 'rm -rf "$WORK"' EXIT
unzip -q "$ZIP" -d "$WORK/unz"
STAGE=/config/.assist-improver-deploy/$NEW-stage
OLDDIR=/config/.assist-improver-deploy/$OLD-replaced
(cd "$WORK/unz" && find custom_components -type f | sort | xargs shasum -a 256) > "$WORK/files"
{
  echo 'set -eu'; echo 'umask 077'
  echo "echo '$(shasum -a 256 "$ZIP" | cut -d' ' -f1)  /tmp/assist-improver-$NEW.zip' | sha256sum -c -s -"
  echo "test ! -e $STAGE && test ! -e $OLDDIR"
  echo "mkdir -p $STAGE"
  echo "unzip -q /tmp/assist-improver-$NEW.zip 'custom_components/*' -d $STAGE"
  echo "sha256sum -c -s - <<'S'"; sed "s#  custom_components#  $STAGE/custom_components#" "$WORK/files"; echo S
  echo "mv /config/custom_components/assist_improver $OLDDIR"
  echo "mv $STAGE/custom_components/assist_improver /config/custom_components/assist_improver"
  echo "rmdir $STAGE/custom_components $STAGE"
  echo "rm -rf $OLDDIR"  # The replaced version's files: an update replaces them.
  echo "rm /tmp/assist-improver-$NEW.zip"
  echo "sha256sum -c -s - <<'I'"; sed "s#  custom_components#  /config/custom_components#" "$WORK/files"; echo I
  echo "echo installed \$(jq -r .version /config/custom_components/assist_improver/manifest.json)"
  echo "ha core check"
} > "$WORK/install.sh"
if [ $DRY = 1 ]; then grep -v '^[0-9a-f]\{64\}' "$WORK/install.sh"; echo "(dry run: $OLD -> $NEW, nothing sent)"; exit 0; fi
scp -q -i "$HA_SSH_KEY" -o BatchMode=yes -o IdentitiesOnly=yes "$ZIP" "$HA_SSH:/tmp/"
"${SSH[@]}" 'bash -s' < "$WORK/install.sh"
if [ $RESTART = 1 ]; then
  SINCE=$("${SSH[@]}" 'date +%s')
  "${SSH[@]}" 'ha core restart && ha core info --raw-json | jq -c "{version:.data.version, boot:.data.boot}"'
  "${SSH[@]}" "bash -s -- $SINCE $NEW" < tools/ha_verify.sh
else
  echo "Installed $NEW; it loads at Home Assistant's next restart (then: tools/deploy.sh --verify)"
fi
