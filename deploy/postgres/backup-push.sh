#!/bin/bash
# backup-push.sh — cron 每日 03:00 UTC
set -euo pipefail
if [ -f /etc/environment.walg ]; then set -a; . /etc/environment.walg; set +a; fi
: "${PGDATA:=/var/lib/postgresql/data}"
echo "[backup] start $(date -u +%FT%TZ)"
wal-g backup-push "$PGDATA"
wal-g delete retain FULL 7 --confirm
echo "[backup] done $(date -u +%FT%TZ)"
