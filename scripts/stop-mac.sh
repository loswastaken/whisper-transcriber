#!/usr/bin/env bash
# Stop the Transcriber started by scripts/run-mac.sh (SIGTERM to the pid in its
# pidfile, only if that pid is still a uvicorn process).
set -euo pipefail

APP_HOME="${APP_HOME:-$HOME/Apps/transcriber}"
PIDFILE="$APP_HOME/transcriber.pid"

if [[ ! -f "$PIDFILE" ]]; then
  echo "Not running (no $PIDFILE)."
  exit 0
fi

pid="$(cat "$PIDFILE" 2>/dev/null || true)"
if [[ ! "$pid" =~ ^[0-9]+$ ]]; then
  echo "Removing unreadable pidfile $PIDFILE."
  rm -f "$PIDFILE"
  exit 1
fi

cmd="$(ps -o command= -p "$pid" 2>/dev/null || true)"
case "$cmd" in
  *uvicorn*app.main:app*) ;;
  "")
    echo "Not running (pid $pid is gone); removing stale pidfile."
    rm -f "$PIDFILE"
    exit 0
    ;;
  *)
    echo "pid $pid is not Transcriber ($cmd); leaving it alone and removing stale pidfile."
    rm -f "$PIDFILE"
    exit 0
    ;;
esac

kill -TERM "$pid"
for _ in $(seq 1 50); do
  kill -0 "$pid" 2>/dev/null || break
  sleep 0.2
done
rm -f "$PIDFILE"
if kill -0 "$pid" 2>/dev/null; then
  echo "Sent SIGTERM to $pid; it is still shutting down."
else
  echo "Stopped (pid $pid)."
fi
