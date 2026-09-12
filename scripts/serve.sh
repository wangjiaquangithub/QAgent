#!/usr/bin/env bash
#
# start.sh - Start all QAgent development services
#
# Must be run from the repo root directory.

set -e

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# ── Load environment variables from .env ──────────────────────────────────────
if [ -f "$REPO_ROOT/.env" ]; then
    set -a
    source "$REPO_ROOT/.env"
    set +a
fi

# ── Argument parsing ─────────────────────────────────────────────────────────

DEV_MODE=true

UV_BIN="${UV_BIN:-uv}"
if [ -x "$HOME/.local/bin/uv" ]; then
    UV_BIN="$HOME/.local/bin/uv"
fi

# In WSL/Linux, avoid reusing Windows-created backend/.venv.
# WSL + repo on /mnt/*: put the venv on Linux ext4 ($HOME) — uv sync on drvfs is too slow and
# LangGraph may miss the wait-for-port deadline.
# Note: .env may set UV_PROJECT_ENVIRONMENT=.venv-wsl; that still lives on /mnt and stays slow, so
# we force the native path unless EVOFLOW_SKIP_NATIVE_WSL_VENV=1.
_DEFAULT_UV_ENV=".venv-wsl"
if [ -f /proc/version ] && grep -qi microsoft /proc/version 2>/dev/null; then
    case "$REPO_ROOT" in
        /mnt/*)
            _DEFAULT_UV_ENV="${HOME}/.venvs/evoflow-backend"
            mkdir -p "$(dirname "$_DEFAULT_UV_ENV")"
            ;;
    esac
fi
export UV_PROJECT_ENVIRONMENT="${UV_PROJECT_ENVIRONMENT:-$_DEFAULT_UV_ENV}"
export UV_LINK_MODE="${UV_LINK_MODE:-copy}"
if [ "${EVOFLOW_SKIP_NATIVE_WSL_VENV:-0}" != "1" ] && [ -f /proc/version ] && grep -qi microsoft /proc/version 2>/dev/null; then
    case "$REPO_ROOT" in
        /mnt/*)
            export UV_PROJECT_ENVIRONMENT="${HOME}/.venvs/evoflow-backend"
            mkdir -p "$(dirname "$UV_PROJECT_ENVIRONMENT")"
            ;;
    esac
fi

# Next.js dev requires Node >=20.9; many WSL images still ship Node 18. Prefer a user install (see scripts/install-node20-wsl.sh).
if [ -x "${HOME}/.local/nodejs/bin/node" ]; then
    _SYS_NODE_MAJ=$(command -v node >/dev/null 2>&1 && node -p "process.versions.node.split('.')[0]" 2>/dev/null || echo 0)
    _LOCAL_NODE_MAJ=$("${HOME}/.local/nodejs/bin/node" -p "process.versions.node.split('.')[0]" 2>/dev/null || echo 0)
    if [ "${_SYS_NODE_MAJ:-0}" -lt 20 ] && [ "${_LOCAL_NODE_MAJ:-0}" -ge 20 ]; then
        export PATH="${HOME}/.local/nodejs/bin:${PATH}"
    fi
fi

mkdir -p logs

# ── Banner ────────────────────────────────────────────────────────────────────

echo ""
echo "=========================================="
echo "  Starting QAgent Development Server"
echo "=========================================="
echo ""
if $DEV_MODE; then
    echo "  Mode: DEV  (hot-reload enabled)"
    echo "  Tip:  run \`make start\` in production mode"
else
    echo "  Mode: PROD (hot-reload disabled)"
    echo "  Tip:  run \`make dev\` to start in development mode"
fi
echo ""
echo "Services starting up..."
echo "  → Backend: LangGraph + Gateway"
echo ""

# ── Config check ─────────────────────────────────────────────────────────────

if ! { \
        [ -n "$EVOFLOW_CONFIG_PATH" ] && [ -f "$EVOFLOW_CONFIG_PATH" ] || \
        [ -f backend/config.yaml ] || \
        [ -f config.yaml ]; \
    }; then
    echo "✗ No QAgent config file found."
    echo "  Checked these locations:"
    echo "    - $EVOFLOW_CONFIG_PATH (when EVOFLOW_CONFIG_PATH is set)"
    echo "    - backend/config.yaml"
    echo "    - ./config.yaml"
    echo ""
    echo "  Run 'make config' from the repo root to generate ./config.yaml, then set required model API keys in .env or your config file."
    exit 1
fi

# ── Auto-upgrade config ──────────────────────────────────────────────────

if [ "${EVOFLOW_SKIP_CONFIG_UPGRADE:-0}" != "1" ]; then
    "$REPO_ROOT/scripts/config-upgrade.sh"
else
    echo "Skipping config upgrade (EVOFLOW_SKIP_CONFIG_UPGRADE=1)."
fi

# ── Cleanup trap ─────────────────────────────────────────────────────────────

# Keep lifecycle state private to this invocation.  In particular, do not use
# `pkill -f`: matching command text can kill an unrelated QAgent session (or a
# shell merely running a diagnostic command containing that text).
RUNTIME_DIR="${TMPDIR:-/tmp}/qagent-serve-$$"
GATEWAY_STOP_FILE="$RUNTIME_DIR/gateway.stop"
GATEWAY_CHILD_PID_FILE="$RUNTIME_DIR/gateway-child.pid"
DAEMON_PID_FILE="${QAGENT_DAEMON_PID_FILE:-}"
mkdir -p "$RUNTIME_DIR"
printf '%s\n' "$REPO_ROOT" > "$RUNTIME_DIR/repo-root"
printf '%s\n' "$$" > "$RUNTIME_DIR/owner.pid"
if [ -n "$DAEMON_PID_FILE" ]; then
    mkdir -p "$(dirname "$DAEMON_PID_FILE")"
    printf '%s\n' "$$" > "$DAEMON_PID_FILE"
fi

# Add a process's descendants in child-first order.  This is deliberately based
# on PIDs captured by this script, so cleanup cannot affect another checkout or
# a separately started QAgent service.
collect_descendants() {
    local parent_pid="$1"
    local child_pid

    command -v pgrep >/dev/null 2>&1 || return 0
    while IFS= read -r child_pid; do
        [ -n "$child_pid" ] || continue
        # Never signal this shell, even if a PID were unexpectedly reused.
        [ "$child_pid" = "$$" ] && continue
        collect_descendants "$child_pid"
        PROCESS_TREE_PIDS+=("$child_pid")
    done < <(pgrep -P "$parent_pid" 2>/dev/null || true)
}

terminate_process_tree() {
    local root_pid="$1"
    local pid attempt still_running

    [ -n "$root_pid" ] || return 0
    kill -0 "$root_pid" 2>/dev/null || return 0
    [ "$root_pid" = "$$" ] && return 0

    PROCESS_TREE_PIDS=()
    collect_descendants "$root_pid"
    PROCESS_TREE_PIDS+=("$root_pid")

    for pid in "${PROCESS_TREE_PIDS[@]}"; do
        kill -TERM "$pid" 2>/dev/null || true
    done

    # A reloader/worker occasionally ignores TERM while blocked.  Escalate only
    # within this invocation's recorded process tree, never via a global match.
    for attempt in 1 2 3 4 5 6 7 8 9 10; do
        still_running=false
        for pid in "${PROCESS_TREE_PIDS[@]}"; do
            if kill -0 "$pid" 2>/dev/null; then
                still_running=true
                break
            fi
        done
        "$still_running" || return 0
        sleep 0.2
    done

    for pid in "${PROCESS_TREE_PIDS[@]}"; do
        kill -KILL "$pid" 2>/dev/null || true
    done
}

CLEANUP_STARTED=false
cleanup() {
    local exit_code="${1:-0}"
    local gateway_child_pid=""

    "$CLEANUP_STARTED" && return
    CLEANUP_STARTED=true
    trap - EXIT INT TERM

    echo ""
    echo "Shutting down services..."
    # Tell the supervisor not to restart a child while its process tree is
    # being terminated, then stop only the processes this script launched.
    : > "$GATEWAY_STOP_FILE"
    # The PID file also covers the narrow case where the supervisor itself has
    # already died and its Gateway child was re-parented before cleanup runs.
    if [ -r "$GATEWAY_CHILD_PID_FILE" ]; then
        IFS= read -r gateway_child_pid < "$GATEWAY_CHILD_PID_FILE" || true
        case "$gateway_child_pid" in
            ''|*[!0-9]*) ;;
            "$$") ;;
            *) terminate_process_tree "$gateway_child_pid" ;;
        esac
    fi
    terminate_process_tree "${GATEWAY_SUPERVISOR_PID:-}"
    terminate_process_tree "${LANGGRAPH_PID:-}"
    if [ -n "$DAEMON_PID_FILE" ] && [ -r "$DAEMON_PID_FILE" ]; then
        local recorded_daemon_pid=""
        IFS= read -r recorded_daemon_pid < "$DAEMON_PID_FILE" || true
        [ "$recorded_daemon_pid" = "$$" ] && rm -f "$DAEMON_PID_FILE"
    fi
    rm -rf "$RUNTIME_DIR"

    echo "Cleaning up sandbox containers..."
    ./scripts/cleanup-containers.sh evo-flow-sandbox 2>/dev/null || true
    echo "✓ All services stopped"
    exit "$exit_code"
}
trap 'cleanup $?' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# ── Start services ────────────────────────────────────────────────────────────

mkdir -p logs

if $DEV_MODE; then
    LANGGRAPH_EXTRA_FLAGS="--no-reload"
    GATEWAY_EXTRA_FLAGS="--reload --reload-include='*.yaml' --reload-include='.env'"
else
    LANGGRAPH_EXTRA_FLAGS="--no-reload"
    GATEWAY_EXTRA_FLAGS=""
fi
GATEWAY_PORT=8012
export BG_JOB_ISOLATED_LOOPS="${BG_JOB_ISOLATED_LOOPS:-true}"
# langgraph dev subprocess defaults N_JOBS_PER_WORKER to 1 unless --n-jobs-per-worker is set
export N_JOBS_PER_WORKER="${N_JOBS_PER_WORKER:-10}"

LOG_DATE="$(date +%Y-%m-%d)"
mkdir -p logs
(cd backend && PYTHONPATH=. "$UV_BIN" run python -c "from app.gateway.logging_setup import prune_old_daily_logs, resolve_gateway_logs_dir; d=resolve_gateway_logs_dir(); prune_old_daily_logs(d,'gateway'); prune_old_daily_logs(d,'langgraph')" 2>/dev/null) || true

echo "Starting LangGraph server..."
# Read log_level from config.yaml, fallback to env var, then to "info"
CONFIG_LOG_LEVEL=$(grep -m1 '^log_level:' config.yaml 2>/dev/null | awk '{print $2}' | tr -d ' ')
LANGGRAPH_LOG_LEVEL="${LANGGRAPH_LOG_LEVEL:-${CONFIG_LOG_LEVEL:-info}}"
(
    cd backend
    exec env NO_COLOR=1 PYTHONUNBUFFERED=1 "$UV_BIN" run langgraph dev --no-browser --allow-blocking \
        --n-jobs-per-worker "$N_JOBS_PER_WORKER" --server-log-level "$LANGGRAPH_LOG_LEVEL" \
        $LANGGRAPH_EXTRA_FLAGS
) > "logs/langgraph-${LOG_DATE}.log" 2>&1 &
LANGGRAPH_PID=$!
./scripts/wait-for-port.sh 2024 240 "LangGraph" || {
    echo "  See logs/langgraph-${LOG_DATE}.log for details"
    tail -20 "../logs/langgraph-${LOG_DATE}.log"
    if grep -qE "config_version|outdated|Environment variable .* not found|KeyError|ValidationError|config\.yaml" "../logs/langgraph-${LOG_DATE}.log" 2>/dev/null; then
        echo ""
        echo "  Hint: This may be a configuration issue. Try running 'make config-upgrade' to update your config.yaml."
    fi
    cleanup 1
}
echo "✓ LangGraph server started on localhost:2024"

echo "Starting Gateway API..."
# Keep Uvicorn in a dedicated supervisor.  In development mode its WatchFiles
# reloader can exit independently while LangGraph remains alive; previously the
# final `wait` then kept this script alive but left port 8012 unserved forever.
run_gateway_supervisor() {
    local exit_code
    local gateway_child_pid

    while [ ! -e "$GATEWAY_STOP_FILE" ]; do
        (
            cd backend
            exec env EVOFLOW_LANGGRAPH_URL="${EVOFLOW_LANGGRAPH_URL:-http://127.0.0.1:2024}" \
                PYTHONUNBUFFERED=1 PYTHONPATH=. "$UV_BIN" run uvicorn app.gateway.app:app \
                --host 0.0.0.0 --port "$GATEWAY_PORT" $GATEWAY_EXTRA_FLAGS
        ) &
        gateway_child_pid=$!
        printf '%s\n' "$gateway_child_pid" > "$GATEWAY_CHILD_PID_FILE"

        # `set -e` would otherwise make a non-zero Uvicorn exit terminate this
        # supervisor before it gets the chance to restart the Gateway.
        if wait "$gateway_child_pid"; then
            exit_code=0
        else
            exit_code=$?
        fi
        rm -f "$GATEWAY_CHILD_PID_FILE"

        [ -e "$GATEWAY_STOP_FILE" ] && break
        echo "⚠ Gateway API exited (status ${exit_code}); restarting in 2 seconds..." >&2
        sleep 2
    done
}
run_gateway_supervisor &
GATEWAY_SUPERVISOR_PID=$!
./scripts/wait-for-port.sh $GATEWAY_PORT 30 "Gateway API" || {
    echo "✗ Gateway API failed to start. Last log output:"
    tail -60 "logs/gateway-${LOG_DATE}.log" 2>/dev/null || tail -60 logs/gateway.log 2>/dev/null || true
    echo ""
    echo "Likely configuration errors:"
    grep -E "Failed to load configuration|Environment variable .* not found|config\.yaml.*not found" "logs/gateway-${LOG_DATE}.log" 2>/dev/null | tail -5 || grep -E "Failed to load configuration|Environment variable .* not found|config\.yaml.*not found" logs/gateway.log 2>/dev/null | tail -5 || true
    echo ""
    echo "  Hint: Try running 'make config-upgrade' to update your config.yaml with the latest fields."
    cleanup 1
}
echo "✓ Gateway API started on localhost:$GATEWAY_PORT"

# ── Ready ─────────────────────────────────────────────────────────────────────

echo ""
echo "=========================================="
if $DEV_MODE; then
    echo "  ✓ QAgent development server is running!"
else
    echo "  ✓ QAgent production server is running!"
fi
echo "=========================================="
echo ""
echo "  🌐 LangGraph:   http://localhost:2024"
echo "  📡 Gateway:     http://localhost:$GATEWAY_PORT"
echo ""
echo "  📋 Logs (daily, 7-day retention):"
echo "     - LangGraph: logs/langgraph-${LOG_DATE}.log"
echo "     - Gateway:   logs/gateway-${LOG_DATE}.log"
echo ""
echo "Press Ctrl+C to stop all services"

wait
