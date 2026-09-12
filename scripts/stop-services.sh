#!/usr/bin/env bash
# Stop QAgent services started by this checkout's scripts/serve.sh only.
#
# Runtime state is written by serve.sh into a private qagent-serve-* directory.
# We validate both the recorded repository and the live command line before
# signalling a PID, avoiding unsafe command-text-wide pkill patterns.

set -u

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
QUIET=false
if [ "${1:-}" = "--quiet" ]; then
    QUIET=true
elif [ "$#" -gt 0 ]; then
    echo "Usage: $0 [--quiet]" >&2
    exit 2
fi

say() {
    "$QUIET" || echo "$*"
}

is_serve_owner() {
    local pid="$1"
    local command_line

    kill -0 "$pid" 2>/dev/null || return 1
    command_line="$(ps -ww -p "$pid" -o command= 2>/dev/null || true)"
    case "$command_line" in
        *"$REPO_ROOT/scripts/serve.sh"*) return 0 ;;
        *) return 1 ;;
    esac
}

wait_for_exit() {
    local pid="$1"
    local attempt

    for attempt in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
        kill -0 "$pid" 2>/dev/null || return 0
        sleep 0.2
    done
    return 1
}

owner_pids=()
for runtime_dir in "${TMPDIR:-/tmp}"/qagent-serve-*; do
    [ -d "$runtime_dir" ] || continue
    [ -r "$runtime_dir/repo-root" ] || continue

    recorded_root="$(cat "$runtime_dir/repo-root" 2>/dev/null || true)"
    [ "$recorded_root" = "$REPO_ROOT" ] || continue
    [ -r "$runtime_dir/owner.pid" ] || continue

    owner_pid="$(cat "$runtime_dir/owner.pid" 2>/dev/null || true)"
    case "$owner_pid" in
        ''|*[!0-9]*)
            rm -rf "$runtime_dir"
            continue
            ;;
    esac

    if is_serve_owner "$owner_pid"; then
        owner_pids+=("$owner_pid")
    else
        # PID has exited or been reused.  Its private state cannot manage a
        # live QAgent serve owner, so discard only that stale state directory.
        rm -rf "$runtime_dir"
    fi
done

if [ "${#owner_pids[@]}" -eq 0 ]; then
    say "No QAgent services from this checkout are running."
    exit 0
fi

status=0
for owner_pid in "${owner_pids[@]}"; do
    say "Stopping QAgent service owner $owner_pid..."
    kill -TERM "$owner_pid" 2>/dev/null || true
done

for owner_pid in "${owner_pids[@]}"; do
    if wait_for_exit "$owner_pid"; then
        say "✓ QAgent service owner $owner_pid stopped"
    else
        echo "✗ QAgent service owner $owner_pid did not exit cleanly; refusing to kill unrelated processes." >&2
        status=1
    fi
done

exit "$status"
