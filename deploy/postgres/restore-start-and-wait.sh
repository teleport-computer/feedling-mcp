#!/bin/bash
# Start a prepared PostgreSQL restore and wait for archive recovery to finish.
# pg_ctl -w only means the server accepts connections; during archive recovery
# that can be a read-only server with pg_is_in_recovery() still true.
set -euo pipefail

PGDATA="${PGDATA:-/var/lib/postgresql/data}"
RESTORE_OWNER_USER="${RESTORE_OWNER_USER:-feedling_owner}"
RESTORE_DATABASE="${RESTORE_DATABASE:-feedling}"
RESTORE_SOCKET_DIR="${RESTORE_SOCKET_DIR:-/tmp}"
RESTORE_LOG_PATH="${RESTORE_LOG_PATH:-/tmp/postgres-restore.log}"
RESTORE_RECOVERY_TIMEOUT_SEC="${RESTORE_RECOVERY_TIMEOUT_SEC:-1800}"
RESTORE_POLL_INTERVAL_SEC="${RESTORE_POLL_INTERVAL_SEC:-2}"

case "$RESTORE_RECOVERY_TIMEOUT_SEC" in
    ''|*[!0-9]*|0) echo "RESTORE_RECOVERY_TIMEOUT_SEC must be a positive integer" >&2; exit 2 ;;
esac
case "$RESTORE_POLL_INTERVAL_SEC" in
    ''|*[!0-9]*|0) echo "RESTORE_POLL_INTERVAL_SEC must be a positive integer" >&2; exit 2 ;;
esac

if [ -n "${PG_BIN_DIR:-}" ]; then
    PGBIN="$PG_BIN_DIR"
elif command -v pg_config >/dev/null 2>&1; then
    PGBIN="$(pg_config --bindir)"
elif command -v pg_controldata >/dev/null 2>&1; then
    PGBIN="$(dirname "$(command -v pg_controldata)")"
elif command -v postgres >/dev/null 2>&1; then
    PGBIN="$(dirname "$(command -v postgres)")"
else
    echo "RESTORE_POSTGRES_BIN_NOT_FOUND: set PG_BIN_DIR" >&2
    exit 1
fi
export PATH="$PGBIN:$PATH"
mkdir -p "$RESTORE_SOCKET_DIR"

started_at="$(date +%s)"
pg_ctl -D "$PGDATA" -l "$RESTORE_LOG_PATH" \
    -o "-c listen_addresses='' -c unix_socket_directories=$RESTORE_SOCKET_DIR" \
    start -w -t "$RESTORE_RECOVERY_TIMEOUT_SEC"

while true; do
    if ! recovery_state="$(
        psql -X -A -t -h "$RESTORE_SOCKET_DIR" -U "$RESTORE_OWNER_USER" \
            -d "$RESTORE_DATABASE" -c 'SELECT pg_is_in_recovery();' \
            | tr -d '[:space:]'
    )"; then
        echo "RESTORE_RECOVERY_CHECK_FAILED" >&2
        [ ! -f "$RESTORE_LOG_PATH" ] || tail -n 120 "$RESTORE_LOG_PATH" >&2
        exit 1
    fi

    now="$(date +%s)"
    elapsed="$((now-started_at))"
    case "$recovery_state" in
        f)
            echo "RESTORE_RECOVERY_COMPLETE elapsed_seconds=$elapsed"
            exit 0
            ;;
        t)
            if [ "$elapsed" -ge "$RESTORE_RECOVERY_TIMEOUT_SEC" ]; then
                echo "RESTORE_RECOVERY_TIMEOUT elapsed_seconds=$elapsed" >&2
                [ ! -f "$RESTORE_LOG_PATH" ] || tail -n 120 "$RESTORE_LOG_PATH" >&2
                exit 1
            fi
            ;;
        *)
            echo "RESTORE_RECOVERY_INVALID_STATE value=$recovery_state" >&2
            exit 1
            ;;
    esac
    sleep "$RESTORE_POLL_INTERVAL_SEC"
done
