#!/bin/bash
# backup-push.sh — cron 每日 03:00 UTC
# cron 以精简 PATH(/usr/bin:/bin)执行本脚本，不含 wal-g 所在的 /usr/local/bin，
# 裸调 `wal-g` 会 "command not found"——archive_command(wal-push)不受影响是因为
# postgres 进程自带完整 PATH，但本 cron 脚本没有。故显式补 PATH，让 backup-push /
# delete retain 在 cron 环境下都能找到 wal-g。
# 2026-07-14 实测：此前每日 03:00 base backup 全部失败于此，prod/test 都只剩建库
# 时的那一个 base，且 `wal-g delete retain` 从没跑成 → WAL 在 R2 无限堆积。
export PATH=/usr/local/bin:/usr/bin:/bin
set -euo pipefail
if [ -f /etc/environment.walg ]; then set -a; . /etc/environment.walg; set +a; fi
: "${PGDATA:=/var/lib/postgresql/data}"
echo "[backup] start $(date -u +%FT%TZ)"
wal-g backup-push "$PGDATA"
wal-g delete retain FULL 7 --confirm
echo "[backup] done $(date -u +%FT%TZ)"
