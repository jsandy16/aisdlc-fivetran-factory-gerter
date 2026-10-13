#!/usr/bin/env bash
# (Re)start the dry-run stand-ins in a fresh state folder: dryrun/up.sh <state_dir>
set -euo pipefail
STATE="$1"
if [ -f "$STATE/env.json" ]; then
  kill "$(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['pid'])" "$STATE/env.json")" 2>/dev/null || true
  sleep 3
fi
rm -rf "$STATE"
nohup "$(dirname "$0")/../.venv/bin/python" "$(dirname "$0")/servers.py" "$STATE" > "$STATE.log" 2>&1 &
for _ in $(seq 1 40); do [ -f "$STATE/env.json" ] && exit 0; sleep 0.5; done
echo "servers did not start; see $STATE.log" >&2; exit 1
