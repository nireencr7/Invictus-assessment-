#!/bin/sh
# Writes the console's runtime config from the environment, so one image runs everywhere.
#   CONSOLE_MODE      live | snapshot | auto   (auto: use the API if it answers, else the demo snapshot)
#   CONSOLE_API_BASE  empty = same origin via this nginx's /v1 proxy (recommended)
set -eu
case "${CONSOLE_MODE:-auto}" in live|snapshot|auto) ;; *) echo "CONSOLE_MODE must be live, snapshot or auto" >&2; exit 1 ;; esac
cat > /usr/share/nginx/html/config.json <<JSON
{"mode": "${CONSOLE_MODE:-auto}", "apiBase": "${CONSOLE_API_BASE:-}", "snapshotUrl": "demo-snapshot.json"}
JSON
