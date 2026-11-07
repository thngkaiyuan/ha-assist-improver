#!/bin/bash
# Runs on Home Assistant, sent over SSH by tools/deploy.sh: did Assist Improver come up cleanly after Home Assistant's
# last start? It checks that the improver set up (its setup ends with the journal's "Tidy-up done"), and that
# nothing from it was logged at error level since: not in Home Assistant's log (setup failures and tracebacks
# included), not in the improver's own journal. Exit 1 on either, printing what was found.
# Usage: ssh ... 'bash -s -- [SINCE] [VERSION]' < tools/ha_verify.sh
#   SINCE: epoch seconds of the restart deploy.sh just asked for; it waits up to 5 minutes for the setup.
#          Empty: Home Assistant's last start, as its log shows the improver loading.
#   VERSION: the version that should be installed.
set -eu
S=/config/.storage
MANIFEST=/config/custom_components/assist_improver/manifest.json
logs() { ha core logs -n 100000 2>/dev/null | sed 's/\x1b\[[0-9;]*m//g'; }
installed=$(jq -r .version "$MANIFEST")
[ -z "${2:-}" ] || [ "$installed" = "$2" ] || { echo "FAIL: HA has $installed installed, not $2"; exit 1; }
since=${1:-}
if [ -z "$since" ]; then
  loaded=$(logs | grep 'We found a custom integration assist_improver ' | tail -1 | cut -c1-19)
  [ -n "$loaded" ] || { echo "FAIL: Home Assistant's log doesn't show the improver loading"; exit 1; }
  since=$(date -d "$loaded" +%s)
fi
local_since=$(date -d "@$since" '+%Y-%m-%d %H:%M:%S')  # Home Assistant's log is in local time,
utc_since=$(date -u -d "@$since" '+%Y-%m-%dT%H:%M:%S')  # the journal in UTC.
journal() { cat $(ls -t $S/assist_improver.trace_day_* | head -2) | jq -c --arg t "$utc_since" ".data.journal[] | select(.at >= \$t) | $1"; }
set_up=""
for _ in $(seq 60); do
  set_up=$(journal 'select(.message == "Tidy-up done") | .at' | tr -d '"' | sort | head -1)
  [ -n "$set_up" ] && break
  [ -n "${1:-}" ] || break  # The last start: it has set up by now or it won't.
  sleep 5
done
[ -n "$set_up" ] || { echo "FAIL: Assist Improver $installed didn't set up after $local_since (no \"Tidy-up done\" in its journal)"; exit 1; }
# Every error-level record (with its traceback lines) since then that mentions the improver.
errors=$(logs | awk -v t="$local_since" '
  function flush() { if (err && rec ~ /assist_improver/) print rec; rec = ""; err = 0 }
  /^[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:/ {
    if (substr($0, 1, 19) >= t) on = 1
    if (on) { flush(); rec = $0; err = ($3 == "ERROR" || $3 == "CRITICAL") }
    next }
  on { rec = rec "\n" $0 }
  END { flush() }')
journal_errors=$(journal 'select(.level == "error" or .level == "critical") | {at, message}')
if [ -n "$errors$journal_errors" ]; then
  echo "FAIL: Assist Improver $installed logged errors since $local_since:"
  [ -z "$errors" ] || echo "$errors" | head -60
  [ -z "$journal_errors" ] || echo "$journal_errors" | head -20
  exit 1
fi
echo "OK: Assist Improver $installed set up at $set_up (UTC), with no errors since $local_since"
