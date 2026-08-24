#!/usr/bin/env bash
set -euo pipefail

fatal() {
    local slug="$1"
    shift
    echo "${slug}: $*" >&2
    exit 1
}

require_value() {
    local name="$1"
    [ -n "${!name:-}" ] || fatal "BACKUP_HEALTH_INPUT_INVALID" "${name} is required"
}

require_uint() {
    local name="$1" value
    require_value "$name"
    value="${!name}"
    case "$value" in
        *[!0-9]*) fatal "BACKUP_HEALTH_INPUT_INVALID" "${name} must be a non-negative integer" ;;
    esac
}

require_wal_name() {
    local name="$1" value
    require_value "$name"
    value="${!name}"
    [[ "$value" =~ ^[0-9A-F]{24}$ ]] \
        || fatal "BACKUP_HEALTH_INPUT_INVALID" "${name} is not a PostgreSQL WAL filename"
}

MAX_READY_AGE_SEC="${MAX_READY_AGE_SEC:-300}"
MAX_WAL_MB="${MAX_WAL_MB:-4096}"
MAX_BASE_AGE_SEC="${MAX_BASE_AGE_SEC:-93600}"

case "${1:-}" in
    archiver)
        require_wal_name LAST_ARCHIVED_WAL
        require_wal_name CURRENT_WAL
        require_uint UNRESOLVED_ARCHIVE_FAILURE
        require_uint READY_COUNT
        require_uint OLDEST_READY_AGE_SEC
        require_uint WAL_MB
        require_uint MAX_READY_AGE_SEC
        require_uint MAX_WAL_MB

        [ "$UNRESOLVED_ARCHIVE_FAILURE" -eq 0 ] \
            || fatal "ARCHIVE_FAILURE_UNRESOLVED" "last failure is newer than last success"
        if [ "$READY_COUNT" -gt 0 ] \
            && [ "$OLDEST_READY_AGE_SEC" -ge "$MAX_READY_AGE_SEC" ]; then
            fatal "ARCHIVE_READY_STALE" \
                "ready_count=${READY_COUNT} oldest_age=${OLDEST_READY_AGE_SEC}s"
        fi
        [ "$WAL_MB" -lt "$MAX_WAL_MB" ] \
            || fatal "WAL_DIR_TOO_LARGE" "pg_wal=${WAL_MB}MB limit=${MAX_WAL_MB}MB"

        echo "ARCHIVER_HEALTH_OK last=${LAST_ARCHIVED_WAL} current=${CURRENT_WAL} ready=${READY_COUNT} wal_mb=${WAL_MB}"
        ;;
    r2)
        require_wal_name LAST_ARCHIVED_WAL
        require_value NEWEST_WAL_KEY
        require_uint BASE_AGE_SEC
        require_uint MAX_BASE_AGE_SEC

        case "$NEWEST_WAL_KEY" in
            */"${LAST_ARCHIVED_WAL}.lz4") ;;
            *) fatal "R2_WAL_MISMATCH" \
                "database WAL ${LAST_ARCHIVED_WAL} is not the verified R2 object" ;;
        esac
        [ "$BASE_AGE_SEC" -lt "$MAX_BASE_AGE_SEC" ] \
            || fatal "BASE_BACKUP_STALE" \
                "base_age=${BASE_AGE_SEC}s limit=${MAX_BASE_AGE_SEC}s"

        echo "R2_BACKUP_HEALTH_OK last=${LAST_ARCHIVED_WAL} base_age=${BASE_AGE_SEC}s"
        ;;
    *)
        echo "usage: $0 <archiver|r2>" >&2
        exit 2
        ;;
esac
