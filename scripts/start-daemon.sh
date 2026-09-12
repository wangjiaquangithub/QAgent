#!/usr/bin/env bash
# Start the normal supervised QAgent development stack in the background.
# The previous daemon implementation duplicated an obsolete port-8001 startup
# path and used broad pkill -f cleanup; delegating to serve.sh keeps the daemon
# on the same restart and ownership model as foreground development.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

mkdir -p logs
LOG_DATE="$(date +%Y-%m-%d)"
PID_FILE="$REPO_ROOT/logs/qagent-daemon.pid"
LOG_FILE="$REPO_ROOT/logs/serve-${LOG_DATE}.log"

# Only stop owners recorded by this checkout.  Do not match arbitrary command
# lines: a second checkout or a diagnostic shell must remain untouched.
"$REPO_ROOT/scripts/stop-services.sh" --quiet

rm -f "$PID_FILE"
echo "Starting QAgent daemon (supervised Gateway on localhost:8012)..."
nohup env QAGENT_DAEMON_PID_FILE="$PID_FILE" "$REPO_ROOT/scripts/serve.sh" --dev \
    > "$LOG_FILE" 2>&1 < /dev/null &
daemon_pid=$!

# serve.sh writes this PID itself after its private lifecycle state exists.
# Keep the early write as a fallback for an immediately interrupted launch.
printf '%s\n' "$daemon_pid" > "$PID_FILE"

if ! "$REPO_ROOT/scripts/wait-for-port.sh" 2024 240 "LangGraph"; then
    echo "✗ LangGraph failed to start. Last log output:" >&2
    tail -60 "$LOG_FILE" >&2 || true
    "$REPO_ROOT/scripts/stop-services.sh" --quiet || true
    exit 1
fi

if ! "$REPO_ROOT/scripts/wait-for-port.sh" 8012 30 "Gateway API"; then
    echo "✗ Gateway API failed to start. Last log output:" >&2
    tail -60 "$LOG_FILE" >&2 || true
    "$REPO_ROOT/scripts/stop-services.sh" --quiet || true
    exit 1
fi

echo "✓ QAgent daemon is running"
echo "  🌐 LangGraph: http://localhost:2024"
echo "  📡 Gateway:   http://localhost:8012"
echo "  📋 Logs:      $LOG_FILE"
echo "  🛑 Stop:      make stop"
