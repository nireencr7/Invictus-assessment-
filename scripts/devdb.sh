#!/usr/bin/env bash
# Starts a throwaway local Postgres for development, without Docker, if Postgres binaries
# are installed. Data lives in ./.devdb (git-ignored). Usage: scripts/devdb.sh start|stop|status
set -euo pipefail
cd "$(dirname "$0")/.."
PORT="${DEVDB_PORT:-5433}"
DIR="$PWD/.devdb"
BIN="${PG_BIN:-}"
if [ -z "$BIN" ]; then
  for d in $(ls -d /usr/lib/postgresql/*/bin 2>/dev/null | sort -r) /opt/homebrew/bin /usr/local/bin; do
    if [ -x "$d/initdb" ]; then BIN="$d"; break; fi
  done
fi
[ -z "$BIN" ] && command -v initdb >/dev/null && BIN="$(dirname "$(command -v initdb)")"
[ -z "$BIN" ] && { echo "No Postgres binaries found. Use 'make up' (Docker) instead." >&2; exit 1; }

case "${1:-start}" in
  start)
    if [ ! -d "$DIR/data" ]; then
      mkdir -p "$DIR"
      "$BIN/initdb" -D "$DIR/data" -U postgres --auth=trust -E UTF8 --no-instructions >/dev/null
    fi
    if "$BIN/pg_ctl" -D "$DIR/data" status >/dev/null 2>&1; then echo "devdb already running on :$PORT"; exit 0; fi
    "$BIN/pg_ctl" -D "$DIR/data" -l "$DIR/log" -w \
      -o "-p $PORT -k $DIR -c listen_addresses=127.0.0.1" start >/dev/null
    psql -h 127.0.0.1 -p "$PORT" -U postgres -tc "SELECT 1 FROM pg_database WHERE datname='riverbend'" | grep -q 1 \
      || psql -h 127.0.0.1 -p "$PORT" -U postgres -c "CREATE DATABASE riverbend" >/dev/null
    echo "devdb running: postgresql://postgres@127.0.0.1:$PORT/riverbend"
    ;;
  stop) "$BIN/pg_ctl" -D "$DIR/data" -m fast stop ;;
  status) "$BIN/pg_ctl" -D "$DIR/data" status ;;
esac
